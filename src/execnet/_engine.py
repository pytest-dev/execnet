"""The engine that protocol IO runs on: one loop, on a thread of its own.

:mod:`execnet.raw_trio` runs gateways *directly*, as tasks in the caller's
own nursery.  Every other surface -- :mod:`execnet.sync`,
:mod:`execnet.trio`, :mod:`execnet.aio`, :mod:`execnet.gevent` -- gives them
an engine instead: one OS thread running ``trio.run``, which keeps serving
while the caller's own thread or loop is busy elsewhere.

There is one shared engine per process by default, because an engine is a
thread and a loop, not a resource groups need isolated from each other.
Pass an explicit :class:`ProtocolEngine` when you do want isolation or
deterministic teardown::

    with execnet.ProtocolEngine() as engine:
        group = execnet.Group(engine=engine)
        ...
    # the thread is joined here, rather than at interpreter exit

This module stays free of ``import trio`` so ``import execnet`` does not
load the event loop machinery: the real
:class:`~execnet._trio_engine.TrioEngine` is built on first use.
"""

from __future__ import annotations

import atexit
import os
import sys
import threading
import warnings
from contextlib import suppress
from types import TracebackType
from typing import TYPE_CHECKING
from typing import Any

from ._errors import ActiveGroupsWarning
from ._errors import forked_error

if TYPE_CHECKING:
    from typing_extensions import Self

__all__ = ["ProtocolEngine", "check_not_in_event_loop", "default_engine"]

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


class ProtocolEngine:
    """An event loop on a dedicated thread, shared by gateway groups.

    Constructing one costs nothing.  The thread comes up when a group first
    needs it, or when you ask with :meth:`start` -- which is where an
    application that would rather not discover a broken environment at its
    first ``makegateway()`` should ask.
    """

    def __init__(
        self,
        name: str = "execnet-engine",
        callback_threads: int = DEFAULT_CALLBACK_THREADS,
    ) -> None:
        self.name = name
        self.callback_threads = callback_threads
        self._lock = threading.Lock()
        self._trio_engine: Any = None
        self._closed = False
        #: pid the loop thread was started in; a fork does not copy it
        self._pid: int | None = None

    def __repr__(self) -> str:
        return f"<execnet.ProtocolEngine {self.name!r} {self._state()}>"

    def _state(self) -> str:
        if self._trio_engine is None:
            return "closed" if self._closed else "idle"
        return "running" if self._pid == os.getpid() else "inherited"

    @property
    def running(self) -> bool:
        """Whether this engine has a loop thread *in this process*."""
        return self._state() == "running"

    def start(self) -> Self:
        """Bring the loop thread up now, and return this engine.

        Starting is otherwise lazy -- the thread appears when a group first
        needs it -- which means everything that can go wrong with starting
        one goes wrong at an arbitrary later ``makegateway()``: a
        monkey-patched gevent process, a thread that cannot be created, a
        loop that does not come up within 30s.  Call this where you want to
        find out, typically once at application startup::

            engine = execnet.ProtocolEngine().start()

        Idempotent, and entering a ``ProtocolEngine`` as a context manager
        does it for you.
        """
        self._ensure_started()
        return self

    def _ensure_started(self) -> Any:
        """The started :class:`~execnet._trio_engine.TrioEngine` (internal)."""
        with self._lock:
            if self._closed:
                raise RuntimeError(
                    f"{self!r} was closed: the loop thread is gone, and with it"
                    " every gateway and channel it served. Closing is final --"
                    " build a new ProtocolEngine (and a new Group on it)"
                    " instead of reusing this one."
                )
            if self._trio_engine is not None and self._pid != os.getpid():
                # Recovery after a fork is the child's to make explicitly:
                # silently starting a second loop here would hand back an
                # engine that none of the inherited gateways are attached to.
                raise forked_error(f"{self!r}", self._pid)  # type: ignore[arg-type]
            if self._trio_engine is None:
                from . import _trio_engine

                trio_engine = _trio_engine.TrioEngine(
                    name=self.name, callback_threads=self.callback_threads
                )
                trio_engine.start()
                self._pid = os.getpid()
                self._trio_engine = trio_engine
            return self._trio_engine

    def terminate(self, timeout: float | None = None) -> None:
        """Terminate every group this engine serves; the loop stays up.

        Each group's own bounded contract applies -- termination frame,
        grace period, then kill -- and the groups go concurrently, so this
        takes about ``timeout`` however many there are.  A no-op on an
        engine with no loop thread: there is nothing running to terminate.

        This is the half of :meth:`close` you can call while there is still
        somewhere to report a stuck worker to.  Afterwards the engine is
        still usable, and new groups can be built on it.
        """
        trio_engine = self._trio_engine
        if trio_engine is None or not self.running:
            return
        self._check_not_engine_thread("terminate()")
        trio_engine.call(trio_engine.terminate_groups, timeout)

    def close(self, timeout: float | None = 5.0) -> None:
        """Terminate what is still running, then stop the loop and join.

        Groups still live at close time are terminated for you and warned
        about (:class:`~execnet.ActiveGroupsWarning`) -- their
        workers are real processes, and leaving them behind because the
        loop went away is never what anybody wanted.  Doing it yourself is
        still better: see :meth:`terminate`.

        What closing cannot do is keep those groups usable.  Their protocol
        IO no longer has a loop to run on, so they, their gateways and their
        channels are finished with it.  Closing is final -- for an engine
        that never started, too -- so a group whose engine went away fails
        loudly instead of quietly resurrecting a second loop thread that
        none of its gateways are attached to.

        ``timeout`` bounds both halves: the termination grace, and then the
        join.  A thread that does not join is warned about rather than
        passed over in silence.
        """
        trio_engine = self._trio_engine
        if trio_engine is not None and self.running:
            self._check_not_engine_thread("close()")
            live = trio_engine.live_groups()
            if live:
                warnings.warn(
                    f"{self!r} was closed with {live} still running: closing"
                    " terminates them, because the alternative is leaving"
                    " their worker processes behind. Terminate the groups"
                    " (or the engine) while you can still act on the result.",
                    ActiveGroupsWarning,
                    stacklevel=2,
                )
                with suppress(Exception):
                    trio_engine.call(trio_engine.terminate_groups, timeout)
        with self._lock:
            trio_engine, self._trio_engine = self._trio_engine, None
            self._closed = True
        if trio_engine is not None and not trio_engine.stop(timeout=timeout):
            warnings.warn(
                f"{self!r} did not stop within {timeout}s: its thread is still"
                " running, and whatever wedged the loop is still holding it.",
                ActiveGroupsWarning,
                stacklevel=2,
            )

    def _check_not_engine_thread(self, what: str) -> None:
        """Refuse an operation that would have the loop wait for itself."""
        trio_engine = self._trio_engine
        if trio_engine is not None and trio_engine._on_engine_thread():
            raise RuntimeError(
                f"{what} was called from {self!r}'s own loop thread, where it"
                " would wait for that loop to finish work it is itself"
                " running. Call it from the thread that owns the engine."
            )

    def __enter__(self) -> Self:
        # entering acquires: the block ends by closing the loop thread, so
        # it should begin by having one -- and by having failed here if it
        # cannot be had
        return self.start()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()


_default_lock = threading.Lock()
_default: ProtocolEngine | None = None
#: the pid _default was created in; a forked child inherits a dead thread
_default_pid: int | None = None


def default_engine() -> ProtocolEngine:
    """The process-wide engine, started lazily, stopped at interpreter exit.

    After ``os.fork()`` the child inherits an engine whose thread does not
    exist there, so a child asking for the default engine gets a fresh one
    and can build new groups on it.  What it does *not* get is the
    inherited one working again: everything already attached to that engine
    -- the pre-fork groups, gateways and channels, including the
    module-level ``execnet.makegateway`` group -- stays dead in the child
    and says so (:class:`~execnet._errors.ForkedResourceError`).
    """
    global _default, _default_pid
    with _default_lock:
        pid = os.getpid()
        if _default is None or _default_pid != pid:
            _default = ProtocolEngine()
            _default_pid = pid
        return _default


def _close_default_atexit() -> None:
    engine = _default
    if engine is not None and _default_pid == os.getpid():
        # Terminate first, and quietly: a group that is still running here
        # has already outlived every ``atexit`` handler that could have
        # dealt with it, and a warning emitted this late may not be
        # displayed at all.  Reaping its workers still matters.
        with suppress(Exception):
            engine.terminate(timeout=1.0)
        engine.close(timeout=1.0)


atexit.register(_close_default_atexit)
