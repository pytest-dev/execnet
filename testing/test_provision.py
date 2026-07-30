"""Unit tests for coordinator-side uv worker provisioning."""

from __future__ import annotations

import json
import re

import pytest

import execnet
from execnet import _provision

released = re.fullmatch(r"\d+\.\d+\.\d+", execnet.__version__) is not None


def test_worker_cli_arg_carries_config() -> None:
    spec = execnet.XSpec("popen//id=gw5//execmodel=thread")
    config = json.loads(_provision.worker_cli_arg(spec))
    assert config["id"] == "gw5-worker"
    assert config["execmodel"] == "thread"
    assert config["coordinator_version"] == execnet.__version__


def test_ssh_remote_command_released(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(execnet, "__version__", "9.9.9")
    spec = execnet.XSpec("ssh=host//id=gw0//execmodel=thread")
    command = _provision.ssh_remote_command(spec)
    assert "execnet==9.9.9" in command
    assert "head -c" not in command  # nothing is framed into the launch


@pytest.mark.skipif(released, reason="released execnet resolves from an index")
@pytest.mark.skipif(not _provision.uv_available(), reason="uv required to build wheel")
@pytest.mark.skipif(
    not _provision.provisioning_available(),
    reason="a dev execnet installed without its source tree cannot build a wheel",
)
def test_ssh_remote_command_dev_uses_a_delivered_wheel() -> None:
    # The wheel travels out of band now (its own connection, before the
    # launch), so the launch command just points uv at where it landed --
    # no byte accounting, no `exec` to keep an fd alive.
    spec = execnet.XSpec("ssh=host//id=gw0//execmodel=thread")
    command = _provision.ssh_remote_command(spec)
    wheel = _provision.ssh_wheel(spec)
    assert wheel is not None
    assert wheel.read_bytes()[:2] == b"PK"  # a wheel is a zip archive
    assert _provision.remote_wheel_path(wheel) in command
    assert "head -c" not in command
    assert "mktemp -d" not in command


@pytest.mark.skipif(released, reason="released execnet resolves from an index")
@pytest.mark.skipif(not _provision.uv_available(), reason="uv required to build wheel")
@pytest.mark.skipif(
    not _provision.provisioning_available(),
    reason="a dev execnet installed without its source tree cannot build a wheel",
)
def test_wheel_delivery_command_expands_home_and_drains_stdin() -> None:
    wheel = _provision.ssh_wheel(execnet.XSpec("ssh=host//id=gw0"))
    assert wheel is not None
    command = _provision.wheel_delivery_command(wheel)
    # $HOME must stay expandable: quoting it as a literal would create a
    # directory actually named "~"
    assert '"$HOME"' in command
    assert "~" not in command
    # the coordinator always streams the wheel, so the cached branch has
    # to consume stdin too or it hands the coordinator an EPIPE
    assert "cat > /dev/null" in command


def test_vagrant_ssh_argv() -> None:
    argv = _provision.vagrant_ssh_argv("default", None, "run-worker")
    assert argv == ["vagrant", "ssh", "default", "--", "-C", "run-worker"]
    argv = _provision.vagrant_ssh_argv("default", "/tmp/cfg", "run-worker")
    assert argv == [
        "vagrant",
        "ssh",
        "default",
        "--",
        "-C",
        "-F",
        "/tmp/cfg",
        "run-worker",
    ]


def test_sub_spawn_argv_plain_popen() -> None:
    import sys

    argv, delivery = _provision.sub_spawn_argv({"config": "{}"})
    assert argv == [
        sys.executable,
        "-u",
        "-m",
        "execnet",
        "worker",
        "--config",
        "{}",
    ]
    assert delivery is None


def test_sub_spawn_argv_vagrant_released() -> None:
    request = {
        "config": "{}",
        "vagrant_ssh": "default",
        "requirement": "execnet==9.9.9",
    }
    argv, delivery = _provision.sub_spawn_argv(request)
    assert argv[:5] == ["vagrant", "ssh", "default", "--", "-C"]
    assert "execnet==9.9.9" in argv[-1]
    assert delivery is None
