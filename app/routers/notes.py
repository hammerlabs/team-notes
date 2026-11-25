"""Notes: create, list and search, read, save, delete.

Creating, saving and deleting are each one conditional statement in the
store, judged by whether it changed a row (see app/store/sql.py). This module
only turns the outcome into HTTP: the status, the response body, the ETag,
and waking the note's long polls once the change is committed.

Saving and deleting are checked in this order, so each refusal tells the
caller only what they are entitled to know:

1. authentication (401) and team access (404 to an outsider, never 403), both
   in `team_router`'s dependencies, before the handler runs;
2. the lock rule and, for a delete, ownership (409, 403), as before `If-Match`
   existed: a caller without the lock hears that, whatever version they sent;
3. the precondition: 428 with no `If-Match`, 412 with a malformed or stale one.

Steps 2 and 3 are one conditional statement in the store; its refusal reads
the note and names the first rule that failed.
"""

from typing import Annotated

from fastapi import Header, Query, Response

from app import clock
from app.cursor import encode_cursor
from app.deps import CurrentUser, Member, NotifierDep, StoreDep, team_router
from app.errors import error_responses, precondition_required
from app.preconditions import expected_version, set_etag
from app.schemas import NoteIn, NoteListQuery, NoteOut, NotePageOut, NotePatch, NoteSummaryOut
from app.store.interface import NotePosition, VersionConflict

router = team_router("notes")

# A list, so every `If-Match` line the request carries reaches `expected_version`;
# a `str` would hold only the first line and let a repeated header pass on it.
IfMatch = Annotated[
    list[str] | None,
    Header(
        description='The ETag of the note as last read, for example `"3"`. Required, once; `*` and lists are refused.'
    ),
]


@router.post("/notes", status_code=201, responses=error_responses(409, 413))
def create_note(team_id: str, payload: NoteIn, user: CurrentUser, store: StoreDep, response: Response) -> NoteOut:
    """Create a note in the team, authored by the caller. 409 `team_notes_full` at MAX_NOTES_PER_TEAM."""
    now = clock.now()
    note = store.create_note_if_room(team_id, user.id, payload.title, payload.body, now)
    set_etag(response, note.version)
    return NoteOut.from_note(note, now)


@router.get("/notes")
def list_notes(team_id: str, store: StoreDep, query: Annotated[NoteListQuery, Query()]) -> NotePageOut:
    """The team's notes, newest edit first (ties by id), optionally filtered by `q`, one page at a time.

    `q` is a case-insensitive substring match on title and body; `%` and `_`
    in it match literally. Case folding follows Unicode on SQLite and the
    database's lowercasing on Postgres (see the API contract).
    `next_cursor` is the `cursor` for the next page, or null if this is the
    last. A cursor is only a position: the team check above has already run.
    An unknown parameter (such as `offset`) or a cursor that does not decode
    to a valid position is 422 `validation_error`.
    """
    page = store.notes_page(team_id, query.q, query.limit, query.cursor)
    now = clock.now()
    next_cursor = None
    if page.has_more:
        last = page.notes[-1]
        next_cursor = encode_cursor(NotePosition(updated_at=last.updated_at, id=last.id))
    return NotePageOut(items=[NoteSummaryOut.from_note(note, now) for note in page.notes], next_cursor=next_cursor)


@router.get("/notes/{note_id}")
def get_note(team_id: str, note_id: str, store: StoreDep, response: Response) -> NoteOut:
    """One note, with its live lock (or null) and tag. Its version is also the `ETag` header."""
    note = store.note(team_id, note_id)
    set_etag(response, note.version)
    return NoteOut.from_note(note, clock.now())


@router.patch("/notes/{note_id}", responses=error_responses(409, 412, 413, 428))
def save_note(
    team_id: str,
    note_id: str,
    payload: NotePatch,
    user: CurrentUser,
    store: StoreDep,
    notifier: NotifierDep,
    response: Response,
    if_match: IfMatch = None,
) -> NoteOut:
    """Save an edit. The caller must hold a live lock and send `If-Match` with the current ETag.

    The lock prevents conflicts in normal use; the precondition catches the
    case where the lock's timer was wrong (the holder's lease lapsed, someone
    else saved, and the first holder re-took the lock with stale text). Saving
    keeps the lock. 409 `note_locked` or `lock_required`, then 428
    `precondition_required` or 412 `precondition_failed` (module docstring).
    """
    now = clock.now()
    try:
        note = store.save_note(team_id, note_id, user.id, expected_version(if_match), payload.title, payload.body, now)
    except VersionConflict:
        if if_match is None:
            raise precondition_required() from None
        raise
    notifier.wake(note_id)
    set_etag(response, note.version)
    # The save's WHERE says the caller holds the lock, so the holder is `user`.
    return NoteOut.from_note(note, now, holder=user)


@router.delete("/notes/{note_id}", status_code=204, responses=error_responses(403, 409, 412, 428))
def delete_note(
    team_id: str, note_id: str, membership: Member, store: StoreDep, notifier: NotifierDep, if_match: IfMatch = None
) -> Response:
    """Delete a note permanently, with `If-Match` holding its current ETag.

    Author or admin (else 403), and not while someone else is editing (409);
    then 428 or 412 as for a save.
    """
    try:
        store.delete_note(
            team_id, note_id, membership.user_id, membership.is_admin, expected_version(if_match), clock.now()
        )
    except VersionConflict:
        if if_match is None:
            raise precondition_required() from None
        raise
    notifier.wake(note_id)  # so open long polls answer 404 now rather than later
    return Response(status_code=204)
