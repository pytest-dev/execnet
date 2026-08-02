"""execnet embedded in your own trio run: no engine, no thread.

The gateways here are tasks in *your* nursery, and their protocol IO runs
on *your* loop::

    import trio
    import execnet.raw_trio

    async def main():
        async with execnet.raw_trio.AsyncGroup() as group:
            gateway = await group.makegateway("popen")
            channel = await gateway.remote_exec("channel.send(6 * 7)")
            print(await channel.receive())

    trio.run(main)

That is the whole difference from :mod:`execnet.trio`, and it cuts both
ways.  Cancelling a ``receive`` here cancels exactly that receive, with no
window in which an item is taken and lost; there is no thread hop on any
operation; and structured concurrency covers the gateways like anything
else in your nursery.  In exchange, a step that does not yield -- a
CPU-bound stretch, a blocking call -- stalls protocol IO for every gateway
you have, an error in your task tree cancels gateways mid-protocol, and
execnet's own ``to_thread`` work (reading files for a transfer) competes
with yours for one run-wide thread limiter.  A gateway also cannot outlive
the ``async with`` that made it.

Reach for :mod:`execnet.trio` instead when any of that bites, or when the
same process also drives execnet from blocking or asyncio code: the
engine is shared, this is not.

Transfers and deployments are awaited here too -- ``await transfer(...)``,
``await deploy(deployment, gateway)`` -- and a fan-out across gateways runs
them concurrently.

The error types are shared with the blocking API in :mod:`execnet.sync`.
Items you send must already be simple builtin data (plus channels); the
standalone serializer is intentionally not part of the public API --
``execnet.can_send`` checks a value before you send it; see ``DumpError``.
"""

from ._deploy import Deployed
from ._deploy import Deployment
from ._deploy._async_api import deploy
from ._deploy._async_api import deploy_all
from ._deploy._async_api import transfer
from ._errors import DataFormatError
from ._errors import DumpError
from ._errors import HostNotFound
from ._errors import LoadError
from ._errors import RemoteError
from ._errors import TimeoutError
from ._trio_gateway import AsyncChannel
from ._trio_gateway import AsyncGateway
from ._trio_gateway import AsyncGroup
from ._trio_gateway import open_gateway
from ._xspec import XSpec

__all__ = [
    "AsyncChannel",
    "AsyncGateway",
    "AsyncGroup",
    "DataFormatError",
    "Deployed",
    "Deployment",
    "DumpError",
    "HostNotFound",
    "LoadError",
    "RemoteError",
    "TimeoutError",
    "XSpec",
    "deploy",
    "deploy_all",
    "open_gateway",
    "transfer",
]
