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

#: exceptions that must never be swallowed by a broad ``except``
sysex = (KeyboardInterrupt, SystemExit)

INTERRUPT_TEXT = "keyboard-interrupted"
MAIN_THREAD_ONLY_DEADLOCK_TEXT = (
    "concurrent remote_exec would cause deadlock for main_thread_only execmodel"
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
