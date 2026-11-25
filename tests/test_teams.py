"""Teams and membership: adding and removing members, roles, the member cap and the last-admin rule.

Backs the contract's Teams section. Every test here sends one request at a time. The same count rules under simultaneous
requests are in `test_races.py`; what non-members can reach is in `test_isolation.py`; that removing a member releases
their locks is in `test_locks.py` and `test_changes.py`.
"""

from fastapi import FastAPI
from sqlalchemy.orm import Session

from app import clock, config
from app.models import Membership, User
from helpers import Api, FakeClock, TeamOfTwo, assert_error


def test_creator_becomes_admin_and_sees_only_own_teams(api: Api) -> None:
    """Creating a team makes the caller its admin, and `GET /teams` lists only the caller's own teams."""
    alice, bob = api.signup("alice"), api.signup("bob")
    alpha = api.create_team(alice, "Alpha")
    api.create_team(bob, "Bravo")

    response = api.client.get("/teams", headers=alice.headers)

    assert response.status_code == 200
    teams = response.json()
    assert [(t["id"], t["name"], t["role"]) for t in teams] == [(alpha, "Alpha", "admin")]


def test_teams_are_listed_oldest_team_first_not_by_joining(api: Api, fake_clock: FakeClock) -> None:
    """`GET /teams` lists the caller's teams by when each team was created, oldest first, whatever order they joined in.

    The contract says "oldest first"; this pins that it means the team's age, not the caller's membership's: Bob
    creates "Newer" before he is added to the older "Older", and still sees "Older" first.
    """
    alice, bob = api.signup("alice"), api.signup("bob")
    older = api.create_team(alice, "Older")
    fake_clock.advance(1)
    newer = api.create_team(bob, "Newer")
    fake_clock.advance(1)
    api.add_member(alice, older, bob)

    teams = api.client.get("/teams", headers=bob.headers).json()

    assert [(t["id"], t["role"]) for t in teams] == [(older, "member"), (newer, "admin")]


def test_get_one_team_with_the_callers_role(api: Api, team: TeamOfTwo) -> None:
    """`GET /teams/{team_id}` returns the same Team as the list, with `role` set to the caller's role."""
    alice, bob, team_id = team
    as_admin = api.client.get(f"/teams/{team_id}", headers=alice.headers).json()
    as_member = api.client.get(f"/teams/{team_id}", headers=bob.headers).json()

    assert as_admin == {**as_member, "role": "admin"}
    assert as_member["role"] == "member"
    assert as_member in api.client.get("/teams", headers=bob.headers).json()


def test_team_name_is_validated(api: Api) -> None:
    """Empty and 101-character team names are 422."""
    alice = api.signup("alice")
    for name in ("", "x" * 101):
        response = api.client.post("/teams", json={"name": name}, headers=alice.headers)
        assert_error(response, 422, "validation_error")


def test_admin_adds_member_by_email_with_default_role(api: Api) -> None:
    """An admin adds a member by email in any case; the role defaults to `member`.

    Also pins the Member key set, joining order in the member list, and that the new member now sees the team with role
    `member`.
    """
    alice, bob = api.signup("alice"), api.signup("bob")
    team = api.create_team(alice)

    response = api.client.post(f"/teams/{team}/members", json={"email": "BOB@example.com"}, headers=alice.headers)

    assert response.status_code == 201
    member = response.json()
    assert member["user_id"] == bob.id
    assert member["role"] == "member"
    assert set(member) == {"user_id", "email", "name", "role", "joined_at"}
    listed = api.client.get(f"/teams/{team}/members", headers=bob.headers).json()
    assert [(m["user_id"], m["role"]) for m in listed] == [(alice.id, "admin"), (bob.id, "member")]
    assert [t["role"] for t in api.client.get("/teams", headers=bob.headers).json()] == ["member"]


def test_add_member_errors(api: Api) -> None:
    """Each refusal of add-member, one request at a time.

    Unknown email is 404 `user_not_found` (its own code, so a client can tell it from a missing team), an existing
    member is 409 `already_member`, a non-admin is 403, and an unknown role is 422.
    """
    alice, bob, carol = api.signup("alice"), api.signup("bob"), api.signup("carol")
    team = api.create_team(alice)
    api.add_member(alice, team, bob)
    url = f"/teams/{team}/members"

    assert_error(
        api.client.post(url, json={"email": "nobody@example.com"}, headers=alice.headers), 404, "user_not_found"
    )
    assert_error(api.client.post(url, json={"email": bob.email}, headers=alice.headers), 409, "already_member")
    assert_error(api.client.post(url, json={"email": carol.email}, headers=bob.headers), 403, "forbidden")
    assert_error(
        api.client.post(url, json={"email": carol.email, "role": "owner"}, headers=alice.headers),
        422,
        "validation_error",
    )


def test_team_full_at_member_limit(app: FastAPI, api: Api) -> None:
    """At 200 members the next add is 409 `team_full` and nothing is added.

    The 199 filler members are written straight to the database: going through the API would add 199 sign-ups and test
    nothing more.
    """
    alice, last = api.signup("alice"), api.signup("last")
    team = api.create_team(alice)
    # Fill the team straight in the database: 199 signups through the API
    # would only slow the test down without testing anything more.
    with Session(app.state.store.engine) as db:
        now = clock.now()
        for i in range(config.MAX_TEAM_MEMBERS - 1):
            user = User(email=f"filler{i}@example.com", name="Filler", password_hash="-", created_at=now)
            db.add(user)
            db.flush()
            db.add(Membership(team_id=team, user_id=user.id, role="member", created_at=now))
        db.commit()

    response = api.client.post(f"/teams/{team}/members", json={"email": last.email}, headers=alice.headers)

    assert_error(response, 409, "team_full")
    assert len(api.client.get(f"/teams/{team}/members", headers=alice.headers).json()) == config.MAX_TEAM_MEMBERS


def test_admin_removes_member_and_member_loses_access(api: Api, team: TeamOfTwo) -> None:
    """A removed member gets 404 on the team and no longer sees it in `GET /teams`."""
    alice, bob, team_id = team

    response = api.client.delete(f"/teams/{team_id}/members/{bob.id}", headers=alice.headers)

    assert response.status_code == 204
    assert_error(api.client.get(f"/teams/{team_id}/members", headers=bob.headers), 404, "not_found")
    assert api.client.get("/teams", headers=bob.headers).json() == []


def test_member_can_leave_but_not_remove_others(api: Api) -> None:
    """A plain member may remove themself (leave), but removing someone else is 403."""
    alice, bob, carol = api.signup("alice"), api.signup("bob"), api.signup("carol")
    team = api.create_team(alice)
    api.add_member(alice, team, bob)
    api.add_member(alice, team, carol)

    assert_error(api.client.delete(f"/teams/{team}/members/{carol.id}", headers=bob.headers), 403, "forbidden")
    assert api.client.delete(f"/teams/{team}/members/{bob.id}", headers=bob.headers).status_code == 204


def test_removing_a_non_member_is_404(api: Api) -> None:
    """Removing a user who is not in the team is 404."""
    alice, bob = api.signup("alice"), api.signup("bob")
    team = api.create_team(alice)
    assert_error(api.client.delete(f"/teams/{team}/members/{bob.id}", headers=alice.headers), 404, "not_found")


def test_last_admin_cannot_leave_but_one_of_two_can(api: Api) -> None:
    """A team always keeps an admin.

    The only admin cannot leave (409 `last_admin`); with two, one may leave and the other is then the last.
    """
    alice, bob = api.signup("alice"), api.signup("bob")
    team = api.create_team(alice)
    url = f"/teams/{team}/members"

    assert_error(api.client.delete(f"{url}/{alice.id}", headers=alice.headers), 409, "last_admin")

    api.add_member(alice, team, bob, role="admin")
    assert api.client.delete(f"{url}/{alice.id}", headers=alice.headers).status_code == 204
    assert_error(api.client.delete(f"{url}/{bob.id}", headers=bob.headers), 409, "last_admin")


def test_last_admin_rule_ignores_plain_members(api: Api, team: TeamOfTwo) -> None:
    """The last-admin rule counts admins, not members.

    A guard that counted all memberships would let the only admin leave a team that still has plain members, leaving
    nobody able to manage it.
    """
    alice, _, team_id = team
    # Two memberships exist but only one is an admin, so alice still cannot leave.
    assert_error(api.client.delete(f"/teams/{team_id}/members/{alice.id}", headers=alice.headers), 409, "last_admin")


def test_an_admin_can_remove_another_admin_even_the_creator(api: Api) -> None:
    """Admins are equals: one admin removes another, including the team's creator, who then loses access (404).

    There is no owner role; creating a team only makes the creator its first admin. The last-admin rule is the only
    limit, and it holds here because the remover is still an admin.
    """
    alice, bob = api.signup("alice"), api.signup("bob")
    team = api.create_team(alice)
    api.add_member(alice, team, bob, role="admin")

    assert api.client.delete(f"/teams/{team}/members/{alice.id}", headers=bob.headers).status_code == 204

    members = api.client.get(f"/teams/{team}/members", headers=bob.headers).json()
    assert [(m["user_id"], m["role"]) for m in members] == [(bob.id, "admin")]
    assert_error(api.client.get(f"/teams/{team}", headers=alice.headers), 404, "not_found")
