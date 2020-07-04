# -*- coding: utf-8 -*-
import subprocess
import sys

import execnet
import py
import pytest
from test_gateway import TESTTIMEOUT

execnetdir = py.path.local(execnet.__file__).dirpath().dirpath()

skip_win_pypy = pytest.mark.xfail(
    condition=hasattr(sys, "pypy_version_info") and sys.platform.startswith("win"),
    reason="failing on Windows on PyPy (#63)",
)


def test_exit_blocked_worker_execution_gateway(anypython, makegateway, pool):
    gateway = makegateway("popen//python=%s" % anypython)
    gateway.remote_exec(
        """
        import time
        time.sleep(10.0)
    """
    )

    def doit():
        gateway.exit()
        return 17

    reply = pool.spawn(doit)
    x = reply.get(timeout=5.0)
    assert x == 17


def test_endmarker_delivery_on_remote_killterm(makegateway, execmodel):
    if execmodel.backend != "thread":
        pytest.xfail("test and execnet not compatible to greenlets yet")
    gw = makegateway("popen")
    q = execmodel.queue.Queue()
    channel = gw.remote_exec(
        source="""
        import os, time
        channel.send(os.getpid())
        time.sleep(100)
    """
    )
    pid = channel.receive()
    py.process.kill(pid)
    channel.setcallback(q.put, endmarker=999)
    val = q.get(TESTTIMEOUT)
    assert val == 999
    err = channel._getremoteerror()
    assert isinstance(err, EOFError)


@skip_win_pypy
def test_termination_on_remote_channel_receive(monkeypatch, makegateway):
    if not py.path.local.sysfind("ps"):
        py.test.skip("need 'ps' command to externally check process status")
    monkeypatch.setenv("EXECNET_DEBUG", "2")
    gw = makegateway("popen")
    pid = gw.remote_exec("import os ; channel.send(os.getpid())").receive()
    gw.remote_exec("channel.receive()")
    gw._group.terminate()
    command = ["ps", "-p", str(pid)]
    popen = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    out, err = popen.communicate()
    out = py.builtin._totext(out, "utf8")
    assert str(pid) not in out, out


def test_close_initiating_remote_no_error(testdir, anypython):
    p = testdir.makepyfile(
        """
        import sys
        sys.path.insert(0, sys.argv[1])
        import execnet
        gw = execnet.makegateway("popen")
        print ("remote_exec1")
        ch1 = gw.remote_exec("channel.receive()")
        print ("remote_exec1")
        ch2 = gw.remote_exec("channel.receive()")
        print ("termination")
        execnet.default_group.terminate()
    """
    )
    popen = subprocess.Popen(
        [str(anypython), str(p), str(execnetdir)], stdout=None, stderr=subprocess.PIPE
    )
    out, err = popen.communicate()
    print(err)
    err = err.decode("utf8")
    lines = [x for x in err.splitlines() if "*sys-package" not in x]
    # print (lines)
    assert not lines


def test_terminate_implicit_does_trykill(testdir, anypython, capfd, pool):
    if pool.execmodel != "thread":
        pytest.xfail("only os threading model supported")
    p = testdir.makepyfile(
        """
        import sys
        sys.path.insert(0, %r)
        import execnet
        group = execnet.Group()
        gw = group.makegateway("popen")
        ch = gw.remote_exec("import time ; channel.send(1) ; time.sleep(100)")
        ch.receive() # remote execution started
        sys.stdout.write("1\\n")
        sys.stdout.flush()
        sys.stdout.close()
        class FlushNoOp(object):
            def flush(self):
                pass
        # replace stdout since some python implementations
        # flush and print errors (for example 3.2)
        # see Issue #5319 (from the release notes of 3.2 Alpha 2)
        sys.stdout = FlushNoOp()

        #  use process at-exit group.terminate call
    """
        % str(execnetdir)
    )
    popen = subprocess.Popen([str(anypython), str(p)], stdout=subprocess.PIPE)
    # sync with start-up
    popen.stdout.readline()
    reply = pool.spawn(popen.communicate)
    reply.get(timeout=50)
    out, err = capfd.readouterr()
    lines = [x for x in err.splitlines() if "*sys-package" not in x]
    assert not lines or "Killed" in err
