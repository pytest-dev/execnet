"""The trio-native execnet API.

Everything here is awaited directly inside your own ``trio.run`` — no
host thread, no blocking calls::

    import trio
    import execnet.trio

    async def main():
        async with execnet.trio.AsyncGroup() as group:
            gateway = await group.makegateway("popen")
            channel = await gateway.remote_exec("channel.send(6 * 7)")
            print(await channel.receive())

    trio.run(main)

The error types are shared with the blocking API in :mod:`execnet.sync`.
Items you send must already be simple builtin data (plus channels); the
standalone serializer is intentionally not part of the public API --
``execnet.can_send`` checks a value before you send it; see ``DumpError``.
"""

from ._errors import DataFormatError
from ._errors import DumpError
from ._errors import HostNotFound
from ._errors import LoadError
from ._errors import RemoteError
from ._errors import TimeoutError
from ._trio_gateway import AsyncChannel
from ._trio_gateway import AsyncGateway
from ._trio_gateway import AsyncGroup
from ._trio_gateway import open_popen_gateway
from ._xspec import XSpec

__all__ = [
    "AsyncChannel",
    "AsyncGateway",
    "AsyncGroup",
    "DataFormatError",
    "DumpError",
    "HostNotFound",
    "LoadError",
    "RemoteError",
    "TimeoutError",
    "XSpec",
    "open_popen_gateway",
]
