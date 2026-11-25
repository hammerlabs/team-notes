"""Storage behind one interface, with a SQLite and a Postgres implementation.

    interface.py   what routers may call (`Store`) and the refusals it raises
    sql.py         the operations, written once in SQLAlchemy; the conditional-write design
    sqlite.py      SQLite: PRAGMAs, Unicode casefold search, single-writer guards, "database is locked"
    postgres.py    Postgres: pool, ILIKE search, team row lock for count guards, lock_timeout

`DATABASE_URL` picks one (see `config.database_url`).
"""

from sqlalchemy.engine import make_url

from app.store.interface import Store
from app.store.postgres import PostgresStore
from app.store.sqlite import SqliteStore


def make_store(url: str, pool_size: int) -> Store:
    """The store for a SQLAlchemy database URL, with at most `pool_size` connections."""
    parsed = make_url(url)
    backend = parsed.get_backend_name()
    if backend == "sqlite":
        return SqliteStore(url, pool_size)
    if backend == "postgresql":
        # A bare postgresql:// URL means psycopg2 to SQLAlchemy, which is not
        # installed. psycopg 3 is the driver this project ships with.
        if parsed.drivername == "postgresql":
            parsed = parsed.set(drivername="postgresql+psycopg")
        return PostgresStore(parsed.render_as_string(hide_password=False), pool_size)
    raise ValueError(f"Unsupported database URL {parsed!r}: use sqlite:///<path> or postgresql://...")
