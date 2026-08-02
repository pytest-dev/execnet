"""Exception types and the error texts that cross the wire.

The channel-facing errors (:class:`RemoteError`, :class:`TimeoutError`,
:class:`HostNotFound`, and the :class:`DataFormatError` family) are the one
part of this module that is public: every namespace -- :mod:`execnet.sync`,
:mod:`execnet.trio`, :mod:`execnet.aio` -- re-exports them, because the same
errors are raised whichever surface you drive a gateway from.
"""

from __future__ import annotations

import os
import sys
import traceback
from contextlib import suppress

#: exceptions that must never be swallowed by a broad ``except``
sysex = (KeyboardInterrupt, SystemExit)

INTERRUPT_TEXT = "keyboard-interrupted"


class GatewayReceivedTerminate(Exception):
    """Receiver got a gateway termination message."""


class HostNotFound(ConnectionError):
    """The remote side of a gateway could not be reached."""


class LoopFinishedError(RuntimeError):
    """Work was handed to a loop that has already finished.

    Backend-neutral on purpose: every route into an engine goes through a
    :class:`~execnet._portal.Portal`, and the two backends spell this
    differently (``trio.RunFinishedError``; a plain ``RuntimeError`` from
    ``call_soon_threadsafe`` on a closed asyncio loop).  Callers catch this
    one and stay unaware of which engine they are talking to.
    """


class ActiveGroupsWarning(UserWarning):
    """A :class:`~execnet.ProtocolEngine` was closed with groups still live.

    Closing terminates them rather than leaving their workers behind, but
    it is doing the caller's job at the worst possible moment: at close
    time there is nothing left to report a slow or stuck worker *to*, and
    an engine closed from ``atexit`` may not get to display this warning at
    all.  Terminate the groups where you can still see the result.

    A ``UserWarning`` rather than a ``ResourceWarning`` on purpose: the
    latter is ignored by default, and something that quietly kills worker
    processes should not be quiet.
    """


class ForkedResourceError(OSError):
    """An execnet object was inherited by ``os.fork()`` and is dead here.

    Nothing execnet builds survives a fork: the engine's loop thread is not
    duplicated into the child, and the worker connections belong to the
    parent that opened them.  Rather than let the child block forever on a
    loop that will never run again, every route to the engine checks which
    process it is in and raises this.

    An ``OSError`` because that is what execnet already means by "this
    connection is gone" -- ``__del__`` paths and ``except OSError`` cleanup
    keep working -- but a distinct type, so the paths that would otherwise
    rewrite it as "cannot send (already closed?)" can let the real reason
    through.

    Recovery is explicit and belongs to the child: build a new
    :class:`~execnet.ProtocolEngine` and a new ``Group`` on it.
    """


def forked_error(what: str, origin_pid: int) -> ForkedResourceError:
    """The :class:`ForkedResourceError` for using ``what`` after a fork."""
    return ForkedResourceError(
        f"{what} belongs to pid {origin_pid} and this is pid {os.getpid()}:"
        " execnet objects do not survive os.fork() -- the engine's loop thread"
        " is not duplicated into the child, and the worker connections stay"
        " with the parent. Build a new ProtocolEngine and a new Group in the"
        " child."
    )


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
            # A best-effort diagnostic that must not raise: it runs for a
            # channel nobody kept a reference to, which can be on the engine
            # loop (a close replayed to a late-bound consumer) and as late
            # as interpreter shutdown, where stderr may already be closed.
            # An exception on the loop ends the run for every gateway.
            with suppress(Exception):
                # XXX do this better
                sys.stderr.write(f"[{os.getpid()}] Warning: unhandled {self!r}\n")


class TimeoutError(IOError):
    """Exception indicating that a timeout was reached."""


class DataFormatError(Exception):
    """A value could not cross the channel in execnet's simple wire format.

    execnet only moves *simple* builtin data over a channel -- ``None``,
    ``bool``, ``int``, ``float``, ``complex``, ``bytes``, ``str``, and
    (arbitrarily nested) ``list``/``tuple``/``set``/``frozenset``/``dict`` of
    those -- plus channel references, which pass through as channels.  It does
    **not** pickle: arbitrary instances, functions, ``datetime``, dataclasses,
    pydantic models, numpy arrays, etc. have no wire representation.

    A ``DataFormatError`` therefore signals a caller error to resolve, not a
    transport failure: reduce the value to simple data before sending (and
    reconstruct it after receiving) with an encoding mechanism of your own --
    e.g. pydantic ``model_dump`` / ``model_validate`` or pytest's
    ``pytest_report_to_serializable`` / ``pytest_report_from_serializable``
    hooks.  See the docs, "Sending objects over a channel".
    """


class DumpError(DataFormatError):
    """A value being **sent** is not simple wire data; convert it first.

    Raised by ``channel.send`` (and the internal serializer) when an object is
    not one of execnet's simple wire types.  Fix it at the call site by turning
    the rich object into simple data -- e.g. ``dt.isoformat()``,
    ``dataclasses.asdict(obj)``, ``model.model_dump(mode="json")`` -- rather
    than expecting the channel to pickle it.  Channels are the one non-builtin
    that *is* sendable, so nested channel references are fine.
    """


class LoadError(DataFormatError):
    """Received bytes could not be turned back into an object.

    Raised while **receiving** (deserializing) -- a corrupted or
    protocol-incompatible payload, or data produced by a mismatched peer.
    """
