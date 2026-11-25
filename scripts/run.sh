#!/bin/sh
# Run Team Notes without Docker: sync dependencies, build the docs site, seed
# an empty database, start the server. Safe to re-run.
#
#   scripts/run.sh                       http://127.0.0.1:8765/, SQLite file ./notes.db
#   PORT=9000 HOST=0.0.0.0 scripts/run.sh
#   DATABASE_URL=postgresql://user:pass@host:5432/db scripts/run.sh
#   SEED=0 scripts/run.sh                never add the demo data
#
# Needs only uv. The plain `uv run uvicorn app.main:app` also works; it just
# has no docs site at /site/ (a 404 says how to build it) and no demo data.
#
# The script runs from the project folder, so a relative SQLite DATABASE_URL
# (such as the default sqlite:///notes.db) is a file in the project folder,
# wherever the script was started from.
set -eu
cd "$(dirname "$0")/.."

PORT=${PORT:-8765}
HOST=${HOST:-127.0.0.1}

if ! command -v uv > /dev/null 2>&1; then
    echo "run.sh needs uv: https://docs.astral.sh/uv/getting-started/installation/" >&2
    exit 1
fi
# Checked before the slow steps (sync, docs build, seed), so a busy port fails at once.
if ! uv run --no-project --quiet python -c "
import socket, sys
with socket.socket() as s:
    try:
        s.bind((sys.argv[1], int(sys.argv[2])))
    except OSError:
        sys.exit(1)
" "$HOST" "$PORT"; then
    echo "Port $PORT on $HOST is in use. Stop what is using it, or pick another: PORT=8766 scripts/run.sh" >&2
    exit 1
fi

uv sync --group docs

# MkDocs 1.x plugins print a long notice about MkDocs 2; not ours to act on here.
NO_MKDOCS_2_WARNING=1 DISABLE_MKDOCS_2_WARNING=true uv run mkdocs build --quiet

# --if-empty: only into a database with no accounts, so a database with real
# sign-ups is never mixed with demo data and a re-run never fails here.
if [ "${SEED:-1}" = 1 ]; then
    uv run python -m app.seed --if-empty
fi

# One worker: the long poll's wake-ups live in this process's memory. On
# SIGTERM or Ctrl-C held polls end at once; the timeout bounds the rest.
exec uv run uvicorn app.main:app --host "$HOST" --port "$PORT" --timeout-graceful-shutdown 3
