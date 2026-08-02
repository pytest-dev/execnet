"""What a deployment *is*, and the blocking way to run one.

No trio here: this module is on the ``import execnet`` path, and importing
execnet must not load an event loop.  The async half
(:mod:`execnet._deploy._run`) is imported when somebody deploys.
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from ._manifest import Filter

if TYPE_CHECKING:
    from .._gateway import Gateway
    from ._transfer import Progress

__all__ = ["Deployed", "Deployment", "transfer"]

#: where a workspace lands when the caller does not name a directory.  Not
#: a shell fragment: the worker expands it, in Python, on the host whose
#: home directory it is.
DEFAULT_WORKSPACE_ROOT = "~/.cache/execnet/workspaces"


def _slug(text: str) -> str:
    """Filesystem-safe name for a workspace."""
    import re

    return re.sub(r"[^0-9A-Za-z._-]+", "-", text).strip("-") or "workspace"


class Deployed:
    """Where a :class:`Deployment` put things, and how to reach them.

    ``paths`` maps each local root to the directory it landed in.  Use
    :meth:`translate` for a path inside one -- a caller rewriting its own
    configuration for the remote (arguments, ``rootdir``, data files) needs
    that, and only the deployment knows the answer.
    """

    def __init__(self, workspace: str, python: str, paths: dict[str, str]) -> None:
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
        returning it unchanged would hand the remote a path that may well
        exist there and mean something entirely different.
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

    Provisioning has to happen *before* the process that runs the tests
    exists, because that process has to be running inside the environment
    the project was installed into.  So a deployment is driven through a
    gateway of its own and the workers come afterwards::

        bootstrap = group.makegateway("ssh=host")
        target = execnet.Deployment(".", roots=["testing"]).deploy(bootstrap)
        bootstrap.exit()

        for _ in range(4):
            group.makegateway(f"ssh=host//{target.spec}")

    ``project`` is a directory with a ``pyproject.toml`` and a ``uv.lock``
    -- the lockfile is what makes the remote environment reproducible, and
    its absence is an error rather than a resolve.

    ``roots`` are the local paths a test run needs that the built wheel
    does not contain: tests, ``conftest.py``, fixture data.  A directory
    root lands under the workspace as its own basename; a file root lands
    directly in the workspace.  Either way :attr:`Deployed.paths` says
    where.

    ``name`` names the workspace.  Deployments sharing a name share a
    directory on the host, which is the point on a cluster: the second
    gateway to a machine re-uses the environment the first one built, and
    only what changed is transferred.
    """

    def __init__(
        self,
        project: str | os.PathLike[str],
        roots: Iterable[str | os.PathLike[str]] = (),
        *,
        name: str | None = None,
        workspace: str | None = None,
        filter: Filter | None = None,
        delete: bool = False,
        progress: Progress | None = None,
    ) -> None:
        self.project = Path(project).resolve()
        self.roots = [Path(root).resolve() for root in roots]
        self.name = name or _slug(self.project.name)
        #: an explicit remote directory, or None to derive one from the name
        self.workspace = workspace
        self.workspace_root = DEFAULT_WORKSPACE_ROOT
        #: which paths under a root belong in the transfer
        self.filter = filter
        #: whether a root's remote copy is pruned of what the local one lost
        self.delete = delete
        #: ``(relpath, size)`` as each file body is sent, off the loop
        self.progress = progress
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
        """Deploy through ``gateway``; blocks until the host is ready.

        The gateway is used and left alone -- it is not the one that will
        run anything.  Start the workers afterwards, against
        :attr:`Deployed.spec`.
        """
        return self.deploy_all([gateway])[0]

    def deploy_all(self, gateways: Sequence[Gateway]) -> list[Deployed]:
        """Deploy to every gateway at once; blocks until all are ready.

        One result per gateway, in order.  The wheel is built once and the
        hosts are worked on concurrently, which is the difference between
        deploying to a cluster and deploying to a cluster N times.
        """
        from ._facade import run_blocking
        from ._run import deploy_to

        return run_blocking(gateways, deploy_to, self)


def transfer(
    gateway: Gateway,
    source: str | os.PathLike[str],
    destination: str,
    *,
    filter: Filter | None = None,
    delete: bool = False,
    progress: Progress | None = None,
) -> None:
    """Copy the tree at ``source`` to ``destination`` on ``gateway``.

    Blocking.  Only what differs is sent: the target answers the file list
    with what it is missing, plus a digest for anything whose size matches
    but whose timestamp does not.
    """
    from ._facade import run_blocking
    from ._transfer import transfer_tree_to_all

    async def run(targets: Sequence[object]) -> None:
        await transfer_tree_to_all(
            [(targets[0], destination)],  # type: ignore[list-item]
            source,
            filter=filter,
            delete=delete,
            progress=progress,
        )

    run_blocking([gateway], run)
