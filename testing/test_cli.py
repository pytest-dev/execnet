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
from collections.abc import Iterator

import pytest

import execnet
from execnet import _cli
from execnet import _provision

posix_only = pytest.mark.skipif(
    sys.platform.startswith("win"), reason="needs POSIX fd passing / unix sockets"
)

TESTTIMEOUT = 30.0


def worker_config(**overrides: object) -> str:
    config = {
        "id": "cli-test-worker",
        "profile": "thread",
        "execmodel": "thread",
        "wait": "thread",
        "coordinator_version": execnet.__version__,
    }
    config.update(overrides)
    return json.dumps(config)


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


class TestArgumentGrammar:
    def test_protocol_flags_are_mutually_exclusive(self) -> None:
        with pytest.raises(SystemExit):
            _cli._build_parser().parse_args(
                ["worker", "--protocol-fd", "3", "--protocol-connect", "unix:/x"]
            )

    def test_config_sources_are_mutually_exclusive(self) -> None:
        with pytest.raises(SystemExit):
            _cli._build_parser().parse_args(
                ["worker", "--config", "{}", "--config-fd", "0"]
            )

    @pytest.mark.parametrize(
        ("value", "expected"), [("3", (3,)), ("4,5", (4, 5))]
    )
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
    def test_parse_address(self, address: str, expected: tuple) -> None:
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
                "--config",
                worker_config(),
            ],
            pass_fds=(theirs.fileno(),),
        )
        theirs.close()
        try:
            assert ours.recv(1) == b"1"  # the ready handshake
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
                    "--config",
                    worker_config(),
                ],
                pass_fds=(read_fd,),
                capture_output=True,
                text=True,
                timeout=TESTTIMEOUT,
            )
        finally:
            os.close(read_fd)
            os.close(write_fd)
        assert out.returncode != 0
        assert "not a socket" in out.stderr


class TestConfigSources:
    @posix_only
    def test_config_fd_keeps_it_out_of_argv(self) -> None:
        ours, theirs = socket.socketpair()
        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "execnet",
                "worker",
                "--protocol-fd",
                str(theirs.fileno()),
                "--config-fd",
                "0",
            ],
            pass_fds=(theirs.fileno(),),
            stdin=subprocess.PIPE,
        )
        theirs.close()
        try:
            assert proc.stdin is not None
            proc.stdin.write(worker_config().encode())
            proc.stdin.close()
            assert ours.recv(1) == b"1"
        finally:
            ours.close()
            proc.terminate()
            proc.wait(timeout=TESTTIMEOUT)

    def test_config_file(self, tmp_path) -> None:
        path = tmp_path / "config.json"
        path.write_text(worker_config())
        ns = _cli._build_parser().parse_args(
            ["worker", "--config-file", str(path)]
        )
        assert _cli._load_config(ns)["id"] == "cli-test-worker"

    def test_no_config_source_is_an_error(self) -> None:
        ns = _cli._build_parser().parse_args(["worker"])
        with pytest.raises(SystemExit, match="--config"):
            _cli._load_config(ns)


class TestTransportSelection:
    def test_defaults_per_platform(self) -> None:
        spec = execnet.XSpec("popen")
        expected = "stdio" if sys.platform.startswith("win") else "socket"
        assert _provision.resolve_transport(spec) == expected

    def test_explicit_wins(self) -> None:
        assert _provision.resolve_transport(execnet.XSpec("popen//transport=stdio")) == (
            "stdio"
        )

    def test_unknown_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="unknown transport"):
            _provision.resolve_transport(execnet.XSpec("popen//transport=carrier-pigeon"))

    @posix_only
    def test_socket_transport_keeps_the_protocol_off_stdio(self) -> None:
        group = execnet.Group()
        try:
            gateway = group.makegateway("popen")
            channel = gateway.remote_exec(
                "import sys; channel.send(sys.argv)",
            )
            argv = channel.receive(TESTTIMEOUT)
            assert "--protocol-fd" in argv
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

    @posix_only
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
                "--config",
                worker_config(),
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
                assert client.recv(1) == b"1"
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
        monkeypatch.setattr(_cli, "main", lambda argv: called.append(argv))
        with pytest.warns(DeprecationWarning, match="execnet server"):
            _cli.socketserver_main([":0", "--once"])
        assert called == [["server", ":0", "--once"]]


class TestRemoteCommand:
    """What ends up on the remote host's command line."""

    def test_config_is_not_in_the_remote_argv(self) -> None:
        # env: values are secrets often enough; the remote argv is readable
        # by every user on that host via ps
        spec = execnet.XSpec("ssh=host//id=gw0//env:TOKEN=s3cr3t")
        spec.profile = "thread"
        command = _provision.ssh_remote_command(
            spec, "--protocol-connect", "unix:/tmp/x.sock", config_on_stdin=True
        )
        assert "s3cr3t" not in command
        assert "--config-fd 0" in command

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
