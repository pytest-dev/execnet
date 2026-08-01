"""Deploying a project to a host, so workers can be started against it.

The shape this exists for: a test run on machines that do not share a
filesystem with the coordinator.  Provisioning has to happen *before* the
process that runs the tests exists, because that process has to be running
inside the environment the project was installed into -- so it is done
through a gateway of its own, and the workers come afterwards::

    bootstrap = group.makegateway("ssh=host")
    target = execnet.Deployment(project=".", roots=["testing"]).deploy(bootstrap)
    bootstrap.exit()

    for _ in range(4):
        group.makegateway(f"ssh=host//{target.spec}")

Three steps, in the one order that works:

1. a **frozen environment** -- ``uv sync --frozen --no-install-project``
   from the project's own ``uv.lock``, so the remote resolves nothing and
   gets exactly what the coordinator's lockfile pins;
2. the **artifact** -- a wheel built from the project here and installed
   there, rather than the source tree, so what runs remotely is what the
   project actually ships;
3. the **rest** -- everything a test run needs that the wheel does not
   contain (tests, ``conftest.py``, fixture data), rsynced into the
   workspace.

Step 3 is why the wheel is not enough on its own, and why the deployment
hands back a :class:`Deployed` whose ``paths`` map local roots to where
they landed: the remote layout is a provisioning fact, and the caller --
which knows only local paths -- should not have to reconstruct it by
convention.

Everything travels over the gateway's own protocol: the transfer is
:class:`~execnet.RSync`, and the install is a ``GATEWAY_DEPLOY`` request
the worker serves (:mod:`execnet._deploy_serve`).  No second connection,
no second set of credentials, and it works over any transport a gateway
does -- which is what makes the same code reach a container or a pod.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Iterable
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING
from typing import Any

if TYPE_CHECKING:
    from ._gateway import Gateway

__all__ = ["Deployed", "Deployment"]

#: where a workspace lands when the caller does not name a directory.  A
#: shell-free path -- the worker expands it, in Python, on the host that
#: has the home directory in question.
DEFAULT_WORKSPACE_ROOT = "~/.cache/execnet/workspaces"


def _slug(text: str) -> str:
    """Filesystem-safe name for a workspace."""
    return re.sub(r"[^0-9A-Za-z._-]+", "-", text).strip("-") or "workspace"


def build_project_wheel(project: Path, outdir: Path) -> Path:
    """Build a wheel of ``project`` into ``outdir`` and return its path.

    The path comes from what ``uv build`` reports rather than being
    assembled from the project name and version: a dirty tree gets a
    ``.dYYYYMMDD`` local version that the caller cannot predict.
    """
    from ._provision import _parse_built_wheel

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


class Deployed:
    """Where a :class:`Deployment` put things, and how to reach them.

    ``paths`` maps each local root to the directory it landed in.  Use
    :meth:`translate` for a path inside one of them -- a caller rewriting
    its own configuration for the remote (arguments, ``rootdir``, data file
    locations) needs that, and only the deployment knows the answer.
    """

    def __init__(
        self,
        workspace: str,
        python: str,
        paths: dict[str, str],
    ) -> None:
        #: the remote workspace directory
        self.workspace = workspace
        #: the remote interpreter the project is installed into
        self.python = python
        #: local root -> remote directory
        self.paths = paths

    def __repr__(self) -> str:
        return f"<Deployed workspace={self.workspace!r} python={self.python!r}>"

    @property
    def spec(self) -> str:
        """Spec keys that point a gateway at this deployment.

        Append to whichever transport reaches the host::

            group.makegateway(f"ssh=host//{target.spec}")
        """
        return f"python={self.python}//chdir={self.workspace}"

    def translate(self, path: str | os.PathLike[str]) -> str:
        """The remote path for a local one under a deployed root.

        Raises :class:`ValueError` for a path that was never deployed --
        silently returning it unchanged would hand the remote a path that
        happens to exist there and means something else.
        """
        local = os.path.abspath(os.fspath(path))
        for root, remote in self.paths.items():
            if local == root:
                return remote
            prefix = root.rstrip(os.sep) + os.sep
            if local.startswith(prefix):
                rest = local[len(prefix) :].replace(os.sep, "/")
                return f"{remote}/{rest}"
        raise ValueError(
            f"{local!r} is not under any deployed root ({sorted(self.paths)})"
        )


class Deployment:
    """A project plus the files around it, ready to be put on a host.

    ``project`` is a directory with a ``pyproject.toml`` and a ``uv.lock``
    -- the lockfile is what makes the remote environment reproducible, and
    its absence is an error rather than a resolve.

    ``roots`` are the local paths a test run needs that the built wheel does
    not contain.  Each lands under the workspace as its own basename, which
    is the layout :meth:`Deployed.translate` reports.

    ``name`` names the workspace.  Deployments sharing a name share a
    directory on the host, which is the point on a cluster: the second
    gateway to a machine re-uses the environment the first one built, and
    rsync only moves what changed.
    """

    def __init__(
        self,
        project: str | os.PathLike[str],
        roots: Iterable[str | os.PathLike[str]] = (),
        *,
        name: str | None = None,
        workspace: str | None = None,
        verbose: bool = False,
    ) -> None:
        self.project = Path(project).resolve()
        self.roots = [Path(root).resolve() for root in roots]
        self.name = name or _slug(self.project.name)
        #: an explicit remote directory, or None to derive one from the name
        self.workspace = workspace
        self.verbose = verbose
        if not (self.project / "pyproject.toml").is_file():
            raise ValueError(f"no pyproject.toml in {self.project}")
        if not (self.project / "uv.lock").is_file():
            raise ValueError(
                f"no uv.lock in {self.project}: a deployment installs a frozen"
                " environment, so the lockfile is what it deploys.  Run"
                " `uv lock` in the project first."
            )
        for root in self.roots:
            if not root.exists():
                raise ValueError(f"no such root: {root}")

    def __repr__(self) -> str:
        return f"<Deployment {self.name!r} project={str(self.project)!r}>"

    def deploy(self, gateway: Gateway) -> Deployed:
        """Deploy through ``gateway`` and return where everything landed.

        The gateway is used and left alone -- it is not the one that will
        run anything.  Start the workers afterwards, against
        :attr:`Deployed.spec`.
        """
        workspace = self._prepare(gateway)
        with tempfile.TemporaryDirectory(prefix="execnet-deploy-") as staging:
            wheels = self._stage(Path(staging))
            self._transfer(gateway, Path(staging), workspace)
            paths = self._transfer_roots(gateway, workspace)
            python = self._install(gateway, workspace, wheels)
        return Deployed(workspace, python, paths)

    # -- the steps --

    def _prepare(self, gateway: Gateway) -> str:
        """Ask the host for the workspace directory, creating it."""
        reply = _request(
            gateway,
            {
                "step": "prepare",
                "workspace": self.workspace,
                "root": DEFAULT_WORKSPACE_ROOT,
                "name": self.name,
            },
        )
        return str(reply["workspace"])

    def _stage(self, staging: Path) -> list[str]:
        """Assemble what the environment is built from, as one directory.

        The lockfile and the wheels go together because they are installed
        together, and shipping them as one tree means one rsync rather than
        a transfer per file.
        """
        from . import _provision

        shutil.copy2(self.project / "pyproject.toml", staging / "pyproject.toml")
        shutil.copy2(self.project / "uv.lock", staging / "uv.lock")
        # file roots (a conftest.py, a tox.ini) ride along in the staging
        # tree rather than each buying its own transfer
        for root in self.roots:
            if root.is_file():
                shutil.copy2(root, staging / root.name)
        dist = staging / "dist"
        dist.mkdir()
        wheels = [build_project_wheel(self.project, dist)]

        # execnet itself has to be in the deployed environment: the workers
        # started against it are execnet workers.  A released coordinator
        # could name a requirement instead, but shipping the wheel we
        # already have keeps the remote off the index entirely -- and a dev
        # coordinator has no requirement to name.
        execnet_wheel = _provision.provisioning_wheel()
        if execnet_wheel is not None:
            shutil.copy2(execnet_wheel, dist / execnet_wheel.name)
            wheels.append(dist / execnet_wheel.name)
        return [f"dist/{wheel.name}" for wheel in wheels] + (
            [] if execnet_wheel is not None else [_execnet_requirement()]
        )

    def _transfer(self, gateway: Gateway, staging: Path, workspace: str) -> None:
        from ._rsync import RSync

        rsync = RSync(staging, verbose=self.verbose)
        rsync.add_target(gateway, workspace)
        rsync.send()

    def _transfer_roots(self, gateway: Gateway, workspace: str) -> dict[str, str]:
        """Copy each directory root under the workspace, and report where.

        File roots need no transfer of their own -- :meth:`_stage` put them
        in the staging tree, so they arrived with it.  Either way a root
        lands under the workspace as its own basename, which is the one
        rule a caller has to know.
        """
        from ._rsync import RSync

        paths: dict[str, str] = {}
        for root in self.roots:
            remote = f"{workspace}/{root.name}"
            if root.is_dir():
                rsync = RSync(root, verbose=self.verbose)
                rsync.add_target(gateway, remote)
                rsync.send()
            paths[str(root)] = remote
        return paths

    def _install(
        self, gateway: Gateway, workspace: str, wheels: Sequence[str]
    ) -> str:
        reply = _request(
            gateway,
            {
                "step": "install",
                "workspace": workspace,
                "wheels": list(wheels),
            },
        )
        return str(reply["python"])


def _execnet_requirement() -> str:
    import execnet

    return f"execnet=={execnet.__version__}"


def _request(gateway: Gateway, request: dict[str, Any]) -> dict[str, Any]:
    """One ``GATEWAY_DEPLOY`` round trip."""
    channel = gateway._request_deploy(request)
    reply = channel.receive()
    channel.waitclose()
    assert isinstance(reply, dict)
    return reply
