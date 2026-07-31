"""Machinery for the deprecated pre-Trio module names.

``execnet.gateway_base``, ``execnet.gateway``, ``execnet.multi``,
``execnet.rsync``, ``execnet.rsync_remote`` and ``execnet.xspec`` were never
part of a documented public API -- they were importable only because
``import execnet`` pulled them in transitively.  Their contents now live in
private modules grouped by concern; each old name survives as a thin module
whose ``__getattr__`` warns and forwards.

The supported surfaces are :mod:`execnet` / :mod:`execnet.sync`,
:mod:`execnet.trio`, :mod:`execnet.aio` and :mod:`execnet.gevent`.
"""

from __future__ import annotations

import importlib
import warnings
from typing import Any

#: shims are scheduled for removal in this release -- later in the 3.x
#: series, once the consumers that still import these names (pytest-xdist
#: above all) have released a version that does not.
REMOVED_IN = "a later execnet 3.x release"


def forwarder(shim: str, moved: dict[str, str]) -> Any:
    """Build the ``__getattr__`` for a deprecated module.

    ``moved`` maps each previously-reachable name to the private module that
    now defines it (a relative name such as ``"._channel"``).
    """

    def __getattr__(name: str) -> Any:
        try:
            module = moved[name]
        except KeyError:
            raise AttributeError(
                f"module 'execnet.{shim}' has no attribute {name!r}"
            ) from None
        warnings.warn(
            f"execnet.{shim} is private and will be removed in {REMOVED_IN}; "
            f"{name} now lives in execnet{module}. The supported surfaces are "
            f"execnet, execnet.sync, execnet.trio, execnet.aio and execnet.gevent.",
            DeprecationWarning,
            stacklevel=2,
        )
        return getattr(importlib.import_module(module, __package__), name)

    return __getattr__
