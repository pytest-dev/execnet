import socket
from execnet.gateway import Gateway
from execnet.gateway_bootstrap import HostNotFound
import os, sys, inspect


try: bytes
except NameError: bytes = str

class SocketIO:

    error = (socket.error, EOFError)
    def __init__(self, sock):
        self.sock = sock
        try:
            sock.setsockopt(socket.SOL_IP, socket.IP_TOS, 0x10)# IPTOS_LOWDELAY
            sock.setsockopt(socket.SOL_TCP, socket.TCP_NODELAY, 1)
        except (AttributeError, socket.error):
            sys.stderr.write("WARNING: cannot set socketoption")

    def read(self, numbytes):
        "Read exactly 'bytes' bytes from the socket."
        buf = bytes()
        while len(buf) < numbytes:
            t = self.sock.recv(numbytes - len(buf))
            if not t:
                raise EOFError
            buf += t
        return buf

    def write(self, data):
        self.sock.sendall(data)

    def close_read(self):
        try:
            self.sock.shutdown(0)
        except socket.error:
            pass
    def close_write(self):
        try:
            self.sock.shutdown(1)
        except socket.error:
            pass

    def wait(self):
        pass

    def kill(self):
        pass

class SocketGateway(Gateway):
    """ This Gateway provides interaction with a remote process
        by connecting to a specified socket.  On the remote
        side you need to manually start a small script
        (py/execnet/script/socketserver.py) that accepts
        SocketGateway connections.
    """


    def __init__(self, host, port, id):
        """ instantiate a gateway to a process accessed
            via a host/port specified socket.
        """
        self.host = host = str(host)
        self.port = port = int(port)
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        io = SocketIO(sock)
        io.remoteaddress = '%s:%d' % (self.host, self.port)
        try:
            sock.connect((host, port))
        except socket.gaierror:
            raise HostNotFound(str(sys.exc_info()[1]))
        #XXX: temporary
        from execnet.gateway_bootstrap import bootstrap_socket
        bootstrap_socket(io, id)
        super(SocketGateway, self).__init__(io=io, id=id)

    def new_remote(cls, gateway, id, hostport=None):
        """ return a new (connected) socket gateway,
            instantiated through the given 'gateway'.
        """
        if hostport is None:
            host, port = ('localhost', 0)
        else:
            host, port = hostport

        mydir = os.path.dirname(__file__)
        from execnet.script import socketserver

        # execute the above socketserverbootstrap on the other side
        channel = gateway.remote_exec(socketserver)
        channel.send((host, port))
        (realhost, realport) = channel.receive()
        #self._trace("new_remote received"
        #               "port=%r, hostname = %r" %(realport, hostname))
        if not realhost or realhost=="0.0.0.0":
            realhost = "localhost"
        return cls(realhost, realport, id=id)
    new_remote = classmethod(new_remote)
