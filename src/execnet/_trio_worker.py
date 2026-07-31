"""Worker-side Trio networking and exec scheduling for popen/import bootstrap."""

from __future__ import annotations

import json
import os
import stat
import sys
import threading
from collections.abc import Callable
from collections.abc import Sequence
from contextlib import suppress
from typing import TYPE_CHECKING
from typing import Any
from typing import Protocol

import trio

from ._boundary import Mailbox
from ._boundary import WaitBackend
from ._errors import geterrortext
from ._execmodel import effective_profile
from ._execmodel import get_execmodel
from ._gateway_base import WorkerGateway
from ._serialize import loads_internal
from ._trace import trace

if TYPE_CHECKING:
    from . import _trio_host
    from ._channel import Channel
    from ._execmodel import ExecModel

ExecItem = tuple[Any, ...]


class PoolExec:
    """Exec strategy: run each request on a worker thread (trio's pool).

    The building block of the ``thread`` profile; exec'd code may freely
    start its own event loops (it never shares a thread with ours).
    """

    #: whether _run_worker must hand this strategy the process main thread
    needs_primary_thread = False

    def __init__(self, gateway: WorkerGateway) -> None:
        self.gateway = gateway

    async def admit(self, channel: Channel, item: ExecItem) -> bool:
        """FIFO admission gate; always open for pool placement."""
        return True

    async def run(self, channel: Channel, item: ExecItem) -> None:
        await trio.to_thread.run_sync(
            self.gateway.executetask,
            (channel, item),
            abandon_on_cancel=True,
        )

    def integrate_as_primary_thread(self) -> None:
        raise RuntimeError("pool exec strategy does not use the main thread")

    def trigger_shutdown(self) -> None:
        pass


class PrimaryThreadPump:
    """Runs exec requests handed to it on the process main thread.

    The building block for every strategy that needs a real main thread:
    :meth:`integrate_as_primary_thread` parks there draining a mailbox, and
    :meth:`run` hands one request over and awaits its completion.
    """

    #: whether _run_worker must hand this strategy the process main thread
    needs_primary_thread = True

    def __init__(self, gateway: WorkerGateway) -> None:
        self.gateway = gateway
        self._primary: Mailbox[tuple[Channel, ExecItem, threading.Event] | None] = (
            Mailbox()
        )

    async def admit(self, channel: Channel, item: ExecItem) -> bool:
        """FIFO admission gate; always open."""
        return True

    async def run(self, channel: Channel, item: ExecItem) -> None:
        done = threading.Event()
        self._primary.put((channel, item, done))
        await trio.to_thread.run_sync(done.wait, abandon_on_cancel=True)

    def released(self) -> None:
        """Hook: the main thread is free again (called on it, before done)."""

    def integrate_as_primary_thread(self) -> None:
        """Block the main thread running exec tasks until shutdown."""
        while True:
            task = self._primary.get()
            if task is None:
                break
            channel, item, done = task
            try:
                self.gateway.executetask((channel, item))
            finally:
                # Release before signalling: the next request should see the
                # main thread free as early as we can make it.
                self.released()
                done.set()

    def trigger_shutdown(self) -> None:
        self._primary.put(None)


class HybridExec(PrimaryThreadPump):
    """Exec strategy: primary on the main thread, overflow on pool threads.

    The classic ``thread`` profile shape: a request arriving while the
    main thread is idle claims it (pytest and friends get a true main
    thread); requests arriving while it is busy run on worker threads
    instead of queueing.  The claim is decided during FIFO admission so
    the *first* request always gets the main thread.

    This is also what the retired ``main_thread_only`` profile now maps to:
    it existed for the main-thread guarantee, which the claim provides,
    and its extra behaviour -- refusing a second concurrent remote_exec
    rather than overflowing -- was a deadlock guard, not a feature.

    One difference from that profile is worth knowing: it *serialized*, so
    every sequential remote_exec was guaranteed the main thread.  Here the
    claim is released as the exec finishes, while the channel close that
    tells the coordinator it may send the next one is emitted a moment
    earlier -- so a coordinator that immediately re-execs can, rarely, be
    admitted before the release lands and get a pool thread instead.  The
    *first* request always gets the main thread; a caller that needs the
    guarantee for every request wants the ``trio`` or ``gevent`` profile,
    where placement is not a race.
    """

    def __init__(self, gateway: WorkerGateway) -> None:
        super().__init__(gateway)
        self._pool = PoolExec(gateway)
        self._claim_lock = threading.Lock()
        self._primary_busy = False
        self._claimed: set[int] = set()

    async def admit(self, channel: Channel, item: ExecItem) -> bool:
        with self._claim_lock:
            if not self._primary_busy:
                self._primary_busy = True
                self._claimed.add(channel.id)
        return True

    def released(self) -> None:
        with self._claim_lock:
            self._primary_busy = False

    async def run(self, channel: Channel, item: ExecItem) -> None:
        with self._claim_lock:
            claimed = channel.id in self._claimed
            self._claimed.discard(channel.id)
        if not claimed:
            await self._pool.run(channel, item)
            return
        try:
            await super().run(channel, item)
        finally:
            # released() normally cleared this on the main thread already;
            # repeat it so a cancelled or failed run cannot strand the claim
            self.released()


class GreenletExec:
    """Exec strategy: greenlets on a gevent hub owning the main thread.

    ``profile=gevent``: each request runs as a greenlet spawned by the
    integrate loop; the worker's gevent wakeners make channel
    operations park the greenlet, so concurrent remote_execs cooperate on
    the one main thread.  Requires gevent in the worker environment
    (provisioning adds the ``gevent`` requirement automatically).
    """

    needs_primary_thread = True

    def __init__(self, gateway: WorkerGateway) -> None:
        from ._boundary import make_wakener

        self.gateway = gateway
        # The integrate loop blocks in get() on the hub thread: a gevent
        # wakener parks only its root greenlet, letting exec greenlets run.
        self._primary: Mailbox[tuple[Channel, ExecItem, threading.Event] | None] = (
            Mailbox(make_wakener("gevent"))
        )

    async def admit(self, channel: Channel, item: ExecItem) -> bool:
        return True

    async def run(self, channel: Channel, item: ExecItem) -> None:
        done = threading.Event()
        self._primary.put((channel, item, done))
        await trio.to_thread.run_sync(done.wait, abandon_on_cancel=True)

    def integrate_as_primary_thread(self) -> None:
        """Run the hub on the main thread, spawning a greenlet per exec."""
        import gevent

        def run_exec(channel: Channel, item: ExecItem, done: threading.Event) -> None:
            try:
                self.gateway.executetask((channel, item))
            finally:
                done.set()

        while True:
            task = self._primary.get()
            if task is None:
                break
            gevent.spawn(run_exec, *task)

    def trigger_shutdown(self) -> None:
        self._primary.put(None)


# worker profile -> exec strategy for the sync-facade worker; the "trio"
# profile serves a plain AsyncGateway instead (TaskExec below).
# Future placement strategies slot in here (e.g. subinterpreters).
WORKER_EXEC_STRATEGIES: dict[str, Callable[[WorkerGateway], Any]] = {
    "thread": HybridExec,
    "gevent": GreenletExec,
}


class TaskExec:
    """Exec strategy for the pure-async profile (``profile=trio``).

    Sources run as tasks on the worker's own loop, in the one and only
    thread of the process, and receive an ``AsyncChannel``.  Sources must
    be async: a plain function or a source string without top-level
    ``await`` is rejected before it can starve the loop.  Gateway
    termination cancels running exec tasks (``trio.Cancelled`` inside the
    source).
    """

    def __init__(self, gateway: Any, nursery: trio.Nursery) -> None:
        self.gateway = gateway
        self.nursery = nursery
        self._running = 0

    def active_count(self) -> int:
        return self._running

    def handle_exec(self, gateway: Any, channelid: int, data: bytes) -> None:
        """CHANNEL_EXEC hook; runs inline on the dispatch task."""
        item = loads_internal(data)
        assert isinstance(item, tuple)
        channel = gateway.open_channel(channelid)
        self.nursery.start_soon(self._run_exec, channel, item)

    async def _run_exec(self, channel: Any, item: ExecItem) -> None:
        import ast
        import inspect

        source, file_name, call_name, kwargs = item
        self._running += 1
        try:
            trace(f"async execution starts[{channel.id}]: {source[:50]!r}")
            loc: dict[str, Any] = {"channel": channel, "__name__": "__channelexec__"}
            co = compile(
                source + "\n",
                file_name or "<remote exec>",
                "exec",
                flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT,
            )
            toplevel_await = bool(co.co_flags & inspect.CO_COROUTINE)
            if not toplevel_await and not call_name:
                await channel.aclose(
                    "sync source under profile=trio: the source must use"
                    " top-level await (or pass an async function)"
                )
                return
            if toplevel_await:
                await eval(co, loc)
            else:
                exec(co, loc)  # define the function (no top-level awaits)
            if call_name:
                function = loc[call_name]
                result = function(channel, **kwargs)
                if not hasattr(result, "__await__"):
                    await channel.aclose(
                        f"sync function {call_name!r} under profile=trio:"
                        " remote_exec functions must be async"
                    )
                    return
                await result
        except trio.Cancelled:
            raise
        except EOFError:
            trace("ignoring EOFError from async exec")
        except BaseException as exc:
            trace(f"async exec got exception: {exc!r}")
            await channel.aclose(geterrortext(exc))
            return
        finally:
            self._running -= 1
            trace("async execution finished")
        await channel.aclose()


class TrioWorkerExec:
    """FIFO admission pump feeding an exec placement strategy.

    Exec requests flow through a single pump task so admission happens
    strictly in message-arrival order (trio task scheduling order is
    deliberately unordered, so per-request tasks would race for e.g. the
    main-thread claim).  Where an admitted request runs is the
    strategy's business (:data:`WORKER_EXEC_STRATEGIES`).
    """

    def __init__(
        self,
        host: _trio_host.TrioHost,
        gateway: WorkerGateway,
        strategy: Any,
    ) -> None:
        self.host = host
        self.gateway = gateway
        self.strategy = strategy
        self._lock = threading.Lock()
        self._running = 0
        self._shutting_down = False
        self._idle = threading.Event()
        self._idle.set()
        self._pending_send: trio.MemorySendChannel[tuple[Channel, ExecItem]]
        self._pending_recv: trio.MemoryReceiveChannel[tuple[Channel, ExecItem]]
        self._pending_send, self._pending_recv = trio.open_memory_channel(float("inf"))
        self._pump_started = False

    @property
    def needs_primary_thread(self) -> bool:
        return bool(self.strategy.needs_primary_thread)

    def active_count(self) -> int:
        with self._lock:
            return self._running

    def _track_start(self) -> None:
        with self._lock:
            self._running += 1
            self._idle.clear()

    def _track_finish(self) -> None:
        with self._lock:
            self._running -= 1
            if self._running == 0:
                self._idle.set()

    def schedule(self, channel: Channel, sourcetask: bytes) -> None:
        """Called from the session dispatch on the Trio host thread.

        Must not block: admission checks and exec run in a nursery task.
        """
        item = loads_internal(sourcetask)
        assert isinstance(item, tuple)
        with self._lock:
            if self._shutting_down:
                channel.close("execution disallowed")
                return
        # Already on the Trio host thread (Message handler).
        if not self._pump_started:
            self._pump_started = True
            self.host.start_soon(self._pump)
        self._pending_send.send_nowait((channel, item))

    async def _pump(self) -> None:
        """Admit queued exec requests in FIFO order, then run each as a task."""
        async for channel, item in self._pending_recv:
            if await self.strategy.admit(channel, item):
                self.host.start_soon(self._run_exec, channel, item)

    async def _run_exec(self, channel: Channel, item: ExecItem) -> None:
        self._track_start()
        try:
            await self.strategy.run(channel, item)
        finally:
            self._track_finish()

    def integrate_as_primary_thread(self) -> None:
        self.strategy.integrate_as_primary_thread()

    def trigger_shutdown(self) -> None:
        with self._lock:
            self._shutting_down = True
        self.strategy.trigger_shutdown()

    def waitall(self, timeout: float | None = None) -> bool:
        return self._idle.wait(timeout)


def _devnull() -> str:
    try:
        return os.devnull
    except AttributeError:  # pragma: no cover - defensive
        return "NUL" if os.name == "nt" else "/dev/null"


def _dup_protocol_fds() -> tuple[int, int]:
    """Move the protocol off stdin/stdout, leaving fd 0/1 free to redirect.

    Returns ``(read_fd, write_fd)`` for the Message protocol (the worker
    reads what the coordinator writes to our stdin, and writes what the
    coordinator reads from our stdout).  What happens to fd 0/1 afterwards
    is the caller's choice -- see :func:`apply_stdio`.
    """
    if not hasattr(os, "dup"):  # pragma: no cover - jython legacy
        raise RuntimeError("the execnet worker requires os.dup")
    return os.dup(0), os.dup(1)


def apply_stdio(
    stdin: str = "inherit", stdout: str = "inherit", stderr: str = "inherit"
) -> None:
    """Point the worker's standard fds where the launcher asked.

    ``inherit`` leaves an fd alone, which is the default once the protocol
    has a transport of its own: a worker's output is then the user's, not
    something execnet has to swallow to protect the wire.

    ``close`` (stdin only) reopens fd 0 on the null device *and* closes
    ``sys.stdin``.  Reads through Python raise, while the fd itself stays
    reserved -- genuinely closing it would let the next ``os.open`` land on
    fd 0, where anything writing to "stdin" would corrupt an unrelated file.
    """
    if stdin in ("close", "devnull"):
        fd = os.open(_devnull(), os.O_RDONLY)
        os.dup2(fd, 0)
        os.close(fd)
        if stdin == "close":
            with suppress(Exception):
                sys.stdin.close()
            sys.stdin = os.fdopen(0, "r", closefd=False)
            sys.stdin.close()
        else:
            sys.stdin = os.fdopen(0, "r", closefd=False)

    if stdout == "stderr":
        os.dup2(2, 1)
        sys.stdout = os.fdopen(1, "w", buffering=1, closefd=False)
    elif stdout == "devnull":
        fd = os.open(_devnull(), os.O_WRONLY)
        os.dup2(fd, 1)
        os.close(fd)
        sys.stdout = os.fdopen(1, "w", buffering=1, closefd=False)

    if stderr == "devnull":
        fd = os.open(_devnull(), os.O_WRONLY)
        os.dup2(fd, 2)
        os.close(fd)
        sys.stderr = os.fdopen(2, "w", buffering=1, closefd=False)


class _WorkerIOStub:
    """Minimal IO stub so WorkerGateway can be constructed without sync pipes."""

    def __init__(self, execmodel: ExecModel) -> None:
        self.execmodel = execmodel

    def read(self, numbytes: int) -> bytes:
        raise RuntimeError("sync read not used on Trio worker")

    def write(self, data: bytes) -> None:
        raise RuntimeError("sync write not used on Trio worker")

    def close_read(self) -> None:
        return

    def close_write(self) -> None:
        return

    def wait(self) -> int | None:
        return None

    def kill(self) -> None:
        return


def _build_worker_gateway(
    host: _trio_host.TrioHost,
    id: str,
    model: ExecModel,
    wait: WaitBackend = "thread",
) -> tuple[WorkerGateway, TrioWorkerExec]:
    """Construct the WorkerGateway + exec pump/strategy (no IO yet)."""
    trace(f"creating workergateway on trio id={id!r}")
    io_stub = _WorkerIOStub(model)
    gateway = WorkerGateway(io=io_stub, id=id, _startcount=2)
    gateway._wait_backend = wait

    try:
        strategy_factory = WORKER_EXEC_STRATEGIES[effective_profile(model.backend)]
    except KeyError:
        raise ValueError(
            f"profile {model.backend!r} has no worker exec strategy "
            f"(known: {sorted(WORKER_EXEC_STRATEGIES)})"
        ) from None
    strategy = strategy_factory(gateway)
    trio_exec = TrioWorkerExec(host, gateway, strategy)
    # Duck-type as the exec pool for STATUS / _terminate_execution.
    gateway._execpool = trio_exec
    gateway._trio_exec = trio_exec
    return gateway, trio_exec


def _run_worker(
    host: _trio_host.TrioHost,
    io: Any,
    id: str,
    model: ExecModel,
    wait: WaitBackend = "thread",
) -> None:
    """Attach ``io`` as the gateway session and serve until shutdown."""
    gateway, trio_exec = _build_worker_gateway(host, id, model, wait)

    async def _start() -> _trio_host.SyncBridgeGateway:
        # The bridge attaches itself to the gateway before serving starts,
        # so inbound messages can reply through gateway._send right away.
        return await host.start_session(gateway, io)

    host.call(_start)

    try:
        if trio_exec.needs_primary_thread:
            trace("integrating as primary thread (trio worker)")
            trio_exec.integrate_as_primary_thread()
        gateway.join()
    except KeyboardInterrupt:
        # Match WorkerGateway.serve(): swallow in the worker.
        trace("swallowing keyboardinterrupt, serve finished")
    finally:
        host.stop(timeout=5.0)
        # Trio's to_thread cache uses non-daemon threads that would otherwise
        # keep this disposable worker process alive after serve returns.
        os._exit(0)


async def _make_fd_io(read_fd: int, write_fd: int) -> Any:
    from . import _trio_gateway

    return _trio_gateway.staple_fd_stream(read_fd, write_fd)


async def _serve_async_worker(stream: Any, id: str) -> None:
    """Serve a plain AsyncGateway with task-based exec (profile=trio).

    The whole worker is this one trio run on the process main thread: the
    dispatch loop and every exec'd source share it.  Termination cancels
    running exec tasks.
    """
    from ._trio_gateway import AsyncGateway

    gateway = AsyncGateway(stream, id=id, _startcount=2)
    async with trio.open_nursery() as nursery:
        task_exec = TaskExec(gateway, nursery)
        gateway._exec_handler = task_exec.handle_exec
        gateway._task_exec = task_exec
        await nursery.start(gateway._serve)
        await gateway.wait_closed()
        nursery.cancel_scope.cancel()


class Transport(Protocol):
    """How a worker's protocol stream comes into being.

    Split in two because the stdio transport has to claim fd 0/1 *before*
    anything else touches them (including reading ``--config-fd 0``), while
    opening the stream itself needs a running trio loop.
    """

    #: default stdio disposition once this transport is serving
    stdio_defaults: tuple[str, str, str]

    def prepare(self) -> None:
        """Synchronous fd bookkeeping, before the config is read."""

    async def open(self) -> Any:
        """The protocol ByteStream, ready for the Message protocol."""


async def _send_ready(stream: Any) -> Any:
    """Write the single handshake byte the coordinator waits for."""
    await stream.send_all(b"1")
    return stream


class StdioTransport:
    """The protocol *is* this process's stdin/stdout (the classic shape).

    Nothing else can use fd 0/1 afterwards, so the default disposition
    closes stdin and folds stdout onto stderr -- remote ``print()`` stays
    visible on the coordinator instead of going to the null device, and it
    cannot corrupt the wire because the wire is no longer fd 1.
    """

    stdio_defaults = ("close", "stderr", "inherit")

    def __init__(self) -> None:
        self._fds: tuple[int, int] | None = None

    def prepare(self) -> None:
        self._fds = _dup_protocol_fds()

    async def open(self) -> Any:
        from ._trio_gateway import staple_fd_stream

        assert self._fds is not None, "prepare() first"
        read_fd, write_fd = self._fds
        return await _send_ready(staple_fd_stream(read_fd, write_fd))


class ShareTransport:
    """The protocol socket was duplicated into this process by its launcher.

    The Windows counterpart of an inherited fd: ``subprocess`` refuses
    ``pass_fds`` there, but ``WSADuplicateSocket`` can duplicate a socket
    into a named pid.  The resulting blob is bound to *us*, so it is inert
    to anything else that might read it -- and it arrives in the config
    rather than in argv, because sharing needs our pid and so cannot happen
    until we have been spawned.

    Like any other socket transport, the worker's stdio stays untouched.
    """

    stdio_defaults = ("inherit", "inherit", "inherit")

    def __init__(self) -> None:
        self._blob: bytes | None = None

    def prepare(self) -> None:
        pass

    def adopt(self, config: dict[str, Any]) -> None:
        """Take the share blob out of the config, once it has been read."""
        import base64

        from ._trio_gateway import SHARE_KEY

        raw = config.pop(SHARE_KEY, None)
        if raw is None:
            raise SystemExit(
                f"execnet worker: --protocol-share needs {SHARE_KEY!r} in the config"
            )
        self._blob = base64.b64decode(raw)

    async def open(self) -> Any:
        import socket as _socket

        from . import _trio_host

        assert self._blob is not None, "adopt() first"
        sock = _socket.fromshare(self._blob)  # type: ignore[attr-defined]  # Windows
        # hand over the socket itself, not its fd: fromshare() already knows
        # what this socket is, and making adopt_socket re-derive that from
        # the bare handle is what PyPy on Windows cannot do
        return await _trio_host.adopt_socket(sock)


class FdTransport:
    """The protocol runs over inherited fds: one socket, or a pipe pair."""

    stdio_defaults = ("inherit", "inherit", "inherit")

    def __init__(self, fds: Sequence[int]) -> None:
        self.fds = tuple(fds)

    def prepare(self) -> None:
        pass

    async def open(self) -> Any:
        from . import _trio_host
        from ._trio_gateway import staple_fd_stream

        if len(self.fds) == 2:
            read_fd, write_fd = self.fds
            return await _send_ready(staple_fd_stream(read_fd, write_fd))
        (fd,) = self.fds
        if not stat.S_ISSOCK(os.fstat(fd).st_mode):
            raise ValueError(
                f"--protocol-fd {fd} is not a socket; a single fd must be"
                " bidirectional, use --protocol-fd READFD,WRITEFD for a pipe pair"
            )
        # adopt_socket sends the handshake itself
        return await _trio_host.adopt_socket(fd)


def parse_address(address: str) -> tuple[str, Any]:
    """``unix:/path`` or ``host:port`` -> ``("unix", path)`` / ``("tcp", (h, p))``."""
    if address.startswith("unix:"):
        return "unix", address[len("unix:") :]
    host, sep, port = address.rpartition(":")
    if not sep or not port.isdigit():
        raise ValueError(f"expected unix:/path or host:port, got {address!r}")
    return "tcp", (host or "localhost", int(port))


class ConnectTransport:
    """The worker dials out to the coordinator and serves on that connection.

    This is what lets ssh carry the protocol without owning our stdio: the
    coordinator listens on a local unix socket, ``ssh -R`` forwards it, and
    we connect to the remote end of the forward.
    """

    stdio_defaults = ("inherit", "inherit", "inherit")

    def __init__(self, address: str) -> None:
        self.kind, self.target = parse_address(address)

    def prepare(self) -> None:
        pass

    async def open(self) -> Any:
        if self.kind == "unix":
            stream = await trio.open_unix_socket(self.target)
        else:
            stream = await trio.open_tcp_stream(*self.target)
        return await _send_ready(stream)


class ListenTransport:
    """The worker listens and serves the first coordinator that connects.

    The bound address is printed to stdout as JSON before serving, so a
    launcher that asked for an ephemeral port can learn which one it got.
    """

    stdio_defaults = ("inherit", "inherit", "inherit")

    def __init__(self, address: str) -> None:
        self.kind, self.target = parse_address(address)

    def prepare(self) -> None:
        pass

    async def open(self) -> Any:
        if self.kind == "unix":
            sock = trio.socket.socket(trio.socket.AF_UNIX, trio.socket.SOCK_STREAM)
            await sock.bind(self.target)
            sock.listen(1)
            listeners = [trio.SocketListener(sock)]
            bound: Any = self.target
        else:
            host, port = self.target
            listeners = await trio.open_tcp_listeners(port, host=host)
            bound = listeners[0].socket.getsockname()[:2]
        print(json.dumps({"listening": bound}), flush=True)
        stream = await listeners[0].accept()
        for listener in listeners:
            await listener.aclose()
        return await _send_ready(stream)


def serve_worker(
    config: dict[str, Any],
    transport: Transport,
    *,
    stdin: str | None = None,
    stdout: str | None = None,
    stderr: str | None = None,
) -> None:
    """Serve one gateway for ``config`` over ``transport``, then exit.

    ``transport.prepare()`` must already have run (the CLI does it before
    reading the config, so ``--config-fd 0`` still works under the stdio
    transport).
    """
    _check_version(config["coordinator_version"])
    _apply_worker_setup(config)
    defaults = transport.stdio_defaults
    apply_stdio(
        stdin=stdin if stdin is not None else defaults[0],
        stdout=stdout if stdout is not None else defaults[1],
        stderr=stderr if stderr is not None else defaults[2],
    )

    id = config["id"]
    # "execmodel" is the pre-3.0 spelling; accept both so a version-skewed
    # coordinator still connects.
    profile = effective_profile(config.get("profile") or config["execmodel"])
    wait: WaitBackend = config.get("wait", "thread")

    if profile == "trio":
        # pure-async profile: one thread, the loop owns the main thread
        async def main() -> None:
            await _serve_async_worker(await transport.open(), id)

        trio.run(main)
        os._exit(0)

    from . import _trio_host

    host = _trio_host.TrioHost(name=f"execnet-trio-worker-{id}")
    host.start()
    io = host.call(transport.open)
    _run_worker(host, io, id, get_execmodel(profile), wait)


def _rough_version(version: str) -> tuple[int, ...]:
    """Leading numeric (major, minor) of a version string; ``()`` if unparsable."""
    parts: list[int] = []
    for chunk in version.split(".")[:2]:
        number = ""
        for char in chunk:
            if char.isdigit():
                number += char
            else:
                break
        if not number:
            break
        parts.append(int(number))
    return tuple(parts)


def _apply_worker_setup(config: dict[str, Any]) -> None:
    """Apply chdir/nice/env from the worker config, before serving starts.

    This replaces the classic post-start ``remote_exec`` setup: valid for
    every profile (a trio worker cannot run sync sources) and never
    claims an exec slot.
    """
    path = config.get("chdir")
    if path:
        if not os.path.exists(path):
            os.mkdir(path)
        os.chdir(path)
    nice = config.get("nice")
    if nice and hasattr(os, "nice"):
        os.nice(nice)
    for name, value in config.get("env", {}).items():
        os.environ[name] = value


def _check_version(coordinator_version: str) -> None:
    """Warn on a real (major/minor) execnet version mismatch across the wire.

    A minimal (patch-level) mismatch is tolerated.  For same-interpreter popen
    the versions are always identical; this guards the future remote paths.
    """
    import execnet

    ours = _rough_version(execnet.__version__)
    theirs = _rough_version(coordinator_version)
    if ours and theirs and ours != theirs:
        sys.stderr.write(
            "WARNING: execnet version mismatch: coordinator %s worker %s\n"
            % (coordinator_version, execnet.__version__)
        )
        sys.stderr.flush()
