"""The deprecated 1:N rsync API, on top of the transfer layer.

``RSync`` predates :mod:`execnet._deploy` and is kept because released
pytest-xdist subclasses it -- overriding :meth:`RSync.filter` and the
private :meth:`RSync._report_send_file`, and reading ``_sourcedir`` and
``_verbose``.  Everything it does is now expressed with the same transfer
:func:`execnet.transfer` uses, so there is one implementation rather than
two, and what is left here is the adaptation: a few dozen lines that can
be deleted whole once its callers have moved on.

One behaviour did not survive the move, and it is the one nothing uses:
the optional ``callback`` is handed the *gateway* rather than a channel as
its third argument, and its ``"ack"`` fires when a file is sent rather
than when the far side confirms it -- the new protocol has no per-file
acknowledgement to hang the old timing on.  The ``filter`` hook,
``_report_send_file`` and ``verbose`` reporting are unchanged.

(c) 2006-2009, Armin Rigo, Holger Krekel, Maciej Fijalkowski
"""

from __future__ import annotations

import os
from collections.abc import Callable
from collections.abc import Sequence
from typing import TYPE_CHECKING
from typing import Any

from execnet._gateway import Gateway
from execnet._gateway_base import BaseGateway

if TYPE_CHECKING:
    from execnet._deploy._manifest import Manifest
    from execnet._services import ServiceTarget

#: one added target: where it goes, what to call when it is done, and the
#: per-target options (only ``delete``)
_Target = tuple[Gateway, str, "Callable[[], None] | None", dict[str, Any]]


class RSync:
    """Send a directory structure (recursively) to one or more remotes.

    .. deprecated:: 3.0
       Use :func:`execnet.transfer`, or :class:`execnet.Deployment` for a
       whole project.  This class remains because pytest-xdist subclasses
       it, and is a thin adapter over the transfer those use.

    There is limited support for symlinks: one pointing inside the source
    tree is recreated pointing inside the destination, and any other is
    copied as it stands, whether or not its target exists over there.
    """

    def __init__(self, sourcedir, callback=None, verbose: bool = True) -> None:
        # normalise a trailing separator away now rather than during send():
        # subclasses read _sourcedir before then (xdist takes its basename
        # to decide what the remote directory is called)
        self._sourcedir = os.path.dirname(os.path.join(str(sourcedir), "x"))
        self._verbose = verbose
        assert callback is None or callable(callback)
        self._callback = callback
        self._targets: list[_Target] = []

    def filter(self, path: str) -> bool:
        """Whether ``path`` belongs in the transfer; override to exclude."""
        return True

    def _report_send_file(self, gateway: BaseGateway, modified_rel_path: str) -> None:
        """Called for each file actually sent; override to report otherwise."""
        if self._verbose:
            print(f"{gateway} <= {modified_rel_path}")

    def add_target(
        self,
        gateway: Gateway,
        destdir: str | os.PathLike[str],
        finishedcallback: Callable[[], None] | None = None,
        **options: Any,
    ) -> None:
        """Add a remote target: a gateway and a destination directory."""
        for name in options:
            assert name in ("delete",)
        self._targets.append((gateway, str(destdir), finishedcallback, options))

    def send(self, raises: bool = True) -> None:
        """Send the source directory to every added target.

        ``raises`` says what happens when there are no targets left --
        which is also what a second ``send()`` finds, since sending
        consumes them.
        """
        if not self._targets:
            if raises:
                raise OSError(
                    "no targets available, maybe you are trying call send() twice?"
                )
            return
        from execnet._deploy._facade import run_blocking

        targets, self._targets = self._targets, []
        run_blocking([target[0] for target in targets], self._send, targets)

    # -- the async half, run on the gateways' host --

    async def _send(
        self, targets: Sequence[_Target], service_targets: Sequence[ServiceTarget]
    ) -> None:
        import trio

        from execnet._deploy._transfer import snapshot

        # walked once, whatever the number of targets -- as it always was
        manifest = await snapshot(self._sourcedir, self.filter)
        if len(targets) == 1:
            await self._send_one(manifest, targets[0], service_targets[0])
            return
        async with trio.open_nursery() as nursery:
            for target, service_target in zip(targets, service_targets):
                nursery.start_soon(self._send_one, manifest, target, service_target)

    async def _send_one(
        self, manifest: Manifest, target: _Target, service_target: ServiceTarget
    ) -> None:
        from execnet._deploy._transfer import send_manifest

        gateway, destdir, finishedcallback, options = target
        sent: list[tuple[str, int]] = []

        def progress(relpath: str, size: int) -> None:
            # in the thread that read the file, which is off the loop -- an
            # override that prints or takes a lock must not run on it
            sent.append((relpath, size))
            self._report_send_file(gateway, relpath)
            if self._callback is not None:
                self._callback("ack", size, gateway)

        await send_manifest(
            service_target,
            manifest,
            self._sourcedir,
            destdir,
            delete=bool(options.get("delete")),
            progress=progress,
        )
        if self._callback is not None:
            self._callback("list", sum(size for _path, size in sent), gateway)
        if finishedcallback is not None:
            finishedcallback()
