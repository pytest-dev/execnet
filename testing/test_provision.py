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
    command, preamble = _provision.ssh_remote_command(spec)
    assert "execnet==9.9.9" in command
    assert "head -c" not in command  # no shipping
    assert preamble == b""


@pytest.mark.skipif(released, reason="released execnet resolves from an index")
@pytest.mark.skipif(not _provision.uv_available(), reason="uv required to build wheel")
def test_ssh_remote_command_dev_ships_wheel() -> None:
    spec = execnet.XSpec("ssh=host//id=gw0//execmodel=thread")
    command, preamble = _provision.ssh_remote_command(spec)
    assert "mktemp -d" in command
    assert f"head -c {len(preamble)}" in command
    assert preamble[:2] == b"PK"  # a wheel is a zip archive
