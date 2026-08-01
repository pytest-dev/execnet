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

import pytest

import execnet
from execnet import _provision
from execnet._deploy import Deployment

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
def group() -> execnet.Group:
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
        with pytest.raises(ValueError, match="uv.lock"):
            Deployment(tmp_path)

    def test_a_directory_without_a_project_is_refused(self, tmp_path) -> None:
        with pytest.raises(ValueError, match="pyproject.toml"):
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
        channel = gateway._request_deploy({"step": "nonsense"})
        with pytest.raises(execnet.RemoteError, match="unknown deployment step"):
            channel.receive(TESTTIMEOUT)
        assert gateway.remote_exec("channel.send(1)").receive(TESTTIMEOUT) == 1

    def test_the_default_workspace_is_the_hosts_cache(
        self, project: pathlib.Path, group: execnet.Group
    ) -> None:
        # named, not given: the path is expanded on the *host*, where the
        # home directory in question is -- the coordinator cannot know it
        deployment = Deployment(project, name="execnet-deploy-default")
        gateway = group.makegateway("popen//id=deploy-default")
        workspace = deployment._prepare(gateway)
        try:
            assert workspace.endswith("/execnet-deploy-default")
            assert "~" not in workspace
            assert pathlib.Path(workspace).is_dir()
        finally:
            shutil.rmtree(workspace, ignore_errors=True)
