#! /usr/bin/env python
"""
start socket based minimal readline exec server

it can exeuted in 2 modes of operation

1. as normal script, that listens for new connections

2. via existing_gateway.remote_exec (as imported module)

"""

# this part of the program only executes on the server side
#
from __future__ import annotations

import os
import sys
from typing import TYPE_CHECKING

try:
    import fcntl
except ImportError:
    fcntl = None  # type: ignore[assignment]

if TYPE_CHECKING:
    from execnet.gateway_base import Channel
    from execnet.gateway_base import ExecModel

progname = "socket_readline_exec_server-1.2"


debug = 0

if debug:  # and not os.isatty(sys.stdin.fileno())
    f = open("/tmp/execnet-socket-pyout.log", "w")
    old = sys.stdout, sys.stderr
    sys.stdout = sys.stderr = f


def print_(*args) -> None:
    print(" ".join(str(arg) for arg in args))


exec(
    """def exec_(source, locs):
    exec(source, locs)"""
)


def exec_from_one_connection(serversock, execmodel: ExecModel) -> None:
    print_(progname, "Entering Accept loop", serversock.getsockname())
    clientsock, address = serversock.accept()
    print_(progname, "got new connection from {} {}".format(*address))
    clientfile = clientsock.makefile("rb")
    print_("reading line")
    # rstrip so that we can use \r\n for telnet testing
    source = clientfile.readline().rstrip()
    clientfile.close()
    g = {"clientsock": clientsock, "address": address, "execmodel": execmodel}
    source = eval(source)
    if source:
        co = compile(source + "\n", "<socket server>", "exec")
        print_(progname, "compiled source, executing")
        try:
            exec_(co, g)  # type: ignore[name-defined] # noqa: F821
        finally:
            print_(progname, "finished executing code")
            # background thread might hold a reference to this (!?)
            # clientsock.close()


def bind_and_listen(hostport: str | tuple[str, int], execmodel: ExecModel):
    socket = execmodel.socket
    if isinstance(hostport, str):
        host, port = hostport.split(":")
        hostport = (host, int(port))
    serversock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    # set close-on-exec
    if hasattr(fcntl, "FD_CLOEXEC"):
        old = fcntl.fcntl(serversock.fileno(), fcntl.F_GETFD)
        fcntl.fcntl(serversock.fileno(), fcntl.F_SETFD, old | fcntl.FD_CLOEXEC)
    # allow the address to be reused in a reasonable amount of time
    if os.name == "posix" and sys.platform != "cygwin":
        serversock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

    serversock.bind(hostport)
    serversock.listen(5)
    return serversock


def startserver(serversock, execmodel: ExecModel, loop: bool = False) -> None:
    execute_path = os.getcwd()
    try:
        while 1:
            try:
                exec_from_one_connection(serversock, execmodel)
            except (KeyboardInterrupt, SystemExit):
                raise
            except BaseException as exc:
                if debug:
                    import traceback

                    traceback.print_exc()
                else:
                    print_("got exception", exc)
            os.chdir(execute_path)
            if not loop:
                break
    finally:
        print_("leaving socketserver execloop")
        serversock.shutdown(2)


async def _trio_serve(hostport: str, once: bool) -> None:
    """Trio TCP server that spawns a worker subprocess per connection.

    No code is executed inline: each accepted socket is handed (by fd) to a
    fresh ``python -m execnet._trio_worker --socket-fd`` process which serves the
    gateway over it.
    """
    import itertools
    import json
    import subprocess

    import trio

    import execnet

    host, _, port_str = hostport.rpartition(":")
    listeners = await trio.open_tcp_listeners(int(port_str), host=host or None)
    addr = listeners[0].socket.getsockname()
    # Report the bound address (port may be ephemeral) for callers to read.
    print("execnet-socketserver listening on %s %s" % (addr[0], addr[1]), flush=True)

    counter = itertools.count()

    with trio.CancelScope() as scope:

        async def handler(stream: trio.SocketStream) -> None:
            fd = stream.socket.fileno()
            # Synthesise the worker config (socketserver workers default to the
            # thread model, matching the legacy socket server).
            config = json.dumps(
                {
                    "id": "socketworker%d" % next(counter),
                    "execmodel": "thread",
                    "coordinator_version": execnet.__version__,
                }
            )
            proc = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "execnet._trio_worker",
                    config,
                    "--socket-fd",
                    str(fd),
                ],
                pass_fds=[fd],
            )
            # The child forked with a copy of the fd; release ours.
            await stream.aclose()
            if once:
                scope.cancel()
                return
            # Reap the worker without blocking other connections.
            await trio.to_thread.run_sync(proc.wait)

        await trio.serve_listeners(handler, listeners)


def main(argv: list[str] | None = None) -> None:
    """Console entry point (``execnet-socketserver``).

    Serve execnet gateway connections over a socket, spawning a worker
    subprocess per connection.  Intended to be run directly, e.g. provisioned on
    a host with ``uvx --from execnet execnet-socketserver``.
    """
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

elif __name__ == "__channelexec__":
    chan: Channel = globals()["channel"]
    execmodel = chan.gateway.execmodel
    bindname = chan.receive()
    assert isinstance(bindname, (str, tuple))
    sock = bind_and_listen(bindname, execmodel)
    port = sock.getsockname()
    chan.send(port)
    startserver(sock, execmodel)
