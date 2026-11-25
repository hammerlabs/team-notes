#!/bin/sh
# Regenerate the diagram from architecture.ir.json and panels.py.
#
#   docs/diagram/build.sh <Excalidraw plugin scripts dir>
#
# The directory is the one holding the plugin's generate.mjs, export.mjs and
# node_modules (see README.md in this folder). Writes, next to this script:
#   architecture-core.excalidraw/.svg/.png   the request flow only (embedded in the README)
#   architecture.excalidraw/.svg/.png        the full board with the side panels
set -eu
usage="usage: docs/diagram/build.sh <Excalidraw plugin scripts dir>
  The Excalidraw plugin for Claude Code (1.0.0): <plugin dir>/skills/excalidraw/scripts,
  the directory holding generate.mjs, export.mjs and node_modules. See docs/diagram/README.md."
if [ $# -ne 1 ]; then
    echo "$usage" >&2
    exit 2
fi
for file in generate.mjs export.mjs; do
    if [ ! -f "$1/$file" ]; then
        echo "$1 has no $file." >&2
        echo "$usage" >&2
        exit 2
    fi
done
SCRIPTS=$(cd "$1" && pwd)
cd "$(dirname "$0")"

node "$SCRIPTS/generate.mjs" architecture-core.excalidraw < architecture.ir.json
python3 panels.py architecture-core.excalidraw architecture.excalidraw
for name in architecture-core architecture; do
    node "$SCRIPTS/export.mjs" "$name.excalidraw" > /dev/null
    node render.mjs "$SCRIPTS" "$name"
done
