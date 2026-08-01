from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest
from test_gateway import TESTTIMEOUT

import execnet
from execnet import Gateway
from execnet import XSpec
from execnet import _provision

skip_win_pypy = pytest.mark.xfail(
    condition=hasattr(sys, "pypy_version_info") and sys.platform.startswith("win"),
    reason="failing on Windows on PyPy (#63)",
)


class TestXSpec:
    def test_norm_attributes(self) -> None:
        spec = XSpec(
            r"socket=192.168.102.2:8888//python=c:/this/python3.8//chdir=d:\hello"
        )
        assert spec.socket == "192.168.102.2:8888"
        assert spec.python == "c:/this/python3.8"
        assert spec.chdir == r"d:\hello"
        assert spec.nice is None
        assert not hasattr(spec, "_xyz")

        with pytest.raises(AttributeError):
            spec._hello()  # type: ignore[misc,operator]

        spec = XSpec("socket=192.168.102.2:8888//python=python2.5//nice=3")
        assert spec.socket == "192.168.102.2:8888"
        assert spec.python == "python2.5"
        assert spec.chdir is None
        assert spec.nice == "3"

        spec = XSpec("ssh=user@host//chdir=/hello/this//python=/usr/bin/python2.5")
        assert spec.ssh == "user@host"
        assert spec.python == "/usr/bin/python2.5"
        assert spec.chdir == "/hello/this"

        spec = XSpec("popen")
        assert spec.popen is True

    def test_ssh_options(self) -> None:
        spec = XSpec("ssh=-p 22100 user@host//python=python3")
        assert spec.ssh == "-p 22100 user@host"
        assert spec.python == "python3"

        spec = XSpec(
            "ssh=-i ~/.ssh/id_rsa-passwordless_login -p 22100 user@host//python=python3"
        )
        assert spec.ssh == "-i ~/.ssh/id_rsa-passwordless_login -p 22100 user@host"
        assert spec.python == "python3"

    def test_execmodel(self) -> None:
        spec = XSpec("execmodel=thread")
        assert spec.execmodel == "thread"
        spec = XSpec("execmodel=main_thread_only")
        assert spec.execmodel == "main_thread_only"

    def test_ssh_options_and_config(self) -> None:
        spec = XSpec("ssh=-p 22100 user@host//python=python3")
        args = _provision.ssh_argv("-p 22100 user@host", "/home/user/ssh_config", "cmd")
        assert args[:6] == ["ssh", "-C", "-F", "/home/user/ssh_config", "-p", "22100"]
        assert spec.ssh is not None

    def test_vagrant_options(self) -> None:
        args = _provision.vagrant_ssh_argv("default", None, "cmd")
        assert args[:-1] == ["vagrant", "ssh", "default", "--", "-C"]

    def test_popen_with_sudo_python(self) -> None:
        from execnet import _trio_gateway

        spec = XSpec("popen//python=sudo python3//id=gw0")
        args = _trio_gateway.popen_module_args(spec)
        assert args[:6] == ["sudo", "python3", "-u", "-m", "execnet", "worker"]

    def test_env(self) -> None:
        xspec = XSpec("popen//env:NAME=value1")
        assert xspec.env["NAME"] == "value1"

    def test__samefilesystem(self) -> None:
        assert XSpec("popen")._samefilesystem()
        assert XSpec("popen//python=123")._samefilesystem()
        assert not XSpec("popen//chdir=hello")._samefilesystem()

    def test__spec_spec(self) -> None:
        for x in ("popen", "popen//python=this"):
            assert XSpec(x)._spec == x

    def test_samekeyword_twice_raises(self) -> None:
        pytest.raises(ValueError, XSpec, "popen//popen")
        pytest.raises(ValueError, XSpec, "popen//popen=123")

    def test_unknown_keys_allowed(self) -> None:
        xspec = XSpec("hello=3")
        assert xspec.hello == "3"

    def test_repr_and_string(self) -> None:
        for x in ("popen", "popen//python=this"):
            assert repr(XSpec(x)).find("popen") != -1
            assert str(XSpec(x)) == x

    def test_hash_equality(self) -> None:
        assert XSpec("popen") == XSpec("popen")
        assert hash(XSpec("popen")) == hash(XSpec("popen"))
        assert XSpec("popen//python=123") != XSpec("popen")
        assert hash(XSpec("socket=hello:8080")) != hash(XSpec("popen"))


class TestMakegateway:
    def test_no_type(self, makegateway: Callable[[str], Gateway]) -> None:
        pytest.raises(ValueError, lambda: makegateway("hello"))

    def test_wait_backend_comes_from_the_facade(
        self, makegateway: Callable[[str], Gateway]
    ) -> None:
        # not a spec key: the blocking surface decides how *it* parks, and
        # a thread-shaped worker profile parks on threads either way
        gw = makegateway("popen")
        assert gw._wait_backend == "thread"
        channel = gw.remote_exec("channel.send(channel.gateway._wait_backend)")
        assert channel.receive() == "thread"

    @skip_win_pypy
    def test_popen_default(self, makegateway: Callable[[str], Gateway]) -> None:
        gw = makegateway("")
        assert gw.spec.popen
        assert gw.spec.python is None
        rinfo = gw._rinfo()
        # assert rinfo.executable == sys.executable
        assert rinfo.cwd == os.getcwd()
        assert rinfo.version_info == sys.version_info

    @pytest.mark.skipif("not hasattr(os, 'nice')")
    @pytest.mark.xfail(reason="fails due to timing problems on busy single-core VMs")
    def test_popen_nice(self, makegateway: Callable[[str], Gateway]) -> None:
        gw = makegateway("popen")

        def getnice(channel) -> None:
            import os

            if hasattr(os, "nice"):
                channel.send(os.nice(0))
            else:
                channel.send(None)

        remotenice = gw.remote_exec(getnice).receive()
        assert isinstance(remotenice, int)
        gw.exit()
        if remotenice is not None:
            gw = makegateway("popen//nice=5")
            remotenice2 = gw.remote_exec(getnice).receive()
            assert remotenice2 == remotenice + 5

    def test_popen_env(self, makegateway: Callable[[str], Gateway]) -> None:
        gw = makegateway("popen//env:NAME123=123")
        ch = gw.remote_exec(
            """
            import os
            channel.send(os.environ['NAME123'])
        """
        )
        value = ch.receive()
        assert value == "123"

    @skip_win_pypy
    def test_popen_explicit(self, makegateway: Callable[[str], Gateway]) -> None:
        gw = makegateway("popen//python=%s" % sys.executable)
        assert gw.spec.python == sys.executable
        rinfo = gw._rinfo()
        assert rinfo.executable == sys.executable
        assert rinfo.cwd == os.getcwd()
        assert rinfo.version_info == sys.version_info

    @skip_win_pypy
    def test_popen_chdir_absolute(
        self, tmp_path: Path, makegateway: Callable[[str], Gateway]
    ) -> None:
        gw = makegateway("popen//chdir=%s" % tmp_path)
        rinfo = gw._rinfo()
        assert rinfo.cwd == str(tmp_path.resolve())

    @skip_win_pypy
    def test_popen_chdir_newsub(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        makegateway: Callable[[str], Gateway],
    ) -> None:
        monkeypatch.chdir(tmp_path)
        gw = makegateway("popen//chdir=hello")
        rinfo = gw._rinfo()
        expected = str(tmp_path.joinpath("hello").resolve()).lower()
        assert rinfo.cwd.lower() == expected

    def test_ssh(self, specssh: XSpec, makegateway: Callable[[str], Gateway]) -> None:
        sshhost = specssh.ssh
        gw = makegateway("ssh=%s//id=ssh1" % sshhost)
        assert gw.id == "ssh1"

    def test_vagrant(self, makegateway: Callable[[str], Gateway]) -> None:
        vagrant_bin = shutil.which("vagrant")
        if vagrant_bin is None:
            pytest.skip("Vagrant binary not in PATH")
        res = subprocess.run(
            [vagrant_bin, "status", "default", "--machine-readable"],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            check=True,
        ).stdout
        print(res)
        if ",default,state,shutoff\n" in res:
            pytest.xfail("vm shutoff, run `vagrant up` first")
        if ",default,state,not_created\n" in res:
            pytest.xfail("vm not created, run `vagrant up` first")
        if ",default,state,running\n" not in res:
            pytest.fail("unknown vm state")

        gw = makegateway("vagrant_ssh=default//python=python3")
        rinfo = gw._rinfo()
        assert rinfo.cwd == "/home/vagrant"
        assert rinfo.executable == "/usr/bin/python"

    def test_socket(
        self, specsocket: XSpec, makegateway: Callable[[str], Gateway]
    ) -> None:
        gw = makegateway("socket=%s//id=sock1" % specsocket.socket)
        rinfo = gw._rinfo()
        assert rinfo.executable
        assert rinfo.cwd
        assert rinfo.version_info
        assert gw.id == "sock1"
        # we cannot instantiate a second gateway

    @pytest.mark.xfail(reason="we can't instantiate a second gateway")
    def test_socket_second(
        self, specsocket: XSpec, makegateway: Callable[[str], Gateway]
    ) -> None:
        gw = makegateway("socket=%s//id=sock1" % specsocket.socket)
        gw2 = makegateway("socket=%s//id=sock1" % specsocket.socket)
        rinfo = gw._rinfo()
        rinfo2 = gw2._rinfo()
        assert rinfo.executable == rinfo2.executable
        assert rinfo.cwd == rinfo2.cwd
        assert rinfo.version_info == rinfo2.version_info

    @pytest.mark.skipif(
        not _provision.socket_handoff_available(),
        reason="the server must hand the accepted socket to a worker process",
    )
    def test_socket_installvia(self) -> None:
        group = execnet.Group()
        group.makegateway("popen//id=p1")
        gw = group.makegateway("socket//installvia=p1//id=s1")
        assert gw.id == "s1"
        assert gw.remote_status()
        group.terminate()

    @pytest.mark.skipif(
        not _provision.socket_handoff_available(),
        reason="the server must hand the accepted socket to a worker process",
    )
    def test_socket_worker_gets_the_spec(self, tmp_path: Path) -> None:
        """A ``socket=`` worker is configured by its spec, like any other.

        The server spawns it, so its config cannot ride in argv -- it
        travels over the connection instead.  Without that these keys were
        accepted, validated, and then silently dropped.
        """
        group = execnet.Group()
        try:
            group.makegateway("popen//id=p1")
            gw = group.makegateway(
                f"socket//installvia=p1//id=s1//chdir={tmp_path}//env:SPECVAR=here"
            )
            channel = gw.remote_exec(
                "import os; channel.send((os.getcwd(), os.environ.get('SPECVAR')))"
            )
            cwd, var = channel.receive(TESTTIMEOUT)
            assert Path(cwd).resolve() == tmp_path.resolve()
            assert var == "here"
        finally:
            group.terminate(timeout=10)

    @pytest.mark.skipif(
        not _provision.socket_handoff_available(),
        reason="the server must hand the accepted socket to a worker process",
    )
    def test_socket_worker_honours_the_profile(self) -> None:
        group = execnet.Group()
        try:
            group.makegateway("popen//id=p1")
            gw = group.makegateway("socket//installvia=p1//id=s1//profile=trio")
            channel = gw.remote_exec("await channel.send('async')")
            assert channel.receive(TESTTIMEOUT) == "async"
        finally:
            group.terminate(timeout=10)
