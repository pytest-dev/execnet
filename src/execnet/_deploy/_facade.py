"""Running the async core from the surfaces that are not trio.

The driver is one async function whichever way you reach it.  What differs
is who waits: under :mod:`execnet.raw_trio` the caller's own loop already
runs it, and here the caller has no loop, so it goes to the engine and the
calling thread parks the way its facade parks -- an event for plain
threads, a greenlet switch under :mod:`execnet.gevent`.
"""

from __future__ import annotations

import functools
from collections.abc import Awaitable
from collections.abc import Callable
from collections.abc import Sequence
from typing import TYPE_CHECKING
from typing import Any
from typing import TypeVar

from .._engine import check_not_in_event_loop
from .._services import ServiceTarget

if TYPE_CHECKING:
    from .._gateway import Gateway

T = TypeVar("T")


def targets_for(gateways: Sequence[Gateway]) -> list[ServiceTarget]:
    """Service targets for blocking-surface gateways."""
    return [ServiceTarget.from_sync(gateway) for gateway in gateways]


def run_blocking(
    gateways: Sequence[Gateway],
    async_fn: Callable[..., Awaitable[T]],
    *args: Any,
) -> T:
    """Run ``async_fn(*args, targets)`` on the gateways' engine, and wait.

    Every gateway has to be served by the same engine: the driver is one
    task, and a trio task cannot await a channel belonging to a different
    run.  In practice they come from one :class:`~execnet.Group`, which has
    one engine -- so this is a clear error rather than a real limitation.
    """
    if not gateways:
        raise ValueError("no gateways to work on")
    check_not_in_event_loop(f"{getattr(async_fn, '__name__', 'this call')}()")

    sessions = [gateway._trio_session for gateway in gateways]
    if any(session is None for session in sessions):
        raise OSError("a gateway with no connection cannot be worked on")
    engines = {id(session.engine) for session in sessions}
    if len(engines) > 1:
        raise ValueError(
            "all gateways must be served by the same execnet.ProtocolEngine:"
            " one driver task cannot reach channels belonging to another"
            " event loop. Gateways from one Group always share an engine."
        )

    from .._trio_engine import engine_call

    targets = targets_for(gateways)
    return engine_call(  # type: ignore[no-any-return]
        sessions[0].engine,
        gateways[0]._wait_backend,
        functools.partial(async_fn, *args, targets),
    )
