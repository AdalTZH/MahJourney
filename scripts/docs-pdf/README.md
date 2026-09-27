# docs-pdf

Renders a Markdown document in `docs/` to a print-quality PDF, including its
Mermaid diagrams.

Uses the Chrome (or Edge) already installed on the machine via `puppeteer-core`,
so nothing downloads a bundled Chromium. Mermaid is rendered in-browser from the
self-contained bundle in `node_modules`, so no network access is required at
render time.

## Usage

```powershell
cd scripts/docs-pdf
npm install

node build.mjs `
  --in ../../docs/TECHNICAL_DESIGN.md `
  --out ../../docs/MahJourney-Technical-Design.pdf `
  --docid "MahJourney" `
  --subtitle "Agentic dispatch architecture: multi-agent design, guardrails, human-in-the-loop controls, and memory model"
```

Arguments:

| Flag | Required | Meaning |
|---|---|---|
| `--in` | yes | Source Markdown, resolved relative to this folder |
| `--out` | yes | Destination PDF |
| `--title` | no | Cover title; defaults to the document's leading `#` heading |
| `--subtitle` | no | Cover subtitle |
| `--docid` | no | Small uppercase eyebrow line above the title |

Set `CHROME_PATH` if Chrome and Edge are both in non-standard locations.

## What it produces

- A cover page, an auto-generated table of contents from the `##`/`###`
  headings, and one page break before each top-level section.
- Syntax-highlighted code blocks (highlight.js) and print-styled tables that
  avoid breaking across pages.
- Mermaid diagrams rendered to vector SVG, fitted to a 192mm column and
  height-capped so no diagram can exceed a single printable page.

Exit code is `1` if any diagram failed to parse; the failing definition is
printed and also boxed in red in the PDF so it cannot slip through unnoticed.

## Checking diagram legibility

Wide diagrams get scaled down to fit A4, which can make their labels too small
to read in print. After a build, `measure.mjs` reports each diagram's intrinsic
size and the resulting effective font size:

```powershell
node measure.mjs
```

Anything flagged `TIGHT` or `TOO SMALL` should be restructured in the Markdown
source — made narrower and taller, or split into two diagrams — rather than left
to shrink.
