"""The carriers: one result crossing from the engine to a caller's loop.

The interesting behaviour is what happens when the caller is cancelled and
the engine produced something anyway.  Those two events race by
construction, and both orderings are reachable in the wild but neither is
reachable *reliably* by sleeping -- a timing test here passed for a year
without the salvage path ever running.  So each ordering is built
explicitly, by driving the carrier directly.

The rule both carriers implement: a value nobody is left to take is handed
to the call's ``salvage`` rather than dropped; an *error* nobody is left to
take is dropped, because it describes the operation the caller abandoned
and the next call will raise its own.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
import trio

from execnet._bridge import AsyncioCarrier
from execnet._bridge import TrioCarrier


class TestTrioCarrier:
    def test_a_result_arriving_after_the_cancel_is_salvaged(self) -> None:
        salvaged: list[Any] = []

        async def main() -> None:
            carrier = TrioCarrier()
            carrier.set_salvage(salvaged.append)
            with trio.move_on_after(0.01):
                await carrier.wait(shield=False, on_cancel=lambda: None)
            # the engine finishes late and delivers to a caller that is gone
            carrier.resolve(42, None)
            await trio.sleep(0.01)  # let the entry-queue callback run

        trio.run(main)
        assert salvaged == [42]

    def test_a_result_already_here_when_the_cancel_lands_is_salvaged(self) -> None:
        # trio delivers the cancel at the checkpoint even though the event is
        # already set, so the value is sitting in the carrier unclaimed
        salvaged: list[Any] = []

        async def main() -> None:
            carrier = TrioCarrier()
            carrier.set_salvage(salvaged.append)
            carrier.resolve(7, None)
            await trio.sleep(0)  # the delivery lands first
            with trio.CancelScope() as scope:
                scope.cancel()
                await carrier.wait(shield=False, on_cancel=lambda: None)

        trio.run(main)
        assert salvaged == [7]

    def test_an_abandoned_error_is_not_salvaged(self) -> None:
        salvaged: list[Any] = []

        async def main() -> None:
            carrier = TrioCarrier()
            carrier.set_salvage(salvaged.append)
            with trio.move_on_after(0.01):
                await carrier.wait(shield=False, on_cancel=lambda: None)
            carrier.resolve(None, EOFError("gone"))
            await trio.sleep(0.01)

        trio.run(main)
        assert salvaged == []

    def test_nothing_is_salvaged_when_the_caller_takes_it(self) -> None:
        salvaged: list[Any] = []

        async def main() -> None:
            carrier = TrioCarrier()
            carrier.set_salvage(salvaged.append)
            carrier.resolve(1, None)
            assert await carrier.wait(shield=False, on_cancel=lambda: None) == 1

        trio.run(main)
        assert salvaged == []

    def test_a_call_with_no_salvage_still_works(self) -> None:
        async def main() -> None:
            carrier = TrioCarrier()
            with trio.move_on_after(0.01):
                await carrier.wait(shield=False, on_cancel=lambda: None)
            carrier.resolve(5, None)
            await trio.sleep(0.01)

        trio.run(main)  # must not raise


class TestAsyncioCarrier:
    def test_a_result_arriving_after_the_cancel_is_salvaged(self) -> None:
        salvaged: list[Any] = []

        async def main() -> None:
            carrier = AsyncioCarrier()
            carrier.set_salvage(salvaged.append)
            task = asyncio.ensure_future(
                carrier.wait(shield=False, on_cancel=lambda: None)
            )
            await asyncio.sleep(0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            # the engine finishes late and delivers to a caller that is gone
            carrier.resolve(42, None)
            await asyncio.sleep(0.01)

        asyncio.run(main())
        assert salvaged == [42]

    def test_an_abandoned_error_is_not_salvaged(self) -> None:
        salvaged: list[Any] = []

        async def main() -> None:
            carrier = AsyncioCarrier()
            carrier.set_salvage(salvaged.append)
            task = asyncio.ensure_future(
                carrier.wait(shield=False, on_cancel=lambda: None)
            )
            await asyncio.sleep(0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            carrier.resolve(None, EOFError("gone"))
            await asyncio.sleep(0.01)

        asyncio.run(main())
        assert salvaged == []

    def test_nothing_is_salvaged_when_the_caller_takes_it(self) -> None:
        salvaged: list[Any] = []

        async def main() -> None:
            carrier = AsyncioCarrier()
            carrier.set_salvage(salvaged.append)
            carrier.resolve(1, None)
            await asyncio.sleep(0.01)
            assert await carrier.wait(shield=False, on_cancel=lambda: None) == 1

        asyncio.run(main())
        assert salvaged == []
