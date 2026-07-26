"""Cross-thread / cross-loop communication primitives — the boundary kit.

A :class:`LoopPortal` is a handle to a running trio loop that foreign
threads use to run functions on the loop or push work into it (the
consumer -> loop direction).  Because each direction only needs the
*receiving* loop's token, two trio loops in two threads can communicate
by holding each other's portal.

The loop -> consumer direction never blocks the loop and never knows who
is listening: the loop fires a :class:`Wakener`, a single thread-safe
``notify()`` supplied by the consumer.  Implementing that one method is
the entire integration surface for an event loop backend (threads here;
asyncio/gevent wakeners come with their facades).  On top of it sit the
two carriers:

* :class:`Mailbox` -- an item stream (channel payloads, exec requests),
* :class:`OneShot` -- a single result (write acknowledgements, call
  results a consumer wants to await instead of block on).
"""

from __future__ import annotations

import queue
import threading
import time
from collections.abc import Awaitable
from collections.abc import Callable
from typing import Any
from typing import Generic
from typing import Protocol
from typing import TypeVar
from typing import cast

import trio

__all__ = ["LoopPortal", "Mailbox", "OneShot", "ThreadWakener", "Wakener"]

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


class Wakener(Protocol):
    """Thread-safe consumer wakeup fired by the loop.

    ``notify()`` must never block and must be safe from any thread; it is
    the only thing the loop side ever calls.  The blocking mailbox/oneshot
    waits additionally need :meth:`wait`/:meth:`clear` executed in the
    consumer's own context (a thread here, a greenlet for a gevent
    wakener); loop-native consumers such as asyncio wait on their own
    side of ``notify`` instead.
    """

    def notify(self) -> None: ...

    def wait(self, timeout: float | None = None) -> bool: ...

    def clear(self) -> None: ...


class ThreadWakener:
    """Plain-thread wakener on a ``threading.Event``.

    ``threading.Event.wait`` stays interruptible by KeyboardInterrupt on
    the main thread (a C-level ``queue.SimpleQueue.get`` does not), which
    is why the carriers wait on the wakener and drain a queue instead of
    blocking in the queue itself.
    """

    def __init__(self) -> None:
        self._event = threading.Event()

    def notify(self) -> None:
        self._event.set()

    def wait(self, timeout: float | None = None) -> bool:
        return self._event.wait(timeout)

    def clear(self) -> None:
        self._event.clear()


class Mailbox(Generic[T]):
    """Loop -> consumer item stream: an unbounded queue plus a wakener.

    :meth:`put` never blocks and is safe from any thread (including the
    loop thread).  :meth:`get` blocks in the consumer's context via the
    wakener; after draining to empty it clears and re-checks so a ``put``
    racing the clear cannot be lost.  Multiple consumers are allowed.
    """

    def __init__(self, wakener: Wakener | None = None) -> None:
        self._items: queue.SimpleQueue[T] = queue.SimpleQueue()
        self._wakener = ThreadWakener() if wakener is None else wakener

    def put(self, item: T) -> None:
        """Thread-safe; usable from a loop thread (never blocks)."""
        self._items.put(item)
        self._wakener.notify()

    def get_nowait(self) -> T:
        """Return the next item or raise ``queue.Empty``."""
        return self._items.get_nowait()

    def get(self, timeout: float | None = None) -> T:
        """Block until an item is available; TimeoutError after ``timeout``."""
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            if deadline is None:
                self._wakener.wait()
            else:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not self._wakener.wait(remaining):
                    # Final non-blocking check: a put may have raced the
                    # timeout (its notify landing after our last wait).
                    try:
                        return self._items.get_nowait()
                    except queue.Empty:
                        raise TimeoutError(
                            "no item after %r seconds" % timeout
                        ) from None
            try:
                return self._items.get_nowait()
            except queue.Empty:
                # Empty: clear, then re-check so a put between get_nowait
                # and clear cannot be lost.
                self._wakener.clear()
                try:
                    return self._items.get_nowait()
                except queue.Empty:
                    continue


class OneShot(Generic[T]):
    """A single result crossing loop -> consumer exactly once.

    The loop side calls :meth:`set` (or :meth:`set_error`) at most once;
    the consumer blocks in :meth:`wait`, which returns the value,
    re-raises the stored error, or raises ``TimeoutError``.
    """

    _NOTSET = object()

    def __init__(self, wakener: Wakener | None = None) -> None:
        self._wakener = ThreadWakener() if wakener is None else wakener
        self._value: Any = self._NOTSET
        self._error: BaseException | None = None
        self._done = False

    def is_set(self) -> bool:
        return self._done

    def set(self, value: T) -> None:
        """Thread-safe; usable from a loop thread (never blocks)."""
        assert not self._done, "OneShot already resolved"
        self._value = value
        self._done = True
        self._wakener.notify()

    def set_error(self, error: BaseException) -> None:
        """Resolve with an error that :meth:`wait` will re-raise."""
        assert not self._done, "OneShot already resolved"
        self._error = error
        self._done = True
        self._wakener.notify()

    def wait(self, timeout: float | None = None) -> T:
        """Block until resolved; TimeoutError after ``timeout`` seconds."""
        deadline = None if timeout is None else time.monotonic() + timeout
        while not self._done:
            if deadline is None:
                self._wakener.wait()
            else:
                remaining = deadline - time.monotonic()
                if (remaining <= 0 or not self._wakener.wait(remaining)) and (
                    not self._done
                ):
                    raise TimeoutError("not resolved after %r seconds" % timeout)
        if self._error is not None:
            raise self._error
        return cast("T", self._value)
