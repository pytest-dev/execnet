"""Worker services: protocol requests a worker serves *itself*.

A service is infrastructure that used to be a ``remote_exec`` of execnet's
own source -- receiving a file transfer, building an environment.  It is
not exec'd code: it claims no exec slot, ships no source, and runs whatever
the worker's own installed execnet implements.

The core knows only this much of it.  A request is one
``GATEWAY_SERVICE`` frame carrying ``(name, request)``; the worker looks
the name up here and spawns the handler it finds.  Everything else -- what
the names mean, what the payloads contain, what the conversation on the
channel looks like -- belongs to whichever package registered them, which
is why :mod:`execnet._deploy` can be lifted out of this one without the
protocol core noticing.

Handlers are named as import strings and imported when a request for them
arrives.  A coordinator never imports a handler at all, and a worker
imports only what it is actually asked for.

Out of tree services register the same way::

    execnet._services.register("myco.thing", "myco.execnet_thing:serve")

on both ends -- the coordinator to name it in a request, the worker to
resolve it.  Nothing in execnet has to change to make room.
"""

from __future__ import annotations

import importlib
from collections.abc import Awaitable
from collections.abc import Callable
from typing import TYPE_CHECKING
from typing import Any

from ._message import Message
from ._serialize import dumps_internal

if TYPE_CHECKING:
    from ._trio_gateway import AsyncChannel
    from ._trio_gateway import AsyncGateway

#: A service handler: ``(gateway, channelid, request) -> awaited task``.  It
#: runs on the worker's loop as a task of whichever nursery that worker's
#: entry point owns, and must contain its own failures -- an exception
#: leaving it ends ``trio.run`` and takes every gateway in the process with
#: it.  Report on the request's channel instead.
ServiceHandler = Callable[[Any, int, Any], Awaitable[None]]

#: name -> ``"module.path:attribute"``.  Data, not imports: the one place
#: the core spells a feature's name, and one line to delete when a feature
#: leaves.
_REGISTRY: dict[str, str] = {
    # the file transfer and the deployment steps built on it
    "transfer": "execnet._deploy.serve:receive_transfer",
    "deploy": "execnet._deploy.serve:run_deploy_step",
}


def register(name: str, target: str) -> None:
    """Register ``name`` as served by ``target`` (``"module:attribute"``).

    Both ends need it: the coordinator to name it in a request, the worker
    to resolve one.  Re-registering the same target is fine (importing a
    module twice must not be an error); changing one is not, since the two
    ends would then disagree about what a name means.
    """
    existing = _REGISTRY.get(name)
    if existing is not None and existing != target:
        raise ValueError(
            f"service {name!r} is already registered as {existing!r};"
            " a name means one thing on both ends of a connection"
        )
    _REGISTRY[name] = target


def resolve(name: str) -> ServiceHandler:
    """Import and return the handler for ``name`` (worker side)."""
    try:
        target = _REGISTRY[name]
    except KeyError:
        raise LookupError(
            f"no execnet service named {name!r} on this worker"
            f" (known: {sorted(_REGISTRY)}). A coordinator asking for one"
            " this worker does not have usually means the two are running"
            " different execnet versions."
        ) from None
    module_name, _, attribute = target.partition(":")
    module = importlib.import_module(module_name)
    handler: ServiceHandler = getattr(module, attribute)
    return handler


def request_frame(name: str, request: Any) -> bytes:
    """The ``GATEWAY_SERVICE`` payload for one request."""
    return dumps_internal((name, request))


async def open_service_channel(
    gateway: AsyncGateway,
    name: str,
    request: Any,
    *,
    channelid: int | None = None,
) -> AsyncChannel:
    """Ask ``gateway`` for service ``name``; return the channel it runs on.

    ``channelid`` is for the blocking facade, whose gateway has a *second*
    id allocator: the sync ``ChannelFactory`` and the async gateway's own
    counter both hand out odd ids and would collide.  Allocate from the
    sync factory there and pass the result in, as the via transport does.
    """
    channel = gateway.open_channel(channelid)
    await gateway._send(
        Message.GATEWAY_SERVICE, channel.id, request_frame(name, request)
    )
    return channel


class ServiceTarget:
    """A gateway that services can be requested on.

    The one thing the surfaces disagree about.  Under :mod:`execnet.trio` a
    gateway is an :class:`~execnet._trio_gateway.AsyncGateway` that
    allocates its own channel ids; under the blocking and asyncio surfaces
    it is a sync ``Gateway`` whose ids come from its ``ChannelFactory``,
    and taking them from the async side instead would hand out ids the sync
    side is also handing out.

    Everything above this -- transfers, deployments -- takes one of these
    and never learns which kind it got.
    """

    def __init__(self, gateway: AsyncGateway, allocate_id: Any = None) -> None:
        self.gateway = gateway
        self._allocate_id = allocate_id

    @classmethod
    def from_sync(cls, gateway: Any) -> ServiceTarget:
        """A target for a blocking-surface ``Gateway``."""
        session = gateway._trio_session
        if session is None:
            raise OSError(f"{gateway!r} has no connection to run a service on")
        return cls(session, gateway._channelfactory.allocate_id)

    def __repr__(self) -> str:
        return f"<ServiceTarget {self.gateway.id!r}>"

    async def open(self, name: str, request: Any) -> AsyncChannel:
        channelid = None if self._allocate_id is None else self._allocate_id()
        return await open_service_channel(
            self.gateway, name, request, channelid=channelid
        )

    async def request(self, name: str, request: Any) -> Any:
        """One request, one reply, channel closed."""
        # local import: importing execnet must not load an event loop
        from ._async import current_async

        aio = current_async()
        channel = await self.open(name, request)
        try:
            return await channel.receive()
        finally:
            with aio.shielded():
                await channel.aclose()
