"""Pydantic request and response models: the wire format from API_CONTRACT.md.

Request models carry the contract's limits, so a body that breaks one is
refused with 422 before any handler runs. They also refuse keys they do not
declare: a misspelt field (`titel`) or one the server sets itself
(`author_id`, `team_id`) is an error, not something silently dropped.
Response models for notes are built with an explicit `now`, because whether
a lock is live, and so the tag, depends on the time of the request.
"""

import unicodedata
from datetime import datetime
from typing import Annotated, Any, Literal, Self

from pydantic import AfterValidator, BaseModel, ConfigDict, EmailStr, Field, StringConstraints, model_validator
from pydantic.networks import validate_email

from app import config
from app.cursor import Cursor
from app.models import Note, User

Role = Literal["admin", "member"]


# An email address as stored: what `EmailStr` yields, lower-cased.
Email = Annotated[EmailStr, AfterValidator(str.lower)]


def normalize_email(address: str) -> str:
    """The same rule as `Email`, for login, which must not answer a bad address with 422.

    `EmailStr` strips whitespace and unwraps "Name <addr>"; login must do the
    same, or a user could not log in with the string they signed up with.
    Raises `ValueError` if `address` is not a valid email address.
    """
    return validate_email(address)[1].lower()


def _reject_control_characters(text: str) -> str:
    # SQLite cuts a LIKE pattern at a NUL, so "%<NUL>..." would match every
    # note; in a title a newline or NUL would break every list that shows it.
    if any(unicodedata.category(char) == "Cc" for char in text):
        raise ValueError("must not contain control characters")
    return text


SearchText = Annotated[str, AfterValidator(_reject_control_characters)]

# A note title: surrounding spaces are dropped before the length check, so a
# title of only spaces is empty and refused.
Title = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=config.MAX_TITLE_LENGTH),
    AfterValidator(_reject_control_characters),
]


def _reject_nul(text: str) -> str:
    # Postgres cannot store U+0000 in a text column at all. Refused on every
    # database, so the same body is valid or invalid whichever runs.
    if "\x00" in text:
        raise ValueError("must not contain the NUL character (U+0000)")
    return text


# Free text that is stored: names and note bodies. Newlines and tabs are fine.
StoredText = Annotated[str, AfterValidator(_reject_nul)]


# Requests


class RequestModel(BaseModel):
    """Base of every request body: unknown keys are a 422."""

    model_config = ConfigDict(extra="forbid")


class SignupIn(RequestModel):
    email: Email
    password: str = Field(min_length=config.MIN_PASSWORD_LENGTH, max_length=config.MAX_PASSWORD_LENGTH)
    name: StoredText = Field(min_length=1, max_length=config.MAX_NAME_LENGTH)


class LoginIn(RequestModel):
    # Not EmailStr: a malformed address is just wrong credentials, not a 422.
    # The login handler applies `normalize_email` itself.
    email: str = Field(max_length=320)
    password: str = Field(max_length=config.MAX_PASSWORD_LENGTH)


class TeamIn(RequestModel):
    name: StoredText = Field(min_length=1, max_length=config.MAX_NAME_LENGTH)


class MemberIn(RequestModel):
    email: Email
    role: Role = "member"


class NoteIn(RequestModel):
    title: Title
    body: StoredText = Field(default="", max_length=config.MAX_BODY_LENGTH)


class NoteListQuery(RequestModel):
    """The note list's query string.

    Unknown parameters are a 422 like unknown body keys, and `offset` is one:
    ignoring it would hand an offset-paging client the first page again and
    again.
    """

    q: SearchText | None = Field(default=None, max_length=config.MAX_QUERY_LENGTH)
    limit: int = Field(default=config.DEFAULT_PAGE_SIZE, ge=1, le=config.MAX_PAGE_SIZE)
    cursor: Cursor | None = Field(default=None, description="`next_cursor` from the previous page; omit for the first.")


class NotePatch(RequestModel):
    """A save. The version the edit was based on travels in `If-Match`, not here."""

    title: Title | None = None
    body: StoredText | None = Field(default=None, max_length=config.MAX_BODY_LENGTH)

    @model_validator(mode="after")
    def _changes_something(self) -> Self:
        # A save that sets nothing would still bump the version and wake
        # every viewer, for no edit at all.
        if self.title is None and self.body is None:
            raise ValueError("a save must set `title`, `body` or both")
        return self


# Responses


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    email: str
    name: str
    created_at: datetime


class LoginOut(BaseModel):
    token: str
    expires_at: datetime
    user: UserOut


class TeamOut(BaseModel):
    id: str
    name: str
    created_at: datetime
    role: Role


class MemberOut(BaseModel):
    user_id: str
    email: str
    name: str
    role: Role
    joined_at: datetime


class LockOut(BaseModel):
    user_id: str
    user_name: str
    expires_at: datetime
    # How often the holder should renew. The server's number, so clients do
    # not each hard-code a guess against a lease they cannot see.
    renew_after_seconds: int = int(config.LOCK_RENEW_AFTER.total_seconds())

    @classmethod
    def from_note(cls, note: Note, now: datetime, holder: User | None = None) -> Self | None:
        """The note's live lock, or None if it is unlocked or expired.

        `holder` is the lock holder when the caller already has them (a save
        is only made by the holder); otherwise `note.lock_holder` must have
        been loaded with the note (see `store.sql.with_lock_holder`).
        """
        if note.live_lock_user_id(now) is None or note.lock_expires_at is None:
            return None
        holder = holder or note.lock_holder
        if holder is None:
            return None
        return cls(user_id=holder.id, user_name=holder.name, expires_at=note.lock_expires_at)


class NoteSummaryOut(BaseModel):
    """A note without its body, as returned by the list endpoint."""

    id: str
    team_id: str
    author_id: str
    title: str
    version: int
    lock: LockOut | None
    tag: str
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_note(cls, note: Note, now: datetime) -> Self:
        return cls(**_summary_fields(note, now))


class NotePageOut(BaseModel):
    """One page of the note list. `next_cursor` is null on the last page."""

    items: list[NoteSummaryOut]
    next_cursor: str | None


class NoteOut(NoteSummaryOut):
    body: str

    @classmethod
    def from_note(cls, note: Note, now: datetime, holder: User | None = None) -> Self:
        return cls(**_summary_fields(note, now, holder), body=note.body)


def _summary_fields(note: Note, now: datetime, holder: User | None = None) -> dict[str, Any]:
    """The fields NoteSummaryOut and NoteOut share."""
    return {
        "id": note.id,
        "team_id": note.team_id,
        "author_id": note.author_id,
        "title": note.title,
        "version": note.version,
        "lock": LockOut.from_note(note, now, holder),
        "tag": note.tag(now),
        "created_at": note.created_at,
        "updated_at": note.updated_at,
    }


class ChangeOut(BaseModel):
    """What a long poll reports: enough to decide whether to reload the note."""

    tag: str
    version: int
    lock: LockOut | None

    @classmethod
    def from_note(cls, note: Note, now: datetime) -> Self:
        return cls(tag=note.tag(now), version=note.version, lock=LockOut.from_note(note, now))
