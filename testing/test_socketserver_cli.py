"""Test the ``execnet-socketserver`` console entry point end to end."""

from __future__ import annotations

import shutil
import socket
import subprocess
import time
from collections.abc import Iterator

import pytest

import execnet

SERVER = shutil.which("execnet-socketserver")

pytestmark = pytest.mark.skipif(
    SERVER is None, reason="execnet-socketserver console script not installed"
)


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture
def socketserver_port() -> Iterator[int]:
    assert SERVER is not None
    port = _free_port()
    proc = subprocess.Popen(
        [SERVER, f"127.0.0.1:{port}"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        # loop mode: a throwaway probe just consumes one accept iteration
        for _ in range(100):
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
                break
            except OSError:
                time.sleep(0.1)
        else:
            pytest.fail("execnet-socketserver did not start")
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
