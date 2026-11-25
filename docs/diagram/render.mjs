// Rewrite the exported SVG's font stack and re-render the PNG from it.
//
//   node render.mjs <plugin scripts dir> <name>     (reads and rewrites <name>.svg, writes <name>.png)
//
// The plugin's export.mjs lists "Segoe UI Emoji" in its font stack. On Linux
// that resolves to Noto Color Emoji, which draws digits as emoji bitmaps.
// One plain stack for every text fixes it. sharp comes from the plugin's own
// node_modules, so this project installs nothing for it.
import { readFileSync, writeFileSync } from "node:fs";
import { createRequire } from "node:module";

const [scriptsDir, name] = process.argv.slice(2);
if (!scriptsDir || !name) {
  console.error("usage: node render.mjs <plugin scripts dir> <name>");
  process.exit(1);
}
const sharp = createRequire(scriptsDir + "/")("sharp");
// Arrow labels (16 px, dark) get a halo of the process group's background colour, so a line passing
// under a label is hidden there instead of striking the text through (panels.py places the labels).
const svg = readFileSync(`${name}.svg`, "utf8")
  .replaceAll(
    'font-family="Virgil, Segoe UI Emoji, Helvetica, Arial, sans-serif"',
    'font-family="Helvetica, Arial, sans-serif"',
  )
  .replaceAll(
    'font-size="16" fill="#1e1e1e" text-anchor=',
    'font-size="16" fill="#1e1e1e" stroke="#f9fafb" stroke-width="8" stroke-linejoin="round" paint-order="stroke" text-anchor=',
  );
writeFileSync(`${name}.svg`, svg.endsWith("\n") ? svg : `${svg}\n`);
await sharp(Buffer.from(svg), { density: 144 }).png().toFile(`${name}.png`);
console.log(`rendered ${name}.svg and ${name}.png`);
