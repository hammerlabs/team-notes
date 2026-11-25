"""The store's backend-specific facts: which URL picks which store, what is busy, connection settings, the list
query's plan, search folding.

Everything else about storage is tested through the API by the rest of the suite, which runs unchanged on either
database (see `conftest.py`).
"""

import sqlite3
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.exc import OperationalError

from app import config
from app.store import make_store
from app.store.interface import Busy
from app.store.postgres import PostgresStore
from app.store.sqlite import SqliteStore
from helpers import Api, FakeClock, TeamOfTwo


def test_url_picks_the_store_and_postgres_uses_psycopg() -> None:
    """A `sqlite://` URL gets `SqliteStore`; a `postgresql://` URL gets `PostgresStore` on psycopg 3.

    No database is needed: engines connect lazily.
    """
    sqlite = make_store("sqlite:///unused.db", pool_size=1)
    postgres = make_store("postgresql://user:secret@localhost:1/db", pool_size=1)

    assert isinstance(sqlite, SqliteStore)
    assert isinstance(postgres, PostgresStore)
    # A bare postgresql:// would mean psycopg2 to SQLAlchemy, which is not installed.
    assert postgres.engine.dialect.driver == "psycopg"


def test_unknown_database_url_is_refused() -> None:
    """Any other URL scheme fails at start-up with a clear error, not at the first query."""
    with pytest.raises(ValueError, match="Unsupported database URL"):
        make_store("mysql://localhost/notes", pool_size=1)


@pytest.mark.postgres_only
def test_postgres_connections_have_the_documented_settings(app: FastAPI, client: TestClient) -> None:
    """Postgres connections have `lock_timeout` equal to the busy timeout, READ COMMITTED isolation, and a UTF-8 ctype.

    `lock_timeout` is what turns a blocked write into 503 `busy`; the count guards are designed for READ COMMITTED
    (`app/store/sql.py`); ILIKE folds accented letters only under a UTF-8 ctype. Postgres only.
    """
    with app.state.store.engine.connect() as connection:
        show = connection.exec_driver_sql
        lock_timeout_ms = show("SELECT setting FROM pg_settings WHERE name = 'lock_timeout'").scalar()
        assert lock_timeout_ms == str(config.BUSY_TIMEOUT_MS)
        assert show("SHOW transaction_isolation").scalar() == "read committed"
        # ILIKE folds non-ASCII letters only under a UTF-8 LC_CTYPE (not the C locale).
        ctype = show("SELECT datctype FROM pg_database WHERE datname = current_database()").scalar()
        assert "utf8" in ctype.lower().replace("-", "")


# Busy: the errors that mean "retry", on each database.


class _PostgresError(Exception):
    """Stands in for a psycopg error: the store reads only its `sqlstate`."""

    def __init__(self, sqlstate: str) -> None:
        super().__init__(sqlstate)
        self.sqlstate = sqlstate


@pytest.mark.parametrize(
    ("url", "driver_error", "is_busy"),
    [
        ("sqlite:///unused.db", sqlite3.OperationalError("database is locked"), True),
        ("sqlite:///unused.db", sqlite3.OperationalError("disk I/O error"), False),
        ("postgresql://u@localhost:1/db", _PostgresError("55P03"), True),  # lock_not_available
        ("postgresql://u@localhost:1/db", _PostgresError("40P01"), True),  # deadlock_detected
        ("postgresql://u@localhost:1/db", _PostgresError("40001"), True),  # serialization_failure
        ("postgresql://u@localhost:1/db", _PostgresError("08006"), False),  # connection_failure
    ],
    ids=["sqlite-locked", "sqlite-io", "pg-lock-timeout", "pg-deadlock", "pg-serialization", "pg-connection"],
)
def test_only_gave_up_waiting_errors_become_busy(url: str, driver_error: Exception, is_busy: bool) -> None:
    """A database error inside an operation is `Busy` (503 `busy`, safe to retry) only if it means "gave up waiting".

    SQLite's "database is locked", and Postgres's lock timeout, deadlock victim and serialization failure, wrote
    nothing, so a retry is safe and 503 with `Retry-After` is honest. Anything else (a disk or connection failure) is a
    fault and must stay an error (500), not invite retries. Postgres's three cannot be produced on demand by the suite
    (the deadlock and serialization failure cannot happen at READ COMMITTED with this lock order), so this checks the
    mapping itself with stand-in driver errors; the 503 response for a real lock wait is
    `test_http.py::test_locked_database_is_503_busy_with_retry_after`. No database is needed: engines connect lazily.
    """
    store = make_store(url, pool_size=1)
    error = OperationalError("UPDATE notes ...", {}, driver_error)

    with pytest.raises(Busy if is_busy else OperationalError), store._session():
        raise error


def test_pool_timeout_is_busy(database_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Waiting longer than `POOL_TIMEOUT_SECONDS` for a pooled connection is `Busy` (503 `busy`), not a 500.

    Unreachable in the app while each thread holds at most one connection (`app/store/sql.py`), so it is forced here:
    a pool of one connection, held by the test, and a 0.1 s timeout.
    """
    monkeypatch.setattr(config, "POOL_TIMEOUT_SECONDS", 0.1)
    store = make_store(database_url, pool_size=1)
    try:
        with store.engine.connect(), pytest.raises(Busy):
            store.has_users()
    finally:
        store.close()


# The list query's plan on each database.


def test_a_cursor_page_is_one_range_of_the_list_index(app: FastAPI, api: Api, team: TeamOfTwo) -> None:
    """A page after a cursor is an index range on `ix_notes_team_recent`, in index order, on whichever database runs.

    The statement the API runs for a cursor page is captured and explained. SQLite must SEARCH the index with the row
    value as part of the range and need no temporary sort; Postgres (sequential scans off, so a tiny table does not hide
    the choice) must put the row comparison in the Index Cond and plan no Sort. Without this a deep page would read
    and discard every row before it, which is what keyset paging is for. Fails with the index's `id` ascending (the
    sort then disagrees with the row value): checked on both.
    """
    alice, _, team_id = team
    for i in range(3):
        api.create_note(alice, team_id, f"Note {i}")
    cursor = api.client.get(f"/teams/{team_id}/notes", params={"limit": 1}, headers=alice.headers).json()["next_cursor"]
    engine = app.state.store.engine
    captured: list[tuple[str, Any]] = []

    def record(_conn: Any, _cursor: Any, statement: str, parameters: Any, *_: Any) -> None:
        if "FROM notes" in statement:
            captured.append((statement, parameters))

    event.listen(engine, "before_cursor_execute", record)
    try:
        api.client.get(f"/teams/{team_id}/notes", params={"cursor": cursor}, headers=alice.headers)
    finally:
        event.remove(engine, "before_cursor_execute", record)
    [(statement, parameters)] = captured

    with engine.connect() as connection:
        if engine.dialect.name == "sqlite":
            rows = connection.exec_driver_sql(f"EXPLAIN QUERY PLAN {statement}", parameters).all()
            plan = "\n".join(row[-1] for row in rows)
            assert "SEARCH notes USING INDEX ix_notes_team_recent (team_id=? AND (updated_at,id)<(?,?))" in plan, plan
            assert "TEMP B-TREE" not in plan, plan
        else:
            connection.exec_driver_sql("SET enable_seqscan = off")
            plan = "\n".join(row[0] for row in connection.exec_driver_sql(f"EXPLAIN {statement}", parameters))
            assert "Index Scan using ix_notes_team_recent on notes" in plan, plan
            assert "ROW(updated_at, (id)::text) < ROW(" in plan, plan
            assert "Sort" not in plan, plan


# The one documented difference in behaviour (API_CONTRACT.md, Notes).


def _search(api: Api, who: Any, team_id: str, q: str) -> list[str]:
    """Titles found by searching `q` in the team, asserting 200."""
    response = api.client.get(f"/teams/{team_id}/notes", params={"q": q}, headers=who.headers)
    assert response.status_code == 200, response.text
    return [note["title"] for note in response.json()["items"]]


def test_search_folding_on_each_database(api: Api, team: TeamOfTwo, database_url: str, fake_clock: FakeClock) -> None:
    """Both fold accented capitals, in titles and bodies, and neither drops accents. Only SQLite matches "ß" with "SS".

    `café` finds `CAFÉ` in a title and in a body on both, but not `Cafe`: case folds, accents do not. "straße" finding
    "STRASSE" needs Unicode case folding (one character becoming two), which SQLite has here and Postgres's ILIKE does
    not: the one documented behaviour difference between the databases (contract, Notes). The test reads the URL to
    pick the expected answer, so it passes on each and fails if either changes.
    """
    alice, _, team_id = team
    for title, body in [
        ("GROSSE STRASSE", ""),
        ("CAFÉ MENU", ""),
        ("Lunch", "at LE CAFÉ ROUGE"),
        ("Cafe", "no accent"),
    ]:
        api.create_note(alice, team_id, title, body)
        fake_clock.advance(1)  # newest first: Lunch, then CAFÉ MENU

    assert _search(api, alice, team_id, "café") == ["Lunch", "CAFÉ MENU"]
    expected = [] if database_url.startswith("postgresql") else ["GROSSE STRASSE"]
    assert _search(api, alice, team_id, "straße") == expected
