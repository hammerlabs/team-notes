"""The single source of the current time.

Lock leases and session expiry are decided against `now()`. Code must call it
as `clock.now()` (not `from app.clock import now`) so tests can replace it and
jump past a lock expiry without sleeping.
"""

from datetime import UTC, datetime


def now() -> datetime:
    """Current time as a timezone-aware UTC datetime."""
    return datetime.now(UTC)
