"""
execnet
-------

pure python lib for connecting to local and remote Python Interpreters.

Four public namespaces:

* :mod:`execnet.sync` — the blocking API; the top-level ``execnet.*``
  names below are aliases into it.
* :mod:`execnet.trio` — the trio-native API, awaited inside your own
  ``trio.run``.
* :mod:`execnet.aio` — the asyncio-native API, bridged over a Trio
  host thread.
* :mod:`execnet.portal` — cross-thread / cross-loop communication
  primitives shared by all of them.

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
]


#: resolved lazily so ``import execnet`` does not load the trio event loop
#: machinery, plus the deprecated pre-Trio module names -- those used to be
#: reachable here only because the import chain pulled them in, and callers
#: that still do ``execnet.gateway_base.X`` must reach the warning shim.
_LAZY_MODULES = (
    "aio",
    "portal",
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


def __getattr__(name: str) -> Any:
    if name in _LAZY_MODULES:
        import importlib

        return importlib.import_module(f".{name}", __name__)
    if name in _XDIST_COMPAT:
        import importlib
        import warnings

        warnings.warn(
            f"execnet.{name} is a temporary pytest-xdist compatibility shim and"
            " will be removed; the standalone serializer is not public. Use"
            " execnet.can_send(obj) to test whether a value can cross a channel.",
            DeprecationWarning,
            stacklevel=2,
        )
        return getattr(importlib.import_module("._serialize", __name__), name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
