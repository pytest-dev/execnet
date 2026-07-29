"""The pure-async worker profile (profile=trio).

One single thread in the worker: the trio loop owns the main thread and
exec'd sources run as tasks on it, talking through AsyncChannels.  Sync
sources are rejected before they can starve the loop.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
import trio as trio_lib

import execnet
import execnet.trio
from execnet import Gateway

TESTTIMEOUT = 10.0


@pytest.fixture
def trio_gw(makegateway: Callable[[str], Gateway]) -> Gateway:
    return makegateway("popen//profile=trio")


class TestSyncCoordinator:
    def test_top_level_await_roundtrip(self, trio_gw: Gateway) -> None:
        channel = trio_gw.remote_exec("await channel.send(await channel.receive() + 1)")
        channel.send(41)
        assert channel.receive(TESTTIMEOUT) == 42

    def test_async_function_source(self, trio_gw: Gateway) -> None:
        async def source(channel, delta) -> None:
            value = await channel.receive()
            await channel.send(value + delta)

        channel = trio_gw.remote_exec(source, delta=5)
        channel.send(37)
        assert channel.receive(TESTTIMEOUT) == 42

    def test_iteration_and_eof(self, trio_gw: Gateway) -> None:
        channel = trio_gw.remote_exec(
            """
            for i in range(3):
                await channel.send(i * 2)
            """
        )
        assert list(channel) == [0, 2, 4]

    def test_single_thread_on_main(self, trio_gw: Gateway) -> None:
        channel = trio_gw.remote_exec(
            """
            import threading
            await channel.send(
                (
                    threading.active_count(),
                    threading.current_thread() is threading.main_thread(),
                )
            )
            """
        )
        active, on_main = channel.receive(TESTTIMEOUT)
        assert on_main
        assert active == 1

    def test_concurrent_execs_cooperate(self, trio_gw: Gateway) -> None:
        # two execs run as tasks on one loop: the first parks in receive
        # while the second completes -- no threads involved.
        blocked = trio_gw.remote_exec("await channel.send(await channel.receive())")
        side = trio_gw.remote_exec("await channel.send('side')")
        assert side.receive(TESTTIMEOUT) == "side"
        blocked.send("go")
        assert blocked.receive(TESTTIMEOUT) == "go"

    def test_sync_source_rejected(self, trio_gw: Gateway) -> None:
        channel = trio_gw.remote_exec("x = 40 + 2")
        with pytest.raises(channel.RemoteError, match="sync source"):
            channel.receive(TESTTIMEOUT)

    def test_sync_function_rejected(self, trio_gw: Gateway) -> None:
        def source(channel) -> None:
            pass

        channel = trio_gw.remote_exec(source)
        with pytest.raises(channel.RemoteError, match="must be async"):
            channel.receive(TESTTIMEOUT)

    def test_remote_error_traceback(self, trio_gw: Gateway) -> None:
        async def source(channel) -> None:
            raise ValueError(17)

        channel = trio_gw.remote_exec(source)
        with pytest.raises(channel.RemoteError, match="ValueError"):
            channel.receive(TESTTIMEOUT)

    def test_status_and_rinfo(self, trio_gw: Gateway) -> None:
        status = trio_gw.remote_status()
        assert status.profile == "trio"
        # legacy STATUS key, kept for pytest-xdist
        assert status.execmodel == "trio"
        rinfo = trio_gw._rinfo()
        assert rinfo.pid
        assert rinfo.version_info


def test_unknown_profile_rejected(makegateway: Callable[[str], Gateway]) -> None:
    with pytest.raises(ValueError, match="unknown profile"):
        makegateway("popen//profile=nope")
    # the pre-3.0 spelling routes to the same validation
    with pytest.raises(ValueError, match="unknown profile"):
        makegateway("popen//execmodel=nope")


def test_execmodel_is_an_accepted_alias_for_profile(
    makegateway: Callable[[str], Gateway],
) -> None:
    gw = makegateway("popen//execmodel=trio")
    assert gw.spec.profile == "trio"
    assert gw.spec.execmodel == "trio"
    with pytest.raises(ValueError, match="duplicate key"):
        execnet.XSpec("popen//execmodel=trio//profile=thread")


def test_trio_native_coordinator() -> None:
    async def main() -> None:
        async with execnet.trio.AsyncGroup() as group:
            gateway = await group.makegateway("popen//profile=trio")
            channel = await gateway.remote_exec(
                "await channel.send(await channel.receive() * 2)"
            )
            await channel.send(21)
            with trio_lib.fail_after(TESTTIMEOUT):
                assert await channel.receive() == 42

    trio_lib.run(main)
