"""Trio-native gateway core: async dispatch loop and low-level raw channels.

Async-first counterpart of the sync machinery in ``_channel`` /
``_gateway_base``: an
:class:`AsyncGateway` owns a :class:`ByteStream` and runs a single dispatch
task (stream -> ``FrameDecoder`` -> route).  Message handlers execute inline
on that task, so there is no receiver thread and no receive lock.

Two-level channel model:

* :class:`RawChannel` (this module) -- id-routed raw byte payload streams
  over the gateway: no serialization and no callbacks.
  ``CHANNEL_DATA`` payloads route to the channel verbatim; the layer on top
  decides what the bytes mean.
* ``AsyncChannel`` -- the serialized object API layered on a RawChannel.

The code deliberately sticks to idioms an anyio backend can mirror later:
a neutral ``ByteStream`` protocol, the sans-IO ``FrameDecoder``, and
unbounded memory channels.
"""

from __future__ import annotations

import functools
import math
import os
import subprocess
import sys
import types
import uuid
from collections.abc import AsyncIterator
from collections.abc import Callable
from contextlib import asynccontextmanager
from contextlib import suppress
from typing import TYPE_CHECKING
from typing import Any
from typing import Protocol
from typing import TypeVar

import trio

if TYPE_CHECKING:
    from typing_extensions import Self

from ._errors import GatewayReceivedTerminate
from ._errors import HostNotFound
from ._errors import RemoteError
from ._errors import TimeoutError
from ._exec_source import normalize_exec_source
from ._execmodel import resolve_profile
from ._handshake import read_ready
from ._handshake import send_config
from ._message import FrameDecoder
from ._message import Message
from ._message import gateway_info
from ._serialize import dumps_internal
from ._serialize import loads_internal
from ._trace import trace

RECEIVE_CHUNK = 65536

T = TypeVar("T")


async def provision_sync(fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """Run coordinator-side provisioning work off the loop thread.

    Everything in ``_provision`` that decides *what* to launch may block for
    a long time: an ``execnet info`` probe of a ``python=`` target runs a
    subprocess with a 30s timeout, a dev coordinator's ``uv build`` takes
    seconds on a cold cache, and shipped wheels are read whole off disk.
    Run inline it stalls the loop -- the caller's own ``trio.run`` for
    :mod:`execnet.trio`, and for every other surface the *shared* host,
    which is every gateway in the process including other groups'.

    Not abandoned on cancel: the build populates a wheel cache keyed by
    version, and a half-written entry there is one every later gateway
    would pick up.
    """
    return await trio.to_thread.run_sync(functools.partial(fn, *args, **kwargs))


class ByteStream(Protocol):
    """Neutral bidirectional byte-stream protocol for gateway transports.

    ``trio.StapledStream`` (process/fd pipe pairs) and ``trio.SocketStream``
    satisfy this structurally; a future anyio backend's byte streams use the
    same four names.  ``send_eof`` signals write-EOF to the peer (half-close
    for sockets; for pipe pairs trio falls back to closing the send half).
    """

    async def send_all(self, data: bytes) -> None: ...

    async def receive_some(self, max_bytes: int | None = None) -> bytes: ...

    async def send_eof(self) -> None: ...

    async def aclose(self) -> None: ...


def staple_process_stream(process: trio.Process) -> ByteStream:
    """One bidirectional stream over a Trio Process stdin/stdout pair."""
    assert process.stdin is not None
    assert process.stdout is not None
    return trio.StapledStream(process.stdin, process.stdout)


class ThreadedFdStream:
    """A :class:`ByteStream` over blocking fds, doing its IO in worker threads.

    The Windows stand-in for ``trio.lowlevel.FdStream``, which is POSIX-only.
    Trio *does* have Windows pipe streams, but they require handles opened in
    OVERLAPPED mode and register them with an IOCP -- and the stdio a process
    inherits from its parent is an ordinary synchronous pipe, so a worker
    cannot adopt its own fd 0/1 that way.

    Blocking reads and writes therefore go to the thread pool.  A pending
    read is abandoned on cancellation, since nothing can interrupt it short
    of the peer closing; a write is not, because a torn write would leave a
    half-message on the wire and desynchronize the framing.
    """

    def __init__(self, read_fd: int, write_fd: int) -> None:
        self._read_fd: int | None = read_fd
        self._write_fd: int | None = write_fd

    async def receive_some(self, max_bytes: int | None = None) -> bytes:
        fd = self._read_fd
        if fd is None:
            raise trio.ClosedResourceError("stream closed")
        return await trio.to_thread.run_sync(
            os.read, fd, max_bytes or 65536, abandon_on_cancel=True
        )

    async def send_all(self, data: bytes) -> None:
        fd = self._write_fd
        if fd is None:
            raise trio.ClosedResourceError("stream closed")
        view = memoryview(data)
        while view:
            written = await trio.to_thread.run_sync(os.write, fd, view)
            view = view[written:]

    async def send_eof(self) -> None:
        fd, self._write_fd = self._write_fd, None
        if fd is not None:
            os.close(fd)

    async def aclose(self) -> None:
        await self.send_eof()
        fd, self._read_fd = self._read_fd, None
        if fd is not None:
            os.close(fd)
        await trio.lowlevel.checkpoint()


def staple_fd_stream(read_fd: int, write_fd: int) -> ByteStream:
    """One bidirectional stream over OS pipe fds (worker stdio pipes)."""
    if not hasattr(trio.lowlevel, "FdStream"):  # Windows
        return ThreadedFdStream(read_fd, write_fd)
    return trio.StapledStream(
        trio.lowlevel.FdStream(write_fd), trio.lowlevel.FdStream(read_fd)
    )


async def configure_worker(stream: ByteStream, spec: Any, what: str) -> dict[str, Any]:
    """Configure the worker on ``stream`` and wait until it is serving.

    One ``GATEWAY_CONFIG`` frame out, one back (:mod:`execnet._handshake`).
    Identical on every transport, which is why nothing a worker needs has
    to be squeezed into its argv.

    A worker that dies mid-handshake is reported as ``EOFError`` whichever
    way its transport shows that: a dead peer *resets* a socket where a
    pipe reaches EOF, and callers here test for EOF.
    """
    from . import _provision

    config = _provision.worker_config(spec) if spec is not None else {}
    try:
        await send_config(stream, config)
        return await read_ready(stream, what)
    except (trio.BrokenResourceError, trio.ClosedResourceError) as exc:
        error = EOFError(f"the worker went away during the {what} handshake: {exc}")
        raise error from exc


async def open_popen_process(args: list[str]) -> trio.Process:
    return await trio.lowlevel.open_process(
        args,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
    )


def popen_module_args(
    spec: Any, *protocol: str, local_config_on_stdin: bool = False
) -> list[str]:
    """Launch the Trio worker as a module: ``python -m execnet worker``.

    No source is sent over the wire; the worker imports the installed execnet +
    trio.  Used for same-interpreter popen and for a ``python=`` interpreter that
    already has execnet (so ``sys.executable`` stays that interpreter).

    Nothing about the gateway is in here: what this worker *is* arrives as
    the config frame.  ``local_config_on_stdin`` is only the Windows
    ``share`` blob, which has to exist before the stream does.
    """
    from . import _provision

    if getattr(spec, "python", None):
        interpreter = _provision.shell_split_path(spec.python)
    else:
        interpreter = [sys.executable]

    args = [*interpreter, "-u"]
    if getattr(spec, "dont_write_bytecode", False):
        args.append("-B")
    args += ["-m", "execnet", "worker", *protocol]
    if local_config_on_stdin:
        args += ["--config-fd", "0"]
    return args


def popen_worker_argv(
    spec: Any, *protocol: str, local_config_on_stdin: bool = False
) -> list[str]:
    """Argv for a popen worker: direct module launch, or uv-provisioned.

    A bare ``python=`` interpreter without execnet gets execnet + trio
    provisioned via ``uv``; otherwise the worker module is launched directly.
    """
    from . import _provision

    if spec.python and not _provision.target_has_execnet(spec.python):
        return _provision.uv_worker_argv(
            spec, *protocol, local_config_on_stdin=local_config_on_stdin
        )
    return popen_module_args(
        spec, *protocol, local_config_on_stdin=local_config_on_stdin
    )


class RawChannel:
    """Low-level id-routed byte payload stream over an :class:`AsyncGateway`.

    Payload boundaries are preserved: every :meth:`send_bytes` arrives as
    one :meth:`receive_bytes` result on the peer.  No serialization and no
    flow control beyond the gateway's outbound queue.

    Close semantics mirror the sync ``Channel`` state machine:

    * :meth:`aclose` closes both directions (``CHANNEL_CLOSE`` /
      ``CHANNEL_CLOSE_ERROR`` to the peer).
    * :meth:`send_eof` only ends our payload stream (``CHANNEL_LAST_MESSAGE``);
      the peer drains, hits EOF, and may keep sending to us.
    """

    def __init__(self, gateway: AsyncGateway, id: int) -> None:
        self.gateway = gateway
        self.id = id
        self._closed = False  # no more sends (local aclose or remote close)
        self._sent_eof = False
        self._remote_closed = False
        #: whether local code has ever been handed this channel.  If it has,
        #: it owns the object and the gateway's registry is only a router;
        #: if it has not, the registry *is* the only thing holding what
        #: arrived, and a late binder has to find it there.
        self._handed_out = False
        self._receive_closed = trio.Event()  # no more payloads will arrive
        self._remote_error: RemoteError | None = None
        self._payload_send, self._payloads = trio.open_memory_channel[bytes](math.inf)
        # Diversion hooks for a bound facade (sync channel): when set,
        # inbound payloads/closes route out of the loop instead of
        # buffering for receive_bytes.
        self._consumer_payload: Callable[[bytes], None] | None = None
        self._consumer_close: Callable[[RemoteError | None, bool], None] | None = None
        self._pending_close: tuple[RemoteError | None, bool] | None = None

    def __repr__(self) -> str:
        state = "closed" if self._closed else "open"
        return f"<RawChannel id={self.id} {state}>"

    async def send_bytes(self, data: bytes) -> None:
        """Send one payload; the peer receives it as a single item.

        OSError is raised when the channel or gateway is closed, matching
        the sync ``Channel.send`` contract.
        """
        if self._closed or self._sent_eof:
            raise OSError(f"cannot send to {self!r}")
        await self.gateway._send(Message.CHANNEL_DATA, self.id, data)

    async def receive_bytes(self) -> bytes:
        """Receive the next payload.

        Raises EOFError once the peer closed or sent EOF and all payloads
        are drained; a peer close-with-error raises that ``RemoteError``.
        """
        try:
            return await self._payloads.receive()
        except (trio.EndOfChannel, trio.ClosedResourceError):
            raise self._pending_error() from None

    async def send_eof(self) -> None:
        """Signal that no more payloads follow (peer keeps its send side)."""
        if self._closed or self._sent_eof:
            raise OSError(f"cannot send EOF to {self!r}")
        self._sent_eof = True
        await self.gateway._send(Message.CHANNEL_LAST_MESSAGE, self.id)

    async def aclose(self, error: str | None = None) -> None:
        """Close both directions; ``error`` reaches the peer as a RemoteError."""
        if self._closed:
            await trio.lowlevel.checkpoint()
            return
        self._closed = True
        self._payload_send.close()
        self._receive_closed.set()
        self.gateway._forget_channel(self.id)
        if not self._remote_closed:
            # A peer-initiated close needs no reply; a dead gateway is
            # already as closed as it gets.
            with suppress(OSError):
                if error is not None:
                    await self.gateway._send(
                        Message.CHANNEL_CLOSE_ERROR, self.id, dumps_internal(error)
                    )
                else:
                    await self.gateway._send(Message.CHANNEL_CLOSE, self.id)

    def __aiter__(self) -> RawChannel:
        return self

    async def __anext__(self) -> bytes:
        try:
            return await self.receive_bytes()
        except EOFError:
            raise StopAsyncIteration from None

    def _pending_error(self) -> BaseException:
        return (
            self._remote_error
            or self.gateway._error
            or EOFError(f"raw channel {self.id} closed")
        )

    def set_consumer(
        self,
        on_payload: Callable[[bytes], None],
        on_close: Callable[[RemoteError | None, bool], None],
    ) -> None:
        """Divert inbound payloads and the close to callbacks (loop thread).

        Already-buffered payloads flush to ``on_payload`` first, and a close
        that arrived before binding is replayed to ``on_close`` -- so a
        consumer bound late (facade channels bind via a portal post) sees
        the exact inbound order.
        """
        self._consumer_payload = on_payload
        self._consumer_close = on_close
        while True:
            try:
                data = self._payloads.receive_nowait()
            except (trio.WouldBlock, trio.EndOfChannel, trio.ClosedResourceError):
                break
            on_payload(data)
        if self._pending_close is not None:
            error, sendonly = self._pending_close
            self._pending_close = None
            on_close(error, sendonly)
            if not sendonly:
                # the late binder has now claimed the buffered close
                self.gateway._forget_channel(self.id)

    # dispatch-loop internals (inline on the gateway's serve task)

    def _feed(self, data: bytes) -> None:
        if self._consumer_payload is not None:
            self._consumer_payload(data)
            return
        try:
            self._payload_send.send_nowait(data)
        except (trio.BrokenResourceError, trio.ClosedResourceError):
            pass  # locally closed: drop, like the sync channel

    def _close_from_remote(self, error: RemoteError | None, *, sendonly: bool) -> None:
        if error is not None:
            self._remote_error = error
        self._remote_closed = True
        self._receive_closed.set()
        if not sendonly:
            self._closed = True
            # Nothing more can arrive for this id -- the peer closed it, and
            # ids step by two per side and are never reused -- so the
            # registry has no routing left to do.  Dropping it here is what
            # keeps a long-lived gateway from accumulating one dead channel
            # per remote_exec; the object itself lives as long as whoever
            # holds it, buffered payloads and all.
            #
            # Unless nobody holds it: a channel the local side has never
            # asked for exists *only* in the registry, and a passed-channel
            # reference may still bind to it late.  That one has to find the
            # buffered payloads and this close, not a fresh empty channel
            # under the same id, so it stays until it is claimed.
            if self._consumer_close is not None or self._handed_out:
                self.gateway._forget_channel(self.id)
        self._payload_send.close()
        if self._consumer_close is not None:
            self._consumer_close(error, sendonly)
        else:
            self._pending_close = (error, sendonly)


class RawChannelStream:
    """``ByteStream`` over a :class:`RawChannel` -- the frame-native via tunnel.

    The gateway writer performs one ``send_all`` per frame, so every raw
    payload carries exactly one whole sub-protocol frame (the relay
    keeps that invariant in the other direction).  ``receive_some`` buffers
    payloads and honours ``max_bytes`` for the handshake read.
    """

    def __init__(self, raw: RawChannel) -> None:
        self._raw = raw
        self._buf = bytearray()
        self._eof = False

    async def send_all(self, data: bytes) -> None:
        await self._raw.send_bytes(data)

    async def receive_some(self, max_bytes: int | None = None) -> bytes:
        if not self._buf and not self._eof:
            try:
                self._buf += await self._raw.receive_bytes()
            except EOFError:
                self._eof = True
            except RemoteError as exc:
                self._eof = True
                raise EOFError(f"via tunnel closed: {exc}") from None
        if max_bytes is None:
            max_bytes = len(self._buf)
        out = bytes(self._buf[:max_bytes])
        del self._buf[:max_bytes]
        return out

    async def send_eof(self) -> None:
        with suppress(OSError):
            await self._raw.send_eof()

    async def aclose(self) -> None:
        await self._raw.aclose()


class AsyncChannel:
    """Serialized object API over a raw byte channel.

    Every payload is one dumps/loads-serialized item; close/EOF semantics
    and error propagation come from the raw layer.  Channels are
    async-iterable, and :meth:`receive` supports the familiar execnet
    timeout (raising ``TimeoutError``).

    Channel objects themselves serialize: sending an AsyncChannel inside an
    item transfers a reference the peer receives as its own AsyncChannel
    for the same id (the wire CHANNEL opcode, as with sync channels).
    """

    RemoteError = RemoteError
    TimeoutError = TimeoutError

    def __init__(self, raw: RawChannel) -> None:
        self._raw = raw
        self.gateway = raw.gateway
        self.id = raw.id

    def __repr__(self) -> str:
        flag = "closed" if self.isclosed() else "open"
        return f"<AsyncChannel id={self.id} {flag}>"

    def isclosed(self) -> bool:
        """Return True if the channel is closed for sending."""
        return self._raw._closed

    async def send(self, item: object) -> None:
        """Serialize ``item`` and send it to the other side.

        The item must be a simple Python type; OSError is raised when the
        channel or gateway is closed.
        """
        if self.isclosed():
            raise OSError(f"cannot send to {self!r}")
        await self._raw.send_bytes(dumps_internal(item))

    async def receive(self, timeout: float | None = None) -> Any:
        """Receive the next item sent from the other side.

        Raises EOFError once the peer closed or sent EOF, a RemoteError for
        a peer close-with-error, and TimeoutError if no item arrived within
        ``timeout`` seconds.
        """
        if timeout is None:
            data = await self._raw.receive_bytes()
        else:
            try:
                with trio.fail_after(timeout):
                    data = await self._raw.receive_bytes()
            except trio.TooSlowError:
                raise TimeoutError("no item after %r seconds" % timeout) from None
        return loads_internal(data, self)

    async def send_eof(self) -> None:
        """Signal that no more items follow (peer keeps its send side)."""
        await self._raw.send_eof()

    async def aclose(self, error: str | None = None) -> None:
        """Close the channel; ``error`` reaches the peer as a RemoteError."""
        await self._raw.aclose(error)

    async def wait_closed(self) -> None:
        """Wait until the peer closed or sent EOF; reraise remote errors."""
        await self._raw._receive_closed.wait()
        error = self._raw._remote_error or self.gateway._error
        if error is not None:
            raise error

    def __aiter__(self) -> AsyncChannel:
        return self

    async def __anext__(self) -> Any:
        try:
            return await self.receive()
        except EOFError:
            raise StopAsyncIteration from None

    # Unserializer duck-type: loads_internal(data, self) reads
    # _channelfactory off the object to resolve CHANNEL opcodes.

    @property
    def _channelfactory(self) -> _AsyncChannelFactory:
        return self.gateway._channelfactory


class _AsyncChannelFactory:
    """Duck-typed factory for the Unserializer CHANNEL opcode (``.new(id)``)."""

    def __init__(self, gateway: AsyncGateway) -> None:
        self.gateway = gateway

    def new(self, id: int) -> AsyncChannel:
        return self.gateway.open_channel(id)


class AsyncGateway:
    """Async-native gateway: the framed Message protocol over a ByteStream.

    Serving (``serve_gateway`` or ``nursery.start(gateway._serve)``) runs a
    reader task that dispatches messages inline and a writer task draining
    an unbounded outbound queue -- sends never block on the peer.

    ``_startcount`` follows the sync convention: locally allocated channel
    ids step by two, coordinators from 1 (odd) and workers from 2 (even),
    so the two peers never collide.
    """

    _error: BaseException | None = None
    #: where the peer lives, when the transport knows (ssh host, socket addr)
    remoteaddress: str | None = None
    #: CHANNEL_EXEC handler ``(gateway, channelid, data) -> None`` -- set by
    #: an exec strategy (the pure-async worker's TaskExec); without one,
    #: exec requests are rejected.
    _exec_handler: Callable[[AsyncGateway, int, bytes], None] | None = None
    #: the exec strategy (for STATUS numexecuting), when serving as a worker
    _task_exec: Any = None
    #: ``(async_fn, *args) -> None`` spawning a worker-side service task --
    #: set by whichever worker entry point owns a nursery.  Services are the
    #: protocol requests a worker serves itself (rsync today), as opposed to
    #: exec'd code; without one, such a request is rejected.
    _service_spawn: Callable[..., None] | None = None

    def __init__(self, stream: ByteStream, *, id: str, _startcount: int = 1) -> None:
        self._stream = stream
        self.id = id
        self._channels: dict[int, RawChannel] = {}
        self._async_channels: dict[int, AsyncChannel] = {}
        self._channelfactory = _AsyncChannelFactory(self)
        self._count = _startcount
        self._outbound_send, self._outbound = trio.open_memory_channel[
            tuple[bytes, Callable[[BaseException | None], None] | None]
        ](math.inf)
        self._closed = False
        self._serve_started = False
        self._writer_done = trio.Event()
        self._done = trio.Event()

    def __repr__(self) -> str:
        state = "closed" if self._closed else "open"
        return f"<AsyncGateway id={self.id!r} {state}>"

    @property
    def closed(self) -> bool:
        return self._closed

    async def wait_closed(self) -> None:
        """Wait until serving has fully shut down."""
        await self._done.wait()

    def _trace(self, *msg: object) -> None:
        trace(self.id, *msg)

    def open_raw_channel(self, id: int | None = None) -> RawChannel:
        """Return the raw channel for ``id``, allocating a fresh id if None.

        An explicit id attaches to a channel the peer references (e.g. an id
        received in a request payload); the same object is returned if the
        dispatch loop already routed data to it.
        """
        if self._closed:
            raise OSError(f"connection already closed: {self!r}")
        if id is None:
            id = self._count
            self._count += 2
        channel = self._channel_for(id)
        channel._handed_out = True
        return channel

    def open_channel(self, id: int | None = None) -> AsyncChannel:
        """Return the serialized channel for ``id``, allocating one if None."""
        raw = self.open_raw_channel(id)
        try:
            return self._async_channels[raw.id]
        except KeyError:
            channel = self._async_channels[raw.id] = AsyncChannel(raw)
            return channel

    async def remote_exec(
        self,
        source: str | types.FunctionType | Callable[..., object] | types.ModuleType,
        **kwargs: object,
    ) -> AsyncChannel:
        """Connect a new channel to remote execution of ``source``.

        Accepts the same source kinds as the sync ``Gateway.remote_exec``:
        a source string, a pure function called with ``channel`` and
        ``**kwargs``, or a module.  The remote end closes the channel when
        execution finishes.
        """
        source, file_name, call_name = normalize_exec_source(source, kwargs)
        channel = self.open_channel()
        await self._send(
            Message.CHANNEL_EXEC,
            channel.id,
            dumps_internal((source, file_name, call_name, kwargs)),
        )
        return channel

    async def terminate(self) -> None:
        """Send GATEWAY_TERMINATE to the peer, then close this side."""
        if not self._closed:
            with suppress(OSError):
                await self._send(Message.GATEWAY_TERMINATE)
        await self.aclose()

    async def aclose(self) -> None:
        """Flush queued frames, close the stream, and wait for shutdown."""
        if not self._closed:
            self._closed = True
            self._outbound_send.close()
            with trio.move_on_after(5):
                await self._writer_done.wait()
        with trio.CancelScope(shield=True), suppress(Exception):
            await self._stream.aclose()
        if self._serve_started:
            await self._done.wait()
        else:
            self._finish_channels()

    async def _serve(
        self, task_status: trio.TaskStatus[None] = trio.TASK_STATUS_IGNORED
    ) -> None:
        """Run the reader and writer until EOF, termination, or ``aclose``."""
        self._serve_started = True
        try:
            async with trio.open_nursery() as nursery:
                nursery.start_soon(self._writer)
                task_status.started()
                await self._reader()
                # Reader is done: let the writer flush queued frames
                # (close replies), then stop it.
                self._closed = True
                self._outbound_send.close()
                with trio.move_on_after(5):
                    await self._writer_done.wait()
                nursery.cancel_scope.cancel()
        finally:
            self._closed = True
            self._outbound_send.close()
            try:
                await self._finalize()
            finally:
                with trio.CancelScope(shield=True), suppress(Exception):
                    await self._stream.aclose()
                self._done.set()

    async def _finalize(self) -> None:
        """Serve-shutdown hook: release the channel layer.

        The sync facade overrides this to close its sync channels and shut
        down execution instead.
        """
        self._finish_channels()

    async def _reader(self) -> None:
        decoder = FrameDecoder()
        try:
            while True:
                try:
                    data = await self._stream.receive_some(RECEIVE_CHUNK)
                except trio.BrokenResourceError as exc:
                    # A peer that died abruptly *resets* a socket -- Windows
                    # reports WSAECONNRESET -- where a pipe would simply have
                    # reached EOF.  Same event, so report the same thing:
                    # callers test for EOFError, and an endmarker callback
                    # must fire either way.
                    if not self._closed:
                        error = EOFError(f"connection closed: {exc}")
                        error.__cause__ = exc
                        self._error = error
                    return
                except trio.ClosedResourceError as exc:
                    # we closed it; not the peer going away
                    if not self._closed:
                        self._error = exc
                    return
                if not data:
                    decoder.close()  # raises EOFError on a mid-frame EOF
                    raise EOFError("connection closed (no gateway termination)")
                for message in decoder.feed(data):
                    self._trace("received", message)
                    self._dispatch(message)
        except GatewayReceivedTerminate:
            self._trace("received GATEWAY_TERMINATE")
        except EOFError as exc:
            self._trace("EOF without prior gateway termination message")
            self._error = exc

    async def _writer(self) -> None:
        error: BaseException | None = None
        try:
            async for frame, on_written in self._outbound:
                try:
                    await self._stream.send_all(frame)
                except BaseException as exc:
                    error = exc
                    if on_written is not None:
                        on_written(exc)
                    raise
                if on_written is not None:
                    on_written(None)
        except (trio.BrokenResourceError, trio.ClosedResourceError, OSError) as exc:
            self._trace("writer failed", exc)
            if self._error is None:
                self._error = exc
        else:
            # Queue closed and drained: signal write-EOF to the peer.
            with suppress(Exception):
                await self._stream.send_eof()
        finally:
            # No more writes will happen: fail queued frames instead of
            # leaving their senders waiting on acknowledgements.
            self._outbound_send.close()
            self._fail_pending_writes(error)
            self._writer_done.set()

    def _fail_pending_writes(self, error: BaseException | None) -> None:
        exc = error if error is not None else OSError("cannot send (already closed?)")
        while True:
            try:
                _frame, on_written = self._outbound.receive_nowait()
            except (trio.WouldBlock, trio.EndOfChannel, trio.ClosedResourceError):
                return
            if on_written is not None:
                on_written(exc)

    def _dispatch(self, message: Message) -> None:
        """Route one message; runs inline on the serve task."""
        code = message.msgcode
        channelid = message.channelid
        if code == Message.CHANNEL_DATA:
            self._channel_for(channelid)._feed(message.data)
        elif code == Message.CHANNEL_CLOSE:
            self._channel_for(channelid)._close_from_remote(None, sendonly=False)
        elif code == Message.CHANNEL_CLOSE_ERROR:
            error_message = loads_internal(message.data)
            assert isinstance(error_message, str)
            self._channel_for(channelid)._close_from_remote(
                RemoteError(error_message), sendonly=False
            )
        elif code == Message.CHANNEL_LAST_MESSAGE:
            self._channel_for(channelid)._close_from_remote(None, sendonly=True)
        elif code == Message.GATEWAY_TERMINATE:
            raise GatewayReceivedTerminate(self)
        elif code == Message.CHANNEL_EXEC and self._exec_handler is not None:
            self._exec_handler(self, channelid, message.data)
        elif code == Message.GATEWAY_RSYNC and self._service_spawn is not None:
            from ._rsync_serve import serve_rsync_request

            self._service_spawn(serve_rsync_request, self, channelid, message.data)
        elif code == Message.STATUS:
            task_exec = self._task_exec
            status = {
                "numchannels": len(self._channels),
                "numexecuting": task_exec.active_count() if task_exec else 0,
                # tasks on the worker's own loop: no thread budget to run out
                # of, so nothing is refused for capacity here
                "execcapacity": None,
                "profile": "trio",
                # legacy key, same value -- pytest-xdist reads it
                "execmodel": "trio",
            }
            self._send_nowait(Message.CHANNEL_DATA, channelid, dumps_internal(status))
            self._send_nowait(Message.CHANNEL_CLOSE, channelid)
        elif code == Message.GATEWAY_INFO:
            self._send_nowait(
                Message.CHANNEL_DATA, channelid, dumps_internal(gateway_info())
            )
            self._send_nowait(Message.CHANNEL_CLOSE, channelid)
        else:
            # CHANNEL_EXEC / GATEWAY_START_*: not served by the async core
            self._trace("rejecting unsupported message", message)
            self._send_nowait(
                Message.CHANNEL_CLOSE_ERROR,
                channelid,
                dumps_internal(f"unsupported message on async gateway: {message!r}"),
            )

    def _channel_for(self, id: int) -> RawChannel:
        try:
            return self._channels[id]
        except KeyError:
            channel = self._channels[id] = RawChannel(self, id)
            return channel

    def _forget_channel(self, id: int) -> None:
        self._channels.pop(id, None)
        self._async_channels.pop(id, None)

    async def _send(self, msgcode: int, channelid: int = 0, data: bytes = b"") -> None:
        # The queue is unbounded, so this never waits on the peer -- but it
        # is a real checkpoint and raises once the gateway is closed.
        try:
            await self._outbound_send.send(
                (Message(msgcode, channelid, data).pack(), None)
            )
        except (trio.BrokenResourceError, trio.ClosedResourceError) as exc:
            raise OSError("cannot send (already closed?)") from exc

    def enqueue_frame(
        self,
        frame: bytes,
        on_written: Callable[[BaseException | None], None] | None = None,
    ) -> None:
        """Queue one wire frame (sync, loop thread only).

        ``on_written`` fires exactly once: after the frame reached the OS
        write, or with the failure when it never will.  Raises OSError when
        the outbound side is already closed.
        """
        try:
            self._outbound_send.send_nowait((frame, on_written))
        except (trio.BrokenResourceError, trio.ClosedResourceError) as exc:
            raise OSError("cannot send (already closed?)") from exc

    def _send_nowait(self, msgcode: int, channelid: int = 0, data: bytes = b"") -> None:
        """Enqueue a frame from a dispatch handler (sync, inline on the loop)."""
        with suppress(OSError):
            self.enqueue_frame(Message(msgcode, channelid, data).pack())

    def _finish_channels(self) -> None:
        for channel in list(self._channels.values()):
            channel._close_from_remote(None, sendonly=True)
        self._channels.clear()
        self._async_channels.clear()


@asynccontextmanager
async def serve_gateway(
    stream: ByteStream, *, id: str, _startcount: int = 1
) -> AsyncIterator[AsyncGateway]:
    """Serve an :class:`AsyncGateway` over ``stream`` for the ``with`` body."""
    gateway = AsyncGateway(stream, id=id, _startcount=_startcount)
    async with trio.open_nursery() as nursery:
        await nursery.start(gateway._serve)
        try:
            yield gateway
        finally:
            await gateway.aclose()


#: how long to wait for an ssh worker to dial back before giving up
SSH_CONNECT_TIMEOUT = 60.0


def _ssh_argv(
    spec: Any, command: str, forward: tuple[str, str] | None = None
) -> list[str]:
    """ssh/vagrant argv running ``command``, optionally with a ``-R`` forward.

    ``forward`` is ``(remote_socket, local_socket)``.  ``StreamLocalBindUnlink``
    makes sshd replace a stale socket file rather than refuse to bind.
    """
    from . import _provision

    options: list[str] = []
    if forward is not None:
        remote_sock, local_sock = forward
        options += ["-R", f"{remote_sock}:{local_sock}"]
        options += ["-o", "StreamLocalBindUnlink=yes"]
    if spec.ssh is not None:
        return _provision.ssh_argv(spec.ssh, spec.ssh_config, command, options)
    assert spec.vagrant_ssh is not None
    return _provision.vagrant_ssh_argv(
        spec.vagrant_ssh, spec.ssh_config, command, options
    )


def ssh_transport_args(spec: Any) -> list[str]:
    """ssh argv for a stdio-transport ssh worker."""
    from . import _provision

    assert spec.ssh is not None
    return _ssh_argv(spec, _provision.ssh_remote_command(spec))


def vagrant_transport_args(spec: Any) -> list[str]:
    """vagrant-ssh argv for a stdio-transport vagrant_ssh worker."""
    from . import _provision

    assert spec.vagrant_ssh is not None
    return _ssh_argv(spec, _provision.ssh_remote_command(spec))


@asynccontextmanager
async def _dialback_listener() -> AsyncIterator[tuple[str, trio.SocketListener]]:
    """A private unix socket the worker will be forwarded back to."""
    import shutil
    import tempfile

    directory = tempfile.mkdtemp(prefix="execnet-dialback-")
    os.chmod(directory, 0o700)
    path = os.path.join(directory, "gw.sock")
    sock = trio.socket.socket(trio.socket.AF_UNIX, trio.socket.SOCK_STREAM)
    await sock.bind(path)
    sock.listen(1)
    listener = trio.SocketListener(sock)
    try:
        yield path, listener
    finally:
        with trio.CancelScope(shield=True), suppress(Exception):
            await listener.aclose()
        shutil.rmtree(directory, ignore_errors=True)


async def _accept_or_diagnose(
    listener: trio.SocketListener,
    process: trio.Process,
    remoteaddress: str,
) -> trio.SocketStream:
    """Await the worker's dial-back, or explain why it never came.

    With the stdio transport a dead ssh shows up as EOF on the protocol
    pipe.  Here there is no such pipe, so the accept is raced against the
    process exiting -- ssh's 255 still means "could not reach or
    authenticate the host".
    """
    accepted: list[trio.SocketStream] = []
    exited: list[int] = []

    async def accept(nursery: trio.Nursery) -> None:
        accepted.append(await listener.accept())
        nursery.cancel_scope.cancel()

    async def watch(nursery: trio.Nursery) -> None:
        exited.append(await process.wait())
        nursery.cancel_scope.cancel()

    with trio.move_on_after(SSH_CONNECT_TIMEOUT):
        async with trio.open_nursery() as nursery:
            nursery.start_soon(accept, nursery)
            nursery.start_soon(watch, nursery)
    if accepted:
        return accepted[0]
    with trio.CancelScope(shield=True), trio.move_on_after(5):
        process.kill()
        await process.wait()
    if exited and exited[0] == 255:
        raise HostNotFound(remoteaddress)
    if exited:
        raise EOFError(
            f"ssh worker exited with {exited[0]} before connecting back"
            f" to {remoteaddress}"
        )
    raise EOFError(
        f"ssh worker did not connect back within {SSH_CONNECT_TIMEOUT}s"
        f" ({remoteaddress})"
    )


async def connect_ssh_worker(spec: Any) -> tuple[ByteStream, trio.Process]:
    """Spawn an ssh/vagrant worker on the transport ``spec`` resolves to.

    ``transport=socket`` forwards a private unix socket to the remote with
    ``ssh -R`` and lets the worker dial back on it, which leaves the
    worker's stdin, stdout and stderr free for the code it runs.  The
    config travels on the dialled-back connection like everywhere else.
    """
    from . import _provision

    remoteaddress = spec.ssh or spec.vagrant_ssh
    assert remoteaddress is not None
    wheel = await provision_sync(_provision.ssh_wheel, spec)
    if wheel is not None:
        await deliver_remote_wheel(spec, wheel)

    dialback = _provision.resolve_transport(
        spec, available=_provision.ssh_dialback_available()
    )
    if dialback == "stdio":
        args = await provision_sync(
            ssh_transport_args if spec.ssh is not None else vagrant_transport_args,
            spec,
        )
        return await connect_command_worker(args, spec, remoteaddress=remoteaddress)

    async with _dialback_listener() as (local_sock, listener):
        remote_sock = f"/tmp/execnet-{uuid.uuid4().hex}.sock"
        command = await provision_sync(
            _provision.ssh_remote_command,
            spec,
            "--protocol-connect",
            f"unix:{remote_sock}",
        )
        argv = _ssh_argv(spec, command, forward=(remote_sock, local_sock))
        # stdin closed, stdout/stderr inherited: the remote's stdio is the
        # user's now, and nothing of ours travels on it.
        process = await trio.lowlevel.open_process(argv, stdin=subprocess.DEVNULL)
        try:
            stream = await _accept_or_diagnose(listener, process, remoteaddress)
            await configure_worker(stream, spec, "ssh")
        except BaseException:
            with trio.CancelScope(shield=True), trio.move_on_after(5):
                process.kill()
                await process.wait()
            raise
    return stream, process


async def deliver_remote_wheel(spec: Any, wheel: Any) -> None:
    """Ship ``wheel`` to the remote over its own connection, before launching.

    Out of band on purpose: the protocol stream never carries a payload, so
    the launch command needs no ``head -c <N>`` byte accounting and no
    ``exec`` to keep an fd alive.  Skipped remote-side when the file is
    already cached there.
    """
    from . import _provision

    argv = _ssh_argv(spec, _provision.wheel_delivery_command(wheel))
    process = await trio.lowlevel.open_process(argv, stdin=subprocess.PIPE)
    try:
        assert process.stdin is not None
        await process.stdin.send_all(wheel.read_bytes())
        await process.stdin.aclose()
        code = await process.wait()
    except BaseException:
        with trio.CancelScope(shield=True), trio.move_on_after(5):
            process.kill()
            await process.wait()
        raise
    if code != 0:
        raise HostNotFound(
            f"could not deliver the execnet wheel to {spec.ssh or spec.vagrant_ssh}"
            f" (exit {code})"
        )


async def connect_command_worker(
    args: list[str],
    spec: Any = None,
    *,
    remoteaddress: str | None = None,
) -> tuple[ByteStream, trio.Process]:
    """Spawn ``args`` and configure the worker over its stdio.

    The stdio transport: the config frame and the protocol share the one
    stream, which is what a plain ``python -m execnet worker`` reads.  With
    a ``remoteaddress``, a handshake EOF plus exit code 255 (ssh could not
    reach or authenticate the host) becomes :class:`HostNotFound`.
    """
    process = await open_popen_process(args)
    try:
        stream = staple_process_stream(process)
        await configure_worker(stream, spec, "bootstrap")
    except BaseException as exc:
        host_not_found = False
        with trio.CancelScope(shield=True):
            if isinstance(exc, EOFError) and remoteaddress is not None:
                with trio.move_on_after(5):
                    host_not_found = await process.wait() == 255
            with trio.move_on_after(5):
                process.kill()
                await process.wait()
        if host_not_found:
            assert remoteaddress is not None
            raise HostNotFound(remoteaddress) from None
        raise
    return stream, process


#: config key carrying a ``socket.share()`` blob to a ``--protocol-share``
#: worker, base64 encoded because the config is JSON.
SHARE_KEY = "protocol_share"


def share_socket(sock: Any, pid: int) -> str:
    """``socket.share(pid)`` as a base64 string for the worker config."""
    import base64

    return base64.b64encode(sock.share(pid)).decode("ascii")


async def _spawn_with_socket(spec: Any, theirs: Any) -> trio.Process:
    """Spawn a worker owning ``theirs``, by whichever handoff this OS has.

    POSIX passes the fd itself.  Windows cannot -- ``subprocess`` refuses
    ``pass_fds`` there -- so the socket is duplicated into the child with
    ``WSADuplicateSocket``.  That needs the child's pid, so it can only
    happen once the child exists: the flag goes in argv, the blob follows on
    stdin.  It is the one thing that cannot travel in the config frame,
    since it describes the very connection that frame would arrive on.
    """
    from . import _provision

    if not _provision.socket_share_required():
        args = await provision_sync(
            popen_worker_argv, spec, "--protocol-fd", str(theirs.fileno())
        )
        return await trio.lowlevel.open_process(args, pass_fds=(theirs.fileno(),))

    args = await provision_sync(
        popen_worker_argv, spec, "--protocol-share", local_config_on_stdin=True
    )
    process = await trio.lowlevel.open_process(args, stdin=subprocess.PIPE)
    try:
        assert process.stdin is not None
        await process.stdin.send_all(
            dumps_config({SHARE_KEY: share_socket(theirs, process.pid)})
        )
        await process.stdin.aclose()
    except BaseException:
        with trio.CancelScope(shield=True), suppress(Exception):
            process.kill()
            await process.wait()
        raise
    return process


def dumps_config(config: dict[str, Any]) -> bytes:
    """A transport's local config as the bytes a ``--config-fd`` worker reads."""
    import json

    return json.dumps(config).encode("utf-8")


async def connect_popen_worker(spec: Any) -> tuple[ByteStream, trio.Process]:
    """Spawn a local worker for ``spec`` on its resolved transport.

    With ``transport=socket`` the protocol runs over an inherited
    socketpair, so the child's stdin/stdout/stderr stay the user's: remote
    ``print()`` reaches the terminal instead of being swallowed to keep the
    wire clean.  ``transport=stdio`` is the classic pipe pair.
    """
    from . import _provision

    transport = _provision.resolve_transport(
        spec, available=_provision.socket_handoff_available()
    )
    if transport == "stdio":
        return await connect_command_worker(
            await provision_sync(popen_worker_argv, spec), spec
        )

    import socket as _socket

    ours, theirs = _socket.socketpair()
    try:
        process = await _spawn_with_socket(spec, theirs)
    except BaseException:
        ours.close()
        theirs.close()
        raise
    # Our copy has to go now, whichever handoff was used: while we hold it,
    # a worker that dies before the handshake leaves the pair open and the
    # read below waits forever instead of failing.  (Holding it until the
    # handshake was tried, as a fix for a Windows share() race -- it fixed
    # nothing and bought exactly that hang.)
    theirs.close()
    stream = trio.SocketStream(trio.socket.from_stdlib_socket(ours))
    try:
        await configure_worker(stream, spec, "bootstrap")
    except BaseException as exc:
        with trio.CancelScope(shield=True):
            status: int | None = None
            with trio.move_on_after(5):
                process.kill()
                status = await process.wait()
            await stream.aclose()
            if status is not None:
                # what the worker did with itself is the whole diagnosis
                # when it never reached the handshake
                raise EOFError(
                    f"worker exited with {status} before the handshake: {exc}"
                ) from exc
        raise
    return stream, process


async def connect_socket_worker(
    address: tuple[str, int], remoteaddress: str, spec: Any = None
) -> ByteStream:
    """Connect to a running socketserver and configure the worker it spawns.

    The worker is spawned by the *server* and inherits this connection, so
    the config frame reaches it exactly as it would any other worker -- the
    server neither reads it nor needs to know what is in it.
    """
    try:
        stream = await trio.open_tcp_stream(*address)
    except OSError as exc:
        raise HostNotFound(remoteaddress) from exc
    try:
        await configure_worker(stream, spec, "socket")
    except BaseException:
        with trio.CancelScope(shield=True), trio.move_on_after(5):
            await stream.aclose()
        raise
    return stream


async def start_socketserver_via(
    gateway: AsyncGateway, bind_host: str = "localhost"
) -> tuple[str, int]:
    """Ask ``gateway`` (protocol message) to start a one-shot socket listener.

    Returns the ``(host, port)`` the coordinator should connect to.
    """
    channel = gateway.open_channel()
    await gateway._send(
        Message.GATEWAY_START_SOCKET, channel.id, dumps_internal(bind_host)
    )
    realhost, realport = await channel.receive()
    await channel.wait_closed()
    if not realhost or realhost in ("0.0.0.0", "::"):
        realhost = "localhost"
    return realhost, int(realport)


class AsyncGroup:
    """Trio-native group: an async context manager owning the gateway nursery.

    Gateways created with :meth:`makegateway` are served as child tasks of
    the group's nursery.  Leaving the ``async with`` block terminates every
    gateway with the safe_terminate contract: GATEWAY_TERMINATE plus a
    ``timeout`` grace, then kill -- bounded at roughly twice the timeout
    even when a kill gets stuck (see issues #43 / #221).
    """

    def __init__(self, termination_timeout: float = 10.0) -> None:
        self._termination_timeout = termination_timeout
        self._nursery: trio.Nursery | None = None
        self._gateways: list[AsyncGateway] = []
        self._processes: dict[AsyncGateway, trio.Process] = {}
        # Monotonic, not len(self._gateways): terminate() empties that list,
        # and an id that comes round again names two different workers in one
        # session's traces (and in whatever the caller keyed on it).
        self._idcount = 0

    def __repr__(self) -> str:
        ids = [gateway.id for gateway in self._gateways]
        return f"<AsyncGroup {ids}>"

    async def __aenter__(self) -> Self:
        self._nursery_manager = trio.open_nursery()
        self._nursery = await self._nursery_manager.__aenter__()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: types.TracebackType | None,
    ) -> bool | None:
        # The serve tasks only end once their gateways shut down, so
        # terminate before letting the nursery join its children.
        # Shielded: cleanup stays bounded even under cancellation.
        terminate_error: BaseException | None = None
        try:
            with trio.CancelScope(shield=True):
                await self.terminate(self._termination_timeout)
        except BaseException as error:
            terminate_error = error
        self._nursery = None
        suppress_body_exc = await self._nursery_manager.__aexit__(
            exc_type, exc_value, traceback
        )
        if terminate_error is not None:
            raise terminate_error
        return suppress_body_exc

    async def makegateway(self, spec: str | Any = "popen") -> AsyncGateway:
        """Create a gateway for ``spec`` served on the group's nursery.

        All transport types are supported: popen (including uv-provisioned
        ``python=``), ``ssh=``, ``vagrant_ssh=``, ``socket=`` (with
        ``installvia=``), and ``via=`` sub-gateways relayed through a group
        member.
        """
        from ._xspec import XSpec

        if self._nursery is None:
            raise RuntimeError(f"{self!r} is not entered")
        if not isinstance(spec, XSpec):
            spec = XSpec(spec)
        if spec.profile is None:
            # An async coordinator does not imply an async worker: the
            # worker's shape is its own choice, and the default stays the
            # thread profile.  Pass ``profile=trio`` for a worker that runs
            # exec'd async sources as tasks.
            spec.profile = "thread"
        else:
            resolve_profile(spec.profile)
        if spec.id is None:
            spec.id = "gw%d" % self._idcount
            self._idcount += 1
        process: trio.Process | None = None
        remoteaddress: str | None = None
        if spec.via:
            stream: ByteStream = await self._open_via_stream(spec)
            remote = spec.ssh or spec.vagrant_ssh
            if remote:
                remoteaddress = f"{remote}[via {spec.via}]"
        elif spec.socket:
            address, remoteaddress = await self._resolve_socket_address(spec)
            stream = await connect_socket_worker(address, remoteaddress, spec)
        elif spec.ssh or spec.vagrant_ssh:
            remoteaddress = spec.ssh or spec.vagrant_ssh
            stream, process = await connect_ssh_worker(spec)
        elif spec.popen or spec.python:
            stream, process = await connect_popen_worker(spec)
        else:
            raise ValueError(f"unsupported spec for AsyncGroup: {spec!r}")
        # From here to the registration below, the worker is running but
        # nothing owns it yet: a failure (a cancel, most likely -- there is
        # not much else) would leave a process the group never terminates.
        # The connect helpers each clean up after themselves the same way;
        # this is the seam between them and the group.
        try:
            gateway = self._make_gateway(stream, spec)
            gateway.remoteaddress = remoteaddress
            await self._nursery.start(gateway._serve)
        except BaseException:
            with trio.CancelScope(shield=True):
                await self._abandon(stream, process)
            raise
        self._gateways.append(gateway)
        if process is not None:
            self._processes[gateway] = process
            self._nursery.start_soon(self._reap_process, process)
        return gateway

    async def _abandon(self, stream: ByteStream, process: trio.Process | None) -> None:
        """Drop a worker nobody took ownership of (best effort, bounded)."""
        with suppress(Exception):
            await stream.aclose()
        if process is not None:
            with trio.move_on_after(5), suppress(Exception):
                process.kill()
                await process.wait()

    def _make_gateway(self, stream: ByteStream, spec: Any) -> AsyncGateway:
        """Construct the gateway object for a freshly connected stream.

        Overridden by the sync facade to build bridge gateways instead.
        """
        return AsyncGateway(stream, id=spec.id, _startcount=1)

    async def _reap_process(self, process: trio.Process) -> None:
        # Prompt reaping for workers that exit on their own (no zombies).
        with suppress(Exception):
            await process.wait()

    async def _resolve_socket_address(self, spec: Any) -> tuple[tuple[str, int], str]:
        """``((host, port), remoteaddress)`` for a ``socket=`` spec.

        ``installvia=`` asks that group member to start a one-shot
        socketserver first.  The sync facade overrides this to talk to its
        sync coordinator gateway.
        """
        if getattr(spec, "installvia", None):
            coordinator = self._gateway_by_id(spec.installvia)
            realhost, realport = await start_socketserver_via(coordinator)
            return (realhost, realport), "%s:%d" % (realhost, realport)
        assert spec.socket is not None
        host_str, _, port_str = spec.socket.rpartition(":")
        return (host_str, int(port_str)), spec.socket

    async def _open_via_stream(self, spec: Any) -> ByteStream:
        """Ask the ``spec.via`` coordinator to spawn a sub-worker; tunnel over a
        raw channel (each payload one whole sub-protocol frame)."""
        from . import _provision

        coordinator = self._gateway_by_id(spec.via)
        raw = coordinator.open_raw_channel()
        request = await provision_sync(_provision.spawn_request, spec)
        await coordinator._send(
            Message.GATEWAY_START_SUB, raw.id, dumps_internal(request)
        )
        stream = RawChannelStream(raw)
        # the sub's config goes down the tunnel, so the coordinator relaying
        # it never sees this spec's env: values
        await configure_worker(stream, spec, "via")
        return stream

    def _gateway_by_id(self, id: str) -> AsyncGateway:
        for gateway in self._gateways:
            if gateway.id == id:
                return gateway
        raise KeyError(f"no gateway {id!r} in {self!r}")

    async def terminate(self, timeout: float | None = None) -> None:
        """Terminate all gateways; never hangs (kill after ``timeout``).

        Tunneled (``via``) gateways go first so their termination frames
        still travel through a live coordinator.
        """
        gateways = list(self._gateways)
        self._gateways.clear()
        tunneled = [gw for gw in gateways if gw not in self._processes]
        spawned = [gw for gw in gateways if gw in self._processes]
        for batch in (tunneled, spawned):
            if not batch:
                continue
            async with trio.open_nursery() as nursery:
                for gateway in batch:
                    nursery.start_soon(self._terminate_one, gateway, timeout)

    async def _terminate_one(
        self, gateway: AsyncGateway, timeout: float | None
    ) -> None:
        grace = math.inf if timeout is None else timeout
        await gateway.terminate()
        process = self._processes.pop(gateway, None)
        if process is None:
            return
        with trio.move_on_after(grace):
            await process.wait()
        if process.returncode is None:
            process.kill()
            with trio.move_on_after(grace):
                await process.wait()


@asynccontextmanager
async def open_gateway(spec: str | Any = "popen") -> AsyncIterator[AsyncGateway]:
    """Spawn one worker for ``spec`` and serve an AsyncGateway to it.

    Runs inside the caller's own trio run -- no host thread involved.
    Convenience for a single-gateway :class:`AsyncGroup`.
    """
    async with AsyncGroup() as group:
        yield await group.makegateway(spec)
