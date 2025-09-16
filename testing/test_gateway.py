"""
mostly functional tests of gateways.
"""

from __future__ import annotations

import os
import pathlib
import shutil
import signal
import sys
from textwrap import dedent
from typing import Callable

import pytest

import execnet
from execnet import gateway_base
from execnet import gateway_io
from execnet.gateway import Gateway

TESTTIMEOUT = 10.0  # seconds
needs_osdup = pytest.mark.skipif("not hasattr(os, 'dup')")


flakytest = pytest.mark.xfail(
    reason="on some systems this test fails due to timing problems"
)
skip_win_pypy = pytest.mark.xfail(
    condition=hasattr(sys, "pypy_version_info") and sys.platform.startswith("win"),
    reason="failing on Windows on PyPy (#63)",
)


class TestBasicGateway:
    def test_correct_setup(self, gw: Gateway) -> None:
        assert gw.hasreceiver()
        assert gw in gw._group
        assert gw.id in gw._group
        assert gw.spec

    def test_repr_doesnt_crash(self, gw: Gateway) -> None:
        assert isinstance(repr(gw), str)

    def test_attribute__name__(self, gw: Gateway) -> None:
        channel = gw.remote_exec("channel.send(__name__)")
        name = channel.receive()
        assert name == "__channelexec__"

    def test_gateway_status_simple(self, gw: Gateway) -> None:
        status = gw.remote_status()
        assert status.numexecuting == 0

    def test_exc_info_is_clear_after_gateway_startup(self, gw: Gateway) -> None:
        ch = gw.remote_exec(
            """
                import traceback, sys
                excinfo = sys.exc_info()
                if excinfo != (None, None, None):
                    r = traceback.format_exception(*excinfo)
                else:
                    r = 0
                channel.send(r)
        """
        )
        res = ch.receive()
        if res != 0:
            pytest.fail("remote raised\n%s" % res)

    def test_gateway_status_no_real_channel(self, gw: Gateway) -> None:
        numchan = gw._channelfactory.channels()
        gw.remote_status()
        numchan2 = gw._channelfactory.channels()
        # note that on CPython this can not really
        # fail because refcounting leads to immediate
        # closure of temporary channels
        assert numchan2 == numchan

    @flakytest
    def test_gateway_status_busy(self, gw: Gateway) -> None:
        numchannels = gw.remote_status().numchannels
        ch1 = gw.remote_exec("channel.send(1); channel.receive()")
        ch2 = gw.remote_exec("channel.receive()")
        ch1.receive()
        status = gw.remote_status()
        assert status.numexecuting == 2  # number of active execution threads
        assert status.numchannels == numchannels + 2
        ch1.send(None)
        ch2.send(None)
        ch1.waitclose()
        ch2.waitclose()
        for i in range(10):
            status = gw.remote_status()
            if status.numexecuting == 0:
                break
        else:
            pytest.fail("did not get correct remote status")
        # race condition
        assert status.numchannels <= numchannels

    def test_remote_exec_module(self, tmp_path: pathlib.Path, gw: Gateway) -> None:
        p = tmp_path / "remotetest.py"
        p.write_text("channel.send(1)")
        mod = type(os)("remotetest")
        mod.__file__ = str(p)
        channel = gw.remote_exec(mod)
        name = channel.receive()
        assert name == 1
        p.write_text("channel.send(2)")
        channel = gw.remote_exec(mod)
        name = channel.receive()
        assert name == 2

    def test_remote_exec_module_is_removed(
        self, gw: Gateway, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        remotetest = tmp_path / "remote.py"
        remotetest.write_text(
            dedent(
                """
            def remote():
                return True

            if __name__ == '__channelexec__':
                for item in channel:  # noqa
                    channel.send(eval(item))  # noqa

            """
            )
        )

        monkeypatch.syspath_prepend(tmp_path)
        import remote  # type: ignore[import-not-found]

        ch = gw.remote_exec(remote)
        # simulate sending the code to a remote location that does not have
        # access to the source
        shutil.rmtree(tmp_path)
        ch.send("remote()")
        try:
            result = ch.receive()
        finally:
            ch.close()

        assert result is True

    def test_remote_exec_module_with_traceback(
        self,
        gw: Gateway,
        tmp_path: pathlib.Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        remotetestpy = tmp_path / "remotetest.py"
        remotetestpy.write_text(
            dedent(
                """
            def run_me(channel=None):
                raise ValueError('me')

            if __name__ == '__channelexec__':
                run_me()
            """
            )
        )

        monkeypatch.syspath_prepend(tmp_path)
        import remotetest  # type: ignore[import-not-found]

        ch = gw.remote_exec(remotetest)
        try:
            ch.receive()
        except execnet.gateway_base.RemoteError as e:
            assert 'remotetest.py", line 3, in run_me' in str(e)
            assert "ValueError: me" in str(e)
        finally:
            ch.close()

        ch = gw.remote_exec(remotetest.run_me)
        try:
            ch.receive()
        except execnet.gateway_base.RemoteError as e:
            assert 'remotetest.py", line 3, in run_me' in str(e)
            assert "ValueError: me" in str(e)
        finally:
            ch.close()

    def test_correct_setup_no_py(self, gw: Gateway) -> None:
        channel = gw.remote_exec(
            """
            import sys
            channel.send(list(sys.modules))
        """
        )
        remotemodules = channel.receive()
        assert isinstance(remotemodules, list)
        assert "py" not in remotemodules, "py should not be imported on remote side"

    def test_remote_exec_waitclose(self, gw: Gateway) -> None:
        channel = gw.remote_exec("pass")
        channel.waitclose(TESTTIMEOUT)

    def test_remote_exec_waitclose_2(self, gw: Gateway) -> None:
        channel = gw.remote_exec("def gccycle(): pass")
        channel.waitclose(TESTTIMEOUT)

    def test_remote_exec_waitclose_noarg(self, gw: Gateway) -> None:
        channel = gw.remote_exec("pass")
        channel.waitclose()

    def test_remote_exec_error_after_close(self, gw: Gateway) -> None:
        channel = gw.remote_exec("pass")
        channel.waitclose(TESTTIMEOUT)
        pytest.raises(IOError, channel.send, 0)

    def test_remote_exec_no_explicit_close(self, gw: Gateway) -> None:
        channel = gw.remote_exec("channel.close()")
        with pytest.raises(channel.RemoteError) as excinfo:
            channel.waitclose(TESTTIMEOUT)
        assert "explicit" in excinfo.value.formatted

    def test_remote_exec_channel_anonymous(self, gw: Gateway) -> None:
        channel = gw.remote_exec(
            """
           obj = channel.receive()
           channel.send(obj)
        """
        )
        channel.send(42)
        result = channel.receive()
        assert result == 42

    @needs_osdup
    def test_confusion_from_os_write_stdout(self, gw: Gateway) -> None:
        channel = gw.remote_exec(
            """
            import os
            os.write(1, 'confusion!'.encode('ascii'))
            channel.send(channel.receive() * 6)
            channel.send(channel.receive() * 6)
        """
        )
        channel.send(3)
        res = channel.receive()
        assert res == 18
        channel.send(7)
        res = channel.receive()
        assert res == 42

    @needs_osdup
    def test_confusion_from_os_write_stderr(self, gw: Gateway) -> None:
        channel = gw.remote_exec(
            """
            import os
            os.write(2, 'test'.encode('ascii'))
            channel.send(channel.receive() * 6)
            channel.send(channel.receive() * 6)
        """
        )
        channel.send(3)
        res = channel.receive()
        assert res == 18
        channel.send(7)
        res = channel.receive()
        assert res == 42

    def test__rinfo(self, gw: Gateway) -> None:
        rinfo = gw._rinfo()
        assert rinfo.executable
        assert rinfo.cwd
        assert rinfo.version_info
        assert repr(rinfo)
        old = gw.remote_exec(
            """
            import os.path
            cwd = os.getcwd()
            channel.send(os.path.basename(cwd))
            os.chdir('..')
        """
        ).receive()
        try:
            rinfo2 = gw._rinfo()
            assert rinfo2.cwd == rinfo.cwd
            rinfo3 = gw._rinfo(update=True)
            assert rinfo3.cwd != rinfo2.cwd
        finally:
            gw._cache_rinfo = rinfo
            gw.remote_exec("import os ; os.chdir(%r)" % old).waitclose()


class TestPopenGateway:
    gwtype = "popen"

    def test_chdir_separation(
        self, tmp_path: pathlib.Path, makegateway: Callable[[str], Gateway]
    ) -> None:
        with pytest.MonkeyPatch.context() as mp:
            mp.chdir(tmp_path)
            gw = makegateway("popen")
        c = gw.remote_exec("import os ; channel.send(os.getcwd())")
        x = c.receive()
        assert isinstance(x, str)
        assert x.lower() == str(tmp_path).lower()

    def test_remoteerror_readable_traceback(self, gw: Gateway) -> None:
        with pytest.raises(gateway_base.RemoteError) as e:
            gw.remote_exec("x y").waitclose()
        assert "gateway_base" in e.value.formatted

    def test_many_popen(self, makegateway: Callable[[str], Gateway]) -> None:
        num = 4
        l = []
        for i in range(num):
            l.append(makegateway("popen"))
        channels = []
        for gw in l:
            channel = gw.remote_exec("""channel.send(42)""")
            channels.append(channel)
        while channels:
            channel = channels.pop()
            ret = channel.receive()
            assert ret == 42

    def test_rinfo_popen(self, gw: Gateway) -> None:
        rinfo = gw._rinfo()
        assert rinfo.executable == sys.executable
        assert rinfo.cwd == os.getcwd()
        assert rinfo.version_info == sys.version_info

    def test_waitclose_on_remote_killed(
        self, makegateway: Callable[[str], Gateway]
    ) -> None:
        gw = makegateway("popen")
        channel = gw.remote_exec(
            """
            import os
            import time
            channel.send(os.getpid())
            time.sleep(100)
        """
        )
        remotepid = channel.receive()
        assert isinstance(remotepid, int)
        os.kill(remotepid, signal.SIGTERM)
        with pytest.raises(EOFError):
            channel.waitclose(TESTTIMEOUT)
        with pytest.raises(IOError):
            channel.send(None)
        with pytest.raises(EOFError):
            channel.receive()

    def test_receive_on_remote_sysexit(self, gw: Gateway) -> None:
        channel = gw.remote_exec(
            """
            raise SystemExit()
        """
        )
        pytest.raises(channel.RemoteError, channel.receive)

    def test_dont_write_bytecode(self, makegateway: Callable[[str], Gateway]) -> None:
        check_sys_dont_write_bytecode = """
            import sys
            channel.send(sys.dont_write_bytecode)
        """

        gw = makegateway("popen")
        channel = gw.remote_exec(check_sys_dont_write_bytecode)
        ret = channel.receive()
        assert not ret
        gw = makegateway("popen//dont_write_bytecode")
        channel = gw.remote_exec(check_sys_dont_write_bytecode)
        ret = channel.receive()
        assert ret


@pytest.mark.skipif("config.option.broken_isp")
def test_socket_gw_host_not_found(makegateway: Callable[[str], Gateway]) -> None:
    with pytest.raises(execnet.HostNotFound):
        makegateway("socket=qwepoipqwe:9000")


class TestSshPopenGateway:
    gwtype = "ssh"

    def test_sshconfig_config_parsing(
        self, monkeypatch: pytest.MonkeyPatch, makegateway: Callable[[str], Gateway]
    ) -> None:
        l = []
        monkeypatch.setattr(
            gateway_io, "Popen2IOMaster", lambda *args, **kwargs: l.append(args[0])
        )
        with pytest.raises(AttributeError):
            makegateway("ssh=xyz//ssh_config=qwe")

        assert len(l) == 1
        popen_args = l[0]
        i = popen_args.index("-F")
        assert popen_args[i + 1] == "qwe"

    def test_sshaddress(self, gw: Gateway, specssh: execnet.XSpec) -> None:
        assert gw.remoteaddress == specssh.ssh

    def test_host_not_found(
        self, gw: Gateway, makegateway: Callable[[str], Gateway]
    ) -> None:
        with pytest.raises(execnet.HostNotFound):
            makegateway("ssh=nowhere.codespeak.net")


class TestThreads:
    def test_threads(self, makegateway: Callable[[str], Gateway]) -> None:
        gw = makegateway("popen")
        gw.remote_init_threads(3)
        c1 = gw.remote_exec("channel.send(channel.receive())")
        c2 = gw.remote_exec("channel.send(channel.receive())")
        c2.send(1)
        res = c2.receive()
        assert res == 1
        c1.send(42)
        res = c1.receive()
        assert res == 42

    def test_threads_race_sending(self, makegateway: Callable[[str], Gateway]) -> None:
        # multiple threads sending data in parallel
        gw = makegateway("popen")
        num = 5
        gw.remote_init_threads(num)
        print("remote_init_threads(%d)" % num)
        channels = []
        for x in range(num):
            ch = gw.remote_exec(
                """
                for x in range(10):
                    channel.send(''*1000)
                channel.receive()
            """
            )
            channels.append(ch)
        for ch in channels:
            for x in range(10):
                ch.receive(TESTTIMEOUT)
            ch.send(1)
        for ch in channels:
            ch.waitclose(TESTTIMEOUT)

    @flakytest
    def test_status_with_threads(self, makegateway: Callable[[str], Gateway]) -> None:
        gw = makegateway("popen")
        c1 = gw.remote_exec("channel.send(1) ; channel.receive()")
        c2 = gw.remote_exec("channel.send(2) ; channel.receive()")
        c1.receive()
        c2.receive()
        rstatus = gw.remote_status()
        assert rstatus.numexecuting == 2
        c1.send(1)
        c2.send(1)
        c1.waitclose()
        c2.waitclose()
        # there is a slight chance that an execution thread
        # is still active although it's accompanying channel
        # is already closed.
        for i in range(10):
            rstatus = gw.remote_status()
            if rstatus.numexecuting == 0:
                return
        assert 0, "numexecuting didn't drop to zero"


class TestTracing:
    def test_popen_filetracing(
        self,
        tmp_path: pathlib.Path,
        monkeypatch: pytest.MonkeyPatch,
        makegateway: Callable[[str], Gateway],
    ) -> None:
        monkeypatch.setenv("TMP", str(tmp_path))
        monkeypatch.setenv("TEMP", str(tmp_path))  # windows
        monkeypatch.setenv("EXECNET_DEBUG", "1")
        gw = makegateway("popen")
        #  hack out the debuffilename
        fn = gw.remote_exec(
            "import execnet;channel.send(execnet.gateway_base.fn)"
        ).receive()
        assert isinstance(fn, str)
        workerfile = pathlib.Path(fn)
        assert workerfile.exists()
        worker_line = "creating workergateway"
        with workerfile.open() as f:
            for line in f:
                if worker_line in line:
                    break
            else:
                pytest.fail(f"did not find {worker_line!r} in tracefile")
        gw.exit()

    @skip_win_pypy
    @flakytest
    def test_popen_stderr_tracing(
        self,
        capfd: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
        makegateway: Callable[[str], Gateway],
    ) -> None:
        monkeypatch.setenv("EXECNET_DEBUG", "2")
        gw = makegateway("popen")
        pid = gw.remote_exec("import os ; channel.send(os.getpid())").receive()
        _out, err = capfd.readouterr()
        worker_line = "[%s] creating workergateway" % pid
        assert worker_line in err
        gw.exit()

    def test_no_tracing_by_default(self):
        assert gateway_base.trace == gateway_base.notrace, (
            "trace does not to default to empty tracing"
        )


@pytest.mark.parametrize(
    "spec, expected_args",
    [
        ("popen//python=python", ["python"]),
        ("popen//python=sudo -u test python", ["sudo", "-u", "test", "python"]),
        pytest.param(
            r"popen//python=/hans\ alt/bin/python",
            ["/hans alt/bin/python"],
            marks=pytest.mark.skipif(
                sys.platform.startswith("win"), reason="invalid spec on Windows"
            ),
        ),
        ('popen//python="/u/test me/python" -e', ["/u/test me/python", "-e"]),
    ],
)
def test_popen_args(spec: str, expected_args: list[str]) -> None:
    expected_args = [*expected_args, "-u", "-c", gateway_io.popen_bootstrapline]
    args = gateway_io.popen_args(execnet.XSpec(spec))
    assert args == expected_args


@pytest.mark.parametrize(
    "interleave_getstatus",
    [
        pytest.param(True, id="interleave-remote-status"),
        pytest.param(
            False,
            id="no-interleave-remote-status",
            marks=pytest.mark.xfail(
                reason="https://github.com/pytest-dev/execnet/issues/123",
            ),
        ),
    ],
)
def test_regression_gevent_hangs(
    group: execnet.Group, interleave_getstatus: bool
) -> None:
    pytest.importorskip("gevent")
    gw = group.makegateway("popen//execmodel=gevent")

    print(gw.remote_status())

    def sendback(channel) -> None:
        channel.send(1234)

    ch = gw.remote_exec(sendback)
    if interleave_getstatus:
        print(gw.remote_status())
    assert ch.receive(timeout=0.5) == 1234


def test_assert_main_thread_only(
    execmodel: gateway_base.ExecModel, makegateway: Callable[[str], Gateway]
) -> None:
    if execmodel.backend != "main_thread_only":
        pytest.skip("can only run with main_thread_only")

    gw = makegateway(f"execmodel={execmodel.backend}//popen")

    try:
        # Submit multiple remote_exec requests in quick succession and
        # assert that all tasks execute in the main thread. It is
        # necessary to call receive on each channel before the next
        # remote_exec call, since the channel will raise an error if
        # concurrent remote_exec requests are submitted as in
        # test_main_thread_only_concurrent_remote_exec_deadlock.
        for i in range(10):
            ch = gw.remote_exec(
                """
                    import time, threading
                    time.sleep(0.02)
                    channel.send(threading.current_thread() is threading.main_thread())
            """
            )

            try:
                res = ch.receive()
            finally:
                ch.close()
                # This doesn't actually block because we closed
                # the channel already, but it does check for remote
                # errors and raise them.
                ch.waitclose()
            if res is not True:
                pytest.fail("remote raised\n%s" % res)
    finally:
        gw.exit()
        gw.join()


def test_main_thread_only_concurrent_remote_exec_deadlock(
    execmodel: gateway_base.ExecModel, makegateway: Callable[[str], Gateway]
) -> None:
    if execmodel.backend != "main_thread_only":
        pytest.skip("can only run with main_thread_only")

    gw = makegateway(f"execmodel={execmodel.backend}//popen")
    channels = []
    try:
        # Submit multiple remote_exec requests in quick succession and
        # assert that MAIN_THREAD_ONLY_DEADLOCK_TEXT is raised if
        # concurrent remote_exec requests are submitted for the
        # main_thread_only execmodel (as compensation for the lack of
        # back pressure in remote_exec calls which do not attempt to
        # block until the remote main thread is idle).
        for i in range(2):
            channels.append(
                gw.remote_exec(
                    """
                    import threading
                    channel.send(threading.current_thread() is threading.main_thread())
                    # Wait forever, ensuring that the deadlock case triggers.
                    channel.gateway.execmodel.Event().wait()
            """
                )
            )

        expected_results = (
            True,
            execnet.gateway_base.MAIN_THREAD_ONLY_DEADLOCK_TEXT,
        )
        for expected, ch in zip(expected_results, channels):
            try:
                res = ch.receive()
            except execnet.RemoteError as e:
                res = e.formatted
            assert res == expected
    finally:
        for ch in channels:
            ch.close()
        gw.exit()
        gw.join()
