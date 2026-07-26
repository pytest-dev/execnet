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
