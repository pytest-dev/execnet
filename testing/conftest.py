import subprocess
import sys
from functools import lru_cache
from typing import Callable
from typing import Iterator

import execnet.gateway
import pytest
from execnet.gateway_base import get_execmodel
from execnet.gateway_base import WorkerPool

collect_ignore = ["build", "doc/_build"]

rsyncdirs = ["conftest.py", "execnet", "testing", "doc"]


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_setup(item):
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
def makegateway(group_function) -> Callable[[str], execnet.gateway.Gateway]:
    return group_function.makegateway


pytest_plugins = ["pytester", "doctest"]


# configuration information for tests
def pytest_addoption(parser):
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
def specssh(request):
    return getspecssh(request.config)


@pytest.fixture
def specsocket(request):
    return getsocketspec(request.config)


def getgspecs(config):
    return map(execnet.XSpec, config.getvalueorskip("gspecs"))


def getspecssh(config):
    xspecs = getgspecs(config)
    for spec in xspecs:
        if spec.ssh:
            if not shutil.which("ssh"):
                pytest.skip("command not found: ssh")
            return spec
    pytest.skip("need '--gx ssh=...'")


def getsocketspec(config):
    xspecs = getgspecs(config)
    for spec in xspecs:
        if spec.socket:
            return spec
    pytest.skip("need '--gx socket=...'")


def pytest_generate_tests(metafunc):
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
def getexecutable(name):
    if name == "sys.executable":
        return sys.executable
    import shutil

    return shutil.which(name)


@pytest.fixture(params=("sys.executable", "pypy3"))
def anypython(request):
    name = request.param
    executable = getexecutable(name)
    if executable is None:
        pytest.skip(f"no {name} found")
    if "execmodel" in request.fixturenames and name != "sys.executable":
        backend = request.getfixturevalue("execmodel").backend
        if backend != "thread":
            pytest.xfail(f"cannot run {backend!r} execmodel with bare {name}")
    return executable


@pytest.fixture(scope="session")
def group():
    g = execnet.Group()
    yield g
    g.terminate(timeout=1)


@pytest.fixture
def gw(request, execmodel, group):
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
                "socket//id=socket//installvia=%s"
                "//execmodel=%s" % (pname, execmodel.backend)
            )
            gw.proxygw = proxygw
            assert pname in group
        elif request.param == "ssh":
            sshhost = request.getfixturevalue("specssh").ssh
            # we don't use execmodel.backend here
            # but you can set it when specifying the ssh spec
            gw = group.makegateway(f"ssh={sshhost}//id=ssh")
        elif request.param == "proxy":
            group.makegateway("popen//id=proxy-transport")
            gw = group.makegateway(
                "popen//via=proxy-transport//id=proxy"
                "//execmodel=%s" % execmodel.backend
            )
        else:
            assert 0, f"unknown execmodel: {request.param}"
        return gw


@pytest.fixture(params=["thread", "eventlet", "gevent"], scope="session")
def execmodel(request):
    if request.param != "thread":
        pytest.importorskip(request.param)
    if request.param in ("eventlet", "gevent") and sys.platform == "win32":
        pytest.xfail(request.param + " does not work on win32")
    return get_execmodel(request.param)


@pytest.fixture
def pool(execmodel):
    return WorkerPool(execmodel=execmodel)
