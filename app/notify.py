"""In-memory wake-up signals for the long poll.

A viewer waiting on a note is an `asyncio.Event` on the event loop. The save,
lock, unlock and delete handlers, and member removal, call `wake(note_id)`
after they commit.

Those handlers are sync, so they run on worker threads, while the events
belong to the event loop. asyncio objects are not thread-safe, so `wake`
never touches an event directly: it asks the event's loop to set it with
`call_soon_threadsafe`.

The signals live in this process only, which is why the service must run as
a single worker: a second process would not hear the first one's saves.
"""

import asyncio
import threading
from collections import Counter, defaultdict
from collections.abc import Iterator
from contextlib import contextmanager

from app.errors import too_many_polls


class Waiter:
    """One viewer's wake-up signal for one note."""

    def __init__(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._event = asyncio.Event()

    def wake_threadsafe(self) -> None:
        """Set the signal. Safe to call from any thread."""
        try:
            self._loop.call_soon_threadsafe(self._event.set)
        except RuntimeError:
            # The loop is closed (the server is shutting down), so nobody is
            # left waiting. The caller has already committed; raising here
            # would turn a saved change into a 500.
            pass

    async def wait(self, timeout: float) -> None:
        """Return when woken or after `timeout` seconds, whichever is first.

        The signal is cleared on return, so the caller should re-read the
        note afterwards: a wake that lands after the clear but before the
        re-read is seen by the re-read, and one after it sets the signal again.
        """
        try:
            await asyncio.wait_for(self._event.wait(), timeout)
        except TimeoutError:
            pass
        self._event.clear()


class Notifier:
    """Waiters grouped by note id, with a cap on how many one user may hold."""

    def __init__(self, max_polls_per_user: int) -> None:
        # Guards both maps: waiters are added on the event loop and read from
        # worker threads.
        self._lock = threading.Lock()
        self._waiters: defaultdict[str, set[Waiter]] = defaultdict(set)
        # A held poll costs only memory (about 40 KB and one socket), so the
        # cap is not about threads or connections: it stops one token from
        # opening thousands of polls and pinning that memory for 25 seconds.
        self._max_per_user = max_polls_per_user
        self._per_user: Counter[str] = Counter()
        # Set when the server starts shutting down; polls then stop waiting.
        self.stopping = False

    @contextmanager
    def listen(self, note_id: str, user_id: str) -> Iterator[Waiter]:
        """Register a waiter for `note_id` for the duration of the block.

        Must be entered on the event loop. Raises 429 `too_many_polls` if
        `user_id` already holds the maximum. The waiter is removed in
        `finally`, so a poll that ends in any way, including an error, leaves
        nothing behind.
        """
        waiter = Waiter()
        with self._lock:
            if self._per_user[user_id] >= self._max_per_user:
                raise too_many_polls(self._max_per_user)
            self._per_user[user_id] += 1
            self._waiters[note_id].add(waiter)
        try:
            yield waiter
        finally:
            with self._lock:
                waiters = self._waiters[note_id]
                waiters.discard(waiter)
                if not waiters:
                    del self._waiters[note_id]
                self._per_user[user_id] -= 1
                if not self._per_user[user_id]:
                    del self._per_user[user_id]

    def wake(self, note_id: str) -> None:
        """Wake everyone waiting on `note_id`. Call after the change is committed."""
        with self._lock:
            waiters = list(self._waiters.get(note_id, ()))
        for waiter in waiters:
            waiter.wake_threadsafe()

    def stop(self) -> None:
        """End every held poll now, because the server is stopping.

        Without this, the server waits for held polls before it exits, then
        cancels them, and their clients see an error instead of a clean 204.
        """
        self.stopping = True
        with self._lock:
            waiters = [waiter for note_waiters in self._waiters.values() for waiter in note_waiters]
        for waiter in waiters:
            waiter.wake_threadsafe()

    def polls_held_by(self, user_id: str) -> int:
        """How many polls `user_id` has waiting. Used by tests."""
        with self._lock:
            return self._per_user[user_id]

    def waiter_count(self, note_id: str) -> int:
        """How many polls are waiting on `note_id`. Used by tests to check cleanup."""
        with self._lock:
            return len(self._waiters.get(note_id, ()))
