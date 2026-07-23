"""Local ssh-connect tests backed by an in-process asyncssh server.

The coordinator shells out to the system ``ssh`` client, which connects to an
asyncssh server running in its own asyncio-loop thread; the server runs each
requested command as a subprocess with binary-safe stdio passthrough (the
execnet Message protocol needs raw bytes).
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
import threading
from collections.abc import Iterator
from pathlib import Path

import asyncssh
import pytest

import execnet

pytestmark = [
    pytest.mark.skipif(
        shutil.which("ssh") is None, reason="system ssh client required"
    ),
    # asyncssh leaves an un-awaited internal Queue.join coroutine on shutdown,
    # surfaced by pytest's unraisable-exception hook during GC; harmless here.
    pytest.mark.filterwarnings("ignore:coroutine 'Queue.join' was never awaited"),
]

# Committed, intentionally-insecure test keys (see sshkeys/README.md).
SSHKEYS = Path(__file__).parent / "sshkeys"
HOST_KEY = SSHKEYS / "insecure_host_ed25519"
CLIENT_KEY = SSHKEYS / "insecure_client_ed25519"
CLIENT_PUBKEY = SSHKEYS / "insecure_client_ed25519.pub"


class SSHServerThread:
    """asyncssh server on an ephemeral port, driven from its own asyncio thread."""

    def __init__(self, client_key_path: str) -> None:
        # OpenSSH refuses a world-readable private key; git does not preserve
        # 0600, so the caller hands us a temp copy already chmod'd 0600.
        self.client_key_path = client_key_path
        self._loop: asyncio.AbstractEventLoop | None = None
        self._server: asyncssh.SSHAcceptor | None = None
        self.port: int | None = None
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    async def _handle(self, process: asyncssh.SSHServerProcess) -> None:
        proc = await asyncio.create_subprocess_shell(
            process.command or "",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await process.redirect(stdin=proc.stdin, stdout=proc.stdout, stderr=proc.stderr)
        process.exit(await proc.wait())

    async def _serve(self) -> None:
        self._server = await asyncssh.listen(
            "127.0.0.1",
            0,
            server_host_keys=[str(HOST_KEY)],
            authorized_client_keys=str(CLIENT_PUBKEY),
            process_factory=self._handle,
            encoding=None,  # binary stdio
        )
        self.port = self._server.get_port()
        self._ready.set()
        await self._server.wait_closed()

    def _run(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._serve())
        finally:
            # Drain asyncssh's shutdown coroutines so GC does not surface an
            # "un-awaited coroutine" RuntimeWarning.
            pending = asyncio.all_tasks(self._loop)
            for task in pending:
                task.cancel()
            self._loop.run_until_complete(
                asyncio.gather(*pending, return_exceptions=True)
            )
            self._loop.run_until_complete(self._loop.shutdown_asyncgens())
            self._loop.close()

    def start(self) -> None:
        self._thread.start()
        assert self._ready.wait(timeout=10), "ssh server did not start"

    def stop(self) -> None:
        if self._loop is not None and self._server is not None:
            self._loop.call_soon_threadsafe(self._server.close)
        self._thread.join(timeout=5)

    def write_ssh_config(self, path: str) -> None:
        """Write an ssh config with a ``testhost`` alias pointing at this server."""
        with open(path, "w") as f:
            f.write(
                "Host testhost\n"
                "  HostName 127.0.0.1\n"
                f"  Port {self.port}\n"
                "  User testuser\n"
                f"  IdentityFile {self.client_key_path}\n"
                "  IdentitiesOnly yes\n"
                "  StrictHostKeyChecking no\n"
                "  UserKnownHostsFile /dev/null\n"
                "  LogLevel ERROR\n"
            )


@pytest.fixture
def ssh_server(tmp_path) -> Iterator[SSHServerThread]:
    # OpenSSH rejects the committed key's checkout permissions; use a 0600 copy.
    client_key = tmp_path / "client_ed25519"
    client_key.write_bytes(CLIENT_KEY.read_bytes())
    client_key.chmod(0o600)
    server = SSHServerThread(str(client_key))
    server.start()
    yield server
    server.stop()


@pytest.fixture
def ssh_config(ssh_server: SSHServerThread, tmp_path) -> str:
    path = str(tmp_path / "ssh_config")
    ssh_server.write_ssh_config(path)
    return path


def test_ssh_roundtrip(ssh_config: str) -> None:
    group = execnet.Group()
    try:
        gw = group.makegateway(
            f"ssh=testhost//ssh_config={ssh_config}//python={sys.executable}//id=ssh"
        )
        channel = gw.remote_exec("channel.send(channel.receive() + 1)")
        channel.send(41)
        assert channel.receive() == 42
    finally:
        group.terminate(timeout=5.0)
