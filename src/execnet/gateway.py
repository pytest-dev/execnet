"""Deprecated alias for :mod:`execnet._gateway`.

``Gateway`` is exported from :mod:`execnet` and :mod:`execnet.sync`; use those.
"""

from __future__ import annotations

from ._shim import forwarder

_MOVED = {
    "Gateway": "._gateway",
    "RInfo": "._gateway",
    "RemoteStatus": "._gateway",
    "normalize_exec_source": "._exec_source",
    "_find_non_builtin_globals": "._exec_source",
    "_source_of_function": "._exec_source",
}

__getattr__ = forwarder("gateway", _MOVED)
