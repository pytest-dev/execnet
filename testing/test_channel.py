"""
mostly functional tests of gateways.
"""

from __future__ import annotations

import time

import pytest

from execnet.gateway import Gateway
from execnet.gateway_base import Channel

needs_early_gc = pytest.mark.skipif("not hasattr(sys, 'getrefcount')")
needs_osdup = pytest.mark.skipif("not hasattr(os, 'dup')")
TESTTIMEOUT = 10.0  # seconds


class TestChannelBasicBehaviour:
    def test_serialize_error(self, gw: Gateway) -> None:
        ch = gw.remote_exec("channel.send(ValueError(42))")
        excinfo = pytest.raises(ch.RemoteError, ch.receive)
        assert "can't serialize" in str(excinfo.value)

    def test_channel_close_and_then_receive_error(self, gw: Gateway) -> None:
        channel = gw.remote_exec("raise ValueError")
        pytest.raises(channel.RemoteError, channel.receive)

    def test_channel_finish_and_then_EOFError(self, gw: Gateway) -> None:
        channel = gw.remote_exec("channel.send(42)")
        x = channel.receive()
        assert x == 42
        pytest.raises(EOFError, channel.receive)
        pytest.raises(EOFError, channel.receive)
        pytest.raises(EOFError, channel.receive)

    def test_waitclose_timeouterror(self, gw: Gateway) -> None:
        channel = gw.remote_exec("channel.receive()")
        pytest.raises(channel.TimeoutError, channel.waitclose, 0.02)
        channel.send(1)
        channel.waitclose(timeout=TESTTIMEOUT)

    def test_channel_receive_timeout(self, gw: Gateway) -> None:
        channel = gw.remote_exec("channel.send(channel.receive())")
        with pytest.raises(channel.TimeoutError):
            channel.receive(timeout=0.2)
        channel.send(1)
        channel.receive(timeout=TESTTIMEOUT)

    def test_channel_receive_internal_timeout(
        self, gw: Gateway, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        channel = gw.remote_exec(
            """
            import time
            time.sleep(0.5)
            channel.send(1)
        """
        )
        monkeypatch.setattr(channel.__class__, "_INTERNALWAKEUP", 0.2)
        channel.receive()

    def test_channel_close_and_then_receive_error_multiple(self, gw: Gateway) -> None:
        channel = gw.remote_exec("channel.send(42) ; raise ValueError")
        x = channel.receive()
        assert x == 42
        pytest.raises(channel.RemoteError, channel.receive)

    def test_channel__local_close(self, gw: Gateway) -> None:
        channel = gw._channelfactory.new()
        gw._channelfactory._local_close(channel.id)
        channel.waitclose(0.1)

    def test_channel__local_close_error(self, gw: Gateway) -> None:
        channel = gw._channelfactory.new()
        gw._channelfactory._local_close(channel.id, channel.RemoteError("error"))
        pytest.raises(channel.RemoteError, channel.waitclose, 0.01)

    def test_channel_error_reporting(self, gw: Gateway) -> None:
        channel = gw.remote_exec("def foo():\n  return foobar()\nfoo()\n")
        excinfo = pytest.raises(channel.RemoteError, channel.receive)
        msg = str(excinfo.value)
        assert msg.startswith("Traceback (most recent call last):")
        assert "NameError" in msg
        assert "foobar" in msg

    def test_channel_syntax_error(self, gw: Gateway) -> None:
        # missing colon
        channel = gw.remote_exec("def foo()\n return 1\nfoo()\n")
        excinfo = pytest.raises(channel.RemoteError, channel.receive)
        msg = str(excinfo.value)
        assert msg.startswith("Traceback (most recent call last):")
        assert "SyntaxError" in msg

    def test_channel_iter(self, gw: Gateway) -> None:
        channel = gw.remote_exec(
            """
              for x in range(3):
                channel.send(x)
        """
        )
        l = list(channel)
        assert l == [0, 1, 2]

    def test_channel_pass_in_structure(self, gw: Gateway) -> None:
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

    def test_channel_multipass(self, gw: Gateway) -> None:
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

    def test_channel_passing_over_channel(self, gw: Gateway) -> None:
        channel = gw.remote_exec(
            """
            c = channel.gateway.newchannel()
            channel.send(c)
            c.send(42)
            """
        )
        c = channel.receive()
        assert isinstance(c, Channel)
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

    def test_channel_receiver_callback(self, gw: Gateway) -> None:
        l: list[int] = []
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

    def test_channel_callback_after_receive(self, gw: Gateway) -> None:
        l: list[int] = []
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

    def test_waiting_for_callbacks(self, gw: Gateway) -> None:
        l = []

        def callback(msg) -> None:
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

    def test_channel_callback_stays_active(self, gw: Gateway) -> None:
        self.check_channel_callback_stays_active(gw, earlyfree=True)

    def check_channel_callback_stays_active(
        self, gw: Gateway, earlyfree: bool = True
    ) -> Channel | None:
        if gw.spec.execmodel == "gevent":
            pytest.xfail("investigate gevent failure")
        # with 'earlyfree==True', this tests the "sendonly" channel state.
        l: list[int] = []
        channel = gw.remote_exec(
            source="""
            import _thread
            import time
            def producer(subchannel):
                for i in range(5):
                    time.sleep(0.15)
                    subchannel.send(i*100)
            channel2 = channel.receive()
            _thread.start_new_thread(producer, (channel2,))
            del channel2
            """
        )
        subchannel = gw.newchannel()
        subchannel.setcallback(l.append)
        channel.send(subchannel)
        subchan = None if earlyfree else subchannel
        counter = 100
        while len(l) < 5:
            if subchan and subchan.isclosed():
                break
            counter -= 1
            print(counter)
            if not counter:
                pytest.fail("timed out waiting for the answer[%d]" % len(l))
            time.sleep(0.04)  # busy-wait
        assert l == [0, 100, 200, 300, 400]
        return subchan

    @needs_early_gc
    def test_channel_callback_remote_freed(self, gw: Gateway) -> None:
        channel = self.check_channel_callback_stays_active(gw, earlyfree=False)
        assert channel is not None
        # freed automatically at the end of producer()
        channel.waitclose(TESTTIMEOUT)

    def test_channel_endmarker_callback(self, gw: Gateway) -> None:
        l: list[int | Channel] = []
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

    def test_channel_endmarker_callback_error(self, gw: Gateway) -> None:
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

    def test_channel_callback_error(self, gw: Gateway) -> None:
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
        assert isinstance(subchan, Channel)
        subchan.send(1)
        with pytest.raises(subchan.RemoteError) as excinfo:
            subchan.waitclose(TESTTIMEOUT)
        assert "42" in excinfo.value.formatted
        channel.send(1)
        channel.waitclose()


class TestChannelFile:
    def test_channel_file_write(self, gw: Gateway) -> None:
        channel = gw.remote_exec(
            """
            f = channel.makefile()
            f.write("hello world\\n")
            f.close()
            channel.send(42)
        """
        )
        first = channel.receive()
        assert isinstance(first, str)
        assert first.strip() == "hello world"
        second = channel.receive()
        assert second == 42

    def test_channel_file_write_error(self, gw: Gateway) -> None:
        channel = gw.remote_exec("pass")
        f = channel.makefile()
        assert not f.isatty()
        channel.waitclose(TESTTIMEOUT)
        with pytest.raises(IOError):
            f.write(b"hello")

    def test_channel_file_proxyclose(self, gw: Gateway) -> None:
        channel = gw.remote_exec(
            """
            f = channel.makefile(proxyclose=True)
            f.write("hello world")
            f.close()
            channel.send(42)
        """
        )
        first = channel.receive()
        assert isinstance(first, str)
        assert first.strip() == "hello world"
        pytest.raises(channel.RemoteError, channel.receive)

    def test_channel_file_read(self, gw: Gateway) -> None:
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

    def test_channel_file_read_empty(self, gw: Gateway) -> None:
        channel = gw.remote_exec("pass")
        f = channel.makefile(mode="r")
        s = f.read(3)
        assert s == ""
        s = f.read(5)
        assert s == ""

    def test_channel_file_readline_remote(self, gw: Gateway) -> None:
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

    def test_channel_makefile_incompatmode(self, gw: Gateway) -> None:
        channel = gw.newchannel()
        with pytest.raises(ValueError):
            channel.makefile("rw")  # type: ignore[call-overload]
