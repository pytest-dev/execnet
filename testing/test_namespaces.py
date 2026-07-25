"""The three public namespaces: execnet.sync / execnet.trio / execnet.portal.

Top-level ``execnet.*`` is an alias surface over ``execnet.sync``; the
trio namespace exposes the async-native core; the portal namespace the
cross-thread primitives.
"""

from __future__ import annotations

import subprocess
import sys

import execnet
import execnet.portal
import execnet.sync
import execnet.trio


def test_top_level_names_alias_execnet_sync() -> None:
    for name in execnet.sync.__all__:
        assert getattr(execnet, name) is getattr(execnet.sync, name), name


def test_top_level_all_matches_sync_surface() -> None:
    assert set(execnet.__all__) == set(execnet.sync.__all__) | {"__version__"}


def test_trio_namespace_exposes_async_core() -> None:
    from execnet import _trio_gateway

    assert execnet.trio.AsyncGroup is _trio_gateway.AsyncGroup
    assert execnet.trio.AsyncGateway is _trio_gateway.AsyncGateway
    assert execnet.trio.AsyncChannel is _trio_gateway.AsyncChannel
    assert execnet.trio.open_popen_gateway is _trio_gateway.open_popen_gateway
    # serialization + errors are shared with the sync surface
    assert execnet.trio.RemoteError is execnet.RemoteError
    assert execnet.trio.dumps is execnet.dumps


def test_portal_namespace() -> None:
    assert execnet.portal.__all__ == ["LoopPortal", "SyncReceiver"]
    assert execnet.portal.LoopPortal is not None


def test_lazy_submodule_attribute_access() -> None:
    # After ``import execnet`` alone, execnet.trio / execnet.portal are
    # reachable as attributes (PEP 562) without having been imported.
    out = subprocess.run(
        [
            sys.executable,
            "-c",
            "import execnet; print(execnet.trio.AsyncGroup.__name__);"
            " print(execnet.portal.LoopPortal.__name__)",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert out.stdout.split() == ["AsyncGroup", "LoopPortal"]


def test_import_execnet_does_not_import_trio() -> None:
    # The blocking surface must stay importable without loading the trio
    # event loop machinery (it loads lazily on first gateway / namespace
    # use).
    out = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys, execnet; print('trio' in sys.modules)",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert out.stdout.strip() == "False"
