"""
    tests for multi channels and gateway Groups
"""

import execnet
import py

class TestMultiChannelAndGateway:
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
        assert l == [12,12]

    def test_multichannel_send_each(self):
        gm = execnet.Group(["popen"] * 2)
        mc = gm.remote_exec("""
            import os
            channel.send(channel.receive() + 1)
        """)
        mc.send_each(41)
        l = mc.receive_each()
        assert l == [42,42]

    def test_multichannel_receive_queue_for_two_subprocesses(self):
        gm = execnet.Group(["popen"] * 2)
        mc = gm.remote_exec("""
            import os
            channel.send(os.getpid())
        """)
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


from execnet.multi import Group
class TestGroup:
    def test_basic_group(self, monkeypatch):
        import atexit
        atexitlist = []
        monkeypatch.setattr(atexit, 'register', atexitlist.append)
        group = Group()
        assert atexitlist == [group._cleanup_atexit]
        exitlist = []
        class PseudoGW:
            def exit(self):
                exitlist.append(self)
        gw = PseudoGW()
        group._register(gw)
        assert len(exitlist) == 0
        group._cleanup_atexit()
        assert len(exitlist) == 1
        assert exitlist == [gw]
        group._cleanup_atexit()
        assert len(exitlist) == 1

    def test_group_PopenGateway(self):
        group = Group()
        gw = group.makegateway("popen")
        assert list(group) == [gw]
        group._cleanup_atexit()
        assert not group._activegateways

    def test_gateway_and_id(self):
        group = Group()
        gw = group.makegateway("popen//id=hello")
        assert group["hello"] == gw
        py.test.raises((TypeError, AttributeError), "del group['hello']")
        py.test.raises((TypeError, AttributeError), "group['hello'] = 5")
        assert 'hello' in group
        assert gw in group
        assert len(group) == 1
        gw.exit()
        assert 'hello' not in group
        py.test.raises(KeyError, "group['hello']")

    def test_default_group(self):
        oldlist = list(execnet.default_group)
        gw = execnet.makegateway("popen")
        newlist = list(execnet.default_group)
        assert len(newlist) == len(oldlist) + 1
        assert gw in newlist
        assert gw not in oldlist

