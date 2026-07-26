"""Trio-free half of the boundary kit: Wakener, Mailbox, OneShot.

Importable without loading any event loop (``import execnet`` must not
import trio); ``execnet.portal`` re-exports these next to LoopPortal.
"""

from __future__ import annotations

import queue
import threading
import time
from collections.abc import Callable
from typing import Any
from typing import Generic
from typing import Protocol
from typing import TypeVar
from typing import cast

__all__ = [
    "Flag",
    "Mailbox",
    "OneShot",
    "ThreadWakener",
    "Wakener",
    "make_wakener",
    "register_wakener",
]

T = TypeVar("T")


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


class Flag:
    """An idempotent event on a wakener: may be set any number of times.

    Each Flag owns its wakener exclusively -- sharing one wakener between
    carriers would lose wakeups (another carrier's ``clear`` can swallow
    this one's ``notify``).
    """

    def __init__(self, wakener: Wakener | None = None) -> None:
        self._wakener = ThreadWakener() if wakener is None else wakener
        self._flag = False

    def is_set(self) -> bool:
        return self._flag

    def set(self) -> None:
        """Thread-safe; usable from a loop thread (never blocks)."""
        self._flag = True
        self._wakener.notify()

    def wait(self, timeout: float | None = None) -> bool:
        """Block until set; returns whether the flag is set."""
        deadline = None if timeout is None else time.monotonic() + timeout
        while not self._flag:
            if deadline is None:
                self._wakener.wait()
            else:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not self._wakener.wait(remaining):
                    break
        return self._flag


# wait= axis: named wakener factories; each call returns a fresh instance
# (carriers own their wakener exclusively, see Flag).  Backends register
# here next to the built-in "thread"; entries in the lazy table import
# their module (which registers itself) on first use.
_WAKENER_FACTORIES: dict[str, Callable[[], Wakener]] = {
    "thread": ThreadWakener,
}
_LAZY_WAKENER_MODULES: dict[str, str] = {
    "gevent": "execnet._gevent_support",
}


def register_wakener(name: str, factory: Callable[[], Wakener]) -> None:
    """Register a wakener factory for the ``wait=`` spec axis."""
    _WAKENER_FACTORIES[name] = factory


def wakener_names() -> list[str]:
    return sorted(set(_WAKENER_FACTORIES) | set(_LAZY_WAKENER_MODULES))


def make_wakener(name: str) -> Wakener:
    """Create a fresh wakener for the named wait backend."""
    if name not in _WAKENER_FACTORIES and name in _LAZY_WAKENER_MODULES:
        import importlib

        importlib.import_module(_LAZY_WAKENER_MODULES[name])
    try:
        factory = _WAKENER_FACTORIES[name]
    except KeyError:
        raise ValueError(f"unknown wait backend {name!r}") from None
    return factory()
