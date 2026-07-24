"""Trio host thread for execnet Message-protocol IO.

Coordinator and worker both run framed read/write loops here.
Sync Channel/Gateway APIs talk to this host via thread-safe queues and
``trio.from_thread``.
"""

from __future__ import annotations

import itertools
import json
import math
import os
import queue
import subprocess
import sys
import threading
from collections.abc import Awaitable
from collections.abc import Callable
from contextlib import suppress
from typing import TYPE_CHECKING
from typing import Any
from typing import Protocol
from typing import TypeVar
from typing import cast

import trio

from .gateway_base import ExecModel
from .gateway_base import GatewayReceivedTerminate
from .gateway_base import Message
from .gateway_base import dumps_internal
from .gateway_base import loads_internal
from .gateway_base import trace

if TYPE_CHECKING:
    from .gateway import Gateway
    from .gateway_base import BaseGateway

T = TypeVar("T")

_CLOSE_WRITE = object()
_ENABLED_ENV = "EXECNET_TRIO_HOST"


def trio_host_enabled() -> bool:
    """Return whether the Trio IO path should be used when applicable."""
    value = os.environ.get(_ENABLED_ENV, "1").strip().lower()
    return value not in ("0", "false", "no", "off")


def should_use_trio_popen(spec: Any) -> bool:
    """Trio path for local popen.

    Same-interpreter popen launches the worker module directly; a foreign
    interpreter (``python=``) is provisioned via ``uv`` and only taken when
    ``uv`` is available (otherwise the legacy source-copy path handles it).
    """
    if not trio_host_enabled():
        return False
    if not getattr(spec, "popen", False):
        return False
    if getattr(spec, "via", None):
        return False
    execmodel = getattr(spec, "execmodel", None)
    if execmodel not in (None, "thread", "main_thread_only"):
        return False
    if getattr(spec, "python", None):
        from . import _provision

        # Direct launch if the interpreter already has execnet; else uv-provision.
        return _provision.target_has_execnet(spec.python) or _provision.uv_available()
    return True


class AsyncByteIO(Protocol):
    async def read_exact(self, n: int) -> bytes: ...

    async def write_all(self, data: bytes) -> None: ...

    async def aclose_read(self) -> None: ...

    async def aclose_write(self) -> None: ...


async def read_exact_receive_stream(stream: trio.abc.ReceiveStream, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = await stream.receive_some(n - len(buf))
        if not chunk:
            raise EOFError("expected %d bytes, got %d" % (n, len(buf)))
        buf += chunk
    return bytes(buf)


async def read_message(io: AsyncByteIO) -> Message:
    header = await io.read_exact(9)
    msgtype, channel, payload = Message.from_header(header)
    data = await io.read_exact(payload) if payload else b""
    return Message.from_parts(msgtype, channel, data)


class ProcessStreamsIO:
    """Async IO over a Trio Process stdin/stdout pair."""

    def __init__(self, process: trio.Process) -> None:
        assert process.stdin is not None
        assert process.stdout is not None
        self.process = process
        self._stdin = process.stdin
        self._stdout = process.stdout

    async def read_exact(self, n: int) -> bytes:
        return await read_exact_receive_stream(self._stdout, n)

    async def write_all(self, data: bytes) -> None:
        await self._stdin.send_all(data)

    async def aclose_read(self) -> None:
        await self._stdout.aclose()

    async def aclose_write(self) -> None:
        await self._stdin.aclose()


class FdStreamsIO:
    """Async IO over OS file descriptors (worker stdio pipes)."""

    def __init__(self, read_fd: int, write_fd: int) -> None:
        self._read = trio.lowlevel.FdStream(read_fd)
        self._write = trio.lowlevel.FdStream(write_fd)

    async def read_exact(self, n: int) -> bytes:
        return await read_exact_receive_stream(self._read, n)

    async def write_all(self, data: bytes) -> None:
        await self._write.send_all(data)

    async def aclose_read(self) -> None:
        await self._read.aclose()

    async def aclose_write(self) -> None:
        await self._write.aclose()


class SocketStreamIO:
    """Async IO over a single bidirectional Trio stream (a socket).

    ``read_exact`` never over-reads (``receive_some(k)`` returns at most ``k``
    bytes), so no cross-call buffering is needed.  ``aclose`` on a Trio stream is
    idempotent, so close-read and close-write both just close the socket.
    """

    def __init__(self, stream: trio.abc.Stream) -> None:
        self._stream = stream

    async def read_exact(self, n: int) -> bytes:
        return await read_exact_receive_stream(self._stream, n)

    async def write_all(self, data: bytes) -> None:
        await self._stream.send_all(data)

    async def aclose_read(self) -> None:
        await self._stream.aclose()

    async def aclose_write(self) -> None:
        await self._stream.aclose()


_CHANNEL_EOF = object()


class ChannelByteIO:
    """Async byte IO tunnelled over a sync execnet ``Channel``.

    INTERIM HACK: the ``via`` transport currently runs the sub-gateway protocol
    as raw bytes over a channel to the master, which double-frames it (sub frame
    -> CHANNEL_DATA -> master frame).  This bridges the sync ``Channel`` to the
    async protocol; it should be replaced by a proper relayed transport rather
    than tunnelling bytes through the channel layer.

    The channel callback (on the host loop) feeds an unbounded memory channel
    that ``read_exact`` drains; writes ``channel.send`` raw frames.
    """

    def __init__(self, channel: Any) -> None:
        self._channel = channel
        self._send, self._recv = trio.open_memory_channel[Any](math.inf)
        self._buf = bytearray()
        channel.setcallback(self._send.send_nowait, endmarker=_CHANNEL_EOF)

    async def read_exact(self, n: int) -> bytes:
        while len(self._buf) < n:
            data = await self._recv.receive()
            if data is _CHANNEL_EOF:
                raise EOFError("channel closed")
            assert isinstance(data, bytes)
            self._buf += data
        out = bytes(self._buf[:n])
        del self._buf[:n]
        return out

    async def write_all(self, data: bytes) -> None:
        self._channel.send(data)

    async def aclose_read(self) -> None:
        return

    async def aclose_write(self) -> None:
        self._channel.close()


async def adopt_socket(socket_fd: int) -> SocketStreamIO:
    """Worker side: wrap an inherited socket fd and send the handshake.

    Runs on the Trio host loop.  The coordinator waits for ``b"1"`` before
    starting the Message protocol; the worker config comes from the CLI.
    """
    import socket as _socket

    sock = _socket.socket(fileno=socket_fd)
    stream = trio.SocketStream(trio.socket.from_stdlib_socket(sock))
    io = SocketStreamIO(stream)
    await io.write_all(b"1")
    return io


class SyncIOHandle:
    """Sync IO facade for Group.terminate wait/kill/close_write."""

    remoteaddress: str

    def __init__(
        self,
        execmodel: ExecModel,
        session: ProtocolSession,
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
        self._session.request_close_read()

    def close_write(self) -> None:
        self._session.request_close_write()

    def wait(self) -> int | None:
        return self._session.wait_process()

    def kill(self) -> None:
        self._session.kill_process()


class ProtocolSession:
    """Reader/writer tasks for one gateway connection."""

    def __init__(
        self,
        gateway: BaseGateway,
        io: AsyncByteIO,
        *,
        process: trio.Process | None = None,
        host: TrioHost,
    ) -> None:
        self.gateway = gateway
        self.io = io
        self.process = process
        self.host = host
        self._outbound: queue.SimpleQueue[object] = queue.SimpleQueue()
        self._wake: trio.Event | None = None
        self._done = threading.Event()
        self._process_exitcode: int | None = None
        self._process_done = threading.Event()
        self._send_closed = False
        self._lock = threading.Lock()

    def enqueue_message(self, message: Message) -> None:
        """Enqueue a frame; wait until written when safe to block.

        Non-host threads wait for the OS write so abrupt ``os._exit`` (xdist
        crash tests) cannot drop already-"sent" data still in the queue.

        The Trio host thread (receiver callbacks) must not wait — that would
        deadlock the writer task on the same event loop.
        """
        wait = not self.host.is_host_thread()
        done = threading.Event() if wait else None
        errors: list[BaseException] = []
        with self._lock:
            if self._send_closed or self._done.is_set():
                raise OSError("cannot send (already closed?)")
            self._outbound.put((message.pack(), done, errors))
        self._wake_writer()
        if done is None:
            return
        if not done.wait(timeout=120.0):
            raise OSError("cannot send (write timed out)")
        if errors:
            raise OSError("cannot send (already closed?)") from errors[0]

    def _wake_writer(self) -> None:
        wake = self._wake
        if wake is None:
            return
        if self.host.is_host_thread():
            wake.set()
        else:
            try:
                self.host.call_sync(wake.set)
            except Exception:
                pass

    def request_close_write(self) -> None:
        with self._lock:
            if self._send_closed:
                return
            self._send_closed = True
            self._outbound.put(_CLOSE_WRITE)
        self._wake_writer()

    def request_close_read(self) -> None:
        # Reader observes EOF / cancel; nothing required from callers.
        return

    def wait_done(self, timeout: float | None = None) -> bool:
        return self._done.wait(timeout)

    def wait_process(self) -> int | None:
        if self.process is None:
            return None

        async def _wait() -> int | None:
            assert self.process is not None
            # Always await wait() so the child is reaped (no zombies).
            code: int | None = await self.process.wait()
            self._process_exitcode = code
            self._process_done.set()
            return code

        try:
            return self.host.call(_wait)
        except Exception:
            self._process_done.wait()
            return self._process_exitcode

    def kill_process(self) -> None:
        if self.process is None:
            return

        async def _kill() -> None:
            assert self.process is not None
            with trio.move_on_after(5):
                self.process.kill()
                self._process_exitcode = await self.process.wait()
                self._process_done.set()

        try:
            self.host.call(_kill)
        except Exception as exc:
            trace("ERROR killing trio process:", exc)

    def is_alive(self) -> bool:
        return not self._done.is_set()

    async def task(
        self, task_status: trio.TaskStatus[None] = trio.TASK_STATUS_IGNORED
    ) -> None:
        try:
            async with trio.open_nursery() as nursery:
                nursery.start_soon(self._writer)
                if self.process is not None:
                    nursery.start_soon(self._supervisor)
                task_status.started()
                await self._reader()
                nursery.cancel_scope.cancel()
        finally:
            await self._finish()

    async def _reader(self) -> None:
        gateway = self.gateway

        def log(*msg: object) -> None:
            gateway._trace("[trio-receiver]", *msg)

        log("RECEIVER: starting")
        try:
            while True:
                msg = await read_message(self.io)
                log("received", msg)
                with gateway._receivelock:
                    msg.received(gateway)
                    del msg
        except GatewayReceivedTerminate:
            log("GATEWAY_TERMINATE")
        except EOFError as exc:
            log("EOF without prior gateway termination message")
            gateway._error = exc
        except Exception as exc:
            log(gateway._geterrortext(exc))
        log("finishing receiver")

    async def _writer(self) -> None:
        self._wake = trio.Event()
        while True:
            while True:
                try:
                    item = self._outbound.get_nowait()
                except queue.Empty:
                    break
                if not await self._writer_handle_item(item):
                    return
            # Reset wake before re-check to avoid losing a notification.
            self._wake = trio.Event()
            try:
                item = self._outbound.get_nowait()
            except queue.Empty:
                await self._wake.wait()
                continue
            if not await self._writer_handle_item(item):
                return

    async def _writer_handle_item(self, item: object) -> bool:
        """Handle one outbound queue item. Return False when writer should stop."""
        if item is _CLOSE_WRITE:
            try:
                await self.io.aclose_write()
            except Exception as exc:
                self.gateway._trace("aclose_write failed", exc)
            return False
        assert isinstance(item, tuple)
        blob, done, errors = item
        assert isinstance(blob, bytes)
        try:
            await self.io.write_all(blob)
        except Exception as exc:
            self.gateway._trace("write failed", exc)
            errors.append(exc)
            with self._lock:
                self._send_closed = True
        finally:
            if done is not None:
                done.set()
        return True

    async def _supervisor(self) -> None:
        assert self.process is not None
        try:
            self._process_exitcode = await self.process.wait()
        finally:
            self._process_done.set()

    async def _finish(self) -> None:
        gateway = self.gateway
        with self._lock:
            self._send_closed = True
            self._outbound.put(_CLOSE_WRITE)
        self._wake_writer()
        gateway._trace("[trio-receiver] finishing channels")
        gateway._channelfactory._finished_receiving()
        # Unblock WorkerGateway.serve()/join before heavy exec-pool shutdown
        # so the primary thread is not waiting on _done while terminate waits
        # on the primary thread draining work.
        self._done.set()
        gateway._trace("[trio-receiver] terminating execution")
        # May sleep/SIGINT; keep it off the Trio scheduling thread.
        await trio.to_thread.run_sync(
            gateway._terminate_execution, abandon_on_cancel=True
        )
        try:
            await self.io.aclose_read()
        except Exception:
            pass
        try:
            await self.io.aclose_write()
        except Exception:
            pass


class TrioHost:
    """Dedicated OS thread running ``trio.run`` for protocol IO."""

    def __init__(self, name: str = "execnet-trio-host") -> None:
        self._name = name
        self._thread: threading.Thread | None = None
        self._token: trio.lowlevel.TrioToken | None = None
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

    def is_host_thread(self) -> bool:
        return self._thread is not None and threading.current_thread() is self._thread

    def _run(self) -> None:
        trio.run(self._main)

    async def _main(self) -> None:
        self._token = trio.lowlevel.current_trio_token()
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
        if self._token is None:
            raise RuntimeError("TrioHost is not running")
        return cast("T", trio.from_thread.run(async_fn, *args, trio_token=self._token))

    def call_sync(self, sync_fn: Callable[..., T], *args: Any) -> T:
        if self._token is None:
            raise RuntimeError("TrioHost is not running")
        return cast(
            "T", trio.from_thread.run_sync(sync_fn, *args, trio_token=self._token)
        )

    def start_soon(self, async_fn: Callable[..., Any], *args: Any) -> None:
        """Schedule a task on the root nursery (must be called on the host thread)."""
        if not self.is_host_thread():
            raise RuntimeError("start_soon requires the Trio host thread")
        if self._nursery is None:
            raise RuntimeError("TrioHost nursery is not available")
        self._nursery.start_soon(async_fn, *args)

    async def start_session(
        self,
        gateway: BaseGateway,
        io: AsyncByteIO,
        *,
        process: trio.Process | None = None,
    ) -> ProtocolSession:
        if self._nursery is None:
            raise RuntimeError("TrioHost nursery is not available")
        session = ProtocolSession(gateway, io, process=process, host=self)
        await self._nursery.start(session.task)
        return session

    def stop(self, timeout: float | None = 5.0) -> None:
        if not self._started or self._token is None or self._shutdown is None:
            return

        def _set() -> None:
            assert self._shutdown is not None
            self._shutdown.set()

        try:
            trio.from_thread.run_sync(_set, trio_token=self._token)
        except Exception:
            pass
        if self._thread is not None:
            self._thread.join(timeout=timeout)
        self._started = False


async def open_popen_process(args: list[str]) -> trio.Process:
    return await trio.lowlevel.open_process(
        args,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
    )


def popen_module_args(spec: Any) -> list[str]:
    """Launch the Trio worker as a module: ``python -m execnet._trio_worker``.

    No source is sent over the wire; the worker imports the installed execnet +
    trio.  Used for same-interpreter popen and for a ``python=`` interpreter that
    already has execnet (so ``sys.executable`` stays that interpreter).
    """
    from . import _provision

    if getattr(spec, "python", None):
        from .gateway_io import shell_split_path

        interpreter = shell_split_path(spec.python)
    else:
        interpreter = [sys.executable]

    args = [*interpreter, "-u"]
    if getattr(spec, "dont_write_bytecode", False):
        args.append("-B")
    args += ["-m", "execnet._trio_worker", _provision.worker_cli_arg(spec)]
    return args


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


def _open_trio_gateway(
    group: Any,
    spec: Any,
    args: list[str],
    *,
    remoteaddress: str | None = None,
    preamble: bytes = b"",
) -> Gateway:
    """Spawn ``args``, do the worker handshake, and attach a Trio session.

    Shared by the popen and ssh factories; ``args`` already encodes how the
    worker is launched (direct module, uv-provisioned, or wrapped in ssh).
    ``preamble`` is streamed to the worker's stdin before the handshake (used to
    ship a wheel to a remote that receives it with ``head -c``).
    """
    import execnet

    from .gateway_bootstrap import HostNotFound

    host: TrioHost = group._ensure_trio_host()

    async def _create_and_attach() -> Gateway:
        process = await open_popen_process(args)
        try:
            async_io = ProcessStreamsIO(process)
            if preamble:
                await async_io.write_all(preamble)
            ack = await async_io.read_exact(1)
            if ack != b"1":
                raise EOFError(f"bad bootstrap handshake: {ack!r}")
        except EOFError:
            with trio.move_on_after(5):
                code = await process.wait()
                # ssh exits 255 when it cannot reach/authenticate the host.
                if remoteaddress is not None and code == 255:
                    raise HostNotFound(remoteaddress) from None
            with trio.move_on_after(5):
                process.kill()
                await process.wait()
            raise
        except BaseException:
            with trio.move_on_after(5):
                process.kill()
                await process.wait()
            raise

        gw = execnet.Gateway(_TempIO(group.execmodel), spec, defer_receive=True)
        session = await host.start_session(gw, async_io, process=process)
        gw._attach_trio_session(session)
        gw._io = SyncIOHandle(group.execmodel, session, remoteaddress=remoteaddress)
        return gw

    return host.call(_create_and_attach)


def makegateway_popen_trio(group: Any, spec: Any) -> Gateway:
    """Create a popen Gateway on the Trio IO path.

    Same-interpreter popen launches ``python -m execnet._trio_worker`` directly;
    a foreign interpreter (``python=``) is provisioned via ``uv``.  Either way the
    worker imports execnet + trio; nothing is sent over the wire to bootstrap it.
    """
    from . import _provision

    if spec.python and not _provision.target_has_execnet(spec.python):
        # bare interpreter: provision execnet + trio via uv
        args = _provision.uv_worker_argv(spec)
    else:
        # same interpreter, or a python= that already has execnet
        args = popen_module_args(spec)
    return _open_trio_gateway(group, spec, args)


def ssh_trio_args(spec: Any) -> tuple[list[str], bytes]:
    """``(ssh argv, stdin preamble)`` for the Trio ssh path.

    The remote runs the uv-provisioned worker; for a dev coordinator the
    preamble carries the wheel bytes that the remote command receives.
    """
    from . import _provision

    remote_command, preamble = _provision.ssh_remote_command(spec)
    assert spec.ssh is not None
    return _provision.ssh_argv(spec.ssh, spec.ssh_config, remote_command), preamble


def makegateway_ssh_trio(group: Any, spec: Any) -> Gateway:
    """Create an ssh Gateway on the Trio IO path (uv-provisioned worker)."""
    args, preamble = ssh_trio_args(spec)
    return _open_trio_gateway(
        group, spec, args, remoteaddress=spec.ssh, preamble=preamble
    )


def should_use_trio_vagrant(spec: Any) -> bool:
    """Trio path for ``vagrant_ssh=<machine>`` gateways (uv-provisioned worker)."""
    if not trio_host_enabled():
        return False
    if not getattr(spec, "vagrant_ssh", None):
        return False
    if getattr(spec, "via", None):
        return False
    execmodel = getattr(spec, "execmodel", None)
    return execmodel in (None, "thread", "main_thread_only")


def makegateway_vagrant_trio(group: Any, spec: Any) -> Gateway:
    """Create a ``vagrant ssh``-wrapped Gateway on the Trio IO path."""
    from . import _provision

    remote_command, preamble = _provision.ssh_remote_command(spec)
    assert spec.vagrant_ssh is not None
    args = _provision.vagrant_ssh_argv(
        spec.vagrant_ssh, spec.ssh_config, remote_command
    )
    return _open_trio_gateway(
        group, spec, args, remoteaddress=spec.vagrant_ssh, preamble=preamble
    )


def should_use_trio_ssh(spec: Any) -> bool:
    """Trio path for ssh gateways (worker provisioned on the remote via uv).

    ``ssh=…//via=…`` is not a direct ssh connection but a sub-gateway spawned
    by the master; that goes through the via path instead.
    """
    if not trio_host_enabled():
        return False
    if not getattr(spec, "ssh", None):
        return False
    if getattr(spec, "via", None):
        return False
    execmodel = getattr(spec, "execmodel", None)
    return execmodel in (None, "thread", "main_thread_only")


def makegateway_socket_trio(group: Any, spec: Any) -> Gateway:
    """Connect a Trio TCP stream to a running ``execnet-socketserver``.

    The server spawns the worker and synthesises its config, so the coordinator
    just connects, waits for the worker's ``b"1"`` handshake, and attaches a Trio
    session (no local process).
    """
    import execnet

    from .gateway_bootstrap import HostNotFound

    host: TrioHost = group._ensure_trio_host()
    if getattr(spec, "installvia", None):
        realhost, realport = start_socketserver_via(group[spec.installvia])
        address = (realhost, realport)
        remoteaddress = "%s:%d" % (realhost, realport)
    else:
        assert spec.socket is not None
        host_str, _, port_str = spec.socket.rpartition(":")
        address = (host_str, int(port_str))
        remoteaddress = spec.socket

    async def _create_and_attach() -> Gateway:
        try:
            stream = await trio.open_tcp_stream(*address)
        except OSError as exc:
            raise HostNotFound(remoteaddress) from exc
        io = SocketStreamIO(stream)
        try:
            ack = await io.read_exact(1)
            if ack != b"1":
                raise EOFError(f"bad socket handshake: {ack!r}")
        except BaseException:
            with trio.move_on_after(5):
                await stream.aclose()
            raise

        gw = execnet.Gateway(_TempIO(group.execmodel), spec, defer_receive=True)
        session = await host.start_session(gw, io)
        gw._attach_trio_session(session)
        gw._io = SyncIOHandle(group.execmodel, session, remoteaddress=remoteaddress)
        return gw

    return host.call(_create_and_attach)


def should_use_trio_socket(spec: Any) -> bool:
    """Trio path for ``socket=host:port`` gateways, including ``installvia``."""
    if not trio_host_enabled():
        return False
    if not getattr(spec, "socket", None):
        return False
    execmodel = getattr(spec, "execmodel", None)
    return execmodel in (None, "thread", "main_thread_only")


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
    """Spawn a requested sub-worker and relay its Message protocol over the channel.

    Runs on the master's Trio host: bytes from the channel go to the sub's
    stdin, and the sub's stdout goes back on the channel (the ``via`` transport).
    A stdin preamble (shipped wheel for a dev-version ssh sub) is streamed
    before the relayed protocol bytes.
    """
    from . import _provision

    channel = gateway._channelfactory.new(channelid)
    try:
        args, preamble = _provision.sub_spawn_argv(request)
        process = await open_popen_process(args)
    except Exception as exc:
        channel.close(f"could not spawn via sub-gateway: {exc}")
        return
    send_ch, recv_ch = trio.open_memory_channel[Any](math.inf)
    channel.setcallback(send_ch.send_nowait, endmarker=_CHANNEL_EOF)

    async def coordinator_to_sub() -> None:
        assert process.stdin is not None
        if preamble:
            await process.stdin.send_all(preamble)
        async for data in recv_ch:
            if data is _CHANNEL_EOF:
                break
            await process.stdin.send_all(data)
        with trio.move_on_after(5):
            await process.stdin.aclose()

    async def sub_to_coordinator() -> None:
        assert process.stdout is not None
        while True:
            data = await process.stdout.receive_some(65536)
            if not data:
                break
            channel.send(data)
        channel.close()

    try:
        async with trio.open_nursery() as nursery:
            nursery.start_soon(coordinator_to_sub)
            nursery.start_soon(sub_to_coordinator)
    except Exception as exc:
        # Do not let a relay failure crash the host nursery; surface it on
        # the channel so the coordinator does not hang on the handshake.
        gateway._trace("via sub relay failed:", exc)
        with suppress(Exception):
            channel.close(f"via sub-gateway relay failed: {exc}")
    finally:
        with trio.move_on_after(5):
            await process.wait()


def handle_start_sub(gateway: BaseGateway, channelid: int, data: bytes) -> None:
    """Worker handler for ``Message.GATEWAY_START_SUB`` (on the host thread)."""
    request = loads_internal(data)
    assert isinstance(request, dict)
    host: TrioHost = gateway._trio_exec.host  # type: ignore[attr-defined]
    host.start_soon(_start_sub_and_relay, gateway, channelid, request)


def should_use_trio_via(spec: Any) -> bool:
    """Trio path for ``via=<gw>`` sub-gateways (popen, python, ssh, vagrant).

    The master spawns the sub-worker from a ``GATEWAY_START_SUB`` request and
    relays its Message protocol.  Socket subs go through ``installvia`` instead.
    """
    if not trio_host_enabled():
        return False
    if not getattr(spec, "via", None):
        return False
    if getattr(spec, "socket", None):
        return False
    execmodel = getattr(spec, "execmodel", None)
    return execmodel in (None, "thread", "main_thread_only")


def makegateway_via_trio(group: Any, spec: Any) -> Gateway:
    """Create a ``via`` gateway: a sub-worker spawned by and relayed through the master."""
    import execnet

    from . import _provision

    master = group[spec.via]
    host: TrioHost = group._ensure_trio_host()
    channel = master.newchannel()
    request = _provision.spawn_request(spec)
    master._send(Message.GATEWAY_START_SUB, channel.id, dumps_internal(request))
    remote = spec.ssh or spec.vagrant_ssh
    remoteaddress = f"{remote}[via {spec.via}]" if remote else None

    async def _create_and_attach() -> Gateway:
        io = ChannelByteIO(channel)
        ack = await io.read_exact(1)
        if ack != b"1":
            raise EOFError(f"bad via handshake: {ack!r}")
        gw = execnet.Gateway(_TempIO(group.execmodel), spec, defer_receive=True)
        session = await host.start_session(gw, io)
        gw._attach_trio_session(session)
        gw._io = SyncIOHandle(group.execmodel, session, remoteaddress=remoteaddress)
        return gw

    return host.call(_create_and_attach)
