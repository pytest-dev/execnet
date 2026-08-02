"""Cross-thread / cross-loop communication primitives — the boundary kit.

Internal.  A portal is a handle to a running loop that foreign threads use
to run functions on it or push work into it (the consumer -> loop
direction).  Because each direction only needs the *receiving* loop's
handle, two loops in two threads can communicate by holding each other's
portal.

There is one portal per engine backend -- :class:`LoopPortal` for trio,
:class:`AsyncioPortal` for asyncio -- with the same four operations and the
same failure vocabulary, so nothing above them has to know which loop it is
talking to.

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
from ._errors import LoopFinishedError
from ._errors import forked_error

__all__ = [
    "AsyncioPortal",
    "LoopPortal",
    "Mailbox",
    "OneShot",
    "ThreadWakener",
    "Wakener",
]

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
            raise forked_error("the execnet engine loop", self._pid)

    def run(self, async_fn: Callable[..., Awaitable[T]], *args: Any) -> T:
        """Run ``await async_fn(*args)`` on the loop, blocking this thread."""
        self._check_process()
        try:
            return trio.from_thread.run(async_fn, *args, trio_token=self._token)
        except trio.RunFinishedError as exc:
            raise LoopFinishedError(str(exc)) from None

    def run_sync(self, sync_fn: Callable[..., T], *args: Any) -> T:
        """Run ``sync_fn(*args)`` on the loop, blocking this thread."""
        self._check_process()
        try:
            return trio.from_thread.run_sync(sync_fn, *args, trio_token=self._token)
        except trio.RunFinishedError as exc:
            raise LoopFinishedError(str(exc)) from None

    def post(self, sync_fn: Callable[..., object], *args: Any) -> None:
        """Schedule ``sync_fn(*args)`` on the loop without waiting.

        Thread-safe and callable from the loop thread itself; all posts run
        in strict FIFO order (``TrioToken.run_sync_soon``).  Raises
        :class:`~execnet._errors.LoopFinishedError` once the loop has shut
        down, and :class:`~execnet._errors.ForkedResourceError` in a forked
        child.

        ``sync_fn`` must not raise: trio turns an exception from an
        entry-queue callback into ``TrioInternalError`` and tears the whole
        loop down, taking every gateway in the process with it.
        """
        self._check_process()
        try:
            self._token.run_sync_soon(sync_fn, *args)
        except trio.RunFinishedError as exc:
            raise LoopFinishedError(str(exc)) from None


class AsyncioPortal:
    """The same handle for an asyncio loop.

    Constructed on the loop's own thread, like :class:`LoopPortal`, and
    offering the same four operations with the same failure vocabulary.

    Where trio distinguishes "run a coroutine" from "run a sync function",
    asyncio has only the former, so :meth:`run_sync` wraps.  Both go through
    ``run_coroutine_threadsafe``, which -- unlike ``call_soon_threadsafe``
    -- gives back a future to block on.
    """

    def __init__(self) -> None:
        import asyncio

        self._asyncio = asyncio
        self._loop = asyncio.get_running_loop()
        self._pid = os.getpid()

    def is_loop_thread(self) -> bool:
        """Whether the calling thread is running this portal's loop."""
        try:
            return self._asyncio.get_running_loop() is self._loop
        except RuntimeError:
            return False

    def _check_process(self) -> None:
        """Refuse a loop that lives in another process (see :class:`LoopPortal`)."""
        if self._pid != os.getpid():
            raise forked_error("the execnet engine loop", self._pid)

    def _submit(self, coro: Any) -> Any:
        try:
            return self._asyncio.run_coroutine_threadsafe(coro, self._loop)
        except RuntimeError as exc:  # loop closed between the check and here
            coro.close()
            raise LoopFinishedError(str(exc)) from None

    def run(self, async_fn: Callable[..., Awaitable[T]], *args: Any) -> T:
        """Run ``await async_fn(*args)`` on the loop, blocking this thread."""
        self._check_process()
        if self.is_loop_thread():
            raise RuntimeError(
                "this is a blocking function; call it from a thread that is"
                " not running this loop"
            )
        result: T = self._submit(async_fn(*args)).result()
        return result

    def run_sync(self, sync_fn: Callable[..., T], *args: Any) -> T:
        """Run ``sync_fn(*args)`` on the loop, blocking this thread."""

        async def call() -> T:
            return sync_fn(*args)

        return self.run(call)

    def post(self, sync_fn: Callable[..., object], *args: Any) -> None:
        """Schedule ``sync_fn(*args)`` on the loop without waiting.

        Thread-safe and callable from the loop thread itself; all posts run
        in FIFO order.  Raises
        :class:`~execnet._errors.LoopFinishedError` once the loop has shut
        down, and :class:`~execnet._errors.ForkedResourceError` in a forked
        child.

        ``sync_fn`` must not raise: an exception here reaches the loop's
        exception handler, which by default only logs -- so a failure would
        be silently swallowed rather than reported to whoever was waiting.
        """
        self._check_process()
        try:
            self._loop.call_soon_threadsafe(sync_fn, *args)
        except RuntimeError as exc:
            raise LoopFinishedError(str(exc)) from None
