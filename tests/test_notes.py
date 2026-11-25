"""Note lifecycle through the API: create, read, list, search, page, save, delete, schema guards.

Backs the contract's Notes section and its Limits, including the paging guarantee: a cursor walk never repeats a note
and never skips one that stayed unchanged, while other notes are created, edited and deleted between pages. Ordering
tests advance the fake clock between writes so that `updated_at` differs, except the one that pins equal times. The last section writes to the database directly, bypassing the store, to show the schema itself
refuses states no handler should write.

Not here: the edit lock and the version check (`test_locks.py`), concurrent saves and the note cap under simultaneous
requests (`test_races.py`), team scoping of list and search (`test_isolation.py`), search's case folding, including
the one difference between the databases (`test_store.py`).
"""

import base64
import json
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from sqlalchemy import event, update
from sqlalchemy.exc import IntegrityError, StatementError

from app import clock, config
from app.models import Note
from helpers import Api, FakeClock, TeamOfTwo, assert_error, note_url, with_etag


def list_notes(api: Api, who: Any, team_id: str, **params: Any) -> list[dict[str, Any]]:
    """List a team's notes as `who` with `params`, assert 200, and return the `items`."""
    response = api.client.get(f"/teams/{team_id}/notes", params=params, headers=who.headers)
    assert response.status_code == 200, response.text
    return response.json()["items"]


def test_create_and_read(api: Api, team: TeamOfTwo) -> None:
    """A new note starts at version 1, unlocked, tag `1:`, authored by the caller in this team.

    Another member reads back the identical object.
    """
    alice, bob, team_id = team

    note = api.create_note(alice, team_id, "Plan", "First draft")

    assert note["version"] == 1
    assert note["lock"] is None
    assert note["tag"] == "1:"
    assert note["author_id"] == alice.id
    assert note["team_id"] == team_id
    assert api.get_note(bob, team_id, note["id"]) == note


def test_body_defaults_to_empty(api: Api, team: TeamOfTwo) -> None:
    """A note created without `body` is created (201) with the body `""`."""
    alice, _, team_id = team
    response = api.client.post(f"/teams/{team_id}/notes", json={"title": "Empty"}, headers=alice.headers)
    assert response.status_code == 201, response.text
    assert response.json()["body"] == ""


@pytest.mark.parametrize(
    "payload",
    [
        {"title": "", "body": ""},
        {"title": "x" * (config.MAX_TITLE_LENGTH + 1)},
        {"title": "ok", "body": "x" * (config.MAX_BODY_LENGTH + 1)},
        {"body": "no title"},
        {"title": "   "},
        {"title": "line\nbreak"},
        {"title": "nul\x00"},
    ],
    ids=["empty-title", "long-title", "long-body", "missing-title", "blank-title", "newline-title", "nul-title"],
)
def test_note_limits(api: Api, team: TeamOfTwo, payload: dict[str, str]) -> None:
    """Every note-creation input the contract refuses is 422 `validation_error`.

    Case ids name the broken rule: empty, overlong (201 characters), missing, blank (only spaces), multi-line or
    NUL-containing title, and a body over 100,000 characters. Titles are stripped before the length check, so a blank
    title counts as empty.
    """
    alice, _, team_id = team
    response = api.client.post(f"/teams/{team_id}/notes", json=payload, headers=alice.headers)
    assert_error(response, 422, "validation_error")


def test_title_and_body_at_their_limits_are_accepted(api: Api, team: TeamOfTwo) -> None:
    """A 200-character title and a 100,000-character body are accepted: both limits are inclusive.

    Paired with the `long-title` and `long-body` cases of `test_note_limits`, an off-by-one in either check fails one of
    the two.
    """
    alice, _, team_id = team
    api.create_note(alice, team_id, "x" * config.MAX_TITLE_LENGTH, "x" * config.MAX_BODY_LENGTH)


def test_title_is_stripped_on_create_and_save(api: Api, team: TeamOfTwo) -> None:
    """Surrounding whitespace is removed from titles on create and on save.

    Saving a blank title is 422. Both paths are checked because they take different request bodies.
    """
    alice, _, team_id = team
    note = api.create_note(alice, team_id, "  Plan  ")
    assert note["title"] == "Plan"
    api.take_lock(alice, team_id, note["id"])

    saved = api.save_note(alice, team_id, note["id"], 1, title="\tRoadmap ")

    assert saved["title"] == "Roadmap"
    blank = api.client.patch(note_url(team_id, note["id"]), json={"title": " "}, headers=with_etag(alice, 2))
    assert_error(blank, 422, "validation_error")


def test_team_note_cap_is_enforced(api: Api, team: TeamOfTwo, monkeypatch: pytest.MonkeyPatch) -> None:
    """At the per-team note cap the next create is 409 `team_notes_full` and nothing is added.

    Another team is unaffected. The cap is lowered to 2 with `monkeypatch`; the store reads it at call time. On Postgres
    SQLAlchemy reports rowcount -1 for `INSERT ... SELECT` on psycopg, so both counting inserts judge by `RETURNING`;
    a store that trusted the rowcount would read -1 as "inserted" and fail here by accepting a third note. The race for
    the last slot is in `test_races.py`.
    """
    alice, bob, team_id = team
    monkeypatch.setattr(config, "MAX_NOTES_PER_TEAM", 2)
    api.create_note(alice, team_id, "One")
    api.create_note(bob, team_id, "Two")

    response = api.client.post(f"/teams/{team_id}/notes", json={"title": "Three"}, headers=alice.headers)

    assert_error(response, 409, "team_notes_full")
    assert len(list_notes(api, alice, team_id)) == 2
    # The cap is per team.
    other_team = api.create_team(alice, "Other")
    api.create_note(alice, other_team, "Elsewhere")


@pytest.mark.parametrize("fields", [{}, {"title": None, "body": None}], ids=["nothing", "nulls"])
def test_save_that_changes_nothing_is_refused(api: Api, team: TeamOfTwo, fields: dict[str, Any]) -> None:
    """A save with neither `title` nor `body` is 422 and the version stays 1.

    Accepting it would bump the version and wake every viewer for nothing. Ids: `nothing` (keys absent), `nulls` (both
    `null`).
    """
    alice, _, team_id = team
    note = api.create_note(alice, team_id)
    api.take_lock(alice, team_id, note["id"])

    response = api.client.patch(note_url(team_id, note["id"]), json=fields, headers=with_etag(alice, 1))

    assert_error(response, 422, "validation_error")
    assert api.get_note(alice, team_id, note["id"])["version"] == 1


def test_save_bumps_version_and_keeps_the_lock(api: Api, team: TeamOfTwo, fake_clock: FakeClock) -> None:
    """A save increments `version`, moves `updated_at` forward and keeps fields it did not send.

    The lock stays with the saver (the contract: saving does not release the lock).
    """
    alice, _, team_id = team
    note = api.create_note(alice, team_id, "Plan", "v1")
    api.take_lock(alice, team_id, note["id"])
    fake_clock.advance(1)

    saved = api.save_note(alice, team_id, note["id"], 1, body="v2")

    assert saved["version"] == 2
    assert saved["title"] == "Plan"
    assert saved["body"] == "v2"
    assert saved["updated_at"] > note["updated_at"]
    assert saved["lock"]["user_id"] == alice.id
    assert saved["tag"] == f"2:{alice.id}"


def test_save_returns_the_row_it_wrote_not_a_later_one(
    app: FastAPI, api: Api, team: TeamOfTwo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Another save lands right after this one commits; the response is still this save's.

    The response is built from `UPDATE ... RETURNING`, not read back after the commit. The second write is injected in
    the wake-up, which runs after the commit, so a response read back there would return another user's later edit as
    this save's result and fail the test.
    """
    alice, _, team_id = team
    note = api.create_note(alice, team_id, "Plan", "v1")
    api.take_lock(alice, team_id, note["id"])
    real_wake = app.state.notifier.wake

    def wake_after_a_later_save(note_id: str) -> None:
        with app.state.store.engine.begin() as connection:
            connection.execute(update(Note).where(Note.id == note_id).values(version=3, body="someone later"))
        real_wake(note_id)

    monkeypatch.setattr(app.state.notifier, "wake", wake_after_a_later_save)

    saved = api.save_note(alice, team_id, note["id"], 1, body="v2")

    assert (saved["version"], saved["body"]) == (2, "v2")
    assert saved["tag"] == f"2:{alice.id}"


@pytest.mark.parametrize(
    "if_match",
    [f'"{2**63}"', "*", 'W/"1"', '"01"', "1", '"1", "2"', '"' + "9" * 5000 + '"', ""],
    ids=["beyond-64-bits", "star", "weak", "leading-zero", "unquoted", "list", "huge", "empty"],
)
def test_save_with_a_malformed_if_match_is_412_and_changes_nothing(api: Api, team: TeamOfTwo, if_match: str) -> None:
    """A lock holder's save whose `If-Match` names no version is 412 `precondition_failed` carrying the note.

    `*` is refused because it would match any version, the lost update `If-Match` is there to stop. A version beyond the
    database's 64-bit range cannot be bound to a query; a 5,000-digit one would be slow to convert. Both are refused
    before the database sees them, not as a 500. Ids name the value.
    """
    alice, _, team_id = team
    note = api.create_note(alice, team_id, "Plan", "v1")
    api.take_lock(alice, team_id, note["id"])

    response = api.client.patch(note_url(team_id, note["id"]), json={"body": "x"}, headers=with_etag(alice, if_match))

    assert assert_error(response, 412, "precondition_failed")["note"]["version"] == 1
    assert api.get_note(alice, team_id, note["id"])["body"] == "v1"


def test_save_with_if_match_sent_twice_is_412_and_changes_nothing(api: Api, team: TeamOfTwo) -> None:
    """Two `If-Match` header lines are a list, malformed exactly as `"2", "999"` on one line is: 412, note unchanged.

    The first line names the current version, so judging by it alone would save. Fails when only the first header
    line is read: checked.
    """
    alice, _, team_id = team
    note = api.create_note(alice, team_id, "Plan", "v1")
    api.take_lock(alice, team_id, note["id"])
    api.save_note(alice, team_id, note["id"], 1, body="v2")
    headers = [*alice.headers.items(), ("If-Match", '"2"'), ("If-Match", '"999"')]

    response = api.client.patch(note_url(team_id, note["id"]), json={"body": "x"}, headers=headers)

    assert assert_error(response, 412, "precondition_failed")["note"]["version"] == 2
    assert api.get_note(alice, team_id, note["id"])["body"] == "v2"


def test_save_without_if_match_is_428_and_changes_nothing(api: Api, team: TeamOfTwo) -> None:
    """A lock holder's save with no `If-Match` is 428 `precondition_required`, and the note is unchanged."""
    alice, _, team_id = team
    note = api.create_note(alice, team_id, "Plan", "v1")
    api.take_lock(alice, team_id, note["id"])

    response = api.client.patch(note_url(team_id, note["id"]), json={"body": "x"}, headers=alice.headers)

    assert_error(response, 428, "precondition_required")
    assert api.get_note(alice, team_id, note["id"])["body"] == "v1"


def test_list_is_newest_edit_first_and_has_no_body(api: Api, team: TeamOfTwo, fake_clock: FakeClock) -> None:
    """The list is ordered by last edit, newest first, and leaves out `body`.

    A save moves an older note to the front. Each summary shows the note's lock.
    """
    alice, bob, team_id = team
    first = api.create_note(alice, team_id, "First")
    fake_clock.advance(1)
    second = api.create_note(alice, team_id, "Second")
    fake_clock.advance(1)
    api.take_lock(alice, team_id, first["id"])
    api.save_note(alice, team_id, first["id"], 1, body="edited")

    listed = list_notes(api, bob, team_id)

    assert [n["id"] for n in listed] == [first["id"], second["id"]]
    assert "body" not in listed[0]
    assert listed[0]["lock"]["user_id"] == alice.id


def test_search_is_case_insensitive_on_title_and_body(api: Api, team: TeamOfTwo) -> None:
    """`q` matches a substring of the title or the body, ignoring ASCII case, and nothing else."""
    alice, _, team_id = team
    in_title = api.create_note(alice, team_id, "Quarterly ROADMAP")
    in_body = api.create_note(alice, team_id, "Notes", "the roadmap is below")
    api.create_note(alice, team_id, "Lunch", "sandwiches")

    listed = list_notes(api, alice, team_id, q="RoadMap")

    assert {n["id"] for n in listed} == {in_title["id"], in_body["id"]}


@pytest.mark.parametrize(("q", "matching", "other"), [("100%", "100% done", "100 done"), ("a_b", "a_b", "axb")])
def test_search_treats_wildcards_literally(api: Api, team: TeamOfTwo, q: str, matching: str, other: str) -> None:
    """`%` and `_` in `q` match only themselves.

    Unescaped, they are SQL `LIKE` wildcards: `100%` would also find `100 done`, and `a_b` would find `axb`. Case ids
    are `<q>-<title that must match>-<title that must not>`.
    """
    alice, _, team_id = team
    literal = api.create_note(alice, team_id, matching)
    api.create_note(alice, team_id, other)

    listed = list_notes(api, alice, team_id, q=q)

    assert [n["id"] for n in listed] == [literal["id"]]


@pytest.mark.parametrize("q", ["\x00", "road\x00map", "a\nb"], ids=["nul", "embedded-nul", "newline"])
def test_search_rejects_control_characters(api: Api, team: TeamOfTwo, q: str) -> None:
    """A `q` containing NUL or a newline is 422, the contract's rule for `q`. Ids name the character."""
    alice, _, team_id = team
    api.create_note(alice, team_id, "Roadmap")

    response = api.client.get(f"/teams/{team_id}/notes", params={"q": q}, headers=alice.headers)

    assert_error(response, 422, "validation_error")


def walk(
    api: Api, who: Any, team_id: str, between: Callable[[list[str]], None] = lambda _: None, **params: Any
) -> list[str]:
    """Follow `next_cursor` from the first page to the last as `who`; return the ids seen, in order.

    `between` runs after every page but the last, with the ids seen so far, to change the team's notes mid-walk.
    """
    seen: list[str] = []
    cursor = None
    while True:
        query = {**params, "cursor": cursor} if cursor else params
        response = api.client.get(f"/teams/{team_id}/notes", params=query, headers=who.headers)
        assert response.status_code == 200, response.text
        page = response.json()
        seen += [n["id"] for n in page["items"]]
        cursor = page["next_cursor"]
        if cursor is None:
            return seen
        between(list(seen))


@pytest.mark.parametrize("count", [5, 6], ids=["partial-last-page", "full-last-page"])
def test_next_cursor_pages_through_and_ends_with_null(
    api: Api, team: TeamOfTwo, fake_clock: FakeClock, count: int
) -> None:
    """Following `next_cursor` visits every note once, in list order, and ends with `null` after three pages.

    Ids: `partial-last-page` (5 notes, pages of 2) and `full-last-page` (6 notes). The full case shows the server knows
    the last page is last without counting: it fetches one row more than it returns.
    """
    alice, _, team_id = team
    ids = []
    for i in range(count):
        ids.append(api.create_note(alice, team_id, f"Note {i}")["id"])
        fake_clock.advance(1)
    url = f"/teams/{team_id}/notes"

    first = api.client.get(url, params={"limit": 2}, headers=alice.headers).json()
    second = api.client.get(url, params={"limit": 2, "cursor": first["next_cursor"]}, headers=alice.headers).json()
    third = api.client.get(url, params={"limit": 2, "cursor": second["next_cursor"]}, headers=alice.headers).json()

    assert [n["id"] for page in (first, second, third) for n in page["items"]] == ids[::-1]
    assert isinstance(first["next_cursor"], str) and isinstance(second["next_cursor"], str)
    # Also when the last page is exactly full: the server fetches one row
    # more than the page, so it knows nothing follows without a count.
    assert third["next_cursor"] is None


def test_walk_under_concurrent_changes_never_repeats_or_skips_a_stable_note(
    api: Api, team: TeamOfTwo, fake_clock: FakeClock
) -> None:
    """Notes created, edited and deleted between page requests never make a walk repeat a note or skip a stable one.

    The contract's paging guarantee. Twelve notes, pages of 3. Between pages Bob creates a note (it lands in front of
    the walk), edits a note the walk has not reached (it jumps to the front), and deletes one the walk has already passed
    and one it has not. Each of these shifts every later row by one, so offset paging both repeats a note (a row pushed
    back onto the next page) and skips one (a row pulled forward past the boundary). With a cursor the next page starts
    after the last row shown, wherever the rows around it went. Notes touched during the walk may or may not appear
    (the contract says so); every other note must appear exactly once. Fails with offset paging: checked.
    """
    alice, bob, team_id = team
    notes = []
    for i in range(12):
        notes.append(api.create_note(alice, team_id, f"Note {i}"))
        fake_clock.advance(1)
    newest_first = [n["id"] for n in reversed(notes)]
    to_edit = iter(newest_first[:-4:-1])  # the oldest three: still ahead of the walk when each is edited
    to_delete_ahead = iter(newest_first[-4:-7:-1])  # the next three: deleted before the walk reaches them
    touched: set[str] = set()

    def change_the_team(seen_so_far: list[str]) -> None:
        edited, doomed = next(to_edit), next(to_delete_ahead)
        passed = next(n for n in seen_so_far if n not in touched)  # one the walk has shown already
        fake_clock.advance(1)
        touched.add(api.create_note(bob, team_id, "Created during the walk")["id"])
        api.take_lock(bob, team_id, edited)
        api.save_note(bob, team_id, edited, 1, body="edited during the walk")
        assert api.client.delete(f"{note_url(team_id, edited)}/lock", headers=bob.headers).status_code == 204
        for gone in (doomed, passed):
            assert api.delete_note(alice, team_id, gone, 1).status_code == 204
        touched.update({edited, doomed, passed})

    seen = walk(api, alice, team_id, change_the_team, limit=3)

    assert len(seen) == len(set(seen)), "a note was listed twice in one walk"
    stable = [n for n in newest_first if n not in touched]
    assert [n for n in seen if n in stable] == stable, "a note that never changed was skipped or misordered"
    assert len(touched) == 8  # the walk crossed two page boundaries, with all four changes at each


def test_equal_edit_times_across_a_page_boundary(
    api: Api, team: TeamOfTwo, fake_clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Notes saved in the same microsecond are each listed once, ordered by id, whichever page boundary splits them.

    Four notes share one `updated_at` and a fifth is one microsecond newer: the cursor carries the time at full stored
    precision (a cursor rounded to the millisecond or second would skip or repeat here) and the id breaks the tie.
    Walked with every page size from 1 to 5, so a boundary falls between every pair. Fails with the cursor's time cut
    to whole seconds: checked.
    """
    alice, _, team_id = team
    instant = fake_clock.now().replace(microsecond=123456)
    monkeypatch.setattr(clock, "now", lambda: instant)
    tied = [api.create_note(alice, team_id, f"Tied {i}")["id"] for i in range(4)]
    monkeypatch.setattr(clock, "now", lambda: instant + timedelta(microseconds=1))
    newer = api.create_note(alice, team_id, "One microsecond newer")["id"]
    expected = [newer, *sorted(tied, reverse=True)]

    for limit in range(1, 6):
        assert walk(api, alice, team_id, limit=limit) == expected, f"limit={limit}"


def test_search_pages_with_the_same_cursor(api: Api, team: TeamOfTwo, fake_clock: FakeClock) -> None:
    """A search pages like the plain list: matching notes only, each once, newest first, ending with `null`."""
    alice, _, team_id = team
    matching = []
    for i in range(5):
        matching.append(api.create_note(alice, team_id, f"Roadmap {i}")["id"])
        api.create_note(alice, team_id, f"Lunch {i}")
        fake_clock.advance(1)

    assert walk(api, alice, team_id, q="roadmap", limit=2) == matching[::-1]


def test_a_cursor_grants_nothing(api: Api, team: TeamOfTwo, fake_clock: FakeClock) -> None:
    """A cursor is only a position: an outsider holding one gets 404, and in another team it pages that team's notes.

    The team check runs on every page before the cursor is used, and the query is always filtered by the path's team.
    """
    alice, _, team_id = team
    carol = api.signup("carol")
    carols_team = api.create_team(carol, "Carol's")
    carols_older = api.create_note(carol, carols_team, "Older than the cursor")["id"]
    fake_clock.advance(1)
    for i in range(3):
        api.create_note(alice, team_id, f"A{i}")
        fake_clock.advance(1)
    api.create_note(carol, carols_team, "Newer than the cursor")
    cursor = api.client.get(f"/teams/{team_id}/notes", params={"limit": 1}, headers=alice.headers).json()["next_cursor"]

    outsider = api.client.get(f"/teams/{team_id}/notes", params={"cursor": cursor}, headers=carol.headers)
    assert_error(outsider, 404, "not_found")
    # In her own team the cursor is just a point in time: her older note follows it, and Alice's two older notes,
    # which follow it in Alice's team, do not appear.
    assert [n["id"] for n in list_notes(api, carol, carols_team, cursor=cursor)] == [carols_older]


def _cursor_of(payload: object) -> str:
    """A cursor in the server's encoding around `payload`, to forge well-encoded but wrong ones."""
    return base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()


_NOTE_ID = "3f2c8a8e-1b7c-4c39-9a52-6f1f8e2b7d10"
_STAMP = "2026-03-09T14:51:44.907257Z"


@pytest.mark.parametrize(
    "cursor",
    [
        "",
        "not base64!",
        _cursor_of([_STAMP, _NOTE_ID])[:-6],
        _cursor_of([_STAMP, _NOTE_ID]) + "A",
        _cursor_of({"u": _STAMP, "id": _NOTE_ID}),
        _cursor_of([_STAMP, _NOTE_ID, "extra"]),
        _cursor_of([_STAMP, 7]),
        _cursor_of(["yesterday", _NOTE_ID]),
        _cursor_of(["2026-03-09T14:51:44+02:00", _NOTE_ID]),
        _cursor_of([_STAMP, "not-a-uuid"]),
        _cursor_of([_STAMP, _NOTE_ID.upper()]),
        _cursor_of([_STAMP, "\x00"]),
        base64.urlsafe_b64encode(b"\xff\xfe").decode(),
        "A" * (config.MAX_CURSOR_LENGTH + 1),
    ],
    ids=[
        "empty",
        "not-base64",
        "truncated",
        "trailing-garbage",
        "object-not-list",
        "three-items",
        "id-not-string",
        "time-unparseable",
        "time-other-format",
        "id-not-uuid",
        "id-not-canonical",
        "id-nul",
        "not-utf8",
        "too-long",
    ],
)
def test_a_bad_cursor_is_a_422_never_a_500(api: Api, team: TeamOfTwo, cursor: str) -> None:
    """Every cursor the server could not have issued is 422 `validation_error`, detail type `invalid_cursor`.

    Malformed, truncated and tampered values each fail one decoding layer (base64, UTF-8, JSON, shape, time format,
    UUID); ids name the layer. None reaches the database: a NUL in the id, for one, would be a driver error on Postgres.
    """
    alice, _, team_id = team
    api.create_note(alice, team_id)
    response = api.client.get(f"/teams/{team_id}/notes", params={"cursor": cursor}, headers=alice.headers)
    error = assert_error(response, 422, "validation_error")
    assert [(d["loc"], d["type"]) for d in error["details"]] == [(["query", "cursor"], "invalid_cursor")]


def test_a_well_formed_cursor_for_no_note_is_just_a_position(api: Api, team: TeamOfTwo, fake_clock: FakeClock) -> None:
    """A valid cursor naming a note that never existed pages from that point: it is a position, not a reference."""
    alice, _, team_id = team
    old = api.create_note(alice, team_id, "Old")["id"]
    fake_clock.advance(3600)
    api.create_note(alice, team_id, "New")
    between = (fake_clock.now() - timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")

    assert [n["id"] for n in list_notes(api, alice, team_id, cursor=_cursor_of([between, _NOTE_ID]))] == [old]


def test_search_text_up_to_the_limit_is_accepted(api: Api, team: TeamOfTwo) -> None:
    """A `q` of exactly 200 characters is accepted."""
    alice, _, team_id = team
    assert list_notes(api, alice, team_id, q="x" * config.MAX_QUERY_LENGTH) == []


@pytest.mark.parametrize(
    "params",
    [{"limit": 0}, {"limit": config.MAX_PAGE_SIZE + 1}, {"q": "x" * (config.MAX_QUERY_LENGTH + 1)}],
    ids=["limit-0", "limit-over", "q-too-long"],
)
def test_paging_and_search_limits(api: Api, team: TeamOfTwo, params: dict[str, Any]) -> None:
    """Out-of-range `limit` and an overlong `q` are 422; ids name the case.

    These bound every list query; an unbounded `q` (50,001 characters, say) would reach the database and can be a 500.
    """
    alice, _, team_id = team
    response = api.client.get(f"/teams/{team_id}/notes", params=params, headers=alice.headers)
    assert_error(response, 422, "validation_error")


@pytest.mark.parametrize("params", [{"offset": 0}, {"offset": 2}, {"page": 1}], ids=["offset-0", "offset-2", "page"])
def test_unknown_list_parameters_are_refused(api: Api, team: TeamOfTwo, params: dict[str, Any]) -> None:
    """`offset`, or any other parameter the list does not take, is 422 `validation_error` naming it.

    As for unknown body keys. Ignoring `offset` would hand a client paging by offset the first page on every
    request, and it would never reach the end.
    """
    alice, _, team_id = team
    response = api.client.get(f"/teams/{team_id}/notes", params=params, headers=alice.headers)
    error = assert_error(response, 422, "validation_error")
    assert [(d["loc"], d["type"]) for d in error["details"]] == [(["query", next(iter(params))], "extra_forbidden")]


def test_author_deletes_note(api: Api, team: TeamOfTwo) -> None:
    """The author deletes their note; afterwards reading or deleting it again is 404."""
    _, bob, team_id = team
    note = api.create_note(bob, team_id)
    url = note_url(team_id, note["id"])

    assert api.delete_note(bob, team_id, note["id"], 1).status_code == 204
    assert_error(api.client.get(url, headers=bob.headers), 404, "not_found")
    assert_error(api.delete_note(bob, team_id, note["id"], 1), 404, "not_found")
    assert_error(api.delete_note(bob, team_id, note["id"], None), 404, "not_found")


def test_admin_deletes_anyones_note_but_member_cannot(api: Api, team: TeamOfTwo) -> None:
    """An admin may delete any member's note; a member deleting someone else's note is 403."""
    alice, bob, team_id = team
    alices = api.create_note(alice, team_id)
    bobs = api.create_note(bob, team_id)

    assert_error(api.delete_note(bob, team_id, alices["id"], 1), 403, "forbidden")
    assert api.delete_note(alice, team_id, bobs["id"], 1).status_code == 204


def test_notes_outlive_their_authors_membership(api: Api, team: TeamOfTwo) -> None:
    """A removed member's notes stay in the team, still naming them as author; an admin may delete them, a member not.

    Notes belong to the team, not to the author's membership, so removing someone loses none of the team's work. Once
    removed, the author cannot reach the note at all (404, as for any non-member).
    """
    alice, bob, team_id = team
    carol = api.signup("carol")
    api.add_member(alice, team_id, carol)
    note = api.create_note(bob, team_id, "Bob's plan")
    assert api.client.delete(f"/teams/{team_id}/members/{bob.id}", headers=alice.headers).status_code == 204

    assert api.get_note(carol, team_id, note["id"])["author_id"] == bob.id
    assert [n["id"] for n in list_notes(api, carol, team_id)] == [note["id"]]
    assert_error(api.client.get(note_url(team_id, note["id"]), headers=bob.headers), 404, "not_found")
    assert_error(api.delete_note(carol, team_id, note["id"], 1), 403, "forbidden")
    assert api.delete_note(alice, team_id, note["id"], 1).status_code == 204


def test_delete_needs_the_current_etag(api: Api, team: TeamOfTwo) -> None:
    """Delete is fenced by `If-Match`: the author cannot delete a note that changed since they last read it.

    Bob last saw version 1; Alice saved version 2. Bob's delete with `"1"` is 412 `precondition_failed` carrying
    Alice's note, without `If-Match` it is 428, and the note survives both. With `"2"` it goes. Fails with the
    version condition removed from the DELETE: checked.
    """
    alice, bob, team_id = team
    note = api.create_note(bob, team_id, "Plan", "v1")  # Bob last saw version 1
    api.take_lock(alice, team_id, note["id"])
    api.save_note(alice, team_id, note["id"], 1, body="v2 by Alice")
    assert api.client.delete(f"{note_url(team_id, note['id'])}/lock", headers=alice.headers).status_code == 204

    stale = assert_error(api.delete_note(bob, team_id, note["id"], 1), 412, "precondition_failed")
    assert stale["note"]["body"] == "v2 by Alice"
    assert_error(api.delete_note(bob, team_id, note["id"], None), 428, "precondition_required")
    assert api.get_note(bob, team_id, note["id"])["version"] == 2

    assert api.delete_note(bob, team_id, note["id"], 2).status_code == 204


def test_delete_with_if_match_sent_twice_is_412_and_the_note_stays(api: Api, team: TeamOfTwo) -> None:
    """Two `If-Match` header lines on a delete are malformed, as on a save: 412 carrying the note, which survives.

    Fails when only the first header line is read: checked.
    """
    alice, _, team_id = team
    note = api.create_note(alice, team_id, "Plan", "v1")
    headers = [*alice.headers.items(), ("If-Match", '"1"'), ("If-Match", '"999"')]

    response = api.client.delete(note_url(team_id, note["id"]), headers=headers)

    assert assert_error(response, 412, "precondition_failed")["note"]["version"] == 1
    assert api.get_note(alice, team_id, note["id"])["version"] == 1


def test_unknown_note_is_404(api: Api, team: TeamOfTwo) -> None:
    """An unknown note id in a real team is 404 `not_found`."""
    alice, _, team_id = team
    assert_error(api.client.get(note_url(team_id, "no-such-note"), headers=alice.headers), 404, "not_found")


# Storage guards: the schema refuses states no handler should write.


@pytest.mark.parametrize(
    "state", ["expiry-without-holder", "holder-without-expiry", "lock-without-activity", "version-0"]
)
def test_schema_refuses_impossible_note_states(app: FastAPI, api: Api, team: TeamOfTwo, state: str) -> None:
    """The database refuses a half-set lock (any of its three columns without the others) and version 0.

    Two named CHECK constraints, so a bug in any write path fails loudly instead of storing a state the lock and version
    logic never expects. Written straight to the table to test the schema, not a handler. The constraint name is matched
    because both databases include it in the error. Ids name the state.
    """
    alice, _, team_id = team
    note = api.create_note(alice, team_id)  # unlocked, version 1
    values: dict[str, Any] = {
        "expiry-without-holder": {"lock_expires_at": clock.now()},
        "holder-without-expiry": {"lock_user_id": alice.id},
        "lock-without-activity": {"lock_user_id": alice.id, "lock_expires_at": clock.now()},
        "version-0": {"version": 0},
    }[state]
    # Both databases name the violated constraint in the error.
    constraint = "ck_notes_version_positive" if state == "version-0" else "ck_notes_lock_columns_together"

    with pytest.raises(IntegrityError, match=constraint), app.state.store.engine.begin() as connection:
        connection.execute(update(Note).where(Note.id == note["id"]).values(values))


def test_naive_datetimes_are_refused_not_guessed(app: FastAPI, api: Api, team: TeamOfTwo) -> None:
    """Writing a timezone-naive datetime raises instead of being stored as if it were UTC.

    Every time in the app is aware UTC, and lease checks compare stored times, so a naive one is a bug to catch rather
    than a value to guess about.
    """
    alice, _, team_id = team
    note = api.create_note(alice, team_id)
    naive = datetime(2026, 1, 1, 12, 0)

    with pytest.raises(StatementError, match="timezone-aware"), app.state.store.engine.begin() as connection:
        connection.execute(update(Note).where(Note.id == note["id"]).values(updated_at=naive))


def test_note_reads_load_only_the_lock_holders_id_and_name(app: FastAPI, api: Api, team: TeamOfTwo) -> None:
    """List and get each load the lock holder in the same query, and select only the holder's id and name.

    Fails if any read selects the holder's `password_hash` or email. Inspects the emitted SQL, so it is coupled to
    SQLAlchemy's statement text, but not to its alias names: `notes` has no email or password column, so either word
    anywhere in a statement means the holder's were selected.
    """
    alice, bob, team_id = team
    note = api.create_note(alice, team_id)
    api.take_lock(alice, team_id, note["id"])
    statements: list[str] = []

    @event.listens_for(app.state.store.engine, "before_cursor_execute")
    def record(_conn: Any, _cursor: Any, statement: str, *_: Any) -> None:
        if "FROM notes" in statement:
            statements.append(statement)

    page = list_notes(api, bob, team_id)
    seen = api.get_note(bob, team_id, note["id"])

    assert page[0]["lock"]["user_name"] == seen["lock"]["user_name"] == "alice"
    assert len(statements) == 2  # one query each: the holder is joined, not fetched per note
    for statement in statements:
        assert "password_hash" not in statement
        assert "email" not in statement
