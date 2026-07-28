"""The gevent wait backend (wait=gevent): greenlet-parking blocking waits.

Opt-in: requires the ``gevent`` dependency group (``uv sync --group
gevent``); skipped when gevent is not installed.  No monkey-patching is
needed -- the wakener parks the waiting greenlet while the trio host
thread keeps running the protocol.
"""

from __future__ import annotations

import threading

import pytest

gevent = pytest.importorskip("gevent")

import execnet  # noqa: E402
from execnet._boundary import Flag  # noqa: E402
from execnet._boundary import Mailbox  # noqa: E402
from execnet._boundary import make_wakener  # noqa: E402

TESTTIMEOUT = 10.0


@pytest.fixture
def gevent_gw():
    group = execnet.Group()
    try:
        yield group.makegateway("popen//wait=gevent")
    finally:
        group.terminate(timeout=5.0)


class TestGeventWakener:
    def test_mailbox_wakes_greenlet_from_foreign_thread(self) -> None:
        box: Mailbox[str] = Mailbox(make_wakener("gevent"))
        threading.Timer(0.05, box.put, args=["item"]).start()
        result = gevent.spawn(box.get, 5.0)
        assert result.get(timeout=TESTTIMEOUT) == "item"

    def test_notify_before_first_wait_is_not_lost(self) -> None:
        flag = Flag(make_wakener("gevent"))
        flag.set()
        assert flag.wait(timeout=1.0)

    def test_wait_parks_greenlet_not_hub(self) -> None:
        box: Mailbox[str] = Mailbox(make_wakener("gevent"))
        progressed: list[int] = []

        def other() -> None:
            for i in range(5):
                progressed.append(i)
                gevent.sleep(0.01)
            box.put("done")

        waiter = gevent.spawn(box.get, 5.0)
        gevent.spawn(other)
        # if get() blocked the hub, other() could never run and put()
        assert waiter.get(timeout=TESTTIMEOUT) == "done"
        assert progressed == [0, 1, 2, 3, 4]


class TestGeventGateway:
    def test_receive_parks_greenlet_not_hub(self, gevent_gw: execnet.Gateway) -> None:
        channel = gevent_gw.remote_exec("channel.send(channel.receive())")
        progressed: list[int] = []

        def other() -> None:
            for i in range(5):
                progressed.append(i)
                gevent.sleep(0.01)
            # sending from a greenlet blocks-until-written on the
            # gevent wakener, parking only this greenlet
            channel.send("hello")

        waiter = gevent.spawn(channel.receive, TESTTIMEOUT)
        gevent.spawn(other)
        # if receive() blocked the hub, other() could never send and
        # the remote echo could never arrive -> this would hang
        assert waiter.get(timeout=TESTTIMEOUT) == "hello"
        assert progressed == [0, 1, 2, 3, 4]

    def test_waitclose_and_endmarker(self, gevent_gw: execnet.Gateway) -> None:
        channel = gevent_gw.remote_exec("channel.send(1)")
        assert gevent.spawn(channel.receive, TESTTIMEOUT).get(timeout=TESTTIMEOUT) == 1
        gevent.spawn(channel.waitclose, TESTTIMEOUT).get(timeout=TESTTIMEOUT)

    def test_makegateway_parks_greenlet_not_hub(self) -> None:
        # management ops (makegateway/terminate) from a greenlet must not
        # stall the hub: they wait on a OneShot with a gevent wakener.
        group = execnet.Group()
        progressed: list[int] = []

        def other() -> None:
            for i in range(5):
                progressed.append(i)
                gevent.sleep(0.01)

        try:
            ticker = gevent.spawn(other)
            maker = gevent.spawn(group.makegateway, "popen//wait=gevent")
            gw = maker.get(timeout=TESTTIMEOUT)
            channel = gw.remote_exec("channel.send(42)")
            assert gevent.spawn(channel.receive, TESTTIMEOUT).get(TESTTIMEOUT) == 42
            ticker.get(timeout=TESTTIMEOUT)
            assert progressed == [0, 1, 2, 3, 4]
        finally:
            gevent.spawn(group.terminate, 5.0).get(timeout=TESTTIMEOUT)


class TestGeventWorkerProfile:
    """execmodel=gevent: exec'd code runs as greenlets on the main-thread hub."""

    @pytest.fixture
    def worker_gw(self):
        group = execnet.Group()
        try:
            yield group.makegateway("popen//execmodel=gevent")
        finally:
            group.terminate(timeout=5.0)

    def test_execs_are_greenlets_on_main_thread(self, worker_gw) -> None:
        report = """
            import threading
            channel.send(threading.current_thread() is threading.main_thread())
            channel.receive()
        """
        first = worker_gw.remote_exec(report)
        second = worker_gw.remote_exec(report)
        # both run concurrently on the one main thread: greenlets
        assert first.receive(TESTTIMEOUT) is True
        assert second.receive(TESTTIMEOUT) is True
        first.send(None)
        second.send(None)
        first.waitclose(TESTTIMEOUT)
        second.waitclose(TESTTIMEOUT)

    def test_execs_cooperate_via_gevent(self, worker_gw) -> None:
        # the first exec parks in channel.receive() (gevent wakener) while
        # the second completes -- with a blocked hub this would deadlock.
        blocked = worker_gw.remote_exec("channel.send(channel.receive())")
        side = worker_gw.remote_exec(
            """
            import gevent
            gevent.sleep(0.01)
            channel.send('side')
            """
        )
        assert side.receive(TESTTIMEOUT) == "side"
        blocked.send("go")
        assert blocked.receive(TESTTIMEOUT) == "go"

    def test_status_reports_gevent(self, worker_gw) -> None:
        assert worker_gw.remote_status().execmodel == "gevent"


def test_provisioning_adds_gevent_requirement() -> None:
    from execnet import XSpec
    from execnet._provision import _extra_with_tokens
    from execnet._provision import worker_cli_arg

    spec = XSpec("popen//id=g1//execmodel=gevent")
    config = worker_cli_arg(spec)
    assert '"wait": "gevent"' in config
    assert _extra_with_tokens(config) == ["--with", "gevent"]
    plain = worker_cli_arg(XSpec("popen//id=g2//execmodel=thread"))
    assert _extra_with_tokens(plain) == []
