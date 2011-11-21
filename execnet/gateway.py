"""
gateway code for initiating popen, socket and ssh connections.
(c) 2004-2009, Holger Krekel and others
"""

import sys, os, inspect, types, linecache
import textwrap
import execnet
from execnet.gateway_base import Message, Popen2IO, serialize
from execnet import gateway_base
importdir = os.path.dirname(os.path.dirname(execnet.__file__))

class Gateway(gateway_base.BaseGateway):
    """ Gateway to a local or remote Python Intepreter. """

    def __init__(self, io, id):
        super(Gateway, self).__init__(io=io, id=id, _startcount=1)
        self._remote_bootstrap_gateway(io)
        self._initreceive()

    def __repr__(self):
        """ return string representing gateway type and status. """
        try:
            r = (self.hasreceiver() and 'receive-live' or 'not-receiving')
            i = len(self._channelfactory.channels())
        except AttributeError:
            r = "uninitialized"
            i = "no"
        return "<%s id=%r %s, %s active channels>" %(
                self.__class__.__name__, self.id, r, i)

    def exit(self):
        """ trigger gateway exit.  Defer waiting for finishing
        of receiver-thread and subprocess activity to when
        group.terminate() is called.
        """
        self._trace("gateway.exit() called")
        if self not in self._group:
            self._trace("gateway already unregistered with group")
            return
        self._group._unregister(self)
        self._trace("--> sending GATEWAY_TERMINATE")
        try:
            self._send(Message.GATEWAY_TERMINATE)
            self._io.close_write()
        except IOError:
            v = sys.exc_info()[1]
            self._trace("io-error: could not send termination sequence")
            self._trace(" exception: %r" % v)

    def reconfigure(self, py2str_as_py3str=True, py3str_as_py2str=False):
        """
        set the string coercion for this gateway
        the default is to try to convert py2 str as py3 str,
        but not to try and convert py3 str to py2 str
        """
        self._strconfig = (py2str_as_py3str, py3str_as_py2str)
        data = serialize(self._strconfig)
        self._send(Message.RECONFIGURE, data=data)

    def _remote_bootstrap_gateway(self, io):
        """ send gateway bootstrap code to a remote Python interpreter
            endpoint, which reads from io for a string to execute.
        """
        sendexec(io,
            inspect.getsource(gateway_base),
            self._remotesetup,
            "io.write('1'.encode('ascii'))",
            "serve(io, id='%s-slave')" % self.id,
        )
        s = io.read(1)
        assert s == "1".encode('ascii')

    def _rinfo(self, update=False):
        """ return some sys/env information from remote. """
        if update or not hasattr(self, '_cache_rinfo'):
            ch = self.remote_exec(rinfo_source)
            self._cache_rinfo = RInfo(ch.receive())
        return self._cache_rinfo

    def hasreceiver(self):
        """ return True if gateway is able to receive data. """
        return self._receiverthread.isAlive() # approxmimation

    def remote_status(self):
        """ return information object about remote execution status. """
        channel = self.newchannel()
        self._send(Message.STATUS, channel.id)
        statusdict = channel.receive()
        # the other side didn't actually instantiate a channel
        # so we just delete the internal id/channel mapping
        self._channelfactory._local_close(channel.id)
        return RemoteStatus(statusdict)

    def remote_exec(self, source, **kwargs):
        """ return channel object and connect it to a remote
            execution thread where the given ``source`` executes.

            * ``source`` is a string: execute source string remotely
              with a ``channel`` put into the global namespace.
            * ``source`` is a pure function: serialize source and
              call function with ``**kwargs``, adding a
              ``channel`` object to the keyword arguments.
            * ``source`` is a pure module: execute source of module
              with a ``channel`` in its global namespace

            In all cases the binding ``__name__='__channelexec__'``
            will be available in the global namespace of the remotely
            executing code.
        """
        call_name = None
        if isinstance(source, types.ModuleType):
            linecache.updatecache(inspect.getsourcefile(source))
            source = inspect.getsource(source)
        elif isinstance(source, types.FunctionType):
            call_name = source.__name__
            source = _source_of_function(source)
        else:
            source = textwrap.dedent(str(source))

        if call_name is None and kwargs:
            raise TypeError("can't pass kwargs to non-function remote_exec")

        channel = self.newchannel()
        self._send(Message.CHANNEL_EXEC,
                   channel.id,
                   serialize((source, call_name, kwargs)))
        return channel

    def remote_init_threads(self, num=None):
        """ start up to 'num' threads for subsequent
            remote_exec() invocations to allow concurrent
            execution.
        """
        if hasattr(self, '_remotechannelthread'):
            raise IOError("remote threads already running")
        from execnet import threadpool
        source = inspect.getsource(threadpool)
        self._remotechannelthread = self.remote_exec(source)
        self._remotechannelthread.send(num)
        status = self._remotechannelthread.receive()
        assert status == "ok", status

class RInfo:
    def __init__(self, kwargs):
        self.__dict__.update(kwargs)

    def __repr__(self):
        info = ", ".join(["%s=%s" % item
                for item in self.__dict__.items()])
        return "<RInfo %r>" % info

RemoteStatus = RInfo

def rinfo_source(channel):
    import sys, os
    channel.send(dict(
        executable = sys.executable,
        version_info = sys.version_info[:5],
        platform = sys.platform,
        cwd = os.getcwd(),
        pid = os.getpid(),
    ))


def _find_non_builtin_globals(source, codeobj):
    try:
        import ast
    except ImportError:
        return None
    try:
        import __builtin__
    except ImportError:
        import builtins as __builtin__

    vars = dict.fromkeys(codeobj.co_varnames)
    all = []
    for node in ast.walk(ast.parse(source)):
        if (isinstance(node, ast.Name) and node.id not in vars and
            node.id not in __builtin__.__dict__):
            all.append(node.id)
    return all


def _source_of_function(function):
    if function.__name__ == '<lambda>':
        raise ValueError("can't evaluate lambda functions'")
    #XXX: we dont check before remote instanciation
    #     if arguments are used propperly
    args, varargs, keywords, defaults = inspect.getargspec(function)
    if args[0] != 'channel':
        raise ValueError('expected first function argument to be `channel`')

    if sys.version_info < (3,0):
        closure = function.func_closure
        codeobj = function.func_code
    else:
        closure = function.__closure__
        codeobj = function.__code__

    if closure is not None:
        raise ValueError("functions with closures can't be passed")

    try:
        source = inspect.getsource(function)
    except IOError:
        raise ValueError("can't find source file for %s" % function)

    source = textwrap.dedent(source) # just for inner functions

    used_globals = _find_non_builtin_globals(source, codeobj)
    if used_globals:
        raise ValueError(
            "the use of non-builtin globals isn't supported",
            used_globals,
        )

    return source

class PopenCmdGateway(Gateway):
    _remotesetup = "io = init_popen_io()"
    def __init__(self, args, id):
        from subprocess import Popen, PIPE
        self._popen = p = Popen(args, stdin=PIPE, stdout=PIPE)
        io = Popen2IO(p.stdin, p.stdout)
        super(PopenCmdGateway, self).__init__(io=io, id=id)
        # fix for jython 2.5.1
        if p.pid is None:
            p.pid = self.remote_exec(
                "import os; channel.send(os.getpid())").receive()

popen_bootstrapline = "import sys;exec(eval(sys.stdin.readline()))"
class PopenGateway(PopenCmdGateway):
    """ This Gateway provides interaction with a newly started
        python subprocess.
    """
    def __init__(self, id, python=None, spec=None):
        """ instantiate a gateway to a subprocess
            started with the given 'python' executable.
        """
        if not python:
            python = sys.executable
        args = [str(python), '-u']
        if spec is not None and spec.dont_write_bytecode:
            args.append("-B")
        # Slight gymnastics in ordering these arguments because CPython (as of
        # 2.7.1) ignores -B if you provide `python -c "something" -B`
        args.extend(['-c', popen_bootstrapline])
        super(PopenGateway, self).__init__(args, id=id)

    def _remote_bootstrap_gateway(self, io):
        sendexec(io,
                 "import sys",
                 "sys.stdout.write('1')",
                 "sys.stdout.flush()",
                 popen_bootstrapline)
        sendexec(io,
            "import sys ; sys.path.insert(0, %r)" % importdir,
            "from execnet.gateway_base import serve, init_popen_io",
            "serve(init_popen_io(), id='%s-slave')" % self.id,
        )
        s = io.read(1)
        assert s == "1".encode('ascii')

def sendexec(io, *sources):
    source = "\n".join(sources)
    io.write((repr(source)+ "\n").encode('ascii'))

class HostNotFound(Exception):
    pass

class SshGateway(PopenCmdGateway):
    """ This Gateway provides interaction with a remote Python process,
        established via the 'ssh' command line binary.
        The remote side needs to have a Python interpreter executable.
    """
    def __init__(self, sshaddress, id, remotepython=None, ssh_config=None):
        """ instantiate a remote ssh process with the
            given 'sshaddress' and remotepython version.
            you may specify an ssh_config file.
        """
        self.remoteaddress = sshaddress
        if remotepython is None:
            remotepython = "python"
        args = ['ssh', '-C' ]
        if ssh_config is not None:
            args.extend(['-F', str(ssh_config)])
        remotecmd = '%s -c "%s"' %(remotepython, popen_bootstrapline)
        args.extend([sshaddress, remotecmd])
        super(SshGateway, self).__init__(args, id=id)

    def _remote_bootstrap_gateway(self, io):
        try:
            super(SshGateway, self)._remote_bootstrap_gateway(io)
        except EOFError:
            ret = self._popen.wait()
            if ret == 255:
                raise HostNotFound(self.remoteaddress)
