import execnet
import py

rsyncdirs = ['../execnet', '.']

pytest_plugins = ['pytester', 'doctest']
# configuration information for tests
def pytest_addoption(parser):
    group = parser.getgroup("pylib", "py lib testing options")
    group.addoption('--gx',
           action="append", dest="gspecs", default=None,
           help=("add a global test environment, XSpec-syntax. "))

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
        if hasattr(metafunc.cls, 'gwtype'):
            gwtypes = [metafunc.cls.gwtype]
        else:
            gwtypes = ['popen', 'socket', 'ssh']
        for gwtype in gwtypes:
            metafunc.addcall(id=gwtype, param=gwtype)
    elif 'anypython' in metafunc.funcargnames:
        for name in ('python3.1', 'python2.4', 'python2.5', 'python2.6', 
                     'pypy-c', 'jython'):
            metafunc.addcall(id=name, param=name)

def pytest_funcarg__anypython(request):
    name = request.param
    executable = py.path.local.sysfind(name)
    if executable is None:
        py.test.skip("no %s found" % (name,))
    return executable

def pytest_funcarg__gw(request):
    scope = "session"
    if request.param == "popen":
        return request.cached_setup(
                setup=lambda: execnet.makegateway("popen"),
                teardown=lambda gw: gw.exit(),
                extrakey=request.param,
                scope=scope)
    elif request.param == "socket":
        return request.cached_setup(
            setup=setup_socket_gateway,
            teardown=teardown_socket_gateway,
            extrakey=request.param,
            scope=scope)
    elif request.param == "ssh":
        return request.cached_setup(
            setup=lambda: setup_ssh_gateway(request),
            teardown=lambda gw: gw.exit(),
            extrakey=request.param,
            scope=scope)

def setup_socket_gateway():
    proxygw = execnet.makegateway("popen")
    gw = execnet.makegateway("socket//installvia=%s" % proxygw.id)
    gw.proxygw = proxygw
    return gw

def teardown_socket_gateway(gw):
    gw.exit()
    gw.proxygw.exit()

def setup_ssh_gateway(request):
    sshhost = request.getfuncargvalue('specssh').ssh
    gw = execnet.makegateway("ssh=%s" %(sshhost,))
    return gw

