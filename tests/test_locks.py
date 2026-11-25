"""The edit lock as a 60-second lease with a 10-minute idle limit, and the version check that backs it up.

Backs the contract's Lock section, the save rules in Notes, and the lock events of the Client protocol. Time moves only
through the fake clock (`fake_clock.advance`), never by sleeping, so a test can stand exactly one second before or after
a lease boundary, or renew every 20 seconds for half an hour in a few milliseconds.

`test_stale_save_after_lease_lapse_is_a_version_conflict` is the reason the version check exists at all; read it
first. The two `..._judged_before_the_precondition` tests pin the check order: the lock rule before `If-Match`.
`test_idle_holders_renewal_past_the_limit_is_refused_and_frees_the_note` is the idle rule's crux.

Not here: the same rules under simultaneous requests (one of many takes a free lock, delete against take, break against
save) in `test_races.py`, and what viewers are told when a lock changes in `test_changes.py`.
"""

from typing import Any

import pytest

from app import config
from helpers import Account, Api, FakeClock, TeamOfTwo, assert_error, lock_url, note_url, with_etag

LEASE = config.LOCK_LEASE.total_seconds()
IDLE = config.LOCK_IDLE_LIMIT.total_seconds()
RENEW_EVERY = config.LOCK_RENEW_AFTER.total_seconds()

Scene = tuple[Account, Account, Account, str, dict[str, Any]]


@pytest.fixture
def scene(api: Api, team: TeamOfTwo) -> Scene:
    """Alice (admin), Bob and Carol (members) in one team, and a note by Alice."""
    alice, bob, team_id = team
    carol = api.signup("carol")
    api.add_member(alice, team_id, carol)
    note = api.create_note(alice, team_id, "Plan", "v1")
    return alice, bob, carol, team_id, note


def test_taking_a_lock_shows_on_the_note(api: Api, scene: Scene) -> None:
    """A taken lock names its holder and appears identically on the note for other members.

    The tag becomes `1:<holder id>`.
    """
    alice, bob, _, team_id, note = scene

    lock = api.take_lock(bob, team_id, note["id"])

    assert lock["user_id"] == bob.id
    assert lock["user_name"] == "bob"
    seen = api.get_note(alice, team_id, note["id"])
    assert seen["lock"] == lock
    assert seen["tag"] == f"1:{bob.id}"


def test_second_user_is_refused_with_who_holds_it(api: Api, scene: Scene) -> None:
    """A second taker gets 409 `note_locked` carrying the holder's Lock, so a client can say who is editing."""
    alice, bob, _, team_id, note = scene
    held = api.take_lock(alice, team_id, note["id"])

    error = assert_error(api.client.put(lock_url(team_id, note["id"]), headers=bob.headers), 409, "note_locked")

    assert error["lock"] == held


def test_holder_renews_and_others_stay_out(api: Api, scene: Scene, fake_clock: FakeClock) -> None:
    """Re-taking one's own lock extends it past the original expiry.

    Another user is still refused after that expiry, and the tag has not changed: a renewal is not news to viewers.
    """
    alice, bob, _, team_id, note = scene
    first = api.take_lock(alice, team_id, note["id"])
    fake_clock.advance(LEASE - 10)

    renewed = api.take_lock(alice, team_id, note["id"])
    fake_clock.advance(20)  # past the first lease, inside the renewed one

    assert renewed["expires_at"] > first["expires_at"]
    assert_error(api.client.put(lock_url(team_id, note["id"]), headers=bob.headers), 409, "note_locked")
    assert api.get_note(alice, team_id, note["id"])["tag"] == f"1:{alice.id}"


def test_expired_lock_reads_as_unlocked_and_can_be_taken_over(api: Api, scene: Scene, fake_clock: FakeClock) -> None:
    """Once the lease runs out the note reads as unlocked, with nothing written.

    `lock` is null and the tag `1:`. Another member can take it, and the former holder is then refused.
    """
    alice, bob, _, team_id, note = scene
    api.take_lock(alice, team_id, note["id"])
    fake_clock.advance(LEASE + 1)

    seen = api.get_note(bob, team_id, note["id"])
    assert seen["lock"] is None
    assert seen["tag"] == "1:"

    assert api.take_lock(bob, team_id, note["id"])["user_id"] == bob.id
    assert_error(api.client.put(lock_url(team_id, note["id"]), headers=alice.headers), 409, "note_locked")


def test_save_without_lock_is_refused(api: Api, scene: Scene) -> None:
    """Saving without having taken the lock is 409 `lock_required`."""
    alice, _, _, team_id, note = scene
    response = api.client.patch(note_url(team_id, note["id"]), json={"body": "x"}, headers=with_etag(alice, 1))
    assert_error(response, 409, "lock_required")


def test_save_while_someone_else_holds_it_is_refused(api: Api, scene: Scene) -> None:
    """Saving while someone else holds the lock is 409 `note_locked` naming them, and the body is unchanged."""
    alice, bob, _, team_id, note = scene
    api.take_lock(bob, team_id, note["id"])

    response = api.client.patch(note_url(team_id, note["id"]), json={"body": "x"}, headers=with_etag(alice, 1))

    error = assert_error(response, 409, "note_locked")
    assert error["lock"]["user_id"] == bob.id
    assert api.get_note(alice, team_id, note["id"])["body"] == "v1"


def test_save_after_own_lease_lapsed_is_refused(api: Api, scene: Scene, fake_clock: FakeClock) -> None:
    """A holder whose lease ran out, with nobody else taking it, gets `lock_required`.

    The code tells the client to re-take the lock and retry with the same version (Client protocol), rather than treat
    the lock as lost to someone.
    """
    alice, _, _, team_id, note = scene
    api.take_lock(alice, team_id, note["id"])
    fake_clock.advance(LEASE + 1)

    response = api.client.patch(note_url(team_id, note["id"]), json={"body": "x"}, headers=with_etag(alice, 1))

    assert_error(response, 409, "lock_required")


def test_holder_releases_and_release_is_idempotent(api: Api, scene: Scene) -> None:
    """Releasing is 204, and releasing again is still 204, so retrying a release whose response was lost is harmless."""
    alice, bob, _, team_id, note = scene
    api.take_lock(bob, team_id, note["id"])

    assert api.client.delete(lock_url(team_id, note["id"]), headers=bob.headers).status_code == 204
    assert api.client.delete(lock_url(team_id, note["id"]), headers=bob.headers).status_code == 204
    assert api.get_note(alice, team_id, note["id"])["lock"] is None


def test_member_cannot_release_someone_elses_lock(api: Api, scene: Scene) -> None:
    """A plain member releasing someone else's lock is 403, and the lock stays."""
    _, bob, carol, team_id, note = scene
    api.take_lock(bob, team_id, note["id"])

    assert_error(api.client.delete(lock_url(team_id, note["id"]), headers=carol.headers), 403, "forbidden")
    assert api.get_note(carol, team_id, note["id"])["lock"]["user_id"] == bob.id


def test_admin_breaks_a_members_lock(api: Api, scene: Scene) -> None:
    """An admin can release anyone's lock, and the former holder's next save is 409 `lock_required`.

    So they cannot write after losing the lock.
    """
    alice, bob, _, team_id, note = scene
    api.take_lock(bob, team_id, note["id"])

    assert api.client.delete(lock_url(team_id, note["id"]), headers=alice.headers).status_code == 204
    assert api.get_note(alice, team_id, note["id"])["lock"] is None
    # Bob's next save is refused, so he cannot write after losing the lock.
    response = api.client.patch(note_url(team_id, note["id"]), json={"body": "x"}, headers=with_etag(bob, 1))
    assert_error(response, 409, "lock_required")


def test_note_cannot_be_deleted_while_someone_else_edits(api: Api, scene: Scene) -> None:
    """Deleting a note someone else holds the lock on is 409 `note_locked`, even for its admin author.

    The editor would lose their work.
    """
    alice, bob, _, team_id, note = scene
    api.take_lock(bob, team_id, note["id"])

    # Not even by an admin who is also the author: Bob would lose his work.
    error = assert_error(api.delete_note(alice, team_id, note["id"], 1), 409, "note_locked")
    assert error["lock"]["user_id"] == bob.id


def test_note_can_be_deleted_by_its_own_lock_holder(api: Api, scene: Scene) -> None:
    """The lock holder may delete the note they are editing."""
    alice, _, _, team_id, note = scene
    api.take_lock(alice, team_id, note["id"])
    assert api.delete_note(alice, team_id, note["id"], 1).status_code == 204


def test_the_lock_belongs_to_the_user_not_the_session(api: Api, scene: Scene) -> None:
    """A second login of the holder (another tab or device) can renew, save with, and release the first one's lock.

    The lock records a user, not a session, so the contract lets the same person carry on from another tab; the
    version check, not the lock, is what keeps two tabs of one user from overwriting each other
    (`test_races.py::test_concurrent_saves_at_the_same_version_let_exactly_one_through`).
    """
    _, bob, _, team_id, note = scene
    login = api.client.post("/auth/login", json={"email": bob.email, "password": "correct horse"})
    other_tab = {"Authorization": f"Bearer {login.json()['token']}"}
    api.take_lock(bob, team_id, note["id"])
    url = lock_url(team_id, note["id"])

    assert api.client.put(url, params={"renew": "true", "active": "true"}, headers=other_tab).status_code == 200
    saved = api.client.patch(
        note_url(team_id, note["id"]), json={"body": "v2"}, headers={**other_tab, "If-Match": '"1"'}
    )
    assert saved.status_code == 200, saved.text
    assert api.client.delete(url, headers=other_tab).status_code == 204
    assert api.get_note(bob, team_id, note["id"])["lock"] is None


def test_stale_save_after_lease_lapse_is_a_version_conflict(api: Api, scene: Scene, fake_clock: FakeClock) -> None:
    """The Alice and Bob sequence: why the version check stays even with a lock.

    Alice opens version 1 and her laptop sleeps past the lease. Bob takes the lock, saves version 2 and releases. Alice
    wakes, her client re-takes the now-free lock, and she saves text based on version 1 (`If-Match: "1"`). Without the
    version check that would silently overwrite Bob's edit; with it, she gets 412 `precondition_failed` carrying Bob's
    note, and the note is unchanged. Fails with the version condition removed: checked.
    """
    alice, bob, _, team_id, note = scene
    api.take_lock(alice, team_id, note["id"])
    alices_copy = api.client.get(note_url(team_id, note["id"]), headers=alice.headers)

    fake_clock.advance(LEASE + 1)  # Alice's laptop sleeps
    api.take_lock(bob, team_id, note["id"])
    api.save_note(bob, team_id, note["id"], 1, body="Bob's edit")
    assert api.client.delete(lock_url(team_id, note["id"]), headers=bob.headers).status_code == 204

    api.take_lock(alice, team_id, note["id"])  # Alice wakes; the lock is free again
    response = api.client.patch(
        note_url(team_id, note["id"]),
        json={"body": "Alice's stale edit"},
        headers=with_etag(alice, alices_copy.headers["ETag"]),
    )

    error = assert_error(response, 412, "precondition_failed")
    assert (error["note"]["version"], error["note"]["body"]) == (2, "Bob's edit")
    after = api.get_note(bob, team_id, note["id"])
    assert (after["version"], after["body"]) == (2, "Bob's edit")


@pytest.mark.parametrize("if_match", [None, '"7"', "*"], ids=["missing", "stale", "star"])
def test_lock_rule_is_judged_before_the_precondition(api: Api, scene: Scene, if_match: str | None) -> None:
    """A caller without the lock gets today's lock error whatever `If-Match` says, and nothing changes.

    The documented check order: authentication, team access, the lock rule, then the precondition. Bob holds the lock;
    Carol's save is 409 `note_locked`, not 428 or 412. With nobody holding it, Carol's save is 409 `lock_required`. Ids
    name Carol's `If-Match`.
    """
    _, bob, carol, team_id, note = scene
    url = note_url(team_id, note["id"])
    headers = carol.headers if if_match is None else with_etag(carol, if_match)

    assert_error(api.client.patch(url, json={"body": "x"}, headers=headers), 409, "lock_required")
    api.take_lock(bob, team_id, note["id"])
    error = assert_error(api.client.patch(url, json={"body": "x"}, headers=headers), 409, "note_locked")

    assert error["lock"]["user_id"] == bob.id
    after = api.get_note(bob, team_id, note["id"])
    assert (after["version"], after["body"]) == (1, "v1")


@pytest.mark.parametrize("if_match", [None, '"7"', "*"], ids=["missing", "stale", "star"])
def test_delete_lock_and_ownership_are_judged_before_the_precondition(
    api: Api, scene: Scene, if_match: str | None
) -> None:
    """For a delete too: 403 for a member who is not the author, 409 `note_locked` while someone else edits.

    Both whatever `If-Match` says; the note stays. Ids name the `If-Match` sent.
    """
    alice, bob, _, team_id, note = scene

    assert_error(api.delete_note(bob, team_id, note["id"], if_match), 403, "forbidden")
    api.take_lock(bob, team_id, note["id"])
    assert_error(api.delete_note(alice, team_id, note["id"], if_match), 409, "note_locked")

    assert api.get_note(alice, team_id, note["id"])["version"] == 1


def renew(api: Api, who: Account, team_id: str, note_id: str, active: bool | None = True) -> Any:
    """`PUT` the lock with `?renew=true&active=...` as `who` and return the raw response. None sends no `active`."""
    params = {"renew": "true"}
    if active is not None:
        params["active"] = "true" if active else "false"
    return api.client.put(lock_url(team_id, note_id), params=params, headers=who.headers)


def test_lock_says_when_to_renew(api: Api, scene: Scene) -> None:
    """Every Lock carries `renew_after_seconds`: 20, a third of the 60-second lease.

    Clients take the renewal interval from the server instead of hard-coding it.
    """
    alice, bob, _, team_id, note = scene

    lock = api.take_lock(bob, team_id, note["id"])

    assert lock["renew_after_seconds"] == config.LOCK_RENEW_AFTER.total_seconds() == 20
    assert api.get_note(alice, team_id, note["id"])["lock"]["renew_after_seconds"] == 20


def test_renew_only_extends_the_callers_live_lock(api: Api, scene: Scene, fake_clock: FakeClock) -> None:
    """`?renew=true` on one's own live lock extends it and leaves the tag unchanged."""
    alice, _, _, team_id, note = scene
    first = api.take_lock(alice, team_id, note["id"])
    fake_clock.advance(LEASE - 10)

    response = renew(api, alice, team_id, note["id"])

    assert response.status_code == 200, response.text
    assert response.json()["expires_at"] > first["expires_at"]
    assert api.get_note(alice, team_id, note["id"])["tag"] == f"1:{alice.id}"


@pytest.mark.parametrize("how_lost", ["never-taken", "lease-lapsed", "admin-broke-it"])
def test_renew_only_never_takes_a_lock(api: Api, scene: Scene, fake_clock: FakeClock, how_lost: str) -> None:
    """The editor's renewal timer must not be what takes a lost lock back.

    A renewal is `PUT ...?renew=true`, which never takes a lock; a plain `PUT` also takes a free lock, so a timer using
    it would undo an admin's break or a lapsed lease without the user knowing. Ids name how Bob came to hold no lock:
    `never-taken`, `lease-lapsed`, `admin-broke-it`. Each is 409 `lock_required`, and the note stays unlocked.
    """
    alice, bob, _, team_id, note = scene
    if how_lost != "never-taken":
        api.take_lock(bob, team_id, note["id"])
    if how_lost == "lease-lapsed":
        fake_clock.advance(LEASE + 1)
    if how_lost == "admin-broke-it":
        assert api.client.delete(lock_url(team_id, note["id"]), headers=alice.headers).status_code == 204

    assert_error(renew(api, bob, team_id, note["id"]), 409, "lock_required")
    assert api.get_note(alice, team_id, note["id"])["lock"] is None


def test_renew_only_names_the_new_holder(api: Api, scene: Scene, fake_clock: FakeClock) -> None:
    """When someone else has taken the lock since, renew-only is 409 `note_locked` naming them.

    So a client can tell "taken by someone" from "lapsed".
    """
    _, bob, carol, team_id, note = scene
    api.take_lock(bob, team_id, note["id"])
    fake_clock.advance(LEASE + 1)
    carols = api.take_lock(carol, team_id, note["id"])

    error = assert_error(renew(api, bob, team_id, note["id"]), 409, "note_locked")

    assert error["lock"] == carols


def test_removing_a_member_releases_their_locks_in_that_team_only(api: Api, scene: Scene) -> None:
    """Removing a member frees their locks on that team's notes at once, not after the lease.

    Their locks in other teams stay. Without the release, a removed member's lock blocks the note for up to 60 seconds. Removal
    and release happen in one transaction.
    """
    alice, bob, _, team_id, note = scene
    other_team = api.create_team(bob, "Bob's own")
    elsewhere = api.create_note(bob, other_team)
    api.take_lock(bob, team_id, note["id"])
    api.take_lock(bob, other_team, elsewhere["id"])

    assert api.client.delete(f"/teams/{team_id}/members/{bob.id}", headers=alice.headers).status_code == 204

    seen = api.get_note(alice, team_id, note["id"])
    assert (seen["lock"], seen["tag"]) == (None, "1:")
    api.take_lock(alice, team_id, note["id"])  # free at once, not after the lease
    assert api.get_note(bob, other_team, elsewhere["id"])["lock"]["user_id"] == bob.id


# The idle rule: an open tab renews the lease forever, so the lock also ends 10 minutes after the holder's last
# activity (a take, a save, or a renewal with `active=true`).


def keep_renewing(
    api: Api, who: Account, team_id: str, note_id: str, fake_clock: FakeClock, seconds: float, active: bool
) -> None:
    """Renew every 20 s for `seconds`, as an open tab does, each with the same `active` flag; every renewal is 200."""
    for _ in range(int(seconds // RENEW_EVERY)):
        fake_clock.advance(RENEW_EVERY)
        response = renew(api, who, team_id, note_id, active)
        assert response.status_code == 200, response.text


def test_renewal_without_the_activity_flag_is_refused(api: Api, scene: Scene) -> None:
    """A renewal that does not say whether the holder typed is 422 `validation_error` naming `active`.

    The flag is required so a client that does not know the idle rule fails loudly instead of silently holding the
    note. The lock is untouched.
    """
    alice, _, _, team_id, note = scene
    api.take_lock(alice, team_id, note["id"])

    error = assert_error(renew(api, alice, team_id, note["id"], active=None), 422, "validation_error")

    assert error["details"][0]["loc"] == ["query", "active"]
    assert api.get_note(alice, team_id, note["id"])["lock"]["user_id"] == alice.id


def test_idle_holders_renewal_past_the_limit_is_refused_and_frees_the_note(
    api: Api, scene: Scene, fake_clock: FakeClock
) -> None:
    """The tab stays open and renews every 20 s, but nobody types: at the idle limit the renewal is 409 `lock_idle`.

    That renewal also releases the lock: the note reads as unlocked, a second renewal is `lock_required` (there is no
    lock left to find idle), and Bob can take it. Fails with the idle condition removed: checked (SQLite and
    Postgres).
    """
    alice, bob, _, team_id, note = scene
    api.take_lock(alice, team_id, note["id"])
    keep_renewing(api, alice, team_id, note["id"], fake_clock, IDLE - RENEW_EVERY, active=False)
    fake_clock.advance(RENEW_EVERY)  # the idle limit passes

    assert_error(renew(api, alice, team_id, note["id"], active=False), 409, "lock_idle")

    seen = api.get_note(bob, team_id, note["id"])
    assert (seen["lock"], seen["tag"]) == (None, "1:")
    assert_error(renew(api, alice, team_id, note["id"], active=False), 409, "lock_required")
    assert api.take_lock(bob, team_id, note["id"])["user_id"] == bob.id


def test_a_member_who_keeps_typing_never_goes_idle(api: Api, scene: Scene, fake_clock: FakeClock) -> None:
    """Renewing with `active=true` for three times the idle limit keeps the lock throughout, and others stay out."""
    alice, bob, _, team_id, note = scene
    api.take_lock(alice, team_id, note["id"])

    keep_renewing(api, alice, team_id, note["id"], fake_clock, 3 * IDLE, active=True)

    error = assert_error(api.client.put(lock_url(team_id, note["id"]), headers=bob.headers), 409, "note_locked")
    assert error["lock"]["user_id"] == alice.id


def test_a_save_counts_as_activity(api: Api, scene: Scene, fake_clock: FakeClock) -> None:
    """A save restarts the idle clock: inactive renewals carry on past the limit counted from the take."""
    alice, _, _, team_id, note = scene
    api.take_lock(alice, team_id, note["id"])
    keep_renewing(api, alice, team_id, note["id"], fake_clock, IDLE - RENEW_EVERY, active=False)

    api.save_note(alice, team_id, note["id"], 1, body="v2")
    keep_renewing(api, alice, team_id, note["id"], fake_clock, IDLE - RENEW_EVERY, active=False)

    assert api.get_note(alice, team_id, note["id"])["lock"]["user_id"] == alice.id


def test_activity_reported_after_the_limit_does_not_revive_the_lock(
    api: Api, scene: Scene, fake_clock: FakeClock
) -> None:
    """Idleness is judged before the renewal's own flag: once past the limit, even `active=true` gets `lock_idle`.

    From the limit on, other members already see the note as free, so a late renewal must not take it back. The holder
    re-takes it with a plain `PUT`, which counts as activity.
    """
    alice, _, _, team_id, note = scene
    api.take_lock(alice, team_id, note["id"])
    keep_renewing(api, alice, team_id, note["id"], fake_clock, IDLE - RENEW_EVERY, active=False)
    fake_clock.advance(RENEW_EVERY)

    assert_error(renew(api, alice, team_id, note["id"], active=True), 409, "lock_idle")
    assert api.take_lock(alice, team_id, note["id"])["user_id"] == alice.id


def test_renewal_after_the_lease_lapsed_is_lock_required_even_when_the_holder_was_also_idle(
    api: Api, scene: Scene, fake_clock: FakeClock
) -> None:
    """`lock_idle` is only for a live lease: once the lease has lapsed too, the late renewal is `lock_required`.

    Alice renews without typing up to just short of the idle limit, then her tab sleeps past both the lease and the idle
    limit. Her renewal finds no lock of hers at all, so it says so; the note stays free for Bob.
    """
    alice, bob, _, team_id, note = scene
    api.take_lock(alice, team_id, note["id"])
    keep_renewing(api, alice, team_id, note["id"], fake_clock, IDLE - RENEW_EVERY, active=False)
    fake_clock.advance(LEASE + 1)  # past the lease and the idle limit

    assert_error(renew(api, alice, team_id, note["id"], active=True), 409, "lock_required")
    assert api.take_lock(bob, team_id, note["id"])["user_id"] == bob.id


def test_idle_lock_reads_as_free_and_can_be_taken_before_anyone_renews(
    api: Api, scene: Scene, fake_clock: FakeClock
) -> None:
    """An idle lock nobody has renewed yet already reads as unlocked and is taken like a lapsed one.

    The holder's late renewal then names the new holder (`note_locked`), not `lock_idle`: the lock is no longer theirs.
    """
    alice, bob, _, team_id, note = scene
    api.take_lock(alice, team_id, note["id"])
    keep_renewing(api, alice, team_id, note["id"], fake_clock, IDLE - RENEW_EVERY, active=False)
    fake_clock.advance(RENEW_EVERY)

    seen = api.get_note(bob, team_id, note["id"])
    assert (seen["lock"], seen["tag"]) == (None, "1:")
    assert api.take_lock(bob, team_id, note["id"])["user_id"] == bob.id
    error = assert_error(renew(api, alice, team_id, note["id"], active=True), 409, "note_locked")
    assert error["lock"]["user_id"] == bob.id
