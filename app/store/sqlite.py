"""SQLite: the default store, one file, no server.

SQLite runs one writer at a time. That single fact is why the count-based
guards need nothing extra here (`_lock_team`), and why a write can find the
database busy at all (`_is_busy`).
"""

import sqlite3
from typing import Any

from sqlalchemy import ColumnElement, Engine, create_engine, event, func
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app import config
from app.store.sql import SqlStore


class SqliteStore(SqlStore):
    """The store on SQLite: `SqlStore` plus the four hooks, each documented below."""

    def _make_engine(self, url: str, pool_size: int) -> Engine:
        """An engine capped at `pool_size`, usable from any worker thread, with per-connection settings.

        - `check_same_thread=False`: sync code runs on a thread pool, so a pooled connection may be used by a different
          thread than the one that opened it. Safe under the connection rule in `app/store/sql.py`.
        - `pool_size` with no overflow: the cap from the same rule.
        - Every new connection gets its PRAGMAs and the `casefold` function (`_configure_connection`).
        """
        engine = create_engine(
            url,
            connect_args={"check_same_thread": False},
            pool_size=pool_size,
            max_overflow=0,
            pool_timeout=config.POOL_TIMEOUT_SECONDS,
        )
        event.listen(engine, "connect", _configure_connection)
        return engine

    def _contains_folded(self, column: ColumnElement[str], text: str) -> ColumnElement[bool]:
        """Both sides through Python's `str.casefold` (full Unicode case folding); `%` and `_` are literal.

        SQLite's own `lower()` and `LIKE` fold only ASCII, so "CAFÉ" would not match "café". Full folding also turns one
        character into two where Unicode says so: "STRASSE" matches "straße". `autoescape` escapes `%` and `_`. The
        function is registered on each connection by `_configure_connection`.
        """
        return func.casefold(column).contains(text.casefold(), autoescape=True)

    def _lock_team(self, db: Session, team_id: str) -> None:
        """Nothing to do: SQLite already serialises the count and the write.

        The counting statement is itself the transaction's first write, so the driver has already issued BEGIN and
        SQLite holds its single write lock while the statement counts and inserts (or deletes). No other write can
        land between the count and the act.
        """

    def _is_busy(self, error: OperationalError) -> bool:
        """True for "database is locked": another writer held the lock past busy_timeout, so nothing was written."""
        return "database is locked" in str(error.orig)


def _configure_connection(dbapi_connection: sqlite3.Connection, _record: Any) -> None:
    """Runs on every new connection, since SQLite keeps these settings per connection.

    - `journal_mode=WAL`: readers never block the writer, and the writer never blocks readers.
    - `foreign_keys=ON`: SQLite leaves foreign keys off unless asked, per connection.
    - `busy_timeout`: a write waits this long for another writer to finish, then fails with "database is locked",
      which becomes 503 `busy`.
    - `synchronous=FULL`: sync the WAL on every commit, so an acknowledged save survives power loss. (NORMAL would be
      faster and is only crash-safe for the process.)
    - `casefold()`, for search (`SqliteStore._contains_folded`). It is called once per row searched, slower than a
      built-in, but a substring search scans every row of the team either way; a stored casefolded column would cost
      a schema change and a second copy of every body to keep in step.

    How transactions start: the driver runs in Python's legacy transaction mode, which issues BEGIN (deferred) just
    before the first INSERT, UPDATE or DELETE and never before a SELECT. So reads before an operation's first write
    run outside any transaction, each on a fresh snapshot, and a write transaction begins with a write. That is why no
    operation can fail on upgrading a stale read snapshot to a write (SQLITE_BUSY_SNAPSHOT): there is no read snapshot
    to upgrade.
    """
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute(f"PRAGMA busy_timeout={int(config.BUSY_TIMEOUT_MS)}")
    cursor.execute("PRAGMA synchronous=FULL")
    cursor.close()
    dbapi_connection.create_function("casefold", 1, _casefold, deterministic=True)


def _casefold(value: str | None) -> str | None:
    """`str.casefold`, passing NULL through: the SQL function `casefold()` registered on each connection."""
    return None if value is None else value.casefold()
