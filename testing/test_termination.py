
import sys, os
import execnet
import time
import subprocess
import py
from execnet.threadpool import WorkerPool
queue = py.builtin._tryimport('queue', 'Queue')
from testing.test_gateway import TESTTIMEOUT
execnetdir = py.path.local(execnet.__file__).dirpath().dirpath()

def test_exit_blocked_slave_execution_gateway(anypython):
    group = execnet.Group()
    gateway = group.makegateway('popen//python=%s' % anypython)
    channel = gateway.remote_exec("""
        import time
        time.sleep(10.0)
    """)
    def doit():
        gateway.exit()
        return 17

    pool = WorkerPool()
    reply = pool.dispatch(doit)
    x = reply.get(timeout=5.0)
    assert x == 17

def test_endmarker_delivery_on_remote_killterm():
    gw = execnet.makegateway('popen')
    q = queue.Queue()
    channel = gw.remote_exec(source='''
        import os, time
        channel.send(os.getpid())
        time.sleep(100)
    ''')
    pid = channel.receive()
    py.process.kill(pid)
    channel.setcallback(q.put, endmarker=999)
    val = q.get(TESTTIMEOUT)
    assert val == 999
    err = channel._getremoteerror()
    assert isinstance(err, EOFError)

def test_termination_on_remote_channel_receive(monkeypatch):
    if not py.path.local.sysfind('ps'):
        py.test.skip("need 'ps' command to externally check process status")
    monkeypatch.setenv('EXECNET_DEBUG', '2')
    group = execnet.Group()
    gw = group.makegateway("popen")
    pid = gw.remote_exec("import os ; channel.send(os.getpid())").receive()
    gw.remote_exec("channel.receive()")
    group.terminate()
    command = ["ps", "-p", str(pid)]
    popen = subprocess.Popen(command, stdout=subprocess.PIPE, 
                             stderr=subprocess.STDOUT)
    out, err = popen.communicate()
    out = py.builtin._totext(out, 'utf8')
    assert str(pid) not in out, out

def test_close_initiating_remote_no_error(testdir, anypython):
    p = testdir.makepyfile("""
        import sys
        sys.path.insert(0, %r)
        import execnet
        gw = execnet.makegateway("popen")
        gw.remote_init_threads(num=3)
        ch1 = gw.remote_exec("channel.receive()")
        ch2 = gw.remote_exec("channel.receive()")
        ch3 = gw.remote_exec("channel.receive()")
        execnet.default_group.terminate()
    """ % str(execnetdir))
    popen = subprocess.Popen([str(anypython), str(p)], 
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,)
    stdout, stderr = popen.communicate()
    assert not stderr

def test_double_call_to_terminate(testdir, anypython):
    triggerfile = testdir.tmpdir.join("trigger")
    p = testdir.makepyfile("""
        import sys
        sys.path.insert(0, %r)
        triggerfile = %r
        import execnet
        gw = execnet.makegateway("popen")
        gw.remote_exec('''
            import os, time
            while 1: 
                if os.path.exists(%%r):
                    break
                time.sleep(0.2)
        ''' %% triggerfile)
        ok = 0
        try:
            execnet.default_group.terminate(1.0)
        except IOError:
            try:
                execnet.default_group.terminate(0.1)
            except IOError:
                f = open(triggerfile, 'w')
                f.write("")
                f.close()
                execnet.default_group.terminate(5.0)
                ok = 1
        if not ok:
            sys.stderr.write("no-timeout!\\n")
    """ % (str(execnetdir), str(triggerfile)))
    popen = subprocess.Popen([str(anypython), str(p)], 
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,)
    stdout, stderr = popen.communicate()
    assert not stderr
    
