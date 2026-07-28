"""Deprecated alias for :mod:`execnet._rsync_remote`.

The worker half of the rsync protocol; :class:`execnet.RSync` ships it to the
remote side itself, so there is no reason to reference this module.
"""

from __future__ import annotations

from ._shim import forwarder

_MOVED = {"serve_rsync": "._rsync_remote"}

__getattr__ = forwarder("rsync_remote", _MOVED)
