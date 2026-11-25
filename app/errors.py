"""The one way handlers report an error.

Every error response has the contract's shape:
`{"error": {"code": ..., "message": ..., <extra keys>}}`. Handlers raise
`ApiError`, and the store raises its refusals (`store_error` gives each its
status, code and wording); the handlers registered in `main.py` turn them,
and FastAPI's own errors, into that shape.
"""

from typing import Any

from pydantic import BaseModel, ConfigDict

from app import config
from app.schemas import LockOut, NoteOut
from app.store import interface as store


class ApiError(Exception):
    """An error with an HTTP status, a stable machine code and a message.

    `extra` holds the code-specific keys from the contract, such as `lock`
    for `note_locked` or `note` for `precondition_failed`. Values must already
    be JSON-ready.
    """

    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        headers: dict[str, str] | None = None,
        **extra: Any,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.headers = headers
        self.extra = extra

    def body(self) -> dict[str, Any]:
        """The JSON body for this error."""
        return {"error": {"code": self.code, "message": self.message, **self.extra}}


class ErrorBody(BaseModel):
    """`code` and `message` always; code-specific keys (`lock`, `note`, `details`) may follow."""

    model_config = ConfigDict(extra="allow")

    code: str
    message: str


class ErrorOut(BaseModel):
    """The shape of every error response."""

    error: ErrorBody


class ValidationDetail(BaseModel):
    loc: list[str | int]
    message: str
    type: str


class ValidationErrorBody(ErrorBody):
    code: str = "validation_error"
    details: list[ValidationDetail]


class ValidationErrorOut(BaseModel):
    """422 `validation_error`: what FastAPI would describe as HTTPValidationError, in our shape."""

    error: ValidationErrorBody


_RESPONSE_DESCRIPTIONS = {
    401: "`unauthorized`: missing, invalid or expired bearer token (`invalid_credentials` on login).",
    403: "`forbidden`: a team member without the role or ownership this needs.",
    404: "`not_found`: no such resource, or the caller is not a member of the team"
    " (`user_not_found` when adding a member by an unknown email).",
    409: "A conflict with the current state; `error.code` says which.",
    412: "`precondition_failed`: `If-Match` is malformed or names an older version; `error.note` is the current note.",
    413: "`payload_too_large`: the request body is over 1 MB.",
    422: "`validation_error`: the body or a parameter breaks the contract; see `error.details`.",
    428: "`precondition_required`: the request has no `If-Match` header.",
    429: "`too_many_polls`: the caller already holds the maximum number of long polls.",
    503: "`busy`: the database is busy; retry after `Retry-After` seconds.",
}


def error_responses(*statuses: int) -> dict[int | str, dict[str, Any]]:
    """OpenAPI `responses` entries for these error statuses, all in the error shape."""
    return {
        status: {
            "model": ValidationErrorOut if status == 422 else ErrorOut,
            "description": _RESPONSE_DESCRIPTIONS[status],
        }
        for status in statuses
    }


def not_found(what: str) -> ApiError:
    """404 `not_found`. Also what non-members get, so IDs cannot be probed."""
    return ApiError(404, "not_found", f"{what} not found.")


def forbidden(message: str) -> ApiError:
    """403 `forbidden`: a team member who lacks the role or ownership."""
    return ApiError(403, "forbidden", message)


def too_many_polls(limit: int) -> ApiError:
    """429 `too_many_polls`: the caller already has `limit` long polls held open."""
    return ApiError(429, "too_many_polls", f"At most {limit} long polls per user can wait at once.")


def busy() -> ApiError:
    """503 `busy`: the database could not take the request in time. Nothing was written."""
    return ApiError(503, "busy", "The database is busy. Try again shortly.", headers={"Retry-After": "1"})


def note_locked(lock: LockOut | None) -> ApiError:
    """409 `note_locked`, naming who holds the lock and until when.

    `lock` is None only when the lock changed hands twice while the request
    was being answered (see `store.sql._write_or_refuse`): the client should retry.
    """
    message = f"{lock.user_name} is editing this note." if lock else "The note's lock changed meanwhile. Try again."
    return ApiError(409, "note_locked", message, lock=lock.model_dump(mode="json") if lock else None)


def lock_required() -> ApiError:
    """409 `lock_required`: the caller holds no live lock, and nobody else does either."""
    return ApiError(409, "lock_required", "Take the note's lock first.")


def precondition_required() -> ApiError:
    """428 `precondition_required`: a save or delete sent no `If-Match`."""
    return ApiError(428, "precondition_required", "Send If-Match with the ETag of the note you read.")


def store_error(error: store.StoreError) -> ApiError:
    """The HTTP answer to something the store refused or could not do."""
    match error:
        case store.Busy():
            return busy()
        case store.NotFound(what=what):
            return not_found(what)
        case store.EmailTaken():
            return ApiError(409, "email_taken", "An account with this email already exists.")
        case store.UserNotFound():
            # Its own code: a client can tell "no such account" from "you are
            # not in this team" (`not_found`), which the same status otherwise hides.
            return ApiError(404, "user_not_found", "No account has that email.")
        case store.AlreadyMember():
            return ApiError(409, "already_member", "That user is already a member of this team.")
        case store.TeamFull():
            return ApiError(409, "team_full", f"A team has at most {config.MAX_TEAM_MEMBERS} members.")
        case store.TeamNotesFull():
            return ApiError(409, "team_notes_full", f"A team has at most {config.MAX_NOTES_PER_TEAM} notes.")
        case store.LastAdmin():
            return ApiError(409, "last_admin", "The team's last admin cannot be removed.")
        case store.NotAuthor():
            return forbidden("Only the note's author or a team admin can delete it.")
        case store.LockHeldByAnother():
            return forbidden("Someone else holds this lock. Only they or a team admin can release it.")
        case store.LockRequired():
            return lock_required()
        case store.LockIdle():
            minutes = int(config.LOCK_IDLE_LIMIT.total_seconds() // 60)
            return ApiError(
                409, "lock_idle", f"The lock was released after {minutes} minutes without activity. Take it again."
            )
        case store.NoteLocked(note=note, now=now):
            return note_locked(LockOut.from_note(note, now) if note else None)
        case store.VersionConflict(note=note, now=now) if note is not None:
            return ApiError(
                412,
                "precondition_failed",
                "If-Match does not match the note's current ETag. Reload it and reapply your change.",
                note=NoteOut.from_note(note, now).model_dump(mode="json"),
            )
    raise error  # a StoreError with no HTTP meaning is a bug: let the 500 handler log it
