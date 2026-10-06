// render.tsx <brief.json> <out.html> <out.txt>
// Called by `mitsync email`. Reads the enriched brief, writes the HTML
// dashboard and its plain-text alternative. Exits non-zero on any error so
// the caller never sends a half-rendered email.
import { readFileSync, writeFileSync } from "node:fs";
import { render, toPlainText } from "@react-email/render";
import { MorningBrief, type Brief } from "./MorningBrief";

const [input, htmlOut, textOut] = process.argv.slice(2);
if (!input || !htmlOut || !textOut) {
  console.error("usage: render.tsx <brief.json> <out.html> <out.txt>");
  process.exit(2);
}
const brief = JSON.parse(readFileSync(input, "utf8")) as Brief;
const html = await render(<MorningBrief brief={brief} />);
writeFileSync(htmlOut, html);
writeFileSync(textOut, toPlainText(html));
