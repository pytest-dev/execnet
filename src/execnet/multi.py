"""Deprecated alias for :mod:`execnet._multi`.

``Group``, ``MultiChannel``, ``default_group``, ``makegateway`` and
``set_execmodel`` are exported from :mod:`execnet` and :mod:`execnet.sync`;
use those.
"""

from __future__ import annotations

from ._shim import forwarder

_MOVED = {
    "Group": "._multi",
    "MultiChannel": "._multi",
    "default_group": "._multi",
    "makegateway": "._multi",
    "set_execmodel": "._multi",
    "safe_terminate": "._multi",
    "NO_ENDMARKER_WANTED": "._multi",
}

__getattr__ = forwarder("multi", _MOVED)
