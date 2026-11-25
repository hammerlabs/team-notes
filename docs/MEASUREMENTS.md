# Measurements

What the scripts in `scripts/measure/` measure, on what data, and how. Their `README.md` says how to run each one.

- **Reads:** list page 1 (50 notes), a deep page by cursor (the page after 9,950 notes), a search that matches nothing (it scans the whole team), and get one note.
- **Saves:** saves per second, with p50 and p99 latency, at 1, 32 and 128 concurrent savers, and any refused saves (such as 503 `busy`).
- **Throughput:** authenticated `GET` of one note per second at concurrency 1 and 40, and `/health` (no database) the same way as a ceiling.
- **Held long polls:** the cost of 500 held polls on one note (server memory and file descriptors over idle) and how fast they wake after a lock take.

## Data

`seed.py` builds 2 teams of 10,000 notes of 1 to 3 KB of random words (one in five with an accented word) and 50 users in both teams, each with a session token. The random seed is fixed, so the data is the same every time.

## Method

- **Reads** (`reads.py`) run in-process through FastAPI's `TestClient`, so a time covers the whole request path (token, membership, query, JSON) without the network. Warm: one warm-up request, then the median of 9. Cold (SQLite only): checkpoint the WAL, fsync, drop the files from the OS page cache with `posix_fadvise(DONTNEED)`, build a new app, time one request. Postgres's cache cannot be dropped from outside, so it has warm figures only.
- **Saves** (`saves.py`) run against a live uvicorn server with one worker, started by the script. Each saver holds a lock on its own note and saves a 2 KB body back to back for 10 s.
- **Throughput** (`throughput.py`) runs against a live server: each client is one keep-alive connection sending requests back to back for 6 s.
- **Polls** (`polls.py`) run against a live server, Linux only: 10 polls per user (the cap), 50 users. Each run waits until every poll is held, reads memory, takes the lock, and times the returns.
- Each script reports, per metric, the range over its runs (minimum to maximum, with the median) and the host's load before and after. Quote results as ranges with the load, and re-run both versions before comparing two versions of the code.

## Results

Conditions:

- A Linux/arm64 container (Docker through OrbStack) on an Apple-silicon Mac, with the project copied into the container's own filesystem and its environment built there with `uv sync`. Postgres 17 (`postgres:17`, default settings) runs in a second container on the same Docker network.
- Inside the container: `nproc` 10; 8,195,968 kB of memory (`MemTotal`, no cgroup limit), 7,356,928 kB available before the runs.
- Python 3.12.12, SQLite 3.40.1.
- Other work runs on the host at the same time, so the machine is not idle. The load average each script prints is in the tables.

### SQLite

`reads.py`, 5 runs; load average (1 / 5 / 15 min) 4.5 / 1.2 / 0.7 before, 5.1 / 1.4 / 0.7 after.

| Read | Warm (median of 9 per run) | Cold |
|---|---|---|
| list page 1 (limit 50) | 3.1 to 4.6 ms (median 3.2) | 6.3 to 7.3 ms (median 6.9) |
| deep page by cursor (after 9,950) | 3.2 to 4.3 ms (median 3.9) | 5.8 to 8.8 ms (median 6.8) |
| search, no match | 69.2 to 75.2 ms (median 70.1) | 97 to 102 ms (median 101) |
| get a note | 1.9 to 3.2 ms (median 2.8) | 5.3 to 7.0 ms (median 5.9) |

`polls.py`, 500 polls on one note, 3 runs; load average 5.5 / 1.5 / 0.8 before, 6.2 / 1.7 / 0.9 after.

| Metric | Range |
|---|---|
| memory over start | 22.7 to 27.0 MB (median 26.4) |
| descriptors over idle | 499 to 557 (median 499) |
| take response | 584 to 1349 ms (median 723) |
| wake p50 | 934 to 1083 ms (median 986) |
| wake max | 1207 to 1386 ms (median 1306) |

`polls.py` at the target team size, in a later session of the same container setup: 50 polls on one note, one per user, 50 users (`--polls 50 --per-user 1`), 5 runs, load average 5.6 / 2.2 / 1.3 before, 5.2 / 2.1 / 1.3 after; then 500 polls (10 per user) again, 3 runs, load average 5.2 / 2.1 / 1.3 before, 5.7 / 2.3 / 1.3 after.

| Metric | 50 polls, 5 runs | 500 polls, 3 runs |
|---|---|---|
| memory over start | 7.3 to 8.5 MB (median 8.2) | 25.0 to 27.5 MB (median 26.6) |
| descriptors over idle | 49 to 111 (median 49) | 500 to 561 (median 502) |
| take response | 35.2 to 66.5 ms (median 48.7) | 422 to 707 ms (median 525) |
| wake p50 | 60.6 to 70.2 ms (median 67.1) | 383 to 539 ms (median 520) |
| wake max | 66.8 to 75.4 ms (median 70.1) | 587 to 762 ms (median 730) |

`throughput.py`, 6 s per level, 3 runs; load average 5.8 / 1.7 / 0.9 before, 3.7 / 1.9 / 1.0 after.

| Request | Concurrency 1 | Concurrency 40 |
|---|---|---|
| GET a note | 616 to 660 requests/s (median 634) | 597 to 602 requests/s (median 598) |
| /health | 2581 to 2602 requests/s (median 2596) | 3542 to 3683 requests/s (median 3633) |

`saves.py`, 2 KB bodies, 10 s per level, 2 runs; load average 3.4 / 1.9 / 1.0 before, 2.7 / 2.0 / 1.1 after. No run has any other answers (no refused saves).

| Savers | Saves/s | p50 | p99 |
|---|---|---|---|
| 1 | 344 to 345 /s (median 345) | 2.7 to 2.7 ms (median 2.7) | 4.8 to 5.7 ms (median 5.2) |
| 32 | 441 to 454 /s (median 448) | 9.0 to 9.9 ms (median 9.4) | 1160 to 1261 ms (median 1210) |
| 128 | 409 to 434 /s (median 421) | 224 to 235 ms (median 230) | 1646 to 1647 ms (median 1646) |

### Postgres 17

`reads.py`, warm only, 3 runs; load average 2.4 / 1.9 / 1.1 before, 2.3 / 1.9 / 1.1 after.

| Read | Warm (median of 9 per run) |
|---|---|
| list page 1 (limit 50) | 3.2 to 3.5 ms (median 3.3) |
| deep page by cursor (after 9,950) | 3.4 to 3.6 ms (median 3.6) |
| search, no match | 172 to 173 ms (median 172) |
| get a note | 2.5 to 2.8 ms (median 2.5) |

`saves.py`, 2 KB bodies, 10 s per level, 1 run; load average 2.3 / 1.9 / 1.1 before, 3.4 / 2.2 / 1.2 after. No other answers (no refused saves).

| Savers | Saves/s | p50 | p99 |
|---|---|---|---|
| 1 | 275 /s | 3.5 ms | 5.9 ms |
| 32 | 339 /s | 92.8 ms | 150 ms |
| 128 | 342 /s | 381 ms | 447 ms |

`polls.py` and `throughput.py` are not run on Postgres.

### Against the targets

List pages, deep pages and single notes are far under 200 ms on both databases, warm and cold, and saves run at hundreds a second against the roughly 5 a second needed, with no refusals at any level. The full-team search that matches nothing meets 200 ms in every condition measured: 69.2 to 75.2 ms warm and 97 to 102 ms cold on SQLite, and 172 to 173 ms warm on Postgres, where the margin is smallest. Telling a viewer within 1 s is not reliably met with 500 held polls on one note, ten times the target team: the two sessions differ, with a median wake of 934 to 1083 ms and a slowest wake of 1207 to 1386 ms in one, and 383 to 539 ms and 587 to 762 ms in the other, so the figure moves with what else the host is doing. At the target team size of 50 viewers the 1 s requirement is met: the wake p50 is 60.6 to 70.2 ms (median 67.1) and the slowest poll wakes after 66.8 to 75.4 ms (median 70.1) over 5 runs.
