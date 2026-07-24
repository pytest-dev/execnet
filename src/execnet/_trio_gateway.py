"""Trio-native gateway core: async dispatch loop and low-level raw channels.

Async-first counterpart of the sync machinery in ``gateway_base``: an
:class:`AsyncGateway` owns a :class:`ByteStream` and runs a single dispatch
task (stream -> ``FrameDecoder`` -> route).  Message handlers execute inline
on that task, so there is no receiver thread and no receive lock.

Two-level channel model:

* :class:`RawChannel` (this module) -- id-routed raw byte payload streams
  over the gateway: no serialization, no strconfig, no callbacks.
  ``CHANNEL_DATA`` payloads route to the channel verbatim; the layer on top
  decides what the bytes mean.
* ``AsyncChannel`` -- the serialized object API layered on a RawChannel.

The code deliberately sticks to idioms an anyio backend can mirror later:
a neutral ``ByteStream`` protocol, the sans-IO ``FrameDecoder``, and
unbounded memory channels.
"""

from __future__ import annotations

import math
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from contextlib import suppress
from typing import Any
from typing import Protocol

import trio

from .gateway_base import FrameDecoder
from .gateway_base import GatewayReceivedTerminate
from .gateway_base import Message
from .gateway_base import RemoteError
from .gateway_base import TimeoutError
from .gateway_base import Unserializer
from .gateway_base import dumps_internal
from .gateway_base import loads_internal
from .gateway_base import trace

RECEIVE_CHUNK = 65536


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

    _strconfig: tuple[bool, bool] | None = None

    def __init__(self, gateway: AsyncGateway, id: int) -> None:
        self.gateway = gateway
        self.id = id
        self._closed = False  # no more sends (local aclose or remote close)
        self._sent_eof = False
        self._remote_closed = False
        self._receive_closed = trio.Event()  # no more payloads will arrive
        self._remote_error: RemoteError | None = None
        self._payload_send, self._payloads = trio.open_memory_channel[bytes](math.inf)

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

    # dispatch-loop internals (inline on the gateway's serve task)

    def _feed(self, data: bytes) -> None:
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
            self.gateway._forget_channel(self.id)
        self._payload_send.close()


class AsyncChannel:
    """Serialized object API over a :class:`RawChannel`.

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

    async def reconfigure(
        self, py2str_as_py3str: bool = True, py3str_as_py2str: bool = False
    ) -> None:
        """Set the string coercion for both ends of this channel."""
        strconfig = (py2str_as_py3str, py3str_as_py2str)
        self._raw._strconfig = strconfig
        await self.gateway._send(
            Message.RECONFIGURE, self.id, dumps_internal(strconfig)
        )

    def __aiter__(self) -> AsyncChannel:
        return self

    async def __anext__(self) -> Any:
        try:
            return await self.receive()
        except EOFError:
            raise StopAsyncIteration from None

    # Unserializer duck-type: loads_internal(data, self) reads _strconfig
    # and _channelfactory off the object to resolve CHANNEL opcodes.

    @property
    def _strconfig(self) -> tuple[bool, bool]:
        return self._raw._strconfig or self.gateway._strconfig

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

    def __init__(self, stream: ByteStream, *, id: str, _startcount: int = 1) -> None:
        self._stream = stream
        self.id = id
        self._channels: dict[int, RawChannel] = {}
        self._async_channels: dict[int, AsyncChannel] = {}
        self._channelfactory = _AsyncChannelFactory(self)
        self._count = _startcount
        self._strconfig = (Unserializer.py2str_as_py3str, Unserializer.py3str_as_py2str)
        self._outbound_send, self._outbound = trio.open_memory_channel[bytes](math.inf)
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
        return self._channel_for(id)

    def open_channel(self, id: int | None = None) -> AsyncChannel:
        """Return the serialized channel for ``id``, allocating one if None."""
        raw = self.open_raw_channel(id)
        try:
            return self._async_channels[raw.id]
        except KeyError:
            channel = self._async_channels[raw.id] = AsyncChannel(raw)
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
            self._finish_channels()
            with trio.CancelScope(shield=True), suppress(Exception):
                await self._stream.aclose()
            self._done.set()

    async def _reader(self) -> None:
        decoder = FrameDecoder()
        try:
            while True:
                try:
                    data = await self._stream.receive_some(RECEIVE_CHUNK)
                except (trio.BrokenResourceError, trio.ClosedResourceError) as exc:
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
        try:
            async for frame in self._outbound:
                await self._stream.send_all(frame)
        except (trio.BrokenResourceError, trio.ClosedResourceError) as exc:
            self._trace("writer failed", exc)
            if self._error is None:
                self._error = exc
            self._outbound_send.close()  # fail future sends fast
        else:
            # Queue closed and drained: signal write-EOF to the peer.
            with suppress(Exception):
                await self._stream.send_eof()
        finally:
            self._writer_done.set()

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
        elif code == Message.STATUS:
            status = {
                "numchannels": len(self._channels),
                "numexecuting": 0,
                "execmodel": "trio",
            }
            self._send_nowait(Message.CHANNEL_DATA, channelid, dumps_internal(status))
            self._send_nowait(Message.CHANNEL_CLOSE, channelid)
        elif code == Message.RECONFIGURE:
            data = loads_internal(message.data)
            assert isinstance(data, tuple)
            if channelid == 0:
                self._strconfig = data
            else:
                # picked up by the serialized channel layer
                self._channel_for(channelid)._strconfig = data
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
            await self._outbound_send.send(Message(msgcode, channelid, data).pack())
        except (trio.BrokenResourceError, trio.ClosedResourceError) as exc:
            raise OSError("cannot send (already closed?)") from exc

    def _send_nowait(self, msgcode: int, channelid: int = 0, data: bytes = b"") -> None:
        """Enqueue a frame from a dispatch handler (sync, inline on the loop)."""
        with suppress(trio.BrokenResourceError, trio.ClosedResourceError):
            self._outbound_send.send_nowait(Message(msgcode, channelid, data).pack())

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
