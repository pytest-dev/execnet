"""The asyncio-native API: execnet.aio bridged over the Trio host."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from typing import Any
from typing import TypeVar
from typing import cast

import pytest

import execnet.aio
from execnet._engine import default_engine

T = TypeVar("T")

TESTTIMEOUT = 30.0


def run(main: Awaitable[T]) -> T:
    return asyncio.run(asyncio.wait_for(main, TESTTIMEOUT))


def test_popen_roundtrip() -> None:
    async def main() -> None:
        async with execnet.aio.AsyncGroup() as group:
            gateway = await group.makegateway("popen")
            channel = await gateway.remote_exec("channel.send(channel.receive() + 1)")
            await channel.send(41)
            assert await channel.receive() == 42
            await channel.wait_closed()

    run(main())


def test_open_gateway_iteration() -> None:
    async def main() -> list[int]:
        async with execnet.aio.open_gateway() as gateway:
            channel = await gateway.remote_exec(
                "for i in range(4): channel.send(i * 2)"
            )
            return [cast("int", item) async for item in channel]

    assert run(main()) == [0, 2, 4, 6]


def test_receive_timeout() -> None:
    async def main() -> None:
        async with execnet.aio.open_gateway() as gateway:
            channel = await gateway.remote_exec("channel.receive()")
            with pytest.raises(channel.TimeoutError):
                await channel.receive(timeout=0.05)
            await channel.send(None)

    run(main())


def test_remote_error() -> None:
    async def main() -> None:
        async with execnet.aio.open_gateway() as gateway:
            channel = await gateway.remote_exec("raise ValueError(17)")
            with pytest.raises(execnet.aio.RemoteError, match="ValueError"):
                await channel.receive()

    run(main())


def test_channel_passing_wraps_aio() -> None:
    async def main() -> None:
        async with execnet.aio.open_gateway() as gateway:
            channel = await gateway.remote_exec(
                """
                c = channel.gateway.newchannel()
                channel.send(c)
                c.send(42)
                """
            )
            passed = await channel.receive()
            assert isinstance(passed, execnet.aio.AsyncChannel)
            assert await passed.receive() == 42

    run(main())


def test_multiple_gateways_and_send_each() -> None:
    async def main() -> list[Any]:
        async with execnet.aio.AsyncGroup() as group:
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
        group = execnet.aio.AsyncGroup()
        with pytest.raises(RuntimeError, match="not started"):
            await group.makegateway("popen")

    run(main())


def test_terminate_gateway_explicitly() -> None:
    async def main() -> None:
        async with execnet.aio.AsyncGroup() as group:
            gateway = await group.makegateway("popen")
            channel = await gateway.remote_exec("channel.send(1)")
            assert await channel.receive() == 1
            await gateway.terminate()

    run(main())


def test_cancelled_receive_does_not_consume_an_item() -> None:
    # The bridge cancels the host-side receive, so the item stays queued
    # instead of being consumed and dropped -- an asyncio timeout around a
    # receive must behave like passing timeout=.
    async def main() -> None:
        async with execnet.aio.open_gateway() as gateway:
            channel = await gateway.remote_exec(
                """
                channel.receive()
                for i in range(3):
                    channel.send(i)
                """
            )
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(channel.receive(), 0.05)
            # release the worker; nothing was consumed by the cancelled wait
            await channel.send("go")
            assert [await channel.receive() for _ in range(3)] == [0, 1, 2]

    run(main())


def test_cancelled_send_still_arrives() -> None:
    # send is shielded: the caller sees CancelledError but the item is on
    # the wire, rather than a frame half-written to the peer.
    async def main() -> None:
        async with execnet.aio.open_gateway() as gateway:
            channel = await gateway.remote_exec("channel.send(channel.receive() * 2)")
            task = asyncio.ensure_future(channel.send(21))
            await asyncio.sleep(0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert await channel.receive() == 42

    run(main())


def test_group_start_and_aclose_explicitly() -> None:
    # asyncio apps drive this from lifespan hooks rather than "async with"
    async def main() -> None:
        group = execnet.aio.AsyncGroup()
        await group.start()
        try:
            gateway = await group.makegateway("popen")
            channel = await gateway.remote_exec("channel.send(7)")
            assert await channel.receive() == 7
        finally:
            await group.aclose()
        await group.aclose()  # idempotent
        with pytest.raises(RuntimeError, match="not started"):
            await group.makegateway("popen")

    run(main())


def test_groups_share_the_default_engine() -> None:
    async def main() -> None:
        async with execnet.aio.AsyncGroup() as a, execnet.aio.AsyncGroup() as b:
            assert a.engine is b.engine
            assert a.engine is default_engine()

    run(main())


def test_an_item_taken_as_the_cancel_lands_comes_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cancelled receive never costs an item; see testing/test_bridge.py.

    The window is forced rather than raced for: the cancel is queued ahead
    of the engine's delivery on a FIFO loop, so the receiving task is
    cancelled with the item already produced and one callback away.
    """
    from execnet import _bridge

    async def main() -> None:
        async with execnet.aio.open_gateway() as gateway:
            channel = await gateway.remote_exec(
                "channel.receive()\nfor i in range(3): channel.send(i)"
            )
            await channel.send("go")

            loop = asyncio.get_running_loop()
            holder: list[Any] = []
            real = _bridge.AsyncioCarrier.resolve

            def hooked(self: object, result: object, error: object) -> None:
                loop.call_soon_threadsafe(holder[0].cancel)
                real(self, result, error)  # type: ignore[arg-type]

            monkeypatch.setattr(_bridge.AsyncioCarrier, "resolve", hooked)
            holder.append(asyncio.ensure_future(channel.receive()))
            with pytest.raises(asyncio.CancelledError):
                await holder[0]
            monkeypatch.undo()

            assert [await channel.receive() for _ in range(3)] == [0, 1, 2]

    run(main())
