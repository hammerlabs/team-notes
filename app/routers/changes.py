"""The long poll: tell a viewer when a note is saved, locked or unlocked.

This is the one `async` handler in the service. A sync handler would hold a
worker thread for the whole wait, and the pool has 40 of them, so the 41st
viewer would stall everything. Here a waiting viewer is a suspended
coroutine that holds no thread and no database connection.

Order matters, and each step closes a specific gap:

1. Listen first, then read the tag. Reading first would leave a window in
   which a save could land and wake nobody, so the viewer would sleep
   through it (a lost wake-up).
2. Read in one thread-pool hop with its own short session, so the
   connection is back in the pool before the wait starts.
3. Cap each wait at the lock's remaining lease. An expiring lock writes
   nothing and so wakes nobody; the cap makes the poll notice it anyway.
4. After any wake-up, re-read and compare tags. A wake is only a hint, so a
   spurious one simply goes back to waiting. Every read re-checks
   membership, so a member removed mid-poll gets 404 on the next wake, not
   the lock holder's details.
5. Stop waiting if the client disconnects, so an abandoned poll does not
   keep its waiter (and its slot in the per-user cap) for the full wait.
6. Stop waiting when the server begins to shut down (`Notifier.stop`), so
   shutdown does not wait on held polls.
"""

import asyncio
import time
from typing import Annotated

from fastapi import Query, Request, Response
from fastapi.concurrency import run_in_threadpool

from app import clock, config
from app.deps import CurrentUser, NotifierDep, StoreDep, team_router
from app.errors import error_responses
from app.schemas import ChangeOut
from app.store.interface import Store

router = team_router("changes")


@router.get(
    "/notes/{note_id}/changes",
    response_model=ChangeOut,
    responses={204: {"description": "Nothing changed within `wait` seconds."}, **error_responses(429)},
)
async def watch_changes(
    request: Request,
    team_id: str,
    note_id: str,
    user: CurrentUser,
    store: StoreDep,
    notifier: NotifierDep,
    since: str | None = None,
    wait: Annotated[float, Query(ge=0, le=config.MAX_POLL_WAIT_SECONDS)] = config.MAX_POLL_WAIT_SECONDS,
) -> ChangeOut | Response:
    """Return the note's Change once its tag differs from `since`, or 204 after `wait` seconds.

    At most MAX_POLLS_PER_USER polls per user wait at once; another gets 429.
    """
    deadline = time.monotonic() + wait
    disconnected: asyncio.Task[None] | None = None
    with notifier.listen(note_id, user.id) as waiter:
        try:
            while True:
                change = await run_in_threadpool(_read_change, store, team_id, note_id, user.id)
                if change.tag != since:
                    return change
                remaining = deadline - time.monotonic()
                if remaining <= 0 or notifier.stopping:
                    return Response(status_code=204)
                if change.lock is not None:
                    lease_left = (change.lock.expires_at - clock.now()).total_seconds()
                    remaining = min(remaining, max(lease_left, 0.0))
                # Started only once the poll actually waits; most polls with a
                # stale `since` return above without needing it.
                disconnected = disconnected or asyncio.create_task(_until_disconnected(request))
                woken = asyncio.create_task(waiter.wait(remaining))
                await asyncio.wait((woken, disconnected), return_when=asyncio.FIRST_COMPLETED)
                if disconnected.done():
                    woken.cancel()
                    # Nobody is reading any more; this reply goes nowhere.
                    return Response(status_code=204)
        finally:
            if disconnected is not None:
                disconnected.cancel()


async def _until_disconnected(request: Request) -> None:
    """Return when the client goes away.

    A GET's (empty) body has not been read, so the first message is that
    body; after it, the server's next message is the disconnect.
    """
    while (await request.receive())["type"] != "http.disconnect":
        pass


def _read_change(store: Store, team_id: str, note_id: str, user_id: str) -> ChangeOut:
    """The note's current Change, if the caller is still a member (404 if not, or if the note is gone).

    The store reads only the version and lock (not the body), since this runs on every wake.
    """
    return ChangeOut.from_note(store.note_change_for_member(team_id, note_id, user_id), clock.now())
