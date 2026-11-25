"""What held long polls cost the server, and how fast they wake: N polls on one note, then one lock take.

Opens N polls (default 500) on one note with the note's current tag, 10 per user (the per-user cap), waits
until they are all held, and reads the server's resident memory and open file descriptors from /proc. Then
one client takes the note's lock, and each poll's return is timed from the moment the take was sent (so the
take's own handling is included: the server commits, wakes the polls, then answers the take, and under load
polls can be answered before the take's response arrives). Reported: memory over the server's footprint
just after start (Python does not hand freed memory back, so later runs show the high-water mark), held
descriptors over idle, the take's own response time (mostly its commit), and the wake latency's p50 and
maximum, both from sending the take.

    uv run python scripts/measure/polls.py [--runs 5] [--polls 500]

Starts its own server on a free port (Linux only: it reads /proc). Needs polls/10 seeded users (50 for 500).
Runtime: about 6 s per run.
"""

import argparse
import http.client
import json
import os
import threading
import time

from common import host_conditions, live_server, load_meta, percentile, summarise, token


def call(port: int, method: str, path: str, who: str, timeout: float = 60) -> tuple[int, bytes]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    conn.request(method, path, headers={"Authorization": f"Bearer {who}"})
    response = conn.getresponse()
    return response.status, response.read()


def memory_mb(pid: int) -> float:
    with open(f"/proc/{pid}/status") as status:
        line = next(line for line in status if line.startswith("VmRSS"))
    return int(line.split()[1]) / 1024


def descriptors(pid: int) -> int:
    return len(os.listdir(f"/proc/{pid}/fd"))


def one_run(port: int, pid: int, path: str, polls: int, start_mb: float, per_user: int = 10) -> dict[str, float]:
    call(port, "DELETE", path + "/lock", token(0))
    tag = json.loads(call(port, "GET", path, token(1))[1])["tag"]
    idle_fds = descriptors(pid)
    returned: list[tuple[float, int]] = []

    def poll(i: int) -> None:
        status, _ = call(port, "GET", f"{path}/changes?since={tag}&wait=25", token(i // per_user))
        returned.append((time.perf_counter(), status))

    threads = [threading.Thread(target=poll, args=(i,)) for i in range(polls)]
    for thread in threads:
        thread.start()
    deadline = time.monotonic() + 15
    # Within 2%: a connection or two from the previous run may still be closing, and is counted in idle_fds.
    while descriptors(pid) < idle_fds + polls - polls // 50 and time.monotonic() < deadline:
        time.sleep(0.1)
    time.sleep(0.5)
    held_mb, held_fds = memory_mb(pid), descriptors(pid)
    taken_at = time.perf_counter()
    status, _ = call(port, "PUT", path + "/lock", token(0))
    take_ms = (time.perf_counter() - taken_at) * 1000
    assert status == 200, status
    for thread in threads:
        thread.join()
    statuses = {status for _, status in returned}
    assert statuses == {200}, statuses
    latencies = sorted((at - taken_at) * 1000 for at, _ in returned)
    call(port, "DELETE", path + "/lock", token(0))
    return {
        "memory over start (MB)": held_mb - start_mb,
        "descriptors over idle": held_fds - idle_fds,
        "take response (ms)": take_ms,
        "wake p50 (ms)": percentile(latencies, 0.5),
        "wake max (ms)": latencies[-1],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--polls", type=int, default=500)
    parser.add_argument("--per-user", type=int, default=10, help="polls per user, 1 to 10 (default 10)")
    args = parser.parse_args()
    if not os.path.exists("/proc/self/status"):
        print("polls.py needs Linux: it reads the server's memory and descriptors from /proc. Nothing measured.")
        return
    meta = load_meta()
    assert 1 <= args.per_user <= 10, "10 polls per user at most"
    assert args.polls <= meta["users"] * args.per_user, "not enough seeded users"
    team = meta["teams"][1]
    path = f"/teams/{team['id']}/notes/{team['sample_note_ids'][7]}"
    results: dict[str, list[float]] = {}
    print(f"{meta['url'].split(':')[0]}, {args.polls} polls on one note; {host_conditions()}")
    with live_server(meta["url"]) as (port, pid):
        call(port, "GET", path, token(1))  # load the code paths a read uses before taking the baseline
        start_mb = memory_mb(pid)
        for run in range(1, args.runs + 1):
            row = one_run(port, pid, path, args.polls, start_mb, args.per_user)
            for key, value in row.items():
                results.setdefault(key, []).append(value)
            print(f"run {run}: " + "; ".join(f"{k} {v:.1f}" for k, v in row.items()), flush=True)
    print(host_conditions())
    for key, values in results.items():
        name, _, unit = key.partition(" (")
        print(summarise(name, values, unit.rstrip(")") or ""))


if __name__ == "__main__":
    main()
