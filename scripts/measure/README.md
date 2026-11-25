# Measurement scripts

The scripts behind `docs/MEASUREMENTS.md`. They are not part of the app or the test
suite and nothing imports them. Each prints one line per run and then, per metric, the range over all runs
(minimum to maximum, with the median) and the host's load before and after.

**Results depend on the host.** Every figure is wall-clock time on whatever else the machine is doing.
Quote figures as ranges, with the load they were
taken at, and re-run before comparing two versions of the code.

## Run

From the project folder, with the dev dependencies installed (`uv sync`):

```sh
uv run python scripts/measure/seed.py           # .measure/measure.db and .measure/meta.json
uv run python scripts/measure/reads.py
uv run python scripts/measure/polls.py
uv run python scripts/measure/throughput.py
uv run python scripts/measure/saves.py          # changes the seeded notes (re-seed after)
rm -r .measure                                  # the throwaway data; ignored by git and docker
```

Each takes `--runs` (default 5) and the options its `--help` lists.
`polls.py --polls 50 --per-user 1` holds one poll for each of 50 users (`--per-user` defaults to 10).

On Postgres, seed with its URL and the other scripts use the URL that seed.py recorded. The seeder drops
and recreates the app's tables in that database, so use a throwaway server:

```sh
docker run -d --name team-notes-measure-pg -e POSTGRES_PASSWORD=measure -p 127.0.0.1:55440:5432 postgres:17
uv run python scripts/measure/seed.py postgresql://postgres:measure@127.0.0.1:55440/postgres
uv run python scripts/measure/reads.py           # warm only: Postgres's cache cannot be dropped from here
docker rm -fv team-notes-measure-pg
```

## What each measures, and how

| Script | Measures | Method |
|---|---|---|
| `seed.py` | (setup) | 2 teams of 10,000 notes of 1 to 3 KB of random words (one in five with an accented word), 50 users in both teams, each with a session token. Fixed random seed: the same data every time. |
| `reads.py` | list page 1, deep page by cursor (the page after 9,950 notes), a search that matches nothing (it must scan the whole team), get one note | **In-process**: FastAPI's `TestClient` calls the real app and database with no network or server process, so the time is the whole request path (auth, membership, query, JSON). The deep page's cursor is found once, untimed, by following `next_cursor` from page 1; only that page's request is timed. **Warm**: one request, then the median of 9 (`--warm`). **Cold** (SQLite on Linux only; elsewhere the script says so and prints warm figures): checkpoint the WAL, fsync, `posix_fadvise(DONTNEED)` on the database files so the OS page cache forgets them, build a new app with new connections, time one request. |
| `saves.py` | saves per second, and p50 and p99 latency, at 1, 32 and 128 concurrent savers | **Live server** (uvicorn, one worker, started on a free port). Each saver is one keep-alive connection with its own note: it takes the lock, then saves a 2 KB body back to back for 10 s with `If-Match`, renewing every 20 s with `?renew=true&active=true`. 8 client processes. Non-200 answers (such as `busy`) are listed. |
| `throughput.py` | authenticated `GET` of one note per second at concurrency 1 and 40; `/health` the same way as a ceiling | **Live server**. Each concurrent client is one keep-alive connection sending requests back to back for 6 s, over up to 8 client processes. |
| `polls.py` | the cost of 500 held long polls on one note (server memory and file descriptors over idle) and how fast they wake (p50 and max from a lock take's response to each poll's return) | **Live server**, Linux only (reads `/proc`; elsewhere it says so and exits without measuring). 10 polls per user (the cap), 50 users. Each run waits until every poll is held, reads memory, takes the lock, and times the returns. |

`common.py` holds what they share: the data folder, the tokens, starting a live server, the summaries.
