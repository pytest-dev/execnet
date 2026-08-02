"""Trio engine thread for execnet Message-protocol IO.

Coordinator and worker both run framed read/write loops here.
Sync Channel/Gateway APIs talk to this engine via thread-safe queues and
``trio.from_thread``.
"""

from __future__ import annotations

import functools
import math
import queue as _queue
import subprocess
import sys
import threading
import weakref
from collections.abc import Callable
from contextlib import suppress
from typing import TYPE_CHECKING
from typing import Any
from typing import TypeVar

import trio

from ._boundary import Flag
from ._channel import ENDMARKER
from ._channel import NO_ENDMARKER_WANTED
from ._channel import Endmarker
from ._errors import GatewayReceivedTerminate
from ._errors import RemoteError
from ._execmodel import ExecModel
from ._execmodel import get_execmodel
from ._message import FrameDecoder
from ._message import Message
from ._message import gateway_info
from ._portal import OneShot
from ._serialize import dumps_internal
from ._serialize import loads_internal
from ._trace import trace
from ._trio_engine import TrioEngine
from ._trio_gateway import RECEIVE_CHUNK
from ._trio_gateway import AsyncGateway
from ._trio_gateway import AsyncGroup
from ._trio_gateway import ByteStream
from ._trio_gateway import RawChannelStream
from ._trio_gateway import configure_worker
from ._trio_gateway import open_popen_process
from ._trio_gateway import provision_sync
from ._trio_gateway import ssh_transport_args

#: bound on how long the endmarker callback may run during engine shutdown
CONSUMER_ENDMARKER_GRACE = 10.0


def _run_callback(callback: Callable[[Any], Any], data: bytes, channel: Any) -> None:
    """Deserialize one payload and invoke the receiver callback (in a thread)."""
    callback(loads_internal(data, channel))


if TYPE_CHECKING:
    from ._gateway import Gateway
    from ._gateway_base import BaseGateway
    from ._multi import Group

T = TypeVar("T")


# Kept name: the ssh argv/preamble builder moved to the async core.
ssh_trio_args = ssh_transport_args


async def adopt_socket(sock: int | Any) -> trio.SocketStream:
    """Worker side: wrap an inherited socket for the loop.

    Takes an fd or an already-built socket.  Rebuilding one from its fd
    makes the constructor *detect* family/type/proto by querying the
    handle, which is not free and not universally reliable -- PyPy on
    Windows raises ``WinError 10014`` doing it to a handle that arrived
    from ``socket.fromshare()``.  A caller holding a real socket should
    hand it over rather than reduce it to an integer first.

    Writes nothing: by the time this runs the handshake is over (it
    happened on this socket, before there was a loop), and a stray byte
    here would be read as the start of a frame.
    """
    import socket as _socket

    if isinstance(sock, int):
        sock = _socket.socket(fileno=sock)
    return trio.SocketStream(trio.socket.from_stdlib_socket(sock))


class SyncIOHandle:
    """What is left of the sync IO object a ``Gateway`` is built around.

    The Message IO itself belongs to the session, so the gateway's ``_io``
    is down to one live duty: ``Gateway.exit`` closing the write side.
    Waiting for and killing the worker process is the async group's
    (``AsyncGroup._terminate_one``), which is where the process handle is.
    """

    remoteaddress: str

    def __init__(
        self,
        execmodel: ExecModel,
        session: SyncBridgeGateway,
        *,
        remoteaddress: str | None = None,
    ) -> None:
        self.execmodel = execmodel
        self._session = session
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
        engine: TrioEngine,
    ) -> None:
        super().__init__(stream, id=id)
        self.sync_gateway = sync_gateway
        self.engine = engine
        # Services run as tasks on the engine's root nursery.  The request's
        # channel is the *coordinator's* id, which the sync factory here
        # knows nothing about, so a service works on the async channel this
        # session already routes to that id.
        self._service_spawn = engine.start_soon
        self._done_sync: OneShot[None] = OneShot(sync_gateway._new_wakener())
        self._send_closed = False
        self._send_lock = threading.Lock()
        # Attach before any serving can happen: the first inbound message
        # may need to reply through gateway._send, which must already
        # route to this session (not the sync IO stub).
        sync_gateway._attach_trio_session(self)

    # -- engine hooks (run on the engine loop) --

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
            elif code == Message.GATEWAY_INFO:
                gateway._send(
                    Message.CHANNEL_DATA,
                    message.channelid,
                    dumps_internal(gateway_info()),
                )
                gateway._send(Message.CHANNEL_CLOSE, message.channelid)
            elif code == Message.CHANNEL_EXEC:
                channel = gateway._channelfactory.new(message.channelid)
                gateway._local_schedulexec(channel=channel, sourcetask=message.data)
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
            # how many concurrent execs this worker admits before refusing;
            # answered here on the loop, where the thread limiter is readable
            "execcapacity": execpool.capacity() if execpool is not None else 0,
            "profile": gateway.execmodel.backend,
            # legacy key, same value -- pytest-xdist reads it
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
        if getattr(gateway, "_execpool", None) is None:
            # a coordinator has no execution to shut down (its
            # _terminate_execution is a no-op) and the thread hop is not free
            return
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
        with suppress(trio.RunFinishedError):
            self.engine.portal.post(self._install_sync_consumer, ref, channelid)

    def _install_sync_consumer(self, ref: weakref.ref[Any], channelid: int) -> None:
        """Route ``channelid``'s raw payloads/close to the sync channel (loop).

        Idempotent: re-binding to the same hooks just re-flushes the (empty)
        raw buffer, so ``attach_consumer`` can call this itself instead of
        depending on the separately-posted bind having run first.
        """
        raw = self._channel_for(channelid)
        raw.set_consumer(
            functools.partial(self._sync_payload, ref),
            functools.partial(self._sync_close, ref, channelid),
        )

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
            self.engine.portal.post(self._forget_channel, channelid)

    # -- receiver callbacks: a consumer task per channel --

    def attach_consumer(
        self,
        channel: Any,
        callback: Callable[[Any], Any],
        endmarker: Endmarker,
    ) -> None:
        """Switch ``channel`` to callback mode: a loop task drains it.

        The switch runs on the loop (so it cannot interleave with delivery):
        it moves any already-buffered items into the task's inbox, points the
        channel's delivery/close at that inbox, and starts the consumer task.
        It deliberately does *not* touch the raw channel's consumer (the sync
        payload/close hooks bound by :meth:`bind_sync_channel` stay in place);
        delivery keeps flowing through ``Channel._deliver_payload`` /
        ``_close_from_remote``, which divert to the inbox once ``_has_consumer``
        is set.  Rebinding the raw channel here would race the still-queued
        ``bind()`` post (``run_sync`` and ``run_sync_soon`` are not mutually
        ordered) and could be clobbered back to the mailbox.

        The task is handed a strong reference to ``channel`` and thus keeps it
        alive for as long as it consumes -- the channel's lifecycle is bound to
        the task (and to GC once the stream closes), not to a registry.
        """

        def switch() -> None:
            if not self.engine._on_engine_thread():
                # run_on_loop fell back to running us inline: the loop is
                # gone, so no consumer task can ever drain this channel.
                # Fail before touching the channel -- a half-switched channel
                # loses its buffered items, refuses receive(), and leaves
                # waitclose() waiting for a consumer that will never run.
                raise OSError(
                    f"cannot set callback on {channel!r}: the engine loop has"
                    " stopped, so nothing can deliver to it"
                )
            mailbox = channel._mailbox
            if mailbox is None:
                raise OSError(f"{channel!r} has callback already registered")
            inbox_send, inbox_recv = trio.open_memory_channel[bytes](math.inf)

            def feed(data: bytes) -> None:
                with suppress(trio.BrokenResourceError, trio.ClosedResourceError):
                    inbox_send.send_nowait(data)

            def close_inbox() -> None:
                with suppress(trio.ClosedResourceError):
                    inbox_send.close()

            # Drain items buffered before the switch into the task's inbox,
            # preserving order.  An ENDMARKER means the channel already closed.
            saw_end = False
            while True:
                try:
                    item = mailbox.get_nowait()
                except _queue.Empty:
                    break
                if item is ENDMARKER:
                    saw_end = True
                    break
                inbox_send.send_nowait(item)
            channel._mailbox = None
            done = Flag(channel.gateway._new_wakener())
            channel._consumer_done = done
            channel._consumer_feed = feed
            channel._consumer_close_inbox = close_inbox

            def stop() -> None:
                # thread-safe: end the task's inbox from any thread (local close)
                with suppress(trio.RunFinishedError, trio.ClosedResourceError):
                    self.engine.portal.post(inbox_send.close)

            channel._consumer_stop = stop
            channel._has_consumer = True

            # Guarantee the raw channel routes to us right now (idempotent with
            # the bind posted at newchannel()): otherwise, if that bind has not
            # run yet, inbound payloads would buffer unread in the raw channel
            # and the consumer task would wait forever.
            self._install_sync_consumer(weakref.ref(channel), channel.id)

            if saw_end:
                close_inbox()
            self.engine.start_soon(
                self._run_consumer, channel, inbox_recv, callback, endmarker, done
            )

        self.run_on_loop(switch)

    async def _run_consumer(
        self,
        channel: Any,
        inbox: trio.MemoryReceiveChannel[bytes],
        callback: Callable[[Any], Any],
        endmarker: Endmarker,
        done: Flag,
    ) -> None:
        """Drain ``inbox`` into ``callback`` (each call off the loop thread).

        Runs on the engine loop; ``channel`` is held for the task's lifetime so
        the channel stays alive while consuming.  Items are delivered in order
        and each callback runs in a threadpool thread.  On completion (EOF,
        local close, or a raising callback) the endmarker fires and ``done``
        is set -- which is what ``waitclose()`` waits on.
        """
        limiter = self.engine._limiter
        try:
            async for data in inbox:
                try:
                    await trio.to_thread.run_sync(
                        functools.partial(_run_callback, callback, data, channel),
                        limiter=limiter,
                    )
                except Exception as exc:
                    # trio.Cancelled is a BaseException and propagates past
                    # here (engine shutdown); only a real callback/deserialize
                    # failure closes the channel with the error.
                    self._consumer_failed(channel, exc)
                    break
        finally:
            # Fire the endmarker and signal done even while the engine is
            # torn down, but never let a stuck callback hang shutdown forever.
            with trio.CancelScope(shield=True):
                if endmarker is not NO_ENDMARKER_WANTED:
                    with (
                        trio.move_on_after(CONSUMER_ENDMARKER_GRACE),
                        suppress(BaseException),
                    ):
                        await trio.to_thread.run_sync(
                            functools.partial(callback, endmarker), limiter=limiter
                        )
                done.set()

    def _consumer_failed(self, channel: Any, exc: BaseException) -> None:
        """A callback (or its deserialization) raised: close with the error."""
        gateway = self.sync_gateway
        gateway._trace("exception during callback: %s" % exc)
        errortext = gateway._geterrortext(exc)
        with suppress(OSError):
            gateway._send(
                Message.CHANNEL_CLOSE_ERROR, channel.id, dumps_internal(errortext)
            )
        channel._close_from_remote(RemoteError(errortext), sendonly=False)

    def run_on_loop(self, sync_fn: Callable[[], T]) -> T:
        """Run ``sync_fn`` on the engine loop, excluding dispatch interleaving.

        Falls back to running inline once the loop is gone (no more
        deliveries can interleave then anyway).
        """
        portal = self.engine.portal
        if portal.is_loop_thread():
            return sync_fn()
        try:
            return portal.run_sync(sync_fn)
        except trio.RunFinishedError:
            return sync_fn()

    def enqueue_message(self, message: Message) -> None:
        """Enqueue a frame; wait until written when safe to block.

        The Trio engine thread (receiver callbacks) must not wait — that
        would deadlock the writer task on the same event loop.
        """
        frame = message.pack()
        wait = not self.engine.portal.is_loop_thread()
        # The ack carries the write failure as a value (never raised into
        # the OneShot) so a KeyboardInterrupt in wait() stays distinguishable
        # from a stream error.
        ack: OneShot[BaseException | None] | None = (
            OneShot(self.sync_gateway._new_wakener()) if wait else None
        )

        def post() -> None:
            try:
                self._enqueue_frame(frame, ack.set if ack is not None else None)
            except OSError as exc:
                if ack is not None:
                    ack.set(exc)

        with self._send_lock:
            if self._send_closed:
                raise OSError("cannot send (already closed?)")
            try:
                # Through the portal even from the engine thread so every
                # send lands in one global FIFO order.
                self.engine.portal.post(post)
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
                self._enqueue_frame(frame)

        try:
            self.engine.portal.post(post)
        except trio.RunFinishedError:
            raise OSError("cannot send (already closed?)") from None

    def request_close_write(self) -> None:
        with self._send_lock:
            if self._send_closed:
                return
            self._send_closed = True
            with suppress(trio.RunFinishedError):
                # The writer drains queued frames, then signals write-EOF.
                self.engine.portal.post(self._outbound_send.close)

    def wait_done(self, timeout: float | None = None) -> bool:
        try:
            self._done_sync.wait(timeout)
        except TimeoutError:
            return False
        return True

    def is_alive(self) -> bool:
        return not self._done_sync.is_set()


async def start_session(
    engine: TrioEngine, gateway: BaseGateway, io: ByteStream
) -> SyncBridgeGateway:
    """Serve ``gateway`` over ``io`` as a task on ``engine``.

    A function rather than a :class:`TrioEngine` method because the session
    it builds belongs to this layer: the engine offers a nursery to start
    long-lived tasks on and stays ignorant of what they are.
    """
    session = SyncBridgeGateway(
        io, id=str(gateway.id), sync_gateway=gateway, engine=engine
    )
    await engine.start_task(session._serve)
    return session


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


class FacadeAsyncGroup(AsyncGroup):
    """AsyncGroup owning the async side of a sync ``Group``.

    Runs on the group's :class:`TrioEngine`.  Gateways come out as
    :class:`SyncBridgeGateway` objects bound to freshly built sync
    ``Gateway`` facades, and the via / installvia flows go through the sync
    sync coordinator gateway (its dispatch is sync, so async channels cannot be
    on it).
    """

    def __init__(self, group: Group, engine: TrioEngine) -> None:
        super().__init__()
        self.group = group
        self.engine = engine
        self.shutdown = trio.Event()

    def _make_gateway(self, stream: ByteStream, spec: Any) -> AsyncGateway:
        import execnet

        sync_gw = execnet.Gateway(_TempIO(get_execmodel(spec.profile)), spec)
        # the caller's concurrency library, inherited from the facade
        sync_gw._wait_backend = self.group._wait_backend
        return SyncBridgeGateway(
            stream, id=spec.id, sync_gateway=sync_gw, engine=self.engine
        )

    async def _open_via_stream(self, spec: Any) -> ByteStream:
        from . import _provision

        coordinator = self.group[spec.via]
        session = coordinator._trio_session
        assert isinstance(session, SyncBridgeGateway)
        request = await provision_sync(_provision.spawn_request, spec)
        channelid = coordinator._channelfactory.allocate_id()
        # Create the raw channel before the request goes out so no relayed
        # frame can arrive unrouted (we are on the loop: no dispatch races).
        io = RawChannelStream(session._channel_for(channelid))
        coordinator._send(Message.GATEWAY_START_SUB, channelid, dumps_internal(request))
        await configure_worker(io, spec, "via")
        return io

    async def _resolve_socket_address(self, spec: Any) -> tuple[tuple[str, int], str]:
        if getattr(spec, "installvia", None):
            coordinator = self.group[spec.installvia]
            # Blocking sync channel receive on that coordinator: run in a
            # thread while this loop keeps dispatching its messages.
            realhost, realport = await trio.to_thread.run_sync(
                start_socketserver_via, coordinator, abandon_on_cancel=True
            )
            return (realhost, realport), "%s:%d" % (realhost, realport)
        assert spec.socket is not None
        host_str, _, port_str = spec.socket.rpartition(":")
        return (host_str, int(port_str)), spec.socket

    async def run(self, task_status: trio.TaskStatus[FacadeAsyncGroup]) -> None:
        """Own the group nursery as an engine task until :attr:`shutdown`.

        Registered with the engine for exactly this task's lifetime, so
        closing the engine knows what it is about to take down.
        """
        self.engine._register_group(self)
        try:
            async with self:
                task_status.started(self)
                await self.shutdown.wait()
        finally:
            self.engine._forget_group(self)


def makegateway_trio(group: Group, spec: Any) -> Gateway:
    """Create a sync-facade Gateway for ``spec`` on the group's Trio engine."""
    engine: TrioEngine = group._ensure_trio_engine()
    async_group: FacadeAsyncGroup = group._ensure_async_group()
    # e.g. a gevent app: only the calling greenlet parks while the gateway
    # comes up, not the whole hub.
    bridge = group.engine_call(engine, async_group.makegateway, spec)
    assert isinstance(bridge, SyncBridgeGateway)
    gw: Gateway = bridge.sync_gateway  # type: ignore[assignment]
    gw._io = SyncIOHandle(
        get_execmodel(spec.profile),
        bridge,
        remoteaddress=bridge.remoteaddress,
    )
    return gw


def _spawn_socket_worker(sock: Any) -> subprocess.Popen[bytes]:
    """Spawn a worker subprocess serving the accepted socket ``sock``.

    POSIX hands the fd over with ``pass_fds``.  Windows has no such thing,
    so the socket is duplicated into the child with ``WSADuplicateSocket``
    and the blob goes to its stdin: it cannot be built until the child's
    pid exists.

    Nothing else is passed.  What the worker *is* -- its id, profile,
    working directory, environment -- comes from the coordinator's config
    frame, which arrives on the very socket being handed over, so the
    server is not in the business of relaying, filtering or even reading
    it.

    Takes the socket rather than its fd because ``share()`` needs a stdlib
    socket object, and building one from a bare fd makes the constructor
    *detect* family/type/proto by querying the handle.  Passing what the
    caller already knows skips that: it is the one difference between this
    path and the popen one, and the popen one works where this did not.
    """
    from . import _provision
    from ._trio_gateway import SHARE_KEY
    from ._trio_gateway import dumps_config
    from ._trio_gateway import share_socket

    argv = [sys.executable, "-m", "execnet", "worker"]
    fd = sock.fileno()
    if not _provision.socket_share_required():
        return subprocess.Popen([*argv, "--protocol-fd", str(fd)], pass_fds=[fd])

    import socket as _socket

    process = subprocess.Popen(
        [*argv, "--protocol-share", "--config-fd", "0"], stdin=subprocess.PIPE
    )
    try:
        # a view on the accepted socket, so share() can reach it; detach so
        # dropping the view does not close the fd we do not own
        view = _socket.socket(sock.family, sock.type, sock.proto, fileno=fd)
        try:
            blob = share_socket(view, process.pid)
        finally:
            view.detach()
        assert process.stdin is not None
        process.stdin.write(dumps_config({SHARE_KEY: blob}))
        process.stdin.close()
    except BaseException:
        process.kill()
        raise
    return process


async def serve_socket_connection(stream: trio.SocketStream, *, reap: bool) -> None:
    """Hand an accepted socket to a fresh worker subprocess (server side).

    ``reap`` waits for the worker (loop server); when false the worker outlives
    this task (one-shot / installvia).

    A failed spawn closes the connection.  The coordinator is already waiting
    on the other end for a handshake reply that is never coming, and an EOF is
    the only thing that will move it -- without this it waits forever.
    """
    try:
        proc = _spawn_socket_worker(stream.socket)
    except BaseException:
        with trio.CancelScope(shield=True), suppress(Exception):
            await stream.aclose()
        raise
    # The child holds its own copy of the socket now; release ours.
    await stream.aclose()
    if reap:
        await trio.to_thread.run_sync(proc.wait)


async def _start_socket_and_reply(
    gateway: BaseGateway, channelid: int, bind_host: str
) -> None:
    """Bind an ephemeral port, reply with its address, then serve one connection.

    Runs as a task on the worker's Trio engine (scheduled from the message
    handler).  The reply travels back on ``channelid`` like a STATUS reply.

    Refusing *before* replying is what makes an unsupported machine survivable:
    once the address has gone out the coordinator will connect and wait for
    a handshake, and there is no longer any way to tell it why nobody is
    there.  An error close instead surfaces at its ``channel.receive()``.
    """
    from . import _provision

    if not _provision.socket_handoff_available():
        gateway._send(
            Message.CHANNEL_CLOSE_ERROR,
            channelid,
            dumps_internal(
                f"cannot serve a socket gateway on {sys.platform}: this machine "
                "cannot hand an accepted socket to a worker process"
            ),
        )
        return

    listeners = await trio.open_tcp_listeners(0, host=bind_host)
    addr = listeners[0].socket.getsockname()
    gateway._send(Message.CHANNEL_DATA, channelid, dumps_internal((addr[0], addr[1])))
    gateway._send(Message.CHANNEL_CLOSE, channelid)

    stream = await listeners[0].accept()
    for listener in listeners:
        await listener.aclose()
    try:
        await serve_socket_connection(stream, reap=True)
    except Exception as exc:
        # This runs as a task on *this worker's* engine: letting it propagate
        # tears the whole gateway down, so a coordinator asking for one
        # unsupported sub-gateway would lose the coordinator it asked through.
        # The connection is already closed, so the coordinator gets its EOF.
        trace(f"socket gateway for channel {channelid} failed: {exc!r}")


def handle_start_socket(gateway: BaseGateway, channelid: int, data: bytes) -> None:
    """Worker handler for ``Message.GATEWAY_START_SOCKET`` (on the engine thread)."""
    bind_host = loads_internal(data)
    assert isinstance(bind_host, str)
    engine: TrioEngine = gateway._trio_exec.engine  # type: ignore[attr-defined]
    # The receiver runs on the engine thread, so schedule the async work directly.
    engine.start_soon(_start_socket_and_reply, gateway, channelid, bind_host)


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


async def _run_delivery_step(argv: list[str], payload: bytes) -> None:
    """Run an out-of-band delivery command, feeding ``payload`` to its stdin."""
    process = await trio.lowlevel.open_process(argv, stdin=subprocess.PIPE)
    try:
        assert process.stdin is not None
        await process.stdin.send_all(payload)
        await process.stdin.aclose()
        code = await process.wait()
    except BaseException:
        with trio.CancelScope(shield=True), trio.move_on_after(5):
            process.kill()
            await process.wait()
        raise
    if code != 0:
        raise RuntimeError(f"delivery step failed with exit {code}: {argv[0]}")


async def _start_sub_and_relay(
    gateway: BaseGateway, channelid: int, request: dict[str, Any]
) -> None:
    """Spawn a requested sub-worker and relay its Message protocol frames.

    Runs on the coordinator's Trio engine (the ``via`` transport).  The tunnel is
    frame-native both ways: coordinator payloads arrive verbatim through the
    session's raw channel and go to the sub's stdin unchanged (each payload
    one whole frame), while the sub's stdout runs through a FrameDecoder so
    every CHANNEL_DATA sent back carries exactly one frame.  The sub's own
    config is the first of those frames, from the coordinator that wants the
    gateway -- this relay passes it through without reading it.  A shipped
    wheel (dev-version ssh sub) is delivered first, over its own connection,
    so the relayed stream carries protocol bytes only.
    """
    from . import _provision

    def send_close_error(text: str) -> None:
        with suppress(OSError):
            gateway._send(Message.CHANNEL_CLOSE_ERROR, channelid, dumps_internal(text))

    try:
        args, delivery = await provision_sync(_provision.sub_spawn_argv, request)
        if delivery is not None:
            await _run_delivery_step(*delivery)
        process = await open_popen_process(args)
    except Exception as exc:
        send_close_error(f"could not spawn via sub-gateway: {exc}")
        return
    session = gateway._trio_session
    assert isinstance(session, SyncBridgeGateway)
    raw = session._channel_for(channelid)

    async def coordinator_to_sub() -> None:
        assert process.stdin is not None
        with suppress(RemoteError):
            async for data in raw:
                await process.stdin.send_all(data)
        with trio.move_on_after(5):
            await process.stdin.aclose()

    async def sub_to_coordinator() -> None:
        assert process.stdout is not None
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
        # Do not let a relay failure crash the engine nursery; surface it on
        # the channel so the coordinator does not hang on the handshake.
        gateway._trace("via sub relay failed:", exc)
        send_close_error(f"via sub-gateway relay failed: {exc}")
    finally:
        session._forget_channel(channelid)
        with trio.move_on_after(5):
            await process.wait()


def handle_start_sub(gateway: BaseGateway, channelid: int, data: bytes) -> None:
    """Worker handler for ``Message.GATEWAY_START_SUB`` (on the engine thread)."""
    request = loads_internal(data)
    assert isinstance(request, dict)
    engine: TrioEngine = gateway._trio_exec.engine  # type: ignore[attr-defined]
    engine.start_soon(_start_sub_and_relay, gateway, channelid, request)
