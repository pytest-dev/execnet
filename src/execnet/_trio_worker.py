"""Worker-side Trio networking and exec scheduling for popen/import bootstrap."""

from __future__ import annotations

import json
import os
import stat
import sys
import threading
from collections.abc import Callable
from collections.abc import Mapping
from collections.abc import Sequence
from contextlib import suppress
from typing import TYPE_CHECKING
from typing import Any
from typing import Protocol

from ._boundary import Mailbox
from ._boundary import WaitBackend
from ._errors import LoopFinishedError
from ._errors import geterrortext
from ._execmodel import effective_profile
from ._execmodel import get_execmodel
from ._gateway_base import WorkerGateway
from ._handshake import read_config_frame
from ._handshake import send_ready_frame
from ._serialize import loads_internal
from ._trace import trace

if TYPE_CHECKING:
    from . import _trio_engine
    from . import _trio_host
    from ._channel import Channel
    from ._execmodel import ExecModel

ExecItem = tuple[Any, ...]

#: environment variable that downgrades the fatal coordinator/worker version
#: check to a warning; read from the config's ``env:`` values too.
IGNORE_VERSION_SKEW = "EXECNET_IGNORE_VERSION_SKEW"


class PoolExec:
    """Exec strategy: run each request on a worker thread (trio's pool).

    The building block of the ``thread`` profile; exec'd code may freely
    start its own event loops (it never shares a thread with ours).
    """

    #: whether _run_worker must hand this strategy the process main thread
    needs_primary_thread = False
    #: whether each concurrent exec occupies a thread of the worker's budget
    exec_costs_a_thread = True

    def __init__(self, gateway: WorkerGateway) -> None:
        self.gateway = gateway

    async def admit(self, channel: Channel, item: ExecItem) -> bool:
        """FIFO admission gate; always open for pool placement."""
        return True

    async def run(self, channel: Channel, item: ExecItem) -> None:
        from ._async import current_async

        await current_async().to_thread(
            self.gateway.executetask,
            (channel, item),
            abandon_on_cancel=True,
        )

    def integrate_as_primary_thread(self) -> None:
        raise RuntimeError("pool exec strategy does not use the main thread")

    def trigger_shutdown(self) -> None:
        pass


def _thread_signal() -> tuple[Any, Callable[[], None]]:
    """A loop-side event plus the callable that sets it from a foreign thread.

    Waiting for an exec that runs *elsewhere* -- the main thread, a greenlet
    -- must not park a pool thread on a ``threading.Event``: that thread is
    part of the budget exec placement is rationed against, so an exec that
    costs no thread would still spend one waiting for itself.

    Must be built on the loop (it captures a portal into it).
    """
    from ._async import current_async

    aio = current_async()
    done = aio.event()
    portal = _loop_portal()

    def signal() -> None:
        # posted callbacks must not raise, and a loop that ended while the
        # exec ran leaves nobody to wake
        with suppress(LoopFinishedError):
            portal.post(done.set)

    return done, signal


def _loop_portal() -> Any:
    """A portal into the loop this is called on, whichever library it is."""
    from ._async import current_async

    if current_async().name == "trio":
        from ._portal import LoopPortal

        return LoopPortal()
    from ._portal import AsyncioPortal

    return AsyncioPortal()


class PrimaryThreadPump:
    """Runs exec requests handed to it on the process main thread.

    The building block for every strategy that needs a real main thread:
    :meth:`integrate_as_primary_thread` parks there draining a mailbox, and
    :meth:`run` hands one request over and awaits its completion.
    """

    #: whether _run_worker must hand this strategy the process main thread
    needs_primary_thread = True
    #: whether each concurrent exec occupies a thread of the worker's budget
    #: (see TrioWorkerExec.capacity): the primary one does not, but the
    #: overflow HybridExec sends to the pool does
    exec_costs_a_thread = True

    def __init__(self, gateway: WorkerGateway) -> None:
        self.gateway = gateway
        self._primary: Mailbox[tuple[Channel, ExecItem, Callable[[], None]] | None] = (
            Mailbox()
        )

    async def admit(self, channel: Channel, item: ExecItem) -> bool:
        """FIFO admission gate; always open."""
        return True

    async def run(self, channel: Channel, item: ExecItem) -> None:
        done, signal = _thread_signal()
        self._primary.put((channel, item, signal))
        await done.wait()

    def released(self) -> None:
        """Hook: the main thread is free again (called on it, before done)."""

    def integrate_as_primary_thread(self) -> None:
        """Block the main thread running exec tasks until shutdown."""
        while True:
            task = self._primary.get()
            if task is None:
                break
            channel, item, signal = task
            try:
                self.gateway.executetask((channel, item))
            finally:
                # Release before signalling: the next request should see the
                # main thread free as early as we can make it.
                self.released()
                signal()

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
    #: greenlets, not threads: concurrent execs here cost the thread budget
    #: nothing, so they are not rationed against it
    exec_costs_a_thread = False

    def __init__(self, gateway: WorkerGateway) -> None:
        from ._boundary import make_wakener

        self.gateway = gateway
        # The integrate loop blocks in get() on the hub thread: a gevent
        # wakener parks only its root greenlet, letting exec greenlets run.
        self._primary: Mailbox[tuple[Channel, ExecItem, Callable[[], None]] | None] = (
            Mailbox(make_wakener("gevent"))
        )

    async def admit(self, channel: Channel, item: ExecItem) -> bool:
        return True

    async def run(self, channel: Channel, item: ExecItem) -> None:
        done, signal = _thread_signal()
        self._primary.put((channel, item, signal))
        await done.wait()

    def integrate_as_primary_thread(self) -> None:
        """Run the hub on the main thread, spawning a greenlet per exec."""
        import gevent

        def run_exec(
            channel: Channel, item: ExecItem, signal: Callable[[], None]
        ) -> None:
            try:
                self.gateway.executetask((channel, item))
            finally:
                signal()

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
    termination cancels running exec tasks (a cancellation inside the
    source).
    """

    def __init__(self, gateway: Any, nursery: Any) -> None:
        from ._async import current_async

        self._aio = current_async()
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
        except self._aio.Cancelled:
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


def exec_capacity() -> int:
    """How many concurrent execs this worker admits.

    Every placement costs a thread from trio's default limiter: a pool exec
    runs there, and a main-thread exec parks there waiting for the main
    thread to finish.  Channel callbacks and the worker's own internal
    ``to_thread`` work draw on threads too, so exec takes *half* the budget
    and leaves the rest to the machinery that has to keep running while
    execs are in flight.

    Must be called on the loop (the limiter is a trio run-local).
    """
    from ._async import current_async

    total = current_async().thread_budget()
    return max(1, int(total // 2))


class TrioWorkerExec:
    """FIFO admission pump feeding an exec placement strategy.

    Exec requests flow through a single pump task so admission happens
    strictly in message-arrival order (trio task scheduling order is
    deliberately unordered, so per-request tasks would race for e.g. the
    main-thread claim).  Where an admitted request runs is the
    strategy's business (:data:`WORKER_EXEC_STRATEGIES`).

    Admission is bounded by :func:`exec_capacity` and a request over it is
    **refused**, not queued: placement needs a thread, and a request waiting
    for one it cannot get is indistinguishable from a hung exec -- it
    occupies a channel the coordinator is waiting on, with nothing to say.
    A refusal reaches that coordinator as a RemoteError naming the limit.
    """

    def __init__(
        self,
        engine: _trio_engine.TrioEngine,
        gateway: WorkerGateway,
        strategy: Any,
    ) -> None:
        self.engine = engine
        self.gateway = gateway
        self.strategy = strategy
        self._lock = threading.Lock()
        #: admitted and not yet finished -- the number STATUS reports, and
        #: what admission is capped on
        self._running = 0
        #: channel ids currently holding one of those slots, so releasing is
        #: idempotent (it happens at the close, and again when the task ends)
        self._holding: set[int] = set()
        #: resolved on the loop at the first request (see exec_capacity)
        self._capacity: int | None = None
        self._shutting_down = False
        self._idle = threading.Event()
        self._idle.set()
        # Built on the main thread rather than on the loop, so the
        # vocabulary comes from the engine rather than from "which loop am
        # I in".  Without one -- a pump built in isolation, as the tests do
        # -- fall back to the loop that is running.
        from ._async import current_async
        from ._async import for_backend

        self._aio = (
            for_backend(engine.backend) if engine is not None else current_async()
        )
        self._pending_send, self._pending_recv = self._aio.queue()
        self._pump_started = False

    @property
    def needs_primary_thread(self) -> bool:
        return bool(self.strategy.needs_primary_thread)

    def active_count(self) -> int:
        with self._lock:
            return self._running

    def capacity(self) -> int | None:
        """Concurrent execs this worker admits, or None for unbounded.

        Only the thread-shaped strategies are rationed: a greenlet exec
        spends no thread, so bounding it would cap the very thing
        ``profile=gevent`` exists to provide.  Resolved on first use rather
        than in ``__init__``, which runs before there is a loop to read the
        thread limiter from.  Reported by ``remote_status()`` as
        ``execcapacity``, because a coordinator that just had a request
        refused wants the number it hit.
        """
        if not self.strategy.exec_costs_a_thread:
            return None
        if self._capacity is None:
            self._capacity = exec_capacity()
        return self._capacity

    def release_slot(self, channelid: int) -> None:
        """Give back the admission slot ``channelid`` holds, once.

        Called twice on the ordinary path, and the *early* call is the one
        that matters: from ``_close_finished``, just before the exec's
        channel close goes out.  That close is how a coordinator learns it
        may send the next request, so it must not be able to arrive before
        the slot it frees -- otherwise ``waitclose(); remote_exec()`` on a
        worker at capacity is refused for a slot that was already gone.
        The task's own ``finally`` then covers everything that never got as
        far as closing.
        """
        with self._lock:
            if channelid not in self._holding:
                return
            self._holding.discard(channelid)
            self._running -= 1
            if self._running == 0:
                self._idle.set()

    def schedule(self, channel: Channel, sourcetask: bytes) -> None:
        """Called from the session dispatch on the Trio engine thread.

        Must not block: admission checks and exec run in a nursery task.
        """
        item = loads_internal(sourcetask)
        assert isinstance(item, tuple)
        capacity = self.capacity()  # on the loop: the limiter is readable
        with self._lock:
            if self._shutting_down:
                channel.close("execution disallowed")
                return
            if capacity is not None and self._running >= capacity:
                full = True
            else:
                full = False
                self._holding.add(channel.id)
                self._running += 1
                self._idle.clear()
        if full:
            channel.close(
                f"execnet worker {self.gateway.id}: refusing remote_exec, already"
                f" running {capacity} of them -- that is this worker's"
                " concurrency limit (half its thread budget; the rest serves"
                " channel callbacks and protocol work). Use more gateways, or"
                " a profile whose execs are not threads (profile=trio,"
                " profile=gevent), which are not bounded this way."
            )
            return
        # Already on the Trio engine thread (Message handler).
        if not self._pump_started:
            self._pump_started = True
            self.engine.start_soon(self._pump)
        self._pending_send.send_nowait((channel, item))

    async def _pump(self) -> None:
        """Admit queued exec requests in FIFO order, then run each as a task."""
        async for channel, item in self._pending_recv:
            if await self.strategy.admit(channel, item):
                self.engine.start_soon(self._run_exec, channel, item)

    async def _run_exec(self, channel: Channel, item: ExecItem) -> None:
        """Run one admitted request, containing whatever it does.

        This is a task on the worker's *root* nursery, so an exception that
        leaves it ends ``trio.run`` and takes the whole worker down -- and
        since 3.0 the worker's stderr is the user's, so the ExceptionGroup
        lands in their terminal.  The one failure that reliably gets here is
        also the least interesting: ``executetask`` closes the channel when
        the source returns, and a connection that went away in the meantime
        makes that raise.  There is nobody left to tell, so trace and stop.

        Every ``engine.start_soon`` entry point owes the loop this containment;
        the socket and via handlers in ``_trio_host`` do the same.
        """
        try:
            await self.strategy.run(channel, item)
        except self._aio.Cancelled:
            raise
        except BaseException as exc:
            trace(f"exec task for channel {channel.id} failed: {exc!r}")
        finally:
            self.release_slot(channel.id)

    def integrate_as_primary_thread(self) -> None:
        self.strategy.integrate_as_primary_thread()

    def trigger_shutdown(self) -> None:
        with self._lock:
            self._shutting_down = True
        self.strategy.trigger_shutdown()

    def waitall(self, timeout: float | None = None) -> bool:
        return self._idle.wait(timeout)


def _first(*values: str | None) -> str:
    """The first disposition actually asked for."""
    for value in values:
        if value is not None:
            return value
    raise AssertionError("the transport default is never None")


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


def _build_worker_gateway(
    engine: _trio_engine.TrioEngine,
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
    trio_exec = TrioWorkerExec(engine, gateway, strategy)
    # Duck-type as the exec pool for STATUS / _terminate_execution.
    gateway._execpool = trio_exec
    gateway._trio_exec = trio_exec
    return gateway, trio_exec


def _run_worker(
    engine: _trio_engine.TrioEngine,
    io: Any,
    id: str,
    model: ExecModel,
    wait: WaitBackend = "thread",
) -> None:
    """Attach ``io`` as the gateway session and serve until shutdown."""
    from . import _trio_host

    gateway, trio_exec = _build_worker_gateway(engine, id, model, wait)

    async def _start() -> _trio_host.SyncBridgeGateway:
        # The bridge attaches itself to the gateway before serving starts,
        # so inbound messages can reply through gateway._send right away.
        return await _trio_host.start_session(engine, gateway, io)

    engine.call(_start)

    try:
        if trio_exec.needs_primary_thread:
            trace("integrating as primary thread (trio worker)")
            trio_exec.integrate_as_primary_thread()
        gateway.join()
    except KeyboardInterrupt:
        # Match WorkerGateway.serve(): swallow in the worker.
        trace("swallowing keyboardinterrupt, serve finished")
    finally:
        engine.stop(timeout=5.0)
        # Trio's to_thread cache uses non-daemon threads that would otherwise
        # keep this disposable worker process alive after serve returns.
        os._exit(0)


async def _make_fd_io(read_fd: int, write_fd: int) -> Any:
    from . import _trio_gateway

    return await _trio_gateway.staple_fd_stream(read_fd, write_fd)


async def _serve_async_worker(stream: Any, id: str) -> None:
    """Serve a plain AsyncGateway with task-based exec (profile=trio).

    The whole worker is this one trio run on the process main thread: the
    dispatch loop and every exec'd source share it.  Termination cancels
    running exec tasks.
    """
    from ._trio_gateway import AsyncGateway

    gateway = AsyncGateway(stream, id=id, _startcount=2)
    from ._async import current_async

    aio = current_async()
    async with aio.task_scope() as nursery:
        task_exec = TaskExec(gateway, nursery)
        gateway._exec_handler = task_exec.handle_exec
        gateway._task_exec = task_exec
        # a service is not exec'd code, so it runs here too -- which is the
        # only reason this profile, which rejects sync sources, can receive
        # a transfer at all
        gateway._service_spawn = nursery.start_soon
        await nursery.start(gateway._serve)
        await gateway.wait_closed()
        nursery.cancel_scope.cancel()


class _FdChannel:
    """Blocking handshake IO over a pipe pair (or a POSIX socket fd)."""

    def __init__(self, read_fd: int, write_fd: int) -> None:
        self._read_fd = read_fd
        self._write_fd = write_fd

    def recv(self, max_bytes: int) -> bytes:
        return os.read(self._read_fd, max_bytes)

    def sendall(self, data: bytes) -> None:
        view = memoryview(data)
        while view:
            view = view[os.write(self._write_fd, view) :]


class _SocketChannel:
    """Blocking handshake IO over a socket object.

    Not ``_FdChannel(sock.fileno(), ...)``: a Windows socket handle is not
    an ``os.read``-able fd, and the share transport's socket only exists
    there.
    """

    def __init__(self, sock: Any) -> None:
        self._sock = sock

    def recv(self, max_bytes: int) -> bytes:
        data: bytes = self._sock.recv(max_bytes)
        return data

    def sendall(self, data: bytes) -> None:
        self._sock.sendall(data)


class Transport(Protocol):
    """How a worker's protocol stream comes into being.

    Three steps, because the stdio transport has to claim fd 0/1 before
    anything else touches them, the config that decides this worker's
    *shape* arrives over the stream itself (so it has to be read before
    there is a loop -- ``profile=trio`` has no side thread to read it on),
    and only wrapping the result needs a running trio loop.
    """

    #: default stdio disposition once this transport is serving
    stdio_defaults: tuple[str, str, str]

    def prepare(self) -> None:
        """Synchronous fd bookkeeping, before anything reads or writes."""

    def connect(self) -> Any:
        """Make the byte channel exist; blocking, no loop yet.

        Returns a :class:`~execnet._handshake.BlockingChannel` the config
        handshake runs over.
        """

    async def open(self) -> Any:
        """The protocol ByteStream, ready for the Message protocol."""


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

    def connect(self) -> Any:
        assert self._fds is not None, "prepare() first"
        return _FdChannel(*self._fds)

    async def open(self) -> Any:
        from ._trio_gateway import staple_fd_stream

        assert self._fds is not None, "prepare() first"
        read_fd, write_fd = self._fds
        return await staple_fd_stream(read_fd, write_fd)


class ShareTransport:
    """The protocol socket was duplicated into this process by its launcher.

    The Windows counterpart of an inherited fd: ``subprocess`` refuses
    ``pass_fds`` there, but ``WSADuplicateSocket`` can duplicate a socket
    into a named pid.  The resulting blob is bound to *us*, so it is inert
    to anything else that might read it -- and it cannot travel with the
    rest of the config, which arrives *through* the socket it describes.
    That is what ``--config-fd`` is left for.

    Like any other socket transport, the worker's stdio stays untouched.
    """

    stdio_defaults = ("inherit", "inherit", "inherit")

    def __init__(self) -> None:
        self._blob: bytes | None = None
        self._sock: Any = None

    def prepare(self) -> None:
        pass

    def adopt(self, local_config: dict[str, Any]) -> None:
        """Take the share blob out of the transport's local config."""
        import base64

        from ._trio_gateway import SHARE_KEY

        raw = local_config.get(SHARE_KEY)
        if raw is None:
            raise SystemExit(
                f"execnet worker: --protocol-share needs {SHARE_KEY!r}"
                " in the config given by --config-fd"
            )
        self._blob = base64.b64decode(raw)

    def connect(self) -> Any:
        import socket as _socket

        assert self._blob is not None, "adopt() first"
        self._sock = _socket.fromshare(self._blob)  # type: ignore[attr-defined]
        return _SocketChannel(self._sock)

    async def open(self) -> Any:
        from . import _trio_host

        # hand over the socket itself, not its fd: fromshare() already knows
        # what this socket is, and making adopt_socket re-derive that from
        # the bare handle is what PyPy on Windows cannot do
        return await _trio_host.adopt_socket(self._sock)


class FdTransport:
    """The protocol runs over inherited fds: one socket, or a pipe pair."""

    stdio_defaults = ("inherit", "inherit", "inherit")

    def __init__(self, fds: Sequence[int]) -> None:
        self.fds = tuple(fds)

    def prepare(self) -> None:
        pass

    def connect(self) -> Any:
        if len(self.fds) == 2:
            return _FdChannel(*self.fds)
        (fd,) = self.fds
        if not stat.S_ISSOCK(os.fstat(fd).st_mode):
            raise ValueError(
                f"--protocol-fd {fd} is not a socket; a single fd must be"
                " bidirectional, use --protocol-fd READFD,WRITEFD for a pipe pair"
            )
        return _FdChannel(fd, fd)

    async def open(self) -> Any:
        from . import _trio_host
        from ._trio_gateway import staple_fd_stream

        if len(self.fds) == 2:
            read_fd, write_fd = self.fds
            return await staple_fd_stream(read_fd, write_fd)
        return await _trio_host.adopt_socket(self.fds[0])


def parse_address(address: str) -> tuple[str, Any]:
    """``unix:/path`` or ``host:port`` -> ``("unix", path)`` / ``("tcp", (h, p))``."""
    if address.startswith("unix:"):
        return "unix", address[len("unix:") :]
    host, sep, port = address.rpartition(":")
    if not sep or not port.isdigit():
        raise ValueError(f"expected unix:/path or host:port, got {address!r}")
    return "tcp", (host or "localhost", int(port))


async def _socket_stream(sock: Any) -> Any:
    """Wrap an already-connected stdlib socket for the loop."""
    from ._async import current_async

    return await current_async().wrap_socket(sock)


class ConnectTransport:
    """The worker dials out to the coordinator and serves on that connection.

    This is what lets ssh carry the protocol without owning our stdio: the
    coordinator listens on a local unix socket, ``ssh -R`` forwards it, and
    we connect to the remote end of the forward.
    """

    stdio_defaults = ("inherit", "inherit", "inherit")

    def __init__(self, address: str) -> None:
        self.kind, self.target = parse_address(address)
        self._sock: Any = None

    def prepare(self) -> None:
        pass

    def connect(self) -> Any:
        import socket as _socket

        if self.kind == "unix":
            sock = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
            sock.connect(self.target)
        else:
            sock = _socket.create_connection(self.target)
        self._sock = sock
        return _SocketChannel(sock)

    async def open(self) -> Any:
        return await _socket_stream(self._sock)


class ListenTransport:
    """The worker listens and serves the first coordinator that connects.

    The bound address is printed to stdout as JSON before the accept, so a
    launcher that asked for an ephemeral port can learn which one it got.
    """

    stdio_defaults = ("inherit", "inherit", "inherit")

    def __init__(self, address: str) -> None:
        self.kind, self.target = parse_address(address)
        self._sock: Any = None

    def prepare(self) -> None:
        pass

    def connect(self) -> Any:
        import socket as _socket

        if self.kind == "unix":
            listener = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
            listener.bind(self.target)
            bound: Any = self.target
        else:
            host, port = self.target
            listener = _socket.create_server((host, port))
            bound = list(listener.getsockname()[:2])
        listener.listen(1)
        print(json.dumps({"listening": bound}), flush=True)
        try:
            sock, _peer = listener.accept()
        finally:
            listener.close()
        self._sock = sock
        return _SocketChannel(sock)

    async def open(self) -> Any:
        return await _socket_stream(self._sock)


#: pure-async worker profiles: the profile *names the library* the exec'd
#: source may import and await, which is why there is one per library
#: rather than one "async" that follows whatever the worker happens to run.
ASYNC_PROFILES = {"trio": "trio", "asyncio": "asyncio"}


def run_loop(backend: str, async_fn: Any) -> None:
    """Run ``async_fn`` as the whole program, on ``backend``."""
    if backend == "trio":
        import trio

        trio.run(async_fn)
        return
    import asyncio

    asyncio.run(async_fn())


def build_engine(name: str) -> Any:
    """The engine a worker serves its protocol on.

    Mirrors the coordinator's choice (:func:`execnet._engine.pick_backend`):
    trio when it is installed and gevent has not patched the world underneath
    it, asyncio otherwise.
    """
    from ._engine import pick_backend

    backend = pick_backend()
    if backend == "trio":
        from . import _trio_engine

        return _trio_engine.TrioEngine(name=name)
    from . import _asyncio_engine

    return _asyncio_engine.AsyncioEngine(name=name)


def serve_worker(
    transport: Transport,
    *,
    stdin: str | None = None,
    stdout: str | None = None,
    stderr: str | None = None,
) -> None:
    """Handshake over ``transport``, serve the one gateway it configures, exit.

    ``transport.prepare()`` must already have run (the CLI does it before
    anything touches fd 0/1).  Everything after that is driven by the
    coordinator's config frame: it decides this worker's id, profile, wait
    backend, working directory, environment and stdio -- and a worker that
    will not serve answers *on the wire*, so the reason reaches the person
    who asked for the gateway rather than a stderr nobody is reading.
    """
    channel = transport.connect()
    config = read_config_frame(channel)

    refusal = _version_refusal(config["coordinator_version"], config.get("env"))
    if refusal is not None:
        send_ready_frame(channel, error=refusal)
        raise SystemExit(f"execnet worker: {refusal}")

    _apply_worker_setup(config)
    defaults = transport.stdio_defaults
    # explicit CLI flags win over the spec's, which win over the transport's
    apply_stdio(
        stdin=_first(stdin, config.get("stdin"), defaults[0]),
        stdout=_first(stdout, config.get("stdout"), defaults[1]),
        stderr=_first(stderr, config.get("stderr"), defaults[2]),
    )

    id = config["id"]
    # "execmodel" is the pre-3.0 spelling; accept both so a version-skewed
    # coordinator still connects.
    profile = effective_profile(config.get("profile") or config["execmodel"])
    wait: WaitBackend = config.get("wait", "thread")

    import execnet

    send_ready_frame(
        channel,
        execnet=execnet.__version__,
        pid=os.getpid(),
        profile=profile,
        executable=sys.executable,
    )

    if profile in ASYNC_PROFILES:
        # pure-async profile: one thread, the loop owns the main thread, and
        # the profile names the library the exec'd source may use
        async def main() -> None:
            await _serve_async_worker(await transport.open(), id)

        run_loop(ASYNC_PROFILES[profile], main)
        os._exit(0)

    engine = build_engine(name=f"execnet-worker-{id}")
    engine.start()
    io = engine.call(transport.open)
    _run_worker(engine, io, id, get_execmodel(profile), wait)


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


def _version_refusal(
    coordinator_version: str, env: Mapping[str, str] | None = None
) -> str | None:
    """Why this worker will not serve that coordinator, or None to proceed.

    A (major/minor) execnet difference is refused; a patch-level one is
    tolerated.  For same-interpreter popen the versions are always
    identical; this guards the remote paths, where the worker runs whatever
    execnet its own environment has.

    The wire protocol is deliberately unversioned, so a skew has no defined
    behaviour.  Refusing is the only honest answer, and the reason is
    returned rather than raised because it goes back over the handshake --
    the one moment there is still a channel to explain on.  Set
    :data:`IGNORE_VERSION_SKEW` (``popen//env:EXECNET_IGNORE_VERSION_SKEW=1``
    reaches this) to downgrade it to the warning it used to be.
    """
    import execnet

    ours = _rough_version(execnet.__version__)
    theirs = _rough_version(coordinator_version)
    if not (ours and theirs and ours != theirs):
        return None
    versions = f"coordinator {coordinator_version}, worker {execnet.__version__}"
    if _skew_ignored(env or {}):
        sys.stderr.write(f"WARNING: execnet version mismatch: {versions}\n")
        sys.stderr.flush()
        return None
    return (
        f"version mismatch: {versions}. The protocol is not compatible across "
        "major/minor versions -- install a matching execnet in that "
        f"environment, or set {IGNORE_VERSION_SKEW}=1 to continue anyway."
    )


def _skew_ignored(env: Mapping[str, str]) -> bool:
    """Whether the worker was told to tolerate a version skew.

    Read from the config's ``env:`` values as well as the process
    environment, because the config ones are not applied until
    :func:`_apply_worker_setup`, which runs after the check.
    """
    value = env.get(IGNORE_VERSION_SKEW, os.environ.get(IGNORE_VERSION_SKEW, ""))
    return value not in ("", "0")
