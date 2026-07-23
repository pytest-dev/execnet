"""Test the ``execnet-socketserver`` console entry point end to end.

The Trio socketserver binds a port and spawns a ``python -m execnet._trio_worker``
subprocess per connection (no inline code execution); the coordinator connects
over a Trio TCP stream.
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Iterator

import pytest

import execnet

SERVER = shutil.which("execnet-socketserver")

pytestmark = pytest.mark.skipif(
    SERVER is None, reason="execnet-socketserver console script not installed"
)


@pytest.fixture
def socketserver_port() -> Iterator[int]:
    assert SERVER is not None
    proc = subprocess.Popen(
        [SERVER, ":0"],  # ephemeral port; it prints the one it bound
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        assert proc.stdout is not None
        port = None
        while True:
            line = proc.stdout.readline()
            if not line:
                pytest.fail("execnet-socketserver exited before binding")
            if "listening on" in line:
                port = int(line.split()[-1])
                break
        yield port
    finally:
        proc.kill()
        proc.wait(timeout=5)


def test_socketserver_cli_roundtrip(socketserver_port: int) -> None:
    group = execnet.Group()
    try:
        gw = group.makegateway(f"socket=127.0.0.1:{socketserver_port}//id=sock")
        channel = gw.remote_exec("channel.send(channel.receive() + 1)")
        channel.send(41)
        assert channel.receive() == 42
    finally:
        group.terminate(timeout=5.0)
