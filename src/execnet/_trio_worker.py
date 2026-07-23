"""Worker-side Trio networking and exec scheduling for popen/import bootstrap."""

from __future__ import annotations

import os
import queue
import sys
import threading
from typing import TYPE_CHECKING
from typing import Any

import trio

from .gateway_base import MAIN_THREAD_ONLY_DEADLOCK_TEXT
from .gateway_base import WorkerGateway
from .gateway_base import get_execmodel
from .gateway_base import loads_internal
from .gateway_base import trace

if TYPE_CHECKING:
    from . import _trio_host
    from .gateway_base import Channel
    from .gateway_base import ExecModel

ExecItem = tuple[Any, ...]


class TrioWorkerExec:
    """Schedule ``remote_exec`` work from the Trio host nursery.

    * ``thread``: run ``executetask`` via ``trio.to_thread`` (concurrent).
    * ``main_thread_only``: hand off to the process main thread (GUI-safe).
    """

    def __init__(
        self,
        host: _trio_host.TrioHost,
        gateway: WorkerGateway,
        *,
        main_thread_only: bool,
    ) -> None:
        self.host = host
        self.gateway = gateway
        self.main_thread_only = main_thread_only
        self._lock = threading.Lock()
        self._running = 0
        self._shutting_down = False
        self._idle = threading.Event()
        self._idle.set()
        self._primary_q: queue.SimpleQueue[
            tuple[Channel, ExecItem, threading.Event] | None
        ] = queue.SimpleQueue()
        self._primary_wake = threading.Event()
        # Serialize main_thread_only admission (wait+clear) so two tasks cannot
        # both observe the idle Event before either clears it.
        self._admit_lock = trio.Lock()

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
        """Called from the Trio receiver while holding ``_receivelock``.

        Must not block: deadlock checks and exec run in a nursery task.
        """
        item = loads_internal(sourcetask)
        assert isinstance(item, tuple)
        with self._lock:
            if self._shutting_down:
                channel.close("execution disallowed")
                return
        # Already on the Trio host thread (Message handler).
        self.host.start_soon(self._run_exec, channel, item)

    async def _run_exec(self, channel: Channel, item: ExecItem) -> None:
        if self.main_thread_only:
            complete = self.gateway._executetask_complete
            assert complete is not None

            def _wait_slot() -> bool:
                return complete.wait(timeout=1)

            async with self._admit_lock:
                if not await trio.to_thread.run_sync(
                    _wait_slot, abandon_on_cancel=True
                ):
                    channel.close(MAIN_THREAD_ONLY_DEADLOCK_TEXT)
                    return
                complete.clear()

        self._track_start()
        try:
            if self.main_thread_only:
                done = threading.Event()
                self._primary_q.put((channel, item, done))
                self._primary_wake.set()
                await trio.to_thread.run_sync(done.wait, abandon_on_cancel=True)
            else:
                await trio.to_thread.run_sync(
                    self.gateway.executetask,
                    (channel, item),
                    abandon_on_cancel=True,
                )
        finally:
            self._track_finish()

    def integrate_as_primary_thread(self) -> None:
        """Block the main thread running main_thread_only exec tasks."""
        while True:
            self._primary_wake.wait()
            try:
                task = self._primary_q.get_nowait()
            except queue.Empty:
                self._primary_wake.clear()
                try:
                    task = self._primary_q.get_nowait()
                except queue.Empty:
                    continue
            if task is None:
                break
            channel, item, done = task
            try:
                self.gateway.executetask((channel, item))
            finally:
                done.set()

    def trigger_shutdown(self) -> None:
        with self._lock:
            self._shutting_down = True
        self._primary_q.put(None)
        self._primary_wake.set()

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


def serve_popen_trio(id: str, execmodel: str = "thread") -> None:
    """Serve a WorkerGateway with Message IO + exec scheduling on Trio."""
    from . import _trio_host

    model = get_execmodel(execmodel)
    read_fd, write_fd = _prepare_protocol_fds()
    # Bootstrap handshake: the coordinator waits for this byte on our stdout
    # before starting the Message protocol.  We are launched as a plain module
    # (``python -m execnet._trio_worker``), so nothing was sent to bootstrap us.
    os.write(write_fd, b"1")
    # Keep the historic trace token so tests looking for workergateway still pass.
    trace(f"creating workergateway on trio id={id!r}")

    host = _trio_host.TrioHost(name=f"execnet-trio-worker-{id}")
    host.start()
    try:
        io_stub = _WorkerIOStub(model)
        gateway = WorkerGateway(io=io_stub, id=id, _startcount=2)

        main_thread_only = model.backend == "main_thread_only"
        trio_exec = TrioWorkerExec(host, gateway, main_thread_only=main_thread_only)
        # Duck-type as WorkerPool for STATUS / _terminate_execution.
        gateway._execpool = trio_exec  # type: ignore[assignment]
        gateway._trio_exec = trio_exec
        gateway._executetask_complete = None
        if main_thread_only:
            gateway._executetask_complete = model.Event()
            gateway._executetask_complete.set()

        async def _start() -> _trio_host.ProtocolSession:
            async_io = _trio_host.FdStreamsIO(read_fd, write_fd)
            return await host.start_session(gateway, async_io)

        session = host.call(_start)
        gateway._attach_trio_session(session)

        try:
            if main_thread_only:
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
    """Entry point for ``python -m execnet._trio_worker <config-json>``.

    ``<config-json>`` is the coordinator's ``_provision.worker_cli_arg`` payload:
    ``{"id", "execmodel", "coordinator_version"}``.  The worker imports execnet +
    trio from the environment; no source is sent over the wire to bootstrap it.
    """
    import json

    config = json.loads(sys.argv[1])
    _check_version(config["coordinator_version"])
    serve_popen_trio(id=config["id"], execmodel=config["execmodel"])


if __name__ == "__main__":
    _main()
