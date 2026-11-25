"""The wire format in `docs/API_CONTRACT.md`, key by key.

Exact key sets, so a field added to a response (or leaked into it, like a password hash) fails here until the contract
says so. Every timestamp is ISO 8601 UTC ending in `Z`. Unknown request keys are refused. Also the contract's retry rule
for a save whose response was lost, and the NUL rule that keeps both databases giving one answer.

Not here: which status each caller gets on each route (`test_isolation.py`), and the OpenAPI document's error responses
(`test_http.py`).
"""

import re
from typing import Any

import pytest

from helpers import Api, TeamOfTwo, assert_error, changes_url, lock_url, note_url, with_etag

USER = {"id", "email", "name", "created_at"}
TEAM = {"id", "name", "created_at", "role"}
MEMBER = {"user_id", "email", "name", "role", "joined_at"}
LOCK = {"user_id", "user_name", "expires_at", "renew_after_seconds"}
NOTE = {"id", "team_id", "author_id", "title", "body", "version", "lock", "tag", "created_at", "updated_at"}
NOTE_SUMMARY = NOTE - {"body"}
NOTE_PAGE = {"items", "next_cursor"}
CHANGE = {"tag", "version", "lock"}
LOGIN = {"token", "expires_at", "user"}
ERROR = {"code", "message"}

TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?Z$")


def assert_shape(value: dict[str, Any], keys: set[str]) -> None:
    """Exactly these keys, and every `*_at` value a UTC timestamp ending in Z."""
    assert set(value) == keys
    for key in keys:
        if key.endswith("_at"):
            assert TIMESTAMP.match(value[key]), f"{key}={value[key]!r}"


def test_auth_objects(api: Api) -> None:
    """Sign-up, login and `/me` return exactly the contract's User and login objects."""
    signup = api.client.post("/auth/signup", json={"email": "a@example.com", "password": "correct horse", "name": "A"})
    assert_shape(signup.json(), USER)

    login = api.client.post("/auth/login", json={"email": "a@example.com", "password": "correct horse"}).json()
    assert_shape(login, LOGIN)
    assert_shape(login["user"], USER)
    assert_shape(api.client.get("/me", headers={"Authorization": f"Bearer {login['token']}"}).json(), USER)


def test_team_objects(api: Api, team: TeamOfTwo) -> None:
    """Create, get and list return exactly the Team object; the member list exactly Member objects."""
    alice, _, team_id = team
    created = api.client.post("/teams", json={"name": "New"}, headers=alice.headers).json()
    assert_shape(created, TEAM)
    assert_shape(api.client.get(f"/teams/{team_id}", headers=alice.headers).json(), TEAM)
    for listed in api.client.get("/teams", headers=alice.headers).json():
        assert_shape(listed, TEAM)
    for member in api.client.get(f"/teams/{team_id}/members", headers=alice.headers).json():
        assert_shape(member, MEMBER)


def test_note_lock_and_change_objects(api: Api, team: TeamOfTwo) -> None:
    """Note, Lock, NotePage, NoteSummary and Change have exactly their keys.

    Checked on every route that returns them: create, take lock, get, save, list, changes.
    """
    alice, bob, team_id = team
    note = api.create_note(alice, team_id, "Plan", "v1")
    assert_shape(note, NOTE)
    assert note["lock"] is None

    lock = api.take_lock(alice, team_id, note["id"])
    assert_shape(lock, LOCK)
    assert isinstance(lock["renew_after_seconds"], int)

    for seen in (api.get_note(bob, team_id, note["id"]), api.save_note(alice, team_id, note["id"], 1, body="v2")):
        assert_shape(seen, NOTE)
        assert_shape(seen["lock"], LOCK)

    page = api.client.get(f"/teams/{team_id}/notes", headers=bob.headers).json()
    assert_shape(page, NOTE_PAGE)
    assert page["next_cursor"] is None
    assert_shape(page["items"][0], NOTE_SUMMARY)
    assert_shape(page["items"][0]["lock"], LOCK)

    change = api.client.get(changes_url(team_id, note["id"]), headers=bob.headers).json()
    assert_shape(change, CHANGE)
    assert_shape(change["lock"], LOCK)


def test_every_response_with_one_note_carries_its_version_as_etag(api: Api, team: TeamOfTwo) -> None:
    """Create, read and save return `ETag: "<version>"`, equal to the body's `version`, which stays in the body.

    The lock response is a Lock, not a note, so it has none.
    """
    alice, _, team_id = team
    url = f"/teams/{team_id}/notes"
    created = api.client.post(url, json={"title": "Plan"}, headers=alice.headers)
    note_id = created.json()["id"]
    lock = api.client.put(lock_url(team_id, note_id), headers=alice.headers)
    read = api.client.get(note_url(team_id, note_id), headers=alice.headers)
    saved = api.client.patch(note_url(team_id, note_id), json={"body": "v2"}, headers=with_etag(alice, 1))

    assert [(r.headers["ETag"], r.json()["version"]) for r in (created, read, saved)] == [
        ('"1"', 1),
        ('"1"', 1),
        ('"2"', 2),
    ]
    assert "ETag" not in lock.headers


def test_error_objects_carry_their_documented_extras(api: Api, team: TeamOfTwo) -> None:
    """Each error code carries exactly its documented extras.

    `note_locked` carries `lock`, `precondition_failed` carries `note`, `validation_error` carries `details` of `{loc,
    message, type}`, any other code (here `precondition_required`) only `code` and `message`, and the body holds only
    `error`.
    """
    alice, bob, team_id = team
    note = api.create_note(alice, team_id)
    api.take_lock(alice, team_id, note["id"])

    locked = assert_error(api.client.put(lock_url(team_id, note["id"]), headers=bob.headers), 409, "note_locked")
    assert set(locked) == ERROR | {"lock"}
    assert_shape(locked["lock"], LOCK)

    conflict = api.client.patch(note_url(team_id, note["id"]), json={"body": "x"}, headers=with_etag(alice, 7))
    error = assert_error(conflict, 412, "precondition_failed")
    assert set(error) == ERROR | {"note"}
    assert_shape(error["note"], NOTE)

    missing = api.client.patch(note_url(team_id, note["id"]), json={"body": "x"}, headers=alice.headers)
    assert set(assert_error(missing, 428, "precondition_required")) == ERROR

    invalid = assert_error(api.client.post("/teams", json={}, headers=alice.headers), 422, "validation_error")
    assert set(invalid) == ERROR | {"details"}
    assert set(invalid["details"][0]) == {"loc", "message", "type"}

    plain = assert_error(api.client.get(note_url(team_id, "missing"), headers=alice.headers), 404, "not_found")
    assert set(plain) == ERROR
    assert set(api.client.get(note_url(team_id, "missing"), headers=alice.headers).json()) == {"error"}


def test_a_retried_save_is_recognisable_as_already_applied(api: Api, team: TeamOfTwo) -> None:
    """The contract's retry rule, for a save whose response was lost.

    The retry gets `precondition_failed`, but `error.note` is at the sent version + 1 with exactly the sent title and
    body: the first attempt landed, and the client can treat the save as done.
    """
    alice, _, team_id = team
    note = api.create_note(alice, team_id, "Plan", "v1")
    api.take_lock(alice, team_id, note["id"])
    edit = {"title": "Plan", "body": "v2"}
    api.save_note(alice, team_id, note["id"], 1, **edit)  # the response the client never saw

    retry = api.client.patch(note_url(team_id, note["id"]), json=edit, headers=with_etag(alice, 1))

    server = assert_error(retry, 412, "precondition_failed")["note"]
    assert server["version"] == 1 + 1
    assert (server["title"], server["body"]) == (edit["title"], edit["body"])


# Unknown request keys (mass assignment, typos)

# (method, path, body, the key that is not in the contract)
BODIES: list[tuple[str, str, dict[str, Any], str]] = [
    ("POST", "/auth/signup", {"email": "x@example.com", "password": "correct horse", "name": "X", "id": "me"}, "id"),
    ("POST", "/auth/login", {"email": "alice@example.com", "password": "correct horse", "remember": 1}, "remember"),
    ("POST", "/teams", {"name": "T", "id": "chosen"}, "id"),
    ("POST", "/teams/{team_id}/members", {"email": "carol@example.com", "user_id": "x"}, "user_id"),
    ("POST", "/teams/{team_id}/notes", {"title": "T", "author_id": "someone-else"}, "author_id"),
    ("POST", "/teams/{team_id}/notes", {"title": "T", "team_id": "another-team"}, "team_id"),
    ("PATCH", "/teams/{team_id}/notes/{note_id}", {"titel": "typo"}, "titel"),
    ("PATCH", "/teams/{team_id}/notes/{note_id}", {"body": "x", "author_id": "x"}, "author_id"),
    ("PATCH", "/teams/{team_id}/notes/{note_id}", {"body": "x", "lock_user_id": "x"}, "lock_user_id"),
    # The version travels in If-Match; in the body it is an unknown key like any other.
    ("PATCH", "/teams/{team_id}/notes/{note_id}", {"body": "x", "version": 1}, "version"),
]


@pytest.mark.parametrize(("method", "path", "body", "extra"), BODIES, ids=[f"{m} {p} {e}" for m, p, _, e in BODIES])
def test_unknown_request_keys_are_refused(
    api: Api, team: TeamOfTwo, method: str, path: str, body: dict[str, Any], extra: str
) -> None:
    """A request body with a key the contract does not list is 422, naming that key, and changes nothing.

    A misspelt key must not pass as an empty save that bumps the version, and keys the server sets itself
    (`author_id`, `team_id`, `lock_user_id`) must not be silently ignored, which would be an opening for mass
    assignment. Fails with `extra="forbid"` removed from the request models. Case ids are `<METHOD> <path> <extra key>`.
    """
    alice, _, team_id = team
    api.signup("carol")
    note = api.create_note(alice, team_id, "Plan", "v1")
    api.take_lock(alice, team_id, note["id"])
    url = path.format(team_id=team_id, note_id=note["id"])

    # A valid If-Match on every request, so a PATCH body is refused for its key and nothing else.
    headers = with_etag(alice, 1)
    error = assert_error(api.client.request(method, url, json=body, headers=headers), 422, "validation_error")

    assert [d["loc"] for d in error["details"] if d["type"] == "extra_forbidden"] == [["body", extra]]
    after = api.get_note(alice, team_id, note["id"])
    assert (after["version"], after["author_id"], after["title"]) == (1, alice.id, "Plan")


@pytest.mark.parametrize("field", ["signup name", "team name", "new note body", "saved body"])
def test_nul_is_refused_in_stored_text_on_every_database(api: Api, team: TeamOfTwo, field: str) -> None:
    """Postgres cannot store U+0000 in text; SQLite could. Both refuse it, so the contract has one answer.

    Without the rule, Postgres answers 500. Case ids name the field.
    """
    alice, _, team_id = team
    note = api.create_note(alice, team_id)
    api.take_lock(alice, team_id, note["id"])
    text = "before\x00after"
    method, url, body, headers = {
        "signup name": (
            "POST",
            "/auth/signup",
            {"email": "n@example.com", "password": "correct horse", "name": text},
            {},
        ),
        "team name": ("POST", "/teams", {"name": text}, alice.headers),
        "new note body": ("POST", f"/teams/{team_id}/notes", {"title": "T", "body": text}, alice.headers),
        "saved body": ("PATCH", note_url(team_id, note["id"]), {"body": text}, with_etag(alice, 1)),
    }[field]

    assert_error(api.client.request(method, url, json=body, headers=headers), 422, "validation_error")
