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

(c) 2012, Holger Krekel and others
"""

from typing import Any

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
    "default_group",
    "makegateway",
    "set_execmodel",
]


def __getattr__(name: str) -> Any:
    # Lazy namespace modules: keep ``import execnet`` from loading the
    # trio event loop machinery until it is actually used.
    if name in ("aio", "portal", "trio"):
        import importlib

        return importlib.import_module(f".{name}", __name__)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
