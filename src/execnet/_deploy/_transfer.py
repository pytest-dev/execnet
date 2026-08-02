"""The coordinator half of a transfer: async, one task per target.

The conversation, per target, over one service channel::

    ->  manifest                   the whole tree in one message
    <-  wanted                     paths, plus digests for the maybe-changed
    ->  (path, length), chunks     per file, each read in its own thread
    ->  None                       no more bodies
    <-  "done"                     after modes, mtimes, links and deletes

Everything here runs on a loop -- the caller's own under
:mod:`execnet.trio`, the host's under every other surface -- and every
blocking thing it does (walking the tree, reading a file) is a
``to_thread`` hop.  That is what lets a fan-out put twenty targets in
flight at once instead of doing them one after another, which is the
difference between deploying to a cluster and deploying to a cluster
twenty times.
"""

from __future__ import annotations

import functools
import os
from collections.abc import Callable
from collections.abc import Sequence
from hashlib import md5
from typing import TYPE_CHECKING

import trio

from ._manifest import Filter
from ._manifest import Manifest
from ._manifest import Wanted
from ._manifest import walk

if TYPE_CHECKING:
    from .._services import ServiceTarget

#: the service this drives
SERVICE = "transfer"

#: how much of a file body travels in one message.  A whole file in one
#: message spikes memory on both ends by its size, and a deployment ships
#: wheels; there is no flow control yet (a fast sender still outruns a slow
#: receiver into its buffers), so this bounds the spike, not the queue.
CHUNK_SIZE = 1 << 20

#: ``(path, size) -> None``, called as each body is sent.  Runs in the
#: thread that read the file, so it may block.
Progress = Callable[[str, int], None]


async def snapshot(
    source: str | os.PathLike[str], filter: Filter | None = None
) -> Manifest:
    """Walk ``source`` once, off the loop, for however many targets follow."""
    return await trio.to_thread.run_sync(
        functools.partial(walk, os.fspath(source), filter)
    )


def _read_body(
    path: str,
    relpath: str,
    digest: bytes | None,
    progress: Progress | None,
) -> bytes | None:
    """The bytes to send for ``path``: None when unchanged or gone.

    A file that vanished between the walk and here is not an error -- the
    walk cannot hold a tree still, and a filter that deletes things is a
    thing people write.  The receiver leaves what it has.

    ``progress`` fires here, in this thread, because it is the one place
    that already knows the body was read and is already off the loop: a
    reporting callback that prints or takes a lock must not run on it.
    """
    try:
        with open(path, "rb") as stream:
            data = stream.read()
    except OSError:
        return None
    if digest is not None and md5(data).digest() == digest:
        return None
    if progress is not None:
        progress(relpath, len(data))
    return data


async def send_manifest(
    target: ServiceTarget,
    manifest: Manifest,
    source: str | os.PathLike[str],
    destination: str,
    *,
    delete: bool = False,
    progress: Progress | None = None,
) -> None:
    """Send ``manifest``'s tree to ``destination`` on one target.

    Cancelling closes the channel, which is what stops the receiver: it is
    waiting on this conversation and would otherwise sit mid-tree with no
    way to learn that nobody is coming back.
    """
    source = os.fspath(source)
    channel = await target.open(
        SERVICE, {"destination": destination, "delete": delete}
    )
    try:
        await channel.send(manifest.dump())
        wanted = Wanted.load(await channel.receive())
        for path in wanted.paths:
            local = os.path.join(source, *path.split("/"))
            body = await trio.to_thread.run_sync(
                _read_body, local, path, wanted.checksums.get(path), progress
            )
            if body is None:
                # unchanged after all, or gone since the walk
                await channel.send((path, None))
                continue
            # the length first, so the receiver knows how many bytes to
            # expect -- an empty file is zero chunks, not one empty one
            await channel.send((path, len(body)))
            for start in range(0, len(body), CHUNK_SIZE):
                await channel.send(body[start : start + CHUNK_SIZE])
        await channel.send(None)
        reply = await channel.receive()
        if reply != "done":
            raise OSError(f"transfer to {destination} ended with {reply!r}")
    finally:
        with trio.CancelScope(shield=True):
            await channel.aclose()


async def transfer_tree(
    target: ServiceTarget,
    source: str | os.PathLike[str],
    destination: str,
    *,
    filter: Filter | None = None,
    delete: bool = False,
    progress: Progress | None = None,
) -> None:
    """Walk ``source`` and send it to one target."""
    manifest = await snapshot(source, filter)
    await send_manifest(
        target, manifest, source, destination, delete=delete, progress=progress
    )


async def transfer_tree_to_all(
    targets: Sequence[tuple[ServiceTarget, str]],
    source: str | os.PathLike[str],
    *,
    filter: Filter | None = None,
    delete: bool = False,
    progress: Progress | None = None,
) -> None:
    """Send ``source`` to every ``(target, destination)``, concurrently.

    The tree is walked once and shared; each target gets its own task, so
    the slowest one bounds the wall clock rather than the sum of them.
    """
    manifest = await snapshot(source, filter)
    if len(targets) == 1:
        target, destination = targets[0]
        await send_manifest(
            target, manifest, source, destination, delete=delete, progress=progress
        )
        return
    async with trio.open_nursery() as nursery:
        for target, destination in targets:
            nursery.start_soon(
                functools.partial(
                    send_manifest,
                    target,
                    manifest,
                    source,
                    destination,
                    delete=delete,
                    progress=progress,
                )
            )
