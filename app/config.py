"""Settings and product limits.

The only setting read from the environment is the database URL. Everything
else is a product limit from the API contract, kept in one place so the
numbers the README promises are the numbers the code enforces.
"""

import os
from datetime import timedelta


def database_url() -> str:
    """The database to use, from DATABASE_URL.

    `sqlite:///notes.db` (the default: a file in the working directory, no
    setup) or `postgresql://user:password@host:port/dbname`. See app/store.
    """
    return os.environ.get("DATABASE_URL", "sqlite:///notes.db")


SESSION_LIFETIME = timedelta(days=7)
LOCK_LEASE = timedelta(seconds=60)
# Sent with every lock as `renew_after_seconds`. A third of the lease: 20 s
# means one missed renewal is harmless; after two in a row the lease (60 s)
# has run out when the third is sent.
LOCK_RENEW_AFTER = timedelta(seconds=20)
# A lock whose holder has not been active (taken it, saved, or renewed saying
# they typed) for this long is over. About a meeting's absence; an admin can
# break the lock sooner.
LOCK_IDLE_LIMIT = timedelta(minutes=10)
MAX_POLL_WAIT_SECONDS = 25

MAX_TEAM_MEMBERS = 200
MAX_NOTES_PER_TEAM = 10_000
MAX_NAME_LENGTH = 100
MIN_PASSWORD_LENGTH = 8
MAX_PASSWORD_LENGTH = 128
MAX_TITLE_LENGTH = 200
MAX_BODY_LENGTH = 100_000

DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 100
# A cursor the server issues is about 100 characters; anything much longer is
# not one, and is refused before it is decoded.
MAX_CURSOR_LENGTH = 200
MAX_QUERY_LENGTH = 200  # search text `q`

# Requests
# A 100,000-character note body sent as UTF-8 JSON takes at most 400 KB of
# printable text (600 KB if it is all control characters, which JSON must
# escape). Escaping characters outside the Basic Multilingual Plane as `\u`
# surrogate pairs takes 12 bytes each and can exceed this cap (1.2 MB); the
# contract says to send UTF-8 unescaped.
MAX_BODY_BYTES = 1_000_000
MAX_POLLS_PER_USER = 10  # concurrent held long polls per user; the 11th gets 429

# Runtime. Sync handlers and dependencies run on a pool of this many worker
# threads, and the connection pool is capped at the same number ("The
# connection rule" in app/store/sql.py).
WORKER_THREADS = 40
# How long a request may wait for a pooled connection before 503 `busy`.
# Only reachable if a thread ever held two connections, which nothing does.
POOL_TIMEOUT_SECONDS = 10
# How long a write waits for another writer before 503 `busy`: SQLite's
# busy_timeout (its single write lock), Postgres's lock_timeout (a row lock).
BUSY_TIMEOUT_MS = 5000

# SQLite's INTEGER and Postgres's BIGINT are 64-bit signed. A larger number
# cannot be bound to a query at all, so an `If-Match` version above this names
# no version and fails the precondition (412; app/preconditions.py).
MAX_SQL_INTEGER = 2**63 - 1
