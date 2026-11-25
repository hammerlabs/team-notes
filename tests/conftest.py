"""Shared fixtures: a fresh app and database per test, a movable clock, API helpers, and a team.

Tests talk to the real app over HTTP (FastAPI's `TestClient`) and to a real database. Nothing in the database layer is
mocked. The only stand-ins are the clock (`fake_clock`, autouse) and the password hasher's cost parameters
(`cheap_password_hashing`, autouse).

Which database: SQLite, a new file per test, unless `TEST_DATABASE_URL` names a Postgres server. Then each test gets its
own new schema on that server (the app's connections see only it, through `search_path`), dropped afterwards. A
throwaway server for this:

```sh
docker run -d --name team-notes-pgtest -e POSTGRES_PASSWORD=pgtest -p 127.0.0.1:55439:5432 \\
  postgres:17 -c fsync=off -c synchronous_commit=off -c full_page_writes=off
TEST_DATABASE_URL=postgresql://postgres:pgtest@127.0.0.1:55439/postgres uv run pytest
docker rm -fv team-notes-pgtest
```

The fsync flags only make the throwaway server faster; they change nothing the tests check. `-v` removes the container's
anonymous data volume with it. `postgres:17` matches the Docker image; the suite also passes on 16. The report header
says which database a run used.

Every test runs on either database unchanged. The few that inspect one database's internals are marked `sqlite_only` or
`postgres_only` and skipped, with a reason, on the other; so a full run shows one skip on each.
"""

import os
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from argon2 import PasswordHasher
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import make_url

from app import clock, security
from app.main import create_app
from helpers import Api, FakeClock, TeamOfTwo

POSTGRES_URL = os.environ.get("TEST_DATABASE_URL")
ON_POSTGRES = POSTGRES_URL is not None


def pytest_report_header() -> str:
    """Print which database this run uses, so a Postgres run is never mistaken for a SQLite one."""
    return f"database: {'postgres (TEST_DATABASE_URL)' if ON_POSTGRES else 'sqlite'}"


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Skip tests marked for the other database, with the reason shown in the report."""
    for item in items:
        if ON_POSTGRES and item.get_closest_marker("sqlite_only"):
            item.add_marker(
                pytest.mark.skip(reason="inspects SQLite internals; the Postgres equivalent is its own test")
            )
        if not ON_POSTGRES and item.get_closest_marker("postgres_only"):
            item.add_marker(pytest.mark.skip(reason="needs TEST_DATABASE_URL"))


@pytest.fixture(scope="session")
def postgres_admin() -> Iterator[Engine | None]:
    """A connection to the test server for creating and dropping schemas (`None` on SQLite).

    Session-scoped so the whole run shares one engine. Autocommit, because `CREATE SCHEMA` and `DROP SCHEMA` should take
    effect at once rather than wait in a transaction.
    """
    if POSTGRES_URL is None:
        yield None
        return
    url = make_url(POSTGRES_URL)
    if url.drivername == "postgresql":
        url = url.set(drivername="postgresql+psycopg")
    engine = create_engine(url, isolation_level="AUTOCOMMIT")
    yield engine
    engine.dispose()


@pytest.fixture
def database_url(tmp_path: Path, postgres_admin: Engine | None) -> Iterator[str]:
    """A URL to an empty database that only this test uses.

    On SQLite, a file in the test's temporary directory. On Postgres, a new schema named `test_<uuid>` with
    `search_path` set in the URL, so the app needs no schema awareness at all.
    """
    if postgres_admin is None:
        yield f"sqlite:///{tmp_path / 'notes.db'}"
        return
    # A schema per test: creating one and its five tables takes milliseconds,
    # and nothing can leak between tests, unlike truncating shared tables.
    schema = f"test_{uuid.uuid4().hex}"
    with postgres_admin.connect() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    url = postgres_admin.url.update_query_dict({"options": f"-csearch_path={schema}"})
    yield url.render_as_string(hide_password=False)
    with postgres_admin.connect() as connection:
        connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))


@pytest.fixture(autouse=True)
def fake_clock(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    """Replace `app.clock.now` with a `FakeClock` in every test.

    Autouse, so lease and token expiry are always under the test's control: a test moves time with
    `fake_clock.advance(seconds)` and never sleeps to reach an expiry.
    """
    fake = FakeClock()
    monkeypatch.setattr(clock, "now", fake.now)
    return fake


@pytest.fixture(autouse=True)
def cheap_password_hashing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Swap the argon2 hasher for a cheap one with the same algorithm.

    Production argon2 costs about 50 ms and 64 MiB per hash, and the suite signs up hundreds of users. The dummy hash
    used for unknown emails is made at import, before this swap, so it keeps the production cost
    (`test_auth.py::test_the_dummy_hash_has_the_production_cost`).
    """
    monkeypatch.setattr(security, "_hasher", PasswordHasher(time_cost=1, memory_cost=8, parallelism=1))


@pytest.fixture
def app(database_url: str) -> FastAPI:
    """A new app on this test's own database, with production pool and thread sizes.

    Built per test, so nothing (notifier waiters, pool state, monkeypatched limits) leaks between tests.
    """
    return create_app(database_url)


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    """A `TestClient` for `app` with its lifespan run: tables created, thread limit set."""
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def api(client: TestClient) -> Api:
    """`Api` shortcuts over `client`, for setup steps that must succeed."""
    return Api(client)


@pytest.fixture
def team(api: Api) -> TeamOfTwo:
    """Alice (admin) and Bob (member) in one team: the most common starting point."""
    alice, bob = api.signup("alice"), api.signup("bob")
    team_id = api.create_team(alice)
    api.add_member(alice, team_id, bob)
    return alice, bob, team_id
