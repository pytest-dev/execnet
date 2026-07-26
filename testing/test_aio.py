"""The asyncio-native API: execnet.aio bridged over the Trio host."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from typing import Any
from typing import TypeVar

import pytest

import execnet.aio

T = TypeVar("T")

TESTTIMEOUT = 30.0


def run(main: Awaitable[T]) -> T:
    return asyncio.run(asyncio.wait_for(main, TESTTIMEOUT))


def test_popen_roundtrip() -> None:
    async def main() -> None:
        async with execnet.aio.Group() as group:
            gateway = await group.makegateway("popen")
            channel = await gateway.remote_exec("channel.send(channel.receive() + 1)")
            await channel.send(41)
            assert await channel.receive() == 42
            await channel.wait_closed()

    run(main())


def test_open_popen_gateway_iteration() -> None:
    async def main() -> list[int]:
        async with execnet.aio.open_popen_gateway() as gateway:
            channel = await gateway.remote_exec(
                "for i in range(4): channel.send(i * 2)"
            )
            return [item async for item in channel]

    assert run(main()) == [0, 2, 4, 6]


def test_receive_timeout() -> None:
    async def main() -> None:
        async with execnet.aio.open_popen_gateway() as gateway:
            channel = await gateway.remote_exec("channel.receive()")
            with pytest.raises(channel.TimeoutError):
                await channel.receive(timeout=0.05)
            await channel.send(None)

    run(main())


def test_remote_error() -> None:
    async def main() -> None:
        async with execnet.aio.open_popen_gateway() as gateway:
            channel = await gateway.remote_exec("raise ValueError(17)")
            with pytest.raises(execnet.aio.RemoteError, match="ValueError"):
                await channel.receive()

    run(main())


def test_channel_passing_wraps_aio() -> None:
    async def main() -> None:
        async with execnet.aio.open_popen_gateway() as gateway:
            channel = await gateway.remote_exec(
                """
                c = channel.gateway.newchannel()
                channel.send(c)
                c.send(42)
                """
            )
            passed = await channel.receive()
            assert isinstance(passed, execnet.aio.Channel)
            assert await passed.receive() == 42

    run(main())


def test_multiple_gateways_and_send_each() -> None:
    async def main() -> list[Any]:
        async with execnet.aio.Group() as group:
            gateways = [await group.makegateway("popen") for _ in range(2)]
            channels = [
                await gw.remote_exec("channel.send(channel.receive() * 2)")
                for gw in gateways
            ]
            for i, channel in enumerate(channels):
                await channel.send(i + 1)
            return [await channel.receive() for channel in channels]

    assert run(main()) == [2, 4]


def test_group_not_entered() -> None:
    async def main() -> None:
        group = execnet.aio.Group()
        with pytest.raises(RuntimeError, match="not entered"):
            await group.makegateway("popen")

    run(main())


def test_terminate_gateway_explicitly() -> None:
    async def main() -> None:
        async with execnet.aio.Group() as group:
            gateway = await group.makegateway("popen")
            channel = await gateway.remote_exec("channel.send(1)")
            assert await channel.receive() == 1
            await gateway.terminate()

    run(main())
