"""The engine loop itself: one OS thread running ``trio.run``.

:class:`~execnet._engine.ProtocolEngine` is the public handle; this is what
it starts.  Kept apart from the routing layer in
:mod:`execnet._trio_host` so the loop and the things that run *on* it are
not one 1100-line module -- and so the loop's own interface stays small
enough to see: start it, run something on it, stop it.

Everything a caller needs from the loop goes through :meth:`TrioEngine.call`,
:meth:`~TrioEngine._call_pending`, :meth:`~TrioEngine.start_soon` or
:meth:`~TrioEngine.start_task`.  Nothing outside this module reaches for the
nursery: a task started anywhere else would be a task the engine cannot
account for at shutdown.
"""

from __future__ import annotations

import threading
from collections.abc import Awaitable
from collections.abc import Callable
from typing import Any
from typing import TypeVar

import trio

from ._boundary import WaitBackend
from ._boundary import Wakener
from ._engine import DEFAULT_CALLBACK_THREADS
from ._engine import gevent_patched_modules
from ._portal import LoopPortal
from ._portal import OneShot

T = TypeVar("T")


def _check_gevent_not_patched() -> None:
    """Refuse to start a engine loop in a monkey-patched process.

    Every patching variant we measured is broken, and each fails somewhere
    inside trio with an error that says nothing about gevent:
    ``patch_all()`` removes ``select.epoll`` so the IO manager cannot be
    built, ``patch_all(select=False)`` makes trio's wakeup socketpair a
    gevent socket (``EBADF``), and patching neither still leaves
    ``queue.SimpleQueue`` gevent's, so ``from_thread.run`` raises
    ``LoopExit``.  Refusing here costs a dict lookup and turns all three
    into one sentence, before a thread exists to fail on.

    Note this is not what makes :mod:`execnet.gevent` work: that surface's
    waits park the calling greenlet because they wait on a gevent
    primitive, not because the stdlib was swapped underneath them.  It
    works in an unpatched process and is the supported way to drive execnet
    from a gevent application.
    """
    patched = gevent_patched_modules()
    if not patched:
        return
    raise RuntimeError(
        "the execnet engine loop cannot run in this process: gevent has"
        f" monkey-patched {', '.join(patched)}, and the loop needs the real"
        " ones (it is a trio program on its own OS thread). execnet supports"
        " gevent applications that do not monkey-patch these modules --"
        " execnet.gevent parks the calling greenlet on its blocking waits"
        " either way, which is what that namespace is for."
    )


def _startup_hint() -> str:
    """Name gevent when patching is what kept the loop from starting.

    :func:`_check_gevent_not_patched` catches this before the thread
    starts; this stays for a process that patches *after* that check, and
    for whatever else ``gevent.monkey`` grows next.
    """
    patched = gevent_patched_modules()
    if not patched:
        return ""
    return (
        f" -- gevent has monkey-patched {', '.join(patched)}, and the engine loop"
        " needs the real ones. execnet.gevent supports a process that uses"
        " gevent without monkey-patching these modules; its blocking waits"
        " park the calling greenlet either way."
    )


class TrioEngine:
    """Dedicated OS thread running ``trio.run`` for protocol IO."""

    #: which async library this engine's loop is; see ``ProtocolEngine``
    backend = "trio"

    def __init__(
        self,
        name: str = "execnet-trio-engine",
        callback_threads: int = DEFAULT_CALLBACK_THREADS,
    ) -> None:
        self._name = name
        self._callback_threads = callback_threads
        self._thread: threading.Thread | None = None
        self._portal: LoopPortal | None = None
        self._nursery: trio.Nursery | None = None
        self._ready = threading.Event()
        self._shutdown: trio.Event | None = None
        self._started = False
        self._callback_limiter: trio.CapacityLimiter | None = None
        self._startup_error: BaseException | None = None
        #: engine-side groups currently running here, in start order.  Only
        #: touched from the engine thread (a group registers itself as its
        #: task starts and drops out as it ends), so it needs no lock.
        self._groups: list[Any] = []

    def start(self) -> None:
        if self._started:
            return
        _check_gevent_not_patched()
        self._thread = threading.Thread(target=self._run, name=self._name, daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout=30):
            raise RuntimeError("TrioEngine failed to start within 30s")
        error = self._startup_error
        if error is not None:
            raise RuntimeError(
                f"the execnet engine loop could not start: {error!r}{_startup_hint()}"
            ) from error
        self._started = True

    @property
    def portal(self) -> LoopPortal:
        if self._portal is None:
            raise RuntimeError("TrioEngine is not running")
        return self._portal

    @property
    def _limiter(self) -> trio.CapacityLimiter:
        """Bound on concurrent threadpool threads running receiver callbacks."""
        if self._callback_limiter is None:
            raise RuntimeError("TrioEngine is not running")
        return self._callback_limiter

    def _on_engine_thread(self) -> bool:
        return self._portal is not None and self._portal.is_loop_thread()

    def _run(self) -> None:
        try:
            trio.run(self._main)
        except BaseException as exc:
            if self._ready.is_set():
                # the loop was up and died later: nobody is waiting on us,
                # so let the thread report it the loud way
                raise
            # start() is blocked on _ready and would otherwise wait out the
            # full timeout and raise something generic, with the actual
            # reason only on stderr
            self._startup_error = exc
            self._ready.set()

    async def _main(self) -> None:
        self._portal = LoopPortal()
        self._shutdown = trio.Event()
        self._callback_limiter = trio.CapacityLimiter(self._callback_threads)
        try:
            async with trio.open_nursery() as nursery:
                self._nursery = nursery
                self._ready.set()
                await self._shutdown.wait()
                nursery.cancel_scope.cancel()
        finally:
            self._nursery = None

    def call(self, async_fn: Callable[..., Awaitable[T]], *args: Any) -> T:
        return self.portal.run(async_fn, *args)

    def _call_pending(
        self,
        async_fn: Callable[..., Awaitable[T]],
        *args: Any,
        wakener: Wakener | None = None,
    ) -> OneShot[T]:
        """Run ``async_fn`` as an engine task, resolving a :class:`OneShot`.

        The non-blocking counterpart of :meth:`call` for consumers that
        must not block their OS thread (a gevent hub: waiting on the
        OneShot with a gevent wakener parks only the calling greenlet).
        Unlike ``portal.run`` the wait is KeyboardInterrupt-interruptible.
        """
        result: OneShot[T] = OneShot(wakener)

        async def runner() -> None:
            try:
                value = await async_fn(*args)
            except trio.Cancelled:
                if not result.is_set():
                    result.set_error(RuntimeError("trio engine was shut down"))
                raise
            except BaseException as exc:
                result.set_error(exc)
            else:
                result.set(value)

        def spawn() -> None:
            # Posted callbacks must not raise: trio turns an exception from
            # an entry-queue callback into TrioInternalError and tears the
            # whole loop down, taking every gateway in the process with it.
            # An engine that shut down between the post and here is exactly the
            # failure this call already reports as a value.
            try:
                self.start_soon(runner)
            except BaseException as exc:
                error = RuntimeError("trio engine was shut down")
                error.__cause__ = exc
                if not result.is_set():
                    result.set_error(error)

        self.portal.post(spawn)
        return result

    def call_sync(self, sync_fn: Callable[..., T], *args: Any) -> T:
        return self.portal.run_sync(sync_fn, *args)

    def start_soon(self, async_fn: Callable[..., Any], *args: Any) -> None:
        """Schedule a task on the root nursery (must be called on the engine thread)."""
        if not self._on_engine_thread():
            raise RuntimeError("start_soon requires the Trio engine thread")
        if self._nursery is None:
            raise RuntimeError("TrioEngine nursery is not available")
        self._nursery.start_soon(async_fn, *args)

    async def start_task(self, async_fn: Callable[..., Any], *args: Any) -> Any:
        """``await nursery.start(...)`` on the root nursery, from a task on it.

        The one door to the root nursery, so that a long-lived task -- a
        gateway session, a facade group -- is something the engine knows it
        is running rather than something a caller reached in and attached.
        Returns whatever the task passes to ``task_status.started()``.
        """
        if self._nursery is None:
            raise RuntimeError("TrioEngine nursery is not available")
        return await self._nursery.start(async_fn, *args)

    # -- the groups running here (engine thread only) --

    def _register_group(self, group: Any) -> None:
        self._groups.append(group)

    def _forget_group(self, group: Any) -> None:
        if group in self._groups:
            self._groups.remove(group)

    def live_groups(self) -> str:
        """What is still running here, for a message; ``""`` when nothing is.

        Reads a list the engine thread owns, from whichever thread is
        closing.  Racy by construction and deliberately harmless: the worst
        outcome is naming a gateway that finished terminating a moment ago.
        """
        ids = [
            str(gateway.id)
            for group in list(self._groups)
            for gateway in list(group._gateways)
        ]
        if not ids:
            return ""
        return f"{len(self._groups)} group(s), gateways {', '.join(sorted(ids))}"

    async def terminate_groups(self, timeout: float | None = None) -> None:
        """Terminate every group running here, concurrently (engine loop).

        Each group's own bounded contract applies -- termination frame,
        grace, then kill -- so this ends in roughly ``timeout`` however many
        groups there are, rather than in the sum of them.
        """
        groups = list(self._groups)
        if not groups:
            return
        async with trio.open_nursery() as nursery:
            for group in groups:
                nursery.start_soon(group.terminate, timeout)
        for group in groups:
            # lets each group's run task finish, which is what unregisters it
            group.shutdown.set()

    def stop(self, timeout: float | None = 5.0) -> bool:
        """Cancel the root nursery and join the thread; True if it joined.

        A thread that does not join is a leak worth reporting: the loop is
        still running, and whatever wedged it is still holding it.
        """
        if not self._started or self._portal is None or self._shutdown is None:
            return True

        def _set() -> None:
            assert self._shutdown is not None
            self._shutdown.set()

        try:
            # posted rather than run: ``portal.run_sync`` refuses a caller
            # that is itself inside a trio run ("this is a blocking
            # function"), which is exactly where an async application closes
            # its engine from.  Nothing is lost by not waiting for the
            # callback -- the join below is the real wait.
            self._portal.post(_set)
        except Exception:
            pass
        joined = True
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            joined = not self._thread.is_alive()
        self._started = False
        return joined


def engine_call(
    trio_engine: TrioEngine,
    wait_backend: WaitBackend,
    async_fn: Callable[..., Awaitable[T]],
    *args: Any,
) -> T:
    """Run ``async_fn`` on ``trio_engine``, parking the way ``wait_backend`` does.

    ``thread`` keeps the KI-deferred ``portal.run`` path.  Any other backend
    implies the caller may not own its OS thread -- a gevent hub runs every
    other greenlet on it -- so the work becomes an engine task and the wait
    happens on a ``OneShot`` with that backend's wakener.

    The blocking surfaces all funnel through here: ``Group`` for gateway
    creation and termination, and the deployment layer for transfers.
    """
    if wait_backend == "thread":
        return trio_engine.call(async_fn, *args)
    from ._boundary import make_wakener

    pending = trio_engine._call_pending(
        async_fn, *args, wakener=make_wakener(wait_backend)
    )
    return pending.wait()
