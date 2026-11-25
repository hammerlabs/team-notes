"""Many more simultaneous requests than threads or connections.

Pins "The connection rule" explained in `app/store/sql.py`: no request holds a database connection while it waits for a
worker thread. A request that took a connection in `current_user` and kept it across its later thread hops would make
open connections grow with requests in flight, and a pool capped near the thread count would deadlock.

These tests shrink the thread pool and the connection pool to 4 and send 300 requests at once. Every request must
succeed and the pool must never open a fifth connection. Mutation-checked: holding one session for the whole request
makes the burst fail with 503s.
"""

from collections.abc import Callable, Iterator
from typing import Any

import anyio.to_thread
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event

from app import config
from app.main import create_app
from helpers import Api, changes_url, lock_url, note_url, race

THREADS = 4
REQUESTS = 300


@pytest.fixture
def small_app(database_url: str, monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    """An app with 4 worker threads and 4 connections, and a 0.5 s pool timeout.

    With the rule kept, no request ever waits for a connection, so a short pool timeout costs nothing. If the rule
    breaks, the burst fails with 503s within a minute instead of hanging for ten.
    """
    monkeypatch.setattr(config, "POOL_TIMEOUT_SECONDS", 0.5)
    return create_app(database_url, worker_threads=THREADS)


@pytest.fixture
def small_client(small_app: FastAPI) -> Iterator[TestClient]:
    """A `TestClient` for `small_app`."""
    with TestClient(small_app) as client:
        yield client


async def _thread_tokens() -> float:
    """The worker-thread limit, read on the app's event loop."""
    return anyio.to_thread.current_default_thread_limiter().total_tokens


def test_thread_pool_and_connection_pool_are_one_number(client: TestClient, app: FastAPI) -> None:
    """In the production configuration the connection pool and the worker-thread limit are both `WORKER_THREADS`.

    The burst proof holds only while they are equal: with fewer connections than threads, threads would wait for
    connections.
    """
    assert app.state.store.engine.pool.size() == config.WORKER_THREADS
    assert client.portal is not None
    assert client.portal.call(_thread_tokens) == config.WORKER_THREADS


def test_requests_far_beyond_both_pools_all_succeed(small_client: TestClient, small_app: FastAPI) -> None:
    """300 simultaneous requests against 4 threads and 4 connections all succeed.

    No more than 4 connections are ever checked out, and the burst opens no more than 4. The mix crosses every kind of
    thread hop: auth only, team reads, a lock write, and long polls (spread over ten viewers to stay under the per-user
    poll cap).
    """
    api = Api(small_client)
    alice = api.signup("alice")
    team_id = api.create_team(alice)
    note = api.create_note(alice, team_id)
    api.take_lock(alice, team_id, note["id"])
    tag = api.get_note(alice, team_id, note["id"])["tag"]
    # Polls are spread over viewers so none passes the per-user poll cap.
    viewers = [api.signup(f"viewer{i}") for i in range(10)]
    for viewer in viewers:
        api.add_member(alice, team_id, viewer)

    engine = small_app.state.store.engine
    assert small_client.portal is not None
    assert small_client.portal.call(_thread_tokens) == THREADS
    peak = {"checked_out": 0, "opened": 0}

    @event.listens_for(engine, "checkout")
    def on_checkout(*_: Any) -> None:
        peak["checked_out"] = max(peak["checked_out"], engine.pool.checkedout())

    @event.listens_for(engine, "connect")
    def on_connect(*_: Any) -> None:
        peak["opened"] += 1

    # A mix that crosses every kind of hop: auth only, auth + membership +
    # handler, a write, and the async long poll's own read.
    headers = alice.headers
    poll_url, poll_params = changes_url(team_id, note["id"]), {"since": tag, "wait": 0}
    kinds: list[tuple[Callable[[int], Any], int]] = [
        (lambda i: small_client.get("/me", headers=headers), 200),
        (lambda i: small_client.get(note_url(team_id, note["id"]), headers=headers), 200),
        (lambda i: small_client.get(f"/teams/{team_id}/notes", headers=headers), 200),
        (lambda i: small_client.put(lock_url(team_id, note["id"]), headers=headers), 200),
        (lambda i: small_client.get(poll_url, params=poll_params, headers=viewers[i // len(kinds) % 10].headers), 204),
    ]
    requests = [(i, *kinds[i % len(kinds)]) for i in range(REQUESTS)]

    statuses = race(*[lambda i=i, send=send: send(i) for i, send, _ in requests])

    assert statuses == sorted(expected for _, _, expected in requests)
    assert peak["checked_out"] <= THREADS
    # Connections opened during the burst (the pool already had some from setup).
    assert peak["opened"] <= THREADS
    assert engine.pool.checkedout() == 0
