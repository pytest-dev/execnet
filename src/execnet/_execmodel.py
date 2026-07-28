"""Deprecated execution-model presets.

The machinery behind execution models was retired during the Trio port; what
survives is the preset *name*, which maps onto the worker config axes
(``loop=`` / ``exec=`` / ``wait=``).  Kept because pytest-xdist's remote worker
still builds its test queue on ``channel.gateway.execmodel.RLock()``/``Event()``.
"""

from __future__ import annotations

import os
import threading


class ExecModel:
    """Deprecated preset name for an execution model.

    The machinery behind execution models was retired: protocol IO always
    runs on the Trio host and blocking waits go through the boundary kit's
    wakeners (``execnet._boundary``); the name maps onto the worker config
    axes (``loop=`` / ``exec=`` / ``wait=``).  The stdlib-delegating
    members stay for API compatibility (pytest-xdist builds its test queue
    on ``execmodel.RLock``/``Event``) -- every preset is thread-shaped.
    """

    def __init__(self, backend: str) -> None:
        self.backend = backend

    def __repr__(self) -> str:
        return "<ExecModel %r>" % self.backend

    @property
    def queue(self):
        import queue

        return queue

    @property
    def subprocess(self):
        import subprocess

        return subprocess

    @property
    def socket(self):
        import socket

        return socket

    def get_ident(self) -> int:
        import _thread

        return _thread.get_ident()

    def sleep(self, delay: float) -> None:
        import time

        time.sleep(delay)

    def start(self, func, args=()) -> None:
        import _thread

        _thread.start_new_thread(func, args)

    def fdopen(self, fd, mode, bufsize=1, closefd=True):
        return os.fdopen(fd, mode, bufsize, encoding="utf-8", closefd=closefd)

    def Lock(self):
        return threading.RLock()

    def RLock(self):
        return threading.RLock()

    def Event(self) -> threading.Event:
        return threading.Event()


#: worker profiles: where exec'd code runs relative to the protocol loop
EXECMODEL_PROFILES = (
    "thread",  # hybrid: primary on the main thread, overflow on pool threads
    "main_thread_only",  # exec serialized on the main thread (GUI/pytest)
    "trio",  # pure async: loop owns the main thread, async sources as tasks
    "gevent",  # greenlets on a main-thread hub, one per remote_exec
)


def get_execmodel(backend: str | ExecModel) -> ExecModel:
    if isinstance(backend, ExecModel):
        return backend
    if backend in EXECMODEL_PROFILES:
        return ExecModel(backend)
    raise ValueError(f"unknown execmodel {backend!r}")
