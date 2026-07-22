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
        completed = subprocess.run(
            argv, capture_output=True, timeout=30, check=False
        )
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


def uv_run_argv(
    *, python: str | None, requirement: str, module_args: list[str]
) -> list[str]:
    """Build ``uv run [--python X] --with <req> python -u -m execnet._trio_worker …``.

    ``--no-project`` keeps the surrounding execnet checkout from being synced, so
    the ephemeral env holds only the requirement (+ trio).
    """
    argv = ["uv", "run", "--no-project"]
    if python:
        argv += ["--python", python]
    argv += [
        "--with",
        requirement,
        "python",
        "-u",
        "-m",
        "execnet._trio_worker",
        *module_args,
    ]
    return argv


def uv_worker_argv(spec: Any) -> list[str]:
    """Full ``uv run`` argv to launch the Trio worker for ``spec``."""
    import execnet

    module_args = [f"{spec.id}-worker", spec.execmodel, execnet.__version__]
    return uv_run_argv(
        python=spec.python,
        requirement=coordinator_requirement(),
        module_args=module_args,
    )
