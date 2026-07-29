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

Protocol IO keeps running on a Trio host thread (the same engine as the
blocking and trio-native APIs, all transports included); each awaited
operation runs as a task on that host and resolves an asyncio future via
``loop.call_soon_threadsafe``.  No anyio port and no executor threads per
call.

Cancellation crosses the bridge.  Cancelling an awaited ``receive`` (say
by ``asyncio.timeout``) cancels the host-side operation too, so no item
is consumed and dropped.  Operations that must not tear halfway --
``send``, ``send_eof``, ``aclose``, ``terminate`` -- are shielded
instead: the ``CancelledError`` reaches you, but the operation still
completes on the host.

The error types are shared with :mod:`execnet.sync` and
:mod:`execnet.trio`.  Items you send must already be simple builtin data
(plus channels); the standalone serializer is intentionally not part of
the public API -- ``execnet.can_send`` checks a value before you send it;
see ``DumpError``.
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

from ._errors import DataFormatError
from ._errors import DumpError
from ._errors import HostNotFound
from ._errors import LoadError
from ._errors import RemoteError
from ._errors import TimeoutError
from ._host import Host
from ._host import default_host
from ._trio_gateway import AsyncChannel as _TrioChannel
from ._trio_gateway import AsyncGateway as _TrioGateway
from ._trio_gateway import AsyncGroup as _TrioGroup
from ._xspec import XSpec

if TYPE_CHECKING:
    from typing_extensions import Self

__all__ = [
    "AsyncChannel",
    "AsyncGateway",
    "AsyncGroup",
    "DataFormatError",
    "DumpError",
    "Host",
    "HostNotFound",
    "LoadError",
    "RemoteError",
    "TimeoutError",
    "XSpec",
    "default_host",
    "open_gateway",
]

T = TypeVar("T")


class _HostBridge:
    """Await trio-native coroutines on a Trio host from asyncio."""

    def __init__(self, trio_host: Any) -> None:
        self._host = trio_host

    async def call(
        self,
        async_fn: Callable[..., Awaitable[T]],
        *args: Any,
        shield: bool = False,
    ) -> T:
        """Run ``async_fn`` on the host and await its result.

        Unless ``shield``, cancelling the await cancels the host-side
        operation too, so a cancelled ``receive`` does not consume an item
        that nobody will ever see.
        """
        loop = asyncio.get_running_loop()
        future: asyncio.Future[T] = loop.create_future()
        # Built here rather than inside the task: the cancel post can
        # otherwise overtake the task's first step, and cancelling a scope
        # nobody has entered yet still cancels it once entered.
        scope = trio.CancelScope()

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
                with scope:
                    result = await async_fn(*args)
            except trio.Cancelled:
                # host shutdown: the nursery cancel must propagate
                post_result(None, RuntimeError("execnet aio host was shut down"))
                raise
            except BaseException as exc:
                post_result(None, exc)
                return
            if scope.cancelled_caught:
                # cancelled by us -- the awaiter is already gone
                return
            post_result(result, None)

        def spawn() -> None:
            self._host.start_soon(runner)

        try:
            self._host.portal.post(spawn)
        except trio.RunFinishedError:
            raise RuntimeError("execnet aio group is not running") from None

        if shield:
            # The host-side work runs to completion either way; shielding
            # keeps a cancelled caller from abandoning a half-done send.
            return await asyncio.shield(future)
        try:
            return await future
        except asyncio.CancelledError:
            with suppress(trio.RunFinishedError):
                self._host.portal.post(scope.cancel)
            raise


class _HostedGroup(_TrioGroup):
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


class AsyncChannel:
    """asyncio facade over a trio-native channel."""

    RemoteError = RemoteError
    TimeoutError = TimeoutError

    def __init__(self, bridge: _HostBridge, channel: _TrioChannel) -> None:
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

        Cancellable: no item is consumed if the await is cancelled, so
        ``asyncio.timeout`` is equivalent to passing ``timeout``.
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

    def __init__(self, bridge: _HostBridge, gateway: _TrioGateway) -> None:
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


class AsyncGroup:
    """asyncio-native gateway group served on a Trio host thread.

    Usable as an async context manager, or driven explicitly with
    :meth:`start` / :meth:`aclose` from application lifespan hooks.
    Either way, shutting down terminates every gateway with the same
    bounded contract as :class:`execnet.trio.AsyncGroup`.
    """

    def __init__(
        self,
        termination_timeout: float = 10.0,
        *,
        host: Host | None = None,
    ) -> None:
        self._termination_timeout = termination_timeout
        self._host = default_host() if host is None else host
        self._bridge: _HostBridge | None = None
        self._group: _HostedGroup | None = None

    def __repr__(self) -> str:
        state = "running" if self._group is not None else "idle"
        return f"<aio.AsyncGroup {state}>"

    @property
    def host(self) -> Host:
        """The Trio host thread this group's protocol IO runs on."""
        return self._host

    async def start(self) -> None:
        """Bring the host up and start the group task on it."""
        if self._group is not None:
            raise RuntimeError(f"{self!r} is already started")
        trio_host = await _start_host(self._host)
        bridge = _HostBridge(trio_host)

        async def start_group() -> _HostedGroup:
            # runs on the host loop
            group = _HostedGroup(self._termination_timeout)
            assert trio_host._nursery is not None
            started: _HostedGroup = await trio_host._nursery.start(group.run)
            return started

        self._bridge = bridge
        self._group = await bridge.call(start_group, shield=True)

    async def aclose(self) -> None:
        """Terminate every gateway and stop the group task (idempotent).

        The host thread is shared, so it keeps running for other groups.
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
        """Create a gateway for ``spec`` served on the group's host.

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


async def _start_host(host: Host) -> Any:
    """Start ``host`` without blocking the asyncio loop or its executor.

    ``Host._ensure_started`` blocks until the trio loop is ready, so it
    runs on a throwaway thread whose completion is posted back to the
    loop -- never on the default executor, which belongs to the caller's
    application.
    """
    if host.running:
        return host._ensure_started()
    import threading

    loop = asyncio.get_running_loop()
    future: asyncio.Future[Any] = loop.create_future()

    def start() -> None:
        try:
            trio_host = host._ensure_started()
        except BaseException as exc:  # noqa: BLE001
            loop.call_soon_threadsafe(_set_future_error, future, exc)
        else:
            loop.call_soon_threadsafe(_set_future_result, future, trio_host)

    threading.Thread(target=start, name="execnet-host-start", daemon=True).start()
    return await future


def _set_future_result(future: asyncio.Future[Any], value: Any) -> None:
    if not future.cancelled():
        future.set_result(value)


def _set_future_error(future: asyncio.Future[Any], error: BaseException) -> None:
    if not future.cancelled():
        future.set_exception(error)


@asynccontextmanager
async def open_gateway(spec: str | XSpec = "popen") -> AsyncIterator[AsyncGateway]:
    """Spawn one worker for ``spec`` and yield an asyncio gateway to it.

    Convenience for a single-gateway :class:`AsyncGroup`.
    """
    async with AsyncGroup() as group:
        yield await group.makegateway(spec)
