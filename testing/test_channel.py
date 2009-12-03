"""
mostly functional tests of gateways.
"""
import os, sys, time
import py
import execnet
from execnet import gateway_base, gateway
needs_early_gc = py.test.mark.skipif("not hasattr(sys, 'getrefcount')")
needs_osdup = py.test.mark.skipif("not hasattr(os, 'dup')")
queue = py.builtin._tryimport('queue', 'Queue')

TESTTIMEOUT = 10.0 # seconds

class TestChannelBasicBehaviour:
    def test_serialize_error(self, gw):
        ch = gw.remote_exec("channel.send(ValueError(42))")
        excinfo = py.test.raises(ch.RemoteError, ch.receive)
        assert "can't serialize" in str(excinfo.value)

    def test_channel_close_and_then_receive_error(self, gw):
        channel = gw.remote_exec('raise ValueError')
        py.test.raises(channel.RemoteError, channel.receive)

    def test_channel_finish_and_then_EOFError(self, gw):
        channel = gw.remote_exec('channel.send(42)')
        x = channel.receive()
        assert x == 42
        py.test.raises(EOFError, channel.receive)
        py.test.raises(EOFError, channel.receive)
        py.test.raises(EOFError, channel.receive)

    def test_waitclose_timeouterror(self, gw):
        channel = gw.remote_exec("channel.receive()")
        py.test.raises(channel.TimeoutError, channel.waitclose, 0.02)
        channel.send(1)
        channel.waitclose(timeout=TESTTIMEOUT)

    def test_channel_receive_timeout(self, gw):
        channel = gw.remote_exec('channel.send(channel.receive())')
        py.test.raises(channel.TimeoutError, "channel.receive(timeout=0.2)")
        channel.send(1)
        x = channel.receive(timeout=0.1)

    def test_channel_close_and_then_receive_error_multiple(self, gw):
        channel = gw.remote_exec('channel.send(42) ; raise ValueError')
        x = channel.receive()
        assert x == 42
        py.test.raises(channel.RemoteError, channel.receive)

    def test_channel__local_close(self, gw):
        channel = gw._channelfactory.new()
        gw._channelfactory._local_close(channel.id)
        channel.waitclose(0.1)

    def test_channel__local_close_error(self, gw):
        channel = gw._channelfactory.new()
        gw._channelfactory._local_close(channel.id,
                                            channel.RemoteError("error"))
        py.test.raises(channel.RemoteError, channel.waitclose, 0.01)

    def test_channel_error_reporting(self, gw):
        channel = gw.remote_exec('def foo():\n  return foobar()\nfoo()\n')
        try:
            channel.receive()
        except channel.RemoteError:
            e = sys.exc_info()[1]
            assert str(e).startswith('Traceback (most recent call last):')
            assert str(e).find('NameError: global name \'foobar\' '
                               'is not defined') > -1
        else:
            py.test.fail('No exception raised')

    def test_channel_syntax_error(self, gw):
        # missing colon
        channel = gw.remote_exec('def foo()\n return 1\nfoo()\n')
        try:
            channel.receive()
        except channel.RemoteError:
            e = sys.exc_info()[1]
            assert str(e).startswith('Traceback (most recent call last):')
            assert str(e).find('SyntaxError') > -1

    def test_channel_iter(self, gw):
        channel = gw.remote_exec("""
              for x in range(3):
                channel.send(x)
        """)
        l = list(channel)
        assert l == [0, 1, 2]

    def test_channel_passing_over_channel(self, gw):
        channel = gw.remote_exec('''
                    c = channel.gateway.newchannel()
                    channel.send(c)
                    c.send(42)
                  ''')
        c = channel.receive()
        x = c.receive()
        assert x == 42

        # check that the both sides previous channels are really gone
        channel.waitclose(TESTTIMEOUT)
        #assert c.id not in gw._channelfactory
        newchan = gw.remote_exec('''
                    assert %d not in channel.gateway._channelfactory._channels
                  ''' % (channel.id))
        newchan.waitclose(TESTTIMEOUT)
        assert channel.id not in gw._channelfactory._channels

    def test_channel_receiver_callback(self, gw):
        l = []
        #channel = gw.newchannel(receiver=l.append)
        channel = gw.remote_exec(source='''
            channel.send(42)
            channel.send(13)
            channel.send(channel.gateway.newchannel())
            ''')
        channel.setcallback(callback=l.append)
        py.test.raises(IOError, channel.receive)
        channel.waitclose(TESTTIMEOUT)
        assert len(l) == 3
        assert l[:2] == [42,13]
        assert isinstance(l[2], channel.__class__)

    def test_channel_callback_after_receive(self, gw):
        l = []
        channel = gw.remote_exec(source='''
            channel.send(42)
            channel.send(13)
            channel.send(channel.gateway.newchannel())
            ''')
        x = channel.receive()
        assert x == 42
        channel.setcallback(callback=l.append)
        py.test.raises(IOError, channel.receive)
        channel.waitclose(TESTTIMEOUT)
        assert len(l) == 2
        assert l[0] == 13
        assert isinstance(l[1], channel.__class__)

    def test_waiting_for_callbacks(self, gw):
        l = []
        def callback(msg):
            import time; time.sleep(0.2)
            l.append(msg)
        channel = gw.remote_exec(source='''
            channel.send(42)
            ''')
        channel.setcallback(callback)
        channel.waitclose(TESTTIMEOUT)
        assert l == [42]

    def test_channel_callback_stays_active(self, gw):
        self.check_channel_callback_stays_active(gw, earlyfree=True)

    def check_channel_callback_stays_active(self, gw, earlyfree=True):
        # with 'earlyfree==True', this tests the "sendonly" channel state.
        l = []
        channel = gw.remote_exec(source='''
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
            ''')
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
                py.test.fail("timed out waiting for the answer[%d]" % len(l))
            time.sleep(0.04)   # busy-wait
        assert l == [0, 100, 200, 300, 400]
        return subchannel

    @needs_early_gc
    def test_channel_callback_remote_freed(self, gw):
        channel = self.check_channel_callback_stays_active(gw, earlyfree=False)
        # freed automatically at the end of producer()
        channel.waitclose(TESTTIMEOUT)

    def test_channel_endmarker_callback(self, gw):
        l = []
        channel = gw.remote_exec(source='''
            channel.send(42)
            channel.send(13)
            channel.send(channel.gateway.newchannel())
            ''')
        channel.setcallback(l.append, 999)
        py.test.raises(IOError, channel.receive)
        channel.waitclose(TESTTIMEOUT)
        assert len(l) == 4
        assert l[:2] == [42,13]
        assert isinstance(l[2], channel.__class__)
        assert l[3] == 999

    def test_channel_endmarker_callback_error(self, gw):
        q = queue.Queue()
        channel = gw.remote_exec(source='''
            raise ValueError()
        ''')
        channel.setcallback(q.put, endmarker=999)
        val = q.get(TESTTIMEOUT)
        assert val == 999
        err = channel._getremoteerror()
        assert err
        assert str(err).find("ValueError") != -1

class TestChannelFile:
    def test_channel_file_write(self, gw):
        channel = gw.remote_exec("""
            f = channel.makefile()
            f.write("hello world\\n")
            f.close()
            channel.send(42)
        """)
        first = channel.receive()
        assert first.strip() == 'hello world'
        second = channel.receive()
        assert second == 42

    def test_channel_file_write_error(self, gw):
        channel = gw.remote_exec("pass")
        f = channel.makefile()
        channel.waitclose(TESTTIMEOUT)
        py.test.raises(IOError, f.write, 'hello')

    def test_channel_file_proxyclose(self, gw):
        channel = gw.remote_exec("""
            f = channel.makefile(proxyclose=True)
            f.write("hello world")
            f.close()
            channel.send(42)
        """)
        first = channel.receive()
        assert first.strip() == 'hello world'
        py.test.raises(EOFError, channel.receive)

    def test_channel_file_read(self, gw):
        channel = gw.remote_exec("""
            f = channel.makefile(mode='r')
            s = f.read(2)
            channel.send(s)
            s = f.read(5)
            channel.send(s)
        """)
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
        channel = gw.remote_exec("""
            channel.send('123\\n45')
        """)
        channel.waitclose(TESTTIMEOUT)
        f = channel.makefile(mode="r")
        s = f.readline()
        assert s == "123\n"
        s = f.readline()
        assert s == "45"

    def test_channel_makefile_incompatmode(self, gw):
        channel = gw.newchannel()
        py.test.raises(ValueError, 'channel.makefile("rw")')


class TestPopenGateway:
    gwtype = 'popen'

    def test_chdir_separation(self, tmpdir):
        old = tmpdir.chdir()
        try:
            gw = execnet.makegateway('popen')
        finally:
            waschangedir = old.chdir()
        c = gw.remote_exec("import os ; channel.send(os.getcwd())")
        x = c.receive()
        assert x == str(waschangedir)

    def test_remoteerror_readable_traceback(self, gw):
        e = py.test.raises(gateway_base.RemoteError, 
            'gw.remote_exec("x y").waitclose()')
        assert "gateway_base" in e.value.formatted 

    def test_many_popen(self):
        num = 4
        l = []
        for i in range(num):
            l.append(execnet.makegateway('popen'))
        channels = []
        for gw in l:
            channel = gw.remote_exec("""channel.send(42)""")
            channels.append(channel)
##        try:
##            while channels:
##                channel = channels.pop()
##                try:
##                    ret = channel.receive()
##                    assert ret == 42
##                finally:
##                    channel.gateway.exit()
##        finally:
##            for x in channels:
##                x.gateway.exit()
        while channels:
            channel = channels.pop()
            ret = channel.receive()
            assert ret == 42

    def test_rinfo_popen(self, gw):
        rinfo = gw._rinfo()
        assert rinfo.executable == py.std.sys.executable
        assert rinfo.cwd == py.std.os.getcwd()
        assert rinfo.version_info == py.std.sys.version_info

    def test_waitclose_on_remote_killed(self):
        gw = execnet.makegateway('popen')
        channel = gw.remote_exec("""
            import os
            import time
            channel.send(os.getpid())
            time.sleep(100)
        """)
        remotepid = channel.receive()
        py.process.kill(remotepid)
        py.test.raises(EOFError, "channel.waitclose(TESTTIMEOUT)")
        py.test.raises(IOError, channel.send, None)
        py.test.raises(EOFError, channel.receive)

    def test_receive_on_remote_io_closed(self):
        gw = execnet.makegateway('popen')
        channel = gw.remote_exec("""
            raise SystemExit()
        """)
        py.test.raises(EOFError, channel.receive)

def test_socket_gw_host_not_found(gw):
    py.test.raises(execnet.HostNotFound,
            'execnet.makegateway("socket=qwepoipqwe:9000")'
    )

class TestSshPopenGateway:
    gwtype = "ssh"

    def test_sshconfig_config_parsing(self, monkeypatch):
        import subprocess
        l = []
        monkeypatch.setattr(subprocess, 'Popen',
            lambda *args, **kwargs: l.append(args[0]))
        py.test.raises(AttributeError,
            """execnet.makegateway("ssh=xyz//ssh_config=qwe")""")
        assert len(l) == 1
        popen_args = l[0]
        i = popen_args.index('-F')
        assert popen_args[i+1] == "qwe"

    def test_sshaddress(self, gw, specssh):
        assert gw.remoteaddress == specssh.ssh

    def test_host_not_found(self, gw):
        py.test.raises(execnet.HostNotFound,
            "execnet.makegateway('ssh=nowhere.codespeak.net')")

class TestThreads:
    def test_threads(self):
        gw = execnet.makegateway('popen')
        gw.remote_init_threads(3)
        c1 = gw.remote_exec("channel.send(channel.receive())")
        c2 = gw.remote_exec("channel.send(channel.receive())")
        c2.send(1)
        res = c2.receive()
        assert res == 1
        c1.send(42)
        res = c1.receive()
        assert res == 42

    def test_status_with_threads(self):
        gw = execnet.makegateway('popen')
        gw.remote_init_threads(3)
        c1 = gw.remote_exec("channel.send(1) ; channel.receive()")
        c2 = gw.remote_exec("channel.send(2) ; channel.receive()")
        c1.receive()
        c2.receive()
        rstatus = gw.remote_status()
        assert rstatus.numexecuting == 2 + 1
        assert rstatus.execqsize == 0
        c1.send(1)
        c2.send(1)
        c1.waitclose()
        c2.waitclose()
        rstatus = gw.remote_status()
        assert rstatus.numexecuting == 0 + 1
        assert rstatus.execqsize == 0

    def test_threads_twice(self):
        gw = execnet.makegateway('popen')
        gw.remote_init_threads(3)
        py.test.raises(IOError, gw.remote_init_threads, 3)


class TestTracing:        
    def test_debug(self, monkeypatch):
        monkeypatch.setenv('EXECNET_DEBUG', "1")
        source = py.std.inspect.getsource(gateway_base)
        d = {}
        gateway_base.do_exec(source, d) 
        assert 'debugfile' in d 

    def test_popen_filetracing(self, monkeypatch):
        monkeypatch.setenv('EXECNET_DEBUG', "1")
        gw = execnet.makegateway("popen")
        pid = gw.remote_exec("import os ; channel.send(os.getpid())").receive()
        tmpdir = py.path.local(py.std.tempfile.gettempdir())
        slavefile = tmpdir.join("execnet-debug-%s" % pid)
        slave_line = "creating slavegateway"
        for line in slavefile.readlines():
            if slave_line in line:
                break
        else:
            py.test.fail("did not find %r in tracefile" %(slave_line,))
        gw.exit()

    @needs_osdup
    def test_popen_stderr_tracing(self, capfd, monkeypatch):
        monkeypatch.setenv('EXECNET_DEBUG', "2")
        gw = execnet.makegateway("popen")
        pid = gw.remote_exec("import os ; channel.send(os.getpid())").receive()
        out, err = capfd.readouterr()
        slave_line = "[%s] creating slavegateway" % pid
        assert slave_line in err
        gw.exit()

def test_nodebug():
    from execnet import gateway_base
    assert not hasattr(gateway_base, 'debugfile')


