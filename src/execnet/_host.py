"""The Trio host thread that the blocking surfaces drive.

:mod:`execnet.trio` runs gateways *directly*, as tasks in the caller's own
nursery.  Every other surface -- :mod:`execnet.sync`, :mod:`execnet.aio`,
:mod:`execnet.gevent` -- has no loop of its own to put them on, so protocol
IO runs on a :class:`Host`: one OS thread running ``trio.run``.

There is one shared host per process by default, because a host is a
thread and a trio loop, not a resource groups need isolated from each
other.  Pass an explicit :class:`Host` when you do want isolation or
deterministic teardown::

    with execnet.Host() as host:
        group = execnet.Group(host=host)
        ...
    # the thread is joined here, rather than at interpreter exit

This module stays free of ``import trio`` so ``import execnet`` does not
load the event loop machinery: the real :class:`~execnet._trio_host.TrioHost`
is built on first use.
"""

from __future__ import annotations

import atexit
import os
import sys
import threading
from types import TracebackType
from typing import TYPE_CHECKING
from typing import Any

if TYPE_CHECKING:
    from typing_extensions import Self

__all__ = ["Host", "check_not_in_event_loop", "default_host"]

#: default cap on concurrent threadpool threads running receiver callbacks
DEFAULT_CALLBACK_THREADS = 40


def _running_event_loop() -> str | None:
    """``"asyncio"`` / ``"trio"`` when called from inside one, else None.

    Both checks go through ``sys.modules`` first, so a program that never
    imported asyncio or trio pays two dict lookups.  asyncio is probed with
    the private ``_get_running_loop`` because it *returns* None rather than
    raising -- this sits in front of every blocking channel operation, and
    building an exception per send is not free.
    """
    asyncio = sys.modules.get("asyncio")
    if asyncio is not None:
        get_running = getattr(asyncio.events, "_get_running_loop", None)
        if get_running is not None:
            if get_running() is not None:
                return "asyncio"
        else:  # pragma: no cover - every supported CPython has the private one
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                pass
            else:
                return "asyncio"
    trio = sys.modules.get("trio")
    if trio is not None:
        try:
            trio.lowlevel.current_trio_token()
        except RuntimeError:
            pass
        else:
            return "trio"
    return None


#: which namespace to point a caller at, per detected loop
_SURFACE_FOR_LOOP = {
    "asyncio": "execnet.aio (AsyncGroup)",
    "trio": "execnet.trio (AsyncGroup)",
}


def check_not_in_event_loop(what: str) -> None:
    """Raise if ``what`` is about to block a running event loop's thread.

    The blocking surfaces park the calling thread on a wakener, which
    inside a running loop stalls every task on it -- usually as a hang, so
    it is worth turning into an error that names the right namespace.
    """
    loop = _running_event_loop()
    if loop is None:
        return
    raise RuntimeError(
        f"{what} blocks the calling thread and you are inside a running"
        f" {loop} event loop, which would stall every task on it."
        f" Use {_SURFACE_FOR_LOOP[loop]} instead, or run this in a"
        " worker thread."
    )


class Host:
    """A Trio event loop on a dedicated thread, shared by gateway groups.

    Starting is lazy: constructing a Host costs nothing, and the thread
    comes up when a group first needs it.
    """

    def __init__(
        self,
        name: str = "execnet-host",
        callback_threads: int = DEFAULT_CALLBACK_THREADS,
    ) -> None:
        self.name = name
        self.callback_threads = callback_threads
        self._lock = threading.Lock()
        self._trio_host: Any = None

    def __repr__(self) -> str:
        state = "running" if self._trio_host is not None else "idle"
        return f"<execnet.Host {self.name!r} {state}>"

    @property
    def running(self) -> bool:
        return self._trio_host is not None

    def _ensure_started(self) -> Any:
        """The started :class:`~execnet._trio_host.TrioHost` (internal)."""
        with self._lock:
            if self._trio_host is None:
                from . import _trio_host

                trio_host = _trio_host.TrioHost(
                    name=self.name, callback_threads=self.callback_threads
                )
                trio_host.start()
                self._trio_host = trio_host
            return self._trio_host

    def close(self, timeout: float | None = 5.0) -> None:
        """Stop the loop and join the thread (no-op when not running).

        Gateways served by this host must already be terminated; closing
        does not terminate them for you.
        """
        with self._lock:
            trio_host, self._trio_host = self._trio_host, None
        if trio_host is not None:
            trio_host.stop(timeout=timeout)

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()


_default_lock = threading.Lock()
_default: Host | None = None
#: the pid _default was created in; a forked child inherits a dead thread
_default_pid: int | None = None


def default_host() -> Host:
    """The process-wide host, started lazily and stopped at interpreter exit.

    After ``os.fork()`` the child inherits a Host whose thread does not
    exist there, so the first use in a child builds a fresh one.
    """
    global _default, _default_pid
    with _default_lock:
        pid = os.getpid()
        if _default is None or _default_pid != pid:
            _default = Host()
            _default_pid = pid
        return _default


def _close_default_atexit() -> None:
    host = _default
    if host is not None and _default_pid == os.getpid():
        host.close(timeout=1.0)


atexit.register(_close_default_atexit)
