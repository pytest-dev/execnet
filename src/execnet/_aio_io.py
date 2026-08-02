"""asyncio implementations of the byte streams and processes the core needs.

The core reaches IO through :class:`~execnet._trio_gateway.ByteStream` --
four methods, and structurally satisfied by trio's own stream types.  This
module is the other implementation, plus the process handle that goes with
it.

asyncio's streams are a reader/writer pair rather than one object, and its
errors are ``OSError`` subclasses rather than a resource vocabulary, so
each wrapper here does two things: put the pair behind one object, and
translate what goes wrong into the words the core catches
(:mod:`execnet._async`).

What is *not* here yet: TCP and unix listeners, which the ``socket=``,
``installvia=`` and ``ssh=`` transports need.  ``popen`` -- the default,
and the one every test uses -- needs only what is below.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
from typing import Any

from ._async import BrokenResource
from ._async import ClosedResource


def _translate(exc: BaseException) -> BaseException:
    """The core's word for what went wrong with a stream."""
    if isinstance(exc, ConnectionError | BrokenPipeError):
        return BrokenResource(str(exc) or type(exc).__name__)
    if isinstance(exc, OSError):
        return BrokenResource(str(exc) or type(exc).__name__)
    return exc


class AsyncioByteStream:
    """One :class:`ByteStream` over an asyncio reader/writer pair."""

    def __init__(
        self, reader: asyncio.StreamReader | None, writer: asyncio.StreamWriter | None
    ) -> None:
        self._reader = reader
        self._writer = writer
        self._closed = False

    async def send_all(self, data: bytes) -> None:
        if self._closed or self._writer is None:
            raise ClosedResource("stream is closed for sending")
        try:
            self._writer.write(data)
            await self._writer.drain()
        except Exception as exc:
            raise _translate(exc) from None

    async def receive_some(self, max_bytes: int | None = None) -> bytes:
        if self._reader is None:
            raise ClosedResource("stream has no receive side")
        try:
            return await self._reader.read(max_bytes or 65536)
        except Exception as exc:
            raise _translate(exc) from None

    async def send_eof(self) -> None:
        """Half-close the send side, so the peer reads EOF.

        Falls back to closing the writer where the transport cannot
        half-close -- a pipe, mostly -- which is what trio's stapled
        streams do too.
        """
        if self._writer is None:
            return
        try:
            if self._writer.can_write_eof():
                self._writer.write_eof()
            else:
                self._writer.close()
        except Exception as exc:
            raise _translate(exc) from None

    async def aclose(self) -> None:
        self._closed = True
        if self._writer is None:
            return
        try:
            self._writer.close()
            await self._writer.wait_closed()
        except Exception:
            # closing reports what already went wrong; the caller is done
            # with the stream either way
            pass


async def wrap_socket(sock: Any) -> AsyncioByteStream:
    """A stream over an already-connected stdlib socket."""
    reader, writer = await asyncio.open_connection(sock=sock)
    return AsyncioByteStream(reader, writer)


class AsyncioProcess:
    """The process handle the core expects, over ``asyncio.subprocess``.

    Trio's ``Process`` exposes ``stdin``/``stdout`` as streams, ``wait``,
    ``kill``, ``returncode`` and ``pid``; asyncio's has the same names with
    reader/writer objects instead, so only the stream halves need wrapping.
    """

    def __init__(self, process: asyncio.subprocess.Process) -> None:
        self._process = process
        self.stdin = process.stdin
        self.stdout = process.stdout

    @property
    def pid(self) -> int:
        return self._process.pid

    @property
    def returncode(self) -> int | None:
        return self._process.returncode

    async def wait(self) -> int:
        return await self._process.wait()

    def kill(self) -> None:
        try:
            self._process.kill()
        except ProcessLookupError:
            pass  # already gone, which is what kill was for


async def open_process(argv: list[str], **kwargs: Any) -> AsyncioProcess:
    """Spawn ``argv``; the asyncio spelling of ``trio.lowlevel.open_process``."""
    process = await asyncio.create_subprocess_exec(*argv, **kwargs)
    return AsyncioProcess(process)


def staple_process(process: AsyncioProcess) -> AsyncioByteStream:
    """One bidirectional stream over a process's stdin/stdout pair."""
    assert process.stdin is not None
    assert process.stdout is not None
    return AsyncioByteStream(process.stdout, process.stdin)


async def staple_fds(read_fd: int, write_fd: int) -> AsyncioByteStream:
    """One stream over a pair of blocking fds (POSIX).

    The Windows path uses the threaded stand-in in the core, as it does for
    trio, because neither library can wait on a Windows pipe handle.
    """
    if sys.platform == "win32":  # pragma: no cover - POSIX-only path
        raise NotImplementedError("use the threaded fd stream on Windows")
    loop = asyncio.get_running_loop()

    reader = asyncio.StreamReader()
    await loop.connect_read_pipe(
        lambda: asyncio.StreamReaderProtocol(reader), _fdopen(read_fd, "rb")
    )
    transport, protocol = await loop.connect_write_pipe(
        asyncio.streams.FlowControlMixin, _fdopen(write_fd, "wb")
    )
    writer = asyncio.StreamWriter(transport, protocol, reader, loop)
    return AsyncioByteStream(reader, writer)


def _fdopen(fd: int, mode: str) -> Any:
    import os

    return os.fdopen(fd, mode, buffering=0)


#: subprocess constants re-exported so the core can name them once
DEVNULL = subprocess.DEVNULL
PIPE = subprocess.PIPE
