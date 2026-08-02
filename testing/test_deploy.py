"""Deploying a project to a host before any worker runs against it.

The order is the whole point: the process that runs the tests has to be
*inside* the environment the project was installed into, so provisioning
happens through a gateway of its own and the workers come afterwards.

These run against a synthetic project over a popen gateway.  That is not a
weaker test than a remote one would be -- the deployment never touches a
transport, it only drives rsync and ``GATEWAY_DEPLOY`` over whatever
gateway it is handed -- and it keeps the suite free of a network.
"""

from __future__ import annotations

import pathlib
import shutil
import subprocess
import sys
from collections.abc import Iterator
from typing import Any

import pytest

import execnet
from execnet import _provision
from execnet._deploy import Deployment
from execnet._deploy._api import DEFAULT_WORKSPACE_ROOT


def _deploy_request(
    gateway: execnet.Gateway, request: dict[str, Any]
) -> dict[str, Any]:
    """One raw deploy-service round trip, for tests that need a step alone."""
    from execnet._deploy._facade import run_blocking
    from execnet._deploy._run import SERVICE

    async def run(targets: Any) -> Any:
        return await targets[0].request(SERVICE, request)

    reply: dict[str, Any] = run_blocking([gateway], run)
    return reply


TESTTIMEOUT = 300.0

needs_uv = pytest.mark.skipif(
    not _provision.uv_available(), reason="a deployment is built with uv"
)
needs_provisioning = pytest.mark.skipif(
    not _provision.provisioning_available(),
    reason="no execnet wheel to deploy into the environment",
)

PYPROJECT = """\
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "deployed-demo"
version = "0.1.0"
requires-python = ">=3.10"
dependencies = []
"""


@pytest.fixture(scope="module")
def project(tmp_path_factory: pytest.TempPathFactory) -> pathlib.Path:
    """A locked, buildable project with tests that are not in its wheel."""
    root = tmp_path_factory.mktemp("project")
    (root / "pyproject.toml").write_text(PYPROJECT)
    package = root / "src" / "deployed_demo"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("VALUE = 42\n")
    tests = root / "tests"
    tests.mkdir()
    (tests / "test_demo.py").write_text(
        "from deployed_demo import VALUE\n\n\ndef test_value():\n    assert VALUE == 42\n"
    )
    (root / "conftest.py").write_text("# marker file, not part of the wheel\n")
    subprocess.run(["uv", "lock"], cwd=root, check=True, capture_output=True)
    return root


@pytest.fixture
def group() -> Iterator[execnet.Group]:
    group = execnet.Group()
    try:
        yield group
    finally:
        group.terminate(timeout=30.0)


class TestDeploymentInputs:
    def test_a_project_without_a_lockfile_is_refused(self, tmp_path) -> None:
        # a deployment installs a *frozen* environment; without the lockfile
        # there is nothing frozen to install, and resolving on the remote
        # would silently deploy something else
        (tmp_path / "pyproject.toml").write_text(PYPROJECT)
        with pytest.raises(ValueError, match=r"uv\.lock"):
            Deployment(tmp_path)

    def test_a_directory_without_a_project_is_refused(self, tmp_path) -> None:
        with pytest.raises(ValueError, match=r"pyproject\.toml"):
            Deployment(tmp_path)

    def test_a_missing_root_is_refused(self, project) -> None:
        with pytest.raises(ValueError, match="no such root"):
            Deployment(project, roots=[project / "nope"])

    def test_the_workspace_name_defaults_to_the_project(self, project) -> None:
        assert Deployment(project).name == project.name


@needs_uv
@needs_provisioning
class TestDeploy:
    def test_the_workers_run_against_what_was_deployed(
        self, project: pathlib.Path, group: execnet.Group, tmp_path: pathlib.Path
    ) -> None:
        deployment = Deployment(
            project, roots=[project / "tests"], workspace=str(tmp_path / "ws")
        )
        bootstrap = group.makegateway("popen//id=bootstrap")
        target = deployment.deploy(bootstrap)
        bootstrap.exit()

        # the interpreter is the deployed environment's, not this one's
        assert target.python != sys.executable
        worker = group.makegateway(f"popen//id=worker//{target.spec}")
        try:
            channel = worker.remote_exec(
                "import deployed_demo, sys\n"
                "channel.send((deployed_demo.VALUE, sys.executable))"
            )
            value, executable = channel.receive(TESTTIMEOUT)
            assert value == 42
            assert executable == target.python
        finally:
            worker.exit()

    def test_the_workers_are_spawned_through_the_host_it_deployed_on(
        self, project: pathlib.Path, group: execnet.Group, tmp_path: pathlib.Path
    ) -> None:
        """The usual shape: one connection per machine.

        The gateway a deployment runs through stays on as the ``via`` host,
        and the test workers are its local children rather than N more
        connections to the same box.  Deploying first and running after is
        also what keeps the two off each other -- the transfer is done with
        that host's loop before it starts relaying.
        """
        deployment = Deployment(
            project, roots=[project / "tests"], workspace=str(tmp_path / "ws")
        )
        host = group.makegateway("popen//id=viahost")
        target = deployment.deploy(host)

        workers = [
            group.makegateway(f"via=viahost//{target.spec}//id=w{index}")
            for index in range(3)
        ]
        for worker in workers:
            channel = worker.remote_exec(
                "import deployed_demo, os, sys\n"
                "channel.send((deployed_demo.VALUE, sys.executable, os.getcwd()))"
            )
            value, executable, cwd = channel.receive(TESTTIMEOUT)
            assert value == 42
            assert executable == target.python
            assert cwd == target.workspace

    def test_it_carries_what_the_wheel_does_not(
        self, project: pathlib.Path, group: execnet.Group, tmp_path: pathlib.Path
    ) -> None:
        # the reason a wheel is not enough: a test run needs the tests, and
        # they are deliberately not in the artifact
        deployment = Deployment(
            project,
            roots=[project / "tests", project / "conftest.py"],
            workspace=str(tmp_path / "ws"),
        )
        gateway = group.makegateway("popen//id=deploy-roots")
        target = deployment.deploy(gateway)

        remote_tests = target.paths[str(project / "tests")]
        assert (pathlib.Path(remote_tests) / "test_demo.py").is_file()
        # a directory root lands as its own name, a file root directly in
        # the workspace -- one rule, whichever it is
        conftest = target.paths[str(project / "conftest.py")]
        assert conftest == f"{target.workspace}/conftest.py"
        assert pathlib.Path(conftest).is_file()
        # and the caller can translate its own local paths without knowing
        # the remote layout
        assert target.translate(project / "tests" / "test_demo.py") == (
            f"{remote_tests}/test_demo.py"
        )

    def test_an_undeployed_path_does_not_translate(
        self, project: pathlib.Path, group: execnet.Group, tmp_path: pathlib.Path
    ) -> None:
        # returning it unchanged would hand the remote a path that may well
        # exist there and mean something entirely different
        deployment = Deployment(
            project, roots=[project / "tests"], workspace=str(tmp_path / "ws")
        )
        gateway = group.makegateway("popen//id=deploy-translate")
        target = deployment.deploy(gateway)
        with pytest.raises(ValueError, match="not under any deployed root"):
            target.translate("/etc/passwd")

    def test_deploying_twice_reuses_the_workspace(
        self, project: pathlib.Path, group: execnet.Group, tmp_path: pathlib.Path
    ) -> None:
        # the point on a cluster: the second gateway to a machine finds the
        # environment the first one built, and rsync moves only what changed
        deployment = Deployment(project, workspace=str(tmp_path / "ws"))
        gateway = group.makegateway("popen//id=deploy-reuse")
        first = deployment.deploy(gateway)
        second = deployment.deploy(gateway)
        assert first.workspace == second.workspace
        assert first.python == second.python

    def test_a_failing_step_reports_on_its_channel(
        self, project: pathlib.Path, group: execnet.Group
    ) -> None:
        # and leaves the gateway usable: the step is a task on the worker's
        # root nursery, so it has to contain what it raises
        gateway = group.makegateway("popen//id=deploy-failure")
        with pytest.raises(execnet.RemoteError, match="unknown deployment step"):
            _deploy_request(gateway, {"step": "nonsense"})
        assert gateway.remote_exec("channel.send(1)").receive(TESTTIMEOUT) == 1

    def test_the_default_workspace_is_the_hosts_cache(
        self, project: pathlib.Path, group: execnet.Group
    ) -> None:
        # named, not given: the path is expanded on the *host*, where the
        # home directory in question is -- the coordinator cannot know it
        gateway = group.makegateway("popen//id=deploy-default")
        reply = _deploy_request(
            gateway,
            {
                "step": "prepare",
                "workspace": None,
                "root": DEFAULT_WORKSPACE_ROOT,
                "name": "execnet-deploy-default",
            },
        )
        workspace = str(reply["workspace"])
        try:
            assert workspace.endswith("/execnet-deploy-default")
            assert "~" not in workspace
            assert pathlib.Path(workspace).is_dir()
        finally:
            shutil.rmtree(workspace, ignore_errors=True)


@needs_uv
@needs_provisioning
class TestEverySurfaceDeploys:
    """The same deployment, driven from each namespace that can hold a gateway.

    There is one driver -- ``_deploy._run.deploy_to`` -- and four ways in,
    which until now only the blocking one was tested through.  The others
    reach it over their own bridge and had never run at all; ``deploy_all``
    with more than one target had never run from anywhere, so neither had
    the concurrency the docstrings promise or the same-engine check that
    guards it.
    """

    def test_the_blocking_surface(
        self, project: pathlib.Path, group: execnet.Group, tmp_path: pathlib.Path
    ) -> None:
        deployment = Deployment(project, workspace=str(tmp_path / "ws"))
        target = deployment.deploy(group.makegateway("popen//id=sync-deploy"))
        assert target.python != sys.executable
        assert pathlib.Path(target.python).exists()

    def test_the_trio_facade(
        self, project: pathlib.Path, tmp_path: pathlib.Path
    ) -> None:
        import trio

        import execnet.trio

        deployment = Deployment(project, workspace=str(tmp_path / "ws"))

        async def main() -> str:
            async with execnet.trio.AsyncGroup() as group:
                gateway = await group.makegateway("popen//id=trio-deploy")
                target = await execnet.trio.deploy(deployment, gateway)
                return target.python

        python = trio.run(main)
        assert python != sys.executable
        assert pathlib.Path(python).exists()

    def test_the_raw_trio_surface(
        self, project: pathlib.Path, tmp_path: pathlib.Path
    ) -> None:
        import trio

        import execnet.raw_trio

        deployment = Deployment(project, workspace=str(tmp_path / "ws"))

        async def main() -> str:
            async with execnet.raw_trio.open_gateway("popen//id=raw-deploy") as gateway:
                target = await execnet.raw_trio.deploy(deployment, gateway)
                return target.python

        python = trio.run(main)
        assert python != sys.executable
        assert pathlib.Path(python).exists()

    def test_the_asyncio_surface(
        self, project: pathlib.Path, tmp_path: pathlib.Path
    ) -> None:
        import asyncio

        import execnet.aio

        deployment = Deployment(project, workspace=str(tmp_path / "ws"))

        async def main() -> str:
            async with execnet.aio.AsyncGroup() as group:
                gateway = await group.makegateway("popen//id=aio-deploy")
                target = await execnet.aio.deploy(deployment, gateway)
                return target.python

        python = asyncio.run(main())
        assert python != sys.executable
        assert pathlib.Path(python).exists()


@needs_uv
@needs_provisioning
class TestDeployAll:
    """Several targets from one staging build, which nothing exercised."""

    def test_each_target_gets_its_own_workspace(
        self, project: pathlib.Path, group: execnet.Group, tmp_path: pathlib.Path
    ) -> None:
        gateways = [group.makegateway(f"popen//id=fan{n}") for n in range(2)]
        deployments = [
            Deployment(project, workspace=str(tmp_path / f"ws{n}")) for n in range(2)
        ]
        # one deployment object per workspace, but the wheel is built once
        # per deploy_all call, which is the property under test
        first = deployments[0].deploy_all(gateways[:1])
        second = deployments[1].deploy_all(gateways[1:])
        assert first[0].workspace != second[0].workspace
        for result in (*first, *second):
            assert pathlib.Path(result.python).exists()

    def test_one_deployment_reaches_every_gateway(
        self, project: pathlib.Path, group: execnet.Group, tmp_path: pathlib.Path
    ) -> None:
        # the shape a cluster uses: the same workspace name on each machine,
        # here collapsed onto one machine, so they share a workspace
        gateways = [group.makegateway(f"popen//id=all{n}") for n in range(3)]
        deployment = Deployment(project, workspace=str(tmp_path / "shared"))
        results = deployment.deploy_all(gateways)
        assert len(results) == len(gateways)
        assert {result.workspace for result in results} == {str(tmp_path / "shared")}

    def test_deploying_to_no_gateways_is_refused(self, project: pathlib.Path) -> None:
        with pytest.raises(ValueError, match="no gateways"):
            Deployment(project).deploy_all([])

    def test_gateways_from_two_engines_are_refused(
        self, project: pathlib.Path, tmp_path: pathlib.Path
    ) -> None:
        # a fan-out is one task awaiting every gateway's channels, so they
        # have to belong to one engine's run
        from execnet._engine import ProtocolEngine

        other = ProtocolEngine(name="execnet-engine-deploy-second")
        one = execnet.Group()
        two = execnet.Group(engine=other)
        try:
            gateways = [
                one.makegateway("popen//id=e1"),
                two.makegateway("popen//id=e2"),
            ]
            deployment = Deployment(project, workspace=str(tmp_path / "ws"))
            with pytest.raises(ValueError, match=r"same execnet\.ProtocolEngine"):
                deployment.deploy_all(gateways)
        finally:
            one.terminate(timeout=30.0)
            two.terminate(timeout=30.0)
            other.close()
