"""Worker-side Trio networking and exec scheduling for popen/import bootstrap."""

from __future__ import annotations

import functools
import os
import sys
import threading
from collections.abc import Callable
from typing import TYPE_CHECKING
from typing import Any

import trio

from .gateway_base import MAIN_THREAD_ONLY_DEADLOCK_TEXT
from .gateway_base import WorkerGateway
from .gateway_base import get_execmodel
from .gateway_base import geterrortext
from .gateway_base import loads_internal
from .gateway_base import trace
from .portal import Mailbox

if TYPE_CHECKING:
    from . import _trio_host
    from .gateway_base import Channel
    from .gateway_base import ExecModel

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


class MainExec:
    """Exec strategy: serialize each request onto the process main thread.

    The ``main_thread_only`` profile (GUI/signal-safe: pytest under xdist
    runs this way).  Admission waits for the previous request to finish and
    closes the channel with the deadlock text when it cannot within a
    second (a second concurrent remote_exec would deadlock the requester).
    """

    needs_primary_thread = True

    def __init__(self, gateway: WorkerGateway) -> None:
        self.gateway = gateway
        self._primary: Mailbox[tuple[Channel, ExecItem, threading.Event] | None] = (
            Mailbox()
        )

    async def admit(self, channel: Channel, item: ExecItem) -> bool:
        complete = self.gateway._executetask_complete
        assert complete is not None
        wait_slot = functools.partial(complete.wait, timeout=1)
        if not await trio.to_thread.run_sync(wait_slot, abandon_on_cancel=True):
            channel.close(MAIN_THREAD_ONLY_DEADLOCK_TEXT)
            return False
        complete.clear()
        return True

    async def run(self, channel: Channel, item: ExecItem) -> None:
        done = threading.Event()
        self._primary.put((channel, item, done))
        await trio.to_thread.run_sync(done.wait, abandon_on_cancel=True)

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
                done.set()

    def trigger_shutdown(self) -> None:
        self._primary.put(None)


# execmodel profile -> exec strategy for the sync-facade worker; the
# "trio" profile serves a plain AsyncGateway instead (TaskExec below).
# Placement strategies to come: classic hybrid for "thread" (primary
# main + pool overflow), "gevent" (greenlets on a main-thread hub), and
# eventually subinterpreters.
WORKER_EXEC_STRATEGIES: dict[str, Callable[[WorkerGateway], Any]] = {
    "thread": PoolExec,
    "main_thread_only": MainExec,
}


class TaskExec:
    """Exec strategy for the pure-async profile (``execmodel=trio``).

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
                    "sync source under execmodel=trio: the source must use"
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
                        f"sync function {call_name!r} under execmodel=trio:"
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
    main_thread_only slot).  Where an admitted request runs is the
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


def _prepare_protocol_fds() -> tuple[int, int]:
    """Dup protocol pipes off stdin/stdout and redirect stdio to /dev/null.

    Returns ``(read_fd, write_fd)`` for the Message protocol (child reads
    coordinator stdin writes; child writes go to coordinator stdout reads).
    """
    if not hasattr(os, "dup"):  # pragma: no cover - jython legacy
        raise RuntimeError("Trio worker requires os.dup")

    try:
        devnull = os.devnull
    except AttributeError:
        devnull = "NUL" if os.name == "nt" else "/dev/null"

    read_fd = os.dup(0)
    fd = os.open(devnull, os.O_RDONLY)
    os.dup2(fd, 0)
    os.close(fd)

    write_fd = os.dup(1)
    fd = os.open(devnull, os.O_WRONLY)
    os.dup2(fd, 1)

    if os.name == "nt":
        sys.stderr = os.fdopen(os.dup(2), "w", 1)
        os.dup2(fd, 2)
    os.close(fd)

    sys.stdin = os.fdopen(0, "r", 1, closefd=False)
    sys.stdout = os.fdopen(1, "w", 1, closefd=False)
    return read_fd, write_fd


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
    host: _trio_host.TrioHost, id: str, model: ExecModel, wait: str = "thread"
) -> tuple[WorkerGateway, TrioWorkerExec]:
    """Construct the WorkerGateway + exec pump/strategy (no IO yet)."""
    trace(f"creating workergateway on trio id={id!r}")
    io_stub = _WorkerIOStub(model)
    gateway = WorkerGateway(io=io_stub, id=id, _startcount=2)
    gateway._wait_backend = wait

    try:
        strategy_factory = WORKER_EXEC_STRATEGIES[model.backend]
    except KeyError:
        raise ValueError(
            f"execmodel {model.backend!r} has no worker exec strategy "
            f"(known: {sorted(WORKER_EXEC_STRATEGIES)})"
        ) from None
    strategy = strategy_factory(gateway)
    trio_exec = TrioWorkerExec(host, gateway, strategy)
    # Duck-type as the exec pool for STATUS / _terminate_execution.
    gateway._execpool = trio_exec
    gateway._trio_exec = trio_exec
    gateway._executetask_complete = None
    if strategy.needs_primary_thread:
        gateway._executetask_complete = threading.Event()
        gateway._executetask_complete.set()
    return gateway, trio_exec


def _run_worker(
    host: _trio_host.TrioHost, io: Any, id: str, model: ExecModel, wait: str = "thread"
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
    """Serve a plain AsyncGateway with task-based exec (execmodel=trio).

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


def serve_popen_async(id: str) -> None:
    """Serve the pure-async worker over the stdio pipes (execmodel=trio)."""
    from ._trio_gateway import staple_fd_stream

    read_fd, write_fd = _prepare_protocol_fds()
    os.write(write_fd, b"1")

    async def main() -> None:
        await _serve_async_worker(staple_fd_stream(read_fd, write_fd), id)

    trio.run(main)
    os._exit(0)


def serve_socket_async(id: str, socket_fd: int) -> None:
    """Serve the pure-async worker over an inherited socket fd."""
    from . import _trio_host

    async def main() -> None:
        stream = await _trio_host.adopt_socket(socket_fd)
        await _serve_async_worker(stream, id)

    trio.run(main)
    os._exit(0)


def serve_popen_trio(id: str, execmodel: str = "thread", wait: str = "thread") -> None:
    """Serve a WorkerGateway over the stdio pipes (popen / ssh worker)."""
    from . import _trio_host

    model = get_execmodel(execmodel)
    read_fd, write_fd = _prepare_protocol_fds()
    # Bootstrap handshake: the coordinator waits for this byte on our stdout
    # before starting the Message protocol.  We are launched as a plain module
    # (``python -m execnet._trio_worker``), so nothing was sent to bootstrap us.
    os.write(write_fd, b"1")

    host = _trio_host.TrioHost(name=f"execnet-trio-worker-{id}")
    host.start()
    io = host.call(_make_fd_io, read_fd, write_fd)
    _run_worker(host, io, id, model, wait)


def serve_socket_trio(
    id: str, execmodel: str, socket_fd: int, wait: str = "thread"
) -> None:
    """Serve a WorkerGateway over an inherited socket fd.

    Used for the socketserver (an accepted TCP connection) and, in future, a
    popen socketpair.  The socket is adopted inside Trio and the handshake is
    written on the host loop; config comes from the CLI.
    """
    from . import _trio_host

    model = get_execmodel(execmodel)
    host = _trio_host.TrioHost(name=f"execnet-trio-worker-{id}")
    host.start()
    io = host.call(_trio_host.adopt_socket, socket_fd)
    _run_worker(host, io, id, model, wait)


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


def _main() -> None:
    """Entry point for ``python -m execnet._trio_worker <config-json> [--socket-fd N]``.

    ``<config-json>`` is the coordinator's ``_provision.worker_cli_arg`` payload:
    ``{"id", "execmodel", "coordinator_version"}``.  With ``--socket-fd`` the
    worker serves over that inherited socket (socketserver); otherwise over the
    stdio pipes (popen / ssh).  The worker imports execnet + trio from the
    environment; no source is sent over the wire to bootstrap it.
    """
    import argparse
    import json

    parser = argparse.ArgumentParser(prog="execnet._trio_worker")
    parser.add_argument("config", help="JSON worker config")
    # Protocol transport: an inherited socket fd (socketserver, or a future popen
    # socketpair); without it the worker serves over the stdio pipes (ssh).
    parser.add_argument("--socket-fd", type=int, default=None)
    ns = parser.parse_args()

    config = json.loads(ns.config)
    _check_version(config["coordinator_version"])
    _apply_worker_setup(config)
    if config["execmodel"] == "trio":
        # pure-async profile: one thread, the loop owns the main thread
        if ns.socket_fd is not None:
            serve_socket_async(config["id"], ns.socket_fd)
        else:
            serve_popen_async(config["id"])
    elif ns.socket_fd is not None:
        serve_socket_trio(
            config["id"],
            config["execmodel"],
            ns.socket_fd,
            wait=config.get("wait", "thread"),
        )
    else:
        serve_popen_trio(
            id=config["id"],
            execmodel=config["execmodel"],
            wait=config.get("wait", "thread"),
        )


if __name__ == "__main__":
    _main()
