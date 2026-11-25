"""Check-then-act races, run with real concurrent requests.

Every rule with a "check" in it (one account per email, one free seat, one admin left, one lock holder, one save per
version) is enforced inside a single conditional statement or constraint, as `app/store/sql.py` explains. These tests
fire the competing requests from threads released together by a barrier (`helpers.race`) and assert what must hold in
every serial order the database could pick.

Why they can be trusted to catch a broken guard:

- A race only bites if transactions overlap, so most tests use 8 competitors over several rounds, and the two races
  between just two requests run 30 rounds (`PAIR_ROUNDS`). Rounds also matter on Postgres for a duller reason: the
  first race in a test opens new pool connections, which is slow enough to serialise the competitors, so a single
  race there let broken guards pass.
- Mutation checks: a thread-race test here is checked by breaking its guard the way a plausible bug would (the
  rule moved out of the statement into a read before the write, the unique constraint replaced by a lookup, or, on
  Postgres, the team row lock removed), running the test 10 times on each database, and restoring the guard. Each
  checked test's docstring records what is broken and how many runs fail; a test that is not checked says so. With
  the guard intact the same tests pass every control run.
- On Postgres (`TEST_DATABASE_URL`), these exercise what SQLite's single writer gives for free: the team row lock behind
  the count guards, and the re-check of a row's WHERE clause after waiting for another writer.

The last section forces a narrower race deterministically: state that changes between a refused write and the read that
words its error.

Not here: the same rules one request at a time (`test_teams.py`, `test_locks.py`, `test_notes.py`).
"""

from collections.abc import Callable
from datetime import datetime
from typing import Any

import pytest
from fastapi import FastAPI
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app import clock, config
from app.models import Membership, Note
from helpers import Account, Api, TeamOfTwo, assert_error, lock_url, note_url, race, with_etag

ATTEMPTS = 8  # competing requests per race
ROUNDS = 10  # for races with two legal outcomes, so both orders are likely to be seen
# For the two-request races (delete against take, break against save): one round overlaps in only a few runs in
# ten on a loaded host, and with ten rounds a broken guard on Postgres still passed one or two runs in ten.
PAIR_ROUNDS = 30
# For the count guards. With a broken note cap, a single round failed only 3 runs in 4;
# with five rounds the mutation checks failed every run.
CAP_ROUNDS = 5


def test_duplicate_signup_creates_one_account(api: Api) -> None:
    """Eight simultaneous sign-ups with one email create one account; the other seven get 409, in each of ten rounds.

    Enforced by the unique index on email, not by a lookup before the insert. Mutation-checked: with the unique
    constraint removed and a lookup of the email before the insert instead, it failed 10 of 10 runs on SQLite and 10 of
    10 on Postgres (a single round failed only 4 of 10 on SQLite, hence the rounds). Keeping the constraint but not
    catching its error fails every run too, as a 500.
    """
    for round_ in range(ROUNDS):
        body = {"email": f"same{round_}@example.com", "password": "correct horse", "name": "Same"}

        statuses = race(*[lambda body=body: api.client.post("/auth/signup", json=body)] * ATTEMPTS)

        assert statuses == [201] + [409] * (ATTEMPTS - 1), f"round {round_}"


def test_duplicate_member_is_added_once(api: Api) -> None:
    """Eight simultaneous adds of the same user add them once; the other seven get 409, in each of ten rounds.

    Enforced by the membership primary key. Mutation-checked: with the key on (team, user) replaced by a surrogate id
    and a membership lookup before the insert, it failed 10 of 10 runs on SQLite and 10 of 10 on Postgres (a single
    round failed 0 of 10 on Postgres, hence the rounds).
    """
    alice = api.signup("alice")
    team = api.create_team(alice)
    url = f"/teams/{team}/members"
    for round_ in range(ROUNDS):
        newcomer = api.signup(f"newcomer{round_}")
        body = {"email": newcomer.email}

        statuses = race(*[lambda body=body: api.client.post(url, json=body, headers=alice.headers)] * ATTEMPTS)

        assert statuses == [201] + [409] * (ATTEMPTS - 1), f"round {round_}"
        members = api.client.get(url, headers=alice.headers).json()
        assert [m["user_id"] for m in members].count(newcomer.id) == 1, f"round {round_}"


def test_last_member_seat_goes_to_exactly_one(
    app: FastAPI, api: Api, team: TeamOfTwo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With one free seat, eight simultaneous adds give one 201 and seven 409, in each of five rounds.

    Mutation-checked: counting members first and then inserting failed 10 of 10 runs on SQLite, and making
    `PostgresStore._lock_team` a no-op failed 10 of 10 on Postgres.
    """
    alice, _, team_id = team
    url = f"/teams/{team_id}/members"
    # Several rounds, one free seat each: a broken guard can get lucky with
    # the thread schedule once, but not every time.
    for round_ in range(CAP_ROUNDS):
        seats = 3 + round_  # Alice, Bob, and one winner per earlier round, plus one free seat
        monkeypatch.setattr(config, "MAX_TEAM_MEMBERS", seats)
        candidates = [api.signup(f"candidate{round_}-{i}") for i in range(ATTEMPTS)]

        statuses = race(
            *[(lambda c=c: api.client.post(url, json={"email": c.email}, headers=alice.headers)) for c in candidates]
        )

        assert statuses == [201] + [409] * (ATTEMPTS - 1), f"round {round_}"
        with Session(app.state.store.engine) as db:
            assert db.scalar(select(func.count()).where(Membership.team_id == team_id)) == seats


def test_last_note_slot_goes_to_exactly_one(
    app: FastAPI, api: Api, team: TeamOfTwo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With one free note slot, eight simultaneous creates give one 201 and seven 409, in each of five rounds.

    Mutation-checked: counting notes first and then inserting failed 10 of 10 runs on SQLite, and making
    `PostgresStore._lock_team` a no-op failed 10 of 10 on Postgres.
    """
    alice, _, team_id = team
    url = f"/teams/{team_id}/notes"
    for round_ in range(CAP_ROUNDS):
        slots = round_ + 1  # one free slot each round
        monkeypatch.setattr(config, "MAX_NOTES_PER_TEAM", slots)

        statuses = race(
            *[
                (lambda i=i: api.client.post(url, json={"title": f"Note {i}"}, headers=alice.headers))
                for i in range(ATTEMPTS)
            ]
        )

        assert statuses == [201] + [409] * (ATTEMPTS - 1), f"round {round_}"
        with Session(app.state.store.engine) as db:
            assert db.scalar(select(func.count()).where(Note.team_id == team_id)) == slots


def test_admins_leaving_at_once_leave_exactly_one_admin(app: FastAPI, api: Api) -> None:
    """Every admin of a team leaves at the same moment: all but one may go.

    Many contenders over several rounds, because two removals only race if their short transactions overlap.
    Mutation-checked: without the team row lock on Postgres it failed 10 of 10 runs (every round left no admin); on
    SQLite, moving the admin count out of the `DELETE` into a separate read failed 10 of 10.
    """
    for round_ in range(CAP_ROUNDS):
        admins = [api.signup(f"admin{round_}-{i}") for i in range(ATTEMPTS)]
        team = api.create_team(admins[0])
        for admin in admins[1:]:
            api.add_member(admins[0], team, admin, role="admin")
        url = f"/teams/{team}/members"

        statuses = race(*[(lambda a=a, url=url: api.client.delete(f"{url}/{a.id}", headers=a.headers)) for a in admins])

        assert statuses == [204] * (ATTEMPTS - 1) + [409], f"round {round_}"
        with Session(app.state.store.engine) as db:
            roles = db.scalars(select(Membership.role).where(Membership.team_id == team)).all()
        assert roles == ["admin"], f"round {round_}"


def test_only_one_of_many_takes_a_free_lock(api: Api) -> None:
    """Eight members take a free lock at once: one 200, seven 409 `note_locked`, on each of ten fresh notes.

    The lock's own guard: the take is one conditional UPDATE. Mutation-checked: reading the lock first and then
    updating with only the note id in the WHERE failed 10 of 10 runs on SQLite and 10 of 10 on Postgres (a single round
    failed 9 of 10 on SQLite and 0 of 10 on Postgres, hence the rounds).
    """
    alice = api.signup("alice")
    team = api.create_team(alice)
    editors = [api.signup(f"editor{i}") for i in range(ATTEMPTS)]
    for editor in editors:
        api.add_member(alice, team, editor)
    for round_ in range(ROUNDS):
        url = lock_url(team, api.create_note(alice, team)["id"])

        statuses = race(*[(lambda e=e, url=url: api.client.put(url, headers=e.headers)) for e in editors])

        assert statuses == [200] + [409] * (ATTEMPTS - 1), f"round {round_}"


def test_concurrent_saves_at_the_same_version_let_exactly_one_through(api: Api, team: TeamOfTwo) -> None:
    """Eight tabs of the lock holder save with the same `If-Match`: one wins, the rest get 412.

    Every loser's `precondition_failed` carries the winner's text, so a client can show what it lost to.
    Mutation-checked: reading the version separately and updating without the version condition failed 10 of 10 runs.
    Fails with the version condition removed from the save's UPDATE: checked (SQLite and Postgres).
    """
    alice, _, team_id = team
    note = api.create_note(alice, team_id)
    api.take_lock(alice, team_id, note["id"])
    url = note_url(team_id, note["id"])

    responses: list[Any] = []

    def save(i: int) -> Any:
        response = api.client.patch(url, json={"body": f"tab {i}"}, headers=with_etag(alice, 1))
        responses.append(response)
        return response

    statuses = race(*[(lambda i=i: save(i)) for i in range(ATTEMPTS)])

    assert statuses == [200] + [412] * (ATTEMPTS - 1)
    winner = next(r.json() for r in responses if r.status_code == 200)
    for loser in (r for r in responses if r.status_code == 412):
        assert assert_error(loser, 412, "precondition_failed")["note"]["body"] == winner["body"]
    assert api.get_note(alice, team_id, note["id"])["version"] == 2


def test_delete_and_save_on_the_same_etag_never_both_win(api: Api, team: TeamOfTwo) -> None:
    """The lock holder's save and their delete both send `If-Match: "1"`: never both succeed.

    Save first: the note is at version 2 and the delete gets 412 with it. Delete first: the save finds no note (404).
    Thirty rounds, one fresh note each. Fails with the version condition removed from the DELETE: checked.
    """
    alice, _, team_id = team

    def one_round(note_id: str) -> None:
        api.take_lock(alice, team_id, note_id)
        statuses = race(
            lambda: api.delete_note(alice, team_id, note_id, 1),
            lambda: api.client.patch(note_url(team_id, note_id), json={"body": "v2"}, headers=with_etag(alice, 1)),
        )
        after = api.client.get(note_url(team_id, note_id), headers=alice.headers)
        assert (statuses, after.status_code) in (([204, 404], 404), ([200, 412], 200)), statuses

    for _ in range(PAIR_ROUNDS):
        one_round(api.create_note(alice, team_id)["id"])


def test_delete_and_lock_take_never_both_win(api: Api, team: TeamOfTwo) -> None:
    """A delete and a lock take on the same note never both succeed.

    Either the note is gone and the take is 404, or the take won and the delete is 409 with the note still Bob's. Thirty
    rounds, one fresh note each. Mutation-checked: reading the lock first and deleting without the lock condition failed
    10 of 10 runs on SQLite and 10 of 10 on Postgres (with ten rounds, 8 of 10 on Postgres, hence thirty).
    """
    alice, bob, team_id = team

    def one_round(note_id: str) -> None:
        statuses = race(
            lambda: api.delete_note(alice, team_id, note_id, 1),
            lambda: api.client.put(lock_url(team_id, note_id), headers=bob.headers),
        )
        after = api.client.get(note_url(team_id, note_id), headers=alice.headers)
        # Delete first: Bob's take finds no note. Take first: the delete
        # would destroy Bob's edit, so it is refused and the note stays his.
        assert (statuses, after.status_code) in (([204, 404], 404), ([200, 409], 200)), statuses
        if after.status_code == 200:
            assert after.json()["lock"]["user_id"] == bob.id

    for _ in range(PAIR_ROUNDS):
        one_round(api.create_note(alice, team_id)["id"])


def test_lock_break_and_save_never_both_win(api: Api, team: TeamOfTwo) -> None:
    """An admin breaks Bob's lock while Bob saves: the save lands before the break or not at all.

    A save after the break would mean Bob wrote without the lock. The final note looks the same either way, so the test
    reads the save's response, which shows the row it wrote: a save that landed after the break wrote a row with no
    lock. Thirty rounds, one fresh note each. Mutation-checked: moving the lock check out of the UPDATE into a read
    before it (the version check kept) failed 10 of 10 runs on SQLite and 10 of 10 on Postgres (with ten rounds, 9 of 10
    on Postgres, hence thirty). Without the check on the save's response, that mutation passes 10 of 10.
    """
    alice, bob, team_id = team

    def one_round(note_id: str) -> None:
        api.take_lock(bob, team_id, note_id)
        saved: list[Any] = []

        def save() -> Any:
            saved.append(api.client.patch(note_url(team_id, note_id), json={"body": "v2"}, headers=with_etag(bob, 1)))
            return saved[0]

        statuses = race(lambda: api.client.delete(lock_url(team_id, note_id), headers=alice.headers), save)

        after = api.get_note(alice, team_id, note_id)
        assert after["lock"] is None
        if saved[0].status_code == 200:
            assert statuses == [200, 204]
            # The response shows the row the save wrote. Bob's lock on it means
            # the save landed before the break; `null` would mean after it.
            assert (saved[0].json()["lock"] or {}).get("user_id") == bob.id
            assert (after["version"], after["body"]) == (2, "v2")
        else:
            assert statuses == [204, 409]
            assert_error(saved[0], 409, "lock_required")
            assert (after["version"], after["body"]) == (1, "v1")

    for _ in range(PAIR_ROUNDS):
        one_round(api.create_note(alice, team_id, "Plan", "v1")["id"])


def test_idle_release_and_another_take_never_lose_the_new_holders_lock(app: FastAPI, api: Api, team: TeamOfTwo) -> None:
    """Alice's lock is idle; her renewal (which releases it) races Bob's take. Bob always ends up holding the lock.

    Release first: Alice gets `lock_idle` and Bob takes the freed lock. Take first: Bob takes the idle lock and Alice's
    renewal finds it is Bob's (`note_locked`). Never may the release clear the lock Bob has just taken, which a release
    judged on an earlier read would. Thirty rounds, one fresh note each. Not mutation-checked.
    """
    alice, bob, team_id = team

    def one_round(note_id: str) -> None:
        api.take_lock(alice, team_id, note_id)
        with app.state.store.engine.begin() as connection:
            connection.execute(
                update(Note).where(Note.id == note_id).values(lock_active_at=clock.now() - config.LOCK_IDLE_LIMIT)
            )
        statuses = race(
            lambda: api.client.put(
                lock_url(team_id, note_id), params={"renew": "true", "active": "false"}, headers=alice.headers
            ),
            lambda: api.client.put(lock_url(team_id, note_id), headers=bob.headers),
        )
        assert statuses == [200, 409], statuses
        assert api.get_note(alice, team_id, note_id)["lock"]["user_id"] == bob.id

    for _ in range(PAIR_ROUNDS):
        one_round(api.create_note(alice, team_id)["id"])


# A refused write, then a state change, then the read that words the error.
# That second read can contradict the refusal (`note_locked` naming nobody,
# `precondition_failed` at the sender's own version), so the store retries the
# conditional write once when the wording read finds no blocker.


def _set_lock(app: FastAPI, note_id: str, user_id: str | None, expires_at: datetime | None) -> None:
    """Write a note's lock columns directly, bypassing the store, to change state at an exact moment.

    A lock set here has its holder active now.
    """
    active_at = clock.now() if user_id is not None else None
    with app.state.store.engine.begin() as connection:
        connection.execute(
            update(Note)
            .where(Note.id == note_id)
            .values(lock_user_id=user_id, lock_expires_at=expires_at, lock_active_at=active_at)
        )


def _before_first_wording_read(
    monkeypatch: pytest.MonkeyPatch, app: FastAPI, change: Callable[[], None], undo: Callable[[], None] | None = None
) -> list[int]:
    """Run `change` just before each wording read and `undo` right after it. Returns the read count.

    Wraps the store's private `_note_for_refusal`: the read that runs only after a conditional write changed nothing, to
    say why. That seam is exactly where the state can change, so the tests hook it instead of racing threads into it.
    """
    store = app.state.store
    real_read = store._note_for_refusal
    reads: list[int] = []

    def read(*args: Any) -> Any:
        reads.append(1)
        change()
        try:
            return real_read(*args)
        finally:
            if undo:
                undo()

    monkeypatch.setattr(store, "_note_for_refusal", read)
    return reads


@pytest.fixture
def held_by_bob(app: FastAPI, api: Api, team: TeamOfTwo) -> tuple[Account, Account, str, str, datetime]:
    """A note by Alice whose lock Bob holds, and the lock's stored expiry."""
    alice, bob, team_id = team
    note = api.create_note(alice, team_id, "Plan", "v1")
    api.take_lock(bob, team_id, note["id"])
    with Session(app.state.store.engine) as db:
        expires_at = db.scalar(select(Note.lock_expires_at).where(Note.id == note["id"]))
    return alice, bob, team_id, note["id"], expires_at


Held = tuple[Account, Account, str, str, datetime]


def test_take_refused_by_a_lock_released_meanwhile_is_retried(
    app: FastAPI, api: Api, held_by_bob: Held, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A take refused while Bob holds the lock is retried once if the lock is gone by the wording read.

    Alice gets the lock. Without the retry this is `note_locked` with `lock: null`, a refusal naming nobody.
    """
    alice, _, team_id, note_id, _ = held_by_bob
    reads = _before_first_wording_read(monkeypatch, app, lambda: _set_lock(app, note_id, None, None))

    response = api.client.put(lock_url(team_id, note_id), headers=alice.headers)

    assert response.status_code == 200, response.text
    assert response.json()["user_id"] == alice.id
    assert len(reads) == 1


def test_delete_refused_by_a_lock_released_meanwhile_is_retried(
    app: FastAPI, api: Api, held_by_bob: Held, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same for delete: the lock is gone by the wording read, the retried delete succeeds (204)."""
    alice, _, team_id, note_id, _ = held_by_bob
    _before_first_wording_read(monkeypatch, app, lambda: _set_lock(app, note_id, None, None))

    assert api.delete_note(alice, team_id, note_id, 1).status_code == 204


def test_save_refused_then_allowed_by_the_callers_other_tab_is_retried(
    app: FastAPI, api: Api, held_by_bob: Held, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bob's lease lapsed; his other tab re-takes it before the wording read.

    Without the retry the read finds Bob holding the lock at the version he sent, and the answer is
    `precondition_failed` carrying his own version.
    """
    _, bob, team_id, note_id, _ = held_by_bob
    _set_lock(app, note_id, None, None)
    renewed = clock.now() + config.LOCK_LEASE
    _before_first_wording_read(monkeypatch, app, lambda: _set_lock(app, note_id, bob.id, renewed))

    saved = api.save_note(bob, team_id, note_id, 1, body="v2")

    assert (saved["version"], saved["body"]) == (2, "v2")


def test_release_refused_then_allowed_is_retried_not_forbidden(
    app: FastAPI, api: Api, held_by_bob: Held, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Carol's release is refused (Bob holds it), then Bob's lease lapses and Carol takes it in another tab.

    Without the retry the answer is 403 "someone else holds this lock" while Carol held it.
    """
    alice, _, team_id, note_id, _ = held_by_bob
    carol = api.signup("carol")
    api.add_member(alice, team_id, carol)
    later = clock.now() + config.LOCK_LEASE
    _before_first_wording_read(monkeypatch, app, lambda: _set_lock(app, note_id, carol.id, later))

    assert api.client.delete(lock_url(team_id, note_id), headers=carol.headers).status_code == 204
    assert api.get_note(alice, team_id, note_id)["lock"] is None


def test_refusal_after_the_one_retry_is_note_locked_without_a_lock(
    app: FastAPI, api: Api, held_by_bob: Held, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The lock is free whenever the error is worded and Bob's whenever the write runs.

    After one retry the server stops and answers `note_locked` with `lock: null`, which the contract defines as "retry".
    Exactly two reads: the retry is bounded.
    """
    alice, bob, team_id, note_id, expires_at = held_by_bob
    reads = _before_first_wording_read(
        monkeypatch,
        app,
        change=lambda: _set_lock(app, note_id, None, None),
        undo=lambda: _set_lock(app, note_id, bob.id, expires_at),
    )

    response = api.client.put(lock_url(team_id, note_id), headers=alice.headers)

    assert assert_error(response, 409, "note_locked")["lock"] is None
    assert len(reads) == 2
    assert api.get_note(alice, team_id, note_id)["lock"]["user_id"] == bob.id
