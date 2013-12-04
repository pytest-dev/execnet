import execnet
import py
import pytest
import sys
import subprocess
from execnet.gateway_base import get_execmodel, WorkerPool

collect_ignore = ['build', 'doc/_build']

rsyncdirs = ['conftest.py', 'execnet', 'testing', 'doc']

winpymap = {
    'python2.7': r'C:\Python27\python.exe',
    'python2.6': r'C:\Python26\python.exe',
    'python3.1': r'C:\Python31\python.exe',
    'python3.2': r'C:\Python32\python.exe',
    'python3.3': r'C:\Python33\python.exe',
    'python3.4': r'C:\Python34\python.exe',
}

def pytest_runtest_setup(item, __multicall__):
    if item.fspath.purebasename in ('test_group', 'test_info'):
        getspecssh(item.config) # will skip if no gx given
    res = __multicall__.execute()
    if 'pypy' in item.keywords and not item.config.option.pypy:
        py.test.skip("pypy tests skipped, use --pypy to run them.")
    return res

@pytest.fixture
def makegateway(request):
    group = execnet.Group()
    request.addfinalizer(lambda: group.terminate(0.5))
    return group.makegateway

pytest_plugins = ['pytester', 'doctest']
# configuration information for tests
def pytest_addoption(parser):
    group = parser.getgroup("execnet", "execnet testing options")
    group.addoption('--gx',
           action="append", dest="gspecs", default=None,
           help=("add a global test environment, XSpec-syntax. "))
    group.addoption('--gwscope',
           action="store", dest="scope", default="session",
           type="choice", choices=["session", "function"],
           help=("set gateway setup scope, default: session."))
    group.addoption('--pypy', action="store_true", dest="pypy",
           help=("run some tests also against pypy"))
    group.addoption('--broken-isp', action="store_true", dest="broken_isp",
            help=("Skips tests that assume your ISP doesn't put up a landing "
                "page on invalid addresses"))

def pytest_report_header(config):
    lines = []
    lines.append("gateway test setup scope: %s" % config.getvalue("scope"))
    lines.append("execnet: %s -- %s" %(execnet.__file__, execnet.__version__))
    return lines

def pytest_funcarg__specssh(request):
    return getspecssh(request.config)
def pytest_funcarg__specsocket(request):
    return getsocketspec(request.config)
def getgspecs(config=None):
    if config is None:
        config = py.test.config
    return [execnet.XSpec(spec)
                for spec in config.getvalueorskip("gspecs")]

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
    if 'gw' in metafunc.funcargnames:
        assert 'anypython' not in metafunc.funcargnames, "need combine?"
        if hasattr(metafunc.function, 'gwtypes'):
            gwtypes = metafunc.function.gwtypes
        elif hasattr(metafunc.cls, 'gwtype'):
            gwtypes = [metafunc.cls.gwtype]
        else:
            gwtypes = ['popen', 'socket', 'ssh', 'proxy']
        metafunc.parametrize("gw", gwtypes, indirect=True)
    elif 'anypython' in metafunc.funcargnames:
        metafunc.parametrize("anypython", indirect=True, argvalues=
            ('sys.executable', 'python3.3', 'python3.2',
             'python2.6', 'python2.7', 'pypy', 'jython')
        )

def getexecutable(name, cache={}):
    try:
        return cache[name]
    except KeyError:
        if name == 'sys.executable':
            return py.path.local(sys.executable)
        executable = py.path.local.sysfind(name)
        if executable:
            if name == "jython":
                popen = subprocess.Popen([str(executable), "--version"],
                    universal_newlines=True, stderr=subprocess.PIPE)
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
        py.test.skip("no %s found" % (name,))
    if "execmodel" in request.fixturenames and name != 'sys.executable':
        backend = request.getfuncargvalue("execmodel").backend
        if backend != "thread":
            pytest.xfail("cannot run %r execmodel with bare %s" % (backend, name))
    return executable

@pytest.fixture
def gw(request, execmodel):
    scope = request.config.option.scope
    group = request.cached_setup(
        setup=execnet.Group,
        teardown=lambda group: group.terminate(timeout=1),
        extrakey="testgroup",
        scope=scope,
    )
    try:
        return group[request.param]
    except KeyError:
        if request.param == "popen":
            gw = group.makegateway("popen//id=popen//execmodel=%s"
                                   % execmodel.backend)
        elif request.param == "socket":
            #if execmodel.backend != "thread":
            #    pytest.xfail("cannot set remote non-thread execmodel for sockets")
            pname = 'sproxy1'
            if pname not in group:
                proxygw = group.makegateway("popen//id=%s" % pname)
            #assert group['proxygw'].remote_status().receiving
            gw = group.makegateway("socket//id=socket//installvia=%s"
                                   "//execmodel=%s" % (pname, execmodel.backend))
            gw.proxygw = proxygw
            assert pname in group
        elif request.param == "ssh":
            sshhost = request.getfuncargvalue('specssh').ssh
            # we don't use execmodel.backend here
            # but you can set it when specifying the ssh spec
            gw = group.makegateway("ssh=%s//id=ssh" % (sshhost,))
        elif request.param == 'proxy':
            group.makegateway('popen//id=proxy-transport')
            gw = group.makegateway('popen//via=proxy-transport//id=proxy'
                                   '//execmodel=%s' % execmodel.backend)
        return gw


@pytest.fixture(params=["thread", "eventlet", "gevent"], scope="session")
def execmodel(request):
    if request.param != "thread":
        pytest.importorskip(request.param)
    if sys.platform == "win32":
             pytest.xfail("eventlet/gevent do not work onwin32")
    return get_execmodel(request.param)


@pytest.fixture
def pool(execmodel):
    return WorkerPool(execmodel=execmodel)
