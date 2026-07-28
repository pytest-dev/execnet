"""Deprecated alias for :mod:`execnet._xspec`.

``XSpec`` is exported from :mod:`execnet`, :mod:`execnet.sync`,
:mod:`execnet.trio` and :mod:`execnet.aio`; use those.
"""

from __future__ import annotations

from ._shim import forwarder

_MOVED = {"XSpec": "._xspec"}

__getattr__ = forwarder("xspec", _MOVED)
