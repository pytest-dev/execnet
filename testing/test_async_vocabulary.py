"""The vocabulary the protocol core is written against, on both backends.

Every test runs twice, once per async library, because the whole point of
the module is that the core cannot tell which one it got.  Where the two
genuinely differ -- cancellation -- the difference is asserted rather than
smoothed over, so it stays visible.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from typing import Any

import pytest

from execnet._async import MIN_ASYNCIO_PYTHON
from execnet._async import AsyncioAsync
from execnet._async import AsyncLibUnavailable
from execnet._async import TrioAsync
from execnet._async import current_async
from execnet._async import for_backend

BACKENDS = ["trio"] + (["asyncio"] if sys.version_info >= MIN_ASYNCIO_PYTHON else [])


def run(backend: str, async_fn: Callable[..., Any], *args: Any) -> Any:
    """Run ``async_fn`` on the named backend and return its result."""
    if backend == "trio":
        import trio

        return trio.run(async_fn, *args)
    import asyncio

    return asyncio.run(async_fn(*args))


@pytest.fixture(params=BACKENDS)
def backend(request: pytest.FixtureRequest) -> str:
    return str(request.param)


class TestDetection:
    def test_the_running_loop_decides(self, backend: str) -> None:
        async def main() -> str:
            return str(current_async().name)

        assert run(backend, main) == backend

    def test_outside_a_loop_there_is_nothing_to_detect(self) -> None:
        with pytest.raises(RuntimeError, match="no running event loop"):
            current_async()

    def test_each_backend_is_built_once(self) -> None:
        assert for_backend("trio") is for_backend("trio")

    def test_an_unknown_backend_is_refused(self) -> None:
        with pytest.raises(ValueError, match="unknown async backend"):
            for_backend("curio")


class TestPrimitives:
    def test_an_event_wakes_a_waiter(self, backend: str) -> None:
        async def main() -> str:
            aio = current_async()
            event = aio.event()
            seen = []

            async def waiter() -> None:
                await event.wait()
                seen.append("woken")

            async with aio.task_scope() as scope:
                scope.start_soon(waiter)
                await aio.checkpoint()
                event.set()
            return seen[0]

        assert run(backend, main) == "woken"

    def test_a_queue_carries_items_in_order(self, backend: str) -> None:
        async def main() -> list[int]:
            aio = current_async()
            sender, receiver = aio.queue()
            for index in range(3):
                sender.send_nowait(index)
            return [await receiver.receive() for _ in range(3)]

        assert run(backend, main) == [0, 1, 2]

    def test_a_closed_queue_ends_and_stays_ended(self, backend: str) -> None:
        async def main() -> str:
            aio = current_async()
            sender, receiver = aio.queue()
            sender.send_nowait("last")
            sender.close()
            assert await receiver.receive() == "last"
            for _ in range(2):  # every later receive sees the end too
                try:
                    await receiver.receive()
                except aio.CHANNEL_EMPTY:
                    continue
                return "did not end"
            return "ended"

        assert run(backend, main) == "ended"

    def test_sending_to_a_closed_queue_is_refused(self, backend: str) -> None:
        async def main() -> str:
            aio = current_async()
            sender, _ = aio.queue()
            sender.close()
            try:
                sender.send_nowait("nope")
            except aio.CHANNEL_UNUSABLE:
                return "refused"
            return "accepted"

        assert run(backend, main) == "refused"

    def test_receive_nowait_reports_an_empty_queue(self, backend: str) -> None:
        async def main() -> str:
            aio = current_async()
            _, receiver = aio.queue()
            try:
                receiver.receive_nowait()
            except aio.CHANNEL_UNUSABLE:
                return "empty"
            return "got something"

        assert run(backend, main) == "empty"

    def test_a_limiter_bounds_concurrency(self, backend: str) -> None:
        async def main() -> int:
            aio = current_async()
            limiter = aio.limiter(2)
            peak = 0
            live = 0

            async def hold() -> None:
                nonlocal peak, live
                async with limiter:
                    live += 1
                    peak = max(peak, live)
                    await aio.checkpoint()
                    live -= 1

            async with aio.task_scope() as scope:
                for _ in range(6):
                    scope.start_soon(hold)
            return peak

        assert run(backend, main) <= 2

    def test_a_thread_hop_returns_its_value(self, backend: str) -> None:
        async def main() -> int:
            aio = current_async()
            return int(await aio.to_thread(lambda value: value * 2, 21))

        assert run(backend, main) == 42


class TestTaskScope:
    def test_it_waits_for_its_children(self, backend: str) -> None:
        async def main() -> list[str]:
            aio = current_async()
            done: list[str] = []

            async def child(name: str) -> None:
                await aio.checkpoint()
                done.append(name)

            async with aio.task_scope() as scope:
                scope.start_soon(child, "a")
                scope.start_soon(child, "b")
            return sorted(done)

        assert run(backend, main) == ["a", "b"]

    def test_start_waits_until_the_child_is_ready(self, backend: str) -> None:
        async def main() -> Any:
            aio = current_async()

            async def server(task_status: Any) -> None:
                task_status.started("serving")
                await aio.sleep_forever()

            async with aio.task_scope() as scope:
                value = await scope.start(server)
                scope.cancel()
                return value

        assert run(backend, main) == "serving"

    def test_a_failure_before_ready_reaches_the_starter(self, backend: str) -> None:
        async def main() -> str:
            aio = current_async()

            async def broken(task_status: Any) -> None:
                raise ValueError("never ready")

            async with aio.task_scope() as scope:
                try:
                    await scope.start(broken)
                except ValueError as exc:
                    return str(exc)
            return "no error"

        assert run(backend, main) == "never ready"

    def test_cancel_ends_the_children(self, backend: str) -> None:
        async def main() -> str:
            aio = current_async()

            async def forever() -> None:
                await aio.sleep_forever()

            async with aio.task_scope() as scope:
                scope.start_soon(forever)
                scope.start_soon(forever)
                scope.cancel()
            return "returned"

        assert run(backend, main) == "returned"


class TestDeadlines:
    def test_move_on_after_gives_up_quietly(self, backend: str) -> None:
        async def main() -> str:
            aio = current_async()
            with aio.move_on_after(0.01) as scope:
                await aio.sleep_forever()
            assert scope.cancelled_caught
            return "moved on"

        assert run(backend, main) == "moved on"

    def test_move_on_after_does_not_fire_when_the_body_finishes(
        self, backend: str
    ) -> None:
        async def main() -> str:
            aio = current_async()
            with aio.move_on_after(10) as scope:
                await aio.checkpoint()
            assert not scope.cancelled_caught
            return "finished"

        assert run(backend, main) == "finished"

    def test_fail_after_raises(self, backend: str) -> None:
        async def main() -> str:
            aio = current_async()
            try:
                with aio.fail_after(0.01):
                    await aio.sleep_forever()
            except aio.TooSlow:
                return "raised"
            return "did not raise"

        assert run(backend, main) == "raised"

    def test_a_deadline_does_not_eat_an_outer_cancel(self, backend: str) -> None:
        # the delicate one: an outer cancellation arriving while an inner
        # deadline is armed must still reach the outer scope
        async def main() -> str:
            aio = current_async()
            reached: list[str] = []

            async def child() -> None:
                try:
                    with aio.move_on_after(30):
                        await aio.sleep_forever()
                    reached.append("deadline swallowed the outer cancel")
                except aio.Cancelled:
                    reached.append("outer cancel got through")
                    raise

            async with aio.task_scope() as scope:
                scope.start_soon(child)
                await aio.checkpoint()
                scope.cancel()
            return reached[0]

        assert run(backend, main) == "outer cancel got through"


class TestShielding:
    """Where the two backends genuinely differ, and why it does not matter."""

    def test_shielded_cleanup_completes_after_a_cancel(self, backend: str) -> None:
        async def main() -> str:
            aio = current_async()
            done: list[str] = []

            async def child() -> None:
                try:
                    await aio.sleep_forever()
                except aio.Cancelled:
                    with aio.shielded():
                        await aio.checkpoint()
                        done.append("cleanup ran")
                    raise

            async with aio.task_scope() as scope:
                scope.start_soon(child)
                await aio.checkpoint()
                scope.cancel()
            return done[0]

        assert run(backend, main) == "cleanup ran"

    def test_bounded_shielded_cleanup_completes(self, backend: str) -> None:
        # the combination the core actually uses: shielded, but not forever
        async def main() -> str:
            aio = current_async()
            done: list[str] = []

            async def child() -> None:
                try:
                    await aio.sleep_forever()
                except aio.Cancelled:
                    with aio.shielded(), aio.move_on_after(5):
                        await aio.checkpoint()
                        done.append("bounded cleanup ran")
                    raise

            async with aio.task_scope() as scope:
                scope.start_soon(child)
                await aio.checkpoint()
                scope.cancel()
            return done[0]

        assert run(backend, main) == "bounded cleanup ran"

    def test_the_bound_still_fires_on_a_stuck_cleanup(self, backend: str) -> None:
        async def main() -> str:
            aio = current_async()
            done: list[str] = []

            async def child() -> None:
                try:
                    await aio.sleep_forever()
                except aio.Cancelled:
                    with aio.shielded(), aio.move_on_after(0.01):
                        await aio.sleep_forever()
                    done.append("gave up on the cleanup")
                    raise

            async with aio.task_scope() as scope:
                scope.start_soon(child)
                await aio.checkpoint()
                scope.cancel()
            return done[0]

        assert run(backend, main) == "gave up on the cleanup"


class TestAvailability:
    def test_asyncio_is_refused_without_taskgroup(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(sys, "version_info", (3, 10, 12))
        with pytest.raises(AsyncLibUnavailable, match="3.11 or newer"):
            AsyncioAsync()

    def test_trio_is_always_available(self) -> None:
        assert TrioAsync().name == "trio"
