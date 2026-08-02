"""The public namespaces, and the deprecation shims for the private modules.

There is one namespace per concurrency library the caller drives execnet
from: top-level ``execnet.*`` is an alias surface over ``execnet.sync``
(threads), ``execnet.trio`` and ``execnet.aio`` await the same engine from
their own loops, ``execnet.gevent`` parks greenlets, and
``execnet.raw_trio`` embeds the core in the caller's own trio run with no
engine at all.  Everything else in the package is private -- the pre-Trio
module names survive only as warning shims.

The surface tables below are the point of this module.  A namespace that
quietly loses a verb -- ``execnet.gevent`` shipped without ``Deployment``
for a while, and nothing noticed -- is a hole a test should have closed,
and the facades' *deliberate* omissions are only credible if they are
written down somewhere that fails when they change.
"""

from __future__ import annotations

import importlib
import pkgutil
import subprocess
import sys
import warnings
from typing import Any

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


#: the verbs every namespace that talks to a worker must offer.  Kept as
#: data because the failure mode is a namespace silently missing one, not a
#: namespace getting one wrong.
COMMON_NAMES = (
    "ChannelClosed",
    "DataFormatError",
    "DumpError",
    "ExecnetStateError",
    "GatewayGone",
    "HostNotFound",
    "LoadError",
    "RemoteError",
    "TimeoutError",
    "XSpec",
)

#: the deployment layer, which reaches a worker through a service and is
#: therefore available from every surface that can hold a gateway
DEPLOYMENT_NAMES = ("Deployed", "Deployment", "transfer")

#: surfaces that put protocol IO on a ProtocolEngine, and so expose it
ENGINE_NAMES = ("ActiveGroupsWarning", "ProtocolEngine")


def _namespace(name: str) -> Any:
    """Import a public namespace, skipping the one with a hard dependency."""
    if name == "gevent":
        pytest.importorskip("gevent")
    return importlib.import_module(f"execnet.{name}")


@pytest.mark.parametrize("name", COMMON_NAMES)
@pytest.mark.parametrize("namespace", PUBLIC_NAMESPACES)
def test_every_namespace_exports_the_common_names(namespace: str, name: str) -> None:
    module = _namespace(namespace)
    assert name in module.__all__, f"execnet.{namespace} is missing {name}"


@pytest.mark.parametrize("name", DEPLOYMENT_NAMES)
@pytest.mark.parametrize("namespace", PUBLIC_NAMESPACES)
def test_every_namespace_can_deploy(namespace: str, name: str) -> None:
    # execnet.gevent shipped without these while _deploy._facade already had
    # a gevent parking path: the plumbing was there and the names were not.
    # The async namespaces bind ``transfer`` to their own coroutine rather
    # than the blocking one, which is the same verb either way.
    module = _namespace(namespace)
    assert name in module.__all__, f"execnet.{namespace} is missing {name}"


@pytest.mark.parametrize("name", ENGINE_NAMES)
@pytest.mark.parametrize("namespace", ["sync", "gevent", "aio", "trio"])
def test_engine_backed_namespaces_expose_the_engine(namespace: str, name: str) -> None:
    module = _namespace(namespace)
    assert name in module.__all__, f"execnet.{namespace} is missing {name}"


def test_raw_trio_has_no_engine() -> None:
    # it does not have one: the gateways are tasks in the caller's nursery
    for name in ENGINE_NAMES:
        assert name not in execnet.raw_trio.__all__, name


#: the facades' public member sets, pinned.  Adding to these is a decision;
#: the point of writing them down is that it cannot happen by accident.
FACADE_SURFACE = {
    "AsyncGroup": {
        "aclose",
        "engine",
        "makegateway",
        "start",
    },
    "AsyncGateway": {
        "id",
        "remote_exec",
        "remoteaddress",
        "terminate",
    },
    "AsyncChannel": {
        "aclose",
        "id",
        "isclosed",
        "receive",
        "send",
        "send_eof",
        "wait_closed",
    },
}

#: what the raw surface has and a facade deliberately does not.  Channel
#: ids come from an unlocked per-gateway counter that works only because
#: one loop owns it, so a second allocator across the bridge would collide.
RAW_ONLY = {
    "AsyncGateway": {"_open_raw_channel", "open_channel", "_enqueue_frame"},
}


def _public_members(obj: type) -> set[str]:
    return {
        name
        for name in dir(obj)
        if not name.startswith("_") and not isinstance(getattr(obj, name, None), type)
    }


@pytest.mark.parametrize("namespace", ["aio", "trio"])
@pytest.mark.parametrize("classname", sorted(FACADE_SURFACE))
def test_the_facades_have_the_same_public_surface(
    namespace: str, classname: str
) -> None:
    module = _namespace(namespace)
    assert _public_members(getattr(module, classname)) == FACADE_SURFACE[classname]


@pytest.mark.parametrize("namespace", ["aio", "trio"])
@pytest.mark.parametrize("classname", sorted(RAW_ONLY))
def test_the_facades_omit_what_does_not_cross_the_bridge(
    namespace: str, classname: str
) -> None:
    module = _namespace(namespace)
    facade = getattr(module, classname)
    raw = getattr(execnet.raw_trio, classname)
    for name in RAW_ONLY[classname]:
        assert hasattr(raw, name), f"raw_trio.{classname} lost {name}"
        assert not hasattr(facade, name), f"execnet.{namespace}.{classname} has {name}"
