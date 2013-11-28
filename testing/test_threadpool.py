from __future__ import with_statement
import pytest
import py
import sys
from execnet.threadpool import queue, WorkerPool

def test_some():
    pool = WorkerPool()
    q = queue.Queue()
    num = 4

    def f(i):
        q.put(i)
        while q.qsize():
            py.std.time.sleep(0.01)
    for i in range(num):
        pool.spawn(f, i)
    for i in range(num):
        q.get()
    assert len(pool._running) == 4
    pool.shutdown()
    pool.waitall(timeout=1.0)
    #py.std.time.sleep(1)  helps on windows?
    assert len(pool._running) == 0
    assert len(pool._running) == 0

def test_get():
    pool = WorkerPool()
    def f():
        return 42
    reply = pool.spawn(f)
    result = reply.get()
    assert result == 42

def test_get_timeout():
    pool = WorkerPool()
    def f():
        py.std.time.sleep(0.2)
        return 42
    reply = pool.spawn(f)
    with py.test.raises(IOError):
        reply.get(timeout=0.01)

def test_get_excinfo():
    pool = WorkerPool()
    def f():
        raise ValueError("42")
    reply = pool.spawn(f)
    with py.test.raises(ValueError):
        reply.get(1.0)
    with pytest.raises(EOFError):
        reply.get(1.0)

def test_maxthreads():
    pool = WorkerPool(maxthreads=1)
    def f():
        py.std.time.sleep(0.5)
    try:
        pool.spawn(f)
        py.test.raises(IOError, pool.spawn, f)
    finally:
        pool.shutdown()

def test_waitall_timeout():
    pool = WorkerPool()
    q = queue.Queue()
    def f():
        q.get()
    reply = pool.spawn(f)
    pool.shutdown()
    py.test.raises(IOError, pool.waitall, 0.01)
    q.put(None)
    reply.get(timeout=1.0)
    pool.waitall(timeout=0.1)

@py.test.mark.skipif("not hasattr(os, 'dup')")
def test_pool_clean_shutdown():
    capture = py.io.StdCaptureFD()
    pool = WorkerPool()
    def f():
        pass
    pool.spawn(f)
    pool.spawn(f)
    pool.shutdown()
    pool.waitall(timeout=1.0)
    assert not pool._running
    assert not pool._ready
    out, err = capture.reset()
    print(out)
    sys.stderr.write(err + "\n")
    assert err == ''
