"""Worker-side Trio networking entry for popen/import bootstrap."""

from __future__ import annotations

import os
import sys

from . import gateway_base
from .gateway_base import WorkerGateway
from .gateway_base import get_execmodel
from .gateway_base import trace


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

    # Protocol read end: former stdin (fed by coordinator stdout write / our stdin)
    read_fd = os.dup(0)
    fd = os.open(devnull, os.O_RDONLY)
    os.dup2(fd, 0)
    os.close(fd)

    # Protocol write end: former stdout
    write_fd = os.dup(1)
    fd = os.open(devnull, os.O_WRONLY)
    os.dup2(fd, 1)

    if os.name == "nt":
        # Match init_popen_io: keep a stderr handle then point fd 2 at null.
        sys.stderr = os.fdopen(os.dup(2), "w", 1)
        os.dup2(fd, 2)
    os.close(fd)

    # Replace sys.stdin/out with the null fds (closefd=False).
    sys.stdin = os.fdopen(0, "r", 1, closefd=False)
    sys.stdout = os.fdopen(1, "w", 1, closefd=False)
    return read_fd, write_fd


class _WorkerIOStub:
    """Minimal IO stub so WorkerGateway can be constructed without sync pipes."""

    def __init__(self, execmodel: gateway_base.ExecModel) -> None:
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
    """Serve a WorkerGateway with Message IO on a Trio host thread."""
    from . import _trio_host

    model = get_execmodel(execmodel)
    read_fd, write_fd = _prepare_protocol_fds()
    # Keep the historic trace token so tests looking for workergateway still pass.
    trace(f"creating workergateway on trio id={id!r}")

    host = _trio_host.TrioHost(name=f"execnet-trio-worker-{id}")
    host.start()
    try:
        io_stub = _WorkerIOStub(model)
        gateway = WorkerGateway(io=io_stub, id=id, _startcount=2)

        hasprimary = model.backend in ("thread", "main_thread_only")
        gateway._execpool = gateway_base.WorkerPool(model, hasprimary=hasprimary)
        gateway._executetask_complete = None
        if model.backend == "main_thread_only":
            gateway._executetask_complete = model.Event()
            gateway._executetask_complete.set()

        async def _start() -> _trio_host.ProtocolSession:
            async_io = _trio_host.FdStreamsIO(read_fd, write_fd)
            return await host.start_session(gateway, async_io)

        session = host.call(_start)
        gateway._attach_trio_session(session)

        try:
            if hasprimary:
                trace("integrating as primary thread (trio worker)")
                gateway._execpool.integrate_as_primary_thread()
            gateway.join()
        except KeyboardInterrupt:
            # Match WorkerGateway.serve(): swallow in the worker.
            trace("swallowing keyboardinterrupt, serve finished")
    finally:
        host.stop(timeout=5.0)
        # Trio's to_thread cache uses non-daemon threads that would otherwise
        # keep this disposable worker process alive after serve returns.
        os._exit(0)
