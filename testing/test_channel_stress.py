"""Hypothesis stress tests for the channel callback (consumer-task) machinery.

These hammer ``setcallback`` -- the loop-task-plus-threadpool consumer -- with
randomised traffic to check the invariants that must hold no matter the timing:

* every item reaches the callback, exactly once and in send order;
* many callback channels run concurrently without cross-talk;
* switching to a callback after some ``receive()`` calls loses nothing;
* the endmarker is always delivered last;
* a callback channel with no user reference is kept alive by its consumer.

Use ``--stress=N`` to raise the number of examples per test (default: a quick
profile registered in ``conftest.pytest_configure``).
"""

from __future__ import annotations

import gc
import weakref

import pytest

hypothesis = pytest.importorskip("hypothesis")
from hypothesis import given  # noqa: E402
from hypothesis import strategies as st  # noqa: E402

from execnet import Gateway  # noqa: E402
from execnet._serialize import SendPayload  # noqa: E402

# High --stress levels replay one test many times; lift the per-test timeout
# well above the default so that only a real hang (bounded by TESTTIMEOUT on
# every blocking call below) fails, not sheer example count.
pytestmark = pytest.mark.timeout(600)

TESTTIMEOUT = 10.0

# Varied payloads: unbounded ints exercise both the short and long serializer
# paths (and their boundaries), on top of the callback machinery itself.
payload_strategy = st.one_of(
    st.integers(),
    st.text(max_size=20),
    st.booleans(),
    st.none(),
)
items_strategy = st.lists(payload_strategy, max_size=40)


def _echo(channel, items):
    """Remote: send every item, in order, then let the channel close."""
    for item in items:
        channel.send(item)


class TestCallbackStress:
    # popen only: fast to spawn and the callback path is transport-independent
    gwtype = "popen"

    @given(data=items_strategy)
    def test_callback_receives_all_in_order(self, gw: Gateway, data: list[int]) -> None:
        collected: list[int] = []
        channel = gw.remote_exec(_echo, items=data)
        channel.setcallback(collected.append)
        channel.waitclose(TESTTIMEOUT)
        assert collected == data

    @given(batches=st.lists(items_strategy, min_size=1, max_size=6))
    def test_many_channels_stay_ordered_and_isolated(
        self, gw: Gateway, batches: list[list[int]]
    ) -> None:
        results: list[list[int]] = [[] for _ in batches]
        channels = []
        for index, items in enumerate(batches):
            channel = gw.remote_exec(_echo, items=items)
            channel.setcallback(results[index].append)
            channels.append(channel)
        for channel in channels:
            channel.waitclose(TESTTIMEOUT)
        assert results == batches

    @given(data=st.data())
    def test_receive_then_switch_loses_nothing(
        self, gw: Gateway, data: st.DataObject
    ) -> None:
        items = data.draw(items_strategy)
        split = data.draw(st.integers(min_value=0, max_value=len(items)))
        channel = gw.remote_exec(_echo, items=items)
        first = [channel.receive(TESTTIMEOUT) for _ in range(split)]
        rest: list[int] = []
        channel.setcallback(rest.append)
        channel.waitclose(TESTTIMEOUT)
        assert first + rest == items

    @given(data=items_strategy)
    def test_endmarker_is_always_last(self, gw: Gateway, data: list[int]) -> None:
        endmarker = object()
        collected: list[object] = []
        channel = gw.remote_exec(_echo, items=data)
        channel.setcallback(collected.append, endmarker=endmarker)
        channel.waitclose(TESTTIMEOUT)
        assert collected == [*data, endmarker]

    @pytest.mark.skipif(
        "not hasattr(sys, 'getrefcount')", reason="needs refcount GC semantics"
    )
    @given(data=st.lists(payload_strategy, min_size=1, max_size=40))
    def test_callback_channel_kept_alive_then_collected(
        self, gw: Gateway, data: list[SendPayload]
    ) -> None:
        collected: list[int] = []
        channel = gw.remote_exec(_echo, items=data)
        channel.setcallback(collected.append)
        ref = weakref.ref(channel)
        del channel  # only the consumer task holds it now

        import time

        deadline = time.time() + TESTTIMEOUT
        while ref() is not None and time.time() < deadline:
            gc.collect()
            time.sleep(0.02)
        assert collected == data
        assert ref() is None  # consumer finished -> reclaimed
