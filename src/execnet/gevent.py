"""The blocking execnet API for gevent applications.

Identical to :mod:`execnet.sync` except that every blocking wait parks the
calling *greenlet* rather than its OS thread::

    import execnet.gevent

    group = execnet.gevent.Group()
    gateway = group.makegateway("popen")
    channel = gateway.remote_exec("channel.send(6 * 7)")
    print(channel.receive())          # parks this greenlet, not the hub

Protocol IO runs on the shared :class:`~execnet.ProtocolEngine` as it does
for every
blocking surface; the difference is only which primitive a waiter parks
on, so a slow ``receive`` no longer stalls the whole hub.  Requires
gevent (``execnet[gevent]``).

This is about the *caller*: the worker's own shape is the ``profile=``
spec key, and ``profile=gevent`` is an independent choice.

Importing this module monkey-patches nothing, and **the process it runs in
must not have monkey-patched either**: the engine loop is a Trio program on
its own OS thread, and it needs the real ``select`` (for ``epoll``),
``socket``, ``thread`` and ``queue``, which ``gevent.monkey`` replaces
process-wide.  Patching is not what makes this namespace work anyway --
its waits park the calling greenlet because they wait on a gevent
primitive, not because the stdlib was swapped underneath them.  Starting a
an engine in a patched process is refused before the loop thread exists, with
an error naming what was patched.
"""

from __future__ import annotations

import gevent  # noqa: F401  -- fail at import time when gevent is missing

from ._errors import DataFormatError
from ._errors import DumpError
from ._errors import HostNotFound
from ._errors import LoadError
from ._errors import RemoteError
from ._errors import TimeoutError
from ._multi import Group as _SyncGroup
from ._multi import MultiChannel
from ._xspec import XSpec
from .sync import Channel
from .sync import Gateway
from .sync import ProtocolEngine
from .sync import RSync

__all__ = [
    "Channel",
    "DataFormatError",
    "DumpError",
    "Gateway",
    "Group",
    "HostNotFound",
    "LoadError",
    "MultiChannel",
    "ProtocolEngine",
    "RSync",
    "RemoteError",
    "TimeoutError",
    "XSpec",
    "default_group",
    "makegateway",
]


class Group(_SyncGroup):
    """A gateway group whose blocking waits park greenlets.

    Every gateway it creates inherits the gevent wait backend, so
    ``channel.receive()``, ``waitclose()``, sends waiting on their write
    acknowledgement, ``join()`` and ``Group.terminate()`` all yield to the
    hub instead of blocking the thread running it.
    """

    _wait_backend = "gevent"


#: convenience group for scripts; real applications should own a Group
default_group = Group()
makegateway = default_group.makegateway
