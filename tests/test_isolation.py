"""Team isolation: every team-scoped endpoint, as every kind of caller.

Read this file first. The matrix runs each endpoint under `/teams/{team_id}` as an admin, a member, an outsider (an
admin of another team) and an anonymous caller. Outsiders must get 404, never 403, so they cannot tell whether a team or
note exists.

Two structural tests keep the matrix honest as routes are added: `test_every_team_route_is_guarded` fails if any served
route that names a team or a note does not depend on `require_member`, and `test_matrix_covers_every_team_route` fails
if one has no row in `CASES`. Both walk the app's route objects rather than the OpenAPI document, so hidden routes are
seen too. Removing the router-level guard from `team_router()` makes the first fail on 7 routes.

How to read a matrix case id: `test_access_matrix[<METHOD> <route>-<role>]`, for example `[DELETE
/teams/{team_id}/notes/{note_id}-member]`; the expected status is that role's field in the route's `Case`.
"""

import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.dependencies.models import Dependant
from fastapi.testclient import TestClient

from app.deps import CurrentUser, require_member
from helpers import Account, Api, assert_error, lock_url, note_url


@dataclass
class World:
    """Alice (admin) and Bob (member) in team A, with Dave as a removable member and a note by Alice.

    Carol is the admin of team B. Erin is in no team.
    """

    alice: Account
    bob: Account
    carol: Account
    dave: Account
    erin: Account
    team_a: str
    team_b: str
    note_id: str
    note_b_id: str


@pytest.fixture
def world(api: Api) -> World:
    """Each team has a note, both matching the search "secret", so a missing team filter shows up."""
    alice, bob, carol, dave, erin = (api.signup(n) for n in ("alice", "bob", "carol", "dave", "erin"))
    team_a = api.create_team(alice, "A")
    api.add_member(alice, team_a, bob)
    api.add_member(alice, team_a, dave)
    team_b = api.create_team(carol, "B")
    note = api.create_note(alice, team_a, "Team A secret", "only for team A")
    note_b = api.create_note(carol, team_b, "Team B secret", "only for team B")
    return World(alice, bob, carol, dave, erin, team_a, team_b, note["id"], note_b["id"])


@dataclass
class Case:
    """One endpoint, and the status each kind of caller should get."""

    method: str
    path: str  # route template, as FastAPI registers it
    admin: int
    member: int
    body: Callable[[World], dict[str, Any]] | None = None
    lock_first: bool = False  # take the note's lock as the caller first (saving needs it)
    if_match: bool = False  # send `If-Match` with the note's current ETag (saving and deleting need it)
    outsider: int = 404
    anonymous: int = 401
    params: dict[str, Any] = field(default_factory=dict)

    def url(self, world: World, team_id: str) -> str:
        """The concrete URL for this route under `team_id`, aimed at the world's note and at Dave."""
        return self.path.format(team_id=team_id, note_id=world.note_id, user_id=world.dave.id)


NOTE = "/teams/{team_id}/notes/{note_id}"
CASES = [
    Case("GET", "/teams/{team_id}", admin=200, member=200),
    Case("GET", "/teams/{team_id}/members", admin=200, member=200),
    Case("POST", "/teams/{team_id}/members", admin=201, member=403, body=lambda w: {"email": w.erin.email}),
    Case("DELETE", "/teams/{team_id}/members/{user_id}", admin=204, member=403),
    Case("POST", "/teams/{team_id}/notes", admin=201, member=201, body=lambda w: {"title": "New"}),
    Case("GET", "/teams/{team_id}/notes", admin=200, member=200),
    Case("GET", NOTE, admin=200, member=200),
    Case("PATCH", NOTE, admin=200, member=200, body=lambda w: {"body": "edit"}, lock_first=True, if_match=True),
    Case("DELETE", NOTE, admin=204, member=403, if_match=True),  # the note is Alice's; Bob is not its author
    Case("PUT", NOTE + "/lock", admin=200, member=200),
    Case("DELETE", NOTE + "/lock", admin=204, member=204),
    Case("GET", NOTE + "/changes", admin=200, member=200, params={"wait": 0}),
]
ROLES = ["admin", "member", "outsider", "anonymous"]


def caller(world: World, role: str) -> Account | None:
    """The account playing `role` in the matrix (`None` for anonymous)."""
    return {"admin": world.alice, "member": world.bob, "outsider": world.carol, "anonymous": None}[role]


def send(client: TestClient, world: World, case: Case, who: Account | None, team_id: str) -> Any:
    """Send `case` as `who` under `team_id`, taking the note's lock first if the case needs it."""
    headers = who.headers if who else {}
    if case.lock_first:
        client.put(lock_url(team_id, world.note_id), headers=headers)
    if case.if_match:
        headers = {**headers, "If-Match": '"1"'}
    json = case.body(world) if case.body else None
    return client.request(case.method, case.url(world, team_id), json=json, params=case.params, headers=headers)


@pytest.mark.parametrize("role", ROLES)
@pytest.mark.parametrize("case", CASES, ids=lambda c: f"{c.method} {c.path}")
def test_access_matrix(api: Api, world: World, case: Case, role: str) -> None:
    """Each endpoint answers each kind of caller with the status in its `Case` row.

    Refusals are in the contract's error shape (401 `unauthorized`, 403 `forbidden`, 404 `not_found`). 12 routes times 4
    roles; see the module docstring for how to read a case id.
    """
    response = send(api.client, world, case, caller(world, role), world.team_a)

    expected = getattr(case, role)
    assert response.status_code == expected, response.text
    if expected >= 400:
        assert_error(response, expected, {401: "unauthorized", 403: "forbidden", 404: "not_found"}[expected])


NOTE_CASES = [case for case in CASES if case.path.startswith(NOTE)]


@pytest.mark.parametrize("case", NOTE_CASES, ids=lambda c: f"{c.method} {c.path}")
def test_note_is_unreachable_through_another_teams_path(api: Api, world: World, case: Case) -> None:
    """Carol is an admin of team B. Team A's note id under team B's path is still 404.

    Guards against a lookup by note id alone, which would let any admin reach any note through their own team's path.
    Ids are the note routes.
    """
    response = send(api.client, world, case, world.carol, world.team_b)
    assert_error(response, 404, "not_found")


@pytest.mark.parametrize("params", [{}, {"q": "secret"}], ids=["list", "search"])
def test_list_and_search_return_only_the_teams_own_notes(api: Api, world: World, params: dict[str, str]) -> None:
    """List and search in each team return only that team's note, though both teams have one matching.

    The other team's note matches too, so a list without its team filter fails here. Ids: `list` and `search`.
    """
    for who, team_id, own in ((world.alice, world.team_a, world.note_id), (world.carol, world.team_b, world.note_b_id)):
        page = api.client.get(f"/teams/{team_id}/notes", params=params, headers=who.headers).json()
        assert [note["id"] for note in page["items"]] == [own]


@pytest.mark.parametrize("if_match", [None, '"7"', "*"], ids=["missing", "stale", "star"])
@pytest.mark.parametrize("method", ["PATCH", "DELETE"])
def test_outsider_gets_404_before_any_precondition(api: Api, world: World, method: str, if_match: str | None) -> None:
    """Team access is judged before `If-Match`: an outsider gets 404, never 428 or 412, for a real note.

    A 428 or a 412 carrying the note would tell them the note exists, or show it. Ids name the method and `If-Match`.
    """
    headers = world.carol.headers if if_match is None else {**world.carol.headers, "If-Match": if_match}
    response = api.client.request(method, note_url(world.team_a, world.note_id), json={"body": "x"}, headers=headers)
    assert_error(response, 404, "not_found")


def test_outsider_learns_nothing_from_a_real_vs_fake_team(api: Api, world: World) -> None:
    """An outsider gets the same 404 body for a real team as for a made-up id."""
    real = api.client.get(f"/teams/{world.team_a}/notes", headers=world.carol.headers)
    fake = api.client.get("/teams/00000000-0000-0000-0000-000000000000/notes", headers=world.carol.headers)
    assert real.status_code == fake.status_code == 404
    assert real.json() == fake.json()


def test_outsider_requests_change_nothing(api: Api, world: World) -> None:
    """Every endpoint called by an outsider leaves team A as it was: no save, no lock, no removal.

    A refused request with a side effect would still pass the matrix, which checks only statuses.
    """
    for case in CASES:
        send(api.client, world, case, world.carol, world.team_a)
    note = api.get_note(world.alice, world.team_a, world.note_id)
    assert (note["version"], note["lock"]) == (1, None)
    members = api.client.get(f"/teams/{world.team_a}/members", headers=world.alice.headers).json()
    assert {m["user_id"] for m in members} == {world.alice.id, world.bob.id, world.dave.id}


def test_removed_member_is_refused_on_the_next_request(api: Api, world: World) -> None:
    """Membership is checked on every request, so removal takes effect on the next one.

    Nothing about membership is cached in the token.
    """
    url = note_url(world.team_a, world.note_id)
    assert api.client.get(url, headers=world.bob.headers).status_code == 200

    api.client.delete(f"/teams/{world.team_a}/members/{world.bob.id}", headers=world.alice.headers)

    assert_error(api.client.get(url, headers=world.bob.headers), 404, "not_found")


# Any path that names a team in its first segment (whatever the parameter is
# called) or names a note anywhere.
TEAM_SCOPED = re.compile(r"^/teams/\{[^}]+\}|\{note_id\}")


def served_routes(app: FastAPI) -> Iterator[tuple[str, set[str], Dependant]]:
    """(path, methods, dependency tree) of every route the app serves, as it serves it.

    Walks the route objects, not the OpenAPI document, so a route with `include_in_schema=False` is seen too. FastAPI
    keeps included routers as lazy entries; their effective contexts carry the full path and the dependencies added by
    every enclosing router and include.
    """
    for route in app.routes:
        contexts = route.effective_route_contexts() if hasattr(route, "effective_route_contexts") else [route]
        for context in contexts:
            if getattr(context, "dependant", None) is not None:
                yield context.path, set(context.methods or ()), context.dependant


def _depends_on(dependant: Dependant, call: Callable[..., Any]) -> bool:
    """Whether `call` appears anywhere in the dependency tree of `dependant`."""
    return any(sub.call is call or _depends_on(sub, call) for sub in dependant.dependencies)


def _unguarded(app: FastAPI) -> list[str]:
    """Team- or note-scoped routes whose dependency tree lacks `require_member`, as sortable strings."""
    return sorted(
        f"{sorted(methods)} {path}"
        for path, methods, dependant in served_routes(app)
        if TEAM_SCOPED.search(path) and not _depends_on(dependant, require_member)
    )


def test_every_team_route_is_guarded(app: FastAPI) -> None:
    """Every served route that names a team or a note depends on `require_member`.

    Mutation-checked: removing the router-level dependency from `team_router()` flags 7 routes.
    """
    assert _unguarded(app) == []


def test_guard_check_catches_routes_that_skip_the_member_check(app: FastAPI) -> None:
    """Routes a check of the OpenAPI document alone would miss, plus a nested include, are all flagged.

    A check of the check: a route walk that silently found nothing would let the test above pass, so this adds four
    unguarded routes and requires all four to be flagged.
    """
    rogue = APIRouter(prefix="/teams")

    @rogue.get("/{tid}/export")  # parameter not named team_id
    def export(tid: str, user: CurrentUser) -> None: ...

    @rogue.get("/{team_id}/stats", include_in_schema=False)  # hidden from OpenAPI
    def stats(team_id: str, user: CurrentUser) -> None: ...

    elsewhere = APIRouter()

    @elsewhere.get("/notes/{note_id}")  # a note route outside /teams
    def by_id(note_id: str, user: CurrentUser) -> None: ...

    inner = APIRouter()

    @inner.get("/{team_id}/archive")  # gets its /teams prefix only from the outer include
    def archive(team_id: str, user: CurrentUser) -> None: ...

    outer = APIRouter()
    outer.include_router(inner, prefix="/teams")
    for router in (rogue, elsewhere, outer):
        app.include_router(router)

    assert _unguarded(app) == [
        "['GET'] /notes/{note_id}",
        "['GET'] /teams/{team_id}/archive",
        "['GET'] /teams/{team_id}/stats",
        "['GET'] /teams/{tid}/export",
    ]


def test_matrix_covers_every_team_route(app: FastAPI) -> None:
    """Every team- or note-scoped method and route has exactly one row in `CASES`, and no row is stale."""
    team_routes = {
        (method, path) for path, methods, _ in served_routes(app) if TEAM_SCOPED.search(path) for method in methods
    }
    assert team_routes == {(case.method, case.path) for case in CASES}
