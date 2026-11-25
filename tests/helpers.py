"""Test helpers: a movable clock, URLs, API shortcuts, concurrent requests, a blocked writer, error checks.

A plain module rather than fixtures, so test files import exactly what they use and the helpers can be called with
arguments.
"""

import sqlite3
import threading
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from app import clock


class FakeClock:
    """Real time plus an offset that tests can push forward.

    Keeping real time running (rather than freezing it) matters for the long
    poll: a poll waiting for a lock to expire must see the lease run out on
    its own after the test has jumped close to the expiry.
    """

    def __init__(self) -> None:
        self._real_now = clock.now
        self._offset = timedelta()

    def now(self) -> datetime:
        """Real time plus the offset."""
        return self._real_now() + self._offset

    def advance(self, seconds: float) -> None:
        """Move the app's clock forward by `seconds`, on top of real time."""
        self._offset += timedelta(seconds=seconds)


@dataclass
class Account:
    """A signed-up, logged-in user: id, email, name and bearer token."""

    id: str
    email: str
    name: str
    token: str

    @property
    def headers(self) -> dict[str, str]:
        """The `Authorization` header for this user's requests."""
        return {"Authorization": f"Bearer {self.token}"}


# Alice (admin), Bob (member) and their team's id: the `team` fixture.
TeamOfTwo = tuple[Account, Account, str]


def with_etag(who: Account, version: int | str) -> dict[str, str]:
    """`who`'s `Authorization` header plus `If-Match` naming `version` (an int is quoted as the server's ETag is)."""
    return {**who.headers, "If-Match": f'"{version}"' if isinstance(version, int) else version}


def note_url(team_id: str, note_id: str) -> str:
    """`/teams/{team_id}/notes/{note_id}`."""
    return f"/teams/{team_id}/notes/{note_id}"


def lock_url(team_id: str, note_id: str) -> str:
    """The note's `/lock` URL."""
    return f"{note_url(team_id, note_id)}/lock"


def changes_url(team_id: str, note_id: str) -> str:
    """The note's `/changes` (long poll) URL."""
    return f"{note_url(team_id, note_id)}/changes"


class Api:
    """Thin helpers for test setup. Each asserts the happy-path status."""

    def __init__(self, client: TestClient) -> None:
        self.client = client

    def signup(self, name: str, password: str = "correct horse") -> Account:
        """Sign up and log in `name` (email `<name>@example.com`) and return the account with its token."""
        email = f"{name}@example.com"
        response = self.client.post("/auth/signup", json={"email": email, "password": password, "name": name})
        assert response.status_code == 201, response.text
        response = self.client.post("/auth/login", json={"email": email, "password": password})
        assert response.status_code == 200, response.text
        return Account(id=response.json()["user"]["id"], email=email, name=name, token=response.json()["token"])

    def create_team(self, owner: Account, name: str = "Team") -> str:
        """Create a team owned by `owner` and return its id."""
        response = self.client.post("/teams", json={"name": name}, headers=owner.headers)
        assert response.status_code == 201, response.text
        return response.json()["id"]

    def add_member(self, admin: Account, team_id: str, member: Account, role: str = "member") -> None:
        """Add `member` to the team as `admin`, with `role`."""
        response = self.client.post(
            f"/teams/{team_id}/members", json={"email": member.email, "role": role}, headers=admin.headers
        )
        assert response.status_code == 201, response.text

    def create_note(self, author: Account, team_id: str, title: str = "Plan", body: str = "") -> dict[str, Any]:
        """Create a note as `author` and return the Note."""
        response = self.client.post(
            f"/teams/{team_id}/notes", json={"title": title, "body": body}, headers=author.headers
        )
        assert response.status_code == 201, response.text
        return response.json()

    def take_lock(self, who: Account, team_id: str, note_id: str) -> dict[str, Any]:
        """Take or renew the note's lock as `who` and return the Lock."""
        response = self.client.put(lock_url(team_id, note_id), headers=who.headers)
        assert response.status_code == 200, response.text
        return response.json()

    def get_note(self, who: Account, team_id: str, note_id: str) -> dict[str, Any]:
        """Read the note as `who`."""
        response = self.client.get(note_url(team_id, note_id), headers=who.headers)
        assert response.status_code == 200, response.text
        return response.json()

    def save_note(self, who: Account, team_id: str, note_id: str, version: int, **fields: str) -> dict[str, Any]:
        """Save `fields` on the note as `who`, with `If-Match` naming `version`, and return the saved Note."""
        response = self.client.patch(note_url(team_id, note_id), json=fields, headers=with_etag(who, version))
        assert response.status_code == 200, response.text
        return response.json()

    def delete_note(self, who: Account, team_id: str, note_id: str, version: int | str | None = 1) -> Any:
        """`DELETE` the note as `who` with `If-Match` naming `version` (None: no `If-Match`); the raw response."""
        headers = who.headers if version is None else with_etag(who, version)
        return self.client.delete(note_url(team_id, note_id), headers=headers)


def race(*requests: Callable[[], Any]) -> list[int]:
    """Run `requests` at the same moment and return their status codes, sorted.

    Each request runs on its own thread, and a barrier releases them together, so they reach the server as close to
    simultaneously as threads allow. Which one the database serialises first is up to the database. Tests therefore
    assert only what must hold in every order, and the sorted result lets them compare with, for example, `[201] + [409]
    * 7` whatever the order was.
    """
    barrier = threading.Barrier(len(requests))

    def run(request: Callable[[], Any]) -> int:
        barrier.wait()
        return request().status_code

    with ThreadPoolExecutor(max_workers=len(requests)) as pool:
        return sorted(pool.map(run, requests))


@contextmanager
def signups_blocked_by_another_writer(database_url: str) -> Iterator[None]:
    """While inside, another connection holds the lock a signup must wait for.

    On SQLite that is the single write lock (`BEGIN IMMEDIATE`); on Postgres, a `SHARE` lock on the users table, which
    conflicts with inserting a row but not with reading. Either way the signup's insert waits for the busy timeout,
    which lets `test_http.py` test the 503 `busy` path on both databases with a real lock rather than a mocked error.
    """
    url = make_url(database_url)
    if url.get_backend_name() == "sqlite":
        other_writer = sqlite3.connect(url.database or "", isolation_level=None)
        other_writer.execute("BEGIN IMMEDIATE")
        try:
            yield
        finally:
            other_writer.execute("ROLLBACK")
            other_writer.close()
        return
    engine = create_engine(url)
    try:
        with engine.connect() as other_writer:
            other_writer.execute(text("LOCK TABLE users IN SHARE MODE"))
            try:
                yield
            finally:
                other_writer.rollback()
    finally:
        engine.dispose()


def assert_error(response: Any, status: int, code: str) -> dict[str, Any]:
    """Assert the contract's error shape and return the `error` object.

    Every refusal in the suite goes through here, so each one is checked for its status, its `code` and a non-empty
    `message`, not only its status. Callers check the extra keys a code carries.
    """
    assert response.status_code == status, response.text
    error = response.json()["error"]
    assert error["code"] == code
    assert isinstance(error["message"], str) and error["message"]
    return error
