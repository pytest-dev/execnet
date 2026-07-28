"""Trio socket server for execnet gateways.

Listens on a TCP port and hands each accepted connection (by fd) to a fresh
``python -m execnet._trio_worker`` subprocess that serves the gateway over it.
No code is executed inline.

This module implements the ``execnet-socketserver`` console command, which is
the supported entry point -- run it on the target host, or install-free with
``uvx --from execnet execnet-socketserver``.
"""

from __future__ import annotations


async def _trio_serve(hostport: str, once: bool) -> None:
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
    """Console entry point (``execnet-socketserver``)."""
    import argparse

    import trio

    parser = argparse.ArgumentParser(
        prog="execnet-socketserver",
        description="Serve execnet gateway connections over a socket.",
    )
    parser.add_argument(
        "hostport",
        nargs="?",
        default=":8888",
        help="address to bind as HOST:PORT or :PORT (default: :8888)",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="serve a single connection and exit instead of looping",
    )
    args = parser.parse_args(argv)
    trio.run(_trio_serve, args.hostport, args.once)


if __name__ == "__main__":
    main()
