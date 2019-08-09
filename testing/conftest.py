# -*- coding: utf-8 -*-
import subprocess
import sys

import execnet
import py
import pytest
from execnet.gateway_base import get_execmodel
from execnet.gateway_base import WorkerPool

collect_ignore = ["build", "doc/_build"]

rsyncdirs = ["conftest.py", "execnet", "testing", "doc"]

winpymap = {
    "python2.7": r"C:\Python27\python.exe",
    "python3.4": r"C:\Python34\python.exe",
}


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_setup(item):
    if item.fspath.purebasename in ("test_group", "test_info"):
        getspecssh(item.config)  # will skip if no gx given
    yield
    if "pypy" in item.keywords and not item.config.option.pypy:
        py.test.skip("pypy tests skipped, use --pypy to run them.")


@pytest.fixture
def makegateway(request):
    group = execnet.Group()
    request.addfinalizer(lambda: group.terminate(0.5))
    return group.makegateway


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


def getgspecs(config=None):
    if config is None:
        config = py.test.config
    return map(execnet.XSpec, config.getvalueorskip("gspecs"))


def getspecssh(config=None):
    xspecs = getgspecs(config)
    for spec in xspecs:
        if spec.ssh:
            if not py.path.local.sysfind("ssh"):
                py.test.skip("command not found: ssh")
            return spec
    py.test.skip("need '--gx ssh=...'")


def getsocketspec(config=None):
    xspecs = getgspecs(config)
    for spec in xspecs:
        if spec.socket:
            return spec
    py.test.skip("need '--gx socket=...'")


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
    elif "anypython" in metafunc.fixturenames:
        metafunc.parametrize(
            "anypython",
            indirect=True,
            argvalues=("sys.executable", "python2.7", "pypy", "jython"),
        )


def getexecutable(name, cache={}):
    try:
        return cache[name]
    except KeyError:
        if name == "sys.executable":
            return py.path.local(sys.executable)
        executable = py.path.local.sysfind(name)
        if executable:
            if name == "jython":
                popen = subprocess.Popen(
                    [str(executable), "--version"],
                    universal_newlines=True,
                    stderr=subprocess.PIPE,
                )
                out, err = popen.communicate()
                if not err or "2.5" not in err:
                    executable = None
        cache[name] = executable
        return executable


@pytest.fixture
def anypython(request):
    name = request.param
    executable = getexecutable(name)
    if executable is None:
        if sys.platform == "win32":
            executable = winpymap.get(name, None)
            if executable:
                executable = py.path.local(executable)
                if executable.check():
                    return executable
                executable = None
        py.test.skip("no {} found".format(name))
    if "execmodel" in request.fixturenames and name != "sys.executable":
        backend = request.getfixturevalue("execmodel").backend
        if backend != "thread":
            pytest.xfail("cannot run {!r} execmodel with bare {}".format(backend, name))
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
            gw = group.makegateway("ssh={}//id=ssh".format(sshhost))
        elif request.param == "proxy":
            group.makegateway("popen//id=proxy-transport")
            gw = group.makegateway(
                "popen//via=proxy-transport//id=proxy"
                "//execmodel=%s" % execmodel.backend
            )
        else:
            assert 0, "unknown execmodel: {}".format(request.param)
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
