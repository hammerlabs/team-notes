"""The long poll on `GET .../changes`: viewers hear about saves, locks and unlocks promptly.

Backs the contract's Changes section and the six steps in `app/routers/changes.py`: listen before the first read, read
without holding a connection, cap the wait at the lock's remaining lease, re-read and re-check membership after each
wake, stop on client disconnect, stop on shutdown.

How the tests work, and why they can be trusted:

- A poll runs on a background thread through the real `TestClient` while the test thread acts, so `wake` really crosses
  from a worker thread to the event loop.
- `wait_for_poll` waits until the poll is registered as a waiter instead of sleeping a fixed time. A change before
  registration would still be reported, so this only makes the "woken while waiting" path the one under test.
- "Promptly" means under 1 second, the design's notification target. Upper bounds are loose and there are no tight lower
  bounds, which are flaky on a loaded machine.
- Two orderings are forced deterministically instead of raced: a change between the first read and the wait (by wrapping
  `_read_change`), and a client hang-up (by driving the ASGI app directly, since `TestClient` cannot disconnect).

The SIGTERM hook in `app/main.py` installs only on the main thread, which `TestClient`'s app is not on. So it is tested
three ways at the end: `Notifier.stop` directly, the hook's chaining on this (main) thread, and a real server process
sent SIGTERM.

Not here: the cost of waking many waiters on one note (measured by `scripts/measure/polls.py`, not tested).
"""

import asyncio
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from collections.abc import Callable, Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import Mock, call

import httpx2
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import update

from app import clock, config, main
from app.models import Note
from app.notify import Notifier
from app.routers import changes
from helpers import Account, Api, FakeClock, TeamOfTwo, assert_error, changes_url, lock_url


def wait_until(condition: Callable[[], bool], timeout: float = 2.0) -> None:
    """Check `condition` every few milliseconds; fail the test if it is still false after `timeout`."""
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "timed out waiting for the condition"
        time.sleep(0.005)


@dataclass
class Poll:
    """A long poll running on a background thread."""

    future: Future[tuple[httpx2.Response, float]]

    def result(self) -> tuple[httpx2.Response, float]:
        """The response and the monotonic time it arrived."""
        return self.future.result(timeout=10)


@pytest.fixture
def pool() -> Iterator[ThreadPoolExecutor]:
    """Four background threads for polls, so a test can hold polls open while it acts."""
    with ThreadPoolExecutor(max_workers=4) as executor:
        yield executor


@pytest.fixture
def wait_for_poll(app: FastAPI) -> Callable[[str], None]:
    """Wait until a poll is listening on the note, before the test acts.

    A poll counts from when it starts listening, just before its first read.
    Correctness does not depend on this (a change that lands before the read
    is reported at once); it makes the "woken while waiting" path the likely
    one under test, without a fixed sleep.
    """

    def wait(note_id: str) -> None:
        wait_until(lambda: app.state.notifier.waiter_count(note_id) == 1)

    return wait


@pytest.fixture
def start_poll(api: Api, pool: ThreadPoolExecutor) -> Callable[..., Poll]:
    """Start a long poll as `who` on a background thread and return at once. `since=None` sends none."""

    def start(who: Account, team_id: str, note_id: str, since: str | None, wait: float = 5) -> Poll:
        params: dict[str, Any] = {"wait": wait}
        if since is not None:
            params["since"] = since

        def run() -> tuple[httpx2.Response, float]:
            response = api.client.get(changes_url(team_id, note_id), params=params, headers=who.headers)
            return response, time.monotonic()

        return Poll(pool.submit(run))

    return start


Scene = tuple[Account, Account, str, dict[str, Any]]


@pytest.fixture
def scene(api: Api, team: TeamOfTwo) -> Scene:
    """Alice (admin) edits, Bob (member) watches, one note at tag '1:'."""
    alice, bob, team_id = team
    note = api.create_note(alice, team_id, "Plan", "v1")
    return alice, bob, team_id, note


def test_missing_or_stale_since_returns_at_once(api: Api, scene: Scene) -> None:
    """With no `since`, or one that no longer matches, the poll answers 200 at once instead of waiting.

    A client that missed changes while not polling learns of them on its next poll.
    """
    alice, bob, team_id, note = scene
    api.take_lock(alice, team_id, note["id"])

    for params in ({}, {"since": "1:"}):
        started = time.monotonic()
        response = api.client.get(changes_url(team_id, note["id"]), params=params, headers=bob.headers)
        assert time.monotonic() - started < 0.5
        assert response.status_code == 200
        assert response.json() == {
            "tag": f"1:{alice.id}",
            "version": 1,
            "lock": api.get_note(bob, team_id, note["id"])["lock"],
        }


def test_quiet_poll_returns_204_after_wait(api: Api, scene: Scene) -> None:
    """With nothing changing, the poll ends after `wait` seconds with an empty 204."""
    _, bob, team_id, note = scene
    started = time.monotonic()

    response = api.client.get(
        changes_url(team_id, note["id"]), params={"since": "1:", "wait": 0.3}, headers=bob.headers
    )

    elapsed = time.monotonic() - started
    assert response.status_code == 204
    assert response.content == b""
    assert 0.3 <= elapsed < 1.5


def test_poll_returns_soon_after_a_lock(
    api: Api, scene: Scene, start_poll: Callable[..., Poll], wait_for_poll: Callable[[str], None]
) -> None:
    """A waiting viewer hears that a lock was taken within a second."""
    alice, bob, team_id, note = scene
    poll = start_poll(bob, team_id, note["id"], since="1:")
    wait_for_poll(note["id"])

    api.take_lock(alice, team_id, note["id"])
    acted_at = time.monotonic()

    response, arrived_at = poll.result()
    assert response.status_code == 200
    assert response.json()["tag"] == f"1:{alice.id}"
    assert arrived_at - acted_at < 1.0


def test_poll_returns_soon_after_a_save(
    api: Api, scene: Scene, start_poll: Callable[..., Poll], wait_for_poll: Callable[[str], None]
) -> None:
    """A waiting viewer hears about a save within a second, with the new version."""
    alice, bob, team_id, note = scene
    api.take_lock(alice, team_id, note["id"])
    poll = start_poll(bob, team_id, note["id"], since=f"1:{alice.id}")
    wait_for_poll(note["id"])

    api.save_note(alice, team_id, note["id"], 1, body="v2")
    acted_at = time.monotonic()

    response, arrived_at = poll.result()
    assert response.status_code == 200
    assert response.json()["tag"] == f"2:{alice.id}"
    assert response.json()["version"] == 2
    assert arrived_at - acted_at < 1.0


def test_poll_returns_soon_after_an_unlock(
    api: Api, scene: Scene, start_poll: Callable[..., Poll], wait_for_poll: Callable[[str], None]
) -> None:
    """A waiting viewer hears that the lock was released within a second."""
    alice, bob, team_id, note = scene
    api.take_lock(alice, team_id, note["id"])
    poll = start_poll(bob, team_id, note["id"], since=f"1:{alice.id}")
    wait_for_poll(note["id"])

    api.client.delete(lock_url(team_id, note["id"]), headers=alice.headers)
    acted_at = time.monotonic()

    response, arrived_at = poll.result()
    assert response.status_code == 200
    assert response.json() == {"tag": "1:", "version": 1, "lock": None}
    assert arrived_at - acted_at < 1.0


def test_poll_returns_soon_after_an_idle_release(
    app: FastAPI, api: Api, scene: Scene, start_poll: Callable[..., Poll], wait_for_poll: Callable[[str], None]
) -> None:
    """A renewal that finds its holder idle releases the lock and wakes a waiting viewer within a second.

    Alice's last activity is written back past the idle limit while Bob waits, so her lease still runs and nothing
    wakes the poll (it would wait its full 5 seconds) until her renewal is refused as `lock_idle`. Fails with the
    wake on an idle release removed: checked.
    """
    alice, bob, team_id, note = scene
    api.take_lock(alice, team_id, note["id"])
    poll = start_poll(bob, team_id, note["id"], since=f"1:{alice.id}")
    wait_for_poll(note["id"])
    with app.state.store.engine.begin() as connection:
        connection.execute(
            update(Note).where(Note.id == note["id"]).values(lock_active_at=clock.now() - config.LOCK_IDLE_LIMIT)
        )

    renewal = api.client.put(
        lock_url(team_id, note["id"]), params={"renew": "true", "active": "false"}, headers=alice.headers
    )
    acted_at = time.monotonic()

    assert_error(renewal, 409, "lock_idle")
    response, arrived_at = poll.result()
    assert response.status_code == 200
    assert response.json() == {"tag": "1:", "version": 1, "lock": None}
    assert arrived_at - acted_at < 1.0


def test_poll_returns_when_a_lock_expires(
    api: Api, scene: Scene, start_poll: Callable[..., Poll], fake_clock: FakeClock
) -> None:
    """Nothing is written when a lease runs out, so nothing calls `wake()`.

    The poll still returns when the lock expires, because each wait is capped at the lease's remaining time.
    """
    alice, bob, team_id, note = scene
    api.take_lock(alice, team_id, note["id"])
    fake_clock.advance(config.LOCK_LEASE.total_seconds() - 0.5)  # half a second of lease left
    started = time.monotonic()

    response, arrived_at = start_poll(bob, team_id, note["id"], since=f"1:{alice.id}").result()

    # `lock: None` shows the server saw the lease run out, so no lower bound
    # on the elapsed time is needed (one tight enough to mean anything was
    # flaky on a loaded machine). The upper bound shows the poll did not wait
    # out its full 5 seconds.
    assert response.status_code == 200
    assert response.json() == {"tag": "1:", "version": 1, "lock": None}
    assert arrived_at - started < 1.5


def test_lock_renewal_wakes_nobody(
    app: FastAPI,
    api: Api,
    scene: Scene,
    start_poll: Callable[..., Poll],
    wait_for_poll: Callable[[str], None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A renewal leaves the tag unchanged and calls no `wake`, so viewers are not woken every 20 seconds per editor.

    A spurious wake would still end in 204 (the poll re-reads an unchanged tag and waits again), so the status alone
    cannot show it; the test also spies on `Notifier.wake`.
    """
    # A spurious wake would still end in 204 (the poll re-reads an unchanged
    # tag and goes back to waiting), so also record the wake() calls.
    notifier = app.state.notifier
    wake = Mock(wraps=notifier.wake)
    monkeypatch.setattr(notifier, "wake", wake)
    alice, bob, team_id, note = scene
    api.take_lock(alice, team_id, note["id"])
    assert wake.call_args_list == [call(note["id"])]
    poll = start_poll(bob, team_id, note["id"], since=f"1:{alice.id}", wait=0.3)
    wait_for_poll(note["id"])

    api.take_lock(alice, team_id, note["id"])

    response, _ = poll.result()
    assert response.status_code == 204
    assert wake.call_args_list == [call(note["id"])]


def test_deleted_note_ends_poll_with_404(
    api: Api, scene: Scene, start_poll: Callable[..., Poll], wait_for_poll: Callable[[str], None]
) -> None:
    """Deleting the note ends a waiting poll with 404 `not_found`."""
    alice, bob, team_id, note = scene
    poll = start_poll(bob, team_id, note["id"], since="1:")
    wait_for_poll(note["id"])

    assert api.delete_note(alice, team_id, note["id"], 1).status_code == 204

    response, _ = poll.result()
    assert_error(response, 404, "not_found")


def test_waiting_poll_holds_no_connection_and_cleans_up(
    app: FastAPI, api: Api, scene: Scene, start_poll: Callable[..., Poll]
) -> None:
    """Three polls wait with no database connection checked out, and their waiters are gone once they return.

    A session held across the wait would make open connections equal polls in flight (2,000 polls, 2,000
    connections). The general proof is in `test_burst.py`.
    """
    _, bob, team_id, note = scene
    notifier = app.state.notifier
    polls = [start_poll(bob, team_id, note["id"], since="1:", wait=0.5) for _ in range(3)]

    # A poll uses a connection only inside its auth check and each read, so
    # all three listening with none checked out means none waits holding one.
    wait_until(lambda: notifier.waiter_count(note["id"]) == 3 and app.state.store.engine.pool.checkedout() == 0)

    assert [poll.result()[0].status_code for poll in polls] == [204, 204, 204]
    assert notifier.waiter_count(note["id"]) == 0


@pytest.mark.parametrize("wait", [-1, config.MAX_POLL_WAIT_SECONDS + 1])
def test_wait_is_bounded(api: Api, scene: Scene, wait: int) -> None:
    """A `wait` below 0 or above 25 seconds is 422. Case ids are the `wait` sent."""
    _, bob, team_id, note = scene
    response = api.client.get(
        changes_url(team_id, note["id"]), params={"since": "1:", "wait": wait}, headers=bob.headers
    )
    assert_error(response, 422, "validation_error")


def test_change_between_first_read_and_wait_is_not_lost(
    app: FastAPI, api: Api, scene: Scene, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The lost wake-up, made deterministic: no racing threads, no sleeps.

    A save is committed and `wake` called in the exact window after the poll has read the old tag and before it waits.
    Because the poll registered its waiter before that read, the wake sets its signal and the wait returns at once.
    Listening after the read instead would sleep the full 5 seconds. It wraps the private `_read_change` because that
    is where the window opens.
    """
    _, bob, team_id, note = scene
    real_read = changes._read_change
    tags: list[str] = []
    waiters_at_first_read: list[int] = []

    def read_then_change(*args: object) -> object:
        change = real_read(*args)  # type: ignore[arg-type]
        tags.append(change.tag)
        if len(tags) == 1:
            waiters_at_first_read.append(app.state.notifier.waiter_count(note["id"]))
            with app.state.store.engine.begin() as connection:
                connection.execute(update(Note).where(Note.id == note["id"]).values(version=Note.version + 1))
            app.state.notifier.wake(note["id"])
        return change

    monkeypatch.setattr(changes, "_read_change", read_then_change)
    started = time.monotonic()

    response = api.client.get(changes_url(team_id, note["id"]), params={"since": "1:", "wait": 5}, headers=bob.headers)

    assert response.status_code == 200
    assert response.json()["version"] == 2
    assert tags == ["1:", "2:"]
    assert waiters_at_first_read == [1]
    assert time.monotonic() - started < 1.0


def test_removed_member_poll_ends_404_on_next_wake(
    api: Api, scene: Scene, start_poll: Callable[..., Poll], wait_for_poll: Callable[[str], None]
) -> None:
    """A member removed while polling gets 404 on the poll's next wake.

    Membership is re-checked on every read. Without that, the poll would return the new lock holder's name to a
    non-member.
    """
    alice, bob, team_id, note = scene
    poll = start_poll(bob, team_id, note["id"], since="1:")
    wait_for_poll(note["id"])

    assert api.client.delete(f"/teams/{team_id}/members/{bob.id}", headers=alice.headers).status_code == 204
    api.take_lock(alice, team_id, note["id"])

    response, _ = poll.result()
    error = assert_error(response, 404, "not_found")
    assert "lock" not in error


def test_removing_a_lock_holder_wakes_viewers_and_ends_their_own_poll(
    app: FastAPI, api: Api, scene: Scene, start_poll: Callable[..., Poll]
) -> None:
    """Removal releases the member's locks in the same transaction and wakes those notes.

    Viewers hear the unlock within a second, and the removed holder's own poll ends with 404.
    """
    alice, bob, team_id, note = scene
    api.take_lock(bob, team_id, note["id"])
    held = f"1:{bob.id}"
    viewer = start_poll(alice, team_id, note["id"], since=held)
    holder = start_poll(bob, team_id, note["id"], since=held)
    wait_until(lambda: app.state.notifier.waiter_count(note["id"]) == 2)

    assert api.client.delete(f"/teams/{team_id}/members/{bob.id}", headers=alice.headers).status_code == 204
    acted_at = time.monotonic()

    response, arrived_at = viewer.result()
    assert response.json() == {"tag": "1:", "version": 1, "lock": None}
    assert arrived_at - acted_at < 1.0
    response, arrived_at = holder.result()
    assert_error(response, 404, "not_found")
    assert arrived_at - acted_at < 1.0


def test_polls_per_user_are_capped(app: FastAPI, api: Api, scene: Scene) -> None:
    """A user's eleventh held poll is 429 `too_many_polls`; another user is unaffected; slots come back as polls end.

    Without a cap, 5,000 polls whose sockets close after 50 ms pin 1 GB of memory and 5,000 waiters for 27 seconds.
    """
    alice, bob, team_id, note = scene
    notifier = app.state.notifier
    url = changes_url(team_id, note["id"])
    held = {"since": "1:", "wait": 5}

    with ThreadPoolExecutor(max_workers=config.MAX_POLLS_PER_USER) as executor:
        polls = [
            executor.submit(api.client.get, url, params=held, headers=bob.headers)
            for _ in range(config.MAX_POLLS_PER_USER)
        ]
        wait_until(lambda: notifier.polls_held_by(bob.id) == config.MAX_POLLS_PER_USER)

        refused = api.client.get(url, params=held, headers=bob.headers)
        assert_error(refused, 429, "too_many_polls")
        # The cap is per user: Alice's poll is unaffected.
        assert api.client.get(url, params={"since": "1:", "wait": 0}, headers=alice.headers).status_code == 204

        api.take_lock(alice, team_id, note["id"])  # wakes Bob's polls
        assert [poll.result(timeout=5).status_code for poll in polls] == [200] * config.MAX_POLLS_PER_USER

    assert notifier.polls_held_by(bob.id) == 0
    assert api.client.get(url, params={"since": "1:", "wait": 0}, headers=bob.headers).status_code == 200


def test_client_disconnect_ends_the_wait(app: FastAPI, client: TestClient, scene: Scene) -> None:
    """A dropped poll gives back its waiter and its slot in the cap at once, not after `wait`.

    `TestClient` cannot hang up mid-request, so this drives the ASGI app directly with a client that disconnects once
    the poll is waiting. (The `client` fixture is only here to have run the app's startup.)
    """
    _, bob, team_id, note = scene
    notifier = app.state.notifier
    sent: list[dict[str, Any]] = []

    async def scenario() -> float:
        hang_up = asyncio.Event()
        messages = [{"type": "http.request", "body": b"", "more_body": False}]

        async def receive() -> dict[str, Any]:
            if messages:
                return messages.pop()
            await hang_up.wait()
            return {"type": "http.disconnect"}

        async def send(message: dict[str, Any]) -> None:
            sent.append(message)

        path = changes_url(team_id, note["id"])
        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": path,
            "raw_path": path.encode(),
            "root_path": "",
            "query_string": b"since=1:&wait=10",
            "headers": [(b"host", b"test"), (b"authorization", f"Bearer {bob.token}".encode())],
            "client": ("127.0.0.1", 50000),
            "server": ("test", 80),
        }
        request = asyncio.create_task(app(scope, receive, send))
        deadline = time.monotonic() + 2
        while notifier.polls_held_by(bob.id) == 0:
            assert time.monotonic() < deadline and not request.done()
            await asyncio.sleep(0.005)
        hung_up_at = time.monotonic()
        hang_up.set()
        await asyncio.wait_for(request, timeout=2)
        return time.monotonic() - hung_up_at

    elapsed = asyncio.run(scenario())

    assert elapsed < 0.5
    assert notifier.polls_held_by(bob.id) == 0
    assert notifier.waiter_count(note["id"]) == 0


def test_wake_after_the_event_loop_closed_is_harmless() -> None:
    """At shutdown a save can commit after the loop is gone; that must not become a 500.

    The save has already committed when it calls `wake`, so raising there would report a saved change as a failure.
    """
    notifier = Notifier(max_polls_per_user=1)
    loop = asyncio.new_event_loop()

    async def listen_and_leave_registered() -> None:
        notifier.listen("note", "user").__enter__()

    loop.run_until_complete(listen_and_leave_registered())
    loop.close()

    notifier.wake("note")  # would raise RuntimeError("Event loop is closed")


def test_server_stopping_ends_held_polls_at_once(
    app: FastAPI, api: Api, scene: Scene, start_poll: Callable[..., Poll], wait_for_poll: Callable[[str], None]
) -> None:
    """On SIGTERM `app/main.py` calls `Notifier.stop`, so shutdown is not held up by polls.

    Held polls end at once with 204; without `stop`, a server with a held poll takes up to 24 seconds to stop. Calls
    `stop` directly, since the signal hook is not installed under `TestClient`.
    """
    _, bob, team_id, note = scene
    poll = start_poll(bob, team_id, note["id"], since="1:")
    wait_for_poll(note["id"])

    app.state.notifier.stop()
    stopped_at = time.monotonic()

    response, arrived_at = poll.result()
    assert response.status_code == 204
    assert arrived_at - stopped_at < 1.0


def test_shutdown_signal_runs_the_hook_then_the_servers_own_handler() -> None:
    """On the main thread, SIGTERM and SIGINT run the shutdown callback first and then the handler they replaced.

    The replaced handler is uvicorn's, which starts the server's own shutdown, so it must still run, and after the
    callback: polls must be told to stop before the server waits for open requests. Off the main thread nothing is
    installed (the reason `TestClient` never installs it).
    """
    originals = {signum: signal.getsignal(signum) for signum in (signal.SIGTERM, signal.SIGINT)}
    order: list[str] = []
    try:
        for signum in originals:
            signal.signal(signum, lambda sig, frame: order.append(f"server {signal.Signals(sig).name}"))
        main._on_shutdown_signal(lambda: order.append("hook"))

        signal.raise_signal(signal.SIGTERM)
        signal.raise_signal(signal.SIGINT)

        assert order == ["hook", "server SIGTERM", "hook", "server SIGINT"]

        before = signal.getsignal(signal.SIGTERM)
        thread = threading.Thread(target=main._on_shutdown_signal, args=(lambda: order.append("never"),))
        thread.start()
        thread.join()
        assert signal.getsignal(signal.SIGTERM) is before
    finally:
        for signum, handler in originals.items():
            signal.signal(signum, handler)


def _call(port: int, method: str, path: str, body: Any = None, token: str | None = None) -> tuple[int, Any]:
    """One request to a live server; returns (status, parsed JSON body or None)."""
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json", **({"Authorization": f"Bearer {token}"} if token else {})},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        data = response.read()
        return response.status, json.loads(data) if data else None


def test_sigterm_to_a_real_server_ends_held_polls_with_204_at_once(database_url: str) -> None:
    """A real uvicorn process with a held poll, sent SIGTERM, answers the poll 204 at once and exits.

    The end-to-end check of the hook: started exactly as `scripts/run.sh` starts it (one worker,
    `--timeout-graceful-shutdown 3`). Without the hook the poll would be held until uvicorn's 3 s grace ran out and then
    cut off, so the client would get an error, not a 204, and the exit would take over 3 s. Mutation-checked: with the
    hook's line removed from the lifespan, the poll got a 500 after the 3 s grace in 3 of 3 runs. Takes about 3 s (a
    server start, real argon2 for one sign-up and login).
    """
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    command = [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port)]
    command += ["--timeout-graceful-shutdown", "3", "--no-access-log"]
    server = subprocess.Popen(
        command, cwd=Path(main.__file__).parent.parent, env={**os.environ, "DATABASE_URL": database_url}
    )
    try:
        wait_until(lambda: _health(port), timeout=20)
        account = {"email": "alice@example.com", "password": "correct horse", "name": "alice"}
        _call(port, "POST", "/auth/signup", account)
        login = {"email": account["email"], "password": account["password"]}
        token = _call(port, "POST", "/auth/login", login)[1]["token"]
        team_id = _call(port, "POST", "/teams", {"name": "Team"}, token)[1]["id"]
        note = _call(port, "POST", f"/teams/{team_id}/notes", {"title": "Plan"}, token)[1]
        with ThreadPoolExecutor(max_workers=1) as pool:
            poll = pool.submit(
                _call, port, "GET", f"{changes_url(team_id, note['id'])}?since={note['tag']}", None, token
            )
            # A poll at the current tag waits up to 25 s. The server cannot be asked whether it is registered yet,
            # so give it a moment; if SIGTERM still came first, the poll sees `stopping` and answers 204 all the same.
            time.sleep(0.5)
            assert not poll.done()

            server.send_signal(signal.SIGTERM)
            signalled_at = time.monotonic()

            status, _ = poll.result(timeout=10)
            assert status == 204
            assert time.monotonic() - signalled_at < 2.0
        # uvicorn re-raises the signal once it has shut down cleanly, so the exit status is -SIGTERM.
        assert server.wait(timeout=10) in (0, -signal.SIGTERM)
        assert time.monotonic() - signalled_at < 3.0
    finally:
        if server.poll() is None:
            server.kill()
            server.wait()


def _health(port: int) -> bool:
    """Whether a server answers `/health` on `port` yet."""
    try:
        return _call(port, "GET", "/health")[0] == 200
    except OSError:
        return False
