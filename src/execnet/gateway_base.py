"""Core gateway, channel and serialization code shared by coordinator and worker.

:copyright: 2004-2015
:authors:
    - Holger Krekel
    - Armin Rigo
    - Benjamin Peterson
    - Ronny Pfannschmidt
    - many others
"""

from __future__ import annotations

import builtins
import os
import queue as _queue
import struct
import sys
import threading
import traceback
import weakref
from _thread import interrupt_main
from collections.abc import Callable
from collections.abc import Iterator
from contextlib import suppress
from io import BytesIO
from typing import Any
from typing import Literal
from typing import Protocol
from typing import cast
from typing import overload

from ._boundary import Flag
from ._boundary import Mailbox
from ._boundary import Wakener
from ._boundary import make_wakener


class WriteIO(Protocol):
    def write(self, data: bytes, /) -> None: ...


class ReadIO(Protocol):
    def read(self, numbytes: int, /) -> bytes: ...


class IO(Protocol):
    execmodel: ExecModel

    def read(self, numbytes: int, /) -> bytes: ...

    def write(self, data: bytes, /) -> None: ...

    def close_read(self) -> None: ...

    def close_write(self) -> None: ...

    def wait(self) -> int | None: ...

    def kill(self) -> None: ...


class ExecModel:
    """Deprecated preset name for an execution model.

    The machinery behind execution models was retired: protocol IO always
    runs on the Trio host and blocking waits go through the boundary kit's
    wakeners (``execnet._boundary``); the name maps onto the worker config
    axes (``loop=`` / ``exec=`` / ``wait=``).  The stdlib-delegating
    members stay for API compatibility (pytest-xdist builds its test queue
    on ``execmodel.RLock``/``Event``) -- every preset is thread-shaped.
    """

    def __init__(self, backend: str) -> None:
        self.backend = backend

    def __repr__(self) -> str:
        return "<ExecModel %r>" % self.backend

    @property
    def queue(self):
        import queue

        return queue

    @property
    def subprocess(self):
        import subprocess

        return subprocess

    @property
    def socket(self):
        import socket

        return socket

    def get_ident(self) -> int:
        import _thread

        return _thread.get_ident()

    def sleep(self, delay: float) -> None:
        import time

        time.sleep(delay)

    def start(self, func, args=()) -> None:
        import _thread

        _thread.start_new_thread(func, args)

    def fdopen(self, fd, mode, bufsize=1, closefd=True):
        return os.fdopen(fd, mode, bufsize, encoding="utf-8", closefd=closefd)

    def Lock(self):
        return threading.RLock()

    def RLock(self):
        return threading.RLock()

    def Event(self) -> threading.Event:
        return threading.Event()


def get_execmodel(backend: str | ExecModel) -> ExecModel:
    if isinstance(backend, ExecModel):
        return backend
    if backend in ("thread", "main_thread_only"):
        return ExecModel(backend)
    raise ValueError(f"unknown execmodel {backend!r}")


sysex = (KeyboardInterrupt, SystemExit)


DEBUG = os.environ.get("EXECNET_DEBUG")
pid = os.getpid()
if DEBUG == "2":

    def trace(*msg: object) -> None:
        try:
            line = " ".join(map(str, msg))
            sys.stderr.write(f"[{pid}] {line}\n")
            sys.stderr.flush()
        except Exception:
            pass  # nothing we can do, likely interpreter-shutdown

elif DEBUG:
    import os
    import tempfile

    fn = os.path.join(tempfile.gettempdir(), "execnet-debug-%d" % pid)
    # sys.stderr.write("execnet-debug at %r" % (fn,))
    debugfile = open(fn, "w")

    def trace(*msg: object) -> None:
        try:
            line = " ".join(map(str, msg))
            debugfile.write(line + "\n")
            debugfile.flush()
        except Exception as exc:
            try:
                sys.stderr.write(f"[{pid}] exception during tracing: {exc!r}\n")
            except Exception:
                pass  # nothing we can do, likely interpreter-shutdown

else:
    notrace = trace = lambda *msg: None


class Message:
    """Encapsulates Messages and their wire protocol.

    Dispatch lives in the async core and the sync bridge session
    (``AsyncGateway._dispatch`` / ``SyncBridgeGateway._dispatch``); this
    class only carries the framing and the code constants.
    """

    STATUS = 0
    RECONFIGURE = 1
    GATEWAY_TERMINATE = 2
    CHANNEL_EXEC = 3
    CHANNEL_DATA = 4
    CHANNEL_CLOSE = 5
    CHANNEL_CLOSE_ERROR = 6
    CHANNEL_LAST_MESSAGE = 7
    GATEWAY_START_SOCKET = 8
    GATEWAY_START_SUB = 9

    # message code -> name
    _types: dict[int, str] = {
        STATUS: "STATUS",
        RECONFIGURE: "RECONFIGURE",
        GATEWAY_TERMINATE: "GATEWAY_TERMINATE",
        CHANNEL_EXEC: "CHANNEL_EXEC",
        CHANNEL_DATA: "CHANNEL_DATA",
        CHANNEL_CLOSE: "CHANNEL_CLOSE",
        CHANNEL_CLOSE_ERROR: "CHANNEL_CLOSE_ERROR",
        CHANNEL_LAST_MESSAGE: "CHANNEL_LAST_MESSAGE",
        GATEWAY_START_SOCKET: "GATEWAY_START_SOCKET",
        GATEWAY_START_SUB: "GATEWAY_START_SUB",
    }

    def __init__(self, msgcode: int, channelid: int = 0, data: bytes = b"") -> None:
        self.msgcode = msgcode
        self.channelid = channelid
        self.data = data

    def pack(self) -> bytes:
        """Return the full wire frame (9-byte header + payload)."""
        header = struct.pack("!bii", self.msgcode, self.channelid, len(self.data))
        return header + self.data

    @staticmethod
    def from_header(header: bytes) -> tuple[int, int, int]:
        """Unpack a 9-byte header into (msgtype, channelid, payload_len)."""
        if len(header) != 9:
            raise EOFError("couldn't load message header, short read")
        msgtype, channel, payload = struct.unpack("!bii", header)
        return msgtype, channel, payload

    @staticmethod
    def from_parts(msgtype: int, channel: int, data: bytes) -> Message:
        return Message(msgtype, channel, data)

    @staticmethod
    def from_io(io: ReadIO) -> Message:
        try:
            header = io.read(9)  # type 1, channel 4, payload 4
            if not header:
                raise EOFError("empty read")
        except EOFError as e:
            raise EOFError("couldn't load message header, " + e.args[0]) from None
        msgtype, channel, payload = Message.from_header(header)
        return Message(msgtype, channel, io.read(payload))

    def to_io(self, io: WriteIO) -> None:
        io.write(self.pack())

    def __repr__(self) -> str:
        name = self._types[self.msgcode]
        return f"<Message {name} channel={self.channelid} lendata={len(self.data)}>"


class FrameDecoder:
    """Incremental decoder for the 9-byte-header Message framing.

    ``feed(data)`` accepts arbitrary byte chunks and yields every complete
    Message; partial frames buffer internally until more bytes arrive.
    Pure computation — no IO, no awaits, no knowledge of streams — so
    receivers only ever stream bytes in (``receive_some`` loops) and the
    decoder owns framing.
    """

    def __init__(self) -> None:
        self._buffer = bytearray()

    def feed(self, data: bytes) -> Iterator[Message]:
        self._buffer += data
        return self._parse()

    def _parse(self) -> Iterator[Message]:
        while len(self._buffer) >= 9:
            msgtype, channelid, payload_len = Message.from_header(
                bytes(self._buffer[:9])
            )
            if len(self._buffer) < 9 + payload_len:
                return
            payload = bytes(self._buffer[9 : 9 + payload_len])
            del self._buffer[: 9 + payload_len]
            yield Message(msgtype, channelid, payload)

    def close(self) -> None:
        """Signal EOF; raises EOFError if the stream ended mid-frame."""
        if self._buffer:
            raise EOFError(
                "connection closed mid-frame (%d buffered bytes)" % len(self._buffer)
            )


class GatewayReceivedTerminate(Exception):
    """Receiver got a gateway termination message."""


class HostNotFound(ConnectionError):
    """The remote side of a gateway could not be reached."""


def geterrortext(
    exc: BaseException,
    format_exception=traceback.format_exception,
    sysex: tuple[type[BaseException], ...] = sysex,
) -> str:
    try:
        # In py310, can change this to:
        # l = format_exception(exc)
        l = format_exception(type(exc), exc, exc.__traceback__)
        errortext = "".join(l)
    except sysex:
        raise
    except BaseException:
        errortext = f"{type(exc).__name__}: {exc}"
    return errortext


class RemoteError(Exception):
    """Exception containing a stringified error from the other side."""

    def __init__(self, formatted: str) -> None:
        super().__init__()
        self.formatted = formatted

    def __str__(self) -> str:
        return self.formatted

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}: {self.formatted}"

    def warn(self) -> None:
        if self.formatted != INTERRUPT_TEXT:
            # XXX do this better
            sys.stderr.write(f"[{os.getpid()}] Warning: unhandled {self!r}\n")


class TimeoutError(IOError):
    """Exception indicating that a timeout was reached."""


NO_ENDMARKER_WANTED = object()


class Channel:
    """Communication channel between two Python Interpreter execution points.

    A facade over the async core: the gateway's Trio session diverts
    inbound payloads for this id into a :class:`Mailbox` (or a registered
    callback, invoked on the loop thread); ``receive()`` deserializes at
    the call site.  Sends go through ``gateway._send``.
    """

    RemoteError = RemoteError
    TimeoutError = TimeoutError
    _INTERNALWAKEUP = 1000
    _executing = False

    def __init__(self, gateway: BaseGateway, id: int) -> None:
        """:private:"""
        assert isinstance(id, int)
        assert not isinstance(gateway, type)
        self.gateway = gateway
        # XXX: defaults copied from Unserializer
        self._strconfig = getattr(gateway, "_strconfig", (True, False))
        self.id = id
        # serialized payloads (or ENDMARKER); None once a callback is set
        self._mailbox: Mailbox[Any] | None = Mailbox(gateway._new_wakener())
        self._callback: Callable[[Any], Any] | None = None
        self._endmarker: object = NO_ENDMARKER_WANTED
        self._closed = False
        self._receiveclosed = Flag(gateway._new_wakener())
        self._remoteerrors: list[RemoteError] = []

    def _trace(self, *msg: object) -> None:
        self.gateway._trace(self.id, *msg)

    def setcallback(
        self,
        callback: Callable[[Any], Any],
        endmarker: object = NO_ENDMARKER_WANTED,
    ) -> None:
        """Set a callback function for receiving items.

        All already-queued items will immediately trigger the callback.
        Afterwards the callback will execute in the receiver (loop) thread
        for each received data item and calls to ``receive()`` will
        raise an error.
        If an endmarker is specified the callback will eventually
        be called with the endmarker when the channel closes.
        """

        def switch() -> None:
            # Runs on the loop thread (inline without a session), so the
            # switch-over cannot interleave with payload delivery.
            mailbox = self._mailbox
            if mailbox is None:
                raise OSError(f"{self!r} has callback already registered")
            self._mailbox = None
            while 1:
                try:
                    olditem = mailbox.get_nowait()
                except _queue.Empty:
                    if not (self._closed or self._receiveclosed.is_set()):
                        self._callback = callback
                        self._endmarker = endmarker
                        self.gateway._channelfactory._register_callback_channel(self)
                    break
                if olditem is ENDMARKER:
                    if endmarker is not NO_ENDMARKER_WANTED:
                        callback(endmarker)
                    break
                callback(loads_internal(olditem, self))

        self.gateway._run_on_loop(switch)

    def __repr__(self) -> str:
        flag = (self.isclosed() and "closed") or "open"
        return "<Channel id=%d %s>" % (self.id, flag)

    def __del__(self) -> None:
        if self.gateway is None:  # can be None in tests
            return  # type: ignore[unreachable]

        self._trace("channel.__del__")
        # no multithreading issues here, because we have the last ref to 'self'
        if self._closed:
            # state transition "closed" --> "deleted"
            for error in self._remoteerrors:
                error.warn()
        elif self._receiveclosed.is_set():
            # state transition "sendonly" --> "deleted"
            # the remote channel is already in "deleted" state, nothing to do
            pass
        else:
            # state transition "opened" --> "deleted"
            # check if we are in the middle of interpreter shutdown
            # in which case the process will go away and we probably
            # don't need to try to send a closing or last message
            # (and often it won't work anymore to send things out)
            if Message is not None:
                if self._mailbox is None:  # has_callback
                    msgcode = Message.CHANNEL_LAST_MESSAGE
                else:
                    msgcode = Message.CHANNEL_CLOSE
                with suppress(OSError, ValueError):  # ignore problems with sending
                    # Never wait during GC: post the close best-effort.
                    send = getattr(
                        self.gateway, "_send_nonblocking", self.gateway._send
                    )
                    send(msgcode, self.id)
        with suppress(Exception):
            self.gateway._release_channel(self.id)

    def _getremoteerror(self):
        try:
            return self._remoteerrors.pop(0)
        except IndexError:
            try:
                return self.gateway._error
            except AttributeError:
                pass
            return None

    #
    # loop-side delivery (called by the session's raw-channel consumer)
    #
    def _deliver_payload(self, data: bytes) -> None:
        """Route one inbound serialized payload (loop thread)."""
        if self._closed:
            return  # late data for a locally closed channel: drop
        callback = self._callback
        if callback is None:
            mailbox = self._mailbox
            if mailbox is not None:
                mailbox.put(data)
            # no mailbox and no callback: closed for receiving -- drop
        else:
            try:
                callback(loads_internal(data, self))
            except Exception as exc:
                self.gateway._trace("exception during callback: %s" % exc)
                errortext = self.gateway._geterrortext(exc)
                self.gateway._send(
                    Message.CHANNEL_CLOSE_ERROR, self.id, dumps_internal(errortext)
                )
                self._close_from_remote(RemoteError(errortext))

    def _close_from_remote(self, remoteerror=None, *, sendonly: bool = False) -> None:
        """Close initiated by the peer or session shutdown (loop thread)."""
        if remoteerror:
            self._remoteerrors.append(remoteerror)
        mailbox = self._mailbox
        if mailbox is not None:
            mailbox.put(ENDMARKER)
        self._fire_endmarker()
        self.gateway._channelfactory._no_longer_opened(self.id)
        if not sendonly:  # otherwise #--> "sendonly"
            self._closed = True  # --> "closed"
        self._receiveclosed.set()

    def _fire_endmarker(self) -> None:
        callback = self._callback
        if callback is not None:
            self._callback = None
            if self._endmarker is not NO_ENDMARKER_WANTED:
                callback(self._endmarker)

    #
    # public API for channel objects
    #
    def isclosed(self) -> bool:
        """Return True if the channel is closed.

        A closed channel may still hold items.
        """
        return self._closed

    @overload
    def makefile(self, mode: Literal["r"], proxyclose: bool = ...) -> ChannelFileRead:
        pass

    @overload
    def makefile(
        self,
        mode: Literal["w"] = ...,
        proxyclose: bool = ...,
    ) -> ChannelFileWrite:
        pass

    def makefile(
        self,
        mode: Literal["r", "w"] = "w",
        proxyclose: bool = False,
    ) -> ChannelFileWrite | ChannelFileRead:
        """Return a file-like object.

        mode can be 'w' or 'r' for writeable/readable files.
        If proxyclose is true, file.close() will also close the channel.
        """
        if mode == "w":
            return ChannelFileWrite(channel=self, proxyclose=proxyclose)
        elif mode == "r":
            return ChannelFileRead(channel=self, proxyclose=proxyclose)
        raise ValueError(f"mode {mode!r} not available")

    def close(self, error=None) -> None:
        """Close down this channel with an optional error message.

        Note that closing of a channel tied to remote_exec happens
        automatically at the end of execution and cannot
        be done explicitly.
        """
        if self._executing:
            raise OSError("cannot explicitly close channel within remote_exec")
        if self._closed:
            self.gateway._trace(self, "ignoring redundant call to close()")
        if not self._closed:
            # state transition "opened/sendonly" --> "closed"
            # threads warning: the channel might be closed under our feet,
            # but it's never damaging to send too many CHANNEL_CLOSE messages
            # however, if the other side triggered a close already, we
            # do not send back a closed message.
            if not self._receiveclosed.is_set():
                put = self.gateway._send
                if error is not None:
                    put(Message.CHANNEL_CLOSE_ERROR, self.id, dumps_internal(error))
                else:
                    put(Message.CHANNEL_CLOSE, self.id)
                self._trace("sent channel close message")
            if isinstance(error, RemoteError):
                self._remoteerrors.append(error)
            self._closed = True  # --> "closed"
            self._receiveclosed.set()
            mailbox = self._mailbox
            if mailbox is not None:
                mailbox.put(ENDMARKER)
            self._fire_endmarker()
            self.gateway._channelfactory._no_longer_opened(self.id)
            self.gateway._release_channel(self.id)

    def waitclose(self, timeout: float | None = None) -> None:
        """Wait until this channel is closed (or the remote side
        otherwise signalled that no more data was being sent).

        The channel may still hold receiveable items, but not receive
        any more after waitclose() has returned.

        Exceptions from executing code on the other side are reraised as local
        channel.RemoteErrors.

        EOFError is raised if the reading-connection was prematurely closed,
        which often indicates a dying process.

        self.TimeoutError is raised after the specified number of seconds
        (default is None, i.e. wait indefinitely).
        """
        # wait for non-"opened" state
        self._receiveclosed.wait(timeout=timeout)
        if not self._receiveclosed.is_set():
            raise self.TimeoutError("Timeout after %r seconds" % timeout)
        error = self._getremoteerror()
        if error:
            raise error

    def send(self, item: object) -> None:
        """Sends the given item to the other side of the channel,
        possibly blocking if the sender queue is full.

        The item must be a simple Python type and will be
        copied to the other side by value.

        OSError is raised if the write pipe was prematurely closed.
        """
        if self.isclosed():
            raise OSError(f"cannot send to {self!r}")
        self.gateway._send(Message.CHANNEL_DATA, self.id, dumps_internal(item))

    def receive(self, timeout: float | None = None) -> Any:
        """Receive a data item that was sent from the other side.

        timeout: None [default] blocked waiting. A positive number
        indicates the number of seconds after which a channel.TimeoutError
        exception will be raised if no item was received.

        Note that exceptions from the remotely executing code will be
        reraised as channel.RemoteError exceptions containing
        a textual representation of the remote traceback.
        """
        mailbox = self._mailbox
        if mailbox is None:
            raise OSError("cannot receive(), channel has receiver callback")
        try:
            x = mailbox.get(timeout)
        except builtins.TimeoutError:
            raise self.TimeoutError("no item after %r seconds" % timeout) from None
        if x is ENDMARKER:
            mailbox.put(x)  # for other receivers
            raise self._getremoteerror() or EOFError()
        else:
            return loads_internal(x, self)

    def __iter__(self) -> Iterator[Any]:
        return self

    def next(self) -> Any:
        try:
            return self.receive()
        except EOFError:
            raise StopIteration from None

    __next__ = next

    def reconfigure(
        self, py2str_as_py3str: bool = True, py3str_as_py2str: bool = False
    ) -> None:
        """Set the string coercion for this channel.

        The default is to try to convert py2 str as py3 str,
        but not to try and convert py3 str to py2 str
        """
        self._strconfig = (py2str_as_py3str, py3str_as_py2str)
        data = dumps_internal(self._strconfig)
        self.gateway._send(Message.RECONFIGURE, self.id, data=data)


ENDMARKER = object()
INTERRUPT_TEXT = "keyboard-interrupted"
MAIN_THREAD_ONLY_DEADLOCK_TEXT = (
    "concurrent remote_exec would cause deadlock for main_thread_only execmodel"
)


class ChannelFactory:
    """Registry and id allocator for a gateway's sync channels.

    Message routing lives in the Trio session (the sync channel binds a
    consumer on the session's raw channel); the factory only tracks live
    channels -- weakly, so dropping the last user reference triggers
    ``Channel.__del__``'s close message -- and keeps channels with a
    registered callback strongly alive until they close.
    """

    def __init__(self, gateway: BaseGateway, startcount: int = 1) -> None:
        self._channels: weakref.WeakValueDictionary[int, Channel] = (
            weakref.WeakValueDictionary()
        )
        # channels kept strongly alive while their callback is registered
        self._callback_channels: dict[int, Channel] = {}
        self._writelock = threading.Lock()
        self.gateway = gateway
        self.count = startcount
        self.finished = False
        self._list = list  # needed during interp-shutdown

    def new(self, id: int | None = None) -> Channel:
        """Create a new Channel with 'id' (or create new id if None)."""
        with self._writelock:
            if self.finished:
                raise OSError(f"connection already closed: {self.gateway}")
            if id is None:
                id = self.count
                self.count += 2
            try:
                channel = self._channels[id]
            except KeyError:
                channel = self._channels[id] = Channel(self.gateway, id)
                self.gateway._bind_channel(channel)
            return channel

    def allocate_id(self) -> int:
        """Reserve a fresh channel id without creating a Channel object."""
        with self._writelock:
            if self.finished:
                raise OSError(f"connection already closed: {self.gateway}")
            id = self.count
            self.count += 2
            return id

    def channels(self) -> list[Channel]:
        return self._list(self._channels.values())

    #
    # internal methods, called from the loop thread (or local close paths)
    #
    def _register_callback_channel(self, channel: Channel) -> None:
        self._callback_channels[channel.id] = channel

    def _no_longer_opened(self, id: int) -> None:
        self._channels.pop(id, None)
        self._callback_channels.pop(id, None)

    def _local_close(self, id: int, remoteerror=None, sendonly: bool = False) -> None:
        """Close ``id`` as if the peer had closed it (no message is sent)."""
        channel = self._channels.get(id)
        if channel is None:
            # channel already in "deleted" state
            if remoteerror:
                remoteerror.warn()
            self._no_longer_opened(id)
        else:
            channel._close_from_remote(remoteerror, sendonly=sendonly)

    def _finished_receiving(self) -> None:
        with self._writelock:
            self.finished = True
        for id in self._list(self._channels):
            self._local_close(id, sendonly=True)


class ChannelFile:
    def __init__(self, channel: Channel, proxyclose: bool = True) -> None:
        self.channel = channel
        self._proxyclose = proxyclose

    def isatty(self) -> bool:
        return False

    def close(self) -> None:
        if self._proxyclose:
            self.channel.close()

    def __repr__(self) -> str:
        state = (self.channel.isclosed() and "closed") or "open"
        return "<ChannelFile %d %s>" % (self.channel.id, state)


class ChannelFileWrite(ChannelFile):
    def write(self, out: bytes) -> None:
        self.channel.send(out)

    def flush(self) -> None:
        pass


class ChannelFileRead(ChannelFile):
    def __init__(self, channel: Channel, proxyclose: bool = True) -> None:
        super().__init__(channel, proxyclose)
        self._buffer: str | None = None

    def read(self, n: int) -> str:
        try:
            if self._buffer is None:
                self._buffer = cast(str, self.channel.receive())
            while len(self._buffer) < n:
                self._buffer += cast(str, self.channel.receive())
        except EOFError:
            self.close()
        if self._buffer is None:
            ret = ""
        else:
            ret = self._buffer[:n]
            self._buffer = self._buffer[n:]
        return ret

    def readline(self) -> str:
        if self._buffer is not None:
            i = self._buffer.find("\n")
            if i != -1:
                return self.read(i + 1)
            line = self.read(len(self._buffer) + 1)
        else:
            line = self.read(1)
        while line and line[-1] != "\n":
            c = self.read(1)
            if not c:
                break
            line += c
        return line


class BaseGateway:
    _sysex = sysex
    id = "<worker>"
    _trio_session: Any = None
    # Set by the receiver on EOF without a prior termination message.
    _error: BaseException | None = None
    #: wait= axis: which wakener backend this gateway's blocking waits
    #: park on (channels, write-acks, join)
    _wait_backend: str = "thread"

    def __init__(self, io: IO, id, _startcount: int = 2) -> None:
        self.execmodel = io.execmodel
        self._io = io
        self.id = id
        self._strconfig = (Unserializer.py2str_as_py3str, Unserializer.py3str_as_py2str)
        self._channelfactory = ChannelFactory(self, _startcount)
        # globals may be NONE at process-termination
        self.__trace = trace
        self._geterrortext = geterrortext
        self._trio_session = None

    def _trace(self, *msg: object) -> None:
        self.__trace(self.id, *msg)

    def _attach_trio_session(self, session: Any) -> None:
        """Attach the Trio bridge session doing the Message IO."""
        self._trio_session = session
        # Defensive: channels created before the session existed still
        # need their inbound routing diverted to them.
        for channel in self._channelfactory.channels():
            session.bind_sync_channel(channel)

    def _new_wakener(self) -> Wakener:
        """A fresh wakener for one blocking-wait carrier (wait= axis)."""
        return make_wakener(self._wait_backend)

    def _bind_channel(self, channel: Channel) -> None:
        """Divert the session's inbound routing for ``channel.id`` to it."""
        session = self._trio_session
        if session is not None:
            session.bind_sync_channel(channel)

    def _release_channel(self, id: int) -> None:
        """Drop the session's loop-side state for ``id`` (best-effort)."""
        session = self._trio_session
        if session is not None:
            with suppress(Exception):
                session.release_channel(id)

    def _run_on_loop(self, sync_fn: Callable[[], Any]) -> Any:
        """Run ``sync_fn`` on the session's loop thread (inline without one).

        Payload dispatch happens on the loop thread, so state switches run
        there to exclude interleaving with deliveries.
        """
        session = self._trio_session
        if session is None:
            return sync_fn()
        return session.run_on_loop(sync_fn)

    def _terminate_execution(self) -> None:
        pass

    def _send(self, msgcode: int, channelid: int = 0, data: bytes = b"") -> None:
        message = Message(msgcode, channelid, data)
        session = self._trio_session
        if session is not None:
            try:
                session.enqueue_message(message)
                self._trace("sent", message)
            except (OSError, ValueError) as e:
                self._trace("failed to send", message, e)
                raise OSError("cannot send (already closed?)") from e
            return
        try:
            message.to_io(self._io)
            self._trace("sent", message)
        except (OSError, ValueError) as e:
            self._trace("failed to send", message, e)
            # ValueError might be because the IO is already closed
            raise OSError("cannot send (already closed?)") from e

    def _send_nonblocking(self, msgcode: int, channelid: int = 0) -> None:
        """Best-effort send that never waits (used during GC).

        Safe to call from any thread, including while the interpreter or
        the IO loop is shutting down.
        """
        message = Message(msgcode, channelid)
        session = self._trio_session
        if session is not None:
            session.post_message(message)
            return
        message.to_io(self._io)

    def _local_schedulexec(self, channel: Channel, sourcetask: bytes) -> None:
        channel.close("execution disallowed")

    # _____________________________________________________________________
    #
    # High Level Interface
    # _____________________________________________________________________
    #
    def newchannel(self) -> Channel:
        """Return a new independent channel."""
        return self._channelfactory.new()

    def join(self, timeout: float | None = None) -> None:
        """Wait for the receiver (Trio session) to terminate."""
        self._trace("waiting for receiver to finish")
        session = self._trio_session
        if session is not None:
            session.wait_done(timeout)


class WorkerGateway(BaseGateway):
    _trio_exec: Any = None
    # The exec pool (a TrioWorkerExec duck-typed as WorkerPool for STATUS).
    _execpool: Any = None
    _executetask_complete: threading.Event | None = None

    def _local_schedulexec(self, channel: Channel, sourcetask: bytes) -> None:
        trio_exec = self._trio_exec
        if trio_exec is None:
            channel.close("execution disallowed")
            return
        trio_exec.schedule(channel, sourcetask)

    def _terminate_execution(self) -> None:
        # called from receiverthread
        self._trace("shutting down execution pool")
        self._execpool.trigger_shutdown()
        if not self._execpool.waitall(5.0):
            self._trace("execution ongoing after 5 secs, trying interrupt_main")
            # We try hard to terminate execution based on the assumption
            # that there is only one gateway object running per-process.
            if sys.platform != "win32":
                self._trace("sending ourselves a SIGINT")
                os.kill(os.getpid(), 2)  # send ourselves a SIGINT
            elif interrupt_main is not None:
                self._trace("calling interrupt_main()")
                interrupt_main()
            if not self._execpool.waitall(10.0):
                self._trace(
                    "execution did not finish in another 10 secs, calling os._exit()"
                )
                os._exit(1)

    def executetask(
        self,
        item: tuple[Channel, tuple[str, str | None, str | None, dict[str, object]]],
    ) -> None:
        try:
            channel, (source, file_name, call_name, kwargs) = item
            loc: dict[str, Any] = {"channel": channel, "__name__": "__channelexec__"}
            self._trace(f"execution starts[{channel.id}]: {repr(source)[:50]}")
            channel._executing = True
            try:
                co = compile(source + "\n", file_name or "<remote exec>", "exec")
                exec(co, loc)
                if call_name:
                    self._trace("calling %s(**%60r)" % (call_name, kwargs))
                    function = loc[call_name]
                    function(channel, **kwargs)
            finally:
                channel._executing = False
                self._trace("execution finished")
        except KeyboardInterrupt:
            channel.close(INTERRUPT_TEXT)
            raise
        except EOFError:
            self._trace("ignoring EOFError because receiving finished")

        except BaseException as exc:
            if not channel.gateway._channelfactory.finished:
                self._trace(f"got exception: {exc!r}")
                errortext = self._geterrortext(exc)
                channel.close(errortext)
                return
        channel.close()
        if self._executetask_complete is not None:
            # Indicate that this task has finished executing, meaning
            # that there is no possibility of it triggering a deadlock
            # for the next spawn call.
            self._executetask_complete.set()


#
# Cross-Python pickling code, tested from test_serializer.py
#


class DataFormatError(Exception):
    pass


class DumpError(DataFormatError):
    """Error while serializing an object."""


class LoadError(DataFormatError):
    """Error while unserializing an object."""


def bchr(n: int) -> bytes:
    return bytes([n])


DUMPFORMAT_VERSION = bchr(2)

FOUR_BYTE_INT_MAX = 2147483647

FLOAT_FORMAT = "!d"
FLOAT_FORMAT_SIZE = struct.calcsize(FLOAT_FORMAT)
COMPLEX_FORMAT = "!dd"
COMPLEX_FORMAT_SIZE = struct.calcsize(COMPLEX_FORMAT)


class _Stop(Exception):
    pass


class opcode:
    """Container for name -> num mappings."""

    BUILDTUPLE = b"@"
    BYTES = b"A"
    CHANNEL = b"B"
    FALSE = b"C"
    FLOAT = b"D"
    FROZENSET = b"E"
    INT = b"F"
    LONG = b"G"
    LONGINT = b"H"
    LONGLONG = b"I"
    NEWDICT = b"J"
    NEWLIST = b"K"
    NONE = b"L"
    PY2STRING = b"M"
    PY3STRING = b"N"
    SET = b"O"
    SETITEM = b"P"
    STOP = b"Q"
    TRUE = b"R"
    UNICODE = b"S"
    COMPLEX = b"T"


class Unserializer:
    num2func: dict[bytes, Callable[[Unserializer], None]] = {}
    py2str_as_py3str = True  # True
    py3str_as_py2str = False  # false means py2 will get unicode

    def __init__(
        self,
        stream: ReadIO,
        channel_or_gateway: Channel | BaseGateway | None = None,
        strconfig: tuple[bool, bool] | None = None,
    ) -> None:
        if isinstance(channel_or_gateway, Channel):
            gw: BaseGateway | None = channel_or_gateway.gateway
        else:
            gw = channel_or_gateway
        if channel_or_gateway is not None:
            strconfig = channel_or_gateway._strconfig
        if strconfig:
            self.py2str_as_py3str, self.py3str_as_py2str = strconfig
        self.stream = stream
        if gw is None:
            self.channelfactory = None
        else:
            self.channelfactory = gw._channelfactory

    def load(self, versioned: bool = False) -> Any:
        if versioned:
            ver = self.stream.read(1)
            if ver != DUMPFORMAT_VERSION:
                raise LoadError("wrong dumpformat version %r" % ver)
        self.stack: list[object] = []
        try:
            while True:
                opcode = self.stream.read(1)
                if not opcode:
                    raise EOFError
                try:
                    loader = self.num2func[opcode]
                except KeyError:
                    raise LoadError(
                        f"unknown opcode {opcode!r} - wire protocol corruption?"
                    ) from None
                loader(self)
        except _Stop:
            if len(self.stack) != 1:
                raise LoadError("internal unserialization error") from None
            return self.stack.pop(0)
        else:
            raise LoadError("didn't get STOP")

    def load_none(self) -> None:
        self.stack.append(None)

    num2func[opcode.NONE] = load_none

    def load_true(self) -> None:
        self.stack.append(True)

    num2func[opcode.TRUE] = load_true

    def load_false(self) -> None:
        self.stack.append(False)

    num2func[opcode.FALSE] = load_false

    def load_int(self) -> None:
        i = self._read_int4()
        self.stack.append(i)

    num2func[opcode.INT] = load_int

    def load_longint(self) -> None:
        s = self._read_byte_string()
        self.stack.append(int(s))

    num2func[opcode.LONGINT] = load_longint

    load_long = load_int
    num2func[opcode.LONG] = load_long
    load_longlong = load_longint
    num2func[opcode.LONGLONG] = load_longlong

    def load_float(self) -> None:
        binary = self.stream.read(FLOAT_FORMAT_SIZE)
        self.stack.append(struct.unpack(FLOAT_FORMAT, binary)[0])

    num2func[opcode.FLOAT] = load_float

    def load_complex(self) -> None:
        binary = self.stream.read(COMPLEX_FORMAT_SIZE)
        self.stack.append(complex(*struct.unpack(COMPLEX_FORMAT, binary)))

    num2func[opcode.COMPLEX] = load_complex

    def _read_int4(self) -> int:
        value: int = struct.unpack("!i", self.stream.read(4))[0]
        return value

    def _read_byte_string(self) -> bytes:
        length = self._read_int4()
        as_bytes = self.stream.read(length)
        return as_bytes

    def load_py3string(self) -> None:
        as_bytes = self._read_byte_string()
        if self.py3str_as_py2str:
            # XXX Should we try to decode into latin-1?
            self.stack.append(as_bytes)
        else:
            self.stack.append(as_bytes.decode("utf-8"))

    num2func[opcode.PY3STRING] = load_py3string

    def load_py2string(self) -> None:
        as_bytes = self._read_byte_string()
        if self.py2str_as_py3str:
            s: bytes | str = as_bytes.decode("latin-1")
        else:
            s = as_bytes
        self.stack.append(s)

    num2func[opcode.PY2STRING] = load_py2string

    def load_bytes(self) -> None:
        s = self._read_byte_string()
        self.stack.append(s)

    num2func[opcode.BYTES] = load_bytes

    def load_unicode(self) -> None:
        self.stack.append(self._read_byte_string().decode("utf-8"))

    num2func[opcode.UNICODE] = load_unicode

    def load_newlist(self) -> None:
        length = self._read_int4()
        self.stack.append([None] * length)

    num2func[opcode.NEWLIST] = load_newlist

    def load_setitem(self) -> None:
        if len(self.stack) < 3:
            raise LoadError("not enough items for setitem")
        value = self.stack.pop()
        key = self.stack.pop()
        self.stack[-1][key] = value  # type: ignore[index]

    num2func[opcode.SETITEM] = load_setitem

    def load_newdict(self) -> None:
        self.stack.append({})

    num2func[opcode.NEWDICT] = load_newdict

    def _load_collection(self, type_: type) -> None:
        length = self._read_int4()
        if length:
            res = type_(self.stack[-length:])
            del self.stack[-length:]
            self.stack.append(res)
        else:
            self.stack.append(type_())

    def load_buildtuple(self) -> None:
        self._load_collection(tuple)

    num2func[opcode.BUILDTUPLE] = load_buildtuple

    def load_set(self) -> None:
        self._load_collection(set)

    num2func[opcode.SET] = load_set

    def load_frozenset(self) -> None:
        self._load_collection(frozenset)

    num2func[opcode.FROZENSET] = load_frozenset

    def load_stop(self) -> None:
        raise _Stop

    num2func[opcode.STOP] = load_stop

    def load_channel(self) -> None:
        id = self._read_int4()
        assert self.channelfactory is not None
        newchannel = self.channelfactory.new(id)
        self.stack.append(newchannel)

    num2func[opcode.CHANNEL] = load_channel


def dumps(obj: object) -> bytes:
    """Serialize the given obj to a bytestring.

    The obj and all contained objects must be of a builtin
    Python type (so nested dicts, sets, etc. are all OK but
    not user-level instances).
    """
    return _Serializer().save(obj, versioned=True)  # type: ignore[return-value]


def dump(byteio, obj: object) -> None:
    """write a serialized bytestring of the given obj to the given stream."""
    _Serializer(write=byteio.write).save(obj, versioned=True)


def loads(
    bytestring: bytes, py2str_as_py3str: bool = False, py3str_as_py2str: bool = False
) -> Any:
    """Deserialize the given bytestring to an object.

    py2str_as_py3str: If true then string (str) objects previously
                      dumped on Python2 will be loaded as Python3
                      strings which really are text objects.
    py3str_as_py2str: If true then string (str) objects previously
                      dumped on Python3 will be loaded as Python2
                      strings instead of unicode objects.

    If the bytestring was dumped with an incompatible protocol
    version or if the bytestring is corrupted, the
    ``execnet.DataFormatError`` will be raised.
    """
    io = BytesIO(bytestring)
    return load(
        io, py2str_as_py3str=py2str_as_py3str, py3str_as_py2str=py3str_as_py2str
    )


def load(
    io: ReadIO, py2str_as_py3str: bool = False, py3str_as_py2str: bool = False
) -> Any:
    """Derserialize an object form the specified stream.

    Behaviour and parameters are otherwise the same as with ``loads``
    """
    strconfig = (py2str_as_py3str, py3str_as_py2str)
    return Unserializer(io, strconfig=strconfig).load(versioned=True)


def loads_internal(
    bytestring: bytes,
    channelfactory=None,
    strconfig: tuple[bool, bool] | None = None,
) -> Any:
    io = BytesIO(bytestring)
    return Unserializer(io, channelfactory, strconfig).load()


def dumps_internal(obj: object) -> bytes:
    return _Serializer().save(obj)  # type: ignore[return-value]


class _Serializer:
    _dispatch: dict[type, Callable[[_Serializer, object], None]] = {}

    def __init__(self, write: Callable[[bytes], None] | None = None) -> None:
        if write is None:
            self._streamlist: list[bytes] = []
            write = self._streamlist.append
        self._write = write

    def save(self, obj: object, versioned: bool = False) -> bytes | None:
        # calling here is not re-entrant but multiple instances
        # may write to the same stream because of the common platform
        # atomic-write guarantee (concurrent writes each happen atomically)
        if versioned:
            self._write(DUMPFORMAT_VERSION)
        self._save(obj)
        self._write(opcode.STOP)
        try:
            streamlist = self._streamlist
        except AttributeError:
            return None
        return b"".join(streamlist)

    def _save(self, obj: object) -> None:
        tp = type(obj)
        try:
            dispatch = self._dispatch[tp]
        except KeyError:
            methodname = "save_" + tp.__name__
            meth: Callable[[_Serializer, object], None] | None = getattr(
                self.__class__, methodname, None
            )
            if meth is None:
                raise DumpError(f"can't serialize {tp}") from None
            dispatch = self._dispatch[tp] = meth
        dispatch(self, obj)

    def save_NoneType(self, non: None) -> None:
        self._write(opcode.NONE)

    def save_bool(self, boolean: bool) -> None:
        if boolean:
            self._write(opcode.TRUE)
        else:
            self._write(opcode.FALSE)

    def save_bytes(self, bytes_: bytes) -> None:
        self._write(opcode.BYTES)
        self._write_byte_sequence(bytes_)

    def save_str(self, s: str) -> None:
        self._write(opcode.PY3STRING)
        self._write_unicode_string(s)

    def _write_unicode_string(self, s: str) -> None:
        try:
            as_bytes = s.encode("utf-8")
        except UnicodeEncodeError as e:
            raise DumpError("strings must be utf-8 encodable") from e
        self._write_byte_sequence(as_bytes)

    def _write_byte_sequence(self, bytes_: bytes) -> None:
        self._write_int4(len(bytes_), "string is too long")
        self._write(bytes_)

    def _save_integral(self, i: int, short_op: bytes, long_op: bytes) -> None:
        if i <= FOUR_BYTE_INT_MAX:
            self._write(short_op)
            self._write_int4(i)
        else:
            self._write(long_op)
            self._write_byte_sequence(str(i).rstrip("L").encode("ascii"))

    def save_int(self, i: int) -> None:
        self._save_integral(i, opcode.INT, opcode.LONGINT)

    def save_long(self, l: int) -> None:
        self._save_integral(l, opcode.LONG, opcode.LONGLONG)

    def save_float(self, flt: float) -> None:
        self._write(opcode.FLOAT)
        self._write(struct.pack(FLOAT_FORMAT, flt))

    def save_complex(self, cpx: complex) -> None:
        self._write(opcode.COMPLEX)
        self._write(struct.pack(COMPLEX_FORMAT, cpx.real, cpx.imag))

    def _write_int4(
        self, i: int, error: str = "int must be less than %i" % (FOUR_BYTE_INT_MAX,)
    ) -> None:
        if i > FOUR_BYTE_INT_MAX:
            raise DumpError(error)
        self._write(struct.pack("!i", i))

    def save_list(self, L: list[object]) -> None:
        self._write(opcode.NEWLIST)
        self._write_int4(len(L), "list is too long")
        for i, item in enumerate(L):
            self._write_setitem(i, item)

    def _write_setitem(self, key: object, value: object) -> None:
        self._save(key)
        self._save(value)
        self._write(opcode.SETITEM)

    def save_dict(self, d: dict[object, object]) -> None:
        self._write(opcode.NEWDICT)
        for key, value in d.items():
            self._write_setitem(key, value)

    def save_tuple(self, tup: tuple[object, ...]) -> None:
        for item in tup:
            self._save(item)
        self._write(opcode.BUILDTUPLE)
        self._write_int4(len(tup), "tuple is too long")

    def _write_set(self, s: set[object] | frozenset[object], op: bytes) -> None:
        for item in s:
            self._save(item)
        self._write(op)
        self._write_int4(len(s), "set is too long")

    def save_set(self, s: set[object]) -> None:
        self._write_set(s, opcode.SET)

    def save_frozenset(self, s: frozenset[object]) -> None:
        self._write_set(s, opcode.FROZENSET)

    def save_Channel(self, channel: Channel) -> None:
        self._write(opcode.CHANNEL)
        self._write_int4(channel.id)

    def save_AsyncChannel(self, channel: Any) -> None:
        # trio-native channel (execnet._trio_gateway); same wire opcode,
        # duck-typed here to avoid importing the async core.
        self._write(opcode.CHANNEL)
        self._write_int4(channel.id)
