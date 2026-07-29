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
import warnings
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

from ._boundary import WaitBackend
from ._channel import Channel
from ._execmodel import ExecModel
from ._execmodel import get_execmodel
from ._execmodel import resolve_profile
from ._host import Host
from ._host import check_not_in_event_loop
from ._host import default_host
from ._trace import trace
from ._xspec import XSpec

if TYPE_CHECKING:
    from ._gateway import Gateway


NO_ENDMARKER_WANTED = object()


class Group:
    """Gateway Group."""

    defaultspec = "popen"

    #: which primitive this group's blocking waits park on.  Set by the
    #: facade, not by a spec: it describes the *caller's* concurrency
    #: library, which is exactly what picking a namespace already says.
    #: ``execnet.gevent.Group`` overrides it.
    _wait_backend: WaitBackend = "thread"

    def __init__(
        self,
        xspecs: Iterable[XSpec | str | None] = (),
        profile: str | None = None,
        *,
        host: Host | None = None,
        execmodel: str | None = None,
    ) -> None:
        """Initialize a group and make gateways as specified.

        ``profile`` is the default worker profile for gateways created
        without an explicit ``profile=`` in their spec.  ``host`` is the
        Trio host thread to serve this group's protocol IO on; it defaults
        to the process-wide one.  ``execmodel`` is the deprecated spelling
        of ``profile`` (pytest-xdist still passes it).
        """
        if execmodel is not None:
            if profile is not None:
                raise TypeError("pass either profile= or execmodel=, not both")
            warnings.warn(
                "Group(execmodel=...) is deprecated; use Group(profile=...)."
                " execnet has no local execution model any more -- the value"
                " only selects the worker profile.",
                DeprecationWarning,
                stacklevel=2,
            )
            profile = execmodel
        self._gateways: list[Gateway] = []
        self._autoidcounter = 0
        self._autoidlock = Lock()
        self._gateways_to_join: list[Gateway] = []
        self._host = default_host() if host is None else host
        self._async_group: Any = None
        self.set_profile("thread" if profile is None else profile)
        for xspec in xspecs:
            self.makegateway(xspec)
        atexit.register(self._cleanup_atexit)

    @property
    def host(self) -> Host:
        """The Trio host thread this group's protocol IO runs on."""
        return self._host

    def _ensure_trio_host(self) -> Any:
        return self._host._ensure_started()

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
    def profile(self) -> str:
        """Default worker profile for gateways created by this group."""
        return self._profile

    def set_profile(self, profile: str) -> None:
        """Set the default worker profile for newly created gateways.

        NOTE: only settable before any gateway is created.
        """
        if self._gateways:
            raise ValueError(
                "can not set the profile if gateways have been created already"
            )
        self._profile = resolve_profile(profile)

    @property
    def execmodel(self) -> ExecModel:
        """Deprecated: there is no local execution model any more."""
        warnings.warn(
            "Group.execmodel is deprecated: execnet has no local execution"
            " model. Use Group.profile for the worker profile.",
            DeprecationWarning,
            stacklevel=2,
        )
        return get_execmodel(self._profile)

    @property
    def remote_execmodel(self) -> ExecModel:
        """Deprecated alias for :attr:`profile`, as an ExecModel shim."""
        warnings.warn(
            "Group.remote_execmodel is deprecated; use Group.profile.",
            DeprecationWarning,
            stacklevel=2,
        )
        return get_execmodel(self._profile)

    def set_execmodel(
        self, execmodel: str, remote_execmodel: str | None = None
    ) -> None:
        """Deprecated alias for :meth:`set_profile`.

        The *local* execution model it used to set no longer exists -- all
        protocol IO runs on the Trio host -- so only the worker profile is
        taken from these arguments (``remote_execmodel`` when given, else
        ``execmodel``).
        """
        warnings.warn(
            "Group.set_execmodel is deprecated; use Group.set_profile(profile)."
            " execnet has no local execution model, so only the remote value"
            " has an effect.",
            DeprecationWarning,
            stacklevel=2,
        )
        self.set_profile(
            execmodel if remote_execmodel is None else remote_execmodel
        )

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
            profile=name    worker profile: where exec'd code runs relative
                            to the worker's protocol loop.  'thread'
                            (default; the first remote_exec claims the
                            worker main thread, further ones overflow to
                            pool threads), 'trio' (async sources as tasks,
                            single-threaded) or 'gevent' (a greenlet per
                            remote_exec).  Spelled 'execmodel=' before
                            execnet 3.0; that spelling still works.
            chdir=<path>    specifies to which directory to change
            nice=<path>     specifies process priority of new process
            env:NAME=value  specifies a remote environment variable setting.

        If no spec is given, self.defaultspec is used.
        """
        check_not_in_event_loop("Group.makegateway()")
        if not spec:
            spec = self.defaultspec
        if not isinstance(spec, XSpec):
            spec = XSpec(spec)
        self.allocate_id(spec)
        if spec.profile is None:
            spec.profile = self._profile
        else:
            spec.profile = resolve_profile(spec.profile)
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
        # The host is shared and stops itself at exit; a group only owns
        # its gateways and the async group task running on that host.
        trace(f"=== atexit cleanup {self!r} ===")
        self.terminate(timeout=1.0)
        if self._async_group is not None:
            with suppress(Exception):
                self._host._ensure_started().call_sync(
                    self._async_group.shutdown.set
                )
            self._async_group = None

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
        """Terminate the async group, parking correctly for the wait backend.

        A non-thread backend implies the caller may be a greenlet: wait on
        a OneShot instead of blocking the OS thread (which would stall the
        hub for the whole grace).
        """
        trio_host = self._host._ensure_started()
        if self._wait_backend == "thread":
            trio_host.call(self._async_group.terminate, timeout)
            return
        from ._boundary import make_wakener

        trio_host.call_pending(
            self._async_group.terminate,
            timeout,
            wakener=make_wakener(self._wait_backend),
        ).wait()

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
set_profile = default_group.set_profile
#: deprecated alias, see Group.set_execmodel
set_execmodel = default_group.set_execmodel
