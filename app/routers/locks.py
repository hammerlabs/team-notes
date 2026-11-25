"""The edit lock: one editor per note at a time.

The lock is a lease. It lasts LOCK_LEASE from each take or renewal, so an
editor who closes their laptop blocks the note for at most that long, and no
cleanup job is needed: an expired lease counts as free in the statement that
takes it.

An open tab renews the lease for as long as it is open, so the lease alone
cannot tell an editor from someone who walked away. The lock also records when
its holder was last active (a take, a save, or a renewal with `active=true`);
once that is older than LOCK_IDLE_LIMIT the lock is over. Others see it as
free from that instant, and the holder's next renewal is refused with
`lock_idle` and releases it.

`PUT .../lock` takes a free, expired or idle lock, or renews the caller's own.
`PUT .../lock?renew=true&active=...` only renews: an editor's renewal timer
must never be what takes the lock back after it was lost (an admin broke it,
the lease ran out and someone else saved, or the holder went idle), or the
editor would carry on typing over a note that changed under them.
"""

from typing import Annotated

from fastapi import Query, Response

from app import clock, config
from app.deps import CurrentUser, Member, NotifierDep, StoreDep, team_router
from app.errors import ApiError, error_responses
from app.schemas import LockOut
from app.store.interface import LockIdle, LockOutcome

router = team_router("locks")


@router.put("/notes/{note_id}/lock", responses=error_responses(409))
def take_lock(
    team_id: str,
    note_id: str,
    user: CurrentUser,
    store: StoreDep,
    notifier: NotifierDep,
    renew: bool = False,
    active: Annotated[
        bool | None,
        Query(description="Required with `renew=true`: whether the holder has typed since the previous renewal."),
    ] = None,
) -> LockOut:
    """Take a free, expired or idle lock, or renew the caller's own. Idempotent.

    With `renew=true`, only renew, and `active` is required: 409
    `lock_required` if the caller no longer holds a live lock, `note_locked`
    if someone else does, `lock_idle` if the caller's lock had gone idle while
    its lease was still live (it is released). A lapsed lease is
    `lock_required` whether or not the caller was also idle. Without `renew`,
    `active` is ignored: taking is activity.
    """
    if renew and active is None:
        # Required so that a client that does not know the idle rule fails
        # loudly, instead of renewing as "not active" or, worse, as "active".
        raise ApiError(
            422,
            "validation_error",
            "The request is not valid.",
            details=[{"loc": ["query", "active"], "message": "Field required with renew=true", "type": "missing"}],
        )
    now = clock.now()
    expires_at = now + config.LOCK_LEASE
    # The server trusts the client's `active` flag: it cannot see typing. A
    # member whose client always sends `active=true` keeps the lock while the
    # tab is open, bounded only by an admin breaking it.
    is_activity = not renew or bool(active)
    outcome = store.take_lock(team_id, note_id, user.id, now, expires_at, renew_only=renew, active=is_activity)
    # A renewal leaves the tag as it was, so it wakes nobody. A take and an
    # idle release change it, so viewers are woken, as for any release.
    if outcome is not LockOutcome.RENEWED:
        notifier.wake(note_id)
    if outcome is LockOutcome.IDLE_RELEASED:
        raise LockIdle()
    return LockOut(user_id=user.id, user_name=user.name, expires_at=expires_at)


@router.delete("/notes/{note_id}/lock", status_code=204, responses=error_responses(403, 409))
def release_lock(team_id: str, note_id: str, membership: Member, store: StoreDep, notifier: NotifierDep) -> Response:
    """Release the lock. The holder can, and an admin can break anyone's.

    Releasing a note that is not locked (or whose lease has run out, or whose
    holder is idle) is a no-op 204, so a retried release is harmless.
    """
    if store.release_lock(team_id, note_id, membership.user_id, membership.is_admin, clock.now()):
        notifier.wake(note_id)
    return Response(status_code=204)
