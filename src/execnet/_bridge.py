"""Awaiting engine-side work from a caller's own event loop.

Two of execnet's surfaces are async and do not own the loop their protocol
IO runs on: :mod:`execnet.aio` cannot (its caller is asyncio and the engine
is trio), and :mod:`execnet.trio` chooses not to, so that a busy caller
loop cannot stall the protocol.  Both need the same thing -- run a
trio-native coroutine on the engine, wait for it here, and forward a cancel
in the other direction -- and the engine half of that is identical.  Only
*how the caller waits* differs, which is what a :class:`Carrier` is.

The three things in :meth:`EngineBridge.call` that look incidental and are
not:

* the ``trio.CancelScope`` is built before the task exists.  A cancel
  posted immediately can otherwise overtake the task's first step;
  cancelling a scope nobody has entered yet still cancels it once entered.
* the function posted to the engine's entry queue must not raise.  Trio
  turns an exception there into ``TrioInternalError`` and tears the whole
  loop down -- every gateway in the process, not just this call's.
* a task cancelled by *us* posts nothing back.  The awaiter is already
  gone, and resolving a carrier nobody holds is at best wasted work.

What this cannot make identical is cancellation.  The engine-side
operation is cancelled through the scope, but there is a window after the
engine took an item and before it reaches the caller in which the cancel
lands and the item is lost.  Callers that must not lose it use
``shield=True``, which the two surfaces then honour in their own idiom --
see :class:`AsyncioCarrier` and :class:`TrioCarrier`.
"""

from __future__ import annotations

import threading
from collections.abc import Awaitable
from collections.abc import Callable
from collections.abc import Sequence
from contextlib import suppress
from typing import TYPE_CHECKING
from typing import Any
from typing import TypeVar

import trio

from ._async import current_async
from ._errors import LoopFinishedError
from ._trio_gateway import AsyncGroup as _TrioGroup

if TYPE_CHECKING:
    from ._engine import ProtocolEngine
    from ._trio_engine import TrioEngine

T = TypeVar("T")

#: what a call reports when the engine went away underneath it
ENGINE_GONE = "the execnet engine was shut down"


class EngineGroup(_TrioGroup):
    """A trio-native group owned by the engine rather than by a caller.

    This is the structural difference between a facade and
    :mod:`execnet.raw_trio`.  There the group's nursery is on the caller's
    stack, so its gateways cannot outlive the ``async with`` that made
    them.  Here the group is itself a long-lived task on the engine, parked
    on :attr:`shutdown`, so a gateway is a handle the caller can hold, pass
    around, and close from wherever it likes.
    """

    def __init__(self, termination_timeout: float, engine: TrioEngine) -> None:
        super().__init__(termination_timeout)
        # built on the engine loop, like FacadeAsyncGroup
        self._aio = current_async()
        self.engine = engine
        self.shutdown = self._aio.event()
        self.finished = self._aio.event()

    async def run(self, task_status: trio.TaskStatus[EngineGroup]) -> None:
        # registered for exactly this task's lifetime, so closing the engine
        # knows what it is about to take down
        self.engine._register_group(self)
        try:
            async with self:
                task_status.started(self)
                await self.shutdown.wait()
        finally:
            self.engine._forget_group(self)
            self.finished.set()


class Carrier:
    """One result crossing from the engine to a caller's loop.

    Two halves with different rules.  :meth:`resolve` runs on the engine
    thread: thread-safe, never blocking, and never raising -- it is reached
    from an entry-queue callback.  :meth:`wait` runs on the caller's loop
    and is a normal awaitable there.

    A value that arrives with nobody left to take it is *salvaged* rather
    than dropped, if the call supplied somewhere to put it.  That is what
    keeps a cancelled ``receive`` from eating an item: the engine either
    never took one, or took one that comes back.  Both the salvage decision
    and the delivery run on the caller's loop thread, so they cannot race
    each other however the cancel and the result interleave.
    """

    #: where an unclaimed value goes, when the call named somewhere
    _salvage: Callable[[Any], None] | None = None
    #: set once the awaiter is gone; read by a delivery arriving afterwards
    _abandoned = False

    def set_salvage(self, salvage: Callable[[Any], None] | None) -> None:
        self._salvage = salvage

    def _give_up(self, result: Any, error: BaseException | None) -> None:
        """Hand an unclaimed *value* to the salvage, if there is one.

        An unclaimed *error* is dropped on purpose: it describes the
        operation the caller just abandoned, and the next call will raise
        its own.
        """
        if error is None and self._salvage is not None:
            self._salvage(result)

    def resolve(self, result: Any, error: BaseException | None) -> None:
        """Deliver to the caller's loop (engine thread; must not raise)."""
        raise NotImplementedError

    async def wait(
        self, *, shield: bool, on_cancel: Callable[[], None]
    ) -> Any:  # pragma: no cover - interface
        """Await the result, calling ``on_cancel`` if the caller is cancelled."""
        raise NotImplementedError


class AsyncioCarrier(Carrier):
    """A carrier backed by an ``asyncio.Future``.

    ``shield=True`` is ``asyncio.shield``: the ``CancelledError`` still
    reaches the caller, and the engine-side work runs to completion
    regardless.  That is asyncio's meaning of shielding and it is left
    alone -- see :class:`TrioCarrier` for the other one.
    """

    def __init__(self) -> None:
        import asyncio

        self._asyncio = asyncio
        self._loop = asyncio.get_running_loop()
        self._future: Any = self._loop.create_future()

    def resolve(self, result: Any, error: BaseException | None) -> None:
        def deliver() -> None:
            if self._abandoned or self._future.cancelled():
                self._give_up(result, error)
                return
            if error is not None:
                self._future.set_exception(error)
            else:
                self._future.set_result(result)

        # the caller's loop may already be gone at interpreter/test teardown;
        # the result is simply undeliverable then
        with suppress(RuntimeError):
            self._loop.call_soon_threadsafe(deliver)

    async def wait(self, *, shield: bool, on_cancel: Callable[[], None]) -> Any:
        if shield:
            return await self._asyncio.shield(self._future)
        try:
            return await self._future
        except self._asyncio.CancelledError:
            # the result may already be here (cancelled between delivery and
            # this task being scheduled) or still on its way; mark it either
            # way, so whichever of the two runs second does the salvaging
            self._abandoned = True
            if self._future.done() and not self._future.cancelled():
                self._give_up(self._future.result(), None)
            on_cancel()
            raise


class TrioCarrier(Carrier):
    """A carrier backed by a ``trio.Event`` in the caller's own run.

    ``shield=True`` is a shielded ``trio.CancelScope``, so the wait itself
    becomes uncancellable and the caller stays until the operation is done.
    That differs from :class:`AsyncioCarrier`, deliberately: it is what a
    shield means in trio, and it is the stronger guarantee -- an operation
    that must not tear in half is also one whose completion the caller
    should not run ahead of.
    """

    def __init__(self) -> None:
        self._token = trio.lowlevel.current_trio_token()
        self._done = trio.Event()
        self._result: Any = None
        self._error: BaseException | None = None

    def resolve(self, result: Any, error: BaseException | None) -> None:
        def deliver() -> None:
            # runs on the caller's loop thread, from its entry queue, so it
            # is under the same must-not-raise rule as the engine side
            self._result = result
            self._error = error
            self._done.set()
            if self._abandoned:
                self._give_up(result, error)

        # the caller's run may already be over; nothing to deliver to then
        with suppress(trio.RunFinishedError):
            self._token.run_sync_soon(deliver)

    def _take(self) -> Any:
        if self._error is not None:
            raise self._error
        return self._result

    async def wait(self, *, shield: bool, on_cancel: Callable[[], None]) -> Any:
        if shield:
            with trio.CancelScope(shield=True):
                await self._done.wait()
            return self._take()
        try:
            await self._done.wait()
        except trio.Cancelled:
            # trio delivers the cancel at the checkpoint even when the event
            # is already set, so a result that arrived first is sitting right
            # here unclaimed; one that has not arrived is salvaged by the
            # delivery instead.
            self._abandoned = True
            if self._done.is_set():
                self._give_up(self._result, self._error)
            on_cancel()
            raise
        return self._take()


class EngineBridge:
    """Run trio-native coroutines on an engine, awaited from another loop."""

    #: the caller-side carrier this bridge's surface waits on
    carrier: type[Carrier]

    def __init__(self, trio_engine: TrioEngine) -> None:
        self._engine = trio_engine

    async def call(
        self,
        async_fn: Callable[..., Awaitable[T]],
        *args: Any,
        shield: bool = False,
        salvage: Callable[[Any], None] | None = None,
    ) -> T:
        """Run ``async_fn`` on the engine and await its result.

        Unless ``shield``, cancelling the await cancels the engine-side
        operation too.  That cancel and the engine's work race, so an
        operation that *consumes* something -- a ``receive`` -- passes a
        ``salvage``: a value the engine had already produced goes there
        instead of being dropped, and the caller loses nothing whichever
        way the race went.
        """
        carrier = self.carrier()
        carrier.set_salvage(salvage)
        scope = trio.CancelScope()

        async def runner() -> None:
            try:
                with scope:
                    result = await async_fn(*args)
            except trio.Cancelled:
                # engine shutdown: the nursery cancel must propagate
                carrier.resolve(None, RuntimeError(ENGINE_GONE))
                raise
            except BaseException as exc:
                carrier.resolve(None, exc)
                return
            if scope.cancelled_caught:
                return  # cancelled by us -- the awaiter is already gone
            carrier.resolve(result, None)

        def spawn() -> None:
            try:
                self._engine.start_soon(runner)
            except BaseException as exc:
                error = RuntimeError(ENGINE_GONE)
                error.__cause__ = exc
                carrier.resolve(None, error)

        try:
            self._engine.portal.post(spawn)
        except LoopFinishedError:
            raise RuntimeError("the execnet engine is not running") from None

        def cancel_engine_side() -> None:
            with suppress(LoopFinishedError):
                self._engine.portal.post(scope.cancel)

        result: T = await carrier.wait(shield=shield, on_cancel=cancel_engine_side)
        return result


class AsyncioBridge(EngineBridge):
    carrier = AsyncioCarrier


class TrioBridge(EngineBridge):
    carrier = TrioCarrier


def targets_for_bridge(gateways: Sequence[Any]) -> tuple[EngineBridge, list[Any]]:
    """One bridge and one service target per gateway, or a clear error.

    A fan-out is a single task awaiting every gateway's channels, so they
    all have to belong to one engine's run: a trio task cannot await a
    channel that belongs to another.  The blocking surface has always
    checked this (``execnet._deploy._facade.run_blocking``); the facades
    used to take the first gateway's bridge and hope, which turned a
    two-engine mistake into a cross-run await rather than a sentence.
    """
    if not gateways:
        raise ValueError("no gateways to work on")
    bridges = {id(gateway._bridge): gateway._bridge for gateway in gateways}
    if len(bridges) > 1:
        raise ValueError(
            "all gateways must be served by the same execnet.ProtocolEngine:"
            " one driver task cannot reach channels belonging to another"
            " event loop. Gateways from one AsyncGroup always share an engine."
        )
    bridge: EngineBridge = next(iter(bridges.values()))
    return bridge, [gateway._target() for gateway in gateways]


async def start_engine(engine: ProtocolEngine, carrier: Carrier) -> Any:
    """Start ``engine`` without blocking the caller's loop; return its engine.

    ``ProtocolEngine._ensure_started`` blocks until the loop is ready --
    up to 30 seconds if something is wrong with the environment -- so it
    runs on a throwaway thread whose completion is posted back to the
    caller's loop.  Never on a shared thread pool: those belong to the
    caller's application, and this one waits rather than works.
    """
    if engine.running:
        return engine._ensure_started()

    def start() -> None:
        try:
            carrier.resolve(engine._ensure_started(), None)
        except BaseException as exc:
            carrier.resolve(None, exc)

    threading.Thread(target=start, name="execnet-engine-start", daemon=True).start()
    return await carrier.wait(shield=False, on_cancel=lambda: None)
