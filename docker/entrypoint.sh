#!/bin/sh
# Starts the app container: seed if asked and the database has no accounts,
# then replace this shell with uvicorn.
#
# `exec` makes uvicorn PID 1, so `docker stop`'s SIGTERM reaches it directly:
# held long polls end at once with 204, and --timeout-graceful-shutdown bounds
# how long any other open request delays the exit.
#
# Environment:
#   DATABASE_URL                  the database (compose points it at the `db` service)
#   TEAM_NOTES_SEED=1 (default)   seed demo data when the database has no accounts; 0 to skip
set -eu

if [ "${TEAM_NOTES_SEED:-1}" = 1 ]; then
    # --if-empty: a database with real sign-ups never gets demo data, and a
    # restart never fails here. It also creates the tables if they are missing.
    python -m app.seed --if-empty
fi

# The host port is chosen by whoever publishes 8000 (compose: TEAM_NOTES_PORT,
# default 8765), so name both.
echo "entrypoint: serving on port 8000 in the container; open http://localhost:<published host port, default 8765>/" >&2

# One worker: the long poll's wake-ups live in this process's memory.
exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --timeout-graceful-shutdown 3
