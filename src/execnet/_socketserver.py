"""Trio socket server for execnet gateways.

Listens on a TCP port and hands each accepted connection (by fd) to a fresh
worker subprocess that serves the gateway over it.  No code is executed
inline.

The supported entry point is ``execnet server`` (see :mod:`execnet._cli`) --
run it on the target host, or install-free with
``uvx --from execnet execnet server``.  The old ``execnet-socketserver``
console command still works and forwards here with a DeprecationWarning.
"""

from __future__ import annotations


async def serve(hostport: str, once: bool) -> None:
    """Bind ``hostport`` and hand accepted connections to worker processes."""
    import trio

    from execnet import _trio_host

    host, _, port_str = hostport.rpartition(":")
    listeners = await trio.open_tcp_listeners(int(port_str), host=host or None)
    addr = listeners[0].socket.getsockname()
    # Report the bound address (port may be ephemeral) for callers to read.
    print("execnet-socketserver listening on %s %s" % (addr[0], addr[1]), flush=True)

    if once:
        stream = await listeners[0].accept()
        for listener in listeners:
            await listener.aclose()
        # The worker outlives this one-shot server.
        await _trio_host.serve_socket_connection(stream, reap=False)
        return

    async def handler(stream: trio.SocketStream) -> None:
        await _trio_host.serve_socket_connection(stream, reap=True)

    await trio.serve_listeners(handler, listeners)


def main(argv: list[str] | None = None) -> None:
    """Deprecated ``execnet-socketserver`` console entry point."""
    from ._cli import socketserver_main

    socketserver_main(argv)


if __name__ == "__main__":
    main()
