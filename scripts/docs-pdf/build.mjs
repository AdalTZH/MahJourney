/**
 * Renders a Markdown document (including Mermaid diagrams) to PDF using the
 * locally installed Chrome via puppeteer-core.
 *
 * Mermaid is rendered in-browser from the self-contained IIFE bundle
 * (dist/mermaid.min.js sets globalThis.mermaid), so no network access and no
 * bundled Chromium download are needed. The page exposes
 * window.__RENDER_STATE__ so this script can wait for real completion instead
 * of guessing at a timeout, and can surface per-diagram parse errors.
 *
 * Usage:
 *   node build.mjs --in ../../docs/FOO.md --out ../../docs/FOO.pdf \
 *                  --title "Doc title" --subtitle "Sub" --docid "MahJourney"
 */

import { createRequire } from "node:module";
import { fileURLToPath, pathToFileURL } from "node:url";
import fs from "node:fs";
import path from "node:path";

import MarkdownIt from "markdown-it";
import anchor from "markdown-it-anchor";
import hljs from "highlight.js";
import puppeteer from "puppeteer-core";

const require = createRequire(import.meta.url);
const HERE = path.dirname(fileURLToPath(import.meta.url));

// ---------------------------------------------------------------------------
// Arguments
// ---------------------------------------------------------------------------

function parseArgs(argv) {
  const out = {};
  for (let i = 0; i < argv.length; i += 1) {
    const token = argv[i];
    if (!token.startsWith("--")) continue;
    const key = token.slice(2);
    const next = argv[i + 1];
    if (next === undefined || next.startsWith("--")) {
      out[key] = true;
    } else {
      out[key] = next;
      i += 1;
    }
  }
  return out;
}

const args = parseArgs(process.argv.slice(2));
if (!args.in || !args.out) {
  console.error("usage: node build.mjs --in <file.md> --out <file.pdf>");
  process.exit(2);
}

const inputPath = path.resolve(HERE, args.in);
const outputPath = path.resolve(HERE, args.out);
if (!fs.existsSync(inputPath)) {
  console.error(`input not found: ${inputPath}`);
  process.exit(2);
}

// ---------------------------------------------------------------------------
// Locate Chrome
// ---------------------------------------------------------------------------

function findChrome() {
  const candidates = [
    process.env.CHROME_PATH,
    "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
    "C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe",
    "C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe",
    "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe",
  ].filter(Boolean);
  for (const candidate of candidates) {
    if (fs.existsSync(candidate)) return candidate;
  }
  throw new Error("no Chrome or Edge executable found; set CHROME_PATH");
}

// ---------------------------------------------------------------------------
// Markdown -> HTML
// ---------------------------------------------------------------------------

const source = fs.readFileSync(inputPath, "utf8");

// Pull the leading H1 out of the body: it becomes the cover title instead of
// being rendered twice.
const lines = source.split(/\r?\n/);
let coverTitle = args.title || path.basename(inputPath);
let bodyStart = 0;
for (let i = 0; i < lines.length; i += 1) {
  const match = /^#\s+(.*)$/.exec(lines[i]);
  if (match) {
    if (!args.title) coverTitle = match[1].trim();
    bodyStart = i + 1;
    break;
  }
  if (lines[i].trim() !== "") break;
}
const body = lines.slice(bodyStart).join("\n");

const mermaidBlocks = [];

const md = new MarkdownIt({
  html: true,
  linkify: false,
  typographer: false,
  highlight(code, language) {
    if (language && hljs.getLanguage(language)) {
      try {
        return `<pre class="hljs"><code>${
          hljs.highlight(code, { language, ignoreIllegals: true }).value
        }</code></pre>`;
      } catch {
        /* fall through to escaped output */
      }
    }
    return `<pre class="hljs"><code>${md.utils.escapeHtml(code)}</code></pre>`;
  },
});

md.use(anchor, { level: [2, 3], slugify: (s) => slugify(s) });

function slugify(text) {
  return text
    .toLowerCase()
    .replace(/[^\w\s-]/g, "")
    .trim()
    .replace(/\s+/g, "-");
}

// Intercept ```mermaid fences: emit a placeholder div that the browser fills in.
const defaultFence = md.renderer.rules.fence;
md.renderer.rules.fence = function (tokens, idx, options, env, self) {
  const token = tokens[idx];
  const info = (token.info || "").trim().toLowerCase();
  if (info === "mermaid") {
    const id = mermaidBlocks.length;
    mermaidBlocks.push(token.content);
    return (
      `<figure class="diagram">` +
      `<pre class="mermaid" id="mmd-${id}">${md.utils.escapeHtml(token.content)}</pre>` +
      `</figure>\n`
    );
  }
  return defaultFence(tokens, idx, options, env, self);
};

// Build a table of contents from H2/H3.
const tokens = md.parse(body, {});
const toc = [];
for (let i = 0; i < tokens.length; i += 1) {
  const token = tokens[i];
  if (token.type !== "heading_open") continue;
  const level = Number(token.tag.slice(1));
  if (level !== 2 && level !== 3) continue;
  const inline = tokens[i + 1];
  const text = inline && inline.type === "inline" ? inline.content.replace(/`/g, "") : "";
  if (!text) continue;
  toc.push({ level, text, id: slugify(text) });
}

const renderedBody = md.render(body);

const tocHtml = toc
  .map(
    (entry) =>
      `<li class="toc-l${entry.level}"><a href="#${entry.id}">` +
      `<span class="toc-text">${md.utils.escapeHtml(entry.text)}</span>` +
      `</a></li>`,
  )
  .join("\n");

// ---------------------------------------------------------------------------
// Assets
// ---------------------------------------------------------------------------

const assetsDir = path.join(HERE, "assets");
fs.mkdirSync(assetsDir, { recursive: true });
fs.copyFileSync(
  require.resolve("mermaid/dist/mermaid.min.js"),
  path.join(assetsDir, "mermaid.min.js"),
);
fs.copyFileSync(
  require.resolve("highlight.js/styles/github.css"),
  path.join(assetsDir, "highlight.css"),
);

const generated = new Date().toISOString().slice(0, 10);
const subtitle = typeof args.subtitle === "string" ? args.subtitle : "";
const docId = typeof args.docid === "string" ? args.docid : "";

const html = `<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>${md.utils.escapeHtml(coverTitle)}</title>
<link rel="stylesheet" href="./assets/highlight.css">
<style>
  @page { size: A4; }

  :root {
    --ink: #1a1f26;
    --muted: #5b6672;
    --rule: #d4dae1;
    --accent: #14507a;
    --accent-soft: #eef4f9;
    --code-bg: #f6f8fa;
  }

  * { box-sizing: border-box; }

  html { -webkit-print-color-adjust: exact; print-color-adjust: exact; }

  body {
    margin: 0;
    font-family: "Segoe UI", system-ui, -apple-system, "Helvetica Neue", Arial, sans-serif;
    font-size: 9.8pt;
    line-height: 1.55;
    color: var(--ink);
  }

  /* ---------- cover ---------- */
  .cover {
    height: 247mm;
    display: flex;
    flex-direction: column;
    justify-content: center;
    page-break-after: always;
    break-after: page;
  }
  .cover .eyebrow {
    font-size: 8.5pt;
    letter-spacing: .16em;
    text-transform: uppercase;
    color: var(--accent);
    font-weight: 600;
  }
  .cover h1 {
    font-size: 27pt;
    line-height: 1.16;
    margin: 12mm 0 0;
    font-weight: 650;
    letter-spacing: -0.01em;
    border: 0;
    padding: 0;
  }
  .cover .sub {
    font-size: 11.5pt;
    color: var(--muted);
    margin-top: 6mm;
    max-width: 130mm;
  }
  .cover .rule {
    height: 3px;
    width: 46mm;
    background: var(--accent);
    margin: 12mm 0;
  }
  .cover dl {
    display: grid;
    grid-template-columns: 34mm 1fr;
    row-gap: 2.2mm;
    margin: 0;
    font-size: 9.4pt;
  }
  .cover dt { color: var(--muted); }
  .cover dd { margin: 0; font-weight: 550; }

  /* ---------- table of contents ---------- */
  .toc {
    page-break-after: always;
    break-after: page;
  }
  .toc h2 {
    font-size: 15pt;
    margin: 0 0 7mm;
    padding: 0 0 3mm;
    border-bottom: 2px solid var(--accent);
  }
  .toc ol { list-style: none; margin: 0; padding: 0; }
  .toc li { margin: 0; padding: 1.1mm 0; }
  .toc a { color: var(--ink); text-decoration: none; display: block; }
  .toc .toc-l2 { font-weight: 600; border-bottom: 1px dotted var(--rule); }
  .toc .toc-l3 { padding-left: 8mm; font-weight: 400; color: var(--muted); font-size: 9.2pt; }

  /* ---------- headings ---------- */
  h1, h2, h3, h4 {
    font-weight: 640;
    line-height: 1.25;
    page-break-after: avoid;
    break-after: avoid;
    letter-spacing: -0.005em;
  }
  h2 {
    font-size: 15pt;
    margin: 0 0 5mm;
    padding-bottom: 2.6mm;
    border-bottom: 2px solid var(--accent);
    page-break-before: always;
    break-before: page;
  }
  /* The first section must not push a blank page after the ToC. */
  .content > h2:first-child { page-break-before: avoid; break-before: avoid; }
  h3 { font-size: 11.6pt; margin: 7mm 0 2.5mm; color: var(--accent); }
  h4 { font-size: 10pt; margin: 5mm 0 2mm; }

  p { margin: 0 0 3mm; orphans: 3; widows: 3; }

  a { color: var(--accent); text-decoration: none; }

  strong { font-weight: 640; }

  ul, ol { margin: 0 0 3.5mm; padding-left: 6mm; }
  li { margin: 0 0 1.4mm; }

  hr { display: none; }

  blockquote {
    margin: 4mm 0;
    padding: 3mm 5mm;
    background: var(--accent-soft);
    border-left: 3px solid var(--accent);
    page-break-inside: avoid;
    break-inside: avoid;
  }
  blockquote p:last-child { margin-bottom: 0; }

  /* ---------- code ---------- */
  code {
    font-family: "Cascadia Mono", Consolas, "Courier New", monospace;
    font-size: 8.4pt;
    background: var(--code-bg);
    border: 1px solid var(--rule);
    border-radius: 3px;
    padding: 0.4mm 1mm;
  }
  pre {
    background: var(--code-bg);
    border: 1px solid var(--rule);
    border-radius: 4px;
    padding: 3mm 3.5mm;
    margin: 0 0 4mm;
    overflow: visible;
    white-space: pre-wrap;
    word-break: break-word;
    page-break-inside: avoid;
    break-inside: avoid;
  }
  pre code {
    background: none;
    border: 0;
    padding: 0;
    font-size: 8.1pt;
    line-height: 1.45;
  }

  /* ---------- tables ---------- */
  table {
    width: 100%;
    border-collapse: collapse;
    margin: 0 0 4.5mm;
    font-size: 8.6pt;
    page-break-inside: avoid;
    break-inside: avoid;
  }
  thead { display: table-header-group; }
  th, td {
    border: 1px solid var(--rule);
    padding: 1.6mm 2.2mm;
    text-align: left;
    vertical-align: top;
  }
  th { background: var(--accent-soft); font-weight: 620; }
  tbody tr:nth-child(even) { background: #fafbfc; }
  td code, th code { font-size: 7.9pt; }

  /* ---------- diagrams ---------- */
  figure.diagram {
    /* Bleed 8mm into each print margin: diagrams get a 192mm column instead of
       176mm, which measurably improves legibility after down-scaling. */
    margin: 5mm -8mm 6mm;
    padding: 4mm 2mm;
    text-align: center;
    background: #fcfdfe;
    border: 1px solid var(--rule);
    border-radius: 4px;
    page-break-inside: avoid;
    break-inside: avoid;
  }
  figure.diagram pre {
    background: none;
    border: 0;
    padding: 0;
    margin: 0;
    white-space: normal;
  }
  figure.diagram svg { max-width: 100%; height: auto; }
  figure.diagram.render-failed {
    background: #fff6f6;
    border-color: #e3b7b7;
    text-align: left;
  }
  figure.diagram.render-failed pre { white-space: pre-wrap; font-size: 7.6pt; }
</style>
</head>
<body>

<section class="cover">
  ${docId ? `<div class="eyebrow">${md.utils.escapeHtml(docId)}</div>` : ""}
  <h1>${md.utils.escapeHtml(coverTitle)}</h1>
  ${subtitle ? `<div class="sub">${md.utils.escapeHtml(subtitle)}</div>` : ""}
  <div class="rule"></div>
  <dl>
    <dt>Document</dt><dd>Technical Design Document</dd>
    <dt>Status</dt><dd>Implemented — reflects code at time of writing</dd>
    <dt>Generated</dt><dd>${generated}</dd>
    <dt>Source</dt><dd>docs/${md.utils.escapeHtml(path.basename(inputPath))}</dd>
  </dl>
</section>

<nav class="toc">
  <h2>Contents</h2>
  <ol>
${tocHtml}
  </ol>
</nav>

<main class="content">
${renderedBody}
</main>

<script src="./assets/mermaid.min.js"></script>
<script>
  window.__RENDER_STATE__ = { done: false, total: 0, ok: 0, errors: [], heightCapped: [] };

  (async () => {
    const state = window.__RENDER_STATE__;
    try {
      const nodes = Array.from(document.querySelectorAll("pre.mermaid"));
      state.total = nodes.length;

      mermaid.initialize({
        startOnLoad: false,
        theme: "neutral",
        securityLevel: "loose",
        fontFamily: '"Segoe UI", system-ui, Arial, sans-serif',
        flowchart: { useMaxWidth: true, htmlLabels: true, curve: "basis", nodeSpacing: 40, rankSpacing: 46 },
        // Tighter actor boxes and gutters keep sequence diagrams inside the
        // printable column instead of forcing a heavy down-scale.
        sequence: {
          useMaxWidth: true,
          wrap: true,
          width: 104,
          actorMargin: 20,
          boxMargin: 8,
          messageFontSize: 13,
          actorFontSize: 13,
        },
        er: { useMaxWidth: true },
        state: { useMaxWidth: true },
      });

      // A diagram must never be taller than one printable page, otherwise
      // page-break-inside:avoid pushes it onto its own page and it is clipped.
      // 192mm column, 238mm usable height, at 96dpi.
      const COLUMN_PX = 726;
      const MAX_HEIGHT_PX = 900;

      const fit = (svg) => {
        const box = svg.viewBox && svg.viewBox.baseVal;
        if (!box || !box.width || !box.height) return;
        const widthScale = Math.min(1, COLUMN_PX / box.width);
        const heightScale = MAX_HEIGHT_PX / (box.height * widthScale);
        if (heightScale >= 1) return;
        // Height-bound: pin an explicit width so the aspect ratio carries the
        // height under the page limit.
        const finalWidth = Math.floor(box.width * widthScale * heightScale);
        svg.style.width = finalWidth + "px";
        svg.style.maxWidth = finalWidth + "px";
        state.heightCapped.push({ id: svg.id, width: finalWidth });
      };

      // Render one at a time so a single bad diagram is isolated and reported
      // rather than aborting the whole document.
      for (const node of nodes) {
        const definition = node.textContent;
        try {
          const { svg } = await mermaid.render("svg-" + node.id, definition);
          node.innerHTML = svg;
          const element = node.querySelector("svg");
          if (element) fit(element);
          state.ok += 1;
        } catch (error) {
          state.errors.push({ id: node.id, message: String(error && error.message || error) });
          node.closest("figure").classList.add("render-failed");
          node.textContent = "DIAGRAM FAILED TO RENDER\\n\\n" + definition;
        }
      }
    } catch (error) {
      state.errors.push({ id: "*", message: String(error && error.message || error) });
    } finally {
      // Let fonts settle before the PDF snapshot is taken.
      if (document.fonts && document.fonts.ready) {
        try { await document.fonts.ready; } catch {}
      }
      window.__RENDER_STATE__.done = true;
    }
  })();
</script>
</body>
</html>
`;

const htmlPath = path.join(HERE, "render.html");
fs.writeFileSync(htmlPath, html, "utf8");
console.log(`html written: ${htmlPath} (${mermaidBlocks.length} mermaid blocks, ${toc.length} toc entries)`);

// ---------------------------------------------------------------------------
// Print
// ---------------------------------------------------------------------------

const chrome = findChrome();
console.log(`chrome: ${chrome}`);

const browser = await puppeteer.launch({
  executablePath: chrome,
  headless: true,
  args: ["--no-sandbox", "--disable-dev-shm-usage", "--font-render-hinting=none"],
});

try {
  const page = await browser.newPage();
  page.on("pageerror", (error) => console.warn(`  page error: ${error.message}`));
  page.on("console", (message) => {
    if (message.type() === "error") console.warn(`  console error: ${message.text()}`);
  });

  await page.goto(pathToFileURL(htmlPath).href, { waitUntil: "load", timeout: 120000 });
  await page.waitForFunction("window.__RENDER_STATE__ && window.__RENDER_STATE__.done", {
    timeout: 180000,
    polling: 250,
  });

  const state = await page.evaluate("window.__RENDER_STATE__");
  console.log(`diagrams: ${state.ok}/${state.total} rendered`);
  if (state.heightCapped && state.heightCapped.length) {
    console.log(
      `height-capped to fit one page: ${state.heightCapped
        .map((entry) => `${entry.id}@${entry.width}px`)
        .join(", ")}`,
    );
  }
  if (state.errors.length) {
    console.error("DIAGRAM ERRORS:");
    for (const error of state.errors) console.error(`  [${error.id}] ${error.message}`);
  }

  await page.emulateMediaType("print");

  const footer = `
    <div style="width:100%;font-family:'Segoe UI',Arial,sans-serif;font-size:7.4pt;
                color:#6b7682;padding:0 14mm;display:flex;justify-content:space-between;">
      <span>${coverTitle.replace(/[<>&]/g, "")}</span>
      <span>Page <span class="pageNumber"></span> of <span class="totalPages"></span></span>
    </div>`;

  await page.pdf({
    path: outputPath,
    format: "A4",
    printBackground: true,
    displayHeaderFooter: true,
    headerTemplate: "<div></div>",
    footerTemplate: footer,
    margin: { top: "16mm", bottom: "16mm", left: "17mm", right: "17mm" },
    preferCSSPageSize: false,
    timeout: 180000,
  });

  const bytes = fs.statSync(outputPath).size;
  console.log(`pdf written: ${outputPath} (${(bytes / 1024).toFixed(0)} KB)`);
  if (state.errors.length) process.exitCode = 1;
} finally {
  await browser.close();
}
