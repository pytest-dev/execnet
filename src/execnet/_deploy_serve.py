"""Worker side of ``GATEWAY_DEPLOY``: build the environment a deployment needs.

The coordinator has already put a ``pyproject.toml``, a ``uv.lock`` and a
``dist/`` of wheels in the workspace (by rsync, over this same gateway).
What is left is host-side work, and it is here rather than in a
``remote_exec`` for the same reason rsync is: execnet is installed on the
worker, so its own infrastructure travels as protocol requests rather than
as source.

Two steps, because the coordinator needs the workspace path before it can
rsync into it and the install can only run once that rsync is done:

``prepare``
    expand and create the workspace, and report where it is.  Expansion
    happens *here* -- ``~`` means the home directory of whoever runs the
    worker, which the coordinator has no way to know.

``install``
    ``uv sync --frozen`` the lockfile, then install the wheels into that
    environment, and report the interpreter.  Frozen on purpose: the
    remote resolves nothing, so it gets what the coordinator's lockfile
    pins rather than whatever the index holds today.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import trio

from ._errors import geterrortext
from ._trace import trace

#: how long any one uv invocation may take before it is a failure rather
#: than a slow network.  Generous: a cold cache on a fresh host pays for
#: every wheel in the lockfile.
UV_TIMEOUT = 900.0


def _venv_python(workspace: Path) -> Path:
    if sys.platform.startswith("win"):
        return workspace / ".venv" / "Scripts" / "python.exe"
    return workspace / ".venv" / "bin" / "python"


#: environment variables that would point uv at an environment other than
#: the workspace's.  A worker inherits its coordinator's environment, and a
#: coordinator is very often itself running inside a virtualenv -- under
#: which ``uv pip install`` installs into *that* one, silently, leaving the
#: deployed environment without the project and the coordinator's own with
#: a package it never asked for.
_ENV_OVERRIDES = ("VIRTUAL_ENV", "UV_PROJECT_ENVIRONMENT", "CONDA_PREFIX")


def _uv_env() -> dict[str, str]:
    env = dict(os.environ)
    for name in _ENV_OVERRIDES:
        env.pop(name, None)
    return env


def _run_uv(args: list[str], cwd: Path) -> None:
    try:
        proc = subprocess.run(
            ["uv", *args],
            cwd=cwd,
            env=_uv_env(),
            capture_output=True,
            text=True,
            timeout=UV_TIMEOUT,
            check=False,
        )
    except FileNotFoundError:
        raise RuntimeError(
            "a deployment needs uv on the target host, and it is not on PATH"
            f" for {sys.executable}"
        ) from None
    if proc.returncode != 0:
        raise RuntimeError(
            f"`uv {' '.join(args)}` failed in {cwd} with {proc.returncode}:\n"
            f"{proc.stderr.strip()}"
        )


def _prepare(request: dict[str, Any]) -> dict[str, Any]:
    explicit = request.get("workspace")
    if explicit:
        workspace = Path(os.path.expanduser(str(explicit)))
    else:
        root = os.path.expanduser(str(request["root"]))
        workspace = Path(root) / str(request["name"])
    workspace.mkdir(parents=True, exist_ok=True)
    return {"workspace": str(workspace)}


def _install(request: dict[str, Any]) -> dict[str, Any]:
    workspace = Path(str(request["workspace"]))
    # --no-install-project: the project is installed from the wheel the
    # coordinator built, not from a source tree that is not even here.
    _run_uv(["sync", "--frozen", "--no-install-project"], workspace)
    python = _venv_python(workspace)
    if not python.exists():  # pragma: no cover - uv would have failed first
        raise RuntimeError(f"uv sync left no interpreter at {python}")
    wheels = [str(item) for item in request.get("wheels", [])]
    if wheels:
        # --python, not the ambient environment: see _ENV_OVERRIDES.  Naming
        # the interpreter we just built leaves nothing to infer.
        _run_uv(["pip", "install", "--python", str(python), *wheels], workspace)
    return {"workspace": str(workspace), "python": str(python)}


STEPS = {"prepare": _prepare, "install": _install}


def run_step(request: dict[str, Any]) -> dict[str, Any]:
    """Run one deployment step (blocking; called in a worker thread)."""
    step = str(request.get("step"))
    try:
        handler = STEPS[step]
    except KeyError:
        raise ValueError(
            f"unknown deployment step {step!r} (known: {sorted(STEPS)})"
        ) from None
    return handler(request)


async def serve_deploy_request(gateway: Any, channelid: int, data: bytes) -> None:
    """Serve one ``GATEWAY_DEPLOY`` request; a task on the worker's loop.

    Contains its own failures, like every other ``start_soon`` entry point:
    the coordinator is waiting on this channel, so a failure closes it with
    the reason rather than ending ``trio.run`` and taking the process's
    gateways with it.
    """
    from ._serialize import loads_internal

    channel = gateway.open_channel(channelid)
    try:
        request = loads_internal(data)
        assert isinstance(request, dict)
        # uv builds environments and downloads wheels; it belongs nowhere
        # near the loop that has to keep the connection answering
        reply = await trio.to_thread.run_sync(
            run_step, request, abandon_on_cancel=True
        )
        await channel.send(reply)
    except trio.Cancelled:
        raise
    except BaseException as exc:
        trace(f"deployment step on channel {channelid} failed: {exc!r}")
        with trio.CancelScope(shield=True):
            try:
                await channel.aclose(geterrortext(exc))
            except Exception:  # the connection went away first
                pass
        return
    await channel.aclose()
