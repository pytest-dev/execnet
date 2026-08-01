"""
tests for multi channels and gateway Groups
"""

from __future__ import annotations

import time
from collections.abc import Callable

import pytest

import execnet
from execnet import Gateway
from execnet import Group
from execnet import XSpec
from execnet import _provision
from execnet._channel import Channel
from execnet._execmodel import ExecModel


class TestMultiChannelAndGateway:
    def test_multichannel_container_basics(
        self, gw: Gateway, execmodel: ExecModel
    ) -> None:
        mch = execnet.MultiChannel([Channel(gw, i) for i in range(3)])
        assert len(mch) == 3
        channels = list(mch)
        assert len(channels) == 3
        # ordering
        for i in range(3):
            assert channels[i].id == i
            assert channels[i] == mch[i]
        assert channels[0] in mch
        assert channels[1] in mch
        assert channels[2] in mch

    def test_multichannel_receive_each(self) -> None:
        class pseudochannel:
            def receive(self) -> object:
                return 12

        pc1 = pseudochannel()
        pc2 = pseudochannel()
        multichannel = execnet.MultiChannel([pc1, pc2])  # type: ignore[list-item]
        l = multichannel.receive_each(withchannel=True)
        assert len(l) == 2
        assert l == [(pc1, 12), (pc2, 12)]  # type: ignore[comparison-overlap]
        l2 = multichannel.receive_each(withchannel=False)
        assert l2 == [12, 12]

    def test_multichannel_send_each(self) -> None:
        gm = execnet.Group(["popen"] * 2)
        mc = gm.remote_exec(
            """
            import os
            channel.send(channel.receive() + 1)
        """
        )
        mc.send_each(41)
        l = mc.receive_each()
        assert l == [42, 42]

    def test_Group_execmodel_setting(self) -> None:
        gm = execnet.Group()
        gm.set_execmodel("thread")
        assert gm.execmodel.backend == "thread"
        assert gm.remote_execmodel.backend == "thread"
        gm._gateways.append(1)  # type: ignore[arg-type]
        try:
            with pytest.raises(ValueError):
                gm.set_execmodel("main_thread_only")
            assert gm.execmodel.backend == "thread"
        finally:
            gm._gateways.pop()

    def test_multichannel_receive_queue_for_two_subprocesses(self) -> None:
        gm = execnet.Group(["popen"] * 2)
        mc = gm.remote_exec(
            """
            import os
            channel.send(os.getpid())
        """
        )
        queue = mc.make_receive_queue()
        ch, item = queue.get(timeout=10)
        ch2, item2 = queue.get(timeout=10)
        assert ch != ch2
        assert ch.gateway != ch2.gateway
        assert item != item2
        mc.waitclose()

    def test_multichannel_waitclose(self) -> None:
        l = []

        class pseudochannel:
            def waitclose(self) -> None:
                l.append(0)

        multichannel = execnet.MultiChannel([pseudochannel(), pseudochannel()])  # type: ignore[list-item]
        multichannel.waitclose()
        assert len(l) == 2


class TestGroup:
    def test_basic_group(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import atexit

        atexitlist: list[Callable[[], object]] = []
        monkeypatch.setattr(atexit, "register", atexitlist.append)
        group = Group()
        assert atexitlist == [group._cleanup_atexit]
        exitlist = []
        joinlist = []

        class PseudoIO:
            def wait(self) -> None:
                pass

        class PseudoSpec:
            via = None

        class PseudoGW:
            id = "9999"
            _io = PseudoIO()
            spec = PseudoSpec()

            def exit(self) -> None:
                exitlist.append(self)
                group._unregister(self)  # type: ignore[arg-type]

            def join(self) -> None:
                joinlist.append(self)

        gw = PseudoGW()
        group._register(gw)  # type: ignore[arg-type]
        assert len(exitlist) == 0
        assert len(joinlist) == 0
        group._cleanup_atexit()
        assert len(exitlist) == 1
        assert exitlist == [gw]
        assert len(joinlist) == 1
        assert joinlist == [gw]
        group._cleanup_atexit()
        assert len(exitlist) == 1
        assert len(joinlist) == 1

    def test_group_default_spec(self) -> None:
        group = Group()
        group.defaultspec = "not-existing-type"
        pytest.raises(ValueError, group.makegateway)

    def test_group_PopenGateway(self) -> None:
        group = Group()
        gw = group.makegateway("popen")
        assert list(group) == [gw]
        assert group[0] == gw
        assert len(group) == 1
        group._cleanup_atexit()
        assert not group._gateways

    def test_group_ordering_and_termination(self) -> None:
        group = Group()
        group.makegateway("popen//id=3")
        group.makegateway("popen//id=2")
        group.makegateway("popen//id=5")
        gwlist = list(group)
        assert len(gwlist) == 3
        idlist = [x.id for x in gwlist]
        assert idlist == list("325")
        print(group)
        group.terminate()
        print(group)
        assert not group
        assert repr(group) == "<Group []>"

    def test_group_id_allocation(self) -> None:
        group = Group()
        specs = [XSpec("popen"), XSpec("popen//id=hello")]
        group.allocate_id(specs[0])
        group.allocate_id(specs[1])
        gw = group.makegateway(specs[1])
        assert gw.id == "hello"
        gw = group.makegateway(specs[0])
        assert gw.id == "gw0"
        # pytest.raises(ValueError,
        #    group.allocate_id, XSpec("popen//id=hello"))
        group.terminate()

    def test_gateway_and_id(self) -> None:
        group = Group()
        gw = group.makegateway("popen//id=hello")
        assert group["hello"] == gw
        with pytest.raises((TypeError, AttributeError)):
            del group["hello"]  # type: ignore[attr-defined]
        with pytest.raises((TypeError, AttributeError)):
            group["hello"] = 5  # type: ignore[index]
        assert "hello" in group
        assert gw in group
        assert len(group) == 1
        gw.exit()
        assert "hello" not in group
        with pytest.raises(KeyError):
            _ = group["hello"]

    def test_default_group(self) -> None:
        oldlist = list(execnet.default_group)
        gw = execnet.makegateway("popen")
        try:
            newlist = list(execnet.default_group)
            assert len(newlist) == len(oldlist) + 1
            assert gw in newlist
            assert gw not in oldlist
        finally:
            gw.exit()

    def test_remote_exec_args(self) -> None:
        group = Group()
        group.makegateway("popen")

        def fun(channel, arg) -> None:
            channel.send(arg)

        mch = group.remote_exec(fun, arg=1)
        result = mch.receive_each()
        assert result == [1]

    def test_terminate_with_proxying(self) -> None:
        group = Group()
        group.makegateway("popen//id=coordinator")
        group.makegateway("popen//via=coordinator//id=worker")
        group.terminate(1.0)

    @pytest.mark.skipif(
        not _provision.provisioning_available(),
        reason="a via sub-spec ships provisioning material eagerly",
    )
    def test_via_foreign_python(self) -> None:
        # A python= sub-spec through a via coordinator: it resolves the
        # interpreter locally (this interpreter has execnet, so the sub runs
        # the worker module directly, no uv provisioning).
        import sys

        group = Group()
        try:
            group.makegateway("popen//id=coordinator")
            gw = group.makegateway(
                f"popen//python={sys.executable}//via=coordinator//id=sub"
            )
            channel = gw.remote_exec("channel.send(channel.receive() + 1)")
            channel.send(41)
            assert channel.receive() == 42
        finally:
            group.terminate(1.0)


@pytest.mark.timeout(30)
def test_terminate_kills_a_worker_that_will_not_go(execmodel: ExecModel) -> None:
    """Regression for #43/#221: termination stays bounded by its timeout.

    The worker ignores SIGINT and never returns from its exec, so nothing
    short of the kill ends it.  ``terminate(timeout)`` must still come back
    at roughly its own grace -- the bound used to be the thing that broke,
    and it now lives in ``AsyncGroup._terminate_one`` rather than in the
    retired ``safe_terminate`` helper.
    """
    group = Group()
    gw = group.makegateway("popen")
    channel = gw.remote_exec(
        """
        import signal, time
        try:
            signal.signal(signal.SIGINT, signal.SIG_IGN)
        except ValueError:
            pass          # not the main thread: the pool thread ignores it too
        channel.send("blocked")
        while True:
            time.sleep(0.1)
        """
    )
    assert channel.receive(timeout=10) == "blocked"
    start = time.monotonic()
    group.terminate(timeout=1.0)
    assert time.monotonic() - start < 15
