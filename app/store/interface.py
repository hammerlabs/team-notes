"""What the routers may ask of storage, in the service's own words.

Routers see only this module: the `Store` operations and the outcomes they
can report. No SQL, session or driver type crosses this line, so the HTTP
layer reads the same whichever database is behind it.

Every operation is one short unit of work: it opens a connection, does its
statements, commits or not, and gives the connection back before it returns
(why that matters: "The connection rule" in `sql.py`). What it returns is plain data, safe to
read after the connection is gone.

Outcomes that are part of the domain ("the team is full", "someone else holds
the lock") are raised as `Refusal`s. Mapping them to status codes and wording
is the HTTP layer's job (`errors.store_error`).
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from app.models import Membership, Note, Team, User


class StoreError(Exception):
    """Base of everything the store reports on purpose. `errors.store_error` maps each to its HTTP answer."""


class Busy(StoreError):
    """The database could not take the work in time. Nothing was written; a retry is safe."""


class Refusal(StoreError):
    """A domain rule said no. Nothing was written."""


class EmailTaken(Refusal):
    """An account with that email already exists (signing up). 409 `email_taken`."""


class UserNotFound(Refusal):
    """No account has that email (adding a member). 404 `user_not_found`."""


class AlreadyMember(Refusal):
    """The user is already in the team (adding a member). 409 `already_member`."""


class TeamFull(Refusal):
    """The team has `config.MAX_TEAM_MEMBERS` members (adding a member). 409 `team_full`."""


class TeamNotesFull(Refusal):
    """The team has `config.MAX_NOTES_PER_TEAM` notes (creating a note). 409 `team_notes_full`."""


class LastAdmin(Refusal):
    """Removing this member would leave the team without an admin. 409 `last_admin`."""


class NotFound(Refusal):
    """No such team, note or member, as far as the caller may know. 404 `not_found`.

    `what` is "Team", "Note" or "Member".
    """

    def __init__(self, what: str) -> None:
        super().__init__(what)
        self.what = what


class NotAuthor(Refusal):
    """Deleting a note: the caller is neither its author nor a team admin. 403 `forbidden`."""


class LockHeldByAnother(Refusal):
    """Releasing a lock: someone else holds it and the caller is not an admin. 403 `forbidden`."""


class LockRequired(Refusal):
    """The caller holds no live lock, and nobody else does either. 409 `lock_required`."""


class LockIdle(Refusal):
    """A renewal found the caller's lock idle past `config.LOCK_IDLE_LIMIT` and released it. 409 `lock_idle`.

    Raised by the router, not the store: the store reports the release as a
    `LockOutcome` so the router can wake viewers first.
    """


class NoteRefusal(Refusal):
    """A refusal that shows the note as it was when the error was worded.

    `now` is the instant leases were judged against, so the error reports
    the same lock the decision saw.
    """

    def __init__(self, note: Note | None, now: datetime) -> None:
        super().__init__(type(self).__name__)
        self.note = note
        self.now = now


class NoteLocked(NoteRefusal):
    """Someone else holds a live lock on `note`. 409 `note_locked`.

    `note` is None when the lock changed hands twice while the refusal was
    being worded; the contract tells the client to retry.
    """


class VersionConflict(NoteRefusal):
    """The write named a version other than `note`'s current one, or none.

    412 `precondition_failed` carrying `note` (428 `precondition_required`
    when the request sent no `If-Match` at all).
    """


class LockOutcome(Enum):
    """What `take_lock` did: the router wakes viewers for a take and for an idle release."""

    TAKEN = "taken"  # the tag changed: viewers must be woken
    RENEWED = "renewed"  # same holder, same tag: nobody needs waking
    IDLE_RELEASED = "idle_released"  # a renewal found the holder idle and released the lock: wake, then refuse


@dataclass(frozen=True)
class NotePosition:
    """A place in the note list: the sort key of the last note a page showed.

    The list's order is `(updated_at DESC, id DESC)`, a total order, so this
    pair names one point in it whether or not that note still exists.
    """

    updated_at: datetime
    id: str


@dataclass(frozen=True)
class NotePage:
    """Notes of one page, and whether any note follows the page."""

    notes: list[Note]
    has_more: bool


class Store(ABC):
    """Every read and write the service makes.

    Times are passed in, not read here: a lease is judged against the
    instant the request arrived (`clock.now()` in the router), which is also
    the instant the response is built for.
    """

    # Lifecycle

    @abstractmethod
    def create_schema(self) -> None:
        """Create any missing tables. Existing tables are left as they are (there are no migrations)."""

    @abstractmethod
    def close(self) -> None:
        """Close every pooled connection."""

    # Accounts and sessions

    @abstractmethod
    def create_user(self, email: str, name: str, password_hash: str, now: datetime) -> User:
        """A new account. Raises `EmailTaken`."""

    @abstractmethod
    def user_by_email(self, email: str) -> User | None: ...

    @abstractmethod
    def has_users(self) -> bool:
        """Whether any account exists. The seed's `--if-empty` check."""

    @abstractmethod
    def create_session(self, token_hash: str, user_id: str, expires_at: datetime) -> None: ...

    @abstractmethod
    def user_for_token(self, token_hash: str, now: datetime) -> User | None:
        """The user behind an unexpired session, or None."""

    # Teams and membership

    @abstractmethod
    def create_team(self, name: str, admin_id: str, now: datetime) -> Team:
        """A new team with `admin_id` as its admin, in one transaction."""

    @abstractmethod
    def teams_of(self, user_id: str) -> list[tuple[Team, str]]:
        """The user's teams with their role in each, oldest first."""

    @abstractmethod
    def team(self, team_id: str) -> Team | None: ...

    @abstractmethod
    def membership(self, team_id: str, user_id: str) -> Membership | None: ...

    @abstractmethod
    def members(self, team_id: str) -> list[tuple[Membership, User]]:
        """Everyone in the team, in joining order."""

    @abstractmethod
    def add_member_if_room(self, team_id: str, email: str, role: str, now: datetime) -> User:
        """Add the account with `email`; returns it.

        Raises `UserNotFound`, `AlreadyMember`, or `TeamFull` at
        `config.MAX_TEAM_MEMBERS`. The cap holds under concurrent adds.
        """

    @abstractmethod
    def remove_member_keeping_an_admin(self, team_id: str, user_id: str) -> list[str]:
        """Remove a member and release their locks on the team's notes, in one transaction.

        Returns the ids of the notes whose lock was released (to wake their
        viewers). Raises `NotFound("Member")`, or `LastAdmin` if this would
        leave the team without an admin, also under concurrent removals.
        """

    # Notes

    @abstractmethod
    def create_note_if_room(self, team_id: str, author_id: str, title: str, body: str, now: datetime) -> Note:
        """A new note, version 1. Raises `TeamNotesFull` at `config.MAX_NOTES_PER_TEAM`, also under concurrency."""

    @abstractmethod
    def notes_page(self, team_id: str, search: str | None, limit: int, after: NotePosition | None) -> NotePage:
        """Up to `limit` notes of the team that sort after `after` (None: from the start), without bodies.

        Order: newest edit first, then id descending. Reading `body` raises.
        `search` is a case-insensitive substring of title or body, matched
        literally (`%` and `_` are not wildcards). How case is folded is the
        one place the implementations differ visibly; see each one.
        """

    @abstractmethod
    def note(self, team_id: str, note_id: str) -> Note:
        """The note with its lock holder's id and name. Raises `NotFound("Note")`."""

    @abstractmethod
    def note_change_for_member(self, team_id: str, note_id: str, user_id: str) -> Note:
        """What the long poll compares: version and lock, without the body.

        Re-checks membership, so a member removed mid-poll stops seeing the
        note. Raises `NotFound("Team")` or `NotFound("Note")`.
        """

    @abstractmethod
    def save_note(
        self,
        team_id: str,
        note_id: str,
        user_id: str,
        version: int | None,
        title: str | None,
        body: str | None,
        now: datetime,
    ) -> Note:
        """Save if `user_id` holds a live lock and `version` is current. Returns the row written.

        A save is activity: it records `now` as the holder's last activity.

        `version` None means the request named no usable version: the save
        cannot succeed, and the refusal still reports the lock first. The
        returned note's holder is the caller and is not loaded. Raises, in
        this order of precedence, `NotFound`, `NoteLocked` or `LockRequired`,
        then `VersionConflict`.
        """

    @abstractmethod
    def delete_note(
        self, team_id: str, note_id: str, user_id: str, is_admin: bool, version: int | None, now: datetime
    ) -> None:
        """Delete if the caller is author or admin, nobody else holds a live lock, and `version` is current.

        `version` None never matches, as for `save_note`. Raises, in this
        order of precedence, `NotFound`, `NotAuthor`, `NoteLocked`, then
        `VersionConflict`.
        """

    # The edit lock

    @abstractmethod
    def take_lock(
        self,
        team_id: str,
        note_id: str,
        user_id: str,
        now: datetime,
        expires_at: datetime,
        renew_only: bool,
        active: bool,
    ) -> LockOutcome:
        """Renew the caller's live lock, or (unless `renew_only`) take a free, expired or idle one.

        A take records `now` as the holder's last activity; a renewal does so
        only if `active`. A renew-only call on the caller's own lock whose
        holder is idle past `config.LOCK_IDLE_LIMIT` releases it and returns
        `IDLE_RELEASED`. Raises `NoteLocked`, `LockRequired` (renew-only,
        nothing to renew) or `NotFound`.
        """

    @abstractmethod
    def release_lock(self, team_id: str, note_id: str, user_id: str, is_admin: bool, now: datetime) -> bool:
        """Release the caller's lock, or any live lock for an admin. True if one was released.

        No live lock is not an error (False). Raises `LockHeldByAnother`,
        `NoteLocked` with no note (retry), or `NotFound`.
        """
