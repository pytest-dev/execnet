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
import re
import shlex
import shutil
import subprocess
import tempfile
from functools import cache
from pathlib import Path
from typing import Any

_RELEASED_RE = re.compile(r"^\d+\.\d+\.\d+$")


def uv_available() -> bool:
    """Whether the ``uv`` launcher is on PATH."""
    return shutil.which("uv") is not None


@cache
def target_has_execnet(python: str) -> bool:
    """Whether interpreter ``python`` can already import execnet + trio.

    When true the worker can be launched directly on that interpreter
    (preserving ``sys.executable``); otherwise it must be uv-provisioned.
    """
    from .gateway_io import shell_split_path

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
            "coordinator_version": execnet.__version__,
        }
    )


def worker_module_tokens(spec: Any) -> list[str]:
    """``python -u -m execnet._trio_worker <config>`` tokens."""
    return ["python", "-u", "-m", "execnet._trio_worker", worker_cli_arg(spec)]


def _uv_prefix(spec: Any) -> list[str]:
    # --no-project keeps the surrounding execnet checkout from being synced.
    prefix = ["uv", "run", "--no-project"]
    if spec.python:
        prefix += ["--python", spec.python]
    return prefix


def uv_worker_argv(spec: Any) -> list[str]:
    """``uv run`` argv to launch the Trio worker locally (wheel path is local)."""
    return [
        *_uv_prefix(spec),
        "--with",
        coordinator_requirement(),
        *worker_module_tokens(spec),
    ]


def ssh_remote_command(spec: Any) -> tuple[str, bytes]:
    """Remote shell command + stdin preamble to launch the worker over ssh.

    Released coordinator -> ``uv run --with execnet==<ver> …`` with no preamble.
    Dev coordinator -> a POSIX-sh prelude that receives the wheel bytes from
    stdin (``head -c N``) into a temp dir and ``exec``s uv against it; the wheel
    bytes are returned as the preamble to stream before the Message protocol.
    """
    import execnet

    version = execnet.__version__
    worker = worker_module_tokens(spec)
    if _RELEASED_RE.match(version):
        command = shlex.join(
            [*_uv_prefix(spec), "--with", f"execnet=={version}", *worker]
        )
        return command, b""

    wheel = _build_wheel(version)
    data = wheel.read_bytes()
    # "$d/"<name>: expand the temp dir, concatenate the (quoted) wheel filename.
    remote_wheel = '"$d/"' + shlex.quote(wheel.name)
    uv_run = " ".join(shlex.quote(token) for token in [*_uv_prefix(spec), "--with"])
    worker_cmd = " ".join(shlex.quote(token) for token in worker)
    prelude = (
        f"d=$(mktemp -d) && "
        f"head -c {len(data)} > {remote_wheel} && "
        f"exec {uv_run} {remote_wheel} {worker_cmd}"
    )
    return prelude, data
