# ruff: noqa: B018
from __future__ import annotations

import inspect
import os
import subprocess
import sys
import textwrap
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any
from typing import Callable

import pytest

import execnet
from execnet import gateway
from execnet import gateway_base
from execnet import gateway_io
from execnet.gateway_base import ChannelFactory
from execnet.gateway_base import ExecModel
from execnet.gateway_base import Message
from execnet.gateway_base import Popen2IO

skip_win_pypy = pytest.mark.xfail(
    condition=hasattr(sys, "pypy_version_info") and sys.platform.startswith("win"),
    reason="failing on Windows on PyPy (#63)",
)


@pytest.mark.parametrize("val", ["123", 42, [1, 2, 3], ["23", 25]])
class TestSerializeAPI:
    def test_serializer_api(self, val: object) -> None:
        dumped = execnet.dumps(val)
        val2 = execnet.loads(dumped)
        assert val == val2

    def test_mmap(self, tmp_path: Path, val: object) -> None:
        mmap = pytest.importorskip("mmap").mmap
        p = tmp_path / "data.bin"

        p.write_bytes(execnet.dumps(val))
        with p.open("r+b") as f:
            m = mmap(f.fileno(), 0)
            val2 = execnet.load(m)
        assert val == val2

    def test_bytesio(self, val: object) -> None:
        f = BytesIO()
        execnet.dump(f, val)
        read = BytesIO(f.getvalue())
        val2 = execnet.load(read)
        assert val == val2


def test_serializer_api_version_error(monkeypatch: pytest.MonkeyPatch) -> None:
    bchr = gateway_base.bchr
    monkeypatch.setattr(gateway_base, "DUMPFORMAT_VERSION", bchr(1))
    dumped = execnet.dumps(42)
    monkeypatch.setattr(gateway_base, "DUMPFORMAT_VERSION", bchr(2))
    pytest.raises(execnet.DataFormatError, lambda: execnet.loads(dumped))


def test_errors_on_execnet() -> None:
    assert hasattr(execnet, "RemoteError")
    assert hasattr(execnet, "TimeoutError")
    assert hasattr(execnet, "DataFormatError")


def test_subprocess_interaction(anypython: str) -> None:
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

    assert popen.stdin is not None
    assert popen.stdout is not None

    def send(line: str) -> None:
        assert popen.stdin is not None
        popen.stdin.write(line)
        popen.stdin.flush()

    def receive() -> str:
        assert popen.stdout is not None
        return popen.stdout.readline()

    try:
        source = inspect.getsource(read_write_loop) + "read_write_loop()"
        send(repr(source) + "\n")
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


def read_write_loop() -> None:
    sys.stdout.write("ok\n")
    sys.stdout.flush()
    while 1:
        try:
            line = sys.stdin.readline()
            if not line.strip():
                break
            sys.stdout.write("received: %s" % line)
            sys.stdout.flush()
        except (OSError, EOFError):
            break


IO_MESSAGE_EXTRA_SOURCE = """
import sys
backend = sys.argv[1]
from io import BytesIO
import tempfile
temp_out = BytesIO()
temp_in = BytesIO()
io = Popen2IO(temp_out, temp_in, get_execmodel(backend))
for i, handler in enumerate(Message._types):
    print ("checking", i, handler)
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
"""


@dataclass
class Checker:
    python: str
    path: Path
    idx: int = 0

    def run_check(
        self, script: str, *extra_args: str, **process_args: Any
    ) -> subprocess.CompletedProcess[str]:
        self.idx += 1
        check_path = self.path / f"check{self.idx}.py"
        check_path.write_text(script)
        return subprocess.run(
            [self.python, os.fspath(check_path), *extra_args],
            capture_output=True,
            text=True,
            check=True,
            **process_args,
        )


@pytest.fixture
def checker(anypython: str, tmp_path: Path) -> Checker:
    return Checker(python=anypython, path=tmp_path)


def test_io_message(checker: Checker, execmodel: ExecModel) -> None:
    out = checker.run_check(
        inspect.getsource(gateway_base) + IO_MESSAGE_EXTRA_SOURCE, execmodel.backend
    )
    print(out.stdout)
    assert "all passed" in out.stdout


def test_popen_io(checker: Checker, execmodel: ExecModel) -> None:
    out = checker.run_check(
        inspect.getsource(gateway_base)
        + f"""
io = init_popen_io(get_execmodel({execmodel.backend!r}))
io.write(b"hello")
s = io.read(1)
assert s == b"x"
""",
        input="x",
    )
    print(out.stderr)
    assert "hello" in out.stdout


def test_popen_io_readloop(execmodel: ExecModel) -> None:
    sio = BytesIO(b"test")
    io = Popen2IO(sio, sio, execmodel)
    real_read = io._read

    def newread(numbytes: int) -> bytes:
        if numbytes > 1:
            numbytes = numbytes - 1
        return real_read(numbytes)  # type: ignore[no-any-return]

    io._read = newread
    result = io.read(3)
    assert result == b"tes"


def test_rinfo_source(checker: Checker) -> None:
    out = checker.run_check(
        f"""
class Channel:
    def send(self, data):
        assert eval(repr(data), {{}}) == data
channel = Channel()
{inspect.getsource(gateway.rinfo_source)}
print ('all passed')
"""
    )

    print(out.stdout)
    assert "all passed" in out.stdout


def test_geterrortext(checker: Checker) -> None:
    out = checker.run_check(
        inspect.getsource(gateway_base)
        + """
class Arg(Exception):
    pass
errortext = geterrortext(Arg())
assert "Arg" in errortext
try:
    raise ValueError("17")
except ValueError as exc:
    s = geterrortext(exc)
    assert "17" in s
    print ("all passed")
    """
    )
    print(out.stdout)
    assert "all passed" in out.stdout


@pytest.mark.skipif("not hasattr(os, 'dup')")
def test_stdouterrin_setnull(
    execmodel: ExecModel, capfd: pytest.CaptureFixture[str]
) -> None:
    # Backup and restore stdin state, and rely on capfd to handle
    # this for stdout and stderr.
    orig_stdin = sys.stdin
    orig_stdin_fd = os.dup(0)
    try:
        # The returned Popen2IO instance can be garbage collected
        # prematurely since we don't hold a reference here, but we
        # tolerate this because it is intended to leave behind a
        # sane state afterwards.
        gateway_base.init_popen_io(execmodel)
        os.write(1, b"hello")
        os.read(0, 1)
        out, err = capfd.readouterr()
        assert not out
        assert not err
    finally:
        sys.stdin = orig_stdin
        os.dup2(orig_stdin_fd, 0)
        os.close(orig_stdin_fd)


class PseudoChannel:
    class gateway:
        class _channelfactory:
            finished = False

    def __init__(self) -> None:
        self._sent: list[object] = []
        self._closed: list[str | None] = []
        self.id = 1000

    def send(self, obj: object) -> None:
        self._sent.append(obj)

    def close(self, errortext: str | None = None) -> None:
        self._closed.append(errortext)


def test_exectask(execmodel: ExecModel) -> None:
    io = BytesIO()
    io.execmodel = execmodel  # type: ignore[attr-defined]
    gw = gateway_base.WorkerGateway(io, id="something")  # type: ignore[arg-type]
    ch = PseudoChannel()
    gw.executetask((ch, ("raise ValueError()", None, {})))  # type: ignore[arg-type]
    assert "ValueError" in str(ch._closed[0])


class TestMessage:
    def test_wire_protocol(self) -> None:
        for i, handler in enumerate(Message._types):
            one = BytesIO()
            data = b"23"
            # TODO(typing): Maybe make this work.
            Message(i, 42, data).to_io(one)  # type: ignore[arg-type]
            two = BytesIO(one.getvalue())
            msg = Message.from_io(two)
            assert msg.msgcode == i
            assert isinstance(msg, Message)
            assert msg.channelid == 42
            assert msg.data == data
            assert isinstance(repr(msg), str)


class TestPureChannel:
    @pytest.fixture
    def fac(self, execmodel: ExecModel) -> ChannelFactory:
        class FakeGateway:
            def _trace(self, *args) -> None:
                pass

            def _send(self, *k) -> None:
                pass

        FakeGateway.execmodel = execmodel  # type: ignore[attr-defined]
        return ChannelFactory(FakeGateway())  # type: ignore[arg-type]

    def test_factory_create(self, fac: ChannelFactory) -> None:
        chan1 = fac.new()
        assert chan1.id == 1
        chan2 = fac.new()
        assert chan2.id == 3

    def test_factory_getitem(self, fac: ChannelFactory) -> None:
        chan1 = fac.new()
        assert fac._channels[chan1.id] == chan1
        chan2 = fac.new()
        assert fac._channels[chan2.id] == chan2

    def test_channel_timeouterror(self, fac: ChannelFactory) -> None:
        channel = fac.new()
        pytest.raises(IOError, channel.waitclose, timeout=0.01)

    def test_channel_makefile_incompatmode(self, fac) -> None:
        channel = fac.new()
        with pytest.raises(ValueError):
            channel.makefile("rw")


class TestSourceOfFunction:
    def test_lambda_unsupported(self) -> None:
        pytest.raises(ValueError, gateway._source_of_function, lambda: 1)

    def test_wrong_prototype_fails(self) -> None:
        def prototype(wrong) -> None:
            pass

        pytest.raises(ValueError, gateway._source_of_function, prototype)

    def test_function_without_known_source_fails(self) -> None:
        # this one won't be able to find the source
        mess: dict[str, Any] = {}
        exec("def fail(channel): pass", mess, mess)
        print(inspect.getsourcefile(mess["fail"]))
        with pytest.raises(ValueError):
            gateway._source_of_function(mess["fail"])

    def test_function_with_closure_fails(self) -> None:
        mess: dict[str, Any] = {}

        def closure(channel: object) -> None:
            print(mess)

        with pytest.raises(ValueError):
            gateway._source_of_function(closure)

    def test_source_of_nested_function(self) -> None:
        def working(channel: object) -> None:
            pass

        send_source = gateway._source_of_function(working).lstrip("\r\n")
        expected = "def working(channel: object) -> None:\n    pass\n"
        assert send_source == expected


class TestGlobalFinder:
    def check(self, func) -> list[str]:
        src = textwrap.dedent(inspect.getsource(func))
        code = func.__code__
        return gateway._find_non_builtin_globals(src, code)

    def test_local(self) -> None:
        def f(a, b, c):
            d = 3
            return d

        assert self.check(f) == []

    def test_global(self) -> None:
        def f(a, b):
            sys
            d = 4
            return d

        assert self.check(f) == ["sys"]

    def test_builtin(self) -> None:
        def f() -> None:
            len

        assert self.check(f) == []

    def test_function_with_global_fails(self) -> None:
        def func(channel) -> None:
            sys

        pytest.raises(ValueError, gateway._source_of_function, func)

    def test_method_call(self) -> None:
        # method names are reason
        # for the simple code object based heusteric failing
        def f(channel):
            channel.send(dict(testing=2))

        assert self.check(f) == []


@skip_win_pypy
def test_remote_exec_function_with_kwargs(
    anypython: str, makegateway: Callable[[str], gateway.Gateway]
) -> None:
    def func(channel, data) -> None:
        channel.send(data)

    gw = makegateway("popen//python=%s" % anypython)
    print(f"local version_info {sys.version_info!r}")
    print(f"remote info: {gw._rinfo()}")
    ch = gw.remote_exec(func, data=1)
    result = ch.receive()
    assert result == 1


def test_remote_exc__no_kwargs(makegateway: Callable[[], gateway.Gateway]) -> None:
    gw = makegateway()
    with pytest.raises(TypeError):
        gw.remote_exec(gateway_base, kwarg=1)
    with pytest.raises(TypeError):
        gw.remote_exec("pass", kwarg=1)


@skip_win_pypy
def test_remote_exec_inspect_stack(
    makegateway: Callable[[], gateway.Gateway],
) -> None:
    gw = makegateway()
    ch = gw.remote_exec(
        """
        import inspect
        inspect.stack()
        import traceback
        channel.send('\\n'.join(traceback.format_stack()))
    """
    )
    received = ch.receive()
    assert isinstance(received, str)
    assert 'File "<remote exec>"' in received
    ch.waitclose()
