# Diagram sources

The architecture diagram, and how to regenerate it. Each key on it (T, E, D1 to D9, L, S, G) is explained in the [README](../../README.md), which gives the reasons; the diagram only summarises.

The committed `.svg`, `.png` and `.excalidraw` files are complete and usable as they are. Regenerating them needs an external tool, described below; nothing else in the project depends on it.

| File | What it is |
|---|---|
| `architecture.ir.json` | the request flow: boxes, arrows, the process group (edit this for the flow) |
| `panels.py` | the side panels' text, a few box positions, where each arrow label sits, and the session-lookup elbow (edit this for the panels and the layout) |
| `render.mjs` | rewrites the SVG font stack, gives arrow labels a halo, and renders the PNG |
| `build.sh` | runs the steps below in order |
| `architecture-core.excalidraw`, `.svg`, `.png` | the flow alone, embedded in the README |
| `architecture.excalidraw`, `.svg`, `.png` | the full board with the side panels |

The `.excalidraw` files open in [excalidraw.com](https://excalidraw.com) for hand edits, but a rebuild overwrites them; put lasting changes in the two sources.

## Regenerate

The layout and export come from the Excalidraw plugin for Claude Code, version 1.0.0: its `generate.mjs`, `export.mjs` and their `node_modules` (which include `dagre` and `sharp`). They are not part of this project. Pass the directory holding them:

```sh
docs/diagram/build.sh <plugin dir>/skills/excalidraw/scripts
```

It needs Node and `python3`. The steps:

1. `generate.mjs` lays out `architecture.ir.json` into `architecture-core.excalidraw`.
2. `panels.py` sets every text to the plain font (fontFamily 2) and enlarges the box text for README width. It moves the boxes listed in `LAYOUT`, redraws every arrow between its boxes, places each arrow label (`LABELS`), and draws the auth-to-store arrow as an elbow around the membership box (a straight arrow would cross it). Then it writes `architecture.excalidraw` with the panels. Random fields are set from each element's id, so two rebuilds of the same sources are byte-identical.
3. `export.mjs` writes each SVG.
4. `render.mjs` replaces the exported font stack with `Helvetica, Arial, sans-serif`, draws each arrow label over a halo of the background colour (so a line passing under a label never strikes it through), and renders each PNG at 2x. The font step is needed on Linux: the exported stack names "Segoe UI Emoji", which resolves to a colour emoji font that draws digits as emoji bitmaps.

After a rebuild, look at `architecture-core.png` scaled to about 860 px wide (the README's width) and at `architecture.png` at full size: no overlapping text, every label beside or on its own arrow. Then check every line is still true of the code, and that the T panel is still word for word the README's "T. Targets".
