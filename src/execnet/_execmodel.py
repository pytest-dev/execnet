"""Worker profiles, and the deprecated ExecModel shim.

The machinery behind "execution models" was retired during the Trio port.
What the name actually selected -- where exec'd code runs relative to the
worker's protocol loop -- survives as the ``profile=`` spec key and
:data:`WORKER_PROFILES`.

:class:`ExecModel` itself is kept only because pytest-xdist's remote worker
builds its test queue on ``channel.gateway.execmodel.RLock()``/``Event()``.
"""

from __future__ import annotations

import os
import threading
import warnings


class ExecModel:
    """Deprecated preset name for an execution model.

    The machinery behind execution models was retired: protocol IO always
    runs on the Trio host and blocking waits go through the boundary kit's
    wakeners (``execnet._boundary``).  What the name selected survives as
    the ``profile=`` spec key (:data:`WORKER_PROFILES`).  The
    stdlib-delegating members stay for API compatibility (pytest-xdist
    builds its test queue on ``execmodel.RLock``/``Event``) -- every preset
    is thread-shaped.
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
WORKER_PROFILES = (
    "thread",  # hybrid: primary on the main thread, overflow on pool threads
    "trio",  # pure async: loop owns the main thread, async sources as tasks
    "gevent",  # greenlets on a main-thread hub, one per remote_exec
)


#: profiles kept as accepted spellings, mapped to what they now select.
#: ``main_thread_only`` predates the restored hybrid ``thread`` profile,
#: which already hands the first remote_exec the real main thread -- the
#: GUI/signal property it existed for.  What it additionally did was refuse
#: a *second* concurrent remote_exec instead of overflowing to a pool
#: thread; that guard is gone.
DEPRECATED_PROFILES = {"main_thread_only": "thread"}


def effective_profile(name: str) -> str:
    """Validate a profile name and map deprecated spellings, silently.

    For the places that *consume* a profile (worker config, strategy
    lookup).  :func:`resolve_profile` is the one that warns, and is called
    once where the value enters.
    """
    replacement = DEPRECATED_PROFILES.get(name)
    if replacement is not None:
        return replacement
    if name not in WORKER_PROFILES:
        raise ValueError(f"unknown profile {name!r} (known: {list(WORKER_PROFILES)})")
    return name


def resolve_profile(name: str) -> str:
    """Validate a ``profile=`` value, warning about deprecated spellings.

    Callers that hold a caller-supplied :class:`~execnet._xspec.XSpec` must
    *not* write the result back onto it: pytest-xdist reuses a spec object
    across gateways and re-reads ``spec.execmodel`` to decide whether it
    still needs prefixing, so normalizing the value it set makes the second
    use build a spec with a duplicate key.  Validate here, and map with
    :func:`effective_profile` where the value is used.
    """
    replacement = DEPRECATED_PROFILES.get(name)
    if replacement is not None:
        warnings.warn(
            f"the {name!r} worker profile is deprecated and now behaves like"
            f" {replacement!r}, which already runs the first remote_exec on"
            " the worker's main thread. A second concurrent remote_exec no"
            " longer fails -- it runs on a pool thread.",
            DeprecationWarning,
            stacklevel=3,
        )
        return replacement
    if name not in WORKER_PROFILES:
        raise ValueError(f"unknown profile {name!r} (known: {list(WORKER_PROFILES)})")
    return name


def get_execmodel(backend: str | ExecModel) -> ExecModel:
    """Deprecated: build the xdist-facing shim for a profile name."""
    if isinstance(backend, ExecModel):
        return backend
    if backend in WORKER_PROFILES or backend in DEPRECATED_PROFILES:
        return ExecModel(backend)
    raise ValueError(f"unknown profile {backend!r}")
