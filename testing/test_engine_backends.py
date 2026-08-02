"""The engine contract, run against every backend that claims to meet it.

``ProtocolEngine`` can be handed a loop written in either async library.
The core still only runs on trio, so what is pinned here is the *engine's*
own promise -- start, a portal into the loop, one door to the root task
scope, a registry of what is running, and a stop that joins -- plus the
refusal that keeps an unported core from failing somewhere confusing.

Every test that is not explicitly about one backend is parametrized over
both, so the second implementation cannot drift from the first quietly.
"""

from __future__ import annotations

import sys
import threading
import warnings
from typing import Any

import pytest

import execnet
from execnet._asyncio_engine import MIN_PYTHON
from execnet._asyncio_engine import AsyncioEngine
from execnet._engine import BACKENDS
from execnet._engine import ProtocolEngine
from execnet._errors import ForkedResourceError
from execnet._errors import LoopFinishedError
from execnet._trio_engine import TrioEngine

TESTTIMEOUT = 30.0

#: backends this interpreter can actually run
USABLE = [
    name
    for name in sorted(BACKENDS)
    if name != "asyncio" or sys.version_info >= MIN_PYTHON
]


@pytest.fixture(params=USABLE)
def engine(request: pytest.FixtureRequest) -> Any:
    """A started engine per backend, closed afterwards."""
    made = ProtocolEngine(name=f"execnet-engine-{request.param}", backend=request.param)
    try:
        yield made.start()
    finally:
        made.close(timeout=10.0)


class TestTheContract:
    def test_it_runs_a_coroutine_and_returns_the_value(self, engine: Any) -> None:
        async def double(value: int) -> int:
            return value * 2

        assert engine._ensure_started().call(double, 21) == 42

    def test_an_exception_comes_back_to_the_caller(self, engine: Any) -> None:
        async def boom() -> None:
            raise ValueError("from the loop")

        with pytest.raises(ValueError, match="from the loop"):
            engine._ensure_started().call(boom)

    def test_call_sync_runs_on_the_loop_thread(self, engine: Any) -> None:
        loop = engine._ensure_started()
        assert loop.call_sync(loop._on_engine_thread) is True
        assert loop._on_engine_thread() is False

    def test_the_thread_is_named_and_goes_away(self, engine: Any) -> None:
        name = engine.name
        assert any(t.name == name for t in threading.enumerate())
        engine.close(timeout=10.0)
        assert not engine.running
        assert not any(t.name == name for t in threading.enumerate())

    def test_post_is_fire_and_forget_and_ordered(self, engine: Any) -> None:
        loop = engine._ensure_started()
        seen: list[int] = []
        for index in range(10):
            loop.portal.post(seen.append, index)
        # a round trip flushes everything posted before it
        loop.call_sync(lambda: None)
        assert seen == list(range(10))

    def test_posting_to_a_stopped_loop_is_refused(self, engine: Any) -> None:
        loop = engine._ensure_started()
        engine.close(timeout=10.0)
        with pytest.raises(LoopFinishedError):
            loop.portal.post(lambda: None)

    def test_start_task_waits_until_the_task_says_it_is_ready(
        self, engine: Any
    ) -> None:
        loop = engine._ensure_started()
        running = threading.Event()

        async def server(task_status: Any) -> None:
            task_status.started("the value")
            running.set()
            await _forever(engine.backend)

        async def start() -> Any:
            return await loop.start_task(server)

        assert loop.call(start) == "the value"
        assert running.is_set()

    def test_a_failure_before_ready_reaches_the_starter(self, engine: Any) -> None:
        # and only the starter: the engine keeps running afterwards
        loop = engine._ensure_started()

        async def broken(task_status: Any) -> None:
            raise ValueError("never became ready")

        async def start() -> Any:
            return await loop.start_task(broken)

        with pytest.raises(ValueError, match="never became ready"):
            loop.call(start)

        async def fine() -> int:
            return 1

        assert loop.call(fine) == 1

    def test_a_task_that_never_signals_ready_is_reported(self, engine: Any) -> None:
        loop = engine._ensure_started()

        async def forgetful(task_status: Any) -> None:
            return

        async def start() -> Any:
            return await loop.start_task(forgetful)

        with pytest.raises(RuntimeError, match="started"):
            loop.call(start)

    def test_start_soon_requires_the_loop_thread(self, engine: Any) -> None:
        loop = engine._ensure_started()

        async def noop() -> None:
            return

        with pytest.raises(RuntimeError, match="engine thread"):
            loop.start_soon(noop)

    def test_a_forked_child_may_not_reach_the_parent_loop(
        self, engine: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # the parent loop's handle still *accepts* work in a child, so every
        # portal compares pids rather than let the child wait forever
        loop = engine._ensure_started()
        monkeypatch.setattr(loop.portal, "_pid", loop.portal._pid + 1)
        with pytest.raises(ForkedResourceError):
            loop.portal.post(lambda: None)

    def test_nothing_is_registered_on_a_fresh_engine(self, engine: Any) -> None:
        assert engine._ensure_started().live_groups() == ""

    def test_terminate_and_close_are_quiet_with_nothing_running(
        self, engine: Any
    ) -> None:
        engine.terminate(timeout=5.0)
        assert engine.running
        with warnings.catch_warnings():
            warnings.simplefilter("error", execnet.ActiveGroupsWarning)
            engine.close(timeout=10.0)
        assert not engine.running


class TestOnlyTrioHostsTheCore:
    """The asyncio engine is a loop, not yet a place gateways can live."""

    @pytest.mark.skipif(
        sys.version_info < MIN_PYTHON, reason="asyncio engine needs 3.11"
    )
    def test_a_group_on_an_asyncio_engine_says_what_is_missing(self) -> None:
        engine = ProtocolEngine(backend="asyncio", name="execnet-engine-unported")
        try:
            with pytest.raises(NotImplementedError, match="has not been ported"):
                execnet.Group(engine=engine).makegateway("popen")
        finally:
            engine.close(timeout=10.0)

    def test_a_group_on_a_trio_engine_does_not(self) -> None:
        engine = ProtocolEngine(backend="trio", name="execnet-engine-ported")
        group = execnet.Group(engine=engine)
        try:
            channel = group.makegateway("popen").remote_exec("channel.send(1)")
            assert channel.receive(TESTTIMEOUT) == 1
        finally:
            group.terminate(timeout=10.0)
            engine.close(timeout=10.0)


class TestBackendSelection:
    def test_the_default_is_trio(self) -> None:
        assert ProtocolEngine().backend == "trio"
        engine = ProtocolEngine(name="execnet-engine-default-backend")
        try:
            assert isinstance(engine.start()._ensure_started(), TrioEngine)
        finally:
            engine.close(timeout=10.0)

    def test_an_unknown_backend_is_refused_at_construction(self) -> None:
        with pytest.raises(ValueError, match="unknown engine backend"):
            ProtocolEngine(backend="curio")

    def test_the_repr_names_the_backend(self) -> None:
        assert "trio" in repr(ProtocolEngine())

    @pytest.mark.skipif(
        sys.version_info < MIN_PYTHON, reason="asyncio engine needs 3.11"
    )
    def test_asyncio_builds_an_asyncio_loop(self) -> None:
        engine = ProtocolEngine(backend="asyncio", name="execnet-engine-selected")
        try:
            assert isinstance(engine.start()._ensure_started(), AsyncioEngine)
        finally:
            engine.close(timeout=10.0)

    def test_an_interpreter_without_taskgroup_is_refused_with_the_reason(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # refused when the engine is built, not at start(): the Python
        # version is a fact nothing a caller does later can change
        monkeypatch.setattr(sys, "version_info", (3, 10, 12))
        with pytest.raises(RuntimeError, match="3.11 or newer"):
            ProtocolEngine(backend="asyncio")
        with pytest.raises(RuntimeError, match="does not carry a backport"):
            AsyncioEngine()


async def _forever(backend: str) -> None:
    """Park until the engine's own shutdown cancels this task."""
    if backend == "trio":
        import trio

        await trio.sleep_forever()
    else:
        import asyncio

        await asyncio.Event().wait()
