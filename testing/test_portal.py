"""The boundary kit: Mailbox and OneShot on the thread wakener."""

from __future__ import annotations

import queue
import threading

import pytest

from execnet.portal import Mailbox
from execnet.portal import OneShot


class TestMailbox:
    def test_put_get_fifo(self) -> None:
        box: Mailbox[int] = Mailbox()
        for n in range(3):
            box.put(n)
        assert [box.get(), box.get(), box.get()] == [0, 1, 2]

    def test_get_nowait_empty(self) -> None:
        box: Mailbox[int] = Mailbox()
        with pytest.raises(queue.Empty):
            box.get_nowait()

    def test_get_timeout(self) -> None:
        box: Mailbox[int] = Mailbox()
        with pytest.raises(TimeoutError):
            box.get(timeout=0.01)

    def test_get_blocks_until_put_from_other_thread(self) -> None:
        box: Mailbox[str] = Mailbox()
        threading.Timer(0.05, box.put, args=["item"]).start()
        assert box.get(timeout=5.0) == "item"

    def test_get_after_stale_wakeup(self) -> None:
        # A consumed notify leaves the wakener set; the drain pattern must
        # still block (and then receive) rather than spin or miss items.
        box: Mailbox[int] = Mailbox()
        box.put(1)
        assert box.get() == 1
        threading.Timer(0.05, box.put, args=[2]).start()
        assert box.get(timeout=5.0) == 2


class TestOneShot:
    def test_set_then_wait(self) -> None:
        shot: OneShot[int] = OneShot()
        shot.set(42)
        assert shot.is_set()
        assert shot.wait() == 42
        # a resolved OneShot stays readable
        assert shot.wait(timeout=0.01) == 42

    def test_wait_timeout(self) -> None:
        shot: OneShot[int] = OneShot()
        with pytest.raises(TimeoutError):
            shot.wait(timeout=0.01)
        assert not shot.is_set()

    def test_set_error_reraises(self) -> None:
        shot: OneShot[None] = OneShot()
        shot.set_error(RuntimeError("boom"))
        with pytest.raises(RuntimeError, match="boom"):
            shot.wait()

    def test_wait_blocks_until_set_from_other_thread(self) -> None:
        shot: OneShot[str] = OneShot()
        threading.Timer(0.05, shot.set, args=["done"]).start()
        assert shot.wait(timeout=5.0) == "done"

    def test_single_resolution_asserted(self) -> None:
        shot: OneShot[int] = OneShot()
        shot.set(1)
        with pytest.raises(AssertionError):
            shot.set(2)
