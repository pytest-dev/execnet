"""
execnet
-------

pure python lib for connecting to local and remote Python Interpreters.

One namespace per concurrency library you drive execnet from:

* :mod:`execnet.sync` — the blocking API for plain threads; the top-level
  ``execnet.*`` names below are aliases into it.
* :mod:`execnet.trio` — the trio-native API, awaited inside your own
  ``trio.run``.
* :mod:`execnet.aio` — the asyncio-native API.
* :mod:`execnet.gevent` — the blocking API with greenlet-parking waits.

:mod:`execnet.trio` is the only one that runs gateways *directly* as tasks
in your own nursery.  The other three drive a shared Trio host thread, so
their blocking calls must not be made from inside a running event loop.

``can_send`` sits here rather than on any one of them: the wire-format
contract is the same whichever surface you drive a gateway from.

(c) 2012, Holger Krekel and others
"""

from typing import Any

from ._serialize import can_send
from ._version import version as __version__
from .sync import Channel
from .sync import DataFormatError
from .sync import DumpError
from .sync import Gateway
from .sync import Group
from .sync import HostNotFound
from .sync import LoadError
from .sync import MultiChannel
from .sync import RemoteError
from .sync import RSync
from .sync import TimeoutError
from .sync import XSpec
from .sync import default_group
from .sync import makegateway
from .sync import set_execmodel
from .sync import set_profile

__all__ = [
    "Channel",
    "DataFormatError",
    "DumpError",
    "Gateway",
    "Group",
    "HostNotFound",
    "LoadError",
    "MultiChannel",
    "RSync",
    "RemoteError",
    "TimeoutError",
    "XSpec",
    "__version__",
    "can_send",
    "default_group",
    "makegateway",
    "set_execmodel",
    "set_profile",
]


#: resolved lazily so ``import execnet`` does not load the trio event loop
#: machinery, plus the deprecated pre-Trio module names -- those used to be
#: reachable here only because the import chain pulled them in, and callers
#: that still do ``execnet.gateway_base.X`` must reach the warning shim.
_LAZY_MODULES = (
    "aio",
    "gevent",
    "trio",
    "gateway",
    "gateway_base",
    "multi",
    "rsync",
    "rsync_remote",
    "xspec",
)


#: TEMPORARY pytest-xdist compatibility.  ``xdist/remote.py`` probes
#: serializability with ``try: execnet.dumps(x) / except execnet.DumpError``
#: before shipping warning args and report attrs.  The standalone serializer
#: is internal and :func:`can_send` replaces that probe, but dropping the name
#: outright breaks every released xdist, so it stays reachable -- warning, and
#: deliberately absent from ``__all__``.
#:
#: FOLLOW-UP (after the execnet release): port xdist to ``execnet.can_send``,
#: then delete this and its test.  Nothing else may be added here.
_XDIST_COMPAT = ("dumps",)

#: Warn about the shim at most once per process.  xdist reaches it from
#: ``serialize_warning_message``, i.e. from inside pytest's
#: warning-recording hook: warning every time makes recording one warning
#: record another, which records another -- an unbounded loop that wedges
#: the worker.  Once is also simply the right amount of noise for a name
#: whose caller is a library, not the user.
_xdist_compat_warned: set[str] = set()


def __getattr__(name: str) -> Any:
    if name in _LAZY_MODULES:
        import importlib

        return importlib.import_module(f".{name}", __name__)
    if name in _XDIST_COMPAT:
        import importlib

        if name not in _xdist_compat_warned:
            import warnings

            _xdist_compat_warned.add(name)
            warnings.warn(
                f"execnet.{name} is a temporary pytest-xdist compatibility shim"
                " and will be removed; the standalone serializer is not public."
                " Use execnet.can_send(obj) to test whether a value can cross a"
                " channel.",
                DeprecationWarning,
                stacklevel=2,
            )
        return getattr(importlib.import_module("._serialize", __name__), name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
