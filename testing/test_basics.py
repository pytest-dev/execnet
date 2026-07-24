# ruff: noqa: B018
from __future__ import annotations

import inspect
import os
import subprocess
import sys
import textwrap
from collections.abc import Callable
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any

import pytest

import execnet
from execnet import gateway
from execnet import gateway_base
from execnet.gateway_base import ChannelFactory
from execnet.gateway_base import ExecModel
from execnet.gateway_base import Message

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


IO_MESSAGE_EXTRA_SOURCE = """
from io import BytesIO

class BufIO:
    def __init__(self):
        self.buf = BytesIO()

    def write(self, data):
        self.buf.write(data)

    def read(self, numbytes):
        data = self.buf.read(numbytes)
        if len(data) < numbytes:
            raise EOFError("expected %d bytes" % numbytes)
        return data

for i, handler in enumerate(Message._types):
    print ("checking", i, handler)
    for data in "hello", "hello".encode('ascii'):
        io = BufIO()
        msg1 = Message(i, i, dumps(data))
        msg1.to_io(io)
        io.buf.seek(0)
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


def test_io_message(checker: Checker) -> None:
    out = checker.run_check(inspect.getsource(gateway_base) + IO_MESSAGE_EXTRA_SOURCE)
    print(out.stdout)
    assert "all passed" in out.stdout


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
def test_stdouterrin_setnull(capfd: pytest.CaptureFixture[str]) -> None:
    # _prepare_protocol_fds dups the stdio fds for the Message protocol and
    # points fd 0/1 at devnull; writes/reads on the original fds must go
    # nowhere.  Back up and restore the real fds around the call.
    from execnet import _trio_worker

    orig_stdin = sys.stdin
    orig_stdout = sys.stdout
    orig_fd0 = os.dup(0)
    orig_fd1 = os.dup(1)
    try:
        read_fd, write_fd = _trio_worker._prepare_protocol_fds()
        os.close(read_fd)
        os.close(write_fd)
        os.write(1, b"hello")
        os.read(0, 1)
        out, err = capfd.readouterr()
        assert not out
        assert not err
    finally:
        sys.stdin = orig_stdin
        sys.stdout = orig_stdout
        os.dup2(orig_fd0, 0)
        os.dup2(orig_fd1, 1)
        os.close(orig_fd0)
        os.close(orig_fd1)


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


class TestFrameDecoder:
    def _messages(self) -> list[Message]:
        return [
            Message(Message.CHANNEL_DATA, 1, b"x" * 20),
            Message(Message.STATUS, 42, b""),
            Message(Message.CHANNEL_DATA, 7, b"y"),
        ]

    def test_single_feed_yields_all(self) -> None:
        decoder = gateway_base.FrameDecoder()
        blob = b"".join(m.pack() for m in self._messages())
        got = list(decoder.feed(blob))
        assert [(m.msgcode, m.channelid, m.data) for m in got] == [
            (m.msgcode, m.channelid, m.data) for m in self._messages()
        ]
        decoder.close()

    @pytest.mark.parametrize("chunksize", [1, 2, 3, 8, 9, 10, 13])
    def test_adversarial_chunk_splits(self, chunksize: int) -> None:
        decoder = gateway_base.FrameDecoder()
        blob = b"".join(m.pack() for m in self._messages())
        got: list[Message] = []
        for start in range(0, len(blob), chunksize):
            got.extend(decoder.feed(blob[start : start + chunksize]))
        assert [(m.msgcode, m.channelid, m.data) for m in got] == [
            (m.msgcode, m.channelid, m.data) for m in self._messages()
        ]
        decoder.close()

    def test_close_mid_frame_raises(self) -> None:
        decoder = gateway_base.FrameDecoder()
        blob = Message(Message.CHANNEL_DATA, 1, b"hello").pack()
        assert list(decoder.feed(blob[:-2])) == []
        with pytest.raises(EOFError, match="mid-frame"):
            decoder.close()

    def test_feed_buffers_even_when_not_iterated(self) -> None:
        decoder = gateway_base.FrameDecoder()
        blob = Message(Message.CHANNEL_DATA, 5, b"data").pack()
        decoder.feed(blob[:4])  # result deliberately not iterated
        (msg,) = decoder.feed(blob[4:])
        assert (msg.msgcode, msg.channelid, msg.data) == (
            Message.CHANNEL_DATA,
            5,
            b"data",
        )

    def test_memory_stream_roundtrip(self) -> None:
        """Protocol-level: frames sent over a trio memory stream pair arrive
        intact through the receive_some + FrameDecoder loop."""
        import trio
        import trio.testing

        messages = self._messages()

        async def main() -> list[Message]:
            ours, theirs = trio.testing.memory_stream_pair()
            received: list[Message] = []

            async def sender() -> None:
                for m in messages:
                    await theirs.send_all(m.pack())
                await theirs.send_eof()

            async def receiver() -> None:
                decoder = gateway_base.FrameDecoder()
                while True:
                    data = await ours.receive_some(4096)
                    if not data:
                        decoder.close()
                        break
                    received.extend(decoder.feed(data))

            async with trio.open_nursery() as nursery:
                nursery.start_soon(sender)
                nursery.start_soon(receiver)
            return received

        received = trio.run(main)
        assert [(m.msgcode, m.channelid, m.data) for m in received] == [
            (m.msgcode, m.channelid, m.data) for m in messages
        ]


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
