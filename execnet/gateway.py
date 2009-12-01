"""
gateway code for initiating popen, socket and ssh connections.
(c) 2004-2009, Holger Krekel and others
"""

import sys, os, inspect, socket, types
import textwrap
import execnet
from execnet.gateway_base import Message, Popen2IO, SocketIO
from execnet import gateway_base
importdir = os.path.dirname(os.path.dirname(execnet.__file__))

class Gateway(gateway_base.BaseGateway):
    """ Gateway to a local or remote Python Intepreter. """

    def __init__(self, io):
        super(Gateway, self).__init__(io=io, _startcount=1)
        self._remote_bootstrap_gateway(io)
        self._initreceive()

    def __repr__(self):
        """ return string representing gateway type and status. """
        if hasattr(self, 'id'):
            id = self.id
        else:
            id = "???"
        if hasattr(self, 'remoteaddress'):
            addr = '[%s]' % (self.remoteaddress,)
        else:
            addr = ''
        try:
            r = (self._receiverthread.isAlive() and "receive-live" or
                 "not-receiving")
            i = len(self._channelfactory.channels())
        except AttributeError:
            r = "uninitialized"
            i = "no"
        return "<%s%s id=%r %s, %s active channels>" %(
                self.__class__.__name__, addr, id, r, i)

    def exit(self):
        """ trigger gateway exit. """
        self._trace("trigger gateway exit")
        try:
            self._group._unregister(self)
        except KeyError:
            return # we assume it's already happened
        self._trace("stopping exec and closing write connection")
        self._stopexec()
        self._stopsend()
        #self.join(timeout=timeout) # receiverthread receive close() messages

    def _remote_bootstrap_gateway(self, io):
        """ send gateway bootstrap code to a remote Python interpreter
            endpoint, which reads from io for a string to execute. 
        """
        sendexec(io, 
            inspect.getsource(gateway_base), 
            self._remotesetup,
            "io.write('1'.encode('ascii'))",
            "serve(io)"
        )
        s = io.read(1)
        assert s == "1".encode('ascii')

    def _rinfo(self, update=False):
        """ return some sys/env information from remote. """
        if update or not hasattr(self, '_cache_rinfo'):
            ch = self.remote_exec(rinfo_source)
            self._cache_rinfo = RInfo(ch.receive())
        return self._cache_rinfo

    def remote_status(self):
        """ return information object about remote execution status. """
        channel = self.newchannel()
        self._send(Message.STATUS(channel.id))
        statusdict = channel.receive()
        # the other side didn't actually instantiate a channel
        # so we just delete the internal id/channel mapping
        self._channelfactory._local_close(channel.id)
        return RemoteStatus(statusdict)

    def remote_exec(self, source):
        """ return channel object and connect it to a remote
            execution thread where the given 'source' executes
            and has the sister 'channel' object in its global
            namespace.
        """
        if isinstance(source, types.ModuleType):
            source = inspect.getsource(source)
        else:
            source = textwrap.dedent(str(source))
        channel = self.newchannel()
        self._send(Message.CHANNEL_OPEN(channel.id, source))
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

class RInfo:
    def __init__(self, kwargs):
        self.__dict__.update(kwargs)

    def __repr__(self):
        info = ", ".join(["%s=%s" % item
                for item in self.__dict__.items()])
        return "<RInfo %r>" % info

RemoteStatus = RInfo

rinfo_source = """
import sys, os
channel.send(dict(
    executable = sys.executable,
    version_info = tuple([sys.version_info[i] for i in range(5)]),
    platform = sys.platform,
    cwd = os.getcwd(),
    pid = os.getpid(),
))
"""

class PopenCmdGateway(Gateway):
    _remotesetup = "io = init_popen_io()"
    def __init__(self, args):
        from subprocess import Popen, PIPE
        self._popen = p = Popen(args, stdin=PIPE, stdout=PIPE)
        io = Popen2IO(p.stdin, p.stdout)
        super(PopenCmdGateway, self).__init__(io=io)

    def exit(self):
        super(PopenCmdGateway, self).exit()
        self._trace("polling Popen subprocess")
        self._popen.poll()

popen_bootstrapline = "import sys ; exec(eval(sys.stdin.readline()))"
class PopenGateway(PopenCmdGateway):
    """ This Gateway provides interaction with a newly started
        python subprocess.
    """
    def __init__(self, python=None):
        """ instantiate a gateway to a subprocess
            started with the given 'python' executable.
        """
        if not python:
            python = sys.executable
        args = [str(python), '-c', popen_bootstrapline]
        super(PopenGateway, self).__init__(args)

    def _remote_bootstrap_gateway(self, io):
        sendexec(io, 
                 "import sys",
                 "sys.stdout.write('1')",
                 "sys.stdout.flush()",
                 popen_bootstrapline)
        sendexec(io, 
            "import sys ; sys.path.insert(0, %r)" % importdir,
            "from execnet.gateway_base import serve, init_popen_io",
            "serve(init_popen_io())",
        )
        s = io.read(1)
        assert s == "1".encode('ascii')

def sendexec(io, *sources):
    source = "\n".join(sources)
    io.write((repr(source)+ "\n").encode('ascii'))

class SocketGateway(Gateway):
    """ This Gateway provides interaction with a remote process
        by connecting to a specified socket.  On the remote
        side you need to manually start a small script
        (py/execnet/script/socketserver.py) that accepts
        SocketGateway connections.
    """
    _remotesetup = "io = SocketIO(clientsock)" 

    def __init__(self, host, port):
        """ instantiate a gateway to a process accessed
            via a host/port specified socket.
        """
        self.host = host = str(host)
        self.port = port = int(port)
        self.remoteaddress = '%s:%d' % (self.host, self.port)
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.connect((host, port))
        except socket.gaierror:
            raise HostNotFound(str(sys.exc_info()[1]))
        io = SocketIO(sock)
        super(SocketGateway, self).__init__(io=io)

    def new_remote(cls, gateway, hostport=None):
        """ return a new (connected) socket gateway, 
            instantiated through the given 'gateway'.
        """
        if hostport is None:
            host, port = ('localhost', 0)
        else:
            host, port = hostport
        
        mydir = os.path.dirname(__file__)
        socketserver = os.path.join(mydir, 'script', 'socketserver.py')
        socketserverbootstrap = "\n".join([
            open(socketserver, 'r').read(), """if 1:
            import socket
            sock = bind_and_listen((%r, %r))
            port = sock.getsockname()
            channel.send(port)
            startserver(sock)
        """ % (host, port)])
        # execute the above socketserverbootstrap on the other side
        channel = gateway.remote_exec(socketserverbootstrap)
        (realhost, realport) = channel.receive()
        #self._trace("new_remote received"
        #               "port=%r, hostname = %r" %(realport, hostname))
        if not realhost or realhost=="0.0.0.0":
            realhost = "localhost"
        return gateway._group.makegateway("socket=%s:%s" %(realhost, realport))
    new_remote = classmethod(new_remote)

class HostNotFound(Exception):
    pass

class SshGateway(PopenCmdGateway):
    """ This Gateway provides interaction with a remote Python process,
        established via the 'ssh' command line binary.
        The remote side needs to have a Python interpreter executable.
    """
    def __init__(self, sshaddress, remotepython=None, ssh_config=None):
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
        super(SshGateway, self).__init__(args)

    def _remote_bootstrap_gateway(self, io):
        try:
            super(SshGateway, self)._remote_bootstrap_gateway(io)
        except EOFError:
            ret = self._popen.wait()
            if ret == 255:
                raise HostNotFound(self.remoteaddress)
