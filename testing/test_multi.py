# -*- coding: utf-8 -*-
"""
    tests for multi channels and gateway Groups
"""
import gc
from time import sleep

import execnet
import py
import pytest
from execnet import XSpec
from execnet.gateway_base import Channel
from execnet.multi import Group
from execnet.multi import safe_terminate


class TestMultiChannelAndGateway:
    def test_multichannel_container_basics(self, gw, execmodel):
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

    def test_multichannel_receive_each(self):
        class pseudochannel:
            def receive(self):
                return 12

        pc1 = pseudochannel()
        pc2 = pseudochannel()
        multichannel = execnet.MultiChannel([pc1, pc2])
        l = multichannel.receive_each(withchannel=True)
        assert len(l) == 2
        assert l == [(pc1, 12), (pc2, 12)]
        l = multichannel.receive_each(withchannel=False)
        assert l == [12, 12]

    def test_multichannel_send_each(self):
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

    def test_Group_execmodel_setting(self):
        gm = execnet.Group()
        gm.set_execmodel("thread")
        assert gm.execmodel.backend == "thread"
        assert gm.remote_execmodel.backend == "thread"
        gm._gateways.append(1)
        try:
            with pytest.raises(ValueError):
                gm.set_execmodel("eventlet")
            assert gm.execmodel.backend == "thread"
        finally:
            gm._gateways.pop()

    def test_multichannel_receive_queue_for_two_subprocesses(self):
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

    def test_multichannel_waitclose(self):
        l = []

        class pseudochannel:
            def waitclose(self):
                l.append(0)

        multichannel = execnet.MultiChannel([pseudochannel(), pseudochannel()])
        multichannel.waitclose()
        assert len(l) == 2


class TestGroup:
    def test_basic_group(self, monkeypatch):
        import atexit

        atexitlist = []
        monkeypatch.setattr(atexit, "register", atexitlist.append)
        group = Group()
        assert atexitlist == [group._cleanup_atexit]
        exitlist = []
        joinlist = []

        class PseudoIO:
            def wait(self):
                pass

        class PseudoSpec:
            via = None

        class PseudoGW:
            id = "9999"
            _io = PseudoIO()
            spec = PseudoSpec()

            def exit(self):
                exitlist.append(self)
                group._unregister(self)

            def join(self):
                joinlist.append(self)

        gw = PseudoGW()
        group._register(gw)
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

    def test_group_default_spec(self):
        group = Group()
        group.defaultspec = "not-existing-type"
        py.test.raises(ValueError, group.makegateway)

    def test_group_PopenGateway(self):
        group = Group()
        gw = group.makegateway("popen")
        assert list(group) == [gw]
        assert group[0] == gw
        assert len(group) == 1
        group._cleanup_atexit()
        assert not group._gateways

    def test_group_ordering_and_termination(self):
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

    def test_group_id_allocation(self):
        group = Group()
        specs = [XSpec("popen"), XSpec("popen//id=hello")]
        group.allocate_id(specs[0])
        group.allocate_id(specs[1])
        gw = group.makegateway(specs[1])
        assert gw.id == "hello"
        gw = group.makegateway(specs[0])
        assert gw.id == "gw0"
        # py.test.raises(ValueError,
        #    group.allocate_id, XSpec("popen//id=hello"))
        group.terminate()

    def test_gateway_and_id(self):
        group = Group()
        gw = group.makegateway("popen//id=hello")
        assert group["hello"] == gw
        with pytest.raises((TypeError, AttributeError)):
            del group["hello"]
        with pytest.raises((TypeError, AttributeError)):
            group["hello"] = 5
        assert "hello" in group
        assert gw in group
        assert len(group) == 1
        gw.exit()
        assert "hello" not in group
        with pytest.raises(KeyError):
            _ = group["hello"]

    def test_default_group(self):
        oldlist = list(execnet.default_group)
        gw = execnet.makegateway("popen")
        try:
            newlist = list(execnet.default_group)
            assert len(newlist) == len(oldlist) + 1
            assert gw in newlist
            assert gw not in oldlist
        finally:
            gw.exit()

    def test_remote_exec_args(self):
        group = Group()
        group.makegateway("popen")

        def fun(channel, arg):
            channel.send(arg)

        mch = group.remote_exec(fun, arg=1)
        result = mch.receive_each()
        assert result == [1]

    def test_terminate_with_proxying(self):
        group = Group()
        group.makegateway("popen//id=master")
        group.makegateway("popen//via=master//id=worker")
        group.terminate(1.0)


def test_safe_terminate(execmodel):
    if execmodel.backend != "threading":
        pytest.xfail(
            "execution model %r does not support task count" % execmodel.backend
        )
    import threading

    active = threading.active_count()
    l = []

    def term():
        sleep(3)

    def kill():
        l.append(1)

    safe_terminate(execmodel, 1, [(term, kill)] * 10)
    assert len(l) == 10
    sleep(0.1)
    gc.collect()
    assert execmodel.active_count() == active


def test_safe_terminate2(execmodel):
    if execmodel.backend != "threading":
        pytest.xfail(
            "execution model %r does not support task count" % execmodel.backend
        )
    import threading

    active = threading.active_count()
    l = []

    def term():
        return

    def kill():
        l.append(1)

    safe_terminate(execmodel, 3, [(term, kill)] * 10)
    assert len(l) == 0
    sleep(0.1)
    gc.collect()
    assert threading.active_count() == active
