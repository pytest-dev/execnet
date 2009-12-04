import execnet
import py
import sys

collect_ignore = ['build', 'doc/_build']

rsyncdirs = ['conftest.py', 'execnet', 'testing', 'doc']

winpymap = {
    'python2.6': r'C:\Python26\python.exe',
    'python2.5': r'C:\Python25\python.exe',
    'python2.4': r'C:\Python24\python.exe',
    'python3.1': r'C:\Python31\python.exe',
}

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
        if hasattr(metafunc.function, 'gwtypes'):
            gwtypes = metafunc.function.gwtypes
        elif hasattr(metafunc.cls, 'gwtype'):
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
        if sys.platform == "win32":
            executable = winpymap.get(name, None)
            if executable:
                executable = py.path.local(executable)
                if executable.check():
                    return executable
        py.test.skip("no %s found" % (name,))
    return executable

def pytest_funcarg__gw(request):
    scope = "session"
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
            gw = group.makegateway("popen//id=popen")
        elif request.param == "socket":
            pname = 'sproxy1'
            if pname not in group:
                proxygw = group.makegateway("popen//id=%s" % pname)
            #assert group['proxygw'].remote_status().receiving
            gw = group.makegateway("socket//id=socket//installvia=%s" % pname)
            gw.proxygw = proxygw
            assert pname in group
            
        elif request.param == "ssh":
            sshhost = request.getfuncargvalue('specssh').ssh
            gw = group.makegateway("ssh=%s//id=ssh" %(sshhost,))
        return gw
