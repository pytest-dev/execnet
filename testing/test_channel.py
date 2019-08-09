# -*- coding: utf-8 -*-
"""
mostly functional tests of gateways.
"""
import time

import py
import pytest
from test_gateway import _find_version

needs_early_gc = pytest.mark.skipif("not hasattr(sys, 'getrefcount')")
needs_osdup = pytest.mark.skipif("not hasattr(os, 'dup')")
queue = py.builtin._tryimport("queue", "Queue")
TESTTIMEOUT = 10.0  # seconds


class TestChannelBasicBehaviour:
    def test_serialize_error(self, gw):
        ch = gw.remote_exec("channel.send(ValueError(42))")
        excinfo = pytest.raises(ch.RemoteError, ch.receive)
        assert "can't serialize" in str(excinfo.value)

    def test_channel_close_and_then_receive_error(self, gw):
        channel = gw.remote_exec("raise ValueError")
        pytest.raises(channel.RemoteError, channel.receive)

    def test_channel_finish_and_then_EOFError(self, gw):
        channel = gw.remote_exec("channel.send(42)")
        x = channel.receive()
        assert x == 42
        pytest.raises(EOFError, channel.receive)
        pytest.raises(EOFError, channel.receive)
        pytest.raises(EOFError, channel.receive)

    def test_waitclose_timeouterror(self, gw):
        channel = gw.remote_exec("channel.receive()")
        pytest.raises(channel.TimeoutError, channel.waitclose, 0.02)
        channel.send(1)
        channel.waitclose(timeout=TESTTIMEOUT)

    def test_channel_receive_timeout(self, gw):
        channel = gw.remote_exec("channel.send(channel.receive())")
        with pytest.raises(channel.TimeoutError):
            channel.receive(timeout=0.2)
        channel.send(1)
        channel.receive(timeout=TESTTIMEOUT)

    def test_channel_receive_internal_timeout(self, gw, monkeypatch):
        channel = gw.remote_exec(
            """
            import time
            time.sleep(0.5)
            channel.send(1)
        """
        )
        monkeypatch.setattr(channel.__class__, "_INTERNALWAKEUP", 0.2)
        channel.receive()

    def test_channel_close_and_then_receive_error_multiple(self, gw):
        channel = gw.remote_exec("channel.send(42) ; raise ValueError")
        x = channel.receive()
        assert x == 42
        pytest.raises(channel.RemoteError, channel.receive)

    def test_channel__local_close(self, gw):
        channel = gw._channelfactory.new()
        gw._channelfactory._local_close(channel.id)
        channel.waitclose(0.1)

    def test_channel__local_close_error(self, gw):
        channel = gw._channelfactory.new()
        gw._channelfactory._local_close(channel.id, channel.RemoteError("error"))
        pytest.raises(channel.RemoteError, channel.waitclose, 0.01)

    def test_channel_error_reporting(self, gw):
        channel = gw.remote_exec("def foo():\n  return foobar()\nfoo()\n")
        excinfo = pytest.raises(channel.RemoteError, channel.receive)
        msg = str(excinfo.value)
        assert msg.startswith("Traceback (most recent call last):")
        assert "NameError" in msg
        assert "foobar" in msg

    def test_channel_syntax_error(self, gw):
        # missing colon
        channel = gw.remote_exec("def foo()\n return 1\nfoo()\n")
        excinfo = pytest.raises(channel.RemoteError, channel.receive)
        msg = str(excinfo.value)
        assert msg.startswith("Traceback (most recent call last):")
        assert "SyntaxError" in msg

    def test_channel_iter(self, gw):
        channel = gw.remote_exec(
            """
              for x in range(3):
                channel.send(x)
        """
        )
        l = list(channel)
        assert l == [0, 1, 2]

    def test_channel_pass_in_structure(self, gw):
        channel = gw.remote_exec(
            """
            ch1, ch2 = channel.receive()
            data = ch1.receive()
            ch2.send(data+1)
        """
        )
        newchan1 = gw.newchannel()
        newchan2 = gw.newchannel()
        channel.send((newchan1, newchan2))
        newchan1.send(1)
        data = newchan2.receive()
        assert data == 2

    def test_channel_multipass(self, gw):
        channel = gw.remote_exec(
            """
            channel.send(channel)
            xchan = channel.receive()
            assert xchan == channel
        """
        )
        newchan = channel.receive()
        assert newchan == channel
        channel.send(newchan)
        channel.waitclose()

    def test_channel_passing_over_channel(self, gw):
        channel = gw.remote_exec(
            """
                    c = channel.gateway.newchannel()
                    channel.send(c)
                    c.send(42)
                  """
        )
        c = channel.receive()
        x = c.receive()
        assert x == 42

        # check that the both sides previous channels are really gone
        channel.waitclose(TESTTIMEOUT)
        # assert c.id not in gw._channelfactory
        newchan = gw.remote_exec(
            """
                    assert %d not in channel.gateway._channelfactory._channels
                  """
            % channel.id
        )
        newchan.waitclose(TESTTIMEOUT)
        assert channel.id not in gw._channelfactory._channels

    def test_channel_receiver_callback(self, gw):
        l = []
        # channel = gw.newchannel(receiver=l.append)
        channel = gw.remote_exec(
            source="""
            channel.send(42)
            channel.send(13)
            channel.send(channel.gateway.newchannel())
            """
        )
        channel.setcallback(callback=l.append)
        pytest.raises(IOError, channel.receive)
        channel.waitclose(TESTTIMEOUT)
        assert len(l) == 3
        assert l[:2] == [42, 13]
        assert isinstance(l[2], channel.__class__)

    def test_channel_callback_after_receive(self, gw):
        l = []
        channel = gw.remote_exec(
            source="""
            channel.send(42)
            channel.send(13)
            channel.send(channel.gateway.newchannel())
            """
        )
        x = channel.receive()
        assert x == 42
        channel.setcallback(callback=l.append)
        pytest.raises(IOError, channel.receive)
        channel.waitclose(TESTTIMEOUT)
        assert len(l) == 2
        assert l[0] == 13
        assert isinstance(l[1], channel.__class__)

    def test_waiting_for_callbacks(self, gw):
        l = []

        def callback(msg):
            import time

            time.sleep(0.2)
            l.append(msg)

        channel = gw.remote_exec(
            source="""
            channel.send(42)
            """
        )
        channel.setcallback(callback)
        channel.waitclose(TESTTIMEOUT)
        assert l == [42]

    def test_channel_callback_stays_active(self, gw):
        self.check_channel_callback_stays_active(gw, earlyfree=True)

    def check_channel_callback_stays_active(self, gw, earlyfree=True):
        if gw.spec.execmodel == "gevent":
            pytest.xfail("investigate gevent failure")
        # with 'earlyfree==True', this tests the "sendonly" channel state.
        l = []
        channel = gw.remote_exec(
            source="""
            try:
                import thread
            except ImportError:
                import _thread as thread
            import time
            def producer(subchannel):
                for i in range(5):
                    time.sleep(0.15)
                    subchannel.send(i*100)
            channel2 = channel.receive()
            thread.start_new_thread(producer, (channel2,))
            del channel2
            """
        )
        subchannel = gw.newchannel()
        subchannel.setcallback(l.append)
        channel.send(subchannel)
        if earlyfree:
            subchannel = None
        counter = 100
        while len(l) < 5:
            if subchannel and subchannel.isclosed():
                break
            counter -= 1
            print(counter)
            if not counter:
                pytest.fail("timed out waiting for the answer[%d]" % len(l))
            time.sleep(0.04)  # busy-wait
        assert l == [0, 100, 200, 300, 400]
        return subchannel

    @needs_early_gc
    def test_channel_callback_remote_freed(self, gw):
        channel = self.check_channel_callback_stays_active(gw, earlyfree=False)
        # freed automatically at the end of producer()
        channel.waitclose(TESTTIMEOUT)

    def test_channel_endmarker_callback(self, gw):
        l = []
        channel = gw.remote_exec(
            source="""
            channel.send(42)
            channel.send(13)
            channel.send(channel.gateway.newchannel())
            """
        )
        channel.setcallback(l.append, 999)
        pytest.raises(IOError, channel.receive)
        channel.waitclose(TESTTIMEOUT)
        assert len(l) == 4
        assert l[:2] == [42, 13]
        assert isinstance(l[2], channel.__class__)
        assert l[3] == 999

    def test_channel_endmarker_callback_error(self, gw):
        q = gw.execmodel.queue.Queue()
        channel = gw.remote_exec(
            source="""
            raise ValueError()
        """
        )
        channel.setcallback(q.put, endmarker=999)
        val = q.get(TESTTIMEOUT)
        assert val == 999
        err = channel._getremoteerror()
        assert err
        assert str(err).find("ValueError") != -1

    def test_channel_callback_error(self, gw):
        channel = gw.remote_exec(
            """
            def f(item):
                raise ValueError(42)
            ch = channel.gateway.newchannel()
            ch.setcallback(f)
            channel.send(ch)
            channel.receive()
            assert ch.isclosed()
        """
        )
        subchan = channel.receive()
        subchan.send(1)
        with pytest.raises(subchan.RemoteError) as excinfo:
            subchan.waitclose(TESTTIMEOUT)
        assert "42" in excinfo.value.formatted
        channel.send(1)
        channel.waitclose()


class TestChannelFile:
    def test_channel_file_write(self, gw):
        channel = gw.remote_exec(
            """
            f = channel.makefile()
            f.write("hello world\\n")
            f.close()
            channel.send(42)
        """
        )
        first = channel.receive()
        assert first.strip() == "hello world"
        second = channel.receive()
        assert second == 42

    def test_channel_file_write_error(self, gw):
        channel = gw.remote_exec("pass")
        f = channel.makefile()
        assert not f.isatty()
        channel.waitclose(TESTTIMEOUT)
        with pytest.raises(IOError):
            f.write("hello")

    def test_channel_file_proxyclose(self, gw):
        channel = gw.remote_exec(
            """
            f = channel.makefile(proxyclose=True)
            f.write("hello world")
            f.close()
            channel.send(42)
        """
        )
        first = channel.receive()
        assert first.strip() == "hello world"
        pytest.raises(channel.RemoteError, channel.receive)

    def test_channel_file_read(self, gw):
        channel = gw.remote_exec(
            """
            f = channel.makefile(mode='r')
            s = f.read(2)
            channel.send(s)
            s = f.read(5)
            channel.send(s)
        """
        )
        channel.send("xyabcde")
        s1 = channel.receive()
        s2 = channel.receive()
        assert s1 == "xy"
        assert s2 == "abcde"

    def test_channel_file_read_empty(self, gw):
        channel = gw.remote_exec("pass")
        f = channel.makefile(mode="r")
        s = f.read(3)
        assert s == ""
        s = f.read(5)
        assert s == ""

    def test_channel_file_readline_remote(self, gw):
        channel = gw.remote_exec(
            """
            channel.send('123\\n45')
        """
        )
        channel.waitclose(TESTTIMEOUT)
        f = channel.makefile(mode="r")
        s = f.readline()
        assert s == "123\n"
        s = f.readline()
        assert s == "45"

    def test_channel_makefile_incompatmode(self, gw):
        channel = gw.newchannel()
        with pytest.raises(ValueError):
            channel.makefile("rw")


class TestStringCoerce:
    @pytest.mark.skipif('sys.version>="3.0"')
    def test_2to3(self, makegateway):
        python = _find_version("3")
        gw = makegateway("popen//python=%s" % python)
        ch = gw.remote_exec("channel.send(channel.receive());" * 2)
        ch.send("a")
        res = ch.receive()
        assert isinstance(res, unicode)

        ch.reconfigure(py3str_as_py2str=True)

        ch.send("a")
        res = ch.receive()
        assert isinstance(res, str)

        gw.reconfigure(py3str_as_py2str=True)
        ch = gw.remote_exec("channel.send(channel.receive());" * 2)

        ch.send("a")
        res = ch.receive()
        assert isinstance(res, str)
        ch.reconfigure(py3str_as_py2str=False, py2str_as_py3str=False)

        ch.send("a")
        res = ch.receive()
        assert isinstance(res, str)
        gw.exit()

    @pytest.mark.skipif('sys.version<"3.0"')
    def test_3to2(self, makegateway):
        python = _find_version("2")
        gw = makegateway("popen//python=%s" % python)

        ch = gw.remote_exec("channel.send(channel.receive());" * 2)
        ch.send(bytes("a", "ascii"))
        res = ch.receive()
        assert isinstance(res, str)

        ch.reconfigure(py3str_as_py2str=True, py2str_as_py3str=False)

        ch.send("a")
        res = ch.receive()
        assert isinstance(res, bytes)

        gw.reconfigure(py3str_as_py2str=True, py2str_as_py3str=False)
        ch = gw.remote_exec("channel.send(channel.receive());" * 2)

        ch.send("a")
        res = ch.receive()
        assert isinstance(res, bytes)

        ch.reconfigure(py3str_as_py2str=False, py2str_as_py3str=True)
        ch.send(bytes("a", "ascii"))
        res = ch.receive()
        assert isinstance(res, str)

        gw.exit()
