"""Protocol tests for the trio-native async gateway core (RawChannel level).

Two AsyncGateways are wired together over an in-memory stream pair (the
transport harness, as in ``TestFrameDecoder``); the assertions cover execnet
semantics: payload routing by channel id, the close/EOF/sendonly state
machine, RemoteError propagation, and gateway termination.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
import trio
import trio.testing

from execnet._trio_gateway import AsyncGateway
from execnet.gateway_base import Message
from execnet.gateway_base import RemoteError
from execnet.gateway_base import dumps_internal
from execnet.gateway_base import loads_internal


@asynccontextmanager
async def gateway_pair() -> AsyncIterator[tuple[AsyncGateway, AsyncGateway]]:
    left_stream, right_stream = trio.testing.memory_stream_pair()
    async with trio.open_nursery() as nursery:
        left = AsyncGateway(left_stream, id="left", _startcount=1)
        right = AsyncGateway(right_stream, id="right", _startcount=2)
        await nursery.start(left._serve)
        await nursery.start(right._serve)
        try:
            yield left, right
        finally:
            await left.aclose()
            await right.aclose()


def test_payload_boundaries_are_preserved() -> None:
    async def main() -> None:
        async with gateway_pair() as (left, right):
            sender = left.open_raw_channel()
            receiver = right.open_raw_channel(sender.id)
            await sender.send_bytes(b"first payload")
            await sender.send_bytes(b"second")
            assert await receiver.receive_bytes() == b"first payload"
            assert await receiver.receive_bytes() == b"second"

    trio.run(main)


def test_send_eof_makes_peer_sendonly() -> None:
    async def main() -> None:
        async with gateway_pair() as (left, right):
            sender = left.open_raw_channel()
            receiver = right.open_raw_channel(sender.id)
            await sender.send_bytes(b"data")
            await sender.send_eof()
            assert await receiver.receive_bytes() == b"data"
            with pytest.raises(EOFError):
                await receiver.receive_bytes()
            # the receiver of an EOF may still send back
            await receiver.send_bytes(b"reply")
            assert await sender.receive_bytes() == b"reply"
            with pytest.raises(OSError, match="cannot send"):
                await sender.send_bytes(b"after eof")

    trio.run(main)


def test_close_drains_then_blocks_both_directions() -> None:
    async def main() -> None:
        async with gateway_pair() as (left, right):
            sender = left.open_raw_channel()
            receiver = right.open_raw_channel(sender.id)
            await sender.send_bytes(b"x")
            await sender.aclose()
            # payloads sent before the close still drain
            assert await receiver.receive_bytes() == b"x"
            with pytest.raises(EOFError):
                await receiver.receive_bytes()
            with pytest.raises(OSError, match="cannot send"):
                await receiver.send_bytes(b"y")
            with pytest.raises(OSError, match="cannot send"):
                await sender.send_bytes(b"z")

    trio.run(main)


def test_close_with_error_raises_remote_error() -> None:
    async def main() -> None:
        async with gateway_pair() as (left, right):
            sender = left.open_raw_channel()
            receiver = right.open_raw_channel(sender.id)
            await sender.aclose(error="boom happened")
            with pytest.raises(RemoteError, match="boom happened"):
                await receiver.receive_bytes()

    trio.run(main)


def test_async_iteration_yields_payloads_until_eof() -> None:
    async def main() -> None:
        async with gateway_pair() as (left, right):
            sender = left.open_raw_channel()
            receiver = right.open_raw_channel(sender.id)
            payloads = [b"a", b"bb", b"ccc"]
            for payload in payloads:
                await sender.send_bytes(payload)
            await sender.send_eof()
            assert [data async for data in receiver] == payloads

    trio.run(main)


def test_terminate_closes_peer_cleanly() -> None:
    async def main() -> None:
        async with gateway_pair() as (left, right):
            channel = right.open_raw_channel()
            await left.terminate()
            await right.wait_closed()
            assert right._error is None
            with pytest.raises(EOFError):
                await channel.receive_bytes()

    trio.run(main)


def test_status_reply_travels_on_raw_channel() -> None:
    async def main() -> None:
        async with gateway_pair() as (left, right):
            channel = left.open_raw_channel()
            await left._send(Message.STATUS, channel.id)
            status = loads_internal(await channel.receive_bytes())
            assert status["execmodel"] == "trio"
            assert status["numexecuting"] == 0
            # the peer closes the status channel after the reply
            with pytest.raises(EOFError):
                await channel.receive_bytes()

    trio.run(main)


def test_unsupported_message_is_rejected_with_remote_error() -> None:
    async def main() -> None:
        async with gateway_pair() as (left, right):
            channel = left.open_raw_channel()
            await left._send(
                Message.CHANNEL_EXEC,
                channel.id,
                dumps_internal(("code", None, None, {})),
            )
            with pytest.raises(RemoteError, match="unsupported message"):
                await channel.receive_bytes()

    trio.run(main)


def test_send_after_gateway_close_raises() -> None:
    async def main() -> None:
        async with gateway_pair() as (left, right):
            channel = left.open_raw_channel()
            await left.aclose()
            with pytest.raises(OSError, match="cannot send"):
                await channel.send_bytes(b"x")
            with pytest.raises(OSError, match="already closed"):
                left.open_raw_channel()

    trio.run(main)


def test_peer_disappearing_surfaces_eof_error() -> None:
    async def main() -> None:
        left_stream, right_stream = trio.testing.memory_stream_pair()
        async with trio.open_nursery() as nursery:
            right = AsyncGateway(right_stream, id="right", _startcount=2)
            await nursery.start(right._serve)
            channel = right.open_raw_channel()
            # peer vanishes without a termination message
            await left_stream.aclose()
            await right.wait_closed()
            with pytest.raises(EOFError):
                await channel.receive_bytes()

    trio.run(main)


def test_mid_frame_eof_is_an_error() -> None:
    async def main() -> None:
        left_stream, right_stream = trio.testing.memory_stream_pair()
        async with trio.open_nursery() as nursery:
            right = AsyncGateway(right_stream, id="right", _startcount=2)
            await nursery.start(right._serve)
            frame = Message(Message.CHANNEL_DATA, 1, b"payload").pack()
            await left_stream.send_all(frame[:5])
            await left_stream.aclose()
            await right.wait_closed()
            assert isinstance(right._error, EOFError)
            assert "mid-frame" in str(right._error)

    trio.run(main)
