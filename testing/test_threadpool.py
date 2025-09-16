import os
from pathlib import Path

import pytest

from execnet.gateway_base import ExecModel
from execnet.gateway_base import WorkerPool


def test_execmodel(execmodel: ExecModel, tmp_path: Path) -> None:
    assert execmodel.backend
    p = tmp_path / "somefile"
    p.write_text("content")
    fd = os.open(p, os.O_RDONLY)
    f = execmodel.fdopen(fd, "r")
    assert f.read() == "content"
    f.close()


def test_execmodel_basic_attrs(execmodel: ExecModel) -> None:
    m = execmodel
    assert callable(m.start)
    assert m.get_ident()


def test_simple(pool: WorkerPool) -> None:
    reply = pool.spawn(lambda: 42)
    assert reply.get() == 42


def test_some(pool: WorkerPool, execmodel: ExecModel) -> None:
    q = execmodel.queue.Queue()
    num = 4

    def f(i: int) -> None:
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


def test_running_semnatics(pool: WorkerPool, execmodel: ExecModel) -> None:
    q = execmodel.queue.Queue()

    def first() -> None:
        q.get()

    reply = pool.spawn(first)
    assert reply.running
    assert pool.active_count() == 1
    q.put(1)
    assert pool.waitall()
    assert pool.active_count() == 0
    assert not reply.running


def test_waitfinish_on_reply(pool: WorkerPool) -> None:
    l = []
    reply = pool.spawn(lambda: l.append(1))
    reply.waitfinish()
    assert l == [1]
    reply = pool.spawn(lambda: 0 / 0)
    reply.waitfinish()  # no exception raised
    pytest.raises(ZeroDivisionError, reply.get)


@pytest.mark.xfail(reason="WorkerPool does not implement limited size")
def test_limited_size(execmodel: ExecModel) -> None:
    pool = WorkerPool(execmodel, size=1)  # type: ignore[call-arg]
    q = execmodel.queue.Queue()
    q2 = execmodel.queue.Queue()
    q3 = execmodel.queue.Queue()

    def first() -> None:
        q.put(1)
        q2.get()

    pool.spawn(first)
    assert q.get() == 1

    def second() -> None:
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


def test_get(pool: WorkerPool) -> None:
    def f() -> int:
        return 42

    reply = pool.spawn(f)
    result = reply.get()
    assert result == 42


def test_get_timeout(execmodel: ExecModel, pool: WorkerPool) -> None:
    def f() -> int:
        execmodel.sleep(0.2)
        return 42

    reply = pool.spawn(f)
    with pytest.raises(IOError):
        reply.get(timeout=0.01)


def test_get_excinfo(pool: WorkerPool) -> None:
    def f() -> None:
        raise ValueError("42")

    reply = pool.spawn(f)
    with pytest.raises(ValueError):
        reply.get(1.0)
    with pytest.raises(ValueError):
        reply.get(1.0)


def test_waitall_timeout(pool: WorkerPool, execmodel: ExecModel) -> None:
    q = execmodel.queue.Queue()

    def f() -> None:
        q.get()

    reply = pool.spawn(f)
    assert not pool.waitall(0.01)
    q.put(None)
    reply.get(timeout=1.0)
    assert pool.waitall(timeout=0.1)


@pytest.mark.skipif(not hasattr(os, "dup"), reason="no os.dup")
def test_pool_clean_shutdown(
    pool: WorkerPool, capfd: pytest.CaptureFixture[str]
) -> None:
    q = pool.execmodel.queue.Queue()

    def f() -> None:
        q.get()

    pool.spawn(f)
    assert not pool.waitall(timeout=1.0)
    pool.trigger_shutdown()
    with pytest.raises(ValueError):
        pool.spawn(f)

    def wait_then_put() -> None:
        pool.execmodel.sleep(0.1)
        q.put(1)

    pool.execmodel.start(wait_then_put)
    assert pool.waitall()
    _out, err = capfd.readouterr()
    assert err == ""


def test_primary_thread_integration(execmodel: ExecModel) -> None:
    if execmodel.backend not in ("thread", "main_thread_only"):
        with pytest.raises(ValueError):
            WorkerPool(execmodel=execmodel, hasprimary=True)
        return
    pool = WorkerPool(execmodel=execmodel, hasprimary=True)
    queue = execmodel.queue.Queue()

    def do_integrate() -> None:
        queue.put(execmodel.get_ident())
        pool.integrate_as_primary_thread()

    execmodel.start(do_integrate)

    def func() -> None:
        queue.put(execmodel.get_ident())

    pool.spawn(func)
    ident1 = queue.get()
    ident2 = queue.get()
    assert ident1 == ident2
    pool.terminate()


def test_primary_thread_integration_shutdown(execmodel: ExecModel) -> None:
    if execmodel.backend not in ("thread", "main_thread_only"):
        pytest.skip("can only run with threading")
    pool = WorkerPool(execmodel=execmodel, hasprimary=True)
    queue = execmodel.queue.Queue()

    def do_integrate() -> None:
        queue.put(execmodel.get_ident())
        pool.integrate_as_primary_thread()

    execmodel.start(do_integrate)
    queue.get()

    queue2 = execmodel.queue.Queue()

    def get_two() -> None:
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
