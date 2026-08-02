"""The blocking :class:`Channel` and its registry and file adapters.

A ``Channel`` is a facade over the async core: the gateway's Trio session
diverts inbound payloads for a channel id into a :class:`~execnet._boundary.Mailbox`
(or a registered callback), and ``receive()`` deserializes at the call site,
off the loop thread.
"""

from __future__ import annotations

import enum
import threading
import weakref
from collections.abc import Callable
from collections.abc import Iterator
from contextlib import suppress
from typing import TYPE_CHECKING
from typing import Any
from typing import Literal
from typing import cast
from typing import overload

from ._boundary import Flag
from ._boundary import Mailbox
from ._errors import RemoteError
from ._errors import TimeoutError
from ._message import Message
from ._serialize import dumps_internal
from ._serialize import loads_internal

if TYPE_CHECKING:
    from ._gateway_base import BaseGateway


class NoEndmarker(enum.Enum):
    """Type of the "no endmarker wanted" sentinel.

    An enum rather than a bare ``object()`` so it is nameable in an
    annotation: an endmarker may be *any* object, so the only thing that
    distinguishes "none wanted" from a legitimate endmarker is identity,
    and a second module inventing its own ``object()`` for it would be
    silently wrong.  ``endmarker: object | Literal[NoEndmarker.NOT_WANTED]``
    says which sentinel a caller has to hand back.
    """

    NOT_WANTED = enum.auto()


NO_ENDMARKER_WANTED = NoEndmarker.NOT_WANTED

#: what an ``endmarker=`` parameter accepts: any object to deliver at the
#: end, or the sentinel meaning "do not deliver one"
Endmarker = object | Literal[NoEndmarker.NOT_WANTED]


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
    #: set once a receiver callback is attached.  A consumer *task* on the
    #: loop drains this channel and runs the callback in a threadpool thread;
    #: the task holds the channel alive, so a callback channel's lifecycle is
    #: bound to consumption (and to GC once the stream closes) rather than to
    #: a strong registry.
    _has_consumer = False
    #: loop-side hooks installed while a consumer is attached: divert one
    #: payload into / close the consumer task's inbox (set by the Trio session,
    #: so they encapsulate the trio memory channel; this module stays trio-free).
    _consumer_feed: Callable[[bytes], None] | None = None
    _consumer_close_inbox: Callable[[], None] | None = None
    #: thread-safe "stop the consumer" hook -- ends the task's inbox.
    _consumer_stop: Callable[[], None] | None = None
    #: set by the consumer task once it has drained every item and fired the
    #: endmarker; ``waitclose()`` waits on this (instead of ``_receiveclosed``)
    #: so it still guarantees "all callbacks have run" before returning.
    _consumer_done: Flag | None = None

    def __init__(self, gateway: BaseGateway, id: int) -> None:
        """:private:"""
        assert isinstance(id, int)
        assert not isinstance(gateway, type)
        self.gateway = gateway
        self.id = id
        # serialized payloads (or ENDMARKER); None once a consumer is attached
        self._mailbox: Mailbox[Any] | None = Mailbox(gateway._new_wakener())
        self._closed = False
        self._receiveclosed = Flag(gateway._new_wakener())
        self._remoteerrors: list[RemoteError] = []

    def _trace(self, *msg: object) -> None:
        self.gateway._trace(self.id, *msg)

    def setcallback(
        self,
        callback: Callable[[Any], Any],
        endmarker: Endmarker = NO_ENDMARKER_WANTED,
    ) -> None:
        """Set a callback function for receiving items.

        A consumer task on the gateway's loop drains this channel and runs
        ``callback`` for each received item in a threadpool thread (so a slow
        callback never blocks the loop); items for one channel are delivered
        strictly in order.  Already-queued items are delivered first.  After
        this call ``receive()`` raises an error.

        The task keeps the channel alive for as long as it is consuming, so a
        callback channel need not be referenced elsewhere.  If an endmarker is
        specified the callback is eventually called with it when the channel
        closes, and ``waitclose()`` does not return until every callback
        (including the endmarker) has run.

        The pool the callbacks run on is shared and bounded (40 threads by
        default, ``ProtocolEngine(callback_threads=...)``).  A callback may block --
        that is the point of running it off the loop -- but callbacks that
        block on *each other*, directly or through a queue only another
        callback drains, can occupy the whole pool and stall every channel in
        the process.  Hand work that waits to a thread of your own.
        """
        self.gateway._start_channel_consumer(self, callback, endmarker)

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
            # A callback channel is held by its consumer task until the stream
            # closes, so by the time __del__ runs it is never in the "opened"
            # state -- this branch only ever fires for a plain receive channel.
            if Message is not None:
                with suppress(OSError, ValueError):  # ignore problems with sending
                    # Never wait during GC: post the close best-effort.
                    send = getattr(
                        self.gateway, "_send_nonblocking", self.gateway._send
                    )
                    send(Message.CHANNEL_CLOSE, self.id)
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
        """Route one inbound serialized payload (loop thread).

        A callback channel diverts payloads into its consumer task's inbox; a
        plain channel queues them for ``receive()``.
        """
        if self._closed:
            return  # late data for a locally closed channel: drop
        feed = self._consumer_feed
        if feed is not None:
            feed(data)
            return
        mailbox = self._mailbox
        if mailbox is not None:
            mailbox.put(data)
        # no consumer and no mailbox: closed for receiving -- drop

    def _close_from_remote(self, remoteerror=None, *, sendonly: bool = False) -> None:
        """Close initiated by the peer or session shutdown (loop thread).

        For a callback channel the consumer task ends separately (its inbox is
        closed) and fires the endmarker; here we only record the state.
        """
        if remoteerror:
            self._remoteerrors.append(remoteerror)
        if self._has_consumer:
            close_inbox = self._consumer_close_inbox
            if close_inbox is not None:
                close_inbox()  # ends the consumer task (it fires the endmarker)
        else:
            mailbox = self._mailbox
            if mailbox is not None:
                mailbox.put(ENDMARKER)
        self.gateway._channelfactory._no_longer_opened(self.id)
        if not sendonly:  # otherwise #--> "sendonly"
            self._closed = True  # --> "closed"
        self._receiveclosed.set()

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
            if self._has_consumer:
                # End the consumer task's inbox; it drains any buffered items,
                # fires the endmarker, and sets _consumer_done.
                stop = self._consumer_stop
                if stop is not None:
                    stop()
            else:
                mailbox = self._mailbox
                if mailbox is not None:
                    mailbox.put(ENDMARKER)
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
        # For a callback channel wait on the consumer task finishing (so every
        # callback, including the endmarker, has run); otherwise wait for the
        # non-"opened" state directly.
        self.gateway._check_usable("channel.waitclose()")
        signal = (
            self._consumer_done
            if self._consumer_done is not None
            else (self._receiveclosed)
        )
        signal.wait(timeout=timeout)
        if not signal.is_set():
            raise self.TimeoutError("Timeout after %r seconds" % timeout)
        error = self._getremoteerror()
        if error:
            raise error

    def send(self, item: object) -> None:
        """Sends the given item to the other side of the channel.

        The item must be a simple Python type and will be
        copied to the other side by value.

        Returns once the data has reached the OS write, which is not the
        same as the peer having read it: there is no flow control, so a
        peer that never receives buffers everything sent to it rather than
        pushing back.  Sending unboundedly to one is a memory leak in *its*
        process.

        OSError is raised if the write pipe was prematurely closed.
        """
        # before the state check: an unusable gateway (inherited by a fork,
        # or driven from inside an event loop) is a caller bug either way,
        # and whether the channel has closed yet is a race -- the diagnostic
        # should not depend on it
        self.gateway._check_usable("channel.send()")
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
        self.gateway._check_usable("channel.receive()")
        mailbox = self._mailbox
        if mailbox is None:
            raise OSError("cannot receive(), channel has receiver callback")
        x = mailbox.get(timeout)
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


ENDMARKER = object()


class ChannelFactory:
    """Registry and id allocator for a gateway's sync channels.

    Message routing lives in the Trio session (the sync channel binds a
    consumer on the session's raw channel); the factory only tracks live
    channels -- weakly, so dropping the last user reference triggers
    ``Channel.__del__``'s close message.  A callback channel is kept alive by
    its consumer task rather than by any registry here.
    """

    def __init__(self, gateway: BaseGateway, startcount: int = 1) -> None:
        self._channels: weakref.WeakValueDictionary[int, Channel] = (
            weakref.WeakValueDictionary()
        )
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
    def _no_longer_opened(self, id: int) -> None:
        self._channels.pop(id, None)

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
