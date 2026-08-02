"""Protocol tests for the trio-native async gateway core (RawChannel level).

Two AsyncGateways are wired together over an in-memory stream pair (the
transport harness, as in ``TestFrameDecoder``); the assertions cover execnet
semantics: payload routing by channel id, the close/EOF/sendonly state
machine, RemoteError propagation, and gateway termination.
"""

from __future__ import annotations

import os
import sys
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
import trio
import trio.testing

from execnet import _errors
from execnet import _trio_gateway
from execnet._errors import RemoteError
from execnet._message import Message
from execnet._serialize import dumps_internal
from execnet._serialize import loads_internal
from execnet._trio_gateway import AsyncChannel
from execnet._trio_gateway import AsyncGateway
from execnet._trio_gateway import AsyncGroup
from execnet._trio_gateway import ThreadedFdStream
from execnet._trio_gateway import open_gateway


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
            sender = left._open_raw_channel()
            receiver = right._open_raw_channel(sender.id)
            await sender.send_bytes(b"first payload")
            await sender.send_bytes(b"second")
            assert await receiver.receive_bytes() == b"first payload"
            assert await receiver.receive_bytes() == b"second"

    trio.run(main)


def test_send_eof_makes_peer_sendonly() -> None:
    async def main() -> None:
        async with gateway_pair() as (left, right):
            sender = left._open_raw_channel()
            receiver = right._open_raw_channel(sender.id)
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
            sender = left._open_raw_channel()
            receiver = right._open_raw_channel(sender.id)
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
            sender = left._open_raw_channel()
            receiver = right._open_raw_channel(sender.id)
            await sender.aclose(error="boom happened")
            with pytest.raises(RemoteError, match="boom happened"):
                await receiver.receive_bytes()

    trio.run(main)


def test_async_iteration_yields_payloads_until_eof() -> None:
    async def main() -> None:
        async with gateway_pair() as (left, right):
            sender = left._open_raw_channel()
            receiver = right._open_raw_channel(sender.id)
            payloads = [b"a", b"bb", b"ccc"]
            for payload in payloads:
                await sender.send_bytes(payload)
            await sender.send_eof()
            assert [data async for data in receiver] == payloads

    trio.run(main)


def test_terminate_closes_peer_cleanly() -> None:
    async def main() -> None:
        async with gateway_pair() as (left, right):
            channel = right._open_raw_channel()
            await left.terminate()
            await right.wait_closed()
            assert right._error is None
            with pytest.raises(EOFError):
                await channel.receive_bytes()

    trio.run(main)


def test_status_reply_travels_on_raw_channel() -> None:
    async def main() -> None:
        async with gateway_pair() as (left, _right):
            channel = left._open_raw_channel()
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
        async with gateway_pair() as (left, _right):
            channel = left._open_raw_channel()
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
        async with gateway_pair() as (left, _right):
            channel = left._open_raw_channel()
            await left.aclose()
            with pytest.raises(OSError, match="cannot send"):
                await channel.send_bytes(b"x")
            with pytest.raises(OSError, match="already closed"):
                left._open_raw_channel()

    trio.run(main)


def test_peer_disappearing_surfaces_eof_error() -> None:
    async def main() -> None:
        left_stream, right_stream = trio.testing.memory_stream_pair()
        async with trio.open_nursery() as nursery:
            right = AsyncGateway(right_stream, id="right", _startcount=2)
            await nursery.start(right._serve)
            channel = right._open_raw_channel()
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


def test_channel_serializes_builtin_items() -> None:
    items = [42, "text", b"bytes", [1, 2], ("a", 1), {"key": [True, None]}, {1, 2}]

    async def main() -> None:
        async with gateway_pair() as (left, right):
            sender = left.open_channel()
            receiver = right.open_channel(sender.id)
            for item in items:
                await sender.send(item)
            await sender.send_eof()
            assert [item async for item in receiver] == items

    trio.run(main)


def test_channel_receive_timeout() -> None:
    async def main() -> None:
        async with gateway_pair() as (left, _right):
            channel = left.open_channel()
            with pytest.raises(_errors.TimeoutError):
                await channel.receive(timeout=0.05)

    trio.run(main)


def test_channel_close_with_error_and_wait_closed() -> None:
    async def main() -> None:
        async with gateway_pair() as (left, right):
            sender = left.open_channel()
            receiver = right.open_channel(sender.id)
            await sender.aclose(error="exec exploded")
            with pytest.raises(RemoteError, match="exec exploded"):
                await receiver.receive()
            with pytest.raises(RemoteError, match="exec exploded"):
                await receiver.wait_closed()

    trio.run(main)


def test_channel_wait_closed_on_clean_eof() -> None:
    async def main() -> None:
        async with gateway_pair() as (left, right):
            sender = left.open_channel()
            receiver = right.open_channel(sender.id)
            await sender.send(1)
            await sender.send_eof()
            await receiver.wait_closed()
            # items sent before the EOF still drain after waitclose
            assert await receiver.receive() == 1

    trio.run(main)


def test_channel_objects_travel_over_the_wire() -> None:
    async def main() -> None:
        async with gateway_pair() as (left, right):
            carrier = left.open_channel()
            right_carrier = right.open_channel(carrier.id)
            extra = left.open_channel()
            await carrier.send({"reply-to": extra})
            received = await right_carrier.receive()
            remote_extra = received["reply-to"]
            assert remote_extra.id == extra.id
            await remote_extra.send("over the transferred channel")
            assert await extra.receive() == "over the transferred channel"

    trio.run(main)


def _remote_add(channel, a, b) -> None:  # type: ignore[no-untyped-def]
    channel.send(a + b)


class TestPopenAsyncGateway:
    """Integration: an AsyncGateway serving a real popen worker inside
    the user's own trio run (no host thread)."""

    def test_remote_exec_roundtrip(self) -> None:
        async def main() -> None:
            async with open_gateway() as gateway:
                channel = await gateway.remote_exec(
                    "channel.send(channel.receive() + 1)"
                )
                await channel.send(41)
                assert await channel.receive() == 42
                await channel.wait_closed()

        trio.run(main)

    def test_remote_exec_function_with_kwargs(self) -> None:
        async def main() -> None:
            async with open_gateway() as gateway:
                channel = await gateway.remote_exec(_remote_add, a=40, b=2)
                assert await channel.receive() == 42

        trio.run(main)

    def test_remote_error_propagates(self) -> None:
        async def main() -> None:
            async with open_gateway() as gateway:
                channel = await gateway.remote_exec("raise ValueError('kaboom')")
                with pytest.raises(RemoteError, match="kaboom"):
                    await channel.receive()

        trio.run(main)

    def test_exec_finish_closes_channel_ending_iteration(self) -> None:
        async def main() -> None:
            async with open_gateway() as gateway:
                channel = await gateway.remote_exec(
                    "for i in range(3): channel.send(i)"
                )
                assert [item async for item in channel] == [0, 1, 2]

        trio.run(main)

    def test_concurrent_remote_execs(self) -> None:
        async def main() -> None:
            async with open_gateway() as gateway:
                results = []

                async def run_one(value: int) -> None:
                    channel = await gateway.remote_exec(
                        "channel.send(channel.receive() * 10)"
                    )
                    await channel.send(value)
                    results.append(await channel.receive())

                async with trio.open_nursery() as nursery:
                    for value in range(5):
                        nursery.start_soon(run_one, value)
                assert sorted(results) == [0, 10, 20, 30, 40]

        trio.run(main)


class TestAsyncGroup:
    def test_multiple_gateways_with_auto_ids(self) -> None:
        async def main() -> None:
            async with AsyncGroup() as group:
                first = await group.makegateway()
                second = await group.makegateway()
                assert {first.id, second.id} == {"gw0", "gw1"}
                for gateway in (first, second):
                    channel = await gateway.remote_exec("channel.send(42)")
                    assert await channel.receive() == 42

        trio.run(main)

    def test_group_exit_terminates_and_reaps_workers(self) -> None:
        async def main() -> None:
            async with AsyncGroup() as group:
                await group.makegateway()
                await group.makegateway()
                processes = list(group._processes.values())
            # workers exited on GATEWAY_TERMINATE, nobody had to kill them
            assert [process.returncode for process in processes] == [0, 0]

        trio.run(main)

    def test_terminate_kills_hung_worker_within_bound(self) -> None:
        async def main() -> None:
            async with AsyncGroup(termination_timeout=1.0) as group:
                gateway = await group.makegateway()
                await gateway.remote_exec("import time\nwhile True: time.sleep(1)")
                processes = list(group._processes.values())
            assert all(process.returncode is not None for process in processes)

        trio.run(main)

    def test_finished_channels_do_not_accumulate(self) -> None:
        """A long-lived gateway must not keep one dead channel per exec.

        The sync surface is protected by its weak channel registry; the
        async ones hold their channels strongly, so a coordinator doing
        many ``remote_exec``s -- a test run, a pod fleet -- grew for as long
        as it lived.  Nothing can arrive for a remotely closed id (ids step
        by two per side and are never reused), so the registry drops it.
        """

        async def main() -> None:
            async with AsyncGroup() as group:
                gateway = await group.makegateway()
                for _ in range(20):
                    channel = await gateway.remote_exec("channel.send(42)")
                    assert await channel.receive() == 42
                    await channel.wait_closed()
                assert gateway._channels == {}
                assert gateway._async_channels == {}

        trio.run(main)

    def test_a_channel_nobody_asked_for_survives_until_it_is_claimed(self) -> None:
        # the exception to the above: a channel the local side has never
        # taken exists only in the registry, and a reference passed in a
        # payload has to find what arrived on it -- not a fresh empty one
        async def main() -> None:
            async with gateway_pair() as (left, right):
                passed = right.open_channel()
                await passed.send(b"ignored")  # ensure the id is live
                sender = right.open_channel()
                # right sends a reference to `passed`, plus data and a close
                # on it, before left ever looks at that id
                await sender.send(passed)
                await passed.send("buffered")
                await passed.aclose()
                await trio.testing.wait_all_tasks_blocked()

                receiver = left.open_channel(sender.id)
                arrived = await receiver.receive()
                assert isinstance(arrived, AsyncChannel)
                assert await arrived.receive() == b"ignored"
                assert await arrived.receive() == "buffered"

        trio.run(main)

    def test_provisioning_does_not_stall_the_loop(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Resolving what to launch must not block the loop it runs on.

        The probe of a ``python=`` target and a dev coordinator's wheel build
        are subprocesses that take from milliseconds to (probe timeout) 30s.
        Run inline they stall every gateway the loop serves, which for the
        shared host is every gateway in the process.
        """
        from execnet import _provision

        real = _provision.target_has_execnet

        def slow_probe(python: str) -> bool:
            time.sleep(0.3)
            return real(python)

        monkeypatch.setattr(_provision, "target_has_execnet", slow_probe)

        async def main() -> None:
            ticks = 0
            stop = trio.Event()

            async def heartbeat() -> None:
                nonlocal ticks
                while not stop.is_set():
                    await trio.sleep(0.01)
                    ticks += 1

            async with trio.open_nursery() as nursery:
                nursery.start_soon(heartbeat)
                async with AsyncGroup() as group:
                    await group.makegateway(f"popen//python={sys.executable}")
                stop.set()
            # the probe alone sleeps 0.3s; an inline call would have let
            # through a couple of ticks at most
            assert ticks > 10

        trio.run(main)

    def test_via_gateway_relays_through_coordinator(self) -> None:
        async def main() -> None:
            async with AsyncGroup() as group:
                coordinator = await group.makegateway("popen//id=coordinator")
                sub = await group.makegateway("popen//via=coordinator")
                coordinator_channel = await coordinator.remote_exec(
                    "import os; channel.send(os.getpid())"
                )
                sub_channel = await sub.remote_exec(
                    "import os; channel.send(os.getpid())"
                )
                coordinator_pid = await coordinator_channel.receive()
                sub_pid = await sub_channel.receive()
                # a real second process, reached through the coordinator's relay
                assert sub_pid != coordinator_pid
                echo = await sub.remote_exec("channel.send(channel.receive() * 2)")
                await echo.send(21)
                assert await echo.receive() == 42

        trio.run(main)

    def test_a_makegateway_that_fails_late_leaves_no_worker(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # between the handshake and the group taking ownership, the worker is
        # running and nothing would ever terminate it -- the connect helpers
        # clean up after themselves, but only until they return
        spawned: list[trio.Process] = []
        connect = _trio_gateway.connect_popen_worker

        async def spy(spec: object) -> tuple[object, trio.Process]:
            stream, process = await connect(spec)
            spawned.append(process)
            return stream, process

        def boom(self: AsyncGroup, stream: object, spec: object) -> AsyncGateway:
            raise RuntimeError("boom")

        monkeypatch.setattr(_trio_gateway, "connect_popen_worker", spy)
        monkeypatch.setattr(AsyncGroup, "_make_gateway", boom)

        async def main() -> None:
            async with AsyncGroup() as group:
                with pytest.raises(RuntimeError, match="boom"):
                    await group.makegateway()

        trio.run(main)
        assert len(spawned) == 1
        # killed and reaped, not left behind for the OS to inherit
        assert spawned[0].returncode is not None

    def test_unsupported_spec_is_rejected(self) -> None:
        async def main() -> None:
            async with AsyncGroup() as group:
                with pytest.raises(ValueError, match="unsupported spec"):
                    await group.makegateway("id=notype")

        trio.run(main)


class TestThreadedFdStream:
    """The Windows stand-in for ``trio.lowlevel.FdStream``.

    Exercised on every platform, since Windows is the only place it is
    *used* and the least convenient place to find out it is broken.
    """

    def test_roundtrip_through_a_pipe_pair(self) -> None:
        async def main() -> None:
            their_read, our_write = os.pipe()
            our_read, their_write = os.pipe()
            stream = ThreadedFdStream(our_read, our_write)
            try:
                await stream.send_all(b"hello ")
                await stream.send_all(b"world")
                assert os.read(their_read, 11) == b"hello world"

                os.write(their_write, b"back")
                assert await stream.receive_some(4) == b"back"
            finally:
                await stream.aclose()
                os.close(their_read)
                os.close(their_write)

        trio.run(main)

    def test_receive_reports_eof_as_empty(self) -> None:
        async def main() -> None:
            our_read, their_write = os.pipe()
            stream = ThreadedFdStream(our_read, os.open(os.devnull, os.O_WRONLY))
            os.close(their_write)
            try:
                assert await stream.receive_some(4) == b""
            finally:
                await stream.aclose()

        trio.run(main)

    def test_send_eof_lets_the_peer_see_the_end(self) -> None:
        async def main() -> None:
            their_read, our_write = os.pipe()
            stream = ThreadedFdStream(os.open(os.devnull, os.O_RDONLY), our_write)
            try:
                await stream.send_all(b"tail")
                await stream.send_eof()
                assert os.read(their_read, 4) == b"tail"
                assert os.read(their_read, 4) == b""  # write end is gone
                with pytest.raises(trio.ClosedResourceError):
                    await stream.send_all(b"more")
            finally:
                await stream.aclose()
                os.close(their_read)

        trio.run(main)

    def test_a_cancelled_read_is_abandoned_not_awaited(self) -> None:
        # a blocking read on a pipe cannot be interrupted, so cancellation
        # must not wait for it -- otherwise shutdown hangs until the peer
        # happens to write.
        async def main() -> None:
            our_read, their_write = os.pipe()
            stream = ThreadedFdStream(our_read, os.open(os.devnull, os.O_WRONLY))
            try:
                with trio.move_on_after(0.2) as scope:
                    await stream.receive_some(4)
                assert scope.cancelled_caught
            finally:
                os.close(their_write)
                await stream.aclose()

        trio.run(main)
