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

The serialization helpers and error types are shared with the blocking
API in :mod:`execnet.sync`.
"""

from ._trio_gateway import AsyncChannel
from ._trio_gateway import AsyncGateway
from ._trio_gateway import AsyncGroup
from ._trio_gateway import ByteStream
from ._trio_gateway import RawChannel
from ._trio_gateway import RawChannelStream
from ._trio_gateway import open_popen_gateway
from ._trio_gateway import serve_gateway
from .gateway_base import DataFormatError
from .gateway_base import DumpError
from .gateway_base import HostNotFound
from .gateway_base import LoadError
from .gateway_base import RemoteError
from .gateway_base import TimeoutError
from .gateway_base import dump
from .gateway_base import dumps
from .gateway_base import load
from .gateway_base import loads
from .xspec import XSpec

__all__ = [
    "AsyncChannel",
    "AsyncGateway",
    "AsyncGroup",
    "ByteStream",
    "DataFormatError",
    "DumpError",
    "HostNotFound",
    "LoadError",
    "RawChannel",
    "RawChannelStream",
    "RemoteError",
    "TimeoutError",
    "XSpec",
    "dump",
    "dumps",
    "load",
    "loads",
    "open_popen_gateway",
    "serve_gateway",
]
