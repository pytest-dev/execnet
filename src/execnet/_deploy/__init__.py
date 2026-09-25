"""Transfer and deployment: an independent layer over the protocol core.

Nothing in ``execnet``'s protocol core knows this package exists.  It
reaches workers through one generic mechanism -- a ``GATEWAY_SERVICE``
request naming a service the worker resolves through
:mod:`execnet._services` -- which is the same door an out-of-tree package
would use.  Deleting this directory and its two registry lines would leave
a working execnet behind.

What it provides:

* :class:`Deployment` / :class:`Deployed` -- a frozen ``uv`` environment,
  the project's own wheel installed into it, and the roots the wheel does
  not carry, put on a host *before* any worker runs against it.
* :func:`transfer` -- send a tree to a host, sending only what differs.

The blocking entry points here are facades: the driver is async and runs
on the host loop, so a fan-out across hosts is concurrent.  The same driver
is what :mod:`execnet.trio` and :mod:`execnet.aio` await directly.
"""

from ._api import Deployed
from ._api import Deployment
from ._api import transfer

__all__ = ["Deployed", "Deployment", "transfer"]
