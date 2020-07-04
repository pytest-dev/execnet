# -*- coding: utf-8 -*-
from __future__ import with_statement

import inspect
import os
import subprocess
import sys

import execnet
import py
import pytest
from execnet import gateway
from execnet import gateway_base
from execnet import gateway_io
from execnet.gateway_base import ChannelFactory
from execnet.gateway_base import Message
from execnet.gateway_base import Popen2IO

try:
    from StringIO import StringIO as BytesIO
except:
    from io import BytesIO


skip_win_pypy = pytest.mark.xfail(
    condition=hasattr(sys, "pypy_version_info") and sys.platform.startswith("win"),
    reason="failing on Windows on PyPy (#63)",
)


class TestSerializeAPI:
    pytestmark = [pytest.mark.parametrize("val", ["123", 42, [1, 2, 3], ["23", 25]])]

    def test_serializer_api(self, val):
        dumped = execnet.dumps(val)
        val2 = execnet.loads(dumped)
        assert val == val2

    def test_mmap(self, tmpdir, val):
        mmap = pytest.importorskip("mmap").mmap
        p = tmpdir.join("data")
        with p.open("wb") as f:
            f.write(execnet.dumps(val))
        f = p.open("r+b")
        m = mmap(f.fileno(), 0)
        val2 = execnet.load(m)
        assert val == val2

    def test_bytesio(self, val):
        f = py.io.BytesIO()
        execnet.dump(f, val)
        read = py.io.BytesIO(f.getvalue())
        val2 = execnet.load(read)
        assert val == val2


def test_serializer_api_version_error(monkeypatch):
    bchr = gateway_base.bchr
    monkeypatch.setattr(gateway_base, "DUMPFORMAT_VERSION", bchr(1))
    dumped = execnet.dumps(42)
    monkeypatch.setattr(gateway_base, "DUMPFORMAT_VERSION", bchr(2))
    pytest.raises(execnet.DataFormatError, lambda: execnet.loads(dumped))


def test_errors_on_execnet():
    assert hasattr(execnet, "RemoteError")
    assert hasattr(execnet, "TimeoutError")
    assert hasattr(execnet, "DataFormatError")


def test_subprocess_interaction(anypython):
    line = gateway_io.popen_bootstrapline
    compile(line, "xyz", "exec")
    args = [str(anypython), "-c", line]
    popen = subprocess.Popen(
        args,
        bufsize=0,
        universal_newlines=True,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
    )

    def send(line):
        popen.stdin.write(line)
        if sys.version_info > (3, 0) or sys.platform.startswith("java"):
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
        send("\n")  # terminate loop
    finally:
        popen.stdin.close()
        popen.stdout.close()
        popen.wait()


def read_write_loop():
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


def test_io_message(anypython, tmpdir, execmodel):
    check = tmpdir.join("check.py")
    check.write(
        py.code.Source(
            gateway_base,
            """
        try:
            from io import BytesIO
        except ImportError:
            from StringIO import StringIO as BytesIO
        import tempfile
        temp_out = BytesIO()
        temp_in = BytesIO()
        io = Popen2IO(temp_out, temp_in, get_execmodel({backend!r}))
        for i, handler in enumerate(Message._types):
            print ("checking %s %s" %(i, handler))
            for data in "hello", "hello".encode('ascii'):
                msg1 = Message(i, i, dumps(data))
                msg1.to_io(io)
                x = io.outfile.getvalue()
                io.outfile.truncate(0)
                io.outfile.seek(0)
                io.infile.seek(0)
                io.infile.write(x)
                io.infile.seek(0)
                msg2 = Message.from_io(io)
                assert msg1.channelid == msg2.channelid, (msg1, msg2)
                assert msg1.data == msg2.data, (msg1.data, msg2.data)
                assert msg1.msgcode == msg2.msgcode
        print ("all passed")
    """.format(
                backend=execmodel.backend
            ),
        )
    )
    # out = py.process.cmdexec("%s %s" %(executable,check))
    out = anypython.sysexec(check)
    print(out)
    assert "all passed" in out


def test_popen_io(anypython, tmpdir, execmodel):
    check = tmpdir.join("check.py")
    check.write(
        py.code.Source(
            gateway_base,
            """
        do_exec("io = init_popen_io(get_execmodel({backend!r}))", globals())
        io.write("hello".encode('ascii'))
        s = io.read(1)
        assert s == "x".encode('ascii')
    """.format(
                backend=execmodel.backend
            ),
        )
    )
    from subprocess import Popen, PIPE

    args = [str(anypython), str(check)]
    proc = Popen(args, stdin=PIPE, stdout=PIPE, stderr=PIPE)
    proc.stdin.write("x".encode("ascii"))
    stdout, stderr = proc.communicate()
    print(stderr)
    proc.wait()
    assert "hello".encode("ascii") in stdout


def test_popen_io_readloop(monkeypatch, execmodel):
    sio = BytesIO("test".encode("ascii"))
    io = Popen2IO(sio, sio, execmodel)
    real_read = io._read

    def newread(numbytes):
        if numbytes > 1:
            numbytes = numbytes - 1
        return real_read(numbytes)

    io._read = newread
    result = io.read(3)
    assert result == "tes".encode("ascii")


def test_rinfo_source(anypython, tmpdir):
    check = tmpdir.join("check.py")
    check.write(
        py.code.Source(
            """
        class Channel:
            def send(self, data):
                assert eval(repr(data), {}) == data
        channel = Channel()
        """,
            gateway.rinfo_source,
            """
        print ('all passed')
    """,
        )
    )
    out = anypython.sysexec(check)
    print(out)
    assert "all passed" in out


def test_geterrortext(anypython, tmpdir):
    check = tmpdir.join("check.py")
    check.write(
        py.code.Source(
            gateway_base,
            """
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
    """,
        )
    )
    out = anypython.sysexec(check)
    print(out)
    assert "all passed" in out


@pytest.mark.skipif("not hasattr(os, 'dup')")
def test_stdouterrin_setnull(execmodel):
    cap = py.io.StdCaptureFD()
    gateway_base.init_popen_io(execmodel)
    os.write(1, "hello".encode("ascii"))
    os.read(0, 1)
    out, err = cap.reset()
    assert not out
    assert not err


class PseudoChannel:
    class gateway:
        class _channelfactory:
            finished = False

    def __init__(self):
        self._sent = []
        self._closed = []
        self.id = 1000

    def send(self, obj):
        self._sent.append(obj)

    def close(self, errortext=None):
        self._closed.append(errortext)


def test_exectask(execmodel):
    io = py.io.BytesIO()
    io.execmodel = execmodel
    gw = gateway_base.WorkerGateway(io, id="something")
    ch = PseudoChannel()
    gw.executetask((ch, ("raise ValueError()", None, {})))
    assert "ValueError" in str(ch._closed[0])


class TestMessage:
    def test_wire_protocol(self):
        for i, handler in enumerate(Message._types):
            one = py.io.BytesIO()
            data = "23".encode("ascii")
            Message(i, 42, data).to_io(one)
            two = py.io.BytesIO(one.getvalue())
            msg = Message.from_io(two)
            assert msg.msgcode == i
            assert isinstance(msg, Message)
            assert msg.channelid == 42
            assert msg.data == data
            assert isinstance(repr(msg), str)


class TestPureChannel:
    @pytest.fixture
    def fac(self, execmodel):
        class Gateway:
            pass

        Gateway.execmodel = execmodel
        return ChannelFactory(Gateway)

    def test_factory_create(self, fac):
        chan1 = fac.new()
        assert chan1.id == 1
        chan2 = fac.new()
        assert chan2.id == 3

    def test_factory_getitem(self, fac):
        chan1 = fac.new()
        assert fac._channels[chan1.id] == chan1
        chan2 = fac.new()
        assert fac._channels[chan2.id] == chan2

    def test_channel_timeouterror(self, fac):
        channel = fac.new()
        pytest.raises(IOError, channel.waitclose, timeout=0.01)

    def test_channel_makefile_incompatmode(self, fac):
        channel = fac.new()
        with pytest.raises(ValueError):
            channel.makefile("rw")


class TestSourceOfFunction(object):
    def test_lambda_unsupported(self):
        pytest.raises(ValueError, gateway._source_of_function, lambda: 1)

    def test_wrong_prototype_fails(self):
        def prototype(wrong):
            pass

        pytest.raises(ValueError, gateway._source_of_function, prototype)

    def test_function_without_known_source_fails(self):
        # this one wont be able to find the source
        mess = {}
        py.builtin.exec_("def fail(channel): pass", mess, mess)
        print(inspect.getsourcefile(mess["fail"]))
        pytest.raises(ValueError, gateway._source_of_function, mess["fail"])

    def test_function_with_closure_fails(self):
        mess = {}

        def closure(channel):
            print(mess)

        pytest.raises(ValueError, gateway._source_of_function, closure)

    def test_source_of_nested_function(self):
        def working(channel):
            pass

        send_source = gateway._source_of_function(working).lstrip("\r\n")
        expected = "def working(channel):\n    pass\n"
        assert send_source == expected


class TestGlobalFinder(object):
    def check(self, func):
        src = py.code.Source(func)
        code = py.code.Code(func)
        return gateway._find_non_builtin_globals(str(src), code.raw)

    def test_local(self):
        def f(a, b, c):
            d = 3
            return d

        assert self.check(f) == []

    def test_global(self):
        def f(a, b):
            sys
            d = 4
            return d

        assert self.check(f) == ["sys"]

    def test_builtin(self):
        def f():
            len

        assert self.check(f) == []

    def test_function_with_global_fails(self):
        def func(channel):
            sys

        pytest.raises(ValueError, gateway._source_of_function, func)

    def test_method_call(self):
        # method names are reason
        # for the simple code object based heusteric failing
        def f(channel):
            channel.send(dict(testing=2))

        assert self.check(f) == []


@skip_win_pypy
def test_remote_exec_function_with_kwargs(anypython, makegateway):
    def func(channel, data):
        channel.send(data)

    gw = makegateway("popen//python=%s" % anypython)
    print("local version_info {!r}".format(sys.version_info))
    print("remote info: {}".format(gw._rinfo()))
    ch = gw.remote_exec(func, data=1)
    result = ch.receive()
    assert result == 1


def test_remote_exc__no_kwargs(makegateway):
    gw = makegateway()
    with pytest.raises(TypeError):
        gw.remote_exec(gateway_base, kwarg=1)
    with pytest.raises(TypeError):
        gw.remote_exec("pass", kwarg=1)


@skip_win_pypy
def test_remote_exec_inspect_stack(makegateway):
    gw = makegateway()
    ch = gw.remote_exec(
        """
        import inspect
        inspect.stack()
        import traceback
        channel.send('\\n'.join(traceback.format_stack()))
    """
    )
    assert 'File "<remote exec>"' in ch.receive()
    ch.waitclose()
