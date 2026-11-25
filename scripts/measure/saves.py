"""Saves per second and their latency against a live server, at several numbers of concurrent savers.

Each saver is one client connection editing its own note: it takes the note's lock, then saves a 2 KB body
in a loop for the duration, sending the version it last saw in `If-Match` each time (renewing the lock every
20 s with `?renew=true&active=true`, as an editing client does). Savers are spread over 8 client processes so the client is not the bottleneck. Reported per
level: saves/s (successful saves over the duration), p50 and p99 of successful saves, and any non-200
answers (`busy` would appear here).

    uv run python scripts/measure/saves.py [--runs 5] [--savers 1,32,128] [--seconds 10]

Starts its own server on a free port (one uvicorn worker, as in production) against the seeded database.
Runtime: about (seconds + 3) x levels x runs; 3 to 4 minutes with the defaults. Saves change the seeded notes'
bodies and versions; re-seed if that matters for a later measurement.
"""

import argparse
import http.client
import json
import multiprocessing
import threading
import time
from collections import Counter
from typing import Any

from common import host_conditions, live_server, load_meta, percentile, summarise, token

BODY = ("x" * 99 + "\n") * 20  # 2,000 characters
PROCESSES = 8


def request(
    conn: http.client.HTTPConnection, method: str, url: str, who: str, body: Any = None, version: int | None = None
) -> tuple[int, bytes]:
    headers = {"Authorization": f"Bearer {who}"}
    if version is not None:
        headers["If-Match"] = f'"{version}"'
    data = None
    if body is not None:
        data = json.dumps(body)
        headers["Content-Type"] = "application/json"
    conn.request(method, url, body=data, headers=headers)
    response = conn.getresponse()
    return response.status, response.read()


def saver(port: int, url: str, who: str, seconds: float, start: threading.Event, out: list) -> None:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
    status, _ = request(conn, "PUT", url + "/lock", who)
    if status != 200:
        out.append((0.0, f"lock {status}"))
        return
    version = json.loads(request(conn, "GET", url, who)[1])["version"]
    start.wait()
    end = time.monotonic() + seconds
    renewed = time.monotonic()
    while time.monotonic() < end:
        began = time.perf_counter()
        status, data = request(conn, "PATCH", url, who, {"body": BODY}, version)
        out.append(((time.perf_counter() - began) * 1000, 200 if status == 200 else f"{status} {data[:60]!r}"))
        if status == 200:
            version += 1
        if time.monotonic() - renewed > 20:
            status, data = request(conn, "PUT", url + "/lock?renew=true&active=true", who)
            if status != 200:
                out.append((0.0, f"renew {status} {data[:60]!r}"))
                break
            renewed = time.monotonic()
    request(conn, "DELETE", url + "/lock", who)


def client_process(port: int, jobs: list, seconds: float, start_at: float, queue: Any) -> None:
    out: list = []
    start = threading.Event()
    threads = [threading.Thread(target=saver, args=(port, url, who, seconds, start, out)) for url, who in jobs]
    for thread in threads:
        thread.start()
    time.sleep(max(0.0, start_at - time.time()))
    start.set()
    for thread in threads:
        thread.join()
    queue.put(out)


def one_level(port: int, meta: dict, savers: int, seconds: float) -> tuple[float, float, float, Counter]:
    notes = [(team["id"], note_id) for team in meta["teams"] for note_id in team["sample_note_ids"]]
    assert savers <= len(notes), f"at most {len(notes)} savers (one note each)"
    jobs = [(f"/teams/{t}/notes/{n}", token(i % meta["users"])) for i, (t, n) in enumerate(notes[:savers])]
    queue: Any = multiprocessing.Queue()
    start_at = time.time() + 2 + savers / 100  # every saver holds its lock before the clock starts
    count = min(PROCESSES, savers)
    procs = [
        multiprocessing.Process(target=client_process, args=(port, jobs[k::count], seconds, start_at, queue))
        for k in range(count)
    ]
    for proc in procs:
        proc.start()
    results = [item for _ in procs for item in queue.get()]
    for proc in procs:
        proc.join()
    ok = sorted(ms for ms, status in results if status == 200)
    others = Counter(status for _, status in results if status != 200)
    return len(ok) / seconds, percentile(ok, 0.5), percentile(ok, 0.99), others


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--savers", default="1,32,128")
    parser.add_argument("--seconds", type=float, default=10)
    args = parser.parse_args()
    meta = load_meta()
    levels = [int(n) for n in args.savers.split(",")]
    rate: dict[int, list[float]] = {n: [] for n in levels}
    p50: dict[int, list[float]] = {n: [] for n in levels}
    p99: dict[int, list[float]] = {n: [] for n in levels}
    print(f"{meta['url'].split(':')[0]}, 2 KB bodies, {args.seconds:.0f} s per level; {host_conditions()}")
    with live_server(meta["url"]) as (port, _):
        for run in range(1, args.runs + 1):
            for savers in levels:
                per_second, median, tail, others = one_level(port, meta, savers, args.seconds)
                rate[savers].append(per_second)
                p50[savers].append(median)
                p99[savers].append(tail)
                print(
                    f"run {run}, {savers} savers: {per_second:.0f} saves/s, p50 {median:.0f} ms, p99 {tail:.0f} ms,"
                    f" other answers {dict(others) or 'none'}",
                    flush=True,
                )
    print(host_conditions())
    for savers in levels:
        print(summarise(f"{savers} savers, saves/s", rate[savers], "/s"))
        print(summarise(f"{savers} savers, p50", p50[savers], "ms"))
        print(summarise(f"{savers} savers, p99", p99[savers], "ms"))


if __name__ == "__main__":
    main()
