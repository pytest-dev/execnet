"""The ``execnet`` command line, and the transports it exposes.

``execnet worker`` is the launch contract between a coordinator and the
process it starts.  These tests drive it the way a coordinator does --
including the transports that keep the protocol off the worker's stdio.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
from typing import Any
from typing import cast

import pytest

import execnet
from execnet import _cli
from execnet import _provision
from execnet._message import Message

posix_only = pytest.mark.skipif(
    sys.platform.startswith("win"), reason="needs POSIX fd passing / unix sockets"
)

TESTTIMEOUT = 30.0


def worker_config(**overrides: object) -> dict[str, object]:
    config: dict[str, object] = {
        "id": "cli-test-worker",
        "profile": "thread",
        "execmodel": "thread",
        "wait": "thread",
        "coordinator_version": execnet.__version__,
    }
    config.update(overrides)
    return config


def config_frame(**overrides: object) -> bytes:
    """What a coordinator sends first, on every transport."""
    return Message(
        Message.GATEWAY_CONFIG, 0, json.dumps(worker_config(**overrides)).encode()
    ).pack()


def recv_exactly(sock: socket.socket, count: int) -> bytes:
    chunks = []
    while count:
        chunk = sock.recv(count)
        if not chunk:
            raise EOFError(f"closed with {count} bytes to go")
        chunks.append(chunk)
        count -= len(chunk)
    return b"".join(chunks)


def read_reply(sock: socket.socket) -> dict[str, Any]:
    """The worker's answer to the config frame: serving, or refusing and why."""
    msgcode, _channel, length = Message.from_header(recv_exactly(sock, 9))
    assert msgcode == Message.GATEWAY_CONFIG
    reply: dict[str, Any] = json.loads(recv_exactly(sock, length))
    return reply


def handshake(sock: socket.socket, **overrides: object) -> dict[str, Any]:
    """Drive the whole worker handshake the way a coordinator does."""
    sock.sendall(config_frame(**overrides))
    return read_reply(sock)


class TestInfo:
    def test_info_reports_what_a_coordinator_needs(self) -> None:
        out = subprocess.run(
            [sys.executable, "-m", "execnet", "info"],
            capture_output=True,
            text=True,
            check=True,
        )
        info = json.loads(out.stdout)
        assert info["execnet"] == execnet.__version__
        assert info["trio"] is not None
        assert info["executable"]
        assert "stdio" in info["protocols"]

    def test_info_matches_the_in_process_view(self) -> None:
        assert _cli.interpreter_info()["execnet"] == execnet.__version__

    def test_probe_uses_info(self) -> None:
        _provision.target_info.cache_clear()
        info = _provision.target_info(sys.executable)
        assert info is not None
        assert info["execnet"] == execnet.__version__
        assert _provision.target_has_execnet(sys.executable)

    def test_probe_rejects_an_interpreter_without_execnet(self, tmp_path) -> None:
        # a python that cannot import execnet fails the probe, which is what
        # sends it down the uv-provisioning path
        _provision.target_info.cache_clear()
        fake = tmp_path / "fake-python"
        fake.write_text("#!/bin/sh\nexit 1\n")
        fake.chmod(0o755)
        assert _provision.target_info(str(fake)) is None
        assert not _provision.target_has_execnet(str(fake))


class TestVersionSkew:
    """A worker refuses a coordinator it cannot speak the protocol with.

    The protocol is unversioned, so a major/minor skew has no defined
    behaviour.  The refusal answers the config frame, so the reason travels
    back to whoever asked for the gateway instead of only to a stderr that
    may be pointed anywhere.
    """

    def test_the_same_version_is_fine(self) -> None:
        from execnet import _trio_worker

        assert _trio_worker._version_refusal(execnet.__version__) is None

    def test_a_patch_level_difference_is_tolerated(self) -> None:
        from execnet import _trio_worker

        major, minor = _trio_worker._rough_version(execnet.__version__)
        assert _trio_worker._version_refusal(f"{major}.{minor}.999") is None

    def test_an_unparsable_version_is_not_second_guessed(self) -> None:
        from execnet import _trio_worker

        assert _trio_worker._version_refusal("some-vendored-build") is None

    def test_a_minor_difference_is_refused(self) -> None:
        from execnet import _trio_worker

        major, minor = _trio_worker._rough_version(execnet.__version__)
        refusal = _trio_worker._version_refusal(f"{major}.{minor + 1}.0")
        assert refusal is not None
        assert "version mismatch" in refusal
        assert _trio_worker.IGNORE_VERSION_SKEW in refusal

    def test_the_env_override_downgrades_it_to_a_warning(
        self, monkeypatch: pytest.MonkeyPatch, capfd
    ) -> None:
        from execnet import _trio_worker

        major, minor = _trio_worker._rough_version(execnet.__version__)
        monkeypatch.setenv(_trio_worker.IGNORE_VERSION_SKEW, "1")
        assert _trio_worker._version_refusal(f"{major}.{minor + 1}.0") is None
        assert "version mismatch" in capfd.readouterr()[1]

    def test_the_override_also_comes_from_the_config_env(self, capfd) -> None:
        # config env: values are not applied until _apply_worker_setup, which
        # runs after the check, so the check has to read them itself
        from execnet import _trio_worker

        major, minor = _trio_worker._rough_version(execnet.__version__)
        assert (
            _trio_worker._version_refusal(
                f"{major}.{minor + 1}.0", {_trio_worker.IGNORE_VERSION_SKEW: "1"}
            )
            is None
        )
        assert "version mismatch" in capfd.readouterr()[1]

    @posix_only
    def test_a_skewed_worker_says_so_on_the_wire(self) -> None:
        from execnet import _trio_worker

        # derived, never spelled out: a literal "impossible" version is only
        # impossible until an environment has it.  A wheel built from a
        # checkout without tags is 0.1.dev1, which is what CI installs -- so
        # a hardcoded 0.1.2 matched there, the worker started, and this
        # blocked on its output until the test timed out.
        major, _ = _trio_worker._rough_version(execnet.__version__)
        skewed = f"{major + 1}.0.0"
        ours, theirs = socket.socketpair()
        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "execnet",
                "worker",
                "--protocol-fd",
                str(theirs.fileno()),
            ],
            pass_fds=(theirs.fileno(),),
            stderr=subprocess.PIPE,
            text=True,
        )
        theirs.close()
        try:
            reply = handshake(ours, coordinator_version=skewed)
            assert reply["ok"] is False
            assert "version mismatch" in reply["error"]
            assert _trio_worker.IGNORE_VERSION_SKEW in reply["error"]
        finally:
            ours.close()
            assert proc.wait(timeout=TESTTIMEOUT) != 0
            assert "version mismatch" in (proc.stderr.read() if proc.stderr else "")


class TestArgumentGrammar:
    def test_protocol_flags_are_mutually_exclusive(self) -> None:
        with pytest.raises(SystemExit):
            _cli._build_parser().parse_args(
                ["worker", "--protocol-fd", "3", "--protocol-connect", "unix:/x"]
            )

    def test_there_is_no_way_to_put_a_config_in_argv(self) -> None:
        # the worker config arrives as a frame; --config-fd is left only for
        # the Windows share blob, which describes the stream itself
        for flag in ("--config", "--config-file"):
            with pytest.raises(SystemExit):
                _cli._build_parser().parse_args(["worker", flag, "{}"])

    @pytest.mark.parametrize(("value", "expected"), [("3", (3,)), ("4,5", (4, 5))])
    def test_protocol_fd_accepts_one_fd_or_a_pair(
        self, value: str, expected: tuple[int, ...]
    ) -> None:
        ns = _cli._build_parser().parse_args(["worker", "--protocol-fd", value])
        assert ns.protocol_fd == expected

    def test_protocol_fd_rejects_nonsense(self) -> None:
        with pytest.raises(SystemExit):
            _cli._build_parser().parse_args(["worker", "--protocol-fd", "a,b,c"])

    @pytest.mark.parametrize(
        ("address", "expected"),
        [
            ("unix:/tmp/x.sock", ("unix", "/tmp/x.sock")),
            ("localhost:8888", ("tcp", ("localhost", 8888))),
            (":8888", ("tcp", ("localhost", 8888))),
        ],
    )
    def test_parse_address(self, address: str, expected: tuple[str, Any]) -> None:
        from execnet._trio_worker import parse_address

        assert parse_address(address) == expected

    def test_parse_address_rejects_a_bare_path(self) -> None:
        from execnet._trio_worker import parse_address

        with pytest.raises(ValueError, match="unix:/path or host:port"):
            parse_address("/tmp/x.sock")


@posix_only
class TestProtocolFd:
    """A worker serving over an inherited socket, driven by hand."""

    def test_socketpair_roundtrip(self) -> None:
        ours, theirs = socket.socketpair()
        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "execnet",
                "worker",
                "--protocol-fd",
                str(theirs.fileno()),
            ],
            pass_fds=(theirs.fileno(),),
        )
        theirs.close()
        try:
            reply = handshake(ours)
            assert reply["ok"] is True
            assert reply["pid"] == proc.pid
            assert reply["profile"] == "thread"
        finally:
            ours.close()
            proc.terminate()
            proc.wait(timeout=TESTTIMEOUT)

    def test_a_plain_pipe_fd_is_rejected(self) -> None:
        # one fd has to be bidirectional; a pipe end needs the read,write form
        read_fd, write_fd = os.pipe()
        try:
            out = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "execnet",
                    "worker",
                    "--protocol-fd",
                    str(read_fd),
                ],
                pass_fds=(read_fd,),
                capture_output=True,
                text=True,
                timeout=TESTTIMEOUT,
                check=False,
            )
        finally:
            os.close(read_fd)
            os.close(write_fd)
        assert out.returncode != 0
        assert "not a socket" in out.stderr


class TestConfigDelivery:
    """The config is a frame on the protocol stream, and nothing else.

    It carries ``env:`` values, so argv is the one place it must never be:
    ``/proc`` is world-readable on the local machine exactly as ``ps`` is
    on a remote one.
    """

    def test_a_worker_needs_no_argv_beyond_its_transport(self) -> None:
        spec = execnet.XSpec("popen//env:SECRET=hunter2//chdir=/tmp")
        spec.id = "gw0"
        from execnet._trio_gateway import popen_worker_argv

        argv = popen_worker_argv(spec, "--protocol-fd", "7")
        assert argv[-2:] == ["--protocol-fd", "7"]
        assert not any("hunter2" in token for token in argv)
        assert not any("chdir" in token for token in argv)

    @posix_only
    def test_the_config_frame_configures_the_worker(self, tmp_path) -> None:
        ours, theirs = socket.socketpair()
        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "execnet",
                "worker",
                "--protocol-fd",
                str(theirs.fileno()),
            ],
            pass_fds=(theirs.fileno(),),
        )
        theirs.close()
        try:
            reply = handshake(ours, id="configured", chdir=str(tmp_path))
            assert reply["ok"] is True
        finally:
            ours.close()
            proc.terminate()
            proc.wait(timeout=TESTTIMEOUT)

    def test_config_fd_carries_only_the_share_blob(self, tmp_path) -> None:
        path = tmp_path / "local.json"
        path.write_text(json.dumps({"protocol_share": "abc"}))
        with path.open() as stream:
            ns = _cli._build_parser().parse_args(
                ["worker", "--protocol-share", "--config-fd", str(stream.fileno())]
            )
            assert _cli._load_local_config(ns) == {"protocol_share": "abc"}

    def test_no_config_fd_means_no_local_config(self) -> None:
        ns = _cli._build_parser().parse_args(["worker"])
        assert _cli._load_local_config(ns) == {}


class TestTransportSelection:
    def test_a_spawned_worker_defaults_to_the_socket_transport(self) -> None:
        # every platform now: POSIX hands over the fd, Windows duplicates the
        # socket with share().  Only a host that can do neither gets stdio.
        spec = execnet.XSpec("popen")
        expected = "socket" if _provision.socket_handoff_available() else "stdio"
        assert (
            _provision.resolve_transport(
                spec, available=_provision.socket_handoff_available()
            )
            == expected
        )
        assert expected == "socket"

    def test_explicit_wins(self) -> None:
        assert _provision.resolve_transport(
            execnet.XSpec("popen//transport=stdio")
        ) == ("stdio")

    def test_unknown_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="unknown transport"):
            _provision.resolve_transport(
                execnet.XSpec("popen//transport=carrier-pigeon")
            )

    def test_unavailable_falls_back_when_unasked(self) -> None:
        assert (
            _provision.resolve_transport(execnet.XSpec("popen"), available=False)
            == "stdio"
        )

    def test_asking_for_an_impossible_transport_is_an_error(self) -> None:
        # the alternative is a gateway that hangs waiting for a worker that
        # was never able to reach us -- which is what ssh on Windows did
        with pytest.raises(ValueError, match="not available"):
            _provision.resolve_transport(
                execnet.XSpec("ssh=host//transport=socket"), available=False
            )

    def test_windows_hands_a_socket_over_by_duplicating_it(self) -> None:
        # `subprocess` refuses pass_fds there, so the capability comes from
        # socket.share() instead -- including on PyPy, once the socket is
        # handed over as a socket rather than rebuilt from its handle
        if _provision.socket_share_required():
            assert _provision.socket_handoff_available()

    def test_ssh_cannot_dial_back_on_windows(self) -> None:
        # no AF_UNIX in CPython there, and Win32-OpenSSH cannot -R a unix socket
        assert _provision.ssh_dialback_available() == (
            not _provision.socket_share_required()
        )

    def test_socket_transport_roundtrip(self) -> None:
        # explicitly, on every platform: this is the only coverage the
        # Windows socket.share() handoff gets
        group = execnet.Group()
        try:
            gateway = group.makegateway("popen//transport=socket")
            channel = gateway.remote_exec("channel.send(6 * 7)")
            assert channel.receive(TESTTIMEOUT) == 42
        finally:
            group.terminate(timeout=5.0)

    def test_socket_transport_keeps_the_protocol_off_stdio(self) -> None:
        # whichever handoff this platform has, the point is the same: the
        # protocol is named on the command line, so it is not fd 0/1
        expected = (
            "--protocol-share"
            if _provision.socket_share_required()
            else "--protocol-fd"
        )
        group = execnet.Group()
        try:
            gateway = group.makegateway("popen")
            channel = gateway.remote_exec(
                "import sys; channel.send(sys.argv)",
            )
            argv = cast("list[str]", channel.receive(TESTTIMEOUT))
            assert expected in argv
        finally:
            group.terminate(timeout=5.0)

    def test_stdio_transport_still_works(self) -> None:
        group = execnet.Group()
        try:
            gateway = group.makegateway("popen//transport=stdio")
            channel = gateway.remote_exec("channel.send(6 * 7)")
            assert channel.receive(TESTTIMEOUT) == 42
        finally:
            group.terminate(timeout=5.0)


class TestWorkerStdio:
    """Whose stdio is it? The code the worker runs, unless told otherwise."""

    def test_socket_transport_inherits_stdio(self, capfd) -> None:
        group = execnet.Group()
        try:
            gateway = group.makegateway("popen")
            gateway.remote_exec("print('INHERITED-STDOUT')").waitclose(TESTTIMEOUT)
        finally:
            group.terminate(timeout=5.0)
        out, _ = capfd.readouterr()
        assert "INHERITED-STDOUT" in out

    def test_stdio_transport_folds_stdout_onto_stderr(self, capfd) -> None:
        # the protocol owns fd 1 here, so remote output cannot go there --
        # but it lands on stderr rather than being discarded
        group = execnet.Group()
        try:
            gateway = group.makegateway("popen//transport=stdio")
            gateway.remote_exec("print('FOLDED-ONTO-STDERR')").waitclose(TESTTIMEOUT)
        finally:
            group.terminate(timeout=5.0)
        out, err = capfd.readouterr()
        assert "FOLDED-ONTO-STDERR" not in out
        assert "FOLDED-ONTO-STDERR" in err

    @posix_only
    def test_stdin_can_be_sent_to_devnull(self) -> None:
        group = execnet.Group()
        try:
            gateway = group.makegateway("popen//stdin=devnull")
            channel = gateway.remote_exec(
                "import os; channel.send(os.readlink('/proc/self/fd/0'))"
            )
            assert channel.receive(TESTTIMEOUT) == "/dev/null"
        finally:
            group.terminate(timeout=5.0)

    @posix_only
    def test_stdout_can_be_silenced(self, capfd) -> None:
        group = execnet.Group()
        try:
            gateway = group.makegateway("popen//stdout=devnull")
            gateway.remote_exec("print('SILENCED')").waitclose(TESTTIMEOUT)
        finally:
            group.terminate(timeout=5.0)
        out, err = capfd.readouterr()
        assert "SILENCED" not in out
        assert "SILENCED" not in err


@posix_only
class TestListenTransport:
    def test_worker_listens_and_reports_its_address(self) -> None:
        directory = tempfile.mkdtemp(prefix="execnet-cli-test-")
        path = os.path.join(directory, "gw.sock")
        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "execnet",
                "worker",
                "--protocol-listen",
                f"unix:{path}",
            ],
            stdout=subprocess.PIPE,
        )
        try:
            assert proc.stdout is not None
            announced = json.loads(proc.stdout.readline())
            assert announced == {"listening": path}
            client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            client.settimeout(TESTTIMEOUT)
            client.connect(path)
            try:
                # a worker that listens is configured like any other: the
                # coordinator that reaches it sends the config frame
                assert handshake(client)["ok"] is True
            finally:
                client.close()
        finally:
            proc.terminate()
            proc.wait(timeout=TESTTIMEOUT)
            shutil.rmtree(directory, ignore_errors=True)


class TestServerCommand:
    def test_server_is_the_socketserver(self) -> None:
        parser = _cli._build_parser()
        ns = parser.parse_args(["server", "127.0.0.1:0", "--once"])
        assert ns.hostport == "127.0.0.1:0"
        assert ns.once is True

    def test_socketserver_alias_warns(self, monkeypatch: pytest.MonkeyPatch) -> None:
        called: list[list[str]] = []
        monkeypatch.setattr(_cli, "main", called.append)
        with pytest.warns(DeprecationWarning, match="execnet server"):
            _cli.socketserver_main([":0", "--once"])
        assert called == [["server", ":0", "--once"]]


needs_provisioning = pytest.mark.skipif(
    not _provision.provisioning_available(),
    reason="a dev execnet installed without its source tree cannot build a"
    " wheel to provision a remote with",
)


class TestRemoteCommand:
    """What ends up on the remote host's command line."""

    pytestmark = needs_provisioning

    def test_no_config_reaches_the_remote_argv(self) -> None:
        # env: values are secrets often enough; the remote argv is readable
        # by every user on that host via ps.  There is no config here at all
        # any more -- it arrives as a frame on the connection.
        spec = execnet.XSpec("ssh=host//id=gw0//env:TOKEN=s3cr3t//chdir=/srv")
        spec.profile = "thread"
        command = _provision.ssh_remote_command(
            spec, "--protocol-connect", "unix:/tmp/x.sock"
        )
        assert "s3cr3t" not in command
        assert "chdir" not in command
        assert "--config" not in command
        assert command.endswith("--protocol-connect unix:/tmp/x.sock")

    def test_launch_command_frames_nothing_in_band(self) -> None:
        spec = execnet.XSpec("ssh=host//id=gw0")
        spec.profile = "thread"
        command = _provision.ssh_remote_command(spec)
        # the wheel travels on its own connection now
        assert "head -c" not in command
        assert "mktemp -d" not in command

    def test_dialback_argv_forwards_a_unix_socket(self) -> None:
        from execnet import _trio_gateway

        spec = execnet.XSpec("ssh=host//id=gw0")
        spec.profile = "thread"
        argv = _trio_gateway._ssh_argv(
            spec, "worker-cmd", forward=("/tmp/remote.sock", "/tmp/local.sock")
        )
        assert "-R" in argv
        assert argv[argv.index("-R") + 1] == "/tmp/remote.sock:/tmp/local.sock"
        # a stale remote socket must not block the bind
        assert "StreamLocalBindUnlink=yes" in argv


class TestSocketWorkerSpawnFailure:
    """A server that cannot start a worker must not leave a coordinator waiting.

    The coordinator connects and blocks for the handshake reply.  Nothing
    else will ever move it, so a failed spawn has to close the connection --
    otherwise one unsupported gateway wedges the whole session, which is what
    ``socket//installvia=`` did on Windows.
    """

    def test_a_failed_spawn_closes_the_connection(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import trio

        from execnet import _trio_gateway
        from execnet import _trio_host

        def boom(sock: Any) -> Any:
            raise RuntimeError("no worker for you")

        monkeypatch.setattr(_trio_host, "_spawn_socket_worker", boom)

        async def main() -> None:
            ours, theirs = socket.socketpair()
            server = trio.SocketStream(trio.socket.from_stdlib_socket(theirs))
            client = trio.SocketStream(trio.socket.from_stdlib_socket(ours))
            async with client, server:
                with pytest.raises(RuntimeError, match="no worker"):
                    await _trio_host.serve_socket_connection(server, reap=False)
                # the coordinator's end: its handshake ends, and says the
                # worker went away rather than surfacing a transport error
                with trio.fail_after(5), pytest.raises(EOFError, match="went away"):
                    await _trio_gateway.configure_worker(client, None, "socket")

        trio.run(main)

    def test_a_host_that_cannot_hand_over_a_socket_refuses_up_front(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # refusing before the address is replied is what makes it diagnosable:
        # afterwards the coordinator is already connecting, and a closed
        # socket can only ever say "EOF".
        from execnet import _trio_host

        monkeypatch.setattr(_provision, "socket_handoff_available", lambda: False)
        sent: list[tuple[int, int, bytes]] = []

        class FakeGateway:
            def _send(self, code: int, channelid: int = 0, data: bytes = b"") -> None:
                sent.append((code, channelid, data))

        import trio

        from execnet._message import Message
        from execnet._serialize import loads_internal

        gateway: Any = FakeGateway()
        trio.run(_trio_host._start_socket_and_reply, gateway, 7, "localhost")

        assert len(sent) == 1
        code, channelid, data = sent[0]
        assert code == Message.CHANNEL_CLOSE_ERROR
        assert channelid == 7
        assert "cannot hand an accepted socket" in cast("str", loads_internal(data))


class TestSocketWorkerConfig:
    """The server hands the connection over; it does not read what is on it.

    The worker it spawns inherits the accepted socket, so the coordinator's
    config frame reaches that worker directly.  Nothing about the gateway
    is the server's to relay, filter, or put in an argv.
    """

    def test_the_server_passes_no_config_to_the_worker(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from execnet import _trio_host

        recorded: list[list[str]] = []

        class FakePopen:
            pid = 4321
            stdin = None

            def __init__(self, args: list[str], **kwargs: Any) -> None:
                recorded.append(args)

        monkeypatch.setattr(_provision, "socket_share_required", lambda: False)
        monkeypatch.setattr(subprocess, "Popen", FakePopen)

        class FakeSocket:
            def fileno(self) -> int:
                return 9

        _trio_host._spawn_socket_worker(FakeSocket())

        (argv,) = recorded
        assert argv[-2:] == ["--protocol-fd", "9"]
        assert not any(token.startswith("--config") for token in argv)

    def test_a_coordinators_config_reaches_the_spawned_worker(self) -> None:
        # end to end through a real server: chdir is a config key, and only
        # the worker can prove it arrived
        import trio

        from execnet import _trio_host

        async def main() -> dict[str, Any]:
            ours, theirs = socket.socketpair()
            server = trio.SocketStream(trio.socket.from_stdlib_socket(theirs))
            async with server:
                await _trio_host.serve_socket_connection(server, reap=False)
            ours.settimeout(TESTTIMEOUT)
            try:
                return handshake(ours, id="from-a-server")
            finally:
                ours.close()

        reply = trio.run(main)
        assert reply["ok"] is True
        assert reply["profile"] == "thread"


class TestShareTransport:
    """The Windows socket handoff. Only ``adopt`` is testable off Windows."""

    def test_adopt_decodes_the_blob_out_of_the_local_config(self) -> None:
        import base64

        from execnet import _trio_worker
        from execnet._trio_gateway import SHARE_KEY

        transport = _trio_worker.ShareTransport()
        # the local config holds the blob and nothing else: what the worker
        # *is* comes from the frame that arrives on the shared socket
        transport.adopt({SHARE_KEY: base64.b64encode(b"blobby").decode("ascii")})
        assert transport._blob == b"blobby"

    def test_adopt_without_a_blob_is_a_clear_error(self) -> None:
        from execnet import _trio_worker

        transport = _trio_worker.ShareTransport()
        with pytest.raises(SystemExit, match="protocol_share"):
            transport.adopt({})

    def test_the_cli_accepts_the_flag(self) -> None:
        ns = _cli._build_parser().parse_args(
            ["worker", "--protocol-share", "--config-fd", "0"]
        )
        assert ns.protocol_share is True

    def test_share_is_exclusive_with_the_other_transports(self) -> None:
        with pytest.raises(SystemExit):
            _cli._build_parser().parse_args(
                ["worker", "--protocol-share", "--protocol-stdio"]
            )


class TestShareHandoffWiring:
    """How the share blob gets from coordinator to worker.

    ``WSADuplicateSocket`` itself only exists on Windows, but everything
    around it -- the flag in argv, the blob on stdin -- is the part that can
    be wired up wrong, and that is testable anywhere.
    """

    def test_popen_spawn_puts_the_flag_in_argv_and_the_blob_on_stdin(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import base64

        import trio

        from execnet import _trio_gateway
        from execnet._trio_gateway import SHARE_KEY

        written: list[bytes] = []
        seen: dict[str, Any] = {}

        class FakeStdin:
            async def send_all(self, data: bytes) -> None:
                written.append(data)

            async def aclose(self) -> None:
                pass

        class FakeProcess:
            pid = 4321
            stdin = FakeStdin()

        async def fake_open_process(args: list[str], **kwargs: Any) -> Any:
            seen["args"] = args
            seen["kwargs"] = kwargs
            return FakeProcess()

        monkeypatch.setattr(_provision, "socket_share_required", lambda: True)
        monkeypatch.setattr(trio.lowlevel, "open_process", fake_open_process)
        monkeypatch.setattr(
            _trio_gateway,
            "share_socket",
            lambda sock, pid: base64.b64encode(b"dup-for-%d" % pid).decode("ascii"),
        )

        spec = execnet.XSpec("popen//id=gw0")
        ours, theirs = socket.socketpair()
        try:
            trio.run(_trio_gateway._spawn_with_socket, spec, theirs)
        finally:
            ours.close()
            theirs.close()

        args = seen["args"]
        assert "--protocol-share" in args
        # the blob is not built until we have a pid, which is only true once
        # the process exists -- so it cannot be in argv even if we wanted it
        assert "--config-fd" in args

        config = json.loads(b"".join(written))
        # the blob, and only the blob: everything else about this worker
        # goes over the socket the blob describes
        assert list(config) == [SHARE_KEY]
        assert base64.b64decode(config[SHARE_KEY]) == b"dup-for-4321"

    def test_server_side_spawn_shares_the_accepted_socket(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import base64

        from execnet import _trio_gateway
        from execnet import _trio_host
        from execnet._trio_gateway import SHARE_KEY

        written: list[bytes] = []
        seen: dict[str, Any] = {}

        class FakeStdin:
            def write(self, data: bytes) -> None:
                written.append(data)

            def close(self) -> None:
                pass

        class FakePopen:
            pid = 99
            stdin = FakeStdin()

        def fake_popen(args: list[str], **kwargs: Any) -> Any:
            seen["args"] = args
            seen["kwargs"] = kwargs
            return FakePopen()

        monkeypatch.setattr(_provision, "socket_share_required", lambda: True)
        monkeypatch.setattr(subprocess, "Popen", fake_popen)
        monkeypatch.setattr(
            _trio_gateway,
            "share_socket",
            lambda sock, pid: base64.b64encode(b"accepted-%d" % pid).decode("ascii"),
        )

        ours, theirs = socket.socketpair()
        try:
            _trio_host._spawn_socket_worker(theirs)
            # the socket we do not own must survive being viewed for share()
            assert theirs.fileno() >= 0
            theirs.send(b"still open")
            assert ours.recv(16) == b"still open"
        finally:
            ours.close()
            theirs.close()

        assert "--protocol-share" in seen["args"]
        assert "pass_fds" not in seen["kwargs"]
        config = json.loads(b"".join(written))
        assert list(config) == [SHARE_KEY]
        assert base64.b64decode(config[SHARE_KEY]) == b"accepted-99"


class TestViaConfigPrivacy:
    """A relaying coordinator never sees the config it is relaying.

    ``via=`` asks one gateway to *spawn* another, so the intermediary
    decides what to launch -- but not what it is.  The sub's config comes
    down the tunnel from the coordinator that wants the gateway, which is
    the only side that has any business holding its ``env:`` values.
    """

    def test_the_spawn_request_carries_no_config(self) -> None:
        spec = execnet.XSpec("popen//via=coord//id=gw1//env:TOKEN=s3cr3t//chdir=/srv")
        request = _provision.spawn_request(spec)
        assert "config" not in request
        assert "s3cr3t" not in json.dumps(request)
        # what provisioning genuinely cannot defer: which environment to build
        assert request["profile"] == "thread"

    def test_the_sub_is_spawned_without_one(self) -> None:
        argv, _delivery = _provision.sub_spawn_argv({"profile": "thread"})
        assert not any(token.startswith("--config") for token in argv)

    def test_env_reaches_a_sub_through_the_tunnel(self) -> None:
        # end to end: the value never touches the intermediary's argv, and
        # still arrives in the sub's environment
        group = execnet.Group()
        try:
            group.makegateway("popen//id=coord")
            sub = group.makegateway("popen//via=coord//env:TUNNELLED=yes")
            channel = sub.remote_exec(
                "import os; channel.send(os.environ['TUNNELLED'])"
            )
            assert channel.receive(TESTTIMEOUT) == "yes"
        finally:
            group.terminate(timeout=TESTTIMEOUT)
