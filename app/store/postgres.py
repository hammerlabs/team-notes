"""Postgres, through psycopg 3 (sync), at READ COMMITTED.

Everything in `sql.py` runs here unchanged except the four hooks. The one
that matters for correctness is `_lock_team`: Postgres runs writers
concurrently, so a guard that counts other rows needs a lock to be atomic.
"""

from typing import Any

from sqlalchemy import ColumnElement, Engine, create_engine, event, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app import config
from app.models import Team
from app.store.sql import SqlStore

_BUSY_SQLSTATES = {
    "55P03",  # lock_not_available: waited longer than lock_timeout for a row or table lock
    "40P01",  # deadlock_detected: chosen as the victim (no lock order here allows one; kept as a backstop)
    "40001",  # serialization_failure: not reachable at READ COMMITTED; listed so a stricter level maps the same
}
"""SQLSTATEs that mean "this transaction gave up or was cancelled because of another one; nothing it did was kept".

All three are answered 503 `busy` with Retry-After, like SQLite's "database is locked".
"""


class PostgresStore(SqlStore):
    """The store on Postgres: `SqlStore` plus the four hooks, each documented below."""

    def _make_engine(self, url: str, pool_size: int) -> Engine:
        """A psycopg engine at READ COMMITTED, capped at `pool_size`, testing each connection on checkout.

        - READ COMMITTED: the guards' reasoning (`_lock_team`) assumes each statement sees everything committed
          before it started. It is Postgres's default; stated so a server default cannot change it.
        - `pool_size` with no overflow: the cap from the connection rule in `app/store/sql.py`.
        - `pool_pre_ping`: a server restart leaves dead sockets in the pool; testing each on checkout (one round trip)
          beats failing one request per pooled connection. A SQLite file has no such failure mode.
        - Every new connection gets `lock_timeout` (`_configure_connection`).
        """
        engine = create_engine(
            url,
            isolation_level="READ COMMITTED",
            pool_size=pool_size,
            max_overflow=0,
            pool_timeout=config.POOL_TIMEOUT_SECONDS,
            pool_pre_ping=True,
        )
        event.listen(engine, "connect", _configure_connection)
        return engine

    def _contains_folded(self, column: ColumnElement[str], text: str) -> ColumnElement[bool]:
        """ILIKE: Postgres lowercases both sides one character at a time; `autoescape` makes `%` and `_` literal.

        Lowercasing follows the database's character type: the libc `en_US.utf8` of the official `postgres` image, or
        the builtin `C.UTF-8` locale the Docker image here creates. Either way "CAFÉ" matches "café". It is
        lowercasing, not Unicode case folding as on SQLite: one character never becomes two, so "straße" does not
        match "STRASSE", and final sigma "ς" does not match "σ". In a database created with the plain C locale only
        ASCII would fold. Postgres 18's `casefold()` would close the gap; it is not used, so behaviour is the same on
        Postgres 16 and 17, which the suite passes on.
        """
        return column.icontains(text, autoescape=True)

    def _lock_team(self, db: Session, team_id: str) -> None:
        """Lock the team's row (`SELECT ... FOR UPDATE`), as its own statement, before the count.

        Two transactions counting members (or notes, or admins) at once would each see only their own snapshot and both
        pass the cap. The row lock makes them take turns: the second waits here until the first commits. At READ
        COMMITTED its *next* statement, the count, then takes a fresh snapshot that includes the first's row. So the
        lock must be its own statement, before the count: a lock inside the counting statement would count from the
        old snapshot.

        SERIALIZABLE would also be correct, but the loser fails with a serialization error to retry (a 503 here)
        instead of queueing for a few milliseconds and getting the right 409. The lock is held only for one short
        transaction, and only guarded writes of this one team wait on it.
        """
        db.execute(select(Team.id).where(Team.id == team_id).with_for_update())

    def _is_busy(self, error: OperationalError) -> bool:
        """True for the SQLSTATEs in `_BUSY_SQLSTATES`: a lock wait timed out, or a deadlock or serialization victim."""
        return getattr(error.orig, "sqlstate", None) in _BUSY_SQLSTATES


def _configure_connection(dbapi_connection: Any, _record: Any) -> None:
    """Runs on every new connection: `lock_timeout` equal to `config.BUSY_TIMEOUT_MS`, committed.

    Postgres has no single write lock, but a write can wait on a row lock (another save of the same note, or a team's
    guard). This bounds that wait like SQLite's busy_timeout; past it the statement fails with lock_not_available,
    which becomes 503 `busy`. Committed, because the pool rolls back every connection it takes back, and a SET inside a
    rolled-back transaction is undone with it.
    """
    with dbapi_connection.cursor() as cursor:
        cursor.execute(f"SET lock_timeout = {int(config.BUSY_TIMEOUT_MS)}")
    dbapi_connection.commit()
