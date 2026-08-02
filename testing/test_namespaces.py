"""The public namespaces, and the deprecation shims for the private modules.

There is one namespace per concurrency library the caller drives execnet
from: top-level ``execnet.*`` is an alias surface over ``execnet.sync``
(threads), ``execnet.trio`` and ``execnet.aio`` expose the async-native core
and its asyncio bridge, and ``execnet.gevent`` the greenlet-parking blocking
surface.  Everything else in the package is private -- the pre-Trio module
names survive only as warning shims.
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
import execnet.raw_trio
import execnet.sync
import execnet.trio

#: the only modules that may be reachable without a leading underscore
PUBLIC_NAMESPACES = ("aio", "gevent", "raw_trio", "sync", "trio")

#: those importable without an optional dependency (execnet.gevent needs gevent)
ALWAYS_IMPORTABLE = tuple(n for n in PUBLIC_NAMESPACES if n != "gevent")

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
    [execnet, *[importlib.import_module(f"execnet.{n}") for n in ALWAYS_IMPORTABLE]],
)
def test_namespace_all_resolves(namespace: object) -> None:
    missing = [n for n in namespace.__all__ if not hasattr(namespace, n)]  # type: ignore[attr-defined]
    assert not missing


def test_gevent_namespace_all_resolves() -> None:
    pytest.importorskip("gevent")
    namespace = importlib.import_module("execnet.gevent")
    assert not [n for n in namespace.__all__ if not hasattr(namespace, n)]
    # the facade is the sync surface with greenlet parking wired in
    assert namespace.Group._wait_backend == "gevent"
    assert issubclass(namespace.Group, execnet.Group)


def test_no_unexpected_public_modules() -> None:
    found = {
        info.name
        for info in pkgutil.iter_modules(execnet.__path__)
        if not info.name.startswith("_")
    }
    assert found == set(PUBLIC_NAMESPACES) | set(SHIMS)


def test_trio_namespace_exposes_async_core() -> None:
    from execnet import _trio_gateway

    assert execnet.raw_trio.AsyncGroup is _trio_gateway.AsyncGroup
    assert execnet.raw_trio.AsyncGateway is _trio_gateway.AsyncGateway
    assert execnet.raw_trio.AsyncChannel is _trio_gateway.AsyncChannel
    assert execnet.raw_trio.open_gateway is _trio_gateway.open_gateway
    # error types are shared with the sync surface; the standalone serializer
    # is intentionally not exposed on any public namespace
    assert execnet.raw_trio.RemoteError is execnet.RemoteError
    assert not hasattr(execnet.raw_trio, "dumps")


def test_trio_namespace_hides_raw_plumbing() -> None:
    # the raw-channel/stream layer is internal routing detail: reachable from
    # execnet._trio_gateway, not advertised on the public namespace
    for name in ("ByteStream", "RawChannel", "RawChannelStream", "serve_gateway"):
        assert name not in execnet.raw_trio.__all__, name


def test_can_send_lives_only_on_the_top_level() -> None:
    assert execnet.can_send({"a": [1, 2.0, b"x", None, (True, frozenset({3}))]})
    assert not execnet.can_send(object())
    # the wire contract does not vary by surface, so it is not mirrored
    for namespace in (execnet.sync, execnet.raw_trio, execnet.trio, execnet.aio):
        assert "can_send" not in namespace.__all__, namespace.__name__


def test_dumps_is_a_temporary_xdist_shim() -> None:
    # FOLLOW-UP: delete this test together with execnet._XDIST_COMPAT once
    # pytest-xdist stops probing with ``execnet.dumps`` / ``except DumpError``
    # and uses ``execnet.can_send`` instead.
    from execnet import _serialize

    assert execnet._XDIST_COMPAT == ("dumps",)
    assert execnet.dumps is _serialize.dumps
    # reachable, but never advertised
    assert "dumps" not in execnet.__all__
    assert "dumps" not in dir(execnet)


def test_dumps_shim_does_not_warn() -> None:
    # xdist reaches this from serialize_warning_message -- once per warning
    # a *user's* test raises, from inside pytest's warning-recording hook.
    # A warning there is attributed to that test, which cannot act on it,
    # and warning on every access made recording one warning record
    # another, unbounded, wedging the run.
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        for _ in range(3):
            execnet.dumps  # noqa: B018


def test_boundary_kit_is_private() -> None:
    # There is no third-party event-loop extension point: the two wait
    # backends are threads and gevent, and every other concurrency library
    # gets a facade instead of a wakener.
    assert not hasattr(execnet, "portal")
    for name in ("Wakener", "Mailbox", "OneShot", "LoopPortal"):
        assert not hasattr(execnet, name), name
    from execnet import _boundary

    assert not hasattr(_boundary, "register_wakener")
    assert _boundary.make_wakener("thread") is not None
    with pytest.raises(ValueError, match="unknown wait backend"):
        _boundary.make_wakener("nope")  # type: ignore[arg-type]


def test_lazy_submodule_attribute_access() -> None:
    # After ``import execnet`` alone, execnet.raw_trio is reachable as an
    # attribute (PEP 562) without having been imported.
    out = subprocess.run(
        [
            sys.executable,
            "-c",
            "import execnet; print(execnet.raw_trio.AsyncGroup.__name__)",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert out.stdout.strip() == "AsyncGroup"


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
