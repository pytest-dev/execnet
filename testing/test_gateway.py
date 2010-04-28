"""
mostly functional tests of gateways.
"""
import os, sys, time
import py
import execnet
from execnet import gateway_base, gateway

TESTTIMEOUT = 10.0 # seconds
needs_osdup = py.test.mark.skipif("not hasattr(os, 'dup')")

def test_deprecation(recwarn, monkeypatch):
    execnet.PopenGateway()
    assert recwarn.pop(DeprecationWarning)
    monkeypatch.setattr(py.std.socket, 'socket', lambda *args: 0/0)
    py.test.raises(Exception, 'execnet.SocketGateway("localhost", 8811)')
    assert recwarn.pop(DeprecationWarning)
    monkeypatch.setattr(py.std.subprocess, 'Popen', lambda *args,**kwargs: 0/0)
    py.test.raises(Exception, 'execnet.SshGateway("not-existing")')
    assert recwarn.pop(DeprecationWarning)

class TestBasicGateway:
    def test_correct_setup(self, gw):
        assert gw.hasreceiver()
        assert gw in gw._group 
        assert gw.id in gw._group 
        assert gw.spec 

    def test_repr_doesnt_crash(self, gw):
        assert isinstance(repr(gw), str)

    def test_attribute__name__(self, gw):
        channel = gw.remote_exec("channel.send(__name__)")
        name = channel.receive()
        assert name == "__channelexec__"

    def test_gateway_status_simple(self, gw):
        status = gw.remote_status()
        assert not status.execqsize
        assert status.numexecuting == 0

    def test_gateway_status_no_real_channel(self, gw):
        numchan = gw._channelfactory.channels()
        st = gw.remote_status()
        numchan2 = gw._channelfactory.channels()
        # note that on CPython this can not really
        # fail because refcounting leads to immediate
        # closure of temporary channels
        assert numchan2 == numchan

    def test_gateway_status_busy(self, gw):
        numchannels = gw.remote_status().numchannels
        ch1 = gw.remote_exec("channel.send(1); channel.receive()")
        ch2 = gw.remote_exec("channel.receive()")
        ch1.receive()
        status = gw.remote_status()
        assert status.numexecuting == 1 # number of active execution threads
        assert status.execqsize == 1 # one more queued
        assert status.numchannels == numchannels + 2
        ch1.send(None)
        ch2.send(None)
        ch1.waitclose()
        ch2.waitclose()
        status = gw.remote_status()
        assert status.execqsize == 0
        assert status.numexecuting == 0
        # race condition 
        assert status.numchannels <= numchannels + 1

    def test_remote_exec_module(self, tmpdir, gw):
        p = tmpdir.join("remotetest.py")
        p.write("channel.send(1)")
        mod = type(os)("remotetest")
        mod.__file__ = str(p)
        channel = gw.remote_exec(mod)
        name = channel.receive()
        assert name == 1

    def test_correct_setup_no_py(self, gw):
        channel = gw.remote_exec("""
            import sys
            channel.send(list(sys.modules))
        """)
        remotemodules = channel.receive()
        assert 'py' not in remotemodules, (
                "py should not be imported on remote side")

    def test_remote_exec_waitclose(self, gw):
        channel = gw.remote_exec('pass')
        channel.waitclose(TESTTIMEOUT)

    def test_remote_exec_waitclose_2(self, gw):
        channel = gw.remote_exec('def gccycle(): pass')
        channel.waitclose(TESTTIMEOUT)

    def test_remote_exec_waitclose_noarg(self, gw):
        channel = gw.remote_exec('pass')
        channel.waitclose()

    def test_remote_exec_error_after_close(self, gw):
        channel = gw.remote_exec('pass')
        channel.waitclose(TESTTIMEOUT)
        py.test.raises(IOError, channel.send, 0)

    def test_remote_exec_no_explicit_close(self, gw):
        channel = gw.remote_exec('channel.close()')
        excinfo = py.test.raises(channel.RemoteError, 
            "channel.waitclose(TESTTIMEOUT)")
        assert "explicit" in excinfo.value.formatted

    def test_remote_exec_channel_anonymous(self, gw):
        channel = gw.remote_exec('''
           obj = channel.receive()
           channel.send(obj)
        ''')
        channel.send(42)
        result = channel.receive()
        assert result == 42

    @needs_osdup
    def test_confusion_from_os_write_stdout(self, gw):
        channel = gw.remote_exec("""
            import os
            os.write(1, 'confusion!'.encode('ascii'))
            channel.send(channel.receive() * 6)
            channel.send(channel.receive() * 6)
        """)
        channel.send(3)
        res = channel.receive()
        assert res == 18
        channel.send(7)
        res = channel.receive()
        assert res == 42

    @needs_osdup
    def test_confusion_from_os_write_stderr(self, gw):
        channel = gw.remote_exec("""
            import os
            os.write(2, 'test'.encode('ascii'))
            channel.send(channel.receive() * 6)
            channel.send(channel.receive() * 6)
        """)
        channel.send(3)
        res = channel.receive()
        assert res == 18
        channel.send(7)
        res = channel.receive()
        assert res == 42

    def test__rinfo(self, gw):
        rinfo = gw._rinfo()
        assert rinfo.executable
        assert rinfo.cwd
        assert rinfo.version_info
        s = repr(rinfo)
        old = gw.remote_exec("""
            import os.path
            cwd = os.getcwd()
            channel.send(os.path.basename(cwd))
            os.chdir('..')
        """).receive()
        try:
            rinfo2 = gw._rinfo()
            assert rinfo2.cwd == rinfo.cwd
            rinfo3 = gw._rinfo(update=True)
            assert rinfo3.cwd != rinfo2.cwd
        finally:
            gw._cache_rinfo = rinfo
            gw.remote_exec("import os ; os.chdir(%r)" % old).waitclose()

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
        assert x.lower() == str(waschangedir).lower()

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

    def test_receive_on_remote_sysexit(self, gw):
        channel = gw.remote_exec("""
            raise SystemExit()
        """)
        py.test.raises(channel.RemoteError, channel.receive)

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

    def test_threads_race_sending(self):
        # multiple threads sending data in parallel 
        gw = execnet.makegateway("popen")
        num = 5
        gw.remote_init_threads(num)
        print ("remote_init_threads(%d)" % num)
        channels = []
        for x in range(num):
            ch = gw.remote_exec("""
                for x in range(10):
                    channel.send(''*1000) 
                channel.receive()
            """)
            channels.append(ch)
        for ch in channels:
            for x in range(10):
                ch.receive(TESTTIMEOUT)
            ch.send(1)
        for ch in channels:
            ch.waitclose(TESTTIMEOUT)

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
    def test_popen_filetracing(self, testdir, monkeypatch):
        tmpdir = testdir.tmpdir
        monkeypatch.setenv("TMP", tmpdir)
        monkeypatch.setenv("TEMP", tmpdir) # windows
        monkeypatch.setenv('EXECNET_DEBUG', "1")
        gw = execnet.makegateway("popen")
        pid = gw.remote_exec("import os ; channel.send(os.getpid())").receive()
        slavefile = tmpdir.join("execnet-debug-%s" % pid)
        assert slavefile.check()
        slave_line = "creating slavegateway"
        for line in slavefile.readlines():
            if slave_line in line:
                break
        else:
            py.test.fail("did not find %r in tracefile" %(slave_line,))
        gw.exit()

    def test_popen_stderr_tracing(self, capfd, monkeypatch):
        monkeypatch.setenv('EXECNET_DEBUG', "2")
        gw = execnet.makegateway("popen")
        pid = gw.remote_exec("import os ; channel.send(os.getpid())").receive()
        out, err = capfd.readouterr()
        slave_line = "[%s] creating slavegateway" % pid
        assert slave_line in err
        gw.exit()

    def test_no_tracing_by_default(self):
        assert gateway_base.trace == gateway_base.notrace, \
                "trace does not to default to empty tracing"
