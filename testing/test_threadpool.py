from __future__ import with_statement
import pytest
import py
import sys
from execnet.threadpool import WorkerPool

def test_simple(pool):
    reply = pool.spawn(lambda: 42)
    assert reply.get() == 42

def test_some(pool, execmodel):
    q = execmodel.Queue()
    num = 4

    def f(i):
        q.put(i)
        while q.qsize():
            execmodel.sleep(0.01)
    for i in range(num):
        pool.spawn(f, i)
    for i in range(num):
        q.get()
    #assert len(pool._running) == 4
    pool.waitall(timeout=1.0)
    #execmodel.time.sleep(1)  helps on windows?
    assert len(pool._running) == 0

def test_limited_size(execmodel):
    pool = WorkerPool(execmodel, size=1)
    q = execmodel.Queue()
    q2 = execmodel.Queue()
    q3 = execmodel.Queue()
    def first():
        q.put(1)
        q2.get()
    pool.spawn(first)
    assert q.get() == 1
    def second():
        q3.put(3)
    # we spawn a second pool to spawn the second function
    # which should block
    pool2 = WorkerPool(execmodel)
    pool2.spawn(pool.spawn, second)
    pytest.raises(IOError, lambda: pool2.waitall(1.0))
    assert q3.qsize() == 0
    q2.put(2)
    pool2.waitall()
    pool.waitall()

def test_get(pool):
    def f():
        return 42
    reply = pool.spawn(f)
    result = reply.get()
    assert result == 42

def test_get_timeout(execmodel, pool):
    def f():
        execmodel.sleep(0.2)
        return 42
    reply = pool.spawn(f)
    with pytest.raises(IOError):
        reply.get(timeout=0.01)

def test_get_excinfo(pool):
    def f():
        raise ValueError("42")
    reply = pool.spawn(f)
    with py.test.raises(ValueError):
        reply.get(1.0)
    with pytest.raises(ValueError):
        reply.get(1.0)

def test_waitall_timeout(pool, execmodel):
    q = execmodel.Queue()
    def f():
        q.get()
    reply = pool.spawn(f)
    pytest.raises(IOError, pool.waitall, 0.01)
    q.put(None)
    reply.get(timeout=1.0)
    pool.waitall(timeout=0.1)

@py.test.mark.skipif("not hasattr(os, 'dup')")
def test_pool_clean_shutdown(pool):
    capture = py.io.StdCaptureFD()
    def f():
        pass
    pool.spawn(f)
    pool.spawn(f)
    pool.waitall(timeout=1.0)
    assert not pool._running
    out, err = capture.reset()
    print(out)
    sys.stderr.write(err + "\n")
    assert err == ''
