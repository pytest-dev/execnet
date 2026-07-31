"""Cross-thread / cross-loop communication primitives — the boundary kit.

Internal.  A :class:`LoopPortal` is a handle to a running trio loop that
foreign threads use to run functions on the loop or push work into it (the
consumer -> loop direction).  Because each direction only needs the
*receiving* loop's token, two trio loops in two threads can communicate by
holding each other's portal.

The loop -> consumer direction never blocks the loop and never knows who
is listening: the loop fires a :class:`Wakener`, a single thread-safe
``notify()`` supplied by the consumer.  There are exactly two wakeners --
OS threads and gevent greenlets -- because every other concurrency library
gets a facade of its own (:mod:`execnet.trio`, :mod:`execnet.aio`) instead.
On top of the wakener sit the two carriers:

* :class:`Mailbox` -- an item stream (channel payloads, exec requests),
* :class:`OneShot` -- a single result (write acknowledgements, call
  results a consumer wants to await instead of block on).
"""

from __future__ import annotations

import os
from collections.abc import Awaitable
from collections.abc import Callable
from typing import Any
from typing import TypeVar

import trio

from ._boundary import Mailbox
from ._boundary import OneShot
from ._boundary import ThreadWakener
from ._boundary import Wakener
from ._errors import forked_error

__all__ = ["LoopPortal", "Mailbox", "OneShot", "ThreadWakener", "Wakener"]

T = TypeVar("T")


class LoopPortal:
    """Handle to a running trio loop, usable from foreign threads.

    Must be constructed on the loop's own thread (it captures the current
    trio token).
    """

    def __init__(self) -> None:
        self._token = trio.lowlevel.current_trio_token()
        self._pid = os.getpid()

    def is_loop_thread(self) -> bool:
        """Whether the calling thread is running this portal's loop."""
        try:
            return trio.lowlevel.current_trio_token() is self._token
        except RuntimeError:
            return False

    def _check_process(self) -> None:
        """Refuse a loop that lives in another process (see fork, below).

        The token of a forked parent's loop still *works* in the child --
        ``run_sync_soon`` happily queues a callback that nothing will ever
        run, and ``from_thread.run`` waits for a reply forever.  This is the
        one choke point every route to the loop goes through, so the check
        sits here rather than on each of them.
        """
        if self._pid != os.getpid():
            raise forked_error("the execnet host loop", self._pid)

    def run(self, async_fn: Callable[..., Awaitable[T]], *args: Any) -> T:
        """Run ``await async_fn(*args)`` on the loop, blocking this thread."""
        self._check_process()
        return trio.from_thread.run(async_fn, *args, trio_token=self._token)

    def run_sync(self, sync_fn: Callable[..., T], *args: Any) -> T:
        """Run ``sync_fn(*args)`` on the loop, blocking this thread."""
        self._check_process()
        return trio.from_thread.run_sync(sync_fn, *args, trio_token=self._token)

    def post(self, sync_fn: Callable[..., object], *args: Any) -> None:
        """Schedule ``sync_fn(*args)`` on the loop without waiting.

        Thread-safe and callable from the loop thread itself; all posts run
        in strict FIFO order (``TrioToken.run_sync_soon``).  Raises
        ``trio.RunFinishedError`` once the loop has shut down, and
        :class:`~execnet._errors.ForkedResourceError` in a forked child.

        ``sync_fn`` must not raise: trio turns an exception from an
        entry-queue callback into ``TrioInternalError`` and tears the whole
        loop down, taking every gateway in the process with it.
        """
        self._check_process()
        self._token.run_sync_soon(sync_fn, *args)
