"""Worker side of ``GATEWAY_RSYNC``: receive an rsync into a directory.

Before 3.0 an rsync target was a ``remote_exec`` of ``_rsync_remote``'s
source.  That worked, but it was the last thing execnet shipped its own
source over the wire to do, it spent an exec slot on infrastructure, and
it could not run against a ``profile=trio`` worker at all -- that profile
rejects sync sources, and the receiver is thoroughly sync.

Now it is a protocol request the worker serves itself, like
``GATEWAY_START_SOCKET`` and ``GATEWAY_START_SUB``: the coordinator sends
``GATEWAY_RSYNC`` on a channel and drives the same conversation as before
over it.

The receiver body is unchanged and stays synchronous.  Rsync is file IO --
``lstat``, ``makedirs``, ``chmod``, whole-file reads and writes -- and
threading a loop through every one of those calls would rewrite it into
something much harder to read for no gain.  Instead it runs in a worker
thread and reaches the channel through :class:`_ThreadChannel`, so the
loop keeps serving every other channel while a tree is copied.
"""

from __future__ import annotations

import functools
from typing import TYPE_CHECKING
from typing import Any
from typing import cast

import trio

from ._errors import geterrortext
from ._rsync_remote import serve_rsync
from ._trace import trace

if TYPE_CHECKING:
    from ._channel import Channel


class _ThreadChannel:
    """The receiver's sync view of an async channel, from a worker thread.

    ``trio.from_thread.run`` needs no token here: the thread was started by
    ``trio.to_thread.run_sync``, so it knows which run it belongs to.
    """

    def __init__(self, channel: Any) -> None:
        self._channel = channel

    def send(self, item: object) -> None:
        trio.from_thread.run(self._channel.send, item)

    def receive(self) -> Any:
        return trio.from_thread.run(self._channel.receive)


async def serve_rsync_request(gateway: Any, channelid: int, request: Any) -> None:
    """Serve one ``GATEWAY_RSYNC`` request; a task on the worker's loop.

    Contains its own failures like every other ``start_soon`` entry point:
    this runs on the worker's *root* nursery, so an exception leaving it
    would end ``trio.run`` and take every gateway in the process with it.
    The coordinator is waiting on this channel, so a failure closes it with
    the reason and surfaces at its ``waitclose()``.
    """
    channel = gateway.open_channel(channelid)
    try:
        destdir, options = request
        # the receiver only ever calls send/receive on it, which is the
        # whole of what _ThreadChannel provides
        sync_view = cast("Channel", _ThreadChannel(channel))
        await trio.to_thread.run_sync(
            functools.partial(serve_rsync, sync_view, destdir, options),
            abandon_on_cancel=True,
        )
    except trio.Cancelled:
        raise
    except BaseException as exc:
        trace(f"rsync on channel {channelid} failed: {exc!r}")
        with trio.CancelScope(shield=True):
            try:
                await channel.aclose(geterrortext(exc))
            except Exception:  # the connection went away first
                pass
        return
    await channel.aclose()
