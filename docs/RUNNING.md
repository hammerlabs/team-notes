# Running it

The details behind the README's "Run it": settings, what each Docker container does, the image, backups.

## The three paths

| Path | Command | Database | Docs site and demo data |
|---|---|---|---|
| Bare, zero setup | `uv run uvicorn app.main:app --port 8765 --timeout-graceful-shutdown 3` | SQLite `./notes.db` | neither |
| Bare, full | `scripts/run.sh` | SQLite `./notes.db` | both |
| Docker | `docker compose up --build` | Postgres 17 in its own container (`db`) | both |

All three need only [uv](https://docs.astral.sh/uv/) (which fetches Python 3.12) or Docker, and serve everything on one port: the API, the test page at `/`, FastAPI's explorers at `/docs` and `/redoc` (these two load scripts from a CDN), and on the two full paths the docs site at `/site/` (it works offline). On the bare zero-setup path, `/site/` answers 404 with a message saying how to build it.

## Settings

`scripts/run.sh` reads:

- `PORT` (default 8765) and `HOST` (default 127.0.0.1). It checks the port is free before the slow steps and suggests `PORT=8766 scripts/run.sh` if not.
- `DATABASE_URL` (default `sqlite:///notes.db`). The script runs from the project folder, so a relative SQLite path is a file in the project folder, wherever the script was started from. `postgresql://user:pass@host:5432/db` points it, or the bare command, at Postgres. Postgres needs a UTF-8 locale for search to fold accented capitals (the contract's search notes).
- `SEED=0` never adds the demo data. Otherwise the demo data is seeded only into a database with no accounts ([seed data](SEED_DATA.md)).

Docker compose reads these from the environment or a `.env` file next to `docker-compose.yml`; `.env.example` lists them:

- `TEAM_NOTES_PORT` (default 8765): the host port, for example `TEAM_NOTES_PORT=8766 docker compose up --build`.
- `TEAM_NOTES_BIND` (default 127.0.0.1): the host address to publish on. `0.0.0.0` makes the app reachable from other machines.
- `TEAM_NOTES_SEED` (default 1): `0` never adds the demo data.
- `TEAM_NOTES_DB_PASSWORD` (default `team-notes-demo`): the app's Postgres password. **The default is a demo value, published here, and fine only for a local demo.** For anything else, set a real one in `.env` before the first start, for example `TEAM_NOTES_DB_PASSWORD=` followed by the output of `openssl rand -hex 24`. Use letters and digits only: compose puts it into the app's `DATABASE_URL` as is. Postgres reads it only when it creates the database on an empty volume; to change it later, run `docker compose exec db psql -U postgres -c "ALTER ROLE team_notes PASSWORD 'new-one'"`, change `.env` to match and run `docker compose up -d`.

## One worker, and stopping

Run one worker process. Long polls are woken by signals held in the process's memory (README, D4), so a second process would never hear the first one's saves. On SIGTERM, held polls end at once with 204 and clients poll again; `--timeout-graceful-shutdown 3` bounds how long any other open request delays the exit.

## Docker

```sh
docker compose up --build        # builds the app image, starts db, waits for it, starts app
docker compose down              # stops both; the data volume stays
docker compose down -v           # stops both and deletes the data
```

Compose names everything after the project (default `team-notes`, or `-p <name>`): the app image `team-notes-app`, the data volume `team-notes_pgdata`, the network `team-notes_default`. Two checkouts started with different `-p` names never share a database.

Two containers:

- **`db`**: the official Debian-based `postgres:17` image, unchanged. On its first start with an empty volume it creates the cluster with Postgres's builtin `C.UTF-8` locale, so `ILIKE` lowercases accented capitals (`café` finds `CAFÉ`), text sorts by code point as on SQLite, and no index depends on the C library version of a floating image tag. It then runs `docker/db-init/10-app-role.sh` (mounted read-only): the app's role `team_notes` owns the `team_notes` database and is not a superuser, and the superuser `postgres` gets no password, so it can log in only from inside the container (`docker compose exec db psql -U postgres`). No port is published: only containers on the compose network reach it. A healthcheck (`pg_isready` over TCP) reports it ready only once the real server, not the first-start set-up, is up.
- **`app`**: the image built from `Dockerfile`, the service alone. It starts only after `db` is healthy (`depends_on: condition: service_healthy`), with `DATABASE_URL=postgresql://team_notes:<password>@db:5432/team_notes`. `docker/entrypoint.sh` seeds the demo data when `TEAM_NOTES_SEED=1` and the database has no accounts, then `exec`s one uvicorn worker, which becomes PID 1 so `docker stop`'s SIGTERM reaches it: held polls end at once and `--timeout-graceful-shutdown 3` bounds the rest. It runs as an unprivileged user (uid 10001) and has its own healthcheck on `/health`.
- The port is published on 127.0.0.1 by default, because the demo accounts share a published password. On start the app's log says which URL to open.

The image is a three-stage build on `python:3.12-slim-trixie`: the locked runtime dependencies (installed with uv, which is not copied into the final image), the docs site built with MkDocs, and the runtime with the venv, `app/`, `static/` and `site/`. It measures 304 MB (`docker image ls`, arm64). uv and every Python dependency are pinned; the base image tags (`python:3.12-slim-trixie`, `postgres:17`) float, and the `Dockerfile` header shows how to pin the Python base by digest.

The app image without compose, against any Postgres:

```sh
docker build -t team-notes-app .
docker run --rm -p 127.0.0.1:8765:8000 -e DATABASE_URL=postgresql://user:pass@host:5432/db team-notes-app
```

## The docs site alone

```sh
NO_MKDOCS_2_WARNING=1 DISABLE_MKDOCS_2_WARNING=true uv run --group docs mkdocs build --strict
```

It writes `site/`, which the app serves at `/site/`. The two variables silence a notice that MkDocs prints about its next major version; `scripts/run.sh` and the `Dockerfile` set them too.

## Backups

SQLite: `sqlite3 notes.db ".backup copy.db"`. Copying the file alone can miss changes still in the WAL file next to it.

Postgres under compose: the data lives on the `<project>_pgdata` volume. Dump it while it runs, and restore into a fresh stack:

```sh
docker compose exec -T db pg_dump -U team_notes --format=custom team_notes > notes.dump
docker compose exec -T db pg_restore -U team_notes --dbname=team_notes --clean --if-exists < notes.dump
```

`--clean` drops the tables before restoring them, so any demo rows already seeded into the fresh stack are replaced by the dump.
