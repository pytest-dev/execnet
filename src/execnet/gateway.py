"""Gateway code for initiating popen, socket and ssh connections.

(c) 2004-2013, Holger Krekel and others
"""

from __future__ import annotations

import types
from collections.abc import Callable
from typing import TYPE_CHECKING
from typing import Any

from . import gateway_base
from ._exec_source import _find_non_builtin_globals
from ._exec_source import _source_of_function
from ._exec_source import normalize_exec_source
from .gateway_base import IO
from .gateway_base import Channel
from .gateway_base import Message
from .multi import Group
from .xspec import XSpec

__all__ = [
    "Gateway",
    "RInfo",
    "RemoteStatus",
    "_find_non_builtin_globals",
    "_source_of_function",
]


class Gateway(gateway_base.BaseGateway):
    """Gateway to a local or remote Python Interpreter."""

    _group: Group

    def __init__(self, io: IO, spec: XSpec) -> None:
        """:private:

        The Trio session doing the Message IO is attached separately via
        ``_attach_trio_session`` once the connection is established.
        """
        super().__init__(io=io, id=spec.id, _startcount=1)
        self.spec = spec
        if spec.wait:
            self._wait_backend = spec.wait

    @property
    def remoteaddress(self) -> str:
        # Only defined for remote IO types.
        return self._io.remoteaddress  # type: ignore[attr-defined,no-any-return]

    def __repr__(self) -> str:
        """A string representing gateway type and status."""
        try:
            r: str = (self.hasreceiver() and "receive-live") or "not-receiving"
            i = str(len(self._channelfactory.channels()))
        except AttributeError:
            r = "uninitialized"
            i = "no"
        return f"<{self.__class__.__name__} id={self.id!r} {r}, {self.execmodel.backend} model, {i} active channels>"

    def exit(self) -> None:
        """Trigger gateway exit.

        Defer waiting for finishing of receiver-thread and subprocess activity
        to when group.terminate() is called.
        """
        self._trace("gateway.exit() called")
        if self not in self._group:
            self._trace("gateway already unregistered with group")
            return
        self._group._unregister(self)
        try:
            self._trace("--> sending GATEWAY_TERMINATE")
            self._send(Message.GATEWAY_TERMINATE)
            self._trace("--> io.close_write")
            self._io.close_write()
        except (ValueError, EOFError, OSError) as exc:
            self._trace("io-error: could not send termination sequence")
            self._trace(" exception: %r" % exc)

    def reconfigure(
        self, py2str_as_py3str: bool = True, py3str_as_py2str: bool = False
    ) -> None:
        """Set the string coercion for this gateway.

        The default is to try to convert py2 str as py3 str, but not to try and
        convert py3 str to py2 str.
        """
        self._strconfig = (py2str_as_py3str, py3str_as_py2str)
        data = gateway_base.dumps_internal(self._strconfig)
        self._send(Message.RECONFIGURE, data=data)

    def _rinfo(self, update: bool = False) -> RInfo:
        """Return some sys/env information from remote.

        A native protocol request (like ``remote_status``): it never
        touches the exec machinery, so it cannot claim an exec slot on
        main-thread-shaped workers.
        """
        if update or not hasattr(self, "_cache_rinfo"):
            channel = self.newchannel()
            self._send(Message.GATEWAY_INFO, channel.id)
            self._cache_rinfo = RInfo(channel.receive())
            # the other side didn't actually instantiate a channel
            # so we just delete the internal id/channel mapping
            self._channelfactory._local_close(channel.id)
        return self._cache_rinfo

    def hasreceiver(self) -> bool:
        """Whether gateway is able to receive data."""
        session = self._trio_session
        return session is not None and bool(session.is_alive())

    def remote_status(self) -> RemoteStatus:
        """Obtain information about the remote execution status."""
        channel = self.newchannel()
        self._send(Message.STATUS, channel.id)
        statusdict = channel.receive()
        # the other side didn't actually instantiate a channel
        # so we just delete the internal id/channel mapping
        self._channelfactory._local_close(channel.id)
        return RemoteStatus(statusdict)

    def remote_exec(
        self,
        source: str | types.FunctionType | Callable[..., object] | types.ModuleType,
        **kwargs: object,
    ) -> Channel:
        """Return channel object and connect it to a remote
        execution thread where the given ``source`` executes.

        * ``source`` is a string: execute source string remotely
          with a ``channel`` put into the global namespace.
        * ``source`` is a pure function: serialize source and
          call function with ``**kwargs``, adding a
          ``channel`` object to the keyword arguments.
        * ``source`` is a pure module: execute source of module
          with a ``channel`` in its global namespace.

        In all cases the binding ``__name__='__channelexec__'``
        will be available in the global namespace of the remotely
        executing code.
        """
        source, file_name, call_name = normalize_exec_source(source, kwargs)
        channel = self.newchannel()
        self._send(
            Message.CHANNEL_EXEC,
            channel.id,
            gateway_base.dumps_internal((source, file_name, call_name, kwargs)),
        )
        return channel

    def remote_init_threads(self, num: int | None = None) -> None:
        """DEPRECATED.  Is currently a NO-OPERATION already."""
        print("WARNING: remote_init_threads() is a no-operation in execnet-1.2")


class RInfo:
    def __init__(self, kwargs) -> None:
        self.__dict__.update(kwargs)

    def __repr__(self) -> str:
        info = ", ".join(f"{k}={v}" for k, v in sorted(self.__dict__.items()))
        return "<RInfo %r>" % info

    if TYPE_CHECKING:

        def __getattr__(self, name: str) -> Any: ...


RemoteStatus = RInfo
