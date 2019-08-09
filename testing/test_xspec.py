# -*- coding: utf-8 -*-
import os
import subprocess
import sys

import execnet
import py
import pytest
from execnet.gateway_io import popen_args
from execnet.gateway_io import ssh_args
from execnet.gateway_io import vagrant_ssh_args

XSpec = execnet.XSpec


skip_win_pypy = pytest.mark.xfail(
    condition=hasattr(sys, "pypy_version_info") and sys.platform.startswith("win"),
    reason="failing on Windows on PyPy (#63)",
)


class TestXSpec:
    def test_norm_attributes(self):
        spec = XSpec(
            r"socket=192.168.102.2:8888//python=c:/this/python2.5" r"//chdir=d:\hello"
        )
        assert spec.socket == "192.168.102.2:8888"
        assert spec.python == "c:/this/python2.5"
        assert spec.chdir == r"d:\hello"
        assert spec.nice is None
        assert not hasattr(spec, "_xyz")

        with pytest.raises(AttributeError):
            spec._hello()

        spec = XSpec("socket=192.168.102.2:8888//python=python2.5//nice=3")
        assert spec.socket == "192.168.102.2:8888"
        assert spec.python == "python2.5"
        assert spec.chdir is None
        assert spec.nice == "3"

        spec = XSpec("ssh=user@host" "//chdir=/hello/this//python=/usr/bin/python2.5")
        assert spec.ssh == "user@host"
        assert spec.python == "/usr/bin/python2.5"
        assert spec.chdir == "/hello/this"

        spec = XSpec("popen")
        assert spec.popen is True

    def test_ssh_options(self):
        spec = XSpec("ssh=-p 22100 user@host//python=python3")
        assert spec.ssh == "-p 22100 user@host"
        assert spec.python == "python3"

        spec = XSpec(
            "ssh=-i ~/.ssh/id_rsa-passwordless_login -p 22100 user@host"
            "//python=python3"
        )
        assert spec.ssh == "-i ~/.ssh/id_rsa-passwordless_login -p 22100 user@host"
        assert spec.python == "python3"

    def test_execmodel(self):
        spec = XSpec("execmodel=thread")
        assert spec.execmodel == "thread"
        spec = XSpec("execmodel=eventlet")
        assert spec.execmodel == "eventlet"

    def test_ssh_options_and_config(self):
        spec = XSpec("ssh=-p 22100 user@host//python=python3")
        spec.ssh_config = "/home/user/ssh_config"
        assert ssh_args(spec)[:6] == ["ssh", "-C", "-F", spec.ssh_config, "-p", "22100"]

    def test_vagrant_options(self):
        spec = XSpec("vagrant_ssh=default//python=python3")
        assert vagrant_ssh_args(spec)[:-1] == ["vagrant", "ssh", "default", "--", "-C"]

    def test_popen_with_sudo_python(self):
        spec = XSpec("popen//python=sudo python3")
        assert popen_args(spec) == [
            "sudo",
            "python3",
            "-u",
            "-c",
            "import sys;exec(eval(sys.stdin.readline()))",
        ]

    def test_env(self):
        xspec = XSpec("popen//env:NAME=value1")
        assert xspec.env["NAME"] == "value1"

    def test__samefilesystem(self):
        assert XSpec("popen")._samefilesystem()
        assert XSpec("popen//python=123")._samefilesystem()
        assert not XSpec("popen//chdir=hello")._samefilesystem()

    def test__spec_spec(self):
        for x in ("popen", "popen//python=this"):
            assert XSpec(x)._spec == x

    def test_samekeyword_twice_raises(self):
        pytest.raises(ValueError, XSpec, "popen//popen")
        pytest.raises(ValueError, XSpec, "popen//popen=123")

    def test_unknown_keys_allowed(self):
        xspec = XSpec("hello=3")
        assert xspec.hello == "3"

    def test_repr_and_string(self):
        for x in ("popen", "popen//python=this"):
            assert repr(XSpec(x)).find("popen") != -1
            assert str(XSpec(x)) == x

    def test_hash_equality(self):
        assert XSpec("popen") == XSpec("popen")
        assert hash(XSpec("popen")) == hash(XSpec("popen"))
        assert XSpec("popen//python=123") != XSpec("popen")
        assert hash(XSpec("socket=hello:8080")) != hash(XSpec("popen"))


class TestMakegateway:
    def test_no_type(self, makegateway):
        pytest.raises(ValueError, lambda: makegateway("hello"))

    @skip_win_pypy
    def test_popen_default(self, makegateway):
        gw = makegateway("")
        assert gw.spec.popen
        assert gw.spec.python is None
        rinfo = gw._rinfo()
        # assert rinfo.executable == sys.executable
        assert rinfo.cwd == os.getcwd()
        assert rinfo.version_info == sys.version_info

    @pytest.mark.skipif("not hasattr(os, 'nice')")
    @pytest.mark.xfail(reason="fails due to timing problems on busy single-core VMs")
    def test_popen_nice(self, makegateway):
        gw = makegateway("popen")

        def getnice(channel):
            import os

            if hasattr(os, "nice"):
                channel.send(os.nice(0))
            else:
                channel.send(None)

        remotenice = gw.remote_exec(getnice).receive()
        gw.exit()
        if remotenice is not None:
            gw = makegateway("popen//nice=5")
            remotenice2 = gw.remote_exec(getnice).receive()
            assert remotenice2 == remotenice + 5

    def test_popen_env(self, makegateway):
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
    def test_popen_explicit(self, makegateway):
        gw = makegateway("popen//python=%s" % sys.executable)
        assert gw.spec.python == sys.executable
        rinfo = gw._rinfo()
        assert rinfo.executable == sys.executable
        assert rinfo.cwd == os.getcwd()
        assert rinfo.version_info == sys.version_info

    @skip_win_pypy
    def test_popen_chdir_absolute(self, testdir, makegateway):
        gw = makegateway("popen//chdir=%s" % testdir.tmpdir)
        rinfo = gw._rinfo()
        assert rinfo.cwd == str(testdir.tmpdir.realpath())

    @skip_win_pypy
    def test_popen_chdir_newsub(self, testdir, makegateway):
        testdir.chdir()
        gw = makegateway("popen//chdir=hello")
        rinfo = gw._rinfo()
        expected = str(testdir.tmpdir.join("hello").realpath()).lower()
        assert rinfo.cwd.lower() == expected

    def test_ssh(self, specssh, makegateway):
        sshhost = specssh.ssh
        gw = makegateway("ssh=%s//id=ssh1" % sshhost)
        rinfo = gw._rinfo()
        assert gw.id == "ssh1"
        gw2 = execnet.SshGateway(sshhost)
        rinfo2 = gw2._rinfo()
        assert rinfo.executable == rinfo2.executable
        assert rinfo.cwd == rinfo2.cwd
        assert rinfo.version_info == rinfo2.version_info

    @pytest.mark.xfail(reason="bad image name", run=False)
    def test_vagrant(self, makegateway, tmpdir, monkeypatch):
        vagrant = py.path.local.sysfind("vagrant")
        if vagrant is None:
            pytest.skip("Vagrant binary not in PATH")
        monkeypatch.chdir(tmpdir)
        subprocess.check_call("vagrant init hashicorp/precise32", shell=True)
        subprocess.check_call("vagrant up --provider virtualbox", shell=True)
        gw = makegateway("vagrant_ssh=default")
        rinfo = gw._rinfo()
        rinfo.cwd == "/home/vagrant"
        rinfo.executable == "/usr/bin/python"
        subprocess.check_call("vagrant halt", shell=True)

    def test_socket(self, specsocket, makegateway):
        gw = makegateway("socket=%s//id=sock1" % specsocket.socket)
        rinfo = gw._rinfo()
        assert rinfo.executable
        assert rinfo.cwd
        assert rinfo.version_info
        assert gw.id == "sock1"
        # we cannot instantiate a second gateway

    @pytest.mark.xfail(reason="we can't instantiate a second gateway")
    def test_socket_second(self, specsocket, makegateway):
        gw = makegateway("socket=%s//id=sock1" % specsocket.socket)
        gw2 = makegateway("socket=%s//id=sock1" % specsocket.socket)
        rinfo = gw._rinfo()
        rinfo2 = gw2._rinfo()
        assert rinfo.executable == rinfo2.executable
        assert rinfo.cwd == rinfo2.cwd
        assert rinfo.version_info == rinfo2.version_info

    def test_socket_installvia(self):
        group = execnet.Group()
        group.makegateway("popen//id=p1")
        gw = group.makegateway("socket//installvia=p1//id=s1")
        assert gw.id == "s1"
        assert gw.remote_status()
        group.terminate()
