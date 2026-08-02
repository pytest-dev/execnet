"""The gateway base classes shared by coordinator and worker.

:class:`BaseGateway` owns the channel factory, the send path and the handle on
the Trio session that does the actual Message IO; :class:`WorkerGateway` adds
the worker-side ``remote_exec`` scheduling and shutdown.

:copyright: 2004-2015
:authors:
    - Holger Krekel
    - Armin Rigo
    - Benjamin Peterson
    - Ronny Pfannschmidt
    - many others
"""

from __future__ import annotations

import os
import sys
from _thread import interrupt_main
from collections.abc import Callable
from contextlib import suppress
from typing import Any

from ._boundary import WaitBackend
from ._boundary import Wakener
from ._boundary import make_wakener
from ._channel import Channel
from ._channel import ChannelFactory
from ._channel import Endmarker
from ._errors import INTERRUPT_TEXT
from ._errors import ForkedResourceError
from ._errors import geterrortext
from ._errors import sysex
from ._message import IO
from ._message import Message
from ._trace import trace


class BaseGateway:
    _sysex = sysex
    id = "<worker>"
    _trio_session: Any = None
    # Set by the receiver on EOF without a prior termination message.
    _error: BaseException | None = None
    #: which primitive this gateway's blocking waits park on (channels,
    #: write-acks, join).  Inherited from the facade coordinator-side, and
    #: derived from the worker profile worker-side.
    _wait_backend: WaitBackend = "thread"
    #: whether blocking operations refuse to run inside a foreign event
    #: loop.  Only coordinator-side: exec'd code in a worker may legitimately
    #: run its own loop and talk to its channel from inside it.
    _guard_event_loop = False

    def _check_usable(self, what: str) -> None:
        """Refuse ``what`` when this gateway cannot possibly serve it.

        Two caller bugs, checked before anything else (in particular before
        the channel-state check, so which one you are told about does not
        depend on whether the peer has closed yet): using a gateway that a
        fork left behind in another process, and blocking a running event
        loop's own thread.
        """
        if self._pid != os.getpid():
            from ._errors import forked_error

            raise forked_error(what, self._pid)
        if self._guard_event_loop:
            from ._engine import check_not_in_event_loop

            check_not_in_event_loop(what)

    def __init__(self, io: IO, id, _startcount: int = 2) -> None:
        self.execmodel = io.execmodel
        self._io = io
        self.id = id
        #: pid this gateway's connection (and its engine loop) belongs to
        self._pid = os.getpid()
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
        """A fresh wakener for one blocking-wait carrier."""
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

    def _start_channel_consumer(
        self,
        channel: Channel,
        callback: Callable[[Any], Any],
        endmarker: Endmarker,
    ) -> None:
        """Attach a receiver callback: hand the channel to a consumer task.

        The task drains the channel on the loop and runs ``callback`` in a
        threadpool thread, holding the channel alive while it consumes.
        """
        session = self._trio_session
        if session is None:
            raise OSError(f"cannot set callback on {channel!r}: no active session")
        session.attach_consumer(channel, callback, endmarker)

    def _terminate_execution(self) -> None:
        pass

    def _send(self, msgcode: int, channelid: int = 0, data: bytes = b"") -> None:
        message = Message(msgcode, channelid, data)
        session = self._trio_session
        if session is not None:
            try:
                session.enqueue_message(message)
                self._trace("sent", message)
            except ForkedResourceError:
                # "already closed?" would be a guess, and a wrong one
                self._trace("failed to send", message, "(inherited by a fork)")
                raise
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
        self._check_usable("gateway.join()")
        self._trace("waiting for receiver to finish")
        session = self._trio_session
        if session is not None:
            session.wait_done(timeout)


class WorkerGateway(BaseGateway):
    _trio_exec: Any = None
    # The exec pool (a TrioWorkerExec duck-typed as WorkerPool for STATUS).
    _execpool: Any = None

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
            self._close_finished(channel, INTERRUPT_TEXT)
            raise
        except EOFError:
            self._trace("ignoring EOFError because receiving finished")

        except BaseException as exc:
            if not channel.gateway._channelfactory.finished:
                self._trace(f"got exception: {exc!r}")
                errortext = self._geterrortext(exc)
                self._close_finished(channel, errortext)
                return
        self._close_finished(channel)

    def _close_finished(self, channel: Channel, error: str | None = None) -> None:
        """Close the channel an exec ran on, tolerating a dead connection.

        The close is how the coordinator learns the source finished, so it
        is attempted always -- but the connection going away first is an
        ordinary teardown race (a killed worker, a terminate that outran the
        exec), and there is no longer anyone to raise at.  Letting the OSError
        out lands it in the exec task, whose nursery is the worker's root one.

        The exec's admission slot goes back *first*: this close is also what
        tells a coordinator at capacity that it may send the next request,
        and it must not be able to arrive before the slot it frees.
        """
        execpool = self._execpool
        if execpool is not None:
            execpool.release_slot(channel.id)
        try:
            channel.close(error)
        except OSError as exc:
            self._trace("could not close", channel, "after execution:", exc)
