"""``execnet.trio``: trio in the caller's run, protocol IO on the engine.

The counterpart of :mod:`testing.test_aio`, and of ``test_trio_gateway``
which drives the same core the other way (``execnet.raw_trio``, gateways as
tasks in the caller's own nursery).  What is tested here is specifically
the facade: that the engine is where the gateways live, that cancellation
crosses the bridge, and that what the surface deliberately does *not*
expose stays unexposed.
"""

from __future__ import annotations

import pytest
import trio

import execnet.raw_trio
import execnet.trio
from execnet._engine import ProtocolEngine
from execnet._engine import default_engine

TESTTIMEOUT = 30.0


def run(async_fn: object, *args: object) -> object:
    async def main() -> object:
        with trio.fail_after(TESTTIMEOUT):
            return await async_fn(*args)  # type: ignore[operator]

    return trio.run(main)


class TestTheSurface:
    def test_popen_roundtrip(self) -> None:
        async def main() -> None:
            async with execnet.trio.AsyncGroup() as group:
                gateway = await group.makegateway("popen")
                channel = await gateway.remote_exec(
                    "channel.send(channel.receive() + 1)"
                )
                await channel.send(41)
                assert await channel.receive() == 42
                await channel.wait_closed()

        run(main)

    def test_open_gateway_iteration(self) -> None:
        async def main() -> list[int]:
            async with execnet.trio.open_gateway() as gateway:
                channel = await gateway.remote_exec(
                    "for i in range(4): channel.send(i * 2)"
                )
                return [item async for item in channel]

        assert run(main) == [0, 2, 4, 6]

    def test_receive_timeout(self) -> None:
        async def main() -> None:
            async with execnet.trio.open_gateway() as gateway:
                channel = await gateway.remote_exec("channel.receive()")
                with pytest.raises(channel.TimeoutError):
                    await channel.receive(timeout=0.05)
                await channel.send(None)

        run(main)

    def test_remote_error(self) -> None:
        async def main() -> None:
            async with execnet.trio.open_gateway() as gateway:
                channel = await gateway.remote_exec("raise ValueError(17)")
                with pytest.raises(execnet.trio.RemoteError, match="ValueError"):
                    await channel.receive()

        run(main)

    def test_channel_passing_wraps_the_facade(self) -> None:
        async def main() -> None:
            async with execnet.trio.open_gateway() as gateway:
                channel = await gateway.remote_exec(
                    """
                    c = channel.gateway.newchannel()
                    channel.send(c)
                    c.send(42)
                    """
                )
                passed = await channel.receive()
                assert isinstance(passed, execnet.trio.AsyncChannel)
                assert await passed.receive() == 42

        run(main)

    def test_multiple_gateways(self) -> None:
        async def main() -> list[int]:
            async with execnet.trio.AsyncGroup() as group:
                gateways = [await group.makegateway("popen") for _ in range(2)]
                channels = [
                    await gw.remote_exec("channel.send(channel.receive() * 2)")
                    for gw in gateways
                ]
                for index, channel in enumerate(channels):
                    await channel.send(index + 1)
                return [await channel.receive() for channel in channels]

        assert run(main) == [2, 4]

    def test_group_not_started(self) -> None:
        async def main() -> None:
            group = execnet.trio.AsyncGroup()
            with pytest.raises(RuntimeError, match="not started"):
                await group.makegateway("popen")

        run(main)

    def test_start_and_aclose_explicitly(self) -> None:
        # what a lifespan hook does, rather than an "async with"
        async def main() -> None:
            group = execnet.trio.AsyncGroup()
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

        run(main)

    def test_terminate_gateway_explicitly(self) -> None:
        async def main() -> None:
            async with execnet.trio.AsyncGroup() as group:
                gateway = await group.makegateway("popen")
                assert await (await gateway.remote_exec("channel.send(1)")).receive()
                await gateway.terminate()

        run(main)


class TestItRunsOnTheEngine:
    """The facade's reason to exist, and what follows from it."""

    def test_groups_share_the_default_engine(self) -> None:
        async def main() -> None:
            async with execnet.trio.AsyncGroup() as a, execnet.trio.AsyncGroup() as b:
                assert a.engine is b.engine is default_engine()

        run(main)

    def test_an_explicit_engine_is_used(self) -> None:
        async def main() -> None:
            engine = ProtocolEngine(name="execnet-engine-trio-facade")
            async with execnet.trio.AsyncGroup(engine=engine) as group:
                assert group.engine is engine
                gateway = await group.makegateway("popen")
                assert await (await gateway.remote_exec("channel.send(1)")).receive()
            engine.close()

        run(main)

    def test_a_gateway_outlives_the_nursery_that_made_it(self) -> None:
        # the structural difference from raw_trio: the group's nursery is on
        # the engine, so a gateway is a handle rather than a scoped resource
        async def main() -> None:
            group = execnet.trio.AsyncGroup()
            await group.start()
            try:
                holder: list[object] = []
                async with trio.open_nursery() as nursery:

                    async def make() -> None:
                        holder.append(await group.makegateway("popen"))

                    nursery.start_soon(make)
                # the nursery it was created in is gone; the gateway is not
                gateway = holder[0]
                channel = await gateway.remote_exec("channel.send(11)")  # type: ignore[attr-defined]
                assert await channel.receive() == 11
            finally:
                await group.aclose()

        run(main)

    def test_the_engine_keeps_serving_while_the_caller_loop_blocks(self) -> None:
        # what a caller buys by not being raw_trio: a step that never yields
        # stalls this loop, and the protocol keeps running regardless
        import time

        async def main() -> None:
            async with execnet.trio.open_gateway() as gateway:
                channel = await gateway.remote_exec(
                    "for i in range(3): channel.send(i)"
                )
                time.sleep(0.3)
                assert [await channel.receive() for _ in range(3)] == [0, 1, 2]

        run(main)


class TestCancellation:
    """Cancellation crosses the bridge, and shielding means trio's shielding."""

    def test_a_cancelled_receive_does_not_consume_an_item(self) -> None:
        async def main() -> None:
            async with execnet.trio.open_gateway() as gateway:
                channel = await gateway.remote_exec(
                    """
                    channel.receive()
                    for i in range(3):
                        channel.send(i)
                    """
                )
                with trio.move_on_after(0.05):
                    await channel.receive()
                # release the worker; nothing was consumed by the cancelled wait
                await channel.send("go")
                assert [await channel.receive() for _ in range(3)] == [0, 1, 2]

        run(main)

    def test_a_shielded_send_is_not_cancellable(self) -> None:
        # the documented difference from execnet.aio, where asyncio.shield
        # delivers the CancelledError while the work continues: here the wait
        # itself is uncancellable, so the send completes before we move on
        async def main() -> None:
            async with execnet.trio.open_gateway() as gateway:
                channel = await gateway.remote_exec(
                    "channel.send(channel.receive() * 2)"
                )
                with trio.move_on_after(0.0001) as scope:
                    await channel.send(21)
                assert not scope.cancelled_caught
                assert await channel.receive() == 42

        run(main)

    def test_a_cancelled_scope_leaves_the_gateway_usable(self) -> None:
        async def main() -> None:
            async with execnet.trio.open_gateway() as gateway:
                blocked = await gateway.remote_exec("channel.receive()")
                with trio.move_on_after(0.05):
                    await blocked.receive()
                await blocked.send(None)
                channel = await gateway.remote_exec("channel.send('still here')")
                assert await channel.receive() == "still here"

        run(main)


class TestTheSubset:
    """What the facade does not expose, and why that is not an oversight."""

    @pytest.mark.parametrize(
        "name", ["_open_raw_channel", "open_channel", "_enqueue_frame", "wait_closed"]
    )
    def test_engine_internals_are_not_on_the_facade_gateway(self, name: str) -> None:
        # channel ids come from an unlocked per-gateway counter that works
        # only because one loop owns it; handing that out across the bridge
        # would let two allocators issue the same id
        assert not hasattr(execnet.trio.AsyncGateway, name)

    def test_the_raw_surface_still_has_them(self) -> None:
        assert hasattr(execnet.raw_trio.AsyncGateway, "_open_raw_channel")
        assert hasattr(execnet.raw_trio.AsyncGateway, "open_channel")

    def test_serve_gateway_is_raw_only(self) -> None:
        assert not hasattr(execnet.trio, "serve_gateway")


class TestDeploymentValidation:
    def test_deploying_to_no_gateways_is_refused(self) -> None:
        from execnet._deploy._api import Deployment

        async def main() -> None:
            with pytest.raises(ValueError, match="no gateways"):
                await execnet.trio.deploy_all(
                    Deployment.__new__(Deployment),  # never reached
                    [],
                )

        run(main)

    def test_gateways_from_two_engines_are_refused(self) -> None:
        from execnet._deploy._api import Deployment

        async def main() -> None:
            other = ProtocolEngine(name="execnet-engine-trio-second")
            async with (
                execnet.trio.AsyncGroup() as one,
                execnet.trio.AsyncGroup(engine=other) as two,
            ):
                gateways = [
                    await one.makegateway("popen"),
                    await two.makegateway("popen"),
                ]
                with pytest.raises(ValueError, match=r"same execnet\.ProtocolEngine"):
                    await execnet.trio.deploy_all(
                        Deployment.__new__(Deployment), gateways
                    )
            other.close()

        run(main)


class TestSalvage:
    """A cancelled receive never costs an item, end to end.

    :mod:`testing.test_bridge` covers the carrier's two orderings; what is
    left to check is the wiring -- that a salvaged item reaches the
    channel's slot and comes back out of the next ``receive`` in order, and
    that it is still wrapped if it happens to be a channel.

    The window is forced rather than raced for: the delivery and a cancel
    are queued onto the caller's loop in that order, and its entry queue is
    FIFO, so the receive is guaranteed to be cancelled with the engine's
    item already in hand.
    """

    @staticmethod
    def _cancel_as_the_engine_answers(
        monkeypatch: pytest.MonkeyPatch, scope: trio.CancelScope
    ) -> None:
        """Cancel ``scope`` in the instant the engine hands back its result.

        The cancel is queued *ahead* of the delivery, on a FIFO entry queue,
        so the receiving task is cancelled with the engine's item already
        produced and one callback away.  Queuing it behind the delivery
        instead proves nothing: setting the event reschedules the waiting
        task, and trio will not then deliver a cancel to a task that already
        has a wakeup pending -- the receive simply succeeds.
        """
        from execnet import _bridge

        token = trio.lowlevel.current_trio_token()
        real = _bridge.TrioCarrier.resolve

        def hooked(self: object, result: object, error: object) -> None:
            token.run_sync_soon(scope.cancel)
            real(self, result, error)  # type: ignore[arg-type]

        monkeypatch.setattr(_bridge.TrioCarrier, "resolve", hooked)

    def test_an_item_taken_as_the_cancel_lands_comes_back(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def main() -> None:
            async with execnet.trio.open_gateway() as gateway:
                channel = await gateway.remote_exec(
                    "channel.receive()\nfor i in range(3): channel.send(i)"
                )
                await channel.send("go")
                with trio.CancelScope() as scope:
                    self._cancel_as_the_engine_answers(monkeypatch, scope)
                    await channel.receive()
                assert scope.cancelled_caught
                monkeypatch.undo()
                assert channel._salvaged is not execnet.trio._NOTHING
                # in order, and none of them missing
                assert [await channel.receive() for _ in range(3)] == [0, 1, 2]

        run(main)

    def test_a_salvaged_channel_is_still_wrapped(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def main() -> None:
            async with execnet.trio.open_gateway() as gateway:
                channel = await gateway.remote_exec(
                    """
                    channel.receive()
                    c = channel.gateway.newchannel()
                    channel.send(c)
                    c.send(42)
                    """
                )
                await channel.send("go")
                with trio.CancelScope() as scope:
                    self._cancel_as_the_engine_answers(monkeypatch, scope)
                    await channel.receive()
                monkeypatch.undo()
                passed = await channel.receive()
                assert isinstance(passed, execnet.trio.AsyncChannel)
                assert await passed.receive() == 42

        run(main)

    def test_an_uncancelled_receive_leaves_nothing_behind(self) -> None:
        async def main() -> None:
            async with execnet.trio.open_gateway() as gateway:
                channel = await gateway.remote_exec(
                    "for i in range(2): channel.send(i)"
                )
                assert await channel.receive() == 0
                assert channel._salvaged is execnet.trio._NOTHING
                assert await channel.receive() == 1

        run(main)
