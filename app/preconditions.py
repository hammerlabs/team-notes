"""HTTP preconditions on a note: its version as an ETag, checked with `If-Match`.

A note's ETag is its version as a quoted string (`"3"`). Saving and deleting a
note require `If-Match` with the ETag the client last read. This module only
reads and writes the header; the comparison itself is part of the store's
conditional statement, so nothing can change between the check and the write.
"""

import re

from fastapi import Response

from app import config

# One strong entity tag holding a version: a quoted positive integer with no
# leading zero, exactly as `etag` writes it. At most 19 digits (2^63 - 1 has
# 19), so a huge header is refused here, not converted to an int.
_VERSION_ETAG = re.compile(r'"([1-9][0-9]{0,18})"')


def etag(version: int) -> str:
    """The ETag of a note at `version`."""
    return f'"{version}"'


def set_etag(response: Response, version: int) -> None:
    """Put the note's ETag on a response that returns a single note."""
    response.headers["ETag"] = etag(version)


def expected_version(if_match: list[str] | None) -> int | None:
    """The version the request's `If-Match` lines name, or None if there are none or they name none.

    Only a single strong ETag in the form `etag` writes, on a single header
    line, is accepted. Anything else names no version the note can be at, so it
    fails the precondition:

    - `*` matches any current note, so it would let a client overwrite or
      delete text it never read, the lost update the precondition exists to
      stop. It is refused as malformed.
    - A weak ETag (`W/"3"`) never matches under If-Match's strong comparison.
    - A list (`"3", "4"`) is legal HTTP but no client of this API needs it.
      Repeated header lines are the same list split across lines (RFC 9110,
      5.3), so they are refused alike rather than judged by one of them.
    - A version beyond the database's 64-bit range cannot be bound to a query.
    """
    if if_match is None or len(if_match) != 1:
        return None
    match = _VERSION_ETAG.fullmatch(if_match[0].strip(" \t"))
    if match is None:
        return None
    version = int(match.group(1))
    return version if version <= config.MAX_SQL_INTEGER else None
