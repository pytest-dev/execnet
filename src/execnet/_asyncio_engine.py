"""The engine loop on asyncio, beside the one on trio.

Same contract as :class:`~execnet._trio_engine.TrioEngine` -- a loop on a
thread of its own, a portal into it, one door to its root task scope, and
the groups it is serving -- so that :class:`~execnet.ProtocolEngine` can be
handed either.  What differs is only how the loop is spelled.

**This engine cannot host gateways yet.**  The protocol core
(:mod:`execnet._trio_gateway`) is still trio-native, so a group built on an
asyncio engine is refused with a message saying so rather than failing
somewhere inside trio.  What works today is everything the engine itself
promises: starting and stopping, the portal, tasks on the root scope, and
the group registry.  That is the seam being proved before the core is
ported through it.

Two places where asyncio needs saying out loud:

* ``TaskGroup`` has no ``nursery.start()`` -- no way to start a task and
  wait until it reports itself ready.  :meth:`AsyncioEngine.start_task`
  builds one, with trio's semantics: a failure *before* the task reports
  ready goes to whoever called ``start_task`` and nowhere else, and only a
  failure afterwards reaches the group.
* Cancelling the root scope is spelled by raising out of the ``TaskGroup``
  body rather than by cancelling a scope object.

Requires Python 3.11: ``TaskGroup`` and ``asyncio.timeout`` are the
semantics the core is written against, and emulating them on 3.10 would
mean maintaining a second, worse implementation for a release that reaches
end of life in October 2026.  Older Pythons keep the trio engine.

``mypy`` type-checks this project at its floor, 3.10, where the names this
module is built on do not exist -- hence the ignores on them.  They are the
only ones here, and they go away when the floor reaches 3.11.
"""

from __future__ import annotations

import asyncio
import os
import sys
import threading
from collections.abc import Awaitable
from collections.abc import Callable
from typing import Any
from typing import TypeVar

from ._engine import DEFAULT_CALLBACK_THREADS
from ._errors import LoopFinishedError
from ._portal import AsyncioPortal
from ._portal import OneShot

T = TypeVar("T")

#: the floor for this backend, and why
MIN_PYTHON = (3, 11)


def check_asyncio_available() -> None:
    """Refuse the asyncio engine where its semantics do not exist.

    Before a thread exists to fail on, and naming the fix -- the same shape
    as the gevent refusal in :mod:`execnet._trio_engine`.
    """
    if sys.version_info >= MIN_PYTHON:
        return
    have = ".".join(str(part) for part in sys.version_info[:3])
    want = ".".join(str(part) for part in MIN_PYTHON)
    raise RuntimeError(
        f"the asyncio engine needs Python {want} or newer (this is {have}):"
        " it is written against asyncio.TaskGroup and asyncio.timeout, and"
        " execnet does not carry a backport of them. Use the default trio"
        " engine on this interpreter."
    )


class _Shutdown(BaseException):
    """Raised out of the root TaskGroup body to cancel every child."""


class _TaskStatus:
    """The ``task_status`` object :meth:`AsyncioEngine.start_task` passes in.

    Duck-types trio's, so the same task functions work on both engines: a
    task calls ``started(value)`` once it is ready to be used, and the
    caller of ``start_task`` gets that value back.
    """

    def __init__(self) -> None:
        self._event = asyncio.Event()
        self._value: Any = None
        self._error: BaseException | None = None

    def started(self, value: Any = None) -> None:
        if self._event.is_set():
            raise RuntimeError("task_status.started() called more than once")
        self._value = value
        self._event.set()

    def fail(self, error: BaseException) -> None:
        self._error = error
        self._event.set()

    def is_set(self) -> bool:
        return self._event.is_set()

    async def wait(self) -> Any:
        await self._event.wait()
        if self._error is not None:
            raise self._error
        return self._value


async def _until_ready(
    async_fn: Callable[..., Any], args: tuple[Any, ...], status: _TaskStatus
) -> None:
    """Run ``async_fn``, routing a pre-ready failure to the starter.

    Trio's ``nursery.start()`` hands a failure that happens before
    ``started()`` to the caller of ``start()`` and does *not* also fail the
    nursery.  A ``TaskGroup`` child cannot be un-enrolled, so the failure is
    caught here instead and the task ends quietly, leaving the starter to
    raise it.
    """
    try:
        await async_fn(*args, task_status=status)
    except BaseException as exc:
        if not status.is_set():
            status.fail(exc)
            return
        raise
    if not status.is_set():
        status.fail(
            RuntimeError(f"{async_fn!r} ended without calling task_status.started()")
        )


class AsyncioEngine:
    """Dedicated OS thread running ``asyncio.run`` for protocol IO."""

    #: which async library this engine's loop is; see ``ProtocolEngine``
    backend = "asyncio"

    def __init__(
        self,
        name: str = "execnet-asyncio-engine",
        callback_threads: int = DEFAULT_CALLBACK_THREADS,
    ) -> None:
        check_asyncio_available()
        self._name = name
        self._callback_threads = callback_threads
        self._thread: threading.Thread | None = None
        self._portal: AsyncioPortal | None = None
        self._taskgroup: Any = None
        self._ready = threading.Event()
        self._shutdown: asyncio.Event | None = None
        self._started = False
        self._callback_limiter: asyncio.Semaphore | None = None
        self._startup_error: BaseException | None = None
        #: engine-side groups running here; engine thread only, so no lock
        self._groups: list[Any] = []

    def start(self) -> None:
        if self._started:
            return
        self._thread = threading.Thread(target=self._run, name=self._name, daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout=30):
            raise RuntimeError("AsyncioEngine failed to start within 30s")
        error = self._startup_error
        if error is not None:
            raise RuntimeError(
                f"the execnet engine loop could not start: {error!r}"
            ) from error
        self._started = True

    @property
    def portal(self) -> AsyncioPortal:
        if self._portal is None:
            raise RuntimeError("AsyncioEngine is not running")
        return self._portal

    @property
    def _limiter(self) -> asyncio.Semaphore:
        """Bound on concurrent threadpool threads running receiver callbacks."""
        if self._callback_limiter is None:
            raise RuntimeError("AsyncioEngine is not running")
        return self._callback_limiter

    def _on_engine_thread(self) -> bool:
        return self._portal is not None and self._portal.is_loop_thread()

    def _run(self) -> None:
        try:
            asyncio.run(self._main())
        except BaseException as exc:
            if self._ready.is_set():
                # the loop was up and died later: nobody is waiting on us,
                # so let the thread report it the loud way
                raise
            self._startup_error = exc
            self._ready.set()

    async def _main(self) -> None:
        self._portal = AsyncioPortal()
        self._shutdown = asyncio.Event()
        self._callback_limiter = asyncio.Semaphore(self._callback_threads)
        try:
            try:
                async with asyncio.TaskGroup() as taskgroup:  # type: ignore[attr-defined]
                    self._taskgroup = taskgroup
                    self._ready.set()
                    await self._shutdown.wait()
                    # the asyncio spelling of nursery.cancel_scope.cancel()
                    raise _Shutdown
            except BaseExceptionGroup as group:  # type: ignore[name-defined]
                # split by type, not subgroup(predicate): a predicate is
                # offered the *group* as well as its leaves, so
                # ``not isinstance(exc, _Shutdown)`` matches the group
                # itself and keeps everything.
                _, remaining = group.split(_Shutdown)
                if remaining is not None:
                    raise remaining from None
        finally:
            self._taskgroup = None

    def call(self, async_fn: Callable[..., Awaitable[T]], *args: Any) -> T:
        return self.portal.run(async_fn, *args)

    def _call_pending(
        self,
        async_fn: Callable[..., Awaitable[T]],
        *args: Any,
        wakener: Any = None,
    ) -> OneShot[T]:
        """Run ``async_fn`` as an engine task, resolving a :class:`OneShot`."""
        result: OneShot[T] = OneShot(wakener)

        async def runner() -> None:
            try:
                value = await async_fn(*args)
            except asyncio.CancelledError:
                if not result.is_set():
                    result.set_error(RuntimeError("asyncio engine was shut down"))
                raise
            except BaseException as exc:
                result.set_error(exc)
            else:
                result.set(value)

        def spawn() -> None:
            # A posted callback that raises reaches the loop's exception
            # handler, which only logs -- so the waiter would hang.  An
            # engine that shut down between the post and here is exactly the
            # failure this call already reports as a value.
            try:
                self.start_soon(runner)
            except BaseException as exc:
                error = RuntimeError("asyncio engine was shut down")
                error.__cause__ = exc
                if not result.is_set():
                    result.set_error(error)

        self.portal.post(spawn)
        return result

    def call_sync(self, sync_fn: Callable[..., T], *args: Any) -> T:
        return self.portal.run_sync(sync_fn, *args)

    def start_soon(self, async_fn: Callable[..., Any], *args: Any) -> None:
        """Schedule a task on the root scope (must be called on the engine thread)."""
        if not self._on_engine_thread():
            raise RuntimeError("start_soon requires the asyncio engine thread")
        if self._taskgroup is None:
            raise RuntimeError("AsyncioEngine task group is not available")
        self._taskgroup.create_task(async_fn(*args))

    async def start_task(self, async_fn: Callable[..., Any], *args: Any) -> Any:
        """Start ``async_fn`` on the root scope and wait until it is ready.

        The one door to the root scope, so that a long-lived task is
        something the engine knows it is running.  Returns whatever the task
        passes to ``task_status.started()``.
        """
        if self._taskgroup is None:
            raise RuntimeError("AsyncioEngine task group is not available")
        status = _TaskStatus()
        self._taskgroup.create_task(_until_ready(async_fn, args, status))
        return await status.wait()

    # -- the groups running here (engine thread only) --

    def _register_group(self, group: Any) -> None:
        self._groups.append(group)

    def _forget_group(self, group: Any) -> None:
        if group in self._groups:
            self._groups.remove(group)

    def live_groups(self) -> str:
        """What is still running here, for a message; ``""`` when nothing is."""
        ids = [
            str(gateway.id)
            for group in list(self._groups)
            for gateway in list(group._gateways)
        ]
        if not ids:
            return ""
        return f"{len(self._groups)} group(s), gateways {', '.join(sorted(ids))}"

    async def terminate_groups(self, timeout: float | None = None) -> None:
        """Terminate every group running here, concurrently (engine loop)."""
        groups = list(self._groups)
        if not groups:
            return
        async with asyncio.TaskGroup() as taskgroup:  # type: ignore[attr-defined]
            for group in groups:
                taskgroup.create_task(group.terminate(timeout))
        for group in groups:
            group.shutdown.set()

    def stop(self, timeout: float | None = 5.0) -> bool:
        """Cancel the root scope and join the thread; True if it joined."""
        if not self._started or self._portal is None or self._shutdown is None:
            return True

        def _set() -> None:
            assert self._shutdown is not None
            self._shutdown.set()

        try:
            # posted rather than run, for the same reason the trio engine
            # posts: a caller inside its own running loop cannot block on
            # this one, and the join below is the real wait anyway
            self._portal.post(_set)
        except (LoopFinishedError, RuntimeError):
            pass
        joined = True
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            joined = not self._thread.is_alive()
        self._started = False
        return joined
