"""Deprecated alias for :mod:`execnet._rsync`.

``RSync`` is exported from :mod:`execnet` and :mod:`execnet.sync`; use those.
"""

from __future__ import annotations

from ._shim import forwarder

_MOVED = {"RSync": "._rsync"}

__getattr__ = forwarder("rsync", _MOVED)
