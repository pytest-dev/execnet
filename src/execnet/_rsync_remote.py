"""The pre-3.0 rsync receiver, kept only for its deprecated module name.

execnet no longer drives this: :class:`execnet.RSync` and
:func:`execnet.transfer` both speak the ``transfer`` service
(:mod:`execnet._deploy`), which needs no source shipped to a worker.  The
body below still works if it is ``remote_exec``-ed by hand, which is what
``execnet.rsync_remote`` used to be for, and goes when that shim does.

(c) 2006-2013, Armin Rigo, Holger Krekel, Maciej Fijalkowski
"""

from __future__ import annotations

from contextlib import suppress
from typing import TYPE_CHECKING
from typing import Literal
from typing import cast

if TYPE_CHECKING:
    from execnet._channel import Channel


def serve_rsync(
    channel: Channel,
    destdir: str | None = None,
    options: dict[str, object] | None = None,
) -> None:
    """Receive one rsync into ``destdir``.

    The channel is the only thing this needs, so the same body serves both
    ways it is reached: as a worker-side ``GATEWAY_RSYNC`` handler, which
    passes the destination in (and is how execnet drives it), and as an
    exec'd source that reads it off the channel first, which is the shape
    the pre-3.0 protocol used.
    """
    import os
    import shutil
    import stat
    from hashlib import md5

    if destdir is None:
        destdir, options = cast("tuple[str, dict[str, object]]", channel.receive())
    assert options is not None
    modifiedfiles = []

    def remove(path: str) -> None:
        assert path.startswith(destdir)
        try:
            os.unlink(path)
        except OSError:
            # assume it's a dir
            shutil.rmtree(path, True)

    def receive_directory_structure(path: str, relcomponents: list[str]) -> None:
        try:
            st = os.lstat(path)
        except OSError:
            st = None
        msg = channel.receive()
        if isinstance(msg, list):
            if st and not stat.S_ISDIR(st.st_mode):
                os.unlink(path)
                st = None
            if not st:
                os.makedirs(path)
            mode = msg.pop(0)
            if mode:
                # Ensure directories are writable, otherwise a
                # permission denied error (EACCES) would be raised
                # when attempting to receive read-only directory
                # structures.
                os.chmod(path, mode | 0o700)
            entrynames = {}
            for entryname in msg:
                destpath = os.path.join(path, entryname)
                receive_directory_structure(destpath, [*relcomponents, entryname])
                entrynames[entryname] = True
            if options.get("delete"):
                for othername in os.listdir(path):
                    if othername not in entrynames:
                        otherpath = os.path.join(path, othername)
                        remove(otherpath)
        elif msg is not None:
            assert isinstance(msg, tuple)
            checksum = None
            if st:
                if stat.S_ISREG(st.st_mode):
                    msg_mode, msg_mtime, msg_size = msg
                    if msg_size != st.st_size:
                        pass
                    elif msg_mtime != st.st_mtime:
                        with open(path, "rb") as fp:
                            checksum = md5(fp.read()).digest()
                    elif msg_mode and msg_mode != st.st_mode:
                        os.chmod(path, msg_mode | 0o700)
                        return
                    else:
                        return  # already fine
                else:
                    remove(path)
            channel.send(("send", (relcomponents, checksum)))
            modifiedfiles.append((path, msg))

    receive_directory_structure(destdir, [])

    STRICT_CHECK = False  # seems most useful this way for py.test
    channel.send(("list_done", None))

    for path, (mode, time, size) in modifiedfiles:
        data = cast(bytes, channel.receive())
        channel.send(("ack", path[len(destdir) + 1 :]))
        if data is not None:
            if STRICT_CHECK and len(data) != size:
                raise OSError(f"file modified during rsync: {path!r}")
            with open(path, "wb") as fp:
                fp.write(data)
        try:
            if mode:
                os.chmod(path, mode)
            os.utime(path, (time, time))
        except OSError:
            pass
        del data
    channel.send(("links", None))

    msg = channel.receive()
    while msg != 42:
        # we get symlink
        _type, relpath, linkpoint = cast(
            "tuple[Literal['linkbase', 'link'], str, str]", msg
        )
        path = os.path.join(destdir, relpath)
        with suppress(OSError):
            remove(path)

        if _type == "linkbase":
            src = os.path.join(destdir, linkpoint)
        else:
            assert _type == "link", _type
            src = linkpoint
        os.symlink(src, path)
        msg = channel.receive()
    channel.send(("done", None))


if __name__ == "__channelexec__":
    serve_rsync(channel)  # type: ignore[name-defined]  # noqa:F821
