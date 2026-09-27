/**
 * Reports each rendered diagram's intrinsic SVG size and the effective text
 * scale once it is fitted to the A4 text column, so over-wide diagrams that
 * would print illegibly can be identified and restructured.
 */
import { pathToFileURL, fileURLToPath } from "node:url";
import path from "node:path";
import puppeteer from "puppeteer-core";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const COLUMN_PX = 726; // 192mm diagram column (176mm text + 8mm bleed each side) at 96dpi

const browser = await puppeteer.launch({
  executablePath: "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
  headless: true,
  args: ["--no-sandbox"],
});

try {
  const page = await browser.newPage();
  await page.setViewport({ width: 700, height: 990 });
  await page.goto(pathToFileURL(path.join(HERE, "render.html")).href, { waitUntil: "load" });
  await page.waitForFunction("window.__RENDER_STATE__ && window.__RENDER_STATE__.done", {
    timeout: 180000,
    polling: 250,
  });

  const rows = await page.evaluate((columnPx) => {
    return Array.from(document.querySelectorAll("figure.diagram svg")).map((svg, index) => {
      const viewBox = svg.getAttribute("viewBox") || "";
      const parts = viewBox.split(/[\s,]+/).map(Number);
      const width = parts[2] || svg.getBoundingClientRect().width;
      const height = parts[3] || svg.getBoundingClientRect().height;
      const scale = Math.min(1, columnPx / width);
      // Mermaid's default label font is ~16px CSS.
      const effectivePt = 16 * scale * 0.75;
      return {
        n: index + 1,
        w: Math.round(width),
        h: Math.round(height),
        scale: Number(scale.toFixed(3)),
        pt: Number(effectivePt.toFixed(1)),
      };
    });
  }, COLUMN_PX);

  console.log("  #   width  height  scale  effective-pt  verdict");
  for (const row of rows) {
    const verdict = row.pt >= 7 ? "ok" : row.pt >= 5.5 ? "TIGHT" : "TOO SMALL";
    console.log(
      `  ${String(row.n).padStart(2)}  ${String(row.w).padStart(5)}  ${String(row.h).padStart(6)}` +
        `  ${String(row.scale).padStart(5)}  ${String(row.pt).padStart(12)}  ${verdict}`,
    );
  }
} finally {
  await browser.close();
}
