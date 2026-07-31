"""Coordinator-side worker provisioning via ``uv``.

Non-same-interpreter Trio workers (foreign-python popen, later ssh/socket) are
launched inside an environment that ``uv`` provisions with a matching execnet +
trio.  The worker itself is always ``python -m execnet worker`` and does the
usual ``b"1"`` handshake on whichever protocol transport it was given; only
the launch prefix differs.

Delivery of execnet into that environment is version-aware:

* released coordinator (``X.Y.Z``) -> ``uv run --with execnet==X.Y.Z``
* dev coordinator (``X.Y.Z.devN+g...``) -> build a wheel from the editable
  install's source tree, cache it keyed by version, and ``uv run --with <wheel>``

``EXECNET_PROVISION_WHEEL`` overrides both: see :data:`PROVISION_WHEEL_ENV`.

trio is pulled transitively as an execnet dependency.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from functools import cache
from pathlib import Path
from typing import Any

_RELEASED_RE = re.compile(r"^\d+\.\d+\.\d+$")

#: ``transport=`` values: where a worker's protocol stream lives.
#:
#: ``socket`` keeps the protocol off the worker's stdio -- an inherited
#: socketpair for popen, an ``ssh -R``-forwarded unix socket for ssh -- so
#: the worker's stdin/stdout/stderr belong to the code it runs.  ``stdio``
#: is the classic shape, and the only one available where the machinery
#: ``socket`` needs is missing.
TRANSPORTS = ("socket", "stdio")


@cache
def socket_handoff_available() -> bool:
    """Whether we can hand a socket to a worker process we spawn ourselves.

    Two different mechanisms, one question.  POSIX passes the fd through
    ``pass_fds``.  Windows refuses that outright, but ``socket.share()``
    (``WSADuplicateSocket``) duplicates the socket into a named pid, and the
    resulting blob rides in the worker config -- see the ``share`` transport
    in ``_trio_worker``.

    The Windows answer is settled by *doing* it once, against our own pid,
    rather than by looking for the method.  An implementation that has the
    name but not a working call -- PyPy on Windows -- would otherwise pass
    the check and fail at the point where the only thing left to tell the
    coordinator is a closed socket.
    """
    import socket as _socket

    if not socket_share_required():
        return True
    try:
        left, right = _socket.socketpair()
        try:
            left.share(os.getpid())  # type: ignore[attr-defined]  # Windows
        finally:
            left.close()
            right.close()
    except Exception:
        return False
    return True


def socket_share_required() -> bool:
    """Whether handing a socket to a child needs ``share()`` and not ``pass_fds``.

    A function rather than a ``sys.platform`` test at each call site: those
    read as dead code to a type checker running with the other platform's
    assumptions, and this is the one question being asked anyway.
    """
    return sys.platform.startswith("win")


def ssh_dialback_available() -> bool:
    """Whether an ssh worker can dial back to us over a forwarded unix socket.

    Needs ``AF_UNIX`` here -- CPython has never exposed it on Windows -- and
    ``StreamLocal`` forwarding in the ssh client and server, which
    Win32-OpenSSH does not implement.  Neither is a coordinator-side choice,
    so ssh gateways there stay on the stdio transport.
    """
    import socket as _socket

    return hasattr(_socket, "AF_UNIX") and not sys.platform.startswith("win")


def default_spawn_transport() -> str:
    """What a worker we spawn gets when the spec does not say.

    Windows can *serve* the socket transport (see :func:`socket_share_required`),
    but stdio is what has years of exercise there while sharing is new, so it
    stays the default until the share path has CI behind it.
    ``transport=socket`` opts in.
    """
    if socket_share_required():
        return "stdio"
    return "socket" if socket_handoff_available() else "stdio"


def resolve_transport(
    spec: Any, *, available: bool = True, default: str | None = None
) -> str:
    """The transport for ``spec``: explicit if given, else the default.

    ``available`` is the caller's capability for *its* kind of gateway --
    :func:`socket_handoff_available` for a worker we spawn,
    :func:`ssh_dialback_available` for one that has to reach back to us.
    Asking for a transport that cannot work is an error at makegateway time,
    rather than a hang once nobody connects.  ``default`` decouples "can
    work" from "is what you get by default".
    """
    requested: str | None = getattr(spec, "transport", None)
    if requested is None:
        if default is not None:
            return default
        return "socket" if available else "stdio"
    if requested not in TRANSPORTS:
        raise ValueError(f"unknown transport {requested!r} (known: {list(TRANSPORTS)})")
    if requested == "socket" and not available:
        raise ValueError(
            "transport=socket is not available for this gateway on "
            f"{sys.platform}; use transport=stdio"
        )
    return requested


def uv_available() -> bool:
    """Whether the ``uv`` launcher is on PATH."""
    return shutil.which("uv") is not None


def shell_split_path(path: str) -> list[str]:
    """Split a ``python=`` value into argv tokens with shell lexing.

    Takes care to handle Windows' ``\\`` correctly.
    """
    if sys.platform.startswith("win"):
        # replace \\ by / otherwise shlex will strip them out
        path = path.replace("\\", "/")
    return shlex.split(path)


@cache
def target_info(python: str) -> dict[str, Any] | None:
    """``execnet info`` from interpreter ``python``, or None if it cannot run.

    One probe answers everything provisioning wants to know before it
    connects: whether execnet is importable at all, which version it is,
    whether trio is there, and which transports it can serve.  An execnet
    too old to have the CLI fails the probe and gets uv-provisioned, which
    is the right outcome.
    """
    argv = [*shell_split_path(python), "-m", "execnet", "info"]
    try:
        completed = subprocess.run(argv, capture_output=True, timeout=30, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    try:
        info: dict[str, Any] = json.loads(completed.stdout)
    except ValueError:
        return None
    return info


def target_has_execnet(python: str) -> bool:
    """Whether ``python`` can host a worker directly (execnet + trio present).

    When true the worker is launched on that interpreter as-is (preserving
    ``sys.executable``); otherwise it must be uv-provisioned.
    """
    info = target_info(python)
    return info is not None and info.get("trio") is not None


def _version_slug(version: str) -> str:
    """Filesystem-safe slug for a version string (may contain ``+``/``.``)."""
    return re.sub(r"[^0-9A-Za-z]+", "_", version)


def _wheel_cache_dir() -> Path:
    d = Path(tempfile.gettempdir()) / "execnet-bootstrap-wheels"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _editable_source_root() -> Path | None:
    """Source tree of an editable execnet install, via PEP 610 ``direct_url.json``.

    Returns ``None`` when execnet is not installed editable (nothing to build).
    """
    from importlib.metadata import PackageNotFoundError
    from importlib.metadata import distribution
    from urllib.parse import urlparse
    from urllib.request import url2pathname

    try:
        dist = distribution("execnet")
    except PackageNotFoundError:
        return None
    raw = dist.read_text("direct_url.json")
    if not raw:
        return None
    info = json.loads(raw)
    if not info.get("dir_info", {}).get("editable"):
        return None
    url = info.get("url", "")
    if not url.startswith("file:"):
        return None
    return Path(url2pathname(urlparse(url).path))


_BUILT_RE = re.compile(r"^Successfully built (?P<path>.+\.whl)\s*$", re.MULTILINE)


def _parse_built_wheel(uv_build_stderr: str) -> Path | None:
    """Extract the exact wheel path from ``uv build``'s ``Successfully built`` line.

    Building the filename ourselves is unsafe: the build-time version (dirty tree
    -> ``.dYYYYMMDD``/differing dev count) can differ from the import-time
    ``__version__``, so we trust the path uv reports.
    """
    matches = _BUILT_RE.findall(uv_build_stderr)
    if len(matches) != 1:
        return None
    return Path(matches[0].strip())


def _build_wheel(version: str) -> Path:
    """Build (and cache) a wheel of the editable execnet source for ``version``."""
    version_dir = _wheel_cache_dir() / _version_slug(version)
    if version_dir.exists():
        cached = sorted(version_dir.glob("*.whl"))
        if len(cached) == 1:
            return cached[0]

    root = _editable_source_root()
    if root is None:
        raise RuntimeError(
            f"cannot provision dev execnet {version!r}: no editable source tree "
            "found (install a released execnet or an editable checkout)"
        )
    version_dir.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        ["uv", "build", "--wheel", "-o", str(version_dir), str(root)],
        check=True,
        capture_output=True,
        text=True,
    )
    wheel = _parse_built_wheel(proc.stderr)
    if wheel is None or not wheel.exists():
        raise RuntimeError(
            f"could not determine wheel path from uv build for {version!r}:\n"
            f"{proc.stderr}"
        )
    return wheel


#: names a prebuilt wheel to provision remotes from, bypassing both the
#: index lookup and the build-from-source path.
#:
#: This exists for testing an *artifact*: CI installs execnet from a built
#: distribution, which leaves a dev version with no editable source tree to
#: build from -- so provisioning would be unavailable exactly where it most
#: wants exercising.  Pointing this at the wheel from the same build makes
#: the workers run the code under test rather than something rebuilt from a
#: checkout that may have moved on.
PROVISION_WHEEL_ENV = "EXECNET_PROVISION_WHEEL"


def _explicit_wheel() -> Path | None:
    """The wheel named by :data:`PROVISION_WHEEL_ENV`, if it is set.

    Set-but-wrong is a configuration error, not a reason to quietly fall
    back to building: the fallback would provision something *other* than
    what the caller asked to test.
    """
    raw = os.environ.get(PROVISION_WHEEL_ENV)
    if not raw:
        return None
    wheel = Path(raw)
    if not wheel.is_file():
        raise RuntimeError(f"{PROVISION_WHEEL_ENV}={raw!r} is not an existing file")
    if wheel.suffix != ".whl":
        raise RuntimeError(f"{PROVISION_WHEEL_ENV}={raw!r} is not a wheel")
    return wheel.resolve()


def provisioning_wheel() -> Path | None:
    """The wheel to provision remotes from, or None to resolve from an index.

    The one place the "released vs dev vs explicitly given" decision is
    made; every launcher (uv popen, ssh, via sub-spawn) routes through it so
    they cannot disagree about what a remote ends up running.
    """
    explicit = _explicit_wheel()
    if explicit is not None:
        return explicit

    import execnet

    version = execnet.__version__
    if _RELEASED_RE.match(version):
        return None
    return _build_wheel(version)


def provisioning_available() -> bool:
    """Whether this coordinator can produce material to provision a remote.

    A released version resolves from an index.  A dev version has to build
    a wheel, which needs the editable checkout it came from -- so a dev
    version installed *from a wheel* (what ``tox --installpkg`` produces,
    and what a CI artifact test runs against) can do neither, unless
    :data:`PROVISION_WHEEL_ENV` hands it one.  Callers that need a remote
    worker should check this rather than let :func:`coordinator_requirement`
    raise.
    """
    import execnet

    if _explicit_wheel() is not None:
        return True
    if _RELEASED_RE.match(execnet.__version__):
        return True
    return _editable_source_root() is not None


def coordinator_requirement() -> str:
    """A ``uv --with`` requirement that installs this coordinator's execnet."""
    import execnet

    wheel = provisioning_wheel()
    if wheel is not None:
        return str(wheel)
    return f"execnet=={execnet.__version__}"


def worker_config(spec: Any) -> dict[str, Any]:
    """The worker config for ``spec`` (the whole 'spec thing'), as a dict.

    Every launcher (popen, uv, ssh) sends this so the worker config lives in
    one place rather than scattered positional args.  A launcher may add to
    it before serializing -- the ``share`` transport puts its duplicated
    socket here, since that cannot exist until the worker has been spawned.
    """
    import execnet

    from ._execmodel import effective_profile

    # the spec keeps whatever the caller spelled; the worker gets what it
    # actually has a strategy for.  A spec that never went through a Group
    # has no profile at all -- the default applies, as it would there.
    profile = effective_profile(spec.profile or "thread")
    config: dict[str, Any] = {
        "id": f"{spec.id}-worker",
        "profile": profile,
        # pre-3.0 spelling, same value: a worker from an older execnet
        # reads this one.  Drop with the XSpec alias.
        "execmodel": profile,
        # derived, not configurable: the gevent profile parks its greenlets
        # on gevent wakeners, every other profile is thread-shaped.
        "wait": "gevent" if profile == "gevent" else "thread",
        "coordinator_version": execnet.__version__,
    }
    # Startup setup applied by the worker before serving (never through
    # remote_exec: an exec slot must not be claimed by bookkeeping).
    if spec.chdir:
        config["chdir"] = spec.chdir
    if spec.nice:
        config["nice"] = int(spec.nice)
    if spec.env:
        config["env"] = spec.env
    return config


def worker_cli_arg(spec: Any) -> str:
    """:func:`worker_config` serialized for ``--config``/``--config-fd``."""
    return json.dumps(worker_config(spec))


def stdio_tokens(spec: Any) -> list[str]:
    """``--stdin/--stdout/--stderr`` tokens for whatever ``spec`` asked for.

    A worker inherits the coordinator's stdio by default now that the
    protocol has a transport of its own, which also means remote code can
    *consume* the coordinator's stdin.  These keys are how a caller says
    otherwise, e.g. ``popen//stdin=devnull``.
    """
    tokens = []
    for name in ("stdin", "stdout", "stderr"):
        value = getattr(spec, name, None)
        if value:
            tokens += [f"--{name}", value]
    return tokens


def _worker_tokens(config: str | None, *protocol: str) -> list[str]:
    """``python -u -m execnet worker`` tokens for a launch.

    The literal ``python`` token is resolved by uv (inside the provisioned
    environment) or the remote shell.  ``config`` of None means the config
    arrives on stdin (``--config-fd 0``), which is what remote launches use:
    a config in argv is visible in ``ps`` to every user on that host, and it
    carries ``env:`` values.
    """
    tokens = ["python", "-u", "-m", "execnet", "worker", *protocol]
    if config is None:
        return [*tokens, "--config-fd", "0"]
    return [*tokens, "--config", config]


def worker_module_tokens(spec: Any, *protocol: str) -> list[str]:
    """``python -u -m execnet worker`` tokens for ``spec``."""
    return _worker_tokens(worker_cli_arg(spec), *protocol)


def _uv_tokens(python: str | None) -> list[str]:
    # --no-project keeps the surrounding execnet checkout from being synced.
    prefix = ["uv", "run", "--no-project"]
    if python:
        prefix += ["--python", python]
    return prefix


def _extra_with_tokens(config: str) -> list[str]:
    """Additional ``--with`` requirements the worker env needs.

    Derived from the worker config itself so every uv launcher (popen,
    ssh, via sub-spawn) provisions the same: the gevent profile / wait
    backend needs gevent importable in the worker.
    """
    parsed = json.loads(config)
    if parsed.get("profile") == "gevent" or parsed.get("wait") == "gevent":
        return ["--with", "gevent"]
    return []


def uv_worker_argv(
    spec: Any, *protocol: str, config_on_stdin: bool = False
) -> list[str]:
    """``uv run`` argv to launch the Trio worker locally (wheel path is local)."""
    config = worker_cli_arg(spec)
    return [
        *_uv_tokens(spec.python),
        "--with",
        coordinator_requirement(),
        *_extra_with_tokens(config),
        *_worker_tokens(None if config_on_stdin else config, *protocol),
    ]


#: where a shipped wheel lands on the remote, keyed by name (which carries
#: the version) so repeated gateways to one host reuse it.  A *shell
#: fragment*, not a path: ``$HOME`` is expanded remotely, and quoting it as
#: a literal would create a directory actually called ``~``.
REMOTE_WHEEL_DIR = '"$HOME"/.cache/execnet/wheels'


def remote_wheel_path(wheel: Path) -> str:
    """Shell fragment for the remote path a shipped wheel is delivered to."""
    return f"{REMOTE_WHEEL_DIR}/{shlex.quote(wheel.name)}"


def wheel_delivery_command(wheel: Path) -> str:
    """Remote sh command that receives ``wheel`` on stdin, unless already there.

    Out of band: run over its *own* ssh connection before the worker
    launch, so the protocol stream never has to carry a payload and the
    launch command needs neither ``head -c <N>`` byte accounting nor
    ``exec`` to keep an fd alive.

    Both branches consume stdin -- the coordinator streams the wheel
    unconditionally, and a remote that skipped the write without draining
    would hand it an EPIPE.
    """
    path = remote_wheel_path(wheel)
    return (
        f"mkdir -p {REMOTE_WHEEL_DIR} || exit 1; "
        f"if [ -s {path} ]; then cat > /dev/null; else cat > {path}.tmp"
        f" && mv {path}.tmp {path}; fi"
    )


def _remote_shell_command(
    python: str | None,
    config: str,
    *protocol: str,
    requirement: str | None = None,
    wheel: Path | None = None,
    config_on_stdin: bool = False,
) -> str:
    """Remote sh command launching the worker via uv.

    ``requirement`` installs from an index; ``wheel`` uses one already
    delivered by :func:`wheel_delivery_command`.
    """
    worker = _worker_tokens(None if config_on_stdin else config, *protocol)
    uv = [*_uv_tokens(python), *_extra_with_tokens(config)]
    if wheel is None:
        assert requirement is not None
        return shlex.join([*uv, "--with", requirement, *worker])
    # the wheel path is remote-side and may contain ~, so it is not quoted
    # by shlex.join -- splice it in after quoting the rest
    return " ".join(
        [shlex.join([*uv, "--with"]), remote_wheel_path(wheel), shlex.join(worker)]
    )


def ssh_remote_command(spec: Any, *protocol: str, config_on_stdin: bool = False) -> str:
    """Remote shell command launching the worker over ssh.

    Released coordinator -> ``uv run --with execnet==<ver> …``.  Dev
    coordinator -> ``uv run --with <delivered wheel> …``; delivering the
    wheel is a separate step (:func:`wheel_delivery_command`).
    """
    import execnet

    config = worker_cli_arg(spec)
    kwargs: dict[str, Any] = {"config_on_stdin": config_on_stdin}
    wheel = provisioning_wheel()
    if wheel is None:
        kwargs["requirement"] = f"execnet=={execnet.__version__}"
    else:
        kwargs["wheel"] = wheel
    return _remote_shell_command(spec.python, config, *protocol, **kwargs)


def ssh_wheel(spec: Any) -> Path | None:
    """The wheel this coordinator must deliver before launching, if any."""
    return provisioning_wheel()


def ssh_argv(
    ssh: str,
    ssh_config: str | None,
    remote_command: str,
    options: list[str] | None = None,
) -> list[str]:
    """``ssh`` client argv running ``remote_command`` on host ``ssh``."""
    args = ["ssh", "-C"]
    if ssh_config:
        args += ["-F", ssh_config]
    if options:
        args += options
    args += ssh.split()
    args.append(remote_command)
    return args


def vagrant_ssh_argv(
    machine: str,
    ssh_config: str | None,
    remote_command: str,
    options: list[str] | None = None,
) -> list[str]:
    """``vagrant ssh`` argv running ``remote_command`` on the named VM.

    Everything after ``--`` is passed through to the underlying ssh client,
    mirroring ``ssh_argv``.
    """
    args = ["vagrant", "ssh", machine, "--", "-C"]
    if ssh_config:
        args += ["-F", ssh_config]
    if options:
        args += options
    args.append(remote_command)
    return args


def spawn_request(spec: Any) -> dict[str, Any]:
    """Payload for ``GATEWAY_START_SUB``: ask a via master to spawn a sub-worker.

    Carries the sub-spec essentials plus provisioning material when the sub
    may need it (ssh or foreign python): a released coordinator sends a pip
    requirement; a dev coordinator ships its wheel bytes for the master to
    materialize into its local wheel cache.

    TODO: the wheel is shipped eagerly because only the master can tell
    whether the target interpreter already has execnet; a wheel-on-demand
    round-trip would avoid the transfer in the common provisioned case.
    """
    import execnet

    request: dict[str, Any] = {
        "config": worker_cli_arg(spec),
        "python": spec.python or None,
        "ssh": spec.ssh or None,
        "vagrant_ssh": spec.vagrant_ssh or None,
        "ssh_config": spec.ssh_config or None,
    }
    if spec.ssh or spec.vagrant_ssh or spec.python:
        wheel = provisioning_wheel()
        if wheel is None:
            request["requirement"] = f"execnet=={execnet.__version__}"
        else:
            request["wheel"] = (wheel.name, wheel.read_bytes())
    return request


def materialize_wheel(name: str, data: bytes) -> Path:
    """Write shipped wheel bytes into the local wheel cache (idempotent)."""
    target = _wheel_cache_dir() / name
    if not target.exists():
        tmp = target.with_name(f"{target.name}.{os.getpid()}.tmp")
        tmp.write_bytes(data)
        tmp.replace(target)
    return target


def _requested_requirement(request: dict[str, Any]) -> tuple[str | None, Path | None]:
    """(uv requirement, local wheel path) from a spawn request's material."""
    requirement = request.get("requirement")
    if isinstance(requirement, str):
        return requirement, None
    shipped = request.get("wheel")
    if shipped is not None:
        name, data = shipped
        path = materialize_wheel(name, data)
        return str(path), path
    return None, None


#: an out-of-band step to run before a sub-worker launch: ``(argv, stdin)``
DeliveryStep = tuple[list[str], bytes]


def sub_spawn_argv(
    request: dict[str, Any],
) -> tuple[list[str], DeliveryStep | None]:
    """(argv, wheel delivery) spawning a requested sub-worker on this host.

    Handles a ``GATEWAY_START_SUB`` request on a via master: plain popen runs
    this interpreter's worker module, a foreign ``python`` runs directly when
    it already has execnet and is uv-provisioned otherwise, and ``ssh`` wraps
    the remote uv command.  A shipped wheel is delivered by the returned
    step -- its own ssh connection, run before the launch -- rather than
    framed into the launch command's stdin.

    The sub's protocol is relayed over its stdio by the master, so it always
    gets the stdio transport.
    """
    config = request["config"]
    assert isinstance(config, str)
    python = request.get("python")
    ssh = request.get("ssh")
    vagrant = request.get("vagrant_ssh")
    if ssh or vagrant:
        requirement, wheel = _requested_requirement(request)
        if requirement is None:
            raise RuntimeError("remote spawn request without provisioning material")
        ssh_config = request.get("ssh_config")

        def wrap(command: str, options: list[str] | None = None) -> list[str]:
            if ssh:
                assert isinstance(ssh, str)
                return ssh_argv(ssh, ssh_config, command, options)
            assert isinstance(vagrant, str)
            return vagrant_ssh_argv(vagrant, ssh_config, command, options)

        delivery: DeliveryStep | None = None
        if wheel is not None:
            delivery = (wrap(wheel_delivery_command(wheel)), wheel.read_bytes())
            command = _remote_shell_command(python, config, wheel=wheel)
        else:
            command = _remote_shell_command(python, config, requirement=requirement)
        return wrap(command), delivery
    if python:
        assert isinstance(python, str)
        if target_has_execnet(python):
            argv = [*shell_split_path(python), "-u", "-m", "execnet", "worker"]
            return [*argv, "--config", config], None
        requirement, _ = _requested_requirement(request)
        if requirement is None or not uv_available():
            raise RuntimeError(
                f"cannot provision sub-worker for python={python!r}: "
                "uv and provisioning material required"
            )
        return [
            *_uv_tokens(python),
            "--with",
            requirement,
            *_worker_tokens(config),
        ], None
    return [
        sys.executable,
        "-u",
        "-m",
        "execnet",
        "worker",
        "--config",
        config,
    ], None
