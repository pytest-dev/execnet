"""The public namespaces, and the deprecation shims for the private modules.

Top-level ``execnet.*`` is an alias surface over ``execnet.sync``; the trio
and aio namespaces expose the async-native core and its asyncio bridge; the
portal namespace the cross-thread primitives.  Everything else in the package
is private -- the pre-Trio module names survive only as warning shims.
"""

from __future__ import annotations

import importlib
import pkgutil
import subprocess
import sys
import warnings

import pytest

import execnet
import execnet.aio
import execnet.portal
import execnet.sync
import execnet.trio

#: the only modules that may be reachable without a leading underscore
PUBLIC_NAMESPACES = ("aio", "portal", "sync", "trio")

#: pre-Trio module names kept as deprecated forwarding shims
SHIMS = ("gateway", "gateway_base", "multi", "rsync", "rsync_remote", "xspec")


def test_top_level_names_alias_execnet_sync() -> None:
    for name in execnet.sync.__all__:
        assert getattr(execnet, name) is getattr(execnet.sync, name), name


def test_top_level_all_matches_sync_surface() -> None:
    # the top level is the sync surface plus the two package-level names:
    # the version, and can_send (a wire-format fact, not a gateway API)
    assert set(execnet.__all__) == set(execnet.sync.__all__) | {
        "__version__",
        "can_send",
    }


@pytest.mark.parametrize(
    "namespace",
    [execnet, *[importlib.import_module(f"execnet.{n}") for n in PUBLIC_NAMESPACES]],
)
def test_namespace_all_resolves(namespace: object) -> None:
    missing = [n for n in namespace.__all__ if not hasattr(namespace, n)]  # type: ignore[attr-defined]
    assert not missing


def test_no_unexpected_public_modules() -> None:
    found = {
        info.name
        for info in pkgutil.iter_modules(execnet.__path__)
        if not info.name.startswith("_")
    }
    assert found == set(PUBLIC_NAMESPACES) | set(SHIMS)


def test_trio_namespace_exposes_async_core() -> None:
    from execnet import _trio_gateway

    assert execnet.trio.AsyncGroup is _trio_gateway.AsyncGroup
    assert execnet.trio.AsyncGateway is _trio_gateway.AsyncGateway
    assert execnet.trio.AsyncChannel is _trio_gateway.AsyncChannel
    assert execnet.trio.open_popen_gateway is _trio_gateway.open_popen_gateway
    # error types are shared with the sync surface; the standalone serializer
    # is intentionally not exposed on any public namespace
    assert execnet.trio.RemoteError is execnet.RemoteError
    assert not hasattr(execnet.trio, "dumps")


def test_trio_namespace_hides_raw_plumbing() -> None:
    # the raw-channel/stream layer is internal routing detail: reachable from
    # execnet._trio_gateway, not advertised on the public namespace
    for name in ("ByteStream", "RawChannel", "RawChannelStream", "serve_gateway"):
        assert name not in execnet.trio.__all__, name


def test_can_send_lives_only_on_the_top_level() -> None:
    assert execnet.can_send({"a": [1, 2.0, b"x", None, (True, frozenset({3}))]})
    assert not execnet.can_send(object())
    # the wire contract does not vary by surface, so it is not mirrored
    for namespace in (execnet.sync, execnet.trio, execnet.aio, execnet.portal):
        assert "can_send" not in namespace.__all__, namespace.__name__


def test_dumps_is_a_temporary_xdist_shim() -> None:
    # FOLLOW-UP: delete this test together with execnet._XDIST_COMPAT once
    # pytest-xdist stops probing with ``execnet.dumps`` / ``except DumpError``
    # and uses ``execnet.can_send`` instead.
    from execnet import _serialize

    assert execnet._XDIST_COMPAT == ("dumps",)
    with pytest.warns(DeprecationWarning, match="temporary pytest-xdist"):
        assert execnet.dumps is _serialize.dumps
    # reachable, but never advertised
    assert "dumps" not in execnet.__all__
    assert "dumps" not in dir(execnet)


def test_portal_namespace() -> None:
    assert execnet.portal.__all__ == [
        "LoopPortal",
        "Mailbox",
        "OneShot",
        "ThreadWakener",
        "Wakener",
    ]
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


@pytest.mark.parametrize("shim", SHIMS)
def test_shim_reachable_as_package_attribute(shim: str) -> None:
    # pytest-xdist reaches these as ``execnet.gateway_base.X`` after a plain
    # ``import execnet``; that used to work because the import chain pulled
    # them in, and must keep working for as long as the shims exist.
    out = subprocess.run(
        [sys.executable, "-c", f"import execnet; print(execnet.{shim}.__name__)"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert out.stdout.strip() == f"execnet.{shim}"


def shim_attributes() -> list[tuple[str, str, str]]:
    """``(shim, attribute, private module)`` for every forwarded name."""
    cases = []
    for shim in SHIMS:
        module = importlib.import_module(f"execnet.{shim}")
        for name, target in module._MOVED.items():
            cases.append((shim, name, target))
    return cases


@pytest.mark.parametrize(("shim", "name", "target"), shim_attributes(), ids=str)
def test_shim_warns_and_forwards(shim: str, name: str, target: str) -> None:
    module = importlib.import_module(f"execnet.{shim}")
    private = importlib.import_module(f"execnet{target}")
    if not hasattr(private, name):
        # ``trace``/``notrace``/``fn`` exist only under a given EXECNET_DEBUG
        pytest.skip(f"execnet{target}.{name} not defined in this configuration")
    with pytest.warns(DeprecationWarning, match=f"execnet.{shim} is private"):
        value = getattr(module, name)
    assert value is getattr(private, name)


@pytest.mark.parametrize("shim", SHIMS)
def test_shim_rejects_unknown_attribute(shim: str) -> None:
    module = importlib.import_module(f"execnet.{shim}")
    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)
        with pytest.raises(AttributeError, match="no attribute 'nonexistent'"):
            getattr(module, "nonexistent")  # noqa: B009
