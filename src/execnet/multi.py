"""
Managing Gateway Groups and interactions with multiple channels.

(c) 2008-2014, Holger Krekel and others
"""

from __future__ import annotations

import atexit
import queue
import threading
import time
import types
from collections.abc import Callable
from collections.abc import Iterable
from collections.abc import Iterator
from collections.abc import Sequence
from contextlib import suppress
from threading import Lock
from typing import TYPE_CHECKING
from typing import Any
from typing import Literal
from typing import TypeAlias
from typing import overload

from ._boundary import wakener_names
from .gateway_base import EXECMODEL_PROFILES
from .gateway_base import Channel
from .gateway_base import ExecModel
from .gateway_base import get_execmodel
from .gateway_base import trace
from .xspec import XSpec

if TYPE_CHECKING:
    from .gateway import Gateway


NO_ENDMARKER_WANTED = object()


class Group:
    """Gateway Group."""

    defaultspec = "popen"

    def __init__(
        self, xspecs: Iterable[XSpec | str | None] = (), execmodel: str = "thread"
    ) -> None:
        """Initialize a group and make gateways as specified.

        execmodel can be one of the supported execution models.
        """
        self._gateways: list[Gateway] = []
        self._autoidcounter = 0
        self._autoidlock = Lock()
        self._gateways_to_join: list[Gateway] = []
        self._trio_host: Any = None
        self._async_group: Any = None
        # we use the same execmodel for all of the Gateway objects
        # we spawn on our side.  Probably we should not allow different
        # execmodels between different groups but not clear.
        # Note that "other side" execmodels may differ and is typically
        # specified by the spec passed to makegateway.
        self.set_execmodel(execmodel)
        for xspec in xspecs:
            self.makegateway(xspec)
        atexit.register(self._cleanup_atexit)

    def _ensure_trio_host(self) -> Any:
        if self._trio_host is None:
            from . import _trio_host

            self._trio_host = _trio_host.TrioHost(name="execnet-trio-group")
            self._trio_host.start()
        return self._trio_host

    def _ensure_async_group(self) -> Any:
        """The FacadeAsyncGroup owning the async side, running on the host."""
        if self._async_group is None:
            from . import _trio_host

            host = self._ensure_trio_host()

            async def _start() -> Any:
                async_group = _trio_host.FacadeAsyncGroup(self, host)
                return await host._nursery.start(async_group.run)

            self._async_group = host.call(_start)
        return self._async_group

    @property
    def execmodel(self) -> ExecModel:
        return self._execmodel

    @property
    def remote_execmodel(self) -> ExecModel:
        return self._remote_execmodel

    def set_execmodel(
        self, execmodel: str, remote_execmodel: str | None = None
    ) -> None:
        """Set the execution model for local and remote site.

        execmodel can be one of the supported execution models.
        It determines the execution model for any newly created gateway.
        If remote_execmodel is not specified it takes on the value of execmodel.

        NOTE: Execution models can only be set before any gateway is created.
        """
        if self._gateways:
            raise ValueError(
                "can not set execution models if gateways have been created already"
            )
        if remote_execmodel is None:
            remote_execmodel = execmodel
        self._execmodel = get_execmodel(execmodel)
        self._remote_execmodel = get_execmodel(remote_execmodel)

    def __repr__(self) -> str:
        idgateways = [gw.id for gw in self]
        return "<Group %r>" % idgateways

    def __getitem__(self, key: int | str | Gateway) -> Gateway:
        if isinstance(key, int):
            return self._gateways[key]
        for gw in self._gateways:
            if gw == key or gw.id == key:
                return gw
        raise KeyError(key)

    def __contains__(self, key: str) -> bool:
        try:
            self[key]
            return True
        except KeyError:
            return False

    def __len__(self) -> int:
        return len(self._gateways)

    def __iter__(self) -> Iterator[Gateway]:
        return iter(list(self._gateways))

    def makegateway(self, spec: XSpec | str | None = None) -> Gateway:
        """Create and configure a gateway to a Python interpreter.

        The ``spec`` string encodes the target gateway type
        and configuration information. The general format is::

            key1=value1//key2=value2//...

        If you leave out the ``=value`` part a True value is assumed.
        Valid types: ``popen``, ``ssh=hostname``, ``socket=host:port``.
        Valid configuration::

            id=<string>     specifies the gateway id
            python=<path>   specifies which python interpreter to execute
            execmodel=name  worker profile: where exec'd code runs relative
                            to the worker's protocol loop.  'thread' (pool
                            threads) or 'main_thread_only' (serialized on
                            the worker main thread, GUI/signal-safe).
            wait=backend    wakener for blocking waits ('thread' default)
            chdir=<path>    specifies to which directory to change
            nice=<path>     specifies process priority of new process
            env:NAME=value  specifies a remote environment variable setting.

        If no spec is given, self.defaultspec is used.
        """
        if not spec:
            spec = self.defaultspec
        if not isinstance(spec, XSpec):
            spec = XSpec(spec)
        self.allocate_id(spec)
        if spec.execmodel is None:
            spec.execmodel = self.remote_execmodel.backend
        elif spec.execmodel not in EXECMODEL_PROFILES:
            raise ValueError(
                f"unknown execmodel {spec.execmodel!r}"
                f" (known profiles: {list(EXECMODEL_PROFILES)})"
            )
        if spec.wait is not None and spec.wait not in wakener_names():
            raise ValueError(
                f"unknown wait backend {spec.wait!r} (known: {wakener_names()})"
            )
        from . import _trio_host

        if not (spec.socket or spec.via or spec.ssh or spec.vagrant_ssh or spec.popen):
            raise ValueError(f"no gateway type found for {spec._spec!r}")
        gw = _trio_host.makegateway_trio(self, spec)
        gw.spec = spec
        self._register(gw)
        # chdir/nice/env travel in the worker config and are applied at
        # worker startup -- no remote_exec, so no exec slot is claimed.
        return gw

    def allocate_id(self, spec: XSpec) -> None:
        """(re-entrant) allocate id for the given xspec object."""
        if spec.id is None:
            with self._autoidlock:
                id = "gw" + str(self._autoidcounter)
                self._autoidcounter += 1
                if id in self:
                    raise ValueError(f"already have gateway with id {id!r}")
                spec.id = id

    def _register(self, gateway: Gateway) -> None:
        assert not hasattr(gateway, "_group")
        assert gateway.id
        assert gateway.id not in self
        self._gateways.append(gateway)
        gateway._group = self

    def _unregister(self, gateway: Gateway) -> None:
        self._gateways.remove(gateway)
        self._gateways_to_join.append(gateway)

    def _cleanup_atexit(self) -> None:
        trace(f"=== atexit cleanup {self!r} ===")
        self.terminate(timeout=1.0)
        if self._async_group is not None:
            with suppress(Exception):
                self._trio_host.call_sync(self._async_group.shutdown.set)
            self._async_group = None
        if self._trio_host is not None:
            self._trio_host.stop(timeout=1.0)
            self._trio_host = None

    def terminate(self, timeout: float | None = None) -> None:
        """Trigger exit of member gateways and wait for termination
        of member gateways and associated subprocesses.

        After waiting timeout seconds try to to kill local sub processes of
        popen- and ssh-gateways.

        Timeout defaults to None meaning open-ended waiting and no kill
        attempts.
        """
        while self or self._gateways_to_join:
            vias: set[str] = set()
            for gw in self:
                if gw.spec.via:
                    vias.add(gw.spec.via)
            for gw in self:
                if gw.id not in vias:
                    gw.exit()
            if self._async_group is not None:
                # Tunneled (via) gateways terminate before their masters,
                # each with a GATEWAY_TERMINATE + timeout grace, then kill;
                # bounded at roughly twice the timeout (issues #43 / #221).
                try:
                    self._host_terminate(timeout)
                except Exception as exc:
                    trace("group terminate error:", exc)
            for gw in self._gateways_to_join:
                gw.join()
            self._gateways_to_join[:] = []

    def _host_terminate(self, timeout: float | None) -> None:
        """Terminate the async group, parking correctly for wait backends.

        A member gateway created with a non-thread ``wait=`` implies the
        caller may be a greenlet: wait on a OneShot instead of blocking
        the OS thread (which would stall the hub for the whole grace).
        """
        backends = {gw._wait_backend for gw in self._gateways_to_join} | {
            gw._wait_backend for gw in self
        }
        backends.discard("thread")
        if backends:
            from ._boundary import make_wakener

            self._trio_host.call_pending(
                self._async_group.terminate,
                timeout,
                wakener=make_wakener(backends.pop()),
            ).wait()
        else:
            self._trio_host.call(self._async_group.terminate, timeout)

    def remote_exec(
        self,
        source: str | types.FunctionType | Callable[..., object] | types.ModuleType,
        **kwargs,
    ) -> MultiChannel:
        """remote_exec source on all member gateways and return
        a MultiChannel connecting to all sub processes."""
        channels = []
        for gw in self:
            channels.append(gw.remote_exec(source, **kwargs))
        return MultiChannel(channels)


class MultiChannel:
    def __init__(self, channels: Sequence[Channel]) -> None:
        self._channels = channels

    def __len__(self) -> int:
        return len(self._channels)

    def __iter__(self) -> Iterator[Channel]:
        return iter(self._channels)

    def __getitem__(self, key: int) -> Channel:
        return self._channels[key]

    def __contains__(self, chan: Channel) -> bool:
        return chan in self._channels

    def send_each(self, item: object) -> None:
        for ch in self._channels:
            ch.send(item)

    @overload
    def receive_each(self, withchannel: Literal[False] = ...) -> list[Any]:
        pass

    @overload
    def receive_each(self, withchannel: Literal[True]) -> list[tuple[Channel, Any]]:
        pass

    def receive_each(
        self, withchannel: bool = False
    ) -> list[tuple[Channel, Any]] | list[Any]:
        assert not hasattr(self, "_queue")
        l: list[object] = []
        for ch in self._channels:
            obj = ch.receive()
            if withchannel:
                l.append((ch, obj))
            else:
                l.append(obj)
        return l

    def make_receive_queue(self, endmarker: object = NO_ENDMARKER_WANTED):
        try:
            return self._queue  # type: ignore[has-type]
        except AttributeError:
            self._queue: queue.Queue[tuple[Channel, Any]] | None = None
            for ch in self._channels:
                if self._queue is None:
                    self._queue = queue.Queue()

                def putreceived(obj, channel: Channel = ch) -> None:
                    self._queue.put((channel, obj))  # type: ignore[union-attr]

                if endmarker is NO_ENDMARKER_WANTED:
                    ch.setcallback(putreceived)
                else:
                    ch.setcallback(putreceived, endmarker=endmarker)
            return self._queue

    def waitclose(self) -> None:
        first = None
        for ch in self._channels:
            try:
                ch.waitclose()
            except ch.RemoteError as exc:
                if first is None:
                    first = exc
        if first:
            raise first


TermKillFunc: TypeAlias = Callable[[], object]
TermKillPair: TypeAlias = tuple[TermKillFunc, TermKillFunc]


def safe_terminate(
    execmodel: ExecModel,
    timeout: float | None,
    list_of_paired_functions: Sequence[TermKillPair],
) -> None:
    """Run terminate/kill pairs in parallel with a hard wait bound.

    Each termfunc is given ``timeout``.  If it does not finish, killfunc runs.
    The final wait is also bounded so a stuck kill cannot hang the caller
    forever (see issues #43 / #221).  ``execmodel`` is accepted for
    backward compatibility and unused (daemon threads do the waiting).
    """
    errors: list[BaseException] = []

    def termkill(termfunc: TermKillFunc, killfunc: TermKillFunc) -> None:
        term_done = threading.Event()
        term_errors: list[BaseException] = []

        def run_term() -> None:
            try:
                termfunc()
            except BaseException as exc:
                term_errors.append(exc)
            finally:
                term_done.set()

        threading.Thread(target=run_term, daemon=True).start()
        if not term_done.wait(timeout):
            killfunc()
            return
        if term_errors:
            errors.append(term_errors[0])

    threads = [
        threading.Thread(target=termkill, args=pair, daemon=True)
        for pair in list_of_paired_functions
    ]
    for thread in threads:
        thread.start()
    # Allow term timeout plus a kill attempt; never block indefinitely.
    wait_timeout = None if timeout is None else timeout * 2
    deadline = None if wait_timeout is None else time.monotonic() + wait_timeout
    for thread in threads:
        remaining = None if deadline is None else max(0, deadline - time.monotonic())
        thread.join(remaining)
    if errors:
        raise errors[0]


default_group = Group()
makegateway = default_group.makegateway
set_execmodel = default_group.set_execmodel
