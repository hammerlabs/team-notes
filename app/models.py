"""SQLAlchemy tables, plus the SQL conditions the store's statements share.

The same schema serves SQLite and Postgres (see app/store), so every column
type here is chosen to behave the same on both.

IDs are random UUIDs, so they cannot be guessed or counted. They are stored
as 36-character strings, not Postgres's UUID type: an id in a URL is then
compared as text on both databases, so a malformed one is simply not found
(404) instead of a cast error, and the two backends store the same values. The edit lock is
three columns on `notes` rather than a table of its own: a save must check "do
I hold the lock" and "is my version current" together, and with both on one
row that is a single conditional UPDATE.

A lock is live while its lease has not run out and its holder has been active
within `config.LOCK_IDLE_LIMIT`. Both conditions are in `lock_is_live`, so
every statement and every read that judges the lock applies both.
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ColumnElement,
    DateTime,
    Dialect,
    ForeignKey,
    Index,
    String,
    Text,
    TypeDecorator,
    and_,
    or_,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from app import config


class UTCDateTime(TypeDecorator[datetime]):
    """Stores aware UTC datetimes as naive UTC and returns them aware again.

    SQLite has no timezone type and keeps these as fixed-width text, which
    compares in time order; Postgres keeps them as `timestamp without time
    zone`, which compares natively. Both hold microseconds, the precision of
    a Python datetime. Normalising to UTC on the way in is what makes either
    comparison mean the same instant, so the session's time zone setting on
    Postgres never enters into it (no `timestamptz` arithmetic to reason about).
    """

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            # A naive datetime has no defined instant: astimezone() would read
            # it as the server's local time and silently shift every lease
            # and expiry compared against it. Every caller uses clock.now().
            raise ValueError("UTCDateTime needs a timezone-aware datetime")
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        return value.replace(tzinfo=UTC) if value else None


def new_id() -> str:
    """A new random UUID string."""
    return str(uuid.uuid4())


class Base(DeclarativeBase):
    """Declarative base; `Base.metadata.create_all` builds the schema at startup."""


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    email: Mapped[str] = mapped_column(String(320), unique=True)
    name: Mapped[str] = mapped_column(String(100))
    password_hash: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)


class AuthSession(Base):
    """A login. Named to avoid a clash with SQLAlchemy's `Session`."""

    __tablename__ = "sessions"

    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime)


class Team(Base):
    __tablename__ = "teams"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)


class Membership(Base):
    __tablename__ = "memberships"
    __table_args__ = (
        CheckConstraint("role IN ('admin', 'member')", name="ck_memberships_role"),
        # GET /teams looks memberships up by user; the primary key starts with team_id.
        Index("ix_memberships_user_id", "user_id"),
    )

    team_id: Mapped[str] = mapped_column(ForeignKey("teams.id"), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), primary_key=True)
    role: Mapped[str] = mapped_column(String(10))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


class Note(Base):
    __tablename__ = "notes"
    __table_args__ = (
        # The handlers always set or clear the three lock columns together.
        # The database refuses a half-set lock, which every lock check would
        # otherwise have to interpret.
        CheckConstraint(
            "(lock_user_id IS NULL) = (lock_expires_at IS NULL) AND (lock_user_id IS NULL) = (lock_active_at IS NULL)",
            name="ck_notes_lock_columns_together",
        ),
        CheckConstraint("version >= 1", name="ck_notes_version_positive"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    team_id: Mapped[str] = mapped_column(ForeignKey("teams.id"))
    author_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    title: Mapped[str] = mapped_column(String(200))
    body: Mapped[str] = mapped_column(Text, default="")
    # 64-bit on both databases (Postgres's plain INTEGER is 32-bit), so the
    # contract's version range is the same whichever is behind it.
    version: Mapped[int] = mapped_column(BigInteger, default=1)
    lock_user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"))
    lock_expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    # When the holder was last active: took the lock, saved, or renewed saying
    # they had typed. Judged against `config.LOCK_IDLE_LIMIT`.
    lock_active_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime)

    # Never loaded implicitly: a query that needs the holder asks for it with
    # `with_lock_holder()`, which loads only the id and name a response shows
    # (not the email or password hash). Forgetting it raises instead of
    # quietly issuing one extra query per note.
    lock_holder: Mapped[User | None] = relationship(foreign_keys=[lock_user_id], lazy="raise")

    def live_lock_user_id(self, now: datetime) -> str | None:
        """The lock holder's id, or None if unlocked, the lease has run out, or the holder is idle.

        The Python twin of `lock_is_live`, for reads.
        """
        if (
            self.lock_user_id is not None
            and self.lock_expires_at is not None
            and self.lock_active_at is not None
            and self.lock_expires_at > now
            and self.lock_active_at > idle_cutoff(now)
        ):
            return self.lock_user_id
        return None

    def tag(self, now: datetime) -> str:
        """The note's tag, `<version>:<live lock holder or empty>`: what a long poll waits on.

        It changes on save, lock and unlock, but not on renewal (same holder),
        and an expired or idle lock counts as no holder.
        """
        return f"{self.version}:{self.live_lock_user_id(now) or ''}"


# The note list: one team, newest edit first, with id as a stable tie-breaker.
# Both sort columns descend, in the list's order (`SqlStore.notes_page`), so
# the keyset condition `(updated_at, id) < (?, ?)` is one range of this index.
Index("ix_notes_team_recent", Note.team_id, Note.updated_at.desc(), Note.id.desc())


def note_in_team(team_id: str, note_id: str) -> ColumnElement[bool]:
    """SQL condition: the row is note `note_id` and belongs to `team_id`.

    Filtering on team_id as well as id means a member of team B cannot reach
    team A's note through team B's path.
    """
    return and_(Note.id == note_id, Note.team_id == team_id)


def idle_cutoff(now: datetime) -> datetime:
    """The instant a holder must have been active after for their lock to be live at `now`."""
    return now - config.LOCK_IDLE_LIMIT


def lock_is_live(now: datetime) -> ColumnElement[bool]:
    """SQL condition: the note is locked, the lease has not run out, and the holder is not idle."""
    return and_(Note.lock_expires_at > now, Note.lock_active_at > idle_cutoff(now))


def lock_is_idle(now: datetime) -> ColumnElement[bool]:
    """SQL condition: the lease is still running but the holder has been idle past the limit."""
    return and_(Note.lock_expires_at > now, Note.lock_active_at <= idle_cutoff(now))


def lock_held_by(user_id: str, now: datetime) -> ColumnElement[bool]:
    """SQL condition: `user_id` holds a live lock on the note."""
    return and_(Note.lock_user_id == user_id, lock_is_live(now))


def lock_available_to(user_id: str, now: datetime) -> ColumnElement[bool]:
    """SQL condition: the note's lock is free, expired, idle, or already `user_id`'s.

    Treating an expired lease or an idle holder as free inside the statement
    that takes it is what lets locks lapse with no cleanup job.
    """
    return or_(
        Note.lock_user_id.is_(None),
        Note.lock_expires_at <= now,
        Note.lock_active_at <= idle_cutoff(now),
        Note.lock_user_id == user_id,
    )
