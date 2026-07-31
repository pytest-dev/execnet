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


def test_ephemeral_port_is_the_same_for_every_address_family() -> None:
    """One reported port has to be *the* port.

    A wildcard bind with port 0 gives each address family its own random
    port, and only the first is reported -- so a client dialling the other
    family finds nothing there.  Which family comes first is
    platform-dependent (IPv4 on Linux, IPv6 on Windows), so this passed by
    luck here while failing there.
    """
    import trio

    from execnet import _socketserver

    async def main() -> set[int]:
        listeners = await trio.open_tcp_listeners(0, host=None)
        listeners = await _socketserver._one_port(listeners, None)
        try:
            return {l.socket.getsockname()[1] for l in listeners}
        finally:
            for l in listeners:
                await l.aclose()

    assert len(trio.run(main)) == 1
