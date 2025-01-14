from __future__ import annotations

import shutil
import sys
from functools import lru_cache
from typing import Callable
from typing import Generator
from typing import Iterator

import pytest

import execnet
from execnet.gateway import Gateway
from execnet.gateway_base import ExecModel
from execnet.gateway_base import WorkerPool
from execnet.gateway_base import get_execmodel

collect_ignore = ["build", "doc/_build"]

rsyncdirs = ["conftest.py", "execnet", "testing", "doc"]


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_setup(item: pytest.Item) -> Generator[None, None, None]:
    if item.fspath.purebasename in ("test_group", "test_info"):
        getspecssh(item.config)  # will skip if no gx given
    yield
    if "pypy" in item.keywords and not item.config.option.pypy:
        pytest.skip("pypy tests skipped, use --pypy to run them.")


@pytest.fixture
def group_function() -> Iterator[execnet.Group]:
    group = execnet.Group()
    yield group
    group.terminate(0.5)


@pytest.fixture
def makegateway(group_function: execnet.Group) -> Callable[[str], Gateway]:
    return group_function.makegateway


pytest_plugins = ["pytester", "doctest"]


# configuration information for tests
def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("execnet", "execnet testing options")
    group.addoption(
        "--gx",
        action="append",
        dest="gspecs",
        default=None,
        help="add a global test environment, XSpec-syntax. ",
    )
    group.addoption(
        "--pypy",
        action="store_true",
        dest="pypy",
        help="run some tests also against pypy",
    )
    group.addoption(
        "--broken-isp",
        action="store_true",
        dest="broken_isp",
        help=(
            "Skips tests that assume your ISP doesn't put up a landing "
            "page on invalid addresses"
        ),
    )


@pytest.fixture
def specssh(request: pytest.FixtureRequest) -> execnet.XSpec:
    return getspecssh(request.config)


@pytest.fixture
def specsocket(request: pytest.FixtureRequest) -> execnet.XSpec:
    return getsocketspec(request.config)


def getgspecs(config: pytest.Config) -> list[execnet.XSpec]:
    return [execnet.XSpec(gspec) for gspec in config.getvalueorskip("gspecs")]


def getspecssh(config: pytest.Config) -> execnet.XSpec:
    xspecs = getgspecs(config)
    for spec in xspecs:
        if spec.ssh:
            if not shutil.which("ssh"):
                pytest.skip("command not found: ssh")
            return spec
    pytest.skip("need '--gx ssh=...'")


def getsocketspec(config: pytest.Config) -> execnet.XSpec:
    xspecs = getgspecs(config)
    for spec in xspecs:
        if spec.socket:
            return spec
    pytest.skip("need '--gx socket=...'")


def pytest_generate_tests(metafunc: pytest.Metafunc) -> None:
    if "gw" in metafunc.fixturenames:
        assert "anypython" not in metafunc.fixturenames, "need combine?"
        if hasattr(metafunc.function, "gwtypes"):
            gwtypes = metafunc.function.gwtypes
        elif hasattr(metafunc.cls, "gwtype"):
            gwtypes = [metafunc.cls.gwtype]
        else:
            gwtypes = ["popen", "socket", "ssh", "proxy"]
        metafunc.parametrize("gw", gwtypes, indirect=True)


@lru_cache
def getexecutable(name: str) -> str | None:
    if name == "sys.executable":
        return sys.executable
    return shutil.which(name)


@pytest.fixture(params=("sys.executable", "pypy3"))
def anypython(request: pytest.FixtureRequest) -> str:
    name = request.param
    executable = getexecutable(name)
    if executable is None:
        pytest.skip(f"no {name} found")
    if "execmodel" in request.fixturenames and name != "sys.executable":
        backend = request.getfixturevalue("execmodel").backend
        if backend not in ("thread", "main_thread_only"):
            pytest.xfail(f"cannot run {backend!r} execmodel with bare {name}")
    return executable


@pytest.fixture(scope="session")
def group() -> Iterator[execnet.Group]:
    g = execnet.Group()
    yield g
    g.terminate(timeout=1)


@pytest.fixture
def gw(
    request: pytest.FixtureRequest,
    execmodel: ExecModel,
    group: execnet.Group,
) -> Gateway:
    try:
        return group[request.param]
    except KeyError:
        if request.param == "popen":
            gw = group.makegateway("popen//id=popen//execmodel=%s" % execmodel.backend)
        elif request.param == "socket":
            # if execmodel.backend != "thread":
            #    pytest.xfail(
            #        "cannot set remote non-thread execmodel for sockets")
            pname = "sproxy1"
            if pname not in group:
                proxygw = group.makegateway("popen//id=%s" % pname)
            # assert group['proxygw'].remote_status().receiving
            gw = group.makegateway(
                f"socket//id=socket//installvia={pname}//execmodel={execmodel.backend}"
            )
            # TODO(typing): Clarify this assignment.
            gw.proxygw = proxygw  # type: ignore[attr-defined]
            assert pname in group
        elif request.param == "ssh":
            sshhost = request.getfixturevalue("specssh").ssh
            # we don't use execmodel.backend here
            # but you can set it when specifying the ssh spec
            gw = group.makegateway(f"ssh={sshhost}//id=ssh")
        elif request.param == "proxy":
            group.makegateway("popen//id=proxy-transport")
            gw = group.makegateway(
                "popen//via=proxy-transport//id=proxy//execmodel=%s" % execmodel.backend
            )
        else:
            assert 0, f"unknown execmodel: {request.param}"
        return gw


@pytest.fixture(
    params=["thread", "main_thread_only", "eventlet", "gevent"], scope="session"
)
def execmodel(request: pytest.FixtureRequest) -> ExecModel:
    if request.param not in ("thread", "main_thread_only"):
        pytest.importorskip(request.param)
    if request.param in ("eventlet", "gevent") and sys.platform == "win32":
        pytest.xfail(request.param + " does not work on win32")
    return get_execmodel(request.param)


@pytest.fixture
def pool(execmodel: ExecModel) -> WorkerPool:
    return WorkerPool(execmodel=execmodel)
