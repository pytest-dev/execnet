"""The asyncio-native execnet API.

Everything here is awaited inside your own asyncio event loop::

    import asyncio
    import execnet.aio

    async def main():
        async with execnet.aio.AsyncGroup() as group:
            gateway = await group.makegateway("popen")
            channel = await gateway.remote_exec("channel.send(6 * 7)")
            print(await channel.receive())

    asyncio.run(main())

Protocol IO keeps running on a ProtocolEngine (the same engine as the
blocking and trio-native APIs, all transports included); each awaited
operation runs as a task on that engine and resolves an asyncio future via
``loop.call_soon_threadsafe``.  No anyio port and no executor threads per
call.

Cancellation crosses the bridge.  Cancelling an awaited ``receive`` (say
by ``asyncio.timeout``) cancels the engine-side operation too, so normally
no item is consumed and dropped -- but the cancel can also land in the
window after the engine took an item and before it reaches you, and that
item is then lost.  Do not cancel a ``receive`` whose item you still need;
the roadmap tracks closing the window.  Operations that must not tear
halfway -- ``send``, ``send_eof``, ``aclose``, ``terminate`` -- are
shielded instead: the ``CancelledError`` reaches you, but the operation
still completes on the engine.

The error types are shared with :mod:`execnet.sync` and
:mod:`execnet.trio`.  Items you send must already be simple builtin data
(plus channels); the standalone serializer is intentionally not part of
the public API -- ``execnet.can_send`` checks a value before you send it;
see ``DumpError``.
"""

from __future__ import annotations

import functools
import types
from collections.abc import AsyncIterator
from collections.abc import Callable
from collections.abc import Sequence
from contextlib import asynccontextmanager
from contextlib import suppress
from typing import TYPE_CHECKING
from typing import Any
from typing import TypeVar

import trio

from ._bridge import AsyncioBridge
from ._bridge import AsyncioCarrier
from ._bridge import start_engine
from ._deploy import Deployed
from ._deploy import Deployment
from ._engine import ProtocolEngine
from ._engine import default_engine
from ._errors import ActiveGroupsWarning
from ._errors import DataFormatError
from ._errors import DumpError
from ._errors import HostNotFound
from ._errors import LoadError
from ._errors import RemoteError
from ._errors import TimeoutError
from ._trio_gateway import AsyncChannel as _TrioChannel
from ._trio_gateway import AsyncGateway as _TrioGateway
from ._trio_gateway import AsyncGroup as _TrioGroup
from ._xspec import XSpec

if TYPE_CHECKING:
    from typing_extensions import Self

__all__ = [
    "ActiveGroupsWarning",
    "AsyncChannel",
    "AsyncGateway",
    "AsyncGroup",
    "DataFormatError",
    "Deployed",
    "Deployment",
    "DumpError",
    "HostNotFound",
    "LoadError",
    "ProtocolEngine",
    "RemoteError",
    "TimeoutError",
    "XSpec",
    "deploy",
    "deploy_all",
    "open_gateway",
    "transfer",
]

T = TypeVar("T")


class _EngineGroup(_TrioGroup):
    """Trio AsyncGroup living as a task on the engine nursery."""

    def __init__(self, termination_timeout: float, engine: Any) -> None:
        super().__init__(termination_timeout)
        self.engine = engine
        self.shutdown = trio.Event()
        self.finished = trio.Event()

    async def run(self, task_status: trio.TaskStatus[_EngineGroup]) -> None:
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


class AsyncChannel:
    """asyncio facade over a trio-native channel."""

    RemoteError = RemoteError
    TimeoutError = TimeoutError

    def __init__(self, bridge: AsyncioBridge, channel: _TrioChannel) -> None:
        self._bridge = bridge
        self._channel = channel

    @property
    def id(self) -> int:
        return self._channel.id

    def __repr__(self) -> str:
        return f"<aio.AsyncChannel id={self._channel.id}>"

    def isclosed(self) -> bool:
        """Return True if the channel is closed for sending."""
        return self._channel.isclosed()

    async def send(self, item: object) -> None:
        """Serialize ``item`` and send it to the other side.

        Shielded: cancelling raises in the caller but the item is still
        sent, rather than leaving a half-written frame on the wire.
        """
        await self._bridge.call(self._channel.send, item, shield=True)

    async def receive(self, timeout: float | None = None) -> Any:
        """Receive the next item sent from the other side.

        EOFError once the peer closed or sent EOF, RemoteError for a peer
        close-with-error, TimeoutError after ``timeout`` seconds.  A
        received channel reference arrives as an
        :class:`~execnet.aio.AsyncChannel`.

        Cancellable: the engine-side receive is cancelled too, so
        ``asyncio.timeout`` is nearly equivalent to passing ``timeout``.
        The exception is a cancel that arrives once the item is already in
        flight to this coroutine -- it is dropped rather than put back.
        """
        result = await self._bridge.call(self._channel.receive, timeout)
        if isinstance(result, _TrioChannel):
            return AsyncChannel(self._bridge, result)
        return result

    async def send_eof(self) -> None:
        """Signal that no more items follow (peer keeps its send side)."""
        await self._bridge.call(self._channel.send_eof, shield=True)

    async def aclose(self, error: str | None = None) -> None:
        """Close the channel; ``error`` reaches the peer as a RemoteError."""
        await self._bridge.call(self._channel.aclose, error, shield=True)

    async def wait_closed(self) -> None:
        """Wait until the peer closed or sent EOF; reraise remote errors."""
        await self._bridge.call(self._channel.wait_closed)

    def __aiter__(self) -> AsyncChannel:
        return self

    async def __anext__(self) -> Any:
        try:
            return await self.receive()
        except EOFError:
            raise StopAsyncIteration from None


class AsyncGateway:
    """asyncio facade over a trio-native gateway."""

    def __init__(self, bridge: AsyncioBridge, gateway: _TrioGateway) -> None:
        self._bridge = bridge
        self._gateway = gateway

    @property
    def id(self) -> str:
        return self._gateway.id

    @property
    def remoteaddress(self) -> str | None:
        return self._gateway.remoteaddress

    def __repr__(self) -> str:
        return f"<aio.AsyncGateway id={self._gateway.id!r}>"

    async def remote_exec(
        self,
        source: str | types.FunctionType | Callable[..., object] | types.ModuleType,
        **kwargs: object,
    ) -> AsyncChannel:
        """Connect a new channel to remote execution of ``source``.

        Accepts the same source kinds as ``Gateway.remote_exec``: a source
        string, a pure function called with ``channel`` and ``**kwargs``,
        or a module.
        """
        channel = await self._bridge.call(
            functools.partial(self._gateway.remote_exec, source, **kwargs)
        )
        return AsyncChannel(self._bridge, channel)

    async def terminate(self) -> None:
        """Send GATEWAY_TERMINATE to the peer, then close this side."""
        await self._bridge.call(self._gateway.terminate, shield=True)

    def _target(self) -> Any:
        """This gateway as a service target (transfers, deployments)."""
        from ._services import ServiceTarget

        return ServiceTarget(self._gateway)


class AsyncGroup:
    """asyncio-native gateway group served on a ProtocolEngine.

    Usable as an async context manager, or driven explicitly with
    :meth:`start` / :meth:`aclose` from application lifespan hooks.
    Either way, shutting down terminates every gateway with the same
    bounded contract as :class:`execnet.trio.AsyncGroup`.
    """

    def __init__(
        self,
        termination_timeout: float = 10.0,
        *,
        engine: ProtocolEngine | None = None,
    ) -> None:
        self._termination_timeout = termination_timeout
        self._engine = default_engine() if engine is None else engine
        self._bridge: AsyncioBridge | None = None
        self._group: _EngineGroup | None = None

    def __repr__(self) -> str:
        state = "running" if self._group is not None else "idle"
        return f"<aio.AsyncGroup {state}>"

    @property
    def engine(self) -> ProtocolEngine:
        """The :class:`~execnet.ProtocolEngine` this group's IO runs on."""
        return self._engine

    async def start(self) -> None:
        """Bring the engine up and start the group task on it."""
        if self._group is not None:
            raise RuntimeError(f"{self!r} is already started")
        trio_engine = await start_engine(self._engine, AsyncioCarrier())
        bridge = AsyncioBridge(trio_engine)

        async def start_group() -> _EngineGroup:
            # runs on the engine loop
            group = _EngineGroup(self._termination_timeout, trio_engine)
            started: _EngineGroup = await trio_engine.start_task(group.run)
            return started

        self._bridge = bridge
        self._group = await bridge.call(start_group, shield=True)

    async def aclose(self) -> None:
        """Terminate every gateway and stop the group task (idempotent).

        The engine thread is shared, so it keeps running for other groups.
        """
        group, bridge = self._group, self._bridge
        self._group = self._bridge = None
        if group is None or bridge is None:
            return

        async def stop_group() -> None:
            group.shutdown.set()
            await group.finished.wait()

        with suppress(RuntimeError):
            await bridge.call(stop_group, shield=True)

    async def __aenter__(self) -> Self:
        await self.start()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    async def makegateway(self, spec: str | XSpec = "popen") -> AsyncGateway:
        """Create a gateway for ``spec`` served on the group's engine.

        All transports are supported: popen (including uv-provisioned
        ``python=``), ``ssh=``, ``vagrant_ssh=``, ``socket=`` (with
        ``installvia=``), and ``via=`` sub-gateways.  The worker profile
        defaults to ``thread``; pass ``profile=trio`` for a worker that
        runs exec'd async sources as tasks.
        """
        group, bridge = self._group, self._bridge
        if group is None or bridge is None:
            raise RuntimeError(f"{self!r} is not started")
        gateway = await bridge.call(group.makegateway, spec)
        return AsyncGateway(bridge, gateway)


async def transfer(
    gateway: AsyncGateway,
    source: str | Any,
    destination: str,
    **options: Any,
) -> None:
    """Copy a tree to ``destination`` on ``gateway``; see :mod:`execnet.trio`."""
    from ._deploy import _async_api

    await gateway._bridge.call(
        functools.partial(
            _async_api.transfer, gateway._target(), source, destination, **options
        )
    )


async def deploy(deployment: Deployment, gateway: AsyncGateway) -> Deployed:
    """Deploy through ``gateway`` and return where everything landed."""
    results = await deploy_all(deployment, [gateway])
    return results[0]


async def deploy_all(
    deployment: Deployment, gateways: Sequence[AsyncGateway]
) -> list[Deployed]:
    """Deploy to every gateway at once, concurrently on the engine."""
    from ._deploy import _async_api

    if not gateways:
        raise ValueError("no gateways to deploy to")
    bridge = gateways[0]._bridge
    targets = [gateway._target() for gateway in gateways]
    return await bridge.call(
        functools.partial(_async_api.deploy_all, deployment, targets)
    )


@asynccontextmanager
async def open_gateway(spec: str | XSpec = "popen") -> AsyncIterator[AsyncGateway]:
    """Spawn one worker for ``spec`` and yield an asyncio gateway to it.

    Convenience for a single-gateway :class:`AsyncGroup`.
    """
    async with AsyncGroup() as group:
        yield await group.makegateway(spec)
