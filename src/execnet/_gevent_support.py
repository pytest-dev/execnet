"""The gevent wait backend (``wait=gevent``).

Importing this module registers the ``gevent`` wakener factory; the
boundary kit imports it lazily when a spec asks for ``wait=gevent``.

A blocking wait then parks only the calling greenlet: the carrier's
event lives in the waiting greenlet's hub, and the loop side's
``notify()`` crosses threads through a ``loop.async_`` watcher -- the
one libev/libuv primitive gevent documents as safe to use from other
threads.
"""

from __future__ import annotations

import threading
from typing import Any

import gevent.event
from gevent.hub import get_hub

from ._boundary import register_wakener


class GeventWakener:
    """Wakener parking greenlets instead of OS threads.

    ``notify()`` is thread-safe and non-blocking (called from the trio
    host thread).  The hub-bound pieces (event + async watcher) are
    created lazily in the first waiter's hub, so construction is safe
    from any thread -- including the host loop, which creates channels
    for inbound ids.  Waiters must share one hub (one gevent thread per
    carrier), which is the ordinary gevent setup.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._notified = False
        self._event: gevent.event.Event | None = None
        self._watcher: Any = None

    def notify(self) -> None:
        with self._lock:
            self._notified = True
            watcher = self._watcher
        if watcher is not None:
            watcher.send()

    def _ensure(self) -> gevent.event.Event:
        """Create the hub-bound event/watcher in the waiter's context."""
        event = self._event
        if event is None:
            event = gevent.event.Event()
            watcher = get_hub().loop.async_()
            watcher.start(event.set)
            with self._lock:
                self._event = event
                self._watcher = watcher
        return event

    def wait(self, timeout: float | None = None) -> bool:
        event = self._ensure()
        with self._lock:
            # a notify may have fired before the watcher existed
            if self._notified and not event.is_set():
                event.set()
        return bool(event.wait(timeout))

    def clear(self) -> None:
        with self._lock:
            self._notified = False
            event = self._event
        if event is not None:
            event.clear()


register_wakener("gevent", GeventWakener)
