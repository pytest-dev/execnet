"""The ``execnet`` command line.

Three subcommands, reachable both as the ``execnet`` console script and as
``python -m execnet`` (the latter is what provisioning emits for a direct
interpreter launch, where the script's location is not knowable)::

    execnet worker  ...   # serve one gateway over a chosen transport
    execnet server  ...   # accept gateway connections on a socket
    execnet info          # what this interpreter's execnet can do, as JSON

``execnet worker`` is the launch contract between a coordinator and the
process it starts.  Naming the protocol transport explicitly is what makes
ssh socket redirects and trampoline processes expressible: the protocol no
longer has to be the process's stdin/stdout, so a worker's stdio can belong
to the code it runs.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

__all__ = ["main"]

#: what the worker may do with each standard fd once the protocol has a
#: transport of its own.  ``stderr`` for stdout points fd 1 at fd 2, which
#: keeps remote output visible without needing a second stream.
STDIN_DISPOSITIONS = ("inherit", "close", "devnull")
STDOUT_DISPOSITIONS = ("inherit", "devnull", "stderr")
STDERR_DISPOSITIONS = ("inherit", "devnull")


def _protocol_fd(value: str) -> tuple[int, ...]:
    """``N`` (a bidirectional socket) or ``R,W`` (a pipe pair)."""
    try:
        fds = tuple(int(part) for part in value.split(","))
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"expected FD or READFD,WRITEFD, got {value!r}"
        ) from None
    if len(fds) not in (1, 2):
        raise argparse.ArgumentTypeError(
            f"expected FD or READFD,WRITEFD, got {value!r}"
        )
    return fds


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="execnet",
        description="Serve and inspect execnet gateways.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    worker = sub.add_parser(
        "worker",
        help="serve a single gateway to the coordinator that launched us",
        description=(
            "Serve one gateway over the given protocol transport.  Normally"
            " launched by a coordinator, not by hand."
        ),
    )
    transport = worker.add_mutually_exclusive_group()
    transport.add_argument(
        "--protocol-stdio",
        dest="protocol",
        action="store_const",
        const=("stdio", None),
        help="run the protocol over this process's stdin/stdout (default)",
    )
    transport.add_argument(
        "--protocol-fd",
        metavar="FD[,FD]",
        type=_protocol_fd,
        help="run the protocol over an inherited socket fd, or pipe read,write pair",
    )
    transport.add_argument(
        "--protocol-connect",
        metavar="ADDR",
        help="dial out to ADDR (unix:/path or host:port) and serve there",
    )
    transport.add_argument(
        "--protocol-listen",
        metavar="ADDR",
        help="listen on ADDR (unix:/path or host:port) for one connection",
    )

    config = worker.add_mutually_exclusive_group()
    config.add_argument("--config", metavar="JSON", help="worker config as JSON")
    config.add_argument(
        "--config-fd",
        metavar="FD",
        type=int,
        help="read the worker config as JSON from FD until EOF",
    )
    config.add_argument(
        "--config-file", metavar="PATH", help="read the worker config as JSON from PATH"
    )

    worker.add_argument(
        "--stdin", choices=STDIN_DISPOSITIONS, default=None, help="what to do with fd 0"
    )
    worker.add_argument(
        "--stdout",
        choices=STDOUT_DISPOSITIONS,
        default=None,
        help="what to do with fd 1",
    )
    worker.add_argument(
        "--stderr",
        choices=STDERR_DISPOSITIONS,
        default=None,
        help="what to do with fd 2",
    )
    worker.set_defaults(func=_run_worker)

    server = sub.add_parser(
        "server",
        help="accept gateway connections on a socket",
        description=(
            "Listen for coordinator connections and hand each one to a fresh"
            " worker subprocess.  No code is executed in this process."
        ),
    )
    server.add_argument(
        "hostport",
        nargs="?",
        default=":8888",
        help="address to bind as HOST:PORT or :PORT (default: :8888)",
    )
    server.add_argument(
        "--once",
        action="store_true",
        help="serve a single connection and exit instead of looping",
    )
    server.set_defaults(func=_run_server)

    info = sub.add_parser(
        "info",
        help="report this execnet's version and capabilities as JSON",
        description=(
            "Print a JSON object describing this interpreter's execnet."
            " Coordinators use it to decide whether a target interpreter can"
            " host a worker directly or has to be provisioned."
        ),
    )
    info.set_defaults(func=_run_info)

    return parser


def _load_config(ns: argparse.Namespace) -> dict[str, Any]:
    """The worker config, from whichever source was named.

    ``--config-fd`` is what a coordinator should use for anything sensitive:
    a config passed in argv is visible in ``ps`` to every user on the host,
    and it carries ``env:`` values.
    """
    if ns.config is not None:
        raw = ns.config
    elif ns.config_fd is not None:
        with os.fdopen(ns.config_fd, "r", encoding="utf-8") as stream:
            raw = stream.read()
    elif ns.config_file is not None:
        with open(ns.config_file, encoding="utf-8") as stream:
            raw = stream.read()
    else:
        raise SystemExit("execnet worker: one of --config/--config-fd/--config-file")
    config: dict[str, Any] = json.loads(raw)
    return config


def _run_worker(ns: argparse.Namespace) -> None:
    from . import _trio_worker

    if ns.protocol_fd is not None:
        transport: _trio_worker.Transport = _trio_worker.FdTransport(ns.protocol_fd)
    elif ns.protocol_connect is not None:
        transport = _trio_worker.ConnectTransport(ns.protocol_connect)
    elif ns.protocol_listen is not None:
        transport = _trio_worker.ListenTransport(ns.protocol_listen)
    else:
        transport = _trio_worker.StdioTransport()
    # The stdio transport owns fd 0/1, so it has to claim them before the
    # config is read (--config-fd 0 would be the very fd we are moving).
    transport.prepare()
    config = _load_config(ns)
    _trio_worker.serve_worker(
        config,
        transport,
        stdin=ns.stdin,
        stdout=ns.stdout,
        stderr=ns.stderr,
    )


def _run_server(ns: argparse.Namespace) -> None:
    import trio

    from . import _socketserver

    trio.run(_socketserver.serve, ns.hostport, ns.once)


def _run_info(ns: argparse.Namespace) -> None:
    json.dump(interpreter_info(), sys.stdout)
    sys.stdout.write("\n")


def interpreter_info() -> dict[str, Any]:
    """What a coordinator needs to know before launching a worker here.

    Distinct from ``_message.gateway_info``, which answers the in-protocol
    ``GATEWAY_INFO`` request on an established gateway; this one is what a
    coordinator can learn *before* connecting.
    """
    from ._version import version

    return {
        "execnet": version,
        "python": ".".join(str(part) for part in sys.version_info[:3]),
        "executable": sys.executable,
        "platform": sys.platform,
        "protocols": _supported_protocols(),
    }


def _supported_protocols() -> list[str]:
    """Transports this platform can actually serve."""
    protocols = ["stdio", "listen", "connect"]
    if hasattr(os, "dup"):
        protocols.append("fd")
    return sorted(protocols)


def main(argv: list[str] | None = None) -> None:
    """Console entry point (``execnet``) and ``python -m execnet``."""
    parser = _build_parser()
    ns = parser.parse_args(argv)
    ns.func(ns)


def socketserver_main(argv: list[str] | None = None) -> None:
    """Deprecated ``execnet-socketserver`` entry point; use ``execnet server``."""
    import warnings

    warnings.warn(
        "execnet-socketserver is deprecated; use `execnet server` instead.",
        DeprecationWarning,
        stacklevel=2,
    )
    main(["server", *(sys.argv[1:] if argv is None else argv)])
