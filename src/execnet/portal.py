"""Cross-thread / cross-loop communication primitives.

A :class:`LoopPortal` is a handle to a running trio loop that foreign
threads use to run functions on the loop or push work into it.  Because
each direction only needs the *receiving* loop's token, two trio loops in
two threads can communicate by holding each other's portal (the sync
facade's host loop and a user loop; the worker loop and the process main
thread).

:class:`SyncReceiver` covers the opposite direction: a plain thread
(typically the process main thread) receiving items produced on a loop,
in a way that stays interruptible by KeyboardInterrupt.
"""

from __future__ import annotations

import queue
import threading
from collections.abc import Awaitable
from collections.abc import Callable
from typing import Any
from typing import Generic
from typing import TypeVar

import trio

__all__ = ["LoopPortal", "SyncReceiver"]

T = TypeVar("T")


class LoopPortal:
    """Handle to a running trio loop, usable from foreign threads.

    Must be constructed on the loop's own thread (it captures the current
    trio token).
    """

    def __init__(self) -> None:
        self._token = trio.lowlevel.current_trio_token()

    def is_loop_thread(self) -> bool:
        """Whether the calling thread is running this portal's loop."""
        try:
            return trio.lowlevel.current_trio_token() is self._token
        except RuntimeError:
            return False

    def run(self, async_fn: Callable[..., Awaitable[T]], *args: Any) -> T:
        """Run ``await async_fn(*args)`` on the loop, blocking this thread."""
        return trio.from_thread.run(async_fn, *args, trio_token=self._token)

    def run_sync(self, sync_fn: Callable[..., T], *args: Any) -> T:
        """Run ``sync_fn(*args)`` on the loop, blocking this thread."""
        return trio.from_thread.run_sync(sync_fn, *args, trio_token=self._token)

    def post(self, sync_fn: Callable[..., object], *args: Any) -> None:
        """Schedule ``sync_fn(*args)`` on the loop without waiting.

        Thread-safe and callable from the loop thread itself; all posts run
        in strict FIFO order (``TrioToken.run_sync_soon``).  Raises
        ``trio.RunFinishedError`` once the loop has shut down.
        """
        self._token.run_sync_soon(sync_fn, *args)


class SyncReceiver(Generic[T]):
    """Receive items on a plain thread, KeyboardInterrupt-friendly.

    ``queue.SimpleQueue.get`` parks in a C-level lock acquire that shields
    KeyboardInterrupt on the main thread; ``threading.Event.wait`` does not.
    So producers put into an unbounded queue and set a wake event, and
    :meth:`get` waits on the event and drains the queue.
    """

    def __init__(self) -> None:
        self._items: queue.SimpleQueue[T] = queue.SimpleQueue()
        self._wake = threading.Event()

    def put(self, item: T) -> None:
        """Thread-safe; usable from a loop thread (never blocks)."""
        self._items.put(item)
        self._wake.set()

    def get(self) -> T:
        """Block until an item is available (interruptible on main thread)."""
        while True:
            self._wake.wait()
            try:
                return self._items.get_nowait()
            except queue.Empty:
                # Re-check after clearing so a put between get_nowait and
                # clear cannot be lost.
                self._wake.clear()
                try:
                    return self._items.get_nowait()
                except queue.Empty:
                    continue
