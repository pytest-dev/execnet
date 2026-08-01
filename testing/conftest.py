from __future__ import annotations

import shutil
import sys
from collections.abc import Callable
from collections.abc import Generator
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache

import pytest

import execnet
from execnet import Gateway
from execnet import _provision
from execnet._execmodel import ExecModel
from execnet._execmodel import get_execmodel

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
    group.addoption(
        "--stress",
        action="store",
        dest="stress",
        default=None,
        metavar="N",
        help=(
            "how hard the Hypothesis stress tests try: number of examples "
            "per test (e.g. --stress=500). Without it a quick profile runs."
        ),
    )


def pytest_configure(config: pytest.Config) -> None:
    # Register Hypothesis profiles scaled by --stress.  The stress tests reuse
    # a function-scoped gateway across examples on purpose (spawning one per
    # example would dominate the runtime), and each round-trip can be slow, so
    # the health checks for those are suppressed.
    try:
        from hypothesis import HealthCheck
        from hypothesis import settings
    except ImportError:
        return
    suppress = [HealthCheck.function_scoped_fixture, HealthCheck.too_slow]
    settings.register_profile(
        "execnet-quick", max_examples=15, deadline=None, suppress_health_check=suppress
    )
    stress = config.getoption("stress")
    if stress is not None:
        settings.register_profile(
            "execnet-stress",
            max_examples=int(stress),
            deadline=None,
            suppress_health_check=suppress,
        )
        settings.load_profile("execnet-stress")
    else:
        settings.load_profile("execnet-quick")


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
    return executable


@pytest.fixture(scope="session")
def group() -> Iterator[execnet.Group]:
    g = execnet.Group()
    yield g
    g.terminate(timeout=1)


@pytest.fixture
def gw(
    request: pytest.FixtureRequest,
    profile: str,
    group: execnet.Group,
) -> Gateway:
    try:
        return group[request.param]
    except KeyError:
        if request.param == "popen":
            gw = group.makegateway("popen//id=popen//profile=%s" % profile)
        elif request.param == "socket":
            if not _provision.socket_handoff_available():
                # the server accepts the connection and must then give it to
                # a worker process; where neither pass_fds nor a working
                # socket.share() exists (PyPy on Windows) there is no way to
                pytest.skip("this interpreter cannot hand a socket to a worker")
            pname = "sproxy1"
            if pname not in group:
                proxygw = group.makegateway("popen//id=%s" % pname)
            # assert group['proxygw'].remote_status().receiving
            gw = group.makegateway(
                f"socket//id=socket//installvia={pname}//profile={profile}"
            )
            # TODO(typing): Clarify this assignment.
            gw.proxygw = proxygw  # type: ignore[attr-defined]
            assert pname in group
        elif request.param == "ssh":
            sshhost = request.getfixturevalue("specssh").ssh
            # the profile is not forced here; set it in the ssh spec instead
            gw = group.makegateway(f"ssh={sshhost}//id=ssh")
        elif request.param == "proxy":
            group.makegateway("popen//id=proxy-transport")
            gw = group.makegateway(
                "popen//via=proxy-transport//id=proxy//profile=%s" % profile
            )
        else:
            assert 0, f"unknown gateway type: {request.param}"
        return gw


@pytest.fixture(params=["thread"], scope="session")
def profile(request: pytest.FixtureRequest) -> str:
    """The worker profile gateways in this test run are created with."""
    param: str = request.param
    return param


@pytest.fixture(scope="session")
def execmodel(profile: str) -> ExecModel:
    """The deprecated ExecModel shim for ``profile`` (pytest-xdist compat)."""
    return get_execmodel(profile)


@pytest.fixture
def executor() -> Iterator[ThreadPoolExecutor]:
    with ThreadPoolExecutor() as tpe:
        yield tpe
