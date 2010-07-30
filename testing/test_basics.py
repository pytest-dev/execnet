
import py
import sys, os, subprocess, inspect
import execnet
from execnet import gateway_base, gateway
from execnet.gateway_base import Message, Channel, ChannelFactory

def test_errors_on_execnet():
    assert hasattr(execnet, 'RemoteError')
    assert hasattr(execnet, 'TimeoutError')

def test_subprocess_interaction(anypython):
    line = gateway.popen_bootstrapline
    compile(line, 'xyz', 'exec')
    args = [str(anypython), '-c', line]
    popen = subprocess.Popen(args, bufsize=0, universal_newlines=True,
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    def send(line):
        popen.stdin.write(line)
        if sys.version_info > (3,0) or sys.platform.startswith("java"):
            popen.stdin.flush()
    def receive():
        return popen.stdout.readline()

    try:
        source = py.code.Source(read_write_loop, "read_write_loop()")
        repr_source = repr(str(source)) + "\n"
        sendline = repr_source
        send(sendline)
        s = receive()
        assert s == "ok\n"
        send("hello\n")
        s = receive()
        assert s == "received: hello\n"
        send("world\n")
        s = receive()
        assert s == "received: world\n"
        send('\n') # terminate loop
    finally:
        popen.stdin.close()
        popen.stdout.close()
        popen.wait()

def read_write_loop():
    import os, sys
    sys.stdout.write("ok\n")
    sys.stdout.flush()
    while 1:
        try:
            line = sys.stdin.readline()
            if not line.strip():
                break
            sys.stdout.write("received: %s" % line)
            sys.stdout.flush()
        except (IOError, EOFError):
            break

def test_io_message(anypython, tmpdir):
    check = tmpdir.join("check.py")
    check.write(py.code.Source(gateway_base, """
        try:
            from io import BytesIO
        except ImportError:
            from StringIO import StringIO as BytesIO
        import tempfile
        temp_out = BytesIO()
        temp_in = BytesIO()
        io = Popen2IO(temp_out, temp_in)
        unserializer = Unserializer(io)
        for i, msg_cls in Message._types.items():
            print ("checking %s %s" %(i, msg_cls))
            for data in "hello", "hello".encode('ascii'):
                msg1 = msg_cls(i, data)
                msg1.writeto(io)
                x = io.outfile.getvalue()
                io.outfile.truncate(0)
                io.outfile.seek(0)
                io.infile.seek(0)
                io.infile.write(x)
                io.infile.seek(0)
                msg2 = Message.readfrom(unserializer)
                assert msg1.channelid == msg2.channelid, (msg1, msg2)
                assert msg1.data == msg2.data
        print ("all passed")
    """))
    #out = py.process.cmdexec("%s %s" %(executable,check))
    out = anypython.sysexec(check)
    print (out)
    assert "all passed" in out

def test_popen_io(anypython, tmpdir):
    check = tmpdir.join("check.py")
    check.write(py.code.Source(gateway_base, """
        do_exec("io = init_popen_io()", globals())
        io.write("hello".encode('ascii'))
        s = io.read(1)
        assert s == "x".encode('ascii')
    """))
    from subprocess import Popen, PIPE
    args = [str(anypython), str(check)]
    proc = Popen(args, stdin=PIPE, stdout=PIPE, stderr=PIPE)
    proc.stdin.write("x".encode('ascii'))
    stdout, stderr = proc.communicate()
    print (stderr)
    ret = proc.wait()
    assert "hello".encode('ascii') in stdout


def test_rinfo_source(anypython, tmpdir):
    check = tmpdir.join("check.py")
    check.write(py.code.Source("""
        class Channel:
            def send(self, data):
                assert eval(repr(data), {}) == data
        channel = Channel()
        """, gateway.rinfo_source, """
        print ('all passed')
    """))
    out = anypython.sysexec(check)
    print (out)
    assert "all passed" in out

def test_geterrortext(anypython, tmpdir):
    check = tmpdir.join("check.py")
    check.write(py.code.Source(gateway_base, """
        class Arg:
            pass
        errortext = geterrortext((Arg, "1", 4))
        assert "Arg" in errortext
        import sys
        try:
            raise ValueError("17")
        except ValueError:
            excinfo = sys.exc_info()
            s = geterrortext(excinfo)
            assert "17" in s
            print ("all passed")
    """))
    out = anypython.sysexec(check)
    print (out)
    assert "all passed" in out

@py.test.mark.skipif("not hasattr(os, 'dup')")
def test_stdouterrin_setnull():
    cap = py.io.StdCaptureFD()
    io = gateway_base.init_popen_io()
    import os
    os.write(1, "hello".encode('ascii'))
    if os.name == "nt":
        os.write(2, "world")
    os.read(0, 1)
    out, err = cap.reset()
    assert not out
    assert not err

class PseudoChannel:
    def __init__(self):
        self._sent = []
        self._closed = []
        self.id = 1000
    def send(self, obj):
        self._sent.append(obj)
    def close(self, errortext=None):
        self._closed.append(errortext)

def test_exectask():
    io = py.io.BytesIO()
    gw = gateway_base.SlaveGateway(io, id="something")
    ch = PseudoChannel()
    gw.executetask((ch, ("raise ValueError()", None, {})))
    assert "ValueError" in str(ch._closed[0])


class TestMessage:
    def test_wire_protocol(self):
        for cls in Message._types.values():
            one = py.io.BytesIO()
            data = '23'.encode('ascii')
            cls(42, data).writeto(one)
            two = py.io.BytesIO(one.getvalue())
            unserializer = gateway_base.Unserializer(two)
            msg = Message.readfrom(unserializer)
            assert isinstance(msg, cls)
            assert msg.channelid == 42
            assert msg.data == data
            assert isinstance(repr(msg), str)
            # == "<Message.%s channelid=42 '23'>" %(msg.__class__.__name__, )

class TestPureChannel:
    def setup_method(self, method):
        self.fac = ChannelFactory(None)

    def test_factory_create(self):
        chan1 = self.fac.new()
        assert chan1.id == 1
        chan2 = self.fac.new()
        assert chan2.id == 3

    def test_factory_getitem(self):
        chan1 = self.fac.new()
        assert self.fac._channels[chan1.id] == chan1
        chan2 = self.fac.new()
        assert self.fac._channels[chan2.id] == chan2

    def test_channel_timeouterror(self):
        channel = self.fac.new()
        py.test.raises(IOError, channel.waitclose, timeout=0.01)

    def test_channel_makefile_incompatmode(self):
        channel = self.fac.new()
        py.test.raises(ValueError, 'channel.makefile("rw")')


class TestSourceOfFunction(object):

    def test_lambda_unsupported(self):
        py.test.raises(ValueError, gateway._source_of_function, lambda:1)

    def test_wrong_prototype_fails(self):
        def prototype(wrong):
            pass
        py.test.raises(ValueError, gateway._source_of_function, prototype)

    def test_function_without_known_source_fails(self):
        # this one wont be able to find the source
        mess = {}
        py.builtin.exec_('def fail(channel): pass', mess, mess)
        import inspect
        print(inspect.getsourcefile(mess['fail']))
        py.test.raises(ValueError, gateway._source_of_function, mess['fail'])

    def test_function_with_closure_fails(self):
        mess = {}
        def closure(channel):
            print(mess)

        py.test.raises(ValueError, gateway._source_of_function, closure)


    def test_source_of_nested_function(self):
        def working(channel):
            pass

        send_source = gateway._source_of_function(working)
        expected = 'def working(channel):\n    pass\n'
        assert send_source == expected


class TestGlobalFinder(object):

    def setup_class(cls):
        py.test.importorskip('ast')

    def check(self, func):
        src = py.code.Source(func)
        code = py.code.Code(func)
        return gateway._find_non_builtin_globals(str(src), code.raw)

    def test_local(self):
        def f(a, b, c):
            d = 3
            pass

        assert self.check(f) == []

    def test_global(self):
        def f(a, b):
            c = 3
            glob
            d = 4

        assert self.check(f) == ['glob']


    def test_builtin(self):
        def f():
            len

        assert self.check(f) == []

    def test_function_with_global_fails(self):
        def func(channel):
            test
        py.test.raises(ValueError, gateway._source_of_function, func)


def test_remote_exec_function_with_kwargs(anypython):
    def func(channel, data):
        channel.send(data)
    group = execnet.Group()
    gw = group.makegateway('popen//python=%s' % anypython)
    ch = gw.remote_exec(func, data=1)
    result = ch.receive()
    assert result == 1



def test_remote_exc_module_takes_no_kwargs():
    gw = execnet.makegateway()
    py.test.raises(TypeError, gw.remote_exec, gateway_base, kwarg=1)

def test_remote_exec_string_takes_no_kwargs():
    gw = execnet.makegateway()
    py.test.raises(TypeError, gw.remote_exec, 'pass', kwarg=1)

