"""Coordinator-side worker provisioning via ``uv``.

Non-same-interpreter Trio workers (foreign-python popen, later ssh/socket) are
launched inside an environment that ``uv`` provisions with a matching execnet +
trio.  The worker itself is always ``python -m execnet._trio_worker`` and does
the usual ``b"1"`` stdio handshake; only the launch prefix differs.

Delivery of execnet into that environment is version-aware:

* released coordinator (``X.Y.Z``) -> ``uv run --with execnet==X.Y.Z``
* dev coordinator (``X.Y.Z.devN+g...``) -> build a wheel from the editable
  install's source tree, cache it keyed by version, and ``uv run --with <wheel>``

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
def target_has_execnet(python: str) -> bool:
    """Whether interpreter ``python`` can already import execnet + trio.

    When true the worker can be launched directly on that interpreter
    (preserving ``sys.executable``); otherwise it must be uv-provisioned.
    """
    argv = [*shell_split_path(python), "-c", "import execnet, trio"]
    try:
        completed = subprocess.run(argv, capture_output=True, timeout=30, check=False)
        return completed.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


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


def coordinator_requirement() -> str:
    """A ``uv --with`` requirement that installs this coordinator's execnet."""
    import execnet

    version = execnet.__version__
    if _RELEASED_RE.match(version):
        return f"execnet=={version}"
    return str(_build_wheel(version))


def worker_cli_arg(spec: Any) -> str:
    """Single JSON CLI argument carrying the worker config (the whole 'spec thing').

    Passed to ``python -m execnet._trio_worker`` by every launcher (popen, uv,
    ssh) so the worker config lives in one place rather than scattered positional
    args.
    """
    import execnet

    return json.dumps(
        {
            "id": f"{spec.id}-worker",
            "execmodel": spec.execmodel,
            "wait": spec.wait or "thread",
            "coordinator_version": execnet.__version__,
        }
    )


def _worker_tokens(config: str) -> list[str]:
    """``python -u -m execnet._trio_worker <config>`` tokens.

    The literal ``python`` token is resolved by uv (inside the provisioned
    environment) or the remote shell.
    """
    return ["python", "-u", "-m", "execnet._trio_worker", config]


def worker_module_tokens(spec: Any) -> list[str]:
    """``python -u -m execnet._trio_worker <config>`` tokens for ``spec``."""
    return _worker_tokens(worker_cli_arg(spec))


def _uv_tokens(python: str | None) -> list[str]:
    # --no-project keeps the surrounding execnet checkout from being synced.
    prefix = ["uv", "run", "--no-project"]
    if python:
        prefix += ["--python", python]
    return prefix


def uv_worker_argv(spec: Any) -> list[str]:
    """``uv run`` argv to launch the Trio worker locally (wheel path is local)."""
    return [
        *_uv_tokens(spec.python),
        "--with",
        coordinator_requirement(),
        *worker_module_tokens(spec),
    ]


def _remote_shell_command(
    python: str | None,
    config: str,
    *,
    requirement: str | None = None,
    wheel: Path | None = None,
) -> tuple[str, bytes]:
    """Remote sh command + stdin preamble launching the worker via uv.

    With ``requirement`` the remote installs from an index and no preamble is
    needed.  With ``wheel`` a POSIX-sh prelude receives the wheel bytes from
    stdin (``head -c N``) into a temp dir and ``exec``s uv against it; the
    wheel bytes are returned as the preamble to stream before the protocol.
    """
    worker = _worker_tokens(config)
    uv = _uv_tokens(python)
    if wheel is None:
        assert requirement is not None
        return shlex.join([*uv, "--with", requirement, *worker]), b""

    data = wheel.read_bytes()
    # "$d/"<name>: expand the temp dir, concatenate the (quoted) wheel filename.
    remote_wheel = '"$d/"' + shlex.quote(wheel.name)
    uv_run = " ".join(shlex.quote(token) for token in [*uv, "--with"])
    worker_cmd = " ".join(shlex.quote(token) for token in worker)
    prelude = (
        f"d=$(mktemp -d) && "
        f"head -c {len(data)} > {remote_wheel} && "
        f"exec {uv_run} {remote_wheel} {worker_cmd}"
    )
    return prelude, data


def ssh_remote_command(spec: Any) -> tuple[str, bytes]:
    """Remote shell command + stdin preamble to launch the worker over ssh.

    Released coordinator -> ``uv run --with execnet==<ver> …`` with no preamble.
    Dev coordinator -> wheel-shipping prelude (see ``_remote_shell_command``).
    """
    import execnet

    version = execnet.__version__
    config = worker_cli_arg(spec)
    if _RELEASED_RE.match(version):
        return _remote_shell_command(
            spec.python, config, requirement=f"execnet=={version}"
        )
    return _remote_shell_command(spec.python, config, wheel=_build_wheel(version))


def ssh_argv(ssh: str, ssh_config: str | None, remote_command: str) -> list[str]:
    """``ssh`` client argv running ``remote_command`` on host ``ssh``."""
    args = ["ssh", "-C"]
    if ssh_config:
        args += ["-F", ssh_config]
    args += ssh.split()
    args.append(remote_command)
    return args


def vagrant_ssh_argv(
    machine: str, ssh_config: str | None, remote_command: str
) -> list[str]:
    """``vagrant ssh`` argv running ``remote_command`` on the named VM.

    Everything after ``--`` is passed through to the underlying ssh client,
    mirroring ``ssh_argv``.
    """
    args = ["vagrant", "ssh", machine, "--", "-C"]
    if ssh_config:
        args += ["-F", ssh_config]
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
        version = execnet.__version__
        if _RELEASED_RE.match(version):
            request["requirement"] = f"execnet=={version}"
        else:
            wheel = _build_wheel(version)
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


def sub_spawn_argv(request: dict[str, Any]) -> tuple[list[str], bytes]:
    """(argv, stdin preamble) spawning a requested sub-worker on this host.

    Handles a ``GATEWAY_START_SUB`` request on a via master: plain popen runs
    this interpreter's worker module, a foreign ``python`` runs directly when
    it already has execnet and is uv-provisioned otherwise, and ``ssh`` wraps
    the remote uv command (streaming a shipped wheel as the preamble for dev
    versions).
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
        command, preamble = _remote_shell_command(
            python, config, requirement=requirement, wheel=wheel
        )
        ssh_config = request.get("ssh_config")
        if ssh:
            assert isinstance(ssh, str)
            return ssh_argv(ssh, ssh_config, command), preamble
        assert isinstance(vagrant, str)
        return vagrant_ssh_argv(vagrant, ssh_config, command), preamble
    if python:
        assert isinstance(python, str)
        if target_has_execnet(python):
            argv = [*shell_split_path(python), "-u", "-m", "execnet._trio_worker"]
            return [*argv, config], b""
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
        ], b""
    return [sys.executable, "-u", "-m", "execnet._trio_worker", config], b""
