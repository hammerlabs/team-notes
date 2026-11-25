"""The note list's cursor: an opaque string that names a position in the list, nothing more.

A cursor is the URL-safe base64 (unpadded) of a two-item JSON array: the last
listed note's `updated_at`, always with six fractional digits so it keeps the
stored microseconds, and its id. For example
`["2026-03-09T14:51:44.907257Z","3f2c...-..."]`.

It is not signed, because it grants nothing. It carries no team, no search
text and no permission: every page runs the same team access check and the
same `team_id = ?` filter, so a cursor edited to another valid position only
starts the page somewhere else in the caller's own team. What decoding does
guard is the database: a cursor that does not decode to a valid position is
refused as a 422 `validation_error` (detail type `invalid_cursor`), so no
malformed value reaches a query and none can turn into a 500. A cursor that
does decode to a valid position is accepted and pages from that position,
whether or not the server wrote it byte for byte.
"""

import base64
import json
import uuid
from datetime import UTC, datetime
from typing import Annotated

from pydantic import PlainValidator, WithJsonSchema
from pydantic_core import PydanticCustomError

from app import config
from app.store.interface import NotePosition

_TIME_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"


def encode_cursor(position: NotePosition) -> str:
    """The cursor for the page that starts after `position`."""
    stamp = position.updated_at.astimezone(UTC).strftime(_TIME_FORMAT)
    payload = json.dumps([stamp, position.id], separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(payload).rstrip(b"=").decode("ascii")


def decode_cursor(value: object) -> NotePosition:
    """The position a cursor names. Raises a pydantic error of type `invalid_cursor` for anything else.

    Each layer must decode: the base64 alphabet (no characters silently
    skipped), UTF-8, JSON, a list of exactly two strings, a timestamp that
    `strptime` reads with `encode_cursor`'s format, and a canonical UUID.
    Variants that still decode to the same kind of position are accepted, not
    refused: fewer fractional digits, a lower-case `z`, padding `=`, or
    non-ASCII digits in the timestamp. They grant nothing; the page simply
    starts at the position they name. The UUID check keeps characters
    Postgres cannot bind, such as NUL, out of the query.
    """
    if not isinstance(value, str) or len(value) > config.MAX_CURSOR_LENGTH:
        raise _invalid()
    try:
        raw = base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
        decoded = json.loads(raw.decode("utf-8"))
        if not (isinstance(decoded, list) and len(decoded) == 2 and all(isinstance(item, str) for item in decoded)):
            raise ValueError("a cursor holds a list of two strings")
        stamp, note_id = decoded
        updated_at = datetime.strptime(stamp, _TIME_FORMAT).replace(tzinfo=UTC)
        if str(uuid.UUID(note_id)) != note_id:
            raise ValueError("a cursor's note id is a canonical UUID")
    except ValueError as error:
        # binascii.Error, UnicodeDecodeError and JSONDecodeError are all ValueErrors.
        raise _invalid() from error
    return NotePosition(updated_at=updated_at, id=note_id)


def _invalid() -> PydanticCustomError:
    return PydanticCustomError(
        "invalid_cursor", "Not a cursor this server issued. Start again from the first page, without `cursor`."
    )


Cursor = Annotated[
    NotePosition,
    PlainValidator(decode_cursor),
    WithJsonSchema({"type": "string", "maxLength": config.MAX_CURSOR_LENGTH}),
]
"""A `cursor` query parameter: arrives as the opaque string, reaches the handler as a `NotePosition`."""
