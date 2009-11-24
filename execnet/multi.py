"""
Managing Gateway Groups and interactions with multiple channels.

(c) 2008-2009, Holger Krekel and others
"""

import sys, weakref, atexit
import execnet
from execnet import XSpec
from execnet import gateway
from execnet.gateway_base import queue, reraise, trace

NO_ENDMARKER_WANTED = object()

class Group:
    """ Gateway Groups. """
    def __init__(self, xspecs=()):
        """ initialize group and make gateways as specified. """
        # Gateways may evolve to become GC-collectable
        self._activegateways = weakref.WeakKeyDictionary()
        self._id2gateway = weakref.WeakValueDictionary()
        self._autoidcounter = 1
        for xspec in xspecs:
            self.makegateway(xspec)
        atexit.register(self._cleanup_atexit)

    def __repr__(self):
        keys = list(self._id2gateway)
        keys.sort()
        return "<Group %r>" %(keys,)

    def __getitem__(self, key):
        return self._id2gateway[key]

    def __contains__(self, key):
        return key in self._id2gateway or key in self._activegateways

    def __len__(self):
        return len(self._activegateways)

    def __iter__(self):
        return iter(list(self._activegateways))

    def makegateway(self, spec):
        """ create and configure a gateway to a Python interpreter
            specified by a 'execution specification' string.
            The format of the string generally is::

                key1=value1//key2=value2//...

            If you leave out the ``=value`` part a True value is assumed.
        """
        if not isinstance(spec, XSpec):
            spec = XSpec(spec)
        if spec.popen:
            gw = gateway.PopenGateway(python=spec.python)
        elif spec.ssh:
            gw = gateway.SshGateway(spec.ssh, remotepython=spec.python, ssh_config=spec.ssh_config)
        elif spec.socket:
            assert not spec.python, (
                "socket: specifying python executables not yet supported")
            gateway_id = spec.installvia
            if gateway_id:
                viagw = self._id2gateway[gateway_id]
                return gateway.SocketGateway.new_remote(viagw)
            else:
                hostport = spec.socket.split(":")
                gw = gateway.SocketGateway(*hostport)
        else:
            raise ValueError("no gateway type found for %r" % (spec._spec,))
        gw.spec = spec
        self._register(gw, id=spec.id)
        if spec.chdir or spec.nice:
            channel = gw.remote_exec("""
                import os
                path, nice = channel.receive()
                if path:
                    if not os.path.exists(path):
                        os.mkdir(path)
                    os.chdir(path)
                if nice and hasattr(os, 'nice'):
                    os.nice(nice)
            """)
            nice = spec.nice and int(spec.nice) or 0
            channel.send((spec.chdir, nice))
            channel.waitclose()
        return gw

    def _register(self, gateway, id=None):
        assert not hasattr(gateway, '_group')
        if id is None:
            id = self._autoidcounter
            self._autoidcounter += 1
        id = str(id)
        assert id not in self._id2gateway
        assert gateway not in self._activegateways
        self._activegateways[gateway] = True
        self._id2gateway[id] = gateway
        gateway._group = self
        gateway.id = id

    def _unregister(self, gateway):
        del self._activegateways[gateway]
        del self._id2gateway[gateway.id]

    def _cleanup_atexit(self):
        trace("=== atexit cleanup %r ===" %(self,))
        self.terminate()

    def terminate(self):
        """ trigger exit of all gateways. """
        gwlist = []
        while 1:
            try:
                gw, _ = self._activegateways.popitem()
            except KeyError:
                break
            else:
                gw.exit()
                gwlist.append(gw)
        #for gw in gwlist:
        #    gw.join(timeout=1.0)

    def remote_exec(self, source):
        channels = []
        for gw in list(self._activegateways):
            channels.append(gw.remote_exec(source))
        return MultiChannel(channels)

class MultiChannel:
    def __init__(self, channels):
        self._channels = channels

    def send_each(self, item):
        for ch in self._channels:
            ch.send(item)

    def receive_each(self, withchannel=False):
        assert not hasattr(self, '_queue')
        l = []
        for ch in self._channels:
            obj = ch.receive()
            if withchannel:
                l.append((ch, obj))
            else:
                l.append(obj)
        return l

    def make_receive_queue(self, endmarker=NO_ENDMARKER_WANTED):
        try:
            return self._queue
        except AttributeError:
            self._queue = queue.Queue()
            for ch in self._channels:
                def putreceived(obj, channel=ch):
                    self._queue.put((channel, obj))
                if endmarker is NO_ENDMARKER_WANTED:
                    ch.setcallback(putreceived)
                else:
                    ch.setcallback(putreceived, endmarker=endmarker)
            return self._queue


    def waitclose(self):
        first = None
        for ch in self._channels:
            try:
                ch.waitclose()
            except ch.RemoteError:
                if first is None:
                    first = sys.exc_info()
        if first:
            reraise(*first)


default_group = Group()

makegateway = default_group.makegateway

def PopenGateway(python=None):
    """ instantiate a gateway to a subprocess
        started with the given 'python' executable.
    """
    APIWARN("1.0.0b4", "use makegateway('popen')")
    spec = execnet.XSpec("popen")
    spec.python = python
    return default_group.makegateway(spec)

def SocketGateway(host, port):
    """ This Gateway provides interaction with a remote process
        by connecting to a specified socket.  On the remote
        side you need to manually start a small script
        (py/execnet/script/socketserver.py) that accepts
        SocketGateway connections or use the experimental
        new_remote() method on existing gateways.
    """
    APIWARN("1.0.0b4", "use makegateway('socket=host:port')")
    spec = execnet.XSpec("socket=%s:%s" %(host, port))
    return default_group.makegateway(spec)

def SshGateway(sshaddress, remotepython=None, ssh_config=None):
    """ instantiate a remote ssh process with the
        given 'sshaddress' and remotepython version.
        you may specify an ssh_config file.
    """
    APIWARN("1.0.0b4", "use makegateway('ssh=host')")
    spec = execnet.XSpec("ssh=%s" % sshaddress)
    spec.python = remotepython
    spec.ssh_config = ssh_config
    return default_group.makegateway(spec)

def APIWARN(version, msg, stacklevel=3):
    import warnings
    Warn = DeprecationWarning("(since version %s) %s" %(version, msg))
    warnings.warn(Warn, stacklevel=stacklevel)
