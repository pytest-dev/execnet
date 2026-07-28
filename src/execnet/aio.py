"""The asyncio-native execnet API, bridged over a Trio host thread.

Everything here is awaited inside your own asyncio event loop::

    import asyncio
    import execnet.aio

    async def main():
        async with execnet.aio.Group() as group:
            gateway = await group.makegateway("popen")
            channel = await gateway.remote_exec("channel.send(6 * 7)")
            print(await channel.receive())

    asyncio.run(main())

Protocol IO keeps running on a dedicated Trio host thread (the same
engine as the blocking and trio-native APIs, all transports included);
each awaited operation runs as a task on that host and resolves an
asyncio future via ``loop.call_soon_threadsafe``.  No anyio port and no
executor threads per call.

Note: cancelling a bridged await abandons the operation on the asyncio
side only -- the host-side task runs to completion (a cancelled
``receive`` may still consume the next item).

The error types are shared with :mod:`execnet.sync` and
:mod:`execnet.trio`.  Items you send must already be simple builtin data
(plus channels); the standalone serializer is intentionally not part of
the public API -- see ``DumpError``.
"""

from __future__ import annotations

import asyncio
import functools
import types
from collections.abc import AsyncIterator
from collections.abc import Awaitable
from collections.abc import Callable
from contextlib import asynccontextmanager
from contextlib import suppress
from typing import TYPE_CHECKING
from typing import Any
from typing import TypeVar

import trio

from ._trio_gateway import AsyncChannel
from ._trio_gateway import AsyncGateway
from ._trio_gateway import AsyncGroup
from ._trio_host import TrioHost
from .gateway_base import DataFormatError
from .gateway_base import DumpError
from .gateway_base import HostNotFound
from .gateway_base import LoadError
from .gateway_base import RemoteError
from .gateway_base import TimeoutError
from .xspec import XSpec

if TYPE_CHECKING:
    from typing_extensions import Self

__all__ = [
    "Channel",
    "DataFormatError",
    "DumpError",
    "Gateway",
    "Group",
    "HostNotFound",
    "LoadError",
    "RemoteError",
    "TimeoutError",
    "XSpec",
    "open_popen_gateway",
]

T = TypeVar("T")


class _HostBridge:
    """Await trio-native coroutines on a TrioHost from asyncio."""

    def __init__(self, host: TrioHost) -> None:
        self._host = host

    async def call(self, async_fn: Callable[..., Awaitable[T]], *args: Any) -> T:
        loop = asyncio.get_running_loop()
        future: asyncio.Future[T] = loop.create_future()

        def resolve(result: Any, error: BaseException | None) -> None:
            if future.cancelled():
                return
            if error is not None:
                future.set_exception(error)
            else:
                future.set_result(result)

        def post_result(result: Any, error: BaseException | None) -> None:
            # The asyncio loop may already be gone at interpreter/test
            # teardown; the result is undeliverable then.
            with suppress(RuntimeError):
                loop.call_soon_threadsafe(resolve, result, error)

        async def runner() -> None:
            try:
                result = await async_fn(*args)
            except trio.Cancelled:
                # host shutdown: the nursery cancel must propagate
                post_result(None, RuntimeError("execnet aio host was shut down"))
                raise
            except BaseException as exc:
                post_result(None, exc)
            else:
                post_result(result, None)

        def spawn() -> None:
            self._host.start_soon(runner)

        try:
            self._host.portal.post(spawn)
        except trio.RunFinishedError:
            raise RuntimeError("execnet aio group is not running") from None
        return await future


class _HostedGroup(AsyncGroup):
    """Trio AsyncGroup living as a task on the host nursery."""

    def __init__(self, termination_timeout: float) -> None:
        super().__init__(termination_timeout)
        self.shutdown = trio.Event()
        self.finished = trio.Event()

    async def run(self, task_status: trio.TaskStatus[_HostedGroup]) -> None:
        try:
            async with self:
                task_status.started(self)
                await self.shutdown.wait()
        finally:
            self.finished.set()


class Channel:
    """asyncio facade over a trio-native :class:`AsyncChannel`."""

    RemoteError = RemoteError
    TimeoutError = TimeoutError

    def __init__(self, bridge: _HostBridge, channel: AsyncChannel) -> None:
        self._bridge = bridge
        self._channel = channel

    @property
    def id(self) -> int:
        return self._channel.id

    def __repr__(self) -> str:
        return f"<aio.Channel id={self._channel.id}>"

    def isclosed(self) -> bool:
        """Return True if the channel is closed for sending."""
        return self._channel.isclosed()

    async def send(self, item: object) -> None:
        """Serialize ``item`` and send it to the other side."""
        await self._bridge.call(self._channel.send, item)

    async def receive(self, timeout: float | None = None) -> Any:
        """Receive the next item sent from the other side.

        EOFError once the peer closed or sent EOF, RemoteError for a peer
        close-with-error, TimeoutError after ``timeout`` seconds.  A
        received channel reference arrives as an :class:`~execnet.aio.Channel`.
        """
        result = await self._bridge.call(self._channel.receive, timeout)
        if isinstance(result, AsyncChannel):
            return Channel(self._bridge, result)
        return result

    async def send_eof(self) -> None:
        """Signal that no more items follow (peer keeps its send side)."""
        await self._bridge.call(self._channel.send_eof)

    async def aclose(self, error: str | None = None) -> None:
        """Close the channel; ``error`` reaches the peer as a RemoteError."""
        await self._bridge.call(self._channel.aclose, error)

    async def wait_closed(self) -> None:
        """Wait until the peer closed or sent EOF; reraise remote errors."""
        await self._bridge.call(self._channel.wait_closed)

    def __aiter__(self) -> Channel:
        return self

    async def __anext__(self) -> Any:
        try:
            return await self.receive()
        except EOFError:
            raise StopAsyncIteration from None


class Gateway:
    """asyncio facade over a trio-native :class:`AsyncGateway`."""

    def __init__(self, bridge: _HostBridge, gateway: AsyncGateway) -> None:
        self._bridge = bridge
        self._gateway = gateway

    @property
    def id(self) -> str:
        return self._gateway.id

    @property
    def remoteaddress(self) -> str | None:
        return self._gateway.remoteaddress

    def __repr__(self) -> str:
        return f"<aio.Gateway id={self._gateway.id!r}>"

    async def remote_exec(
        self,
        source: str | types.FunctionType | Callable[..., object] | types.ModuleType,
        **kwargs: object,
    ) -> Channel:
        """Connect a new channel to remote execution of ``source``.

        Accepts the same source kinds as ``Gateway.remote_exec``: a source
        string, a pure function called with ``channel`` and ``**kwargs``,
        or a module.
        """
        channel = await self._bridge.call(
            functools.partial(self._gateway.remote_exec, source, **kwargs)
        )
        return Channel(self._bridge, channel)

    async def terminate(self) -> None:
        """Send GATEWAY_TERMINATE to the peer, then close this side."""
        await self._bridge.call(self._gateway.terminate)


class Group:
    """asyncio-native gateway group over a dedicated Trio host thread.

    An async context manager mirroring :class:`execnet.trio.AsyncGroup`;
    leaving the ``async with`` block terminates every gateway with the
    same bounded contract, then stops the host thread.
    """

    def __init__(self, termination_timeout: float = 10.0) -> None:
        self._termination_timeout = termination_timeout
        self._host: TrioHost | None = None
        self._bridge: _HostBridge | None = None
        self._group: _HostedGroup | None = None

    def __repr__(self) -> str:
        state = "running" if self._group is not None else "idle"
        return f"<aio.Group {state}>"

    async def __aenter__(self) -> Self:
        assert self._host is None, "group already entered"
        loop = asyncio.get_running_loop()
        host = TrioHost(name="execnet-aio-group")
        # start() blocks until the host loop is ready; keep it off the
        # asyncio loop thread.
        await loop.run_in_executor(None, host.start)
        self._host = host
        self._bridge = _HostBridge(host)

        async def start_group() -> _HostedGroup:
            # runs on the host loop
            group = _HostedGroup(self._termination_timeout)
            assert host._nursery is not None
            started: _HostedGroup = await host._nursery.start(group.run)
            return started

        self._group = await self._bridge.call(start_group)
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        group, bridge, host = self._group, self._bridge, self._host
        self._group = self._bridge = None
        if group is not None and bridge is not None:

            async def stop_group() -> None:
                group.shutdown.set()
                await group.finished.wait()

            with suppress(RuntimeError):
                await bridge.call(stop_group)
        if host is not None:
            self._host = None
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, functools.partial(host.stop, 5.0))

    async def makegateway(self, spec: str | XSpec = "popen") -> Gateway:
        """Create a gateway for ``spec`` served on the group's host.

        All transports are supported: popen (including uv-provisioned
        ``python=``), ``ssh=``, ``vagrant_ssh=``, ``socket=`` (with
        ``installvia=``), and ``via=`` sub-gateways.
        """
        group, bridge = self._group, self._bridge
        if group is None or bridge is None:
            raise RuntimeError(f"{self!r} is not entered")
        gateway = await bridge.call(group.makegateway, spec)
        return Gateway(bridge, gateway)


@asynccontextmanager
async def open_popen_gateway(spec: str | XSpec = "popen") -> AsyncIterator[Gateway]:
    """Spawn one popen worker and yield an asyncio Gateway to it.

    Convenience for a single-gateway :class:`Group`.
    """
    async with Group() as group:
        yield await group.makegateway(spec)
