"""
base execnet gateway code send to the other side for bootstrapping.

NOTE: aims to be compatible to Python 2.3-3.1, Jython and IronPython

(C) 2004-2009 Holger Krekel, Armin Rigo, Benjamin Peterson, and others
"""
import sys, os, weakref
import threading, traceback, socket, struct
try:
    import queue
except ImportError:
    import Queue as queue

ISPY3 = sys.version_info > (3, 0)
if ISPY3:
    exec("def do_exec(co, loc): exec(co, loc)\n"
         "def reraise(cls, val, tb): raise val\n")
    unicode = str
    _long_type = int
else:
    exec("def do_exec(co, loc): exec co in loc\n"
         "def reraise(cls, val, tb): raise cls, val, tb\n")
    bytes = str
    _long_type = long

sysex = (KeyboardInterrupt, SystemExit)

if os.environ.get('EXECNET_DEBUG'):
    import tempfile, os.path
    fn = os.path.join(tempfile.gettempdir(), 'execnet-debug-%d' % os.getpid())
    debugfile = open(fn, 'w')
    def trace(msg):
        try:
            debugfile.write(unicode(msg) + "\n")
            debugfile.flush()
        except sysex:
            raise
        except:
            sys.stderr.write("exception during tracing\n")
else:
    def trace(msg): 
        pass


# ___________________________________________________________________________
#
# input output classes
# ___________________________________________________________________________

class SocketIO:
    server_stmt = "io = SocketIO(clientsock)"

    error = (socket.error, EOFError)
    def __init__(self, sock):
        self.sock = sock
        try:
            sock.setsockopt(socket.SOL_IP, socket.IP_TOS, 0x10)# IPTOS_LOWDELAY
            sock.setsockopt(socket.SOL_TCP, socket.TCP_NODELAY, 1)
        except (AttributeError, socket.error):
            e = sys.exc_info()[1]
            sys.stderr.write("WARNING: cannot set socketoption")
        self.readable = self.writeable = True

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
        assert isinstance(data, bytes)
        self.sock.sendall(data)

    def close_read(self):
        if self.readable:
            try:
                self.sock.shutdown(0)
            except socket.error:
                pass
            self.readable = None
    def close_write(self):
        if self.writeable:
            try:
                self.sock.shutdown(1)
            except socket.error:
                pass
            self.writeable = None

class Popen2IO:
    server_stmt = """
import os, sys, tempfile
io = Popen2IO(sys.stdout, sys.stdin)
sys.stdout = tempfile.TemporaryFile('w')
sys.stdin = tempfile.TemporaryFile('r')
"""
    error = (IOError, OSError, EOFError)

    def __init__(self, outfile, infile):
        # we need raw byte streams
        self.outfile, self.infile = outfile, infile
        if sys.platform == "win32":
            import msvcrt
            msvcrt.setmode(infile.fileno(), os.O_BINARY)
            msvcrt.setmode(outfile.fileno(), os.O_BINARY)
        self.readable = self.writeable = True

    def read(self, numbytes):
        """Read exactly 'numbytes' bytes from the pipe. """
        try:
            data = self.infile.buffer.read(numbytes)
        except AttributeError:
            data = self.infile.read(numbytes)
        if len(data) < numbytes:
            raise EOFError
        return data

    def write(self, data):
        """write out all data bytes. """
        assert isinstance(data, bytes)
        try:
            self.outfile.buffer.write(data)
        except AttributeError:
            self.outfile.write(data)
        self.outfile.flush()

    def close_read(self):
        if self.readable:
            self.infile.close()
            self.readable = None

    def close_write(self):
        try:
            self.outfile.close()
        except EnvironmentError:
            pass
        self.writeable = None

class Message:
    """ encapsulates Messages and their wire protocol. """
    _types = {}

    def __init__(self, channelid=0, data=''):
        self.channelid = channelid
        self.data = data

    def writeto(self, serializer):
        serializer.save((self.msgtype, self.channelid, self.data))

    def readfrom(cls, unserializer):
        msgtype, senderid, data = unserializer.load()
        return cls._types[msgtype](senderid, data)
    readfrom = classmethod(readfrom)

    def __repr__(self):
        r = repr(self.data)
        if len(r) > 50:
            return "<Message.%s channelid=%d len=%d>" %(self.__class__.__name__,
                        self.channelid, len(r))
        else:
            return "<Message.%s channelid=%d %r>" %(self.__class__.__name__,
                        self.channelid, self.data)

def _setupmessages():
    class STATUS(Message):
        def received(self, gateway):
            # we use self.channelid to send back information
            # but don't instantiate a channel object
            active_channels = gateway._channelfactory.channels()
            numexec = 0
            for ch in active_channels:
                if getattr(ch, '_executing', False):
                    numexec += 1
            d = {'receiving': True, 
                 'execqsize': gateway._execqueue.qsize(),
                 'numchannels': len(active_channels),
                 'numexecuting': numexec
            }
            gateway._send(Message.CHANNEL_DATA(self.channelid, d))

    class CHANNEL_OPEN(Message):
        def received(self, gateway):
            channel = gateway._channelfactory.new(self.channelid)
            gateway._local_schedulexec(channel=channel, sourcetask=self.data)

    class CHANNEL_NEW(Message):
        def received(self, gateway):
            """ receive a remotely created new (sub)channel. """
            newid = self.data
            newchannel = gateway._channelfactory.new(newid)
            gateway._channelfactory._local_receive(self.channelid, newchannel)

    class CHANNEL_DATA(Message):
        def received(self, gateway):
            gateway._channelfactory._local_receive(self.channelid, self.data)

    class CHANNEL_CLOSE(Message):
        def received(self, gateway):
            gateway._channelfactory._local_close(self.channelid)

    class CHANNEL_CLOSE_ERROR(Message):
        def received(self, gateway):
            remote_error = gateway._channelfactory.RemoteError(self.data)
            gateway._channelfactory._local_close(self.channelid, remote_error)

    class CHANNEL_LAST_MESSAGE(Message):
        def received(self, gateway):
            gateway._channelfactory._local_close(self.channelid, sendonly=True)

    classes = [STATUS, CHANNEL_OPEN, CHANNEL_NEW, CHANNEL_DATA,
               CHANNEL_CLOSE, CHANNEL_CLOSE_ERROR, CHANNEL_LAST_MESSAGE]

    for i, cls in enumerate(classes):
        Message._types[i] = cls
        cls.msgtype = i
        setattr(Message, cls.__name__, cls)

_setupmessages()

def geterrortext(excinfo):
    try:
        l = traceback.format_exception(*excinfo)
        errortext = "".join(l)
    except sysex:
        raise
    except:
        errortext = '%s: %s' % (excinfo[0].__name__,
                                excinfo[1])
    return errortext

class RemoteError(EOFError):
    """ Exception containing a stringified error from the other side. """
    def __init__(self, formatted):
        self.formatted = formatted
        EOFError.__init__(self)

    def __str__(self):
        return self.formatted

    def __repr__(self):
        return "%s: %s" %(self.__class__.__name__, self.formatted)

    def warn(self):
        # XXX do this better
        sys.stderr.write("Warning: unhandled %r\n" % (self,))


NO_ENDMARKER_WANTED = object()

class Channel(object):
    """Communication channel between two Python Interpreter execution points."""
    RemoteError = RemoteError
    _executing = False

    def __init__(self, gateway, id):
        assert isinstance(id, int)
        self.gateway = gateway
        self.id = id
        self._items = queue.Queue()
        self._closed = False
        self._receiveclosed = threading.Event()
        self._remoteerrors = []

    def setcallback(self, callback, endmarker=NO_ENDMARKER_WANTED):
        """ set a callback function for receiving items.

            All already queued items will immediately trigger the callback.
            Afterwards the callback will execute in the receiver thread
            for each received data item and calls to ``receive()`` will
            raise an error.
            If an endmarker is specified the callback will eventually
            be called with the endmarker when the channel closes.
        """
        _callbacks = self.gateway._channelfactory._callbacks
        _receivelock = self.gateway._receivelock
        _receivelock.acquire()
        try:
            if self._items is None:
                raise IOError("%r has callback already registered" %(self,))
            items = self._items
            self._items = None
            while 1:
                try:
                    olditem = items.get(block=False)
                except queue.Empty:
                    if not (self._closed or self._receiveclosed.isSet()):
                        _callbacks[self.id] = (callback, endmarker)
                    break
                else:
                    if olditem is ENDMARKER:
                        items.put(olditem) # for other receivers
                        if endmarker is not NO_ENDMARKER_WANTED:
                            callback(endmarker)
                        break
                    else:
                        callback(olditem)
        finally:
            _receivelock.release()

    def __repr__(self):
        flag = self.isclosed() and "closed" or "open"
        return "<Channel id=%d %s>" % (self.id, flag)

    def __del__(self):
        if self.gateway is None:   # can be None in tests
            return
        self.gateway._trace("Channel(%d).__del__" % self.id)
        # no multithreading issues here, because we have the last ref to 'self'
        if self._closed:
            # state transition "closed" --> "deleted"
            for error in self._remoteerrors:
                error.warn()
        elif self._receiveclosed.isSet():
            # state transition "sendonly" --> "deleted"
            # the remote channel is already in "deleted" state, nothing to do
            pass
        else:
            # state transition "opened" --> "deleted"
            if self._items is None:    # has_callback
                Msg = Message.CHANNEL_LAST_MESSAGE
            else:
                Msg = Message.CHANNEL_CLOSE
            try:
                self.gateway._send(Msg(self.id))
            except ValueError: # XXX IO operation on closed file, why? 
                pass

    def _getremoteerror(self):
        try:
            return self._remoteerrors.pop(0)
        except IndexError:
            return None

    #
    # public API for channel objects
    #
    def isclosed(self):
        """ return True if the channel is closed. A closed
            channel may still hold items.
        """
        return self._closed

    def makefile(self, mode='w', proxyclose=False):
        """ return a file-like object.
            mode can be 'w' or 'r' for writeable/readable files.
            if proxyclose is true file.close() will also close the channel.
        """
        if mode == "w":
            return ChannelFileWrite(channel=self, proxyclose=proxyclose)
        elif mode == "r":
            return ChannelFileRead(channel=self, proxyclose=proxyclose)
        raise ValueError("mode %r not availabe" %(mode,))

    def close(self, error=None):
        """ close down this channel with an optional error message. 
            Note that closing of a channel tied to remote_exec happens 
            automatically at the end of execution and cannot be done explicitely. 
        """
        if self._executing:
            raise IOError("cannot explicitly close channel within remote_exec")
        if self._closed:
            trace("%r channel already closed, close() called." % (self, ))
        if not self._closed:
            # state transition "opened/sendonly" --> "closed"
            # threads warning: the channel might be closed under our feet,
            # but it's never damaging to send too many CHANNEL_CLOSE messages
            put = self.gateway._send
            if error is not None:
                put(Message.CHANNEL_CLOSE_ERROR(self.id, error))
            else:
                put(Message.CHANNEL_CLOSE(self.id))
            trace("%r: sent channel close message" %(self,))
            if isinstance(error, RemoteError):
                self._remoteerrors.append(error)
            self._closed = True         # --> "closed"
            self._receiveclosed.set()
            queue = self._items
            if queue is not None:
                queue.put(ENDMARKER)
            self.gateway._channelfactory._no_longer_opened(self.id)

    def waitclose(self, timeout=None):
        """ wait until this channel is closed (or the remote side
        otherwise signalled that no more data was being sent).
        The channel may still hold receiveable items, but not receive
        any more after waitclose() has returned. exceptions from executing
        code on the other side are reraised as local channel.RemoteErrors.
        """
        self._receiveclosed.wait(timeout=timeout)  # wait for non-"opened" state
        if not self._receiveclosed.isSet():
            raise IOError("Timeout")
        error = self._getremoteerror()
        if error:
            raise error

    def send(self, item):
        """sends the given item to the other side of the channel,
        possibly blocking if the sender queue is full.
        Note that an item needs to be marshallable.
        """
        if self.isclosed():
            raise IOError("cannot send to %r" %(self,))
        if isinstance(item, Channel):
            data = Message.CHANNEL_NEW(self.id, item.id)
        else:
            data = Message.CHANNEL_DATA(self.id, item)
        self.gateway._send(data)

    def receive(self):
        """receives an item that was sent from the other side,
        possibly blocking if there is none.
        Note that exceptions from the other side will be
        reraised as channel.RemoteError exceptions containing
        a textual representation of the remote traceback.
        """
        queue = self._items
        if queue is None:
            raise IOError("calling receive() on channel with receiver callback")
        x = queue.get()
        if x is ENDMARKER:
            queue.put(x)  # for other receivers
            raise self._getremoteerror() or EOFError()
        else:
            return x

    def __iter__(self):
        return self

    def next(self):
        try:
            return self.receive()
        except EOFError:
            raise StopIteration
    __next__ = next

ENDMARKER = object()

class ChannelFactory(object):
    RemoteError = RemoteError

    def __init__(self, gateway, startcount=1):
        self._channels = weakref.WeakValueDictionary()
        self._callbacks = {}
        self._writelock = threading.Lock()
        self.gateway = gateway
        self.count = startcount
        self.finished = False

    def new(self, id=None):
        """ create a new Channel with 'id' (or create new id if None). """
        self._writelock.acquire()
        try:
            if self.finished:
                raise IOError("connexion already closed: %s" % (self.gateway,))
            if id is None:
                id = self.count
                self.count += 2
            channel = Channel(self.gateway, id)
            self._channels[id] = channel
            return channel
        finally:
            self._writelock.release()

    def channels(self):
        return list(self._channels.values())

    #
    # internal methods, called from the receiver thread
    #
    def _no_longer_opened(self, id):
        try:
            del self._channels[id]
        except KeyError:
            pass
        try:
            callback, endmarker = self._callbacks.pop(id)
        except KeyError:
            pass
        else:
            if endmarker is not NO_ENDMARKER_WANTED:
                callback(endmarker)

    def _local_close(self, id, remoteerror=None, sendonly=False):
        channel = self._channels.get(id)
        if channel is None:
            # channel already in "deleted" state
            if remoteerror:
                remoteerror.warn()
        else:
            # state transition to "closed" state
            if remoteerror:
                channel._remoteerrors.append(remoteerror)
            if not sendonly: # otherwise #--> "sendonly"
                channel._closed = True          # --> "closed"
            channel._receiveclosed.set()
            queue = channel._items
            if queue is not None:
                queue.put(ENDMARKER)
        self._no_longer_opened(id)

    def _local_receive(self, id, data):
        # executes in receiver thread
        try:
            callback, endmarker = self._callbacks[id]
        except KeyError:
            channel = self._channels.get(id)
            queue = channel and channel._items
            if queue is None:
                pass    # drop data
            else:
                queue.put(data)
        else:
            callback(data)   # even if channel may be already closed

    def _finished_receiving(self):
        self._writelock.acquire()
        try:
            self.finished = True
        finally:
            self._writelock.release()
        for id in list(self._channels):
            self._local_close(id, sendonly=True)
        for id in list(self._callbacks):
            self._no_longer_opened(id)

class ChannelFile(object):
    def __init__(self, channel, proxyclose=True):
        self.channel = channel
        self._proxyclose = proxyclose

    def close(self):
        if self._proxyclose:
            self.channel.close()

    def __repr__(self):
        state = self.channel.isclosed() and 'closed' or 'open'
        return '<ChannelFile %d %s>' %(self.channel.id, state)

class ChannelFileWrite(ChannelFile):
    def write(self, out):
        self.channel.send(out)

    def flush(self):
        pass

class ChannelFileRead(ChannelFile):
    def __init__(self, channel, proxyclose=True):
        super(ChannelFileRead, self).__init__(channel, proxyclose)
        self._buffer = ""

    def read(self, n):
        while len(self._buffer) < n:
            try:
                self._buffer += self.channel.receive()
            except EOFError:
                self.close()
                break
        ret = self._buffer[:n]
        self._buffer = self._buffer[n:]
        return ret

    def readline(self):
        i = self._buffer.find("\n")
        if i != -1:
            return self.read(i+1)
        line = self.read(len(self._buffer)+1)
        while line and line[-1] != "\n":
            c = self.read(1)
            if not c:
                break
            line += c
        return line

class BaseGateway(object):
    exc_info = sys.exc_info

    class _StopExecLoop(Exception):
        pass

    def __init__(self, io, _startcount=2):
        self._io = io
        self._channelfactory = ChannelFactory(self, _startcount)
        self._receivelock = threading.RLock()
        self._serializer = Serializer(io)
        self._trace = trace  # globals may be NONE at process-termination

    def _initreceive(self):
        self._receiverthread = threading.Thread(name="receiver",
                                 target=self._thread_receiver)
        self._receiverthread.setDaemon(1)
        self._receiverthread.start()

    def _thread_receiver(self):
        self._trace("starting to receive")
        unserializer = Unserializer(self._io)
        try:
            while 1:
                try:
                    msg = Message.readfrom(unserializer)
                    self._trace("received <- %r" % msg)
                    _receivelock = self._receivelock
                    _receivelock.acquire()
                    try:
                        msg.received(self)
                    finally:
                        _receivelock.release()
                except sysex:
                    break
                except EOFError:
                    break
                except:
                    self._trace("RECEIVERTHREAD: %s " %(
                        geterrortext(self.exc_info()), ))
                    break
        finally:
            # XXX we need to signal fatal error states to
            #     channels/callbacks, particularly ones
            #     where the other side just died.
            self._stopexec()
            try:
                self._stopsend()
            except IOError:
                self._trace('IOError on _stopsend()')
            self._channelfactory._finished_receiving()
            if threading: # might be None during shutdown/finalization
                self._trace('leaving %r' % threading.currentThread())

    def _send(self, msg):
        assert isinstance(msg, Message)
        msg.writeto(self._serializer)
        self._trace('sent -> %r' % msg)

    def _stopsend(self):
        self._io.close_write()
        self._trace('closed IO write pipe')

    def _stopexec(self):
        pass

    def _local_schedulexec(self, channel, sourcetask):
        channel.close("execution disallowed")

    # _____________________________________________________________________
    #
    # High Level Interface
    # _____________________________________________________________________
    #
    def newchannel(self):
        return self._channelfactory.new()

    def join(self, timeout=None):
        """ Wait for receiverthread to terminate. """
        current = threading.currentThread()
        if self._receiverthread.isAlive():
            self._trace("joining receiver thread")
            self._receiverthread.join(timeout)
        else:
            self._trace("gateway.join() called while receiverthread "
                        "already finished")

class SlaveGateway(BaseGateway):
    def _stopexec(self):
        self._execqueue.put(None)

    def _local_schedulexec(self, channel, sourcetask):
        self._execqueue.put((channel, sourcetask))

    def serve(self, joining=True):
        self._execqueue = queue.Queue()
        self._initreceive()
        try:
            while 1:
                item = self._execqueue.get()
                if item is None:
                    self._stopsend()
                    break
                try:
                    self.executetask(item)
                except self._StopExecLoop:
                    break
        finally:
            self._trace("slavegateway.serve finished")
        if joining:
            self.join()

    def executetask(self, item):
        channel, source = item
        try:
            loc = {'channel' : channel, '__name__': '__channelexec__'}
            self._trace("execution starts[%s]: %s" % (channel.id, repr(source)[:50]))
            channel._executing = True
            try:
                co = compile(source+'\n', '', 'exec')
                do_exec(co, loc)
            finally:
                channel._executing = False
                self._trace("execution finished")
        except self._StopExecLoop:
            channel.close()
            raise
        except:
            excinfo = self.exc_info()
            self._trace("got exception: %s" % (excinfo[1],))
            errortext = geterrortext(excinfo)
            channel.close(errortext)
        else:
            channel.close()

#
# Cross-Python pickling code, tested from test_serializer.py
#

class SerializeError(Exception):
    pass

class SerializationError(SerializeError):
    """Error while serializing an object."""

class UnserializationError(SerializeError):
    """Error while unserializing an object."""

if ISPY3:
    def b(s):
        return s.encode("latin-1")
else:
    b = str

FOUR_BYTE_INT_MAX = 2147483647

FLOAT_FORMAT = "!d"
FLOAT_FORMAT_SIZE = struct.calcsize(FLOAT_FORMAT)

class _Stop(Exception):
    pass

class Unserializer(object):
    num2func = {} # is filled after this class definition
    py2str_as_py3str = False # True
    py3str_as_py2str = False  # false means py2 will get unicode

    def __init__(self, stream):
        self.stream = stream

    def load(self):
        self.stack = []
        try:
            while True:
                opcode = self.stream.read(1)
                if not opcode:
                    raise EOFError
                try:
                    loader = self.num2func[opcode]
                except KeyError:
                    raise UnserializationError("unkown opcode %r - "
                        "wire protocol corruption?" % (opcode,))
                loader(self)
        except _Stop:
            if len(self.stack) != 1:
                raise UnserializationError("internal unserialization error")
            return self.stack[0]
        else:
            raise UnserializationError("didn't get STOP")

    def load_none(self):
        self.stack.append(None)

    def load_true(self):
        self.stack.append(True)

    def load_false(self):
        self.stack.append(False)

    def load_int(self):
        i = self._read_int4()
        self.stack.append(i)

    def load_longint(self):
        s = self._read_byte_string()
        self.stack.append(int(s))

    if ISPY3:
        load_long = load_int
        load_longlong = load_longint
    else:
        def load_long(self):
            i = self._read_int4()
            self.stack.append(long(i))

        def load_longlong(self):
            l = self._read_byte_string()
            self.stack.append(long(l))

    def load_float(self):
        binary = self.stream.read(FLOAT_FORMAT_SIZE)
        self.stack.append(struct.unpack(FLOAT_FORMAT, binary)[0])

    def _read_int4(self):
        return struct.unpack("!i", self.stream.read(4))[0]

    def _read_byte_string(self):
        length = self._read_int4()
        as_bytes = self.stream.read(length)
        return as_bytes

    def load_py3string(self):
        as_bytes = self._read_byte_string()
        if not ISPY3 and self.py3str_as_py2str:
            # XXX Should we try to decode into latin-1?
            self.stack.append(as_bytes)
        else:
            self.stack.append(as_bytes.decode("utf-8"))

    def load_py2string(self):
        as_bytes = self._read_byte_string()
        if ISPY3 and self.py2str_as_py3str:
            s = as_bytes.decode("latin-1")
        else:
            s = as_bytes
        self.stack.append(s)

    def load_bytes(self):
        s = self._read_byte_string()
        self.stack.append(s)

    def load_unicode(self):
        self.stack.append(self._read_byte_string().decode("utf-8"))

    def load_newlist(self):
        length = self._read_int4()
        self.stack.append([None] * length)

    def load_setitem(self):
        if len(self.stack) < 3:
            raise UnserializationError("not enough items for setitem")
        value = self.stack.pop()
        key = self.stack.pop()
        self.stack[-1][key] = value

    def load_newdict(self):
        self.stack.append({})

    def _load_tuple(self):
        length = self._read_int4()
        if length:
            tup = tuple(self.stack[-length:])
            del self.stack[-length:]
        else:
            tup = ()
        return tup

    def load_buildtuple(self):
        self.stack.append(self._load_tuple())

    def load_set(self):
        self.stack.append(set(self._load_tuple()))

    def load_frozenset(self):
        self.stack.append(frozenset(self._load_tuple()))

    def load_stop(self):
        raise _Stop

# automatically build opcodes and byte-encoding 

class opcode:
    """ container for name -> num mappings. """ 

def _buildopcodes():
    l = []
    for name, func in Unserializer.__dict__.items():
        if name.startswith("load_"):
            opname = name[5:].upper()
            l.append((opname, func))
    l.sort()
    for i,(opname, func) in enumerate(l):
        assert i < 26, "xxx"
        i = b(chr(64+i))
        Unserializer.num2func[i] = func 
        setattr(opcode, opname, i)

_buildopcodes()

class Serializer(object):
    WRITE_ON_SUCCESS=True # more robust against serialize failures

    def __init__(self, stream):
        self.dispatch = {}
        self._stream = stream

    def save(self, obj):
        if self.WRITE_ON_SUCCESS:
            self.streamlist = []
            self._write = self.streamlist.append
        else:
            self._write = self.stream.write
        self._save(obj)
        self._write(opcode.STOP)
        if self.WRITE_ON_SUCCESS:
            # atomic write! (compatible to python3 and python2)
            s = type(self.streamlist[0])().join(self.streamlist)
            self._stream.write(s)

    def _save(self, obj):
        tp = type(obj)
        try:
            dispatch = self.dispatch[tp]
        except KeyError:
            methodname = 'save_' + tp.__name__.lower()
            meth = getattr(self, methodname, None)
            if meth is None:
                raise SerializationError("can't serialize %s" % (tp,))
            dispatch = self.dispatch[tp] = meth
        dispatch(obj)

    def save_nonetype(self, non):
        self._write(opcode.NONE)

    def save_bool(self, boolean):
        if boolean:
            self._write(opcode.TRUE)
        else:
            self._write(opcode.FALSE)

    def save_bytes(self, bytes_):
        self._write(opcode.BYTES)
        self._write_byte_sequence(bytes_)

    if ISPY3:
        def save_str(self, s):
            self._write(opcode.PY3STRING)
            self._write_unicode_string(s)
    else:
        def save_str(self, s):
            self._write(opcode.PY2STRING)
            self._write_byte_sequence(s)

        def save_unicode(self, s):
            self._write(opcode.UNICODE)
            self._write_unicode_string(s)

    def _write_unicode_string(self, s):
        try:
            as_bytes = s.encode("utf-8")
        except UnicodeEncodeError:
            raise SerializationError("strings must be utf-8 encodable")
        self._write_byte_sequence(as_bytes)

    def _write_byte_sequence(self, bytes_):
        self._write_int4(len(bytes_), "string is too long")
        self._write(bytes_)

    def _save_integral(self, i, short_op, long_op):
        if i <= FOUR_BYTE_INT_MAX:
            self._write(short_op)
            self._write_int4(i)
        else:
            self._write(long_op)
            self._write_byte_sequence(str(i).rstrip("L").encode("ascii"))

    def save_int(self, i):
        self._save_integral(i, opcode.INT, opcode.LONGINT)

    def save_long(self, l):
        self._save_integral(l, opcode.LONG, opcode.LONGLONG)

    def save_float(self, flt):
        self._write(opcode.FLOAT)
        self._write(struct.pack(FLOAT_FORMAT, flt))

    def _write_int4(self, i, error="int must be less than %i" %
                    (FOUR_BYTE_INT_MAX,)):
        if i > FOUR_BYTE_INT_MAX:
            raise SerializationError(error)
        self._write(struct.pack("!i", i))

    def save_list(self, L):
        self._write(opcode.NEWLIST)
        self._write_int4(len(L), "list is too long")
        for i, item in enumerate(L):
            self._write_setitem(i, item)

    def _write_setitem(self, key, value):
        self._save(key)
        self._save(value)
        self._write(opcode.SETITEM)

    def save_dict(self, d):
        self._write(opcode.NEWDICT)
        for key, value in d.items():
            self._write_setitem(key, value)

    def save_tuple(self, tup):
        for item in tup:
            self._save(item)
        self._write(opcode.BUILDTUPLE)
        self._write_int4(len(tup), "tuple is too long")

    def _write_set(self, s, op):
        for item in s:
            self._save(item)
        self._write(op)
        self._write_int4(len(s), "set is too long")

    def save_set(self, s):
        self._write_set(s, opcode.SET)

    def save_frozenset(self, s):
        self._write_set(s, opcode.FROZENSET)
