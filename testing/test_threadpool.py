# -*- coding: utf-8 -*-
from __future__ import with_statement

import os
import sys

import py
import pytest
from execnet.gateway_base import WorkerPool


def test_execmodel(execmodel, tmpdir):
    assert execmodel.backend
    p = tmpdir.join("somefile")
    p.write("content")
    fd = os.open(str(p), os.O_RDONLY)
    f = execmodel.fdopen(fd, "r")
    assert f.read() == "content"
    f.close()


def test_execmodel_basic_attrs(execmodel):
    m = execmodel
    assert callable(m.start)
    assert m.get_ident()


def test_simple(pool):
    reply = pool.spawn(lambda: 42)
    assert reply.get() == 42


def test_some(pool, execmodel):
    q = execmodel.queue.Queue()
    num = 4

    def f(i):
        q.put(i)
        while q.qsize():
            execmodel.sleep(0.01)

    for i in range(num):
        pool.spawn(f, i)
    for i in range(num):
        q.get()
    # assert len(pool._running) == 4
    assert pool.waitall(timeout=1.0)
    # execmodel.sleep(1)  helps on windows?
    assert len(pool._running) == 0


def test_running_semnatics(pool, execmodel):
    q = execmodel.queue.Queue()

    def first():
        q.get()

    reply = pool.spawn(first)
    assert reply.running
    assert pool.active_count() == 1
    q.put(1)
    assert pool.waitall()
    assert pool.active_count() == 0
    assert not reply.running


def test_waitfinish_on_reply(pool):
    l = []
    reply = pool.spawn(lambda: l.append(1))
    reply.waitfinish()
    assert l == [1]
    reply = pool.spawn(lambda: 0 / 0)
    reply.waitfinish()  # no exception raised
    pytest.raises(ZeroDivisionError, reply.get)


@pytest.mark.xfail(reason="WorkerPool does not implement limited size")
def test_limited_size(execmodel):
    pool = WorkerPool(execmodel, size=1)
    q = execmodel.queue.Queue()
    q2 = execmodel.queue.Queue()
    q3 = execmodel.queue.Queue()

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
    assert not pool2.waitall(1.0)
    assert q3.qsize() == 0
    q2.put(2)
    assert pool2.waitall()
    assert pool.waitall()


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
    q = execmodel.queue.Queue()

    def f():
        q.get()

    reply = pool.spawn(f)
    assert not pool.waitall(0.01)
    q.put(None)
    reply.get(timeout=1.0)
    assert pool.waitall(timeout=0.1)


@py.test.mark.skipif("not hasattr(os, 'dup')")
def test_pool_clean_shutdown(pool):
    capture = py.io.StdCaptureFD()
    q = pool.execmodel.queue.Queue()

    def f():
        q.get()

    pool.spawn(f)
    assert not pool.waitall(timeout=1.0)
    pool.trigger_shutdown()
    with pytest.raises(ValueError):
        pool.spawn(f)

    def wait_then_put():
        pool.execmodel.sleep(0.1)
        q.put(1)

    pool.execmodel.start(wait_then_put)
    assert pool.waitall()
    out, err = capture.reset()
    sys.stdout.write(out + "\n")
    sys.stderr.write(err + "\n")
    assert err == ""


def test_primary_thread_integration(execmodel):
    if execmodel.backend != "thread":
        with pytest.raises(ValueError):
            WorkerPool(execmodel=execmodel, hasprimary=True)
        return
    pool = WorkerPool(execmodel=execmodel, hasprimary=True)
    queue = execmodel.queue.Queue()

    def do_integrate():
        queue.put(execmodel.get_ident())
        pool.integrate_as_primary_thread()

    execmodel.start(do_integrate)

    def func():
        queue.put(execmodel.get_ident())

    pool.spawn(func)
    ident1 = queue.get()
    ident2 = queue.get()
    assert ident1 == ident2
    pool.terminate()


def test_primary_thread_integration_shutdown(execmodel):
    if execmodel.backend != "thread":
        pytest.skip("can only run with threading")
    pool = WorkerPool(execmodel=execmodel, hasprimary=True)
    queue = execmodel.queue.Queue()

    def do_integrate():
        queue.put(execmodel.get_ident())
        pool.integrate_as_primary_thread()

    execmodel.start(do_integrate)
    queue.get()

    queue2 = execmodel.queue.Queue()

    def get_two():
        queue.put(execmodel.get_ident())
        queue2.get()

    reply = pool.spawn(get_two)
    # make sure get_two is running and blocked on queue2
    queue.get()
    # then shut down
    pool.trigger_shutdown()
    # and let get_two finish
    queue2.put(1)
    reply.get()
    assert pool.waitall(5.0)
