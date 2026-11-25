"""List, search and get timings, in-process: the app's whole request path, without a network or a server.

Each request goes through FastAPI's TestClient into the real app and database, so the time is routing,
authentication, the membership check, the query and serialising the response. Warm: one request to fill
caches, then the median of N (default 9). Cold (SQLite on Linux only): the WAL is checkpointed, the database
file's pages are dropped from the OS page cache (`posix_fadvise(DONTNEED)` after an fsync), a new app with
new connections is built, and one request is timed. On Postgres the server's own cache cannot be dropped
from here, and other systems (macOS) have no `posix_fadvise`, so there only warm figures are printed.

The deep page is the list page after the first 9,950 notes (the last page of a 10,000-note team). Its
cursor is found once, before any timing, by following `next_cursor` from the first page; only the request
for that one page is timed. With keyset paging it should cost about what page 1 costs.

    uv run python scripts/measure/reads.py [--runs 5] [--warm 9]

Runtime: about 10 s per run on SQLite with cold reads; a few seconds warm only.
"""

import argparse
import os
import sqlite3
import statistics
import time
from urllib.parse import urlencode

from common import host_conditions, load_meta, summarise, token
from fastapi.testclient import TestClient
from sqlalchemy.engine import make_url

from app.main import create_app

HEADERS = {"Authorization": f"Bearer {token(1)}"}
PAGE = 50


def drop_from_page_cache(path: str) -> None:
    """Checkpoint the WAL into the main file, then ask the kernel to forget the files' cached pages."""
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    connection.close()
    for suffix in ("", "-wal", "-shm"):
        if os.path.exists(path + suffix):
            fd = os.open(path + suffix, os.O_RDONLY)
            os.fsync(fd)
            os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
            os.close(fd)


def timed(client: TestClient, url: str) -> float:
    """Milliseconds for one GET, which must succeed."""
    started = time.perf_counter()
    response = client.get(url, headers=HEADERS)
    elapsed = (time.perf_counter() - started) * 1000
    assert response.status_code == 200, response.text
    return elapsed


def cursor_after(client: TestClient, base: str, notes: int) -> str:
    """The cursor of the page that starts after the first `notes` notes, found by following `next_cursor`."""
    cursor: str | None = None
    for _ in range(notes // PAGE):
        params = {"limit": PAGE, **({"cursor": cursor} if cursor else {})}
        response = client.get(f"{base}?{urlencode(params)}", headers=HEADERS)
        assert response.status_code == 200, response.text
        cursor = response.json()["next_cursor"]
        assert cursor, f"the list ended before {notes} notes"
    assert cursor is not None
    return cursor


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--warm", type=int, default=9, help="timed warm requests per run; the median is kept")
    args = parser.parse_args()
    meta = load_meta()
    url = meta["url"]
    sqlite_path = make_url(url).database if make_url(url).get_backend_name() == "sqlite" else None
    cold_reads = bool(sqlite_path) and hasattr(os, "posix_fadvise")
    team = meta["teams"][0]
    base = f"/teams/{team['id']}/notes"
    depth = team["notes"] - PAGE
    with TestClient(create_app(url)) as client:
        deep_cursor = cursor_after(client, base, depth)
    cases = {
        f"list page 1 (limit {PAGE})": f"{base}?limit={PAGE}",
        f"deep page by cursor (after {depth:,})": f"{base}?{urlencode({'limit': PAGE, 'cursor': deep_cursor})}",
        "search, no match": f"{base}?q=qqqxqqq",
        "get a note": f"{base}/{team['sample_note_ids'][5]}",
    }
    print(f"{make_url(url).get_backend_name()}, team of {team['notes']} notes; {host_conditions()}")
    if sqlite_path and not cold_reads:
        print("cold reads need Linux (os.posix_fadvise is not available here): warm figures only")
    warm: dict[str, list[float]] = {label: [] for label in cases}
    cold: dict[str, list[float]] = {label: [] for label in cases}
    for run in range(1, args.runs + 1):
        row = []
        for label, path in cases.items():
            if cold_reads and sqlite_path:
                drop_from_page_cache(sqlite_path)
                with TestClient(create_app(url)) as client:
                    client.get("/health")
                    cold[label].append(timed(client, path))
            with TestClient(create_app(url)) as client:
                timed(client, path)
                warm[label].append(statistics.median(timed(client, path) for _ in range(args.warm)))
            row.append(f"{label} {warm[label][-1]:.1f} ms" + (f" (cold {cold[label][-1]:.0f})" if cold_reads else ""))
        print(f"run {run}: " + "; ".join(row), flush=True)
    print(host_conditions())
    for label in cases:
        print(summarise(f"{label}, warm median of {args.warm}", warm[label], "ms"))
        if cold[label]:
            print(summarise(f"{label}, cold", cold[label], "ms"))


if __name__ == "__main__":
    main()
