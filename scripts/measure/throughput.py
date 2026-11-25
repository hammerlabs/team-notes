"""Authenticated reads per second against a live server: `GET` of one note at several concurrencies.

Each concurrent client is one keep-alive connection sending requests back to back for the duration; clients
are spread over up to 8 processes. Every request is authenticated (token lookup) and membership-checked, as
real reads are. `/health` (no database) is measured the same way as a ceiling for the HTTP stack alone.

    uv run python scripts/measure/throughput.py [--runs 5] [--concurrency 1,40] [--seconds 6]

Starts its own server on a free port. Runtime: about (seconds + 1) x levels x 2 x runs; about 1.5 minutes.
"""

import argparse
import http.client
import multiprocessing
import threading
import time
from typing import Any

from common import host_conditions, live_server, load_meta, summarise, token

PROCESSES = 8


def client_process(port: int, connections: int, path: str, start_at: float, seconds: float, queue: Any) -> None:
    count = [0]
    lock = threading.Lock()

    def one() -> None:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
        done = 0
        time.sleep(max(0.0, start_at - time.time()))
        end = start_at + seconds
        while time.time() < end:
            conn.request("GET", path, headers={"Authorization": f"Bearer {token(1)}"})
            response = conn.getresponse()
            response.read()
            assert response.status == 200, response.status
            done += 1
        with lock:
            count[0] += done

    threads = [threading.Thread(target=one) for _ in range(connections)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    queue.put(count[0])


def one_level(port: int, path: str, concurrency: int, seconds: float) -> float:
    queue: Any = multiprocessing.Queue()
    processes = min(PROCESSES, concurrency)
    start_at = time.time() + 1
    procs = [
        multiprocessing.Process(
            target=client_process,
            args=(
                port,
                concurrency // processes + (1 if i < concurrency % processes else 0),
                path,
                start_at,
                seconds,
                queue,
            ),
        )
        for i in range(processes)
    ]
    for proc in procs:
        proc.start()
    total = sum(queue.get() for _ in procs)
    for proc in procs:
        proc.join()
    return total / seconds


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--concurrency", default="1,40")
    parser.add_argument("--seconds", type=float, default=6)
    args = parser.parse_args()
    meta = load_meta()
    team = meta["teams"][0]
    paths = {"GET a note": f"/teams/{team['id']}/notes/{team['sample_note_ids'][3]}", "/health": "/health"}
    levels = [int(n) for n in args.concurrency.split(",")]
    results: dict[tuple[str, int], list[float]] = {(label, c): [] for label in paths for c in levels}
    print(f"{meta['url'].split(':')[0]}, {args.seconds:.0f} s per level; {host_conditions()}")
    with live_server(meta["url"]) as (port, _):
        for run in range(1, args.runs + 1):
            row = []
            for label, path in paths.items():
                for concurrency in levels:
                    rate = one_level(port, path, concurrency, args.seconds)
                    results[(label, concurrency)].append(rate)
                    row.append(f"{label} at {concurrency}: {rate:.0f}/s")
            print(f"run {run}: " + "; ".join(row), flush=True)
    print(host_conditions())
    for (label, concurrency), values in results.items():
        print(summarise(f"{label}, concurrency {concurrency}", values, "requests/s"))


if __name__ == "__main__":
    main()
