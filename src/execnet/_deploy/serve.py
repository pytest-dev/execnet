"""Worker side of the transfer and deploy services.

Reached only through :mod:`execnet._services`, which imports this module
the first time a request for one of its names arrives -- a coordinator
never imports it, and a worker that is never asked to receive anything
never pays for it either.

Both handlers keep their bodies synchronous and run them in a worker
thread, reaching the channel through ``trio.from_thread``.  Receiving a
tree is ``lstat``/``mkdir``/``chmod``/``utime``/``symlink`` and whole-file
writes; building an environment is waiting on ``uv``.  Threading a loop
through either would rewrite fiddly, well-tested logic into something
harder to read, for a thread each.  The cost is real and named: that
thread comes from the same budget exec placement rations, so a worker
receiving several trees at once has fewer left to run work on.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
from hashlib import md5
from pathlib import Path
from typing import Any

import trio

from .._errors import geterrortext
from .._trace import trace
from ._manifest import Entry
from ._manifest import Manifest
from ._manifest import Wanted


class _ThreadChannel:
    """A channel's sync view, from a thread ``to_thread`` started.

    ``trio.from_thread.run`` needs no token: the thread knows which run it
    belongs to because that run started it.
    """

    def __init__(self, channel: Any) -> None:
        self._channel = channel

    def send(self, item: object) -> None:
        trio.from_thread.run(self._channel.send, item)

    def receive(self) -> Any:
        return trio.from_thread.run(self._channel.receive)


async def _serve(handler: Any, gateway: Any, channelid: int, request: Any) -> None:
    """Run one service body in a thread, reporting on its channel.

    Contains its own failures: this is a task on the worker's *root*
    nursery, so an exception leaving it would end ``trio.run`` and take
    every gateway in the process with it.  The coordinator is waiting on
    this channel, so that is where the reason goes.
    """
    channel = gateway.open_channel(channelid)
    try:
        await trio.to_thread.run_sync(
            handler, _ThreadChannel(channel), request, abandon_on_cancel=True
        )
    except trio.Cancelled:
        raise
    except BaseException as exc:
        trace(f"service on channel {channelid} failed: {exc!r}")
        with trio.CancelScope(shield=True):
            try:
                await channel.aclose(geterrortext(exc))
            except Exception:  # the connection went away first
                pass
        return
    await channel.aclose()


# -- the transfer service --


def _existing(path: Path, entry: Entry) -> bytes | None:
    """Whether ``entry`` still has to be sent, and what we have if unsure.

    Same size and same mtime: assume identical, which is what makes a
    re-transfer of an unchanged tree nearly free.  Same size, different
    mtime: hand back a digest and let the sender decide -- that is the case
    where a rebuild produced the same bytes.
    """
    try:
        st = os.lstat(path)
    except OSError:
        return b""  # missing: send it
    if not stat.S_ISREG(st.st_mode):
        _remove(path)
        return b""
    if st.st_size != entry.size:
        return b""
    if st.st_mtime == entry.mtime:
        return None  # identical as far as anyone can tell
    with open(path, "rb") as stream:
        return md5(stream.read()).digest()


def _remove(path: Path) -> None:
    try:
        os.unlink(path)
    except OSError:
        shutil.rmtree(path, ignore_errors=True)


def receive_tree(channel: Any, request: dict[str, Any]) -> None:
    """Receive one tree into ``request["destination"]`` (blocking)."""
    destination = Path(os.path.expanduser(str(request["destination"])))
    delete = bool(request.get("delete"))

    manifest = Manifest.load(channel.receive())
    destination.mkdir(parents=True, exist_ok=True)

    # directories first, so files and links have somewhere to land
    wanted: list[str] = []
    checksums: dict[str, bytes] = {}
    for entry in manifest.entries:
        target = destination.joinpath(*entry.path.split("/"))
        if entry.kind == "dir":
            if target.exists() and not target.is_dir():
                _remove(target)
            # writable whatever the mode says: a read-only directory we
            # then have to put files into is a permission error later
            target.mkdir(parents=True, exist_ok=True)
            os.chmod(target, entry.mode | 0o700)
        elif entry.kind == "file":
            digest = _existing(target, entry)
            if digest is not None:
                wanted.append(entry.path)
                if digest:
                    checksums[entry.path] = digest
    channel.send(Wanted(tuple(wanted), checksums).dump())

    # bodies, in the order we asked for them
    pending = dict.fromkeys(wanted, True)
    while True:
        header = channel.receive()
        if header is None:
            break
        path, length = header
        target = destination.joinpath(*path.split("/"))
        pending.pop(path, None)
        if length is None:
            continue  # unchanged after all, or gone before it could be read
        with open(target, "wb") as stream:
            received = 0
            while received < length:
                chunk = channel.receive()
                stream.write(chunk)
                received += len(chunk)

    # modes and times, once every body is in place
    for entry in manifest.entries:
        if entry.kind != "file":
            continue
        target = destination.joinpath(*entry.path.split("/"))
        try:
            os.chmod(target, entry.mode)
            os.utime(target, (entry.mtime, entry.mtime))
        except OSError:
            pass  # never arrived, or is not ours to touch

    for entry in manifest.entries:
        if entry.kind != "link":
            continue
        target = destination.joinpath(*entry.path.split("/"))
        _remove(target)
        source = (
            str(destination.joinpath(*entry.target.split("/")))
            if entry.internal
            else entry.target
        )
        os.symlink(source, target)

    if delete:
        _delete_unlisted(destination, manifest)
    channel.send("done")


def _delete_unlisted(destination: Path, manifest: Manifest) -> None:
    """Remove anything under ``destination`` the manifest does not list."""
    keep = {entry.path for entry in manifest.entries}
    for root, dirnames, filenames in os.walk(destination, topdown=True):
        relative = os.path.relpath(root, destination)
        prefix = "" if relative == os.curdir else relative.replace(os.sep, "/") + "/"
        for name in list(dirnames):
            if prefix + name not in keep:
                _remove(Path(root) / name)
                dirnames.remove(name)
        for name in filenames:
            if prefix + name not in keep:
                _remove(Path(root) / name)


async def receive_transfer(gateway: Any, channelid: int, request: Any) -> None:
    """``transfer`` service entry point."""
    await _serve(receive_tree, gateway, channelid, request)


# -- the deploy service --

#: environment variables that would point uv at an environment other than
#: the workspace's.  A worker inherits its coordinator's environment, and a
#: coordinator is very often itself running inside a virtualenv -- under
#: which ``uv pip install`` installs into *that* one, silently, leaving the
#: deployed environment without the project and the coordinator's own with
#: a package it never asked for.
_ENV_OVERRIDES = ("VIRTUAL_ENV", "UV_PROJECT_ENVIRONMENT", "CONDA_PREFIX")

#: how long any one uv invocation may take before it is a failure rather
#: than a slow network.  Generous: a cold cache on a fresh host pays for
#: every wheel in the lockfile.
UV_TIMEOUT = 900.0


def _venv_python(workspace: Path) -> Path:
    if sys.platform.startswith("win"):
        return workspace / ".venv" / "Scripts" / "python.exe"
    return workspace / ".venv" / "bin" / "python"


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
    """Expand and create the workspace, and report where it is.

    Expansion happens here because ``~`` means the home directory of
    whoever runs the worker, which the coordinator cannot know.
    """
    explicit = request.get("workspace")
    if explicit:
        workspace = Path(os.path.expanduser(str(explicit)))
    else:
        root = os.path.expanduser(str(request["root"]))
        workspace = Path(root) / str(request["name"])
    workspace.mkdir(parents=True, exist_ok=True)
    return {"workspace": str(workspace)}


def _install(request: dict[str, Any]) -> dict[str, Any]:
    """Build the frozen environment and install the wheels into it."""
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


def deploy_step(channel: Any, request: dict[str, Any]) -> None:
    """Run one deployment step and answer with its result (blocking)."""
    step = str(request.get("step"))
    try:
        handler = STEPS[step]
    except KeyError:
        raise ValueError(
            f"unknown deployment step {step!r} (known: {sorted(STEPS)})"
        ) from None
    channel.send(handler(request))


async def run_deploy_step(gateway: Any, channelid: int, request: Any) -> None:
    """``deploy`` service entry point."""
    await _serve(deploy_step, gateway, channelid, request)
