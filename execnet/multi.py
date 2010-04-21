"""
Managing Gateway Groups and interactions with multiple channels.

(c) 2008-2009, Holger Krekel and others
"""

import os, sys, atexit
import execnet
from execnet import XSpec
from execnet import gateway
from execnet.gateway_base import queue, reraise, trace, TimeoutError

NO_ENDMARKER_WANTED = object()

class Group:
    """ Gateway Groups. """
    defaultspec = "popen"
    def __init__(self, xspecs=()):
        """ initialize group and make gateways as specified. """
        # Gateways may evolve to become GC-collectable
        self._gateways = []
        self._autoidcounter = 0
        self._gateways_to_join = []
        for xspec in xspecs:
            self.makegateway(xspec)
        atexit.register(self._cleanup_atexit)

    def __repr__(self):
        idgateways = [gw.id for gw in self]
        return "<Group %r>" %(idgateways)

    def __getitem__(self, key):
        if isinstance(key, int):
            return self._gateways[key]
        for gw in self._gateways:
            if gw == key or gw.id == key:
                return gw
        raise KeyError(key)

    def __contains__(self, key):
        try:
            self[key]
            return True
        except KeyError:
            return False

    def __len__(self):
        return len(self._gateways)

    def __iter__(self):
        return iter(list(self._gateways))

    def makegateway(self, spec=None):
        """create and configure a gateway to a Python interpreter.
        The ``spec`` string encodes the target gateway type
        and configuration information. The general format is::

            key1=value1//key2=value2//...

        If you leave out the ``=value`` part a True value is assumed.
        Valid types: ``popen``, ``ssh=hostname``, ``socket=host:port``.
        Valid configuration::

            id=<string>     specifies the gateway id 
            python=<path>   specifies which python interpreter to execute
            chdir=<path>    specifies to which directory to change
            nice=<path>     specifies process priority of new process

        If no spec is given, self.defaultspec is used. 
        """
        if not spec:
            spec = self.defaultspec
        if not isinstance(spec, XSpec):
            spec = XSpec(spec)
        id = self._allocate_id(spec.id)
        if spec.popen:
            gw = gateway.PopenGateway(python=spec.python, id=id)
        elif spec.ssh:
            gw = gateway.SshGateway(spec.ssh, remotepython=spec.python, 
                                    ssh_config=spec.ssh_config, id=id)
        elif spec.socket:
            assert not spec.python, (
                "socket: specifying python executables not yet supported")
            from execnet.gateway_socket import SocketGateway
            gateway_id = spec.installvia
            if gateway_id:
                viagw = self[gateway_id]
                gw = SocketGateway.new_remote(viagw, id=id)
            else:
                host, port = spec.socket.split(":")
                gw = SocketGateway(host, port, id=id)
        else:
            raise ValueError("no gateway type found for %r" % (spec._spec,))
        gw.spec = spec
        self._register(gw)
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

    def _allocate_id(self, id=None):
        if id is None:
            id = "gw" + str(self._autoidcounter)
            self._autoidcounter += 1
        if id in self:
            raise ValueError("already have member gateway with id %r" %(id,))
        return id

    def _register(self, gateway):
        assert not hasattr(gateway, '_group')
        assert gateway.id
        assert id not in self
        self._gateways.append(gateway)
        gateway._group = self

    def _unregister(self, gateway):
        self._gateways.remove(gateway)
        self._gateways_to_join.append(gateway)

    def _cleanup_atexit(self):
        trace("=== atexit cleanup %r ===" %(self,))
        self.terminate(timeout=1.0)

    def terminate(self, timeout=None):
        """ trigger exit of member gateways and wait for termination 
        of member gateways and associated subprocesses.  After waiting 
        timeout seconds try to to kill local sub processes of popen- 
        and ssh-gateways.  Timeout defaults to None meaning 
        open-ended waiting and no kill attempts.
        """
        for gw in self:
            gw.exit()
        def join_receiver_and_wait_for_subprocesses():
            for gw in self._gateways_to_join:
                gw.join()
            while self._gateways_to_join:
                gw = self._gateways_to_join[0]
                if hasattr(gw, '_popen'):
                    gw._popen.wait()
                del self._gateways_to_join[0]
        from execnet.threadpool import WorkerPool
        pool = WorkerPool(1)
        reply = pool.dispatch(join_receiver_and_wait_for_subprocesses)
        try:
            reply.get(timeout=timeout)
        except IOError:
            trace("Gateways did not come down after timeout: %r" 
                  %(self._gateways_to_join))
            while self._gateways_to_join:
                gw = self._gateways_to_join.pop(0)
                popen = getattr(gw, '_popen', None)
                if popen:
                    killpopen(popen)

    def remote_exec(self, source):
        """ remote_exec source on all member gateways and return
            MultiChannel connecting to all sub processes.
        """
        channels = []
        for gw in self:
            channels.append(gw.remote_exec(source))
        return MultiChannel(channels)

class MultiChannel:
    def __init__(self, channels):
        self._channels = channels

    def __len__(self):
        return len(self._channels)

    def __iter__(self):
        return iter(self._channels)
    
    def __getitem__(self, key):
        return self._channels[key]
    
    def __contains__(self, chan):
        return chan in self._channels

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

def killpopen(popen):
    try:
        if hasattr(popen, 'kill'):
            popen.kill()
        else:
            killpid(popen.pid)
    except EnvironmentError:
        sys.stderr.write("ERROR killing: %s\n" %(sys.exc_info()[1]))
        sys.stderr.flush()

def killpid(pid):
    if hasattr(os, 'kill'):
        os.kill(pid, 15)
    elif sys.platform == "win32" or getattr(os, '_name', None) == 'nt':
        try:
            import ctypes
        except ImportError:
            import subprocess
            # T: treekill, F: Force 
            cmd = ("taskkill /T /F /PID %d" %(pid)).split()
            ret = subprocess.call(cmd)
            if ret != 0:
                raise EnvironmentError("taskkill returned %r" %(ret,))
        else:
            PROCESS_TERMINATE = 1
            handle = ctypes.windll.kernel32.OpenProcess(
                        PROCESS_TERMINATE, False, pid)
            ctypes.windll.kernel32.TerminateProcess(handle, -1)
            ctypes.windll.kernel32.CloseHandle(handle)
    else:
        raise EnvironmentError("no method to kill %s" %(pid,))

default_group = Group()
makegateway = default_group.makegateway

