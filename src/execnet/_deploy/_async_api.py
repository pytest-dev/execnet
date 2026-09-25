"""The async verbs, for the surfaces that have a loop of their own.

:mod:`execnet.trio` awaits these directly on its own gateways.
:mod:`execnet.aio` reaches the same functions through its host bridge, so
there is one implementation and three ways in.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from typing import TYPE_CHECKING
from typing import Any

from .._services import ServiceTarget
from ._manifest import Filter

if TYPE_CHECKING:
    from .._trio_gateway import AsyncGateway
    from ._api import Deployed
    from ._api import Deployment
    from ._transfer import Progress

__all__ = ["deploy", "deploy_all", "transfer"]


def _target(gateway: AsyncGateway | ServiceTarget) -> ServiceTarget:
    """Accept a trio-native gateway or an already-built target."""
    if isinstance(gateway, ServiceTarget):
        return gateway
    return ServiceTarget(gateway)


async def transfer(
    gateway: AsyncGateway | ServiceTarget,
    source: str | os.PathLike[str],
    destination: str,
    *,
    filter: Filter | None = None,
    delete: bool = False,
    progress: Progress | None = None,
) -> None:
    """Copy the tree at ``source`` to ``destination`` on ``gateway``.

    Only what differs is sent: the target answers the file list with what
    it is missing, plus a digest for anything whose size matches but whose
    timestamp does not.
    """
    from ._transfer import transfer_tree

    await transfer_tree(
        _target(gateway),
        source,
        destination,
        filter=filter,
        delete=delete,
        progress=progress,
    )


async def deploy(
    deployment: Deployment, gateway: AsyncGateway | ServiceTarget
) -> Deployed:
    """Deploy through ``gateway`` and return where everything landed."""
    results = await deploy_all(deployment, [gateway])
    return results[0]


async def deploy_all(deployment: Deployment, gateways: Sequence[Any]) -> list[Deployed]:
    """Deploy to every gateway at once; one result each, in order.

    The wheel is built once and the hosts are worked on concurrently --
    the difference between deploying to a cluster and deploying to a
    cluster N times.
    """
    from ._run import deploy_to

    return await deploy_to(deployment, [_target(gateway) for gateway in gateways])
