"""Trio host thread for execnet Message-protocol IO.

Coordinator and worker both run framed read/write loops here.
Sync Channel/Gateway APIs talk to this host via thread-safe queues and
``trio.from_thread``.
"""

from __future__ import annotations

import os
import queue
import subprocess
import threading
from collections.abc import Callable
from typing import TYPE_CHECKING
from typing import Any
from typing import Protocol
from typing import TypeVar

import trio

from .gateway_base import ExecModel
from .gateway_base import GatewayReceivedTerminate
from .gateway_base import Message
from .gateway_base import trace

if TYPE_CHECKING:
    from .gateway_base import BaseGateway

T = TypeVar("T")

_CLOSE_WRITE = object()
_ENABLED_ENV = "EXECNET_TRIO_HOST"


def trio_host_enabled() -> bool:
    """Return whether the Trio IO path should be used when applicable."""
    value = os.environ.get(_ENABLED_ENV, "1").strip().lower()
    return value not in ("0", "false", "no", "off")


def should_use_trio_popen(spec: Any) -> bool:
    """PoC: Trio path only for local popen with import bootstrap."""
    if not trio_host_enabled():
        return False
    if not getattr(spec, "popen", False):
        return False
    if getattr(spec, "via", None):
        return False
    if getattr(spec, "python", None):
        return False
    # Worker exec still uses WorkerPool; greenlet models stay on the legacy path.
    execmodel = getattr(spec, "execmodel", None)
    if execmodel not in (None, "thread", "main_thread_only"):
        return False
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
        if done is None:
            return
        if not done.wait(timeout=120.0):
            raise OSError("cannot send (write timed out)")
        if errors:
            raise OSError("cannot send (already closed?)") from errors[0]

    def request_close_write(self) -> None:
        with self._lock:
            if self._send_closed:
                return
            self._send_closed = True
            self._outbound.put(_CLOSE_WRITE)

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
            code = await self.process.wait()
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

    async def task(self, task_status: trio.TaskStatus[None] = trio.TASK_STATUS_IGNORED) -> None:
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
        while True:
            # abandon_on_cancel: queue.get blocks forever until a sentinel;
            # nursery shutdown must not wait on it.
            item = await trio.to_thread.run_sync(
                self._outbound.get, abandon_on_cancel=True
            )
            if item is _CLOSE_WRITE:
                try:
                    await self.io.aclose_write()
                except Exception as exc:
                    self.gateway._trace("aclose_write failed", exc)
                return
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

    async def _supervisor(self) -> None:
        assert self.process is not None
        try:
            self._process_exitcode = await self.process.wait()
        finally:
            self._process_done.set()

    async def _finish(self) -> None:
        gateway = self.gateway
        # Unblock a writer thread left in queue.get after cancel.
        with self._lock:
            self._send_closed = True
            self._outbound.put(_CLOSE_WRITE)
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

    def call(self, async_fn: Callable[..., Any], *args: Any) -> Any:
        if self._token is None:
            raise RuntimeError("TrioHost is not running")
        return trio.from_thread.run(async_fn, *args, trio_token=self._token)

    def call_sync(self, sync_fn: Callable[..., T], *args: Any) -> T:
        if self._token is None:
            raise RuntimeError("TrioHost is not running")
        return trio.from_thread.run_sync(sync_fn, *args, trio_token=self._token)

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


async def bootstrap_import_async(
    process: trio.Process,
    *,
    importdir: str,
    execmodel: str,
    gateway_id: str,
) -> ProcessStreamsIO:
    """Send import-bootstrap source and wait for the handshake byte."""
    io = ProcessStreamsIO(process)
    sources = [
        "import sys",
        "if %r not in sys.path:" % importdir,
        "    sys.path.insert(0, %r)" % importdir,
        "from execnet._trio_worker import serve_popen_trio",
        "sys.stdout.write('1')",
        "sys.stdout.flush()",
        "serve_popen_trio(id=%r, execmodel=%r)" % (f"{gateway_id}-worker", execmodel),
    ]
    source = "\n".join(sources)
    await io.write_all((repr(source) + "\n").encode("utf-8"))
    ack = await io.read_exact(1)
    if ack != b"1":
        raise EOFError(f"bad bootstrap handshake: {ack!r}")
    return io


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


def makegateway_popen_trio(group: Any, spec: Any) -> Any:
    """Create a popen Gateway using Trio process + protocol IO on both sides."""
    import execnet

    from . import gateway_io
    from .gateway_bootstrap import importdir

    host: TrioHost = group._ensure_trio_host()
    args = gateway_io.popen_args(spec)
    remote_execmodel = spec.execmodel

    async def _create_and_attach() -> Any:
        process = await open_popen_process(args)
        try:
            async_io = await bootstrap_import_async(
                process,
                importdir=importdir,
                execmodel=remote_execmodel,
                gateway_id=spec.id,
            )
        except BaseException:
            with trio.move_on_after(5):
                process.kill()
                await process.wait()
            raise

        gw = execnet.Gateway(_TempIO(group.execmodel), spec, defer_receive=True)
        session = await host.start_session(gw, async_io, process=process)
        gw._attach_trio_session(session)
        gw._io = SyncIOHandle(group.execmodel, session)
        return gw

    return host.call(_create_and_attach)
