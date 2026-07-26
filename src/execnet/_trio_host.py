"""Trio host thread for execnet Message-protocol IO.

Coordinator and worker both run framed read/write loops here.
Sync Channel/Gateway APIs talk to this host via thread-safe queues and
``trio.from_thread``.
"""

from __future__ import annotations

import functools
import itertools
import json
import subprocess
import sys
import threading
import weakref
from collections.abc import Awaitable
from collections.abc import Callable
from contextlib import suppress
from typing import TYPE_CHECKING
from typing import Any
from typing import TypeVar

import trio

from ._trio_gateway import RECEIVE_CHUNK
from ._trio_gateway import AsyncGateway
from ._trio_gateway import AsyncGroup
from ._trio_gateway import ByteStream
from ._trio_gateway import RawChannelStream
from ._trio_gateway import open_popen_process
from ._trio_gateway import read_handshake_ack
from ._trio_gateway import ssh_transport_args
from .gateway_base import ExecModel
from .gateway_base import FrameDecoder
from .gateway_base import GatewayReceivedTerminate
from .gateway_base import Message
from .gateway_base import RemoteError
from .gateway_base import dumps_internal
from .gateway_base import loads_internal
from .gateway_base import trace
from .portal import LoopPortal
from .portal import OneShot

if TYPE_CHECKING:
    from .gateway import Gateway
    from .gateway_base import BaseGateway
    from .multi import Group

T = TypeVar("T")


# Kept name: the ssh argv/preamble builder moved to the async core.
ssh_trio_args = ssh_transport_args


async def adopt_socket(socket_fd: int) -> trio.SocketStream:
    """Worker side: wrap an inherited socket fd and send the handshake.

    Runs on the Trio host loop.  The coordinator waits for ``b"1"`` before
    starting the Message protocol; the worker config comes from the CLI.
    """
    import socket as _socket

    sock = _socket.socket(fileno=socket_fd)
    stream = trio.SocketStream(trio.socket.from_stdlib_socket(sock))
    await stream.send_all(b"1")
    return stream


class SyncIOHandle:
    """Sync IO facade for Gateway.exit close_write and terminate wait/kill."""

    remoteaddress: str

    def __init__(
        self,
        execmodel: ExecModel,
        session: SyncBridgeGateway,
        *,
        process: trio.Process | None = None,
        remoteaddress: str | None = None,
    ) -> None:
        self.execmodel = execmodel
        self._session = session
        self._process = process
        if remoteaddress is not None:
            self.remoteaddress = remoteaddress

    def read(self, numbytes: int) -> bytes:
        raise RuntimeError("sync read not supported on Trio IO handle")

    def write(self, data: bytes) -> None:
        raise RuntimeError("sync write not supported on Trio IO handle")

    def close_read(self) -> None:
        return

    def close_write(self) -> None:
        self._session.request_close_write()

    def wait(self) -> int | None:
        process = self._process
        if process is None:
            return None

        async def _wait() -> int | None:
            # Always await wait() so the child is reaped (no zombies).
            code: int | None = await process.wait()
            return code

        try:
            return self._session.host.call(_wait)
        except Exception:
            return process.returncode

    def kill(self) -> None:
        process = self._process
        if process is None:
            return

        async def _kill() -> None:
            with trio.move_on_after(5):
                process.kill()
                await process.wait()

        try:
            self._session.host.call(_kill)
        except Exception as exc:
            trace("ERROR killing trio process:", exc)


class SyncBridgeGateway(AsyncGateway):
    """Async engine serving a sync ``BaseGateway``.

    The reader/writer/framing machinery is inherited from
    :class:`AsyncGateway`; dispatch is overridden to run the classic sync
    ``Message`` handlers (channel queues, callbacks, exec scheduling) under
    the gateway's receive lock instead of routing to raw channels.

    Foreign threads send through :meth:`enqueue_message`: every enqueue goes
    through the portal so loop callbacks and threads land in one global FIFO,
    and non-loop threads wait until the frame hit the OS write (120s ->
    OSError) so an abrupt ``os._exit`` cannot drop already-"sent" data.
    """

    def __init__(
        self,
        stream: ByteStream,
        *,
        id: str,
        sync_gateway: BaseGateway,
        host: TrioHost,
    ) -> None:
        super().__init__(stream, id=id)
        self.sync_gateway = sync_gateway
        self.host = host
        self._done_sync: OneShot[None] = OneShot()
        self._send_closed = False
        self._send_lock = threading.Lock()
        # Attach before any serving can happen: the first inbound message
        # may need to reply through gateway._send, which must already
        # route to this session (not the sync IO stub).
        sync_gateway._attach_trio_session(self)

    # -- engine hooks (run on the host loop) --

    def _dispatch(self, message: Message) -> None:
        """Route one message: sync-facade concerns here, the rest to the core.

        CHANNEL_DATA/CLOSE/CLOSE_ERROR/LAST_MESSAGE fall through to the
        async core, which routes them to the RawChannel whose consumer is
        the bound sync ``Channel``.
        """
        gateway = self.sync_gateway
        code = message.msgcode
        try:
            if code == Message.STATUS:
                self._answer_status(message)
            elif code == Message.CHANNEL_EXEC:
                channel = gateway._channelfactory.new(message.channelid)
                gateway._local_schedulexec(channel=channel, sourcetask=message.data)
            elif code == Message.RECONFIGURE:
                data = loads_internal(message.data, gateway)
                assert isinstance(data, tuple)
                if message.channelid == 0:
                    gateway._strconfig = data
                else:
                    gateway._channelfactory.new(message.channelid)._strconfig = data
            elif code == Message.GATEWAY_START_SOCKET:
                handle_start_socket(gateway, message.channelid, message.data)
            elif code == Message.GATEWAY_START_SUB:
                handle_start_sub(gateway, message.channelid, message.data)
            else:
                super()._dispatch(message)
        except (GatewayReceivedTerminate, EOFError):
            raise
        except Exception as exc:
            gateway._trace("dispatch failed:", gateway._geterrortext(exc))
            raise EOFError("error dispatching message") from exc

    def _answer_status(self, message: Message) -> None:
        # we use the channelid to send back information
        # but don't instantiate a channel object
        gateway = self.sync_gateway
        execpool = getattr(gateway, "_execpool", None)
        d = {
            "numchannels": len(gateway._channelfactory._channels),
            "numexecuting": execpool.active_count() if execpool is not None else 0,
            "execmodel": gateway.execmodel.backend,
        }
        gateway._send(Message.CHANNEL_DATA, message.channelid, dumps_internal(d))
        gateway._send(Message.CHANNEL_CLOSE, message.channelid)

    async def _finalize(self) -> None:
        gateway = self.sync_gateway
        with self._send_lock:
            self._send_closed = True
        if gateway._error is None:
            gateway._error = self._error
        gateway._trace("[trio-bridge] finishing channels")
        gateway._channelfactory._finished_receiving()
        # EOF the loop-side raw channels that have no sync consumer
        # (via-tunnel relays and readers blocked in receive_bytes).
        self._finish_channels()
        # Unblock the worker's join() before heavy exec-pool shutdown
        # so the primary thread is not waiting on _done while terminate waits
        # on the primary thread draining work.
        self._done_sync.set(None)
        gateway._trace("[trio-bridge] terminating execution")
        # May sleep/SIGINT; keep it off the Trio scheduling thread.
        await trio.to_thread.run_sync(
            gateway._terminate_execution, abandon_on_cancel=True
        )

    # -- sync session interface (any thread) --

    def bind_sync_channel(self, channel: Any) -> None:
        """Route inbound data for ``channel.id`` to the sync channel.

        Callable from any thread: binding happens on the loop via the
        portal so it cannot interleave with dispatch, and the raw channel
        replays anything (payloads, a close) that arrived first.  The loop
        side only holds a weakref, preserving the factory's weak-registry
        semantics (GC of the last user reference sends the close message);
        channels with callbacks are kept alive by the factory instead.
        """
        ref = weakref.ref(channel)
        channelid = channel.id

        def bind() -> None:
            raw = self._channel_for(channelid)
            raw.set_consumer(
                functools.partial(self._sync_payload, ref),
                functools.partial(self._sync_close, ref, channelid),
            )

        with suppress(trio.RunFinishedError):
            self.host.portal.post(bind)

    def _sync_payload(self, ref: weakref.ref[Any], data: bytes) -> None:
        channel = ref()
        if channel is not None:
            channel._deliver_payload(data)
        # dead ref: data for a deleted channel is dropped, like before

    def _sync_close(
        self,
        ref: weakref.ref[Any],
        channelid: int,
        error: RemoteError | None,
        sendonly: bool,
    ) -> None:
        channel = ref()
        if channel is None:
            # channel already in "deleted" state
            if error is not None:
                error.warn()
            self.sync_gateway._channelfactory._no_longer_opened(channelid)
            return
        channel._close_from_remote(error, sendonly=sendonly)

    def release_channel(self, channelid: int) -> None:
        """Drop the loop-side raw channel for ``channelid`` (best-effort)."""
        with suppress(trio.RunFinishedError):
            self.host.portal.post(self._forget_channel, channelid)

    def run_on_loop(self, sync_fn: Callable[[], T]) -> T:
        """Run ``sync_fn`` on the host loop, excluding dispatch interleaving.

        Falls back to running inline once the loop is gone (no more
        deliveries can interleave then anyway).
        """
        portal = self.host.portal
        if portal.is_loop_thread():
            return sync_fn()
        try:
            return portal.run_sync(sync_fn)
        except trio.RunFinishedError:
            return sync_fn()

    def enqueue_message(self, message: Message) -> None:
        """Enqueue a frame; wait until written when safe to block.

        The Trio host thread (receiver callbacks) must not wait — that
        would deadlock the writer task on the same event loop.
        """
        frame = message.pack()
        wait = not self.host.portal.is_loop_thread()
        # The ack carries the write failure as a value (never raised into
        # the OneShot) so a KeyboardInterrupt in wait() stays distinguishable
        # from a stream error.
        ack: OneShot[BaseException | None] | None = OneShot() if wait else None

        def post() -> None:
            try:
                self.enqueue_frame(frame, ack.set if ack is not None else None)
            except OSError as exc:
                if ack is not None:
                    ack.set(exc)

        with self._send_lock:
            if self._send_closed:
                raise OSError("cannot send (already closed?)")
            try:
                # Through the portal even from the host thread so every
                # send lands in one global FIFO order.
                self.host.portal.post(post)
            except trio.RunFinishedError:
                raise OSError("cannot send (already closed?)") from None
        if ack is None:
            return
        try:
            error = ack.wait(timeout=120.0)
        except TimeoutError:
            raise OSError("cannot send (write timed out)") from None
        if error is not None:
            raise OSError("cannot send (already closed?)") from error

    def post_message(self, message: Message) -> None:
        """Best-effort non-waiting send (Channel.__del__ during GC)."""
        frame = message.pack()

        def post() -> None:
            with suppress(OSError):
                self.enqueue_frame(frame)

        try:
            self.host.portal.post(post)
        except trio.RunFinishedError:
            raise OSError("cannot send (already closed?)") from None

    def request_close_write(self) -> None:
        with self._send_lock:
            if self._send_closed:
                return
            self._send_closed = True
            with suppress(trio.RunFinishedError):
                # The writer drains queued frames, then signals write-EOF.
                self.host.portal.post(self._outbound_send.close)

    def wait_done(self, timeout: float | None = None) -> bool:
        try:
            self._done_sync.wait(timeout)
        except TimeoutError:
            return False
        return True

    def is_alive(self) -> bool:
        return not self._done_sync.is_set()


class TrioHost:
    """Dedicated OS thread running ``trio.run`` for protocol IO."""

    def __init__(self, name: str = "execnet-trio-host") -> None:
        self._name = name
        self._thread: threading.Thread | None = None
        self._portal: LoopPortal | None = None
        self._nursery: trio.Nursery | None = None
        self._ready = threading.Event()
        self._shutdown: trio.Event | None = None
        self._started = False

    def start(self) -> None:
        if self._started:
            return
        self._thread = threading.Thread(target=self._run, name=self._name, daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout=30):
            raise RuntimeError("TrioHost failed to start")
        self._started = True

    @property
    def portal(self) -> LoopPortal:
        if self._portal is None:
            raise RuntimeError("TrioHost is not running")
        return self._portal

    def is_host_thread(self) -> bool:
        return self._portal is not None and self._portal.is_loop_thread()

    def _run(self) -> None:
        trio.run(self._main)

    async def _main(self) -> None:
        self._portal = LoopPortal()
        self._shutdown = trio.Event()
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

    def call_sync(self, sync_fn: Callable[..., T], *args: Any) -> T:
        return self.portal.run_sync(sync_fn, *args)

    def start_soon(self, async_fn: Callable[..., Any], *args: Any) -> None:
        """Schedule a task on the root nursery (must be called on the host thread)."""
        if not self.is_host_thread():
            raise RuntimeError("start_soon requires the Trio host thread")
        if self._nursery is None:
            raise RuntimeError("TrioHost nursery is not available")
        self._nursery.start_soon(async_fn, *args)

    async def start_session(
        self, gateway: BaseGateway, io: ByteStream
    ) -> SyncBridgeGateway:
        """Serve ``gateway`` over ``io`` as a task on the root nursery."""
        if self._nursery is None:
            raise RuntimeError("TrioHost nursery is not available")
        session = SyncBridgeGateway(
            io, id=str(gateway.id), sync_gateway=gateway, host=self
        )
        await self._nursery.start(session._serve)
        return session

    def stop(self, timeout: float | None = 5.0) -> None:
        if not self._started or self._portal is None or self._shutdown is None:
            return

        def _set() -> None:
            assert self._shutdown is not None
            self._shutdown.set()

        try:
            self._portal.run_sync(_set)
        except Exception:
            pass
        if self._thread is not None:
            self._thread.join(timeout=timeout)
        self._started = False


class _TempIO:
    """Placeholder IO used only while constructing a Trio-backed Gateway."""

    def __init__(self, execmodel: ExecModel) -> None:
        self.execmodel = execmodel

    def read(self, numbytes: int) -> bytes:
        raise RuntimeError("sync read not supported on Trio temp IO")

    def write(self, data: bytes) -> None:
        raise RuntimeError("sync write not supported on Trio temp IO")

    def close_read(self) -> None:
        return

    def close_write(self) -> None:
        return

    def wait(self) -> int | None:
        return None

    def kill(self) -> None:
        return


class FacadeAsyncGroup(AsyncGroup):
    """AsyncGroup owning the async side of a sync ``Group``.

    Runs on the group's :class:`TrioHost`.  Gateways come out as
    :class:`SyncBridgeGateway` objects bound to freshly built sync
    ``Gateway`` facades, and the via / installvia flows go through the sync
    master gateway (its dispatch is sync, so async channels cannot be used
    on it).
    """

    def __init__(self, group: Group, host: TrioHost) -> None:
        super().__init__()
        self.group = group
        self.host = host
        self.shutdown = trio.Event()

    def _make_gateway(self, stream: ByteStream, spec: Any) -> AsyncGateway:
        import execnet

        sync_gw = execnet.Gateway(_TempIO(self.group.execmodel), spec)
        return SyncBridgeGateway(
            stream, id=spec.id, sync_gateway=sync_gw, host=self.host
        )

    async def _open_via_stream(self, spec: Any) -> ByteStream:
        from . import _provision

        master = self.group[spec.via]
        session = master._trio_session
        assert isinstance(session, SyncBridgeGateway)
        channelid = master._channelfactory.allocate_id()
        # Create the raw channel before the request goes out so no relayed
        # frame can arrive unrouted (we are on the loop: no dispatch races).
        io = RawChannelStream(session._channel_for(channelid))
        request = _provision.spawn_request(spec)
        master._send(Message.GATEWAY_START_SUB, channelid, dumps_internal(request))
        await read_handshake_ack(io, "via")
        return io

    async def _resolve_socket_address(self, spec: Any) -> tuple[tuple[str, int], str]:
        if getattr(spec, "installvia", None):
            master = self.group[spec.installvia]
            # Blocking sync channel receive on the master: run in a thread
            # while this loop keeps dispatching the master's messages.
            realhost, realport = await trio.to_thread.run_sync(
                start_socketserver_via, master, abandon_on_cancel=True
            )
            return (realhost, realport), "%s:%d" % (realhost, realport)
        assert spec.socket is not None
        host_str, _, port_str = spec.socket.rpartition(":")
        return (host_str, int(port_str)), spec.socket

    async def run(self, task_status: trio.TaskStatus[FacadeAsyncGroup]) -> None:
        """Own the group nursery as a host task until :attr:`shutdown`."""
        async with self:
            task_status.started(self)
            await self.shutdown.wait()


def makegateway_trio(group: Group, spec: Any) -> Gateway:
    """Create a sync-facade Gateway for ``spec`` on the group's Trio host."""
    host: TrioHost = group._ensure_trio_host()
    async_group: FacadeAsyncGroup = group._ensure_async_group()
    bridge = host.call(async_group.makegateway, spec)
    assert isinstance(bridge, SyncBridgeGateway)
    gw: Gateway = bridge.sync_gateway  # type: ignore[assignment]
    gw._io = SyncIOHandle(
        group.execmodel,
        bridge,
        process=async_group._processes.get(bridge),
        remoteaddress=bridge.remoteaddress,
    )
    return gw


_socket_worker_counter = itertools.count()


def _spawn_socket_worker(fd: int) -> subprocess.Popen[bytes]:
    """Spawn a worker subprocess serving over the inherited socket ``fd``."""
    import execnet

    config = json.dumps(
        {
            "id": "socketworker%d" % next(_socket_worker_counter),
            "execmodel": "thread",
            "coordinator_version": execnet.__version__,
        }
    )
    return subprocess.Popen(
        [sys.executable, "-m", "execnet._trio_worker", config, "--socket-fd", str(fd)],
        pass_fds=[fd],
    )


async def serve_socket_connection(stream: trio.SocketStream, *, reap: bool) -> None:
    """Hand an accepted socket to a fresh worker subprocess (server side).

    ``reap`` waits for the worker (loop server); when false the worker outlives
    this task (one-shot / installvia).
    """
    proc = _spawn_socket_worker(stream.socket.fileno())
    # The child forked with a copy of the fd; release ours.
    await stream.aclose()
    if reap:
        await trio.to_thread.run_sync(proc.wait)


async def _start_socket_and_reply(
    gateway: BaseGateway, channelid: int, bind_host: str
) -> None:
    """Bind an ephemeral port, reply with its address, then serve one connection.

    Runs as a task on the worker's Trio host (scheduled from the message
    handler).  The reply travels back on ``channelid`` like a STATUS reply.
    """
    listeners = await trio.open_tcp_listeners(0, host=bind_host)
    addr = listeners[0].socket.getsockname()
    gateway._send(Message.CHANNEL_DATA, channelid, dumps_internal((addr[0], addr[1])))
    gateway._send(Message.CHANNEL_CLOSE, channelid)

    stream = await listeners[0].accept()
    for listener in listeners:
        await listener.aclose()
    await serve_socket_connection(stream, reap=True)


def handle_start_socket(gateway: BaseGateway, channelid: int, data: bytes) -> None:
    """Worker handler for ``Message.GATEWAY_START_SOCKET`` (on the host thread)."""
    bind_host = loads_internal(data)
    assert isinstance(bind_host, str)
    host: TrioHost = gateway._trio_exec.host  # type: ignore[attr-defined]
    # The receiver runs on the host thread, so schedule the async work directly.
    host.start_soon(_start_socket_and_reply, gateway, channelid, bind_host)


def start_socketserver_via(
    via_gateway: Any, bind_host: str = "localhost"
) -> tuple[str, int]:
    """Ask ``via_gateway`` (protocol message) to start a one-shot socket listener.

    Returns the ``(host, port)`` the coordinator should connect to.
    """
    channel = via_gateway.newchannel()
    via_gateway._send(
        Message.GATEWAY_START_SOCKET, channel.id, dumps_internal(bind_host)
    )
    realhost, realport = channel.receive()
    channel.waitclose()
    if not realhost or realhost in ("0.0.0.0", "::"):
        realhost = "localhost"
    return realhost, int(realport)


async def _start_sub_and_relay(
    gateway: BaseGateway, channelid: int, request: dict[str, Any]
) -> None:
    """Spawn a requested sub-worker and relay its Message protocol frames.

    Runs on the master's Trio host (the ``via`` transport).  The tunnel is
    frame-native both ways: coordinator payloads arrive verbatim through the
    session's raw channel and go to the sub's stdin unchanged (each payload
    one whole frame), while the sub's stdout runs through a FrameDecoder so
    every CHANNEL_DATA sent back carries exactly one frame -- except the
    initial ready byte, which is forwarded on its own for the handshake.
    A stdin preamble (shipped wheel for a dev-version ssh sub) is streamed
    before the relayed protocol bytes.
    """
    from . import _provision

    def send_close_error(text: str) -> None:
        with suppress(OSError):
            gateway._send(Message.CHANNEL_CLOSE_ERROR, channelid, dumps_internal(text))

    try:
        args, preamble = _provision.sub_spawn_argv(request)
        process = await open_popen_process(args)
    except Exception as exc:
        send_close_error(f"could not spawn via sub-gateway: {exc}")
        return
    session = gateway._trio_session
    assert isinstance(session, SyncBridgeGateway)
    raw = session._channel_for(channelid)

    async def coordinator_to_sub() -> None:
        assert process.stdin is not None
        if preamble:
            await process.stdin.send_all(preamble)
        with suppress(RemoteError):
            async for data in raw:
                await process.stdin.send_all(data)
        with trio.move_on_after(5):
            await process.stdin.aclose()

    async def sub_to_coordinator() -> None:
        assert process.stdout is not None
        ack = bytes(await process.stdout.receive_some(1))
        if ack:
            gateway._send(Message.CHANNEL_DATA, channelid, ack)
            decoder = FrameDecoder()
            while True:
                data = bytes(await process.stdout.receive_some(RECEIVE_CHUNK))
                if not data:
                    break
                for message in decoder.feed(data):
                    gateway._send(Message.CHANNEL_DATA, channelid, message.pack())
        with suppress(OSError):
            gateway._send(Message.CHANNEL_CLOSE, channelid)

    try:
        async with trio.open_nursery() as nursery:
            nursery.start_soon(coordinator_to_sub)
            nursery.start_soon(sub_to_coordinator)
    except Exception as exc:
        # Do not let a relay failure crash the host nursery; surface it on
        # the channel so the coordinator does not hang on the handshake.
        gateway._trace("via sub relay failed:", exc)
        send_close_error(f"via sub-gateway relay failed: {exc}")
    finally:
        session._forget_channel(channelid)
        with trio.move_on_after(5):
            await process.wait()


def handle_start_sub(gateway: BaseGateway, channelid: int, data: bytes) -> None:
    """Worker handler for ``Message.GATEWAY_START_SUB`` (on the host thread)."""
    request = loads_internal(data)
    assert isinstance(request, dict)
    host: TrioHost = gateway._trio_exec.host  # type: ignore[attr-defined]
    host.start_soon(_start_sub_and_relay, gateway, channelid, request)
