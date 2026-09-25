"""The async half of a deployment: stage once, then work per target.

Kept apart from :mod:`execnet._deploy._api` because that module is on the
``import execnet`` path and this one imports trio.  The rule the namespace
tests pin is that importing execnet loads no event loop; a deployment is
the first thing that needs one, and it needs it no sooner than the moment
somebody actually deploys.
"""

from __future__ import annotations

import functools
import shutil
import subprocess
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from .._async import current_async
from . import _transfer

if TYPE_CHECKING:
    from .._services import ServiceTarget
    from ._api import Deployed
    from ._api import Deployment

#: the service that runs the environment steps
SERVICE = "deploy"


def _build_project_wheel(project: Path, outdir: Path) -> Path:
    """Build a wheel of ``project`` into ``outdir`` and return its path.

    The path comes from what ``uv build`` reports rather than being
    assembled from the project name and version: a dirty tree gets a
    ``.dYYYYMMDD`` local version that the caller cannot predict.
    """
    from .._provision import _parse_built_wheel

    proc = subprocess.run(
        ["uv", "build", "--wheel", "-o", str(outdir), str(project)],
        check=True,
        capture_output=True,
        text=True,
    )
    wheel = _parse_built_wheel(proc.stderr)
    if wheel is None or not wheel.exists():
        raise RuntimeError(
            f"could not determine the wheel path from uv build for {project}:\n"
            f"{proc.stderr}"
        )
    return wheel


def stage(deployment: Deployment, staging: Path) -> list[str]:
    """Assemble what the environment is built from, as one directory.

    The lockfile and the wheels go together because they are installed
    together, so shipping them as one tree is one transfer rather than one
    per file.  Blocking -- ``uv build`` is a subprocess -- so it runs in a
    thread, once, however many targets follow.
    """
    from .. import _provision

    shutil.copy2(deployment.project / "pyproject.toml", staging / "pyproject.toml")
    shutil.copy2(deployment.project / "uv.lock", staging / "uv.lock")
    # file roots (a conftest.py, a tox.ini) ride along in the staging tree
    # rather than each buying its own transfer
    for root in deployment.roots:
        if root.is_file():
            shutil.copy2(root, staging / root.name)

    dist = staging / "dist"
    dist.mkdir()
    project_wheel = _build_project_wheel(deployment.project, dist)

    # execnet itself has to be in the deployed environment: the workers
    # started against it are execnet workers.  Shipping the wheel we already
    # have keeps the remote off the index entirely -- and a dev coordinator
    # has no requirement it could name instead.
    execnet_wheel = _provision.provisioning_wheel()
    if execnet_wheel is not None:
        shutil.copy2(execnet_wheel, dist / execnet_wheel.name)
        return [f"dist/{project_wheel.name}", f"dist/{execnet_wheel.name}"]

    import execnet

    return [f"dist/{project_wheel.name}", f"execnet=={execnet.__version__}"]


async def deploy_one(
    deployment: Deployment,
    target: ServiceTarget,
    staging: Path,
    wheels: Sequence[str],
) -> Deployed:
    """Put an already-staged deployment onto one target."""
    from ._api import Deployed

    prepared = await target.request(
        SERVICE,
        {
            "step": "prepare",
            "workspace": deployment.workspace,
            "root": deployment.workspace_root,
            "name": deployment.name,
        },
    )
    workspace = str(prepared["workspace"])

    # the staged tree first: it is what the environment is built from
    await _transfer.transfer_tree(
        target, staging, workspace, progress=deployment.progress
    )
    # then each directory root -- separate trees, so separate walks, but
    # nothing makes them wait for each other
    directories = [root for root in deployment.roots if root.is_dir()]
    async with current_async().task_scope() as scope:
        for root in directories:
            scope.start_soon(
                functools.partial(
                    _transfer.transfer_tree,
                    target,
                    root,
                    f"{workspace}/{root.name}",
                    filter=deployment.filter,
                    delete=deployment.delete,
                    progress=deployment.progress,
                )
            )

    installed = await target.request(
        SERVICE, {"step": "install", "workspace": workspace, "wheels": list(wheels)}
    )
    paths = {str(root): f"{workspace}/{root.name}" for root in deployment.roots}
    return Deployed(workspace, str(installed["python"]), paths)


async def deploy_to(
    deployment: Deployment, targets: Sequence[ServiceTarget]
) -> list[Deployed]:
    """Deploy to every target, concurrently, from one staging build.

    The wheel is built once -- it is the same artifact for every host --
    and each target then prepares, receives and installs in its own task.
    Twenty pods is twenty tasks, not twenty deployments in a row.
    """
    results: list[Deployed | None] = [None] * len(targets)
    with tempfile.TemporaryDirectory(prefix="execnet-deploy-") as directory:
        staging = Path(directory)
        wheels = await current_async().to_thread(stage, deployment, staging)

        async def one(index: int, target: ServiceTarget) -> None:
            results[index] = await deploy_one(deployment, target, staging, wheels)

        if len(targets) == 1:
            await one(0, targets[0])
        else:
            async with current_async().task_scope() as scope:
                for index, target in enumerate(targets):
                    scope.start_soon(one, index, target)
    deployed = [result for result in results if result is not None]
    assert len(deployed) == len(targets)
    return deployed
