"""Shared by the measurement scripts: the seeded data set's description, a live server, and summaries.

Not part of the app or the test suite. See README.md in this folder.
"""

import json
import os
import signal
import socket
import statistics
import subprocess
import sys
import time
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

PROJECT = Path(__file__).resolve().parents[2]
# The scripts run as files (`python scripts/measure/x.py`), so make `app` importable. Every script imports
# this module before `app`.
sys.path.insert(0, str(PROJECT))
# Throwaway data lives inside the project, in a folder that git and docker both ignore.
DATA_DIR = PROJECT / ".measure"
DEFAULT_URL = f"sqlite:///{DATA_DIR / 'measure.db'}"
META_PATH = DATA_DIR / "meta.json"


def token(i: int) -> str:
    """The bearer token of seeded user `i` (its hash is stored in `sessions`)."""
    return f"measure-token-{i}"


def load_meta() -> dict[str, Any]:
    """What seed.py wrote: the database URL, the teams with sample note ids, the user count."""
    if not META_PATH.is_file():
        sys.exit(f"{META_PATH} not found: run scripts/measure/seed.py first")
    return json.loads(META_PATH.read_text())


def host_conditions() -> str:
    """One line describing the host while measuring: CPUs and load average. Results depend on it."""
    one, five, fifteen = os.getloadavg()
    return f"host: {os.cpu_count()} CPUs, load average {one:.1f} / {five:.1f} / {fifteen:.1f} (1 / 5 / 15 min)"


def free_port() -> int:
    """A TCP port on 127.0.0.1 that nothing is listening on right now."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@contextmanager
def live_server(database_url: str) -> Iterator[tuple[int, int]]:
    """Run the app as production does (one uvicorn worker) on a free port; yield (port, pid).

    Access logs are off so that writing them to a pipe nobody reads cannot slow the server.
    """
    port = free_port()
    env = {**os.environ, "DATABASE_URL": database_url}
    command = [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port)]
    command += ["--timeout-graceful-shutdown", "3", "--no-access-log", "--log-level", "warning"]
    server = subprocess.Popen(command, cwd=PROJECT, env=env)
    try:
        deadline = time.monotonic() + 30
        while True:
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1).read()
                break
            except OSError:
                if server.poll() is not None or time.monotonic() > deadline:
                    raise RuntimeError("the server did not start") from None
                time.sleep(0.1)
        yield port, server.pid
    finally:
        server.send_signal(signal.SIGTERM)
        try:
            server.wait(timeout=15)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait()


def percentile(sorted_values: list[float], fraction: float) -> float:
    """The value at `fraction` (0 to 1) of an already sorted list; nan if empty."""
    if not sorted_values:
        return float("nan")
    return sorted_values[min(len(sorted_values) - 1, int(len(sorted_values) * fraction))]


def summarise(label: str, values: list[float], unit: str) -> str:
    """`label: min to max unit (median m, n runs)` for one metric over several runs."""
    if not values:
        return f"{label}: no data"
    low, high, middle = min(values), max(values), statistics.median(values)
    digits = 1 if high < 100 else 0
    return f"{label}: {low:.{digits}f} to {high:.{digits}f} {unit} (median {middle:.{digits}f}, {len(values)} runs)"
