"""The exception shape, and the compatibility it has to keep.

Three questions a caller asks, each with an answer they can ``except`` on:
did the other side fail (``RemoteError``), is the connection gone
(``OSError`` and its subclasses), did I use this wrong
(``ExecnetStateError``, deliberately *not* an ``OSError``).

Most of what is pinned here is inheritance rather than behaviour, because
inheritance is the whole contract: a caller writes ``except OSError`` once
and every reason a connection can be gone has to land in it.
"""

from __future__ import annotations

import builtins

import pytest

import execnet
from execnet._channel import Channel

TESTTIMEOUT = 30.0


class TestTheShape:
    @pytest.mark.parametrize(
        ("error", "bases"),
        [
            (execnet.RemoteError, (Exception,)),
            (execnet.DumpError, (execnet.DataFormatError,)),
            (execnet.LoadError, (execnet.DataFormatError,)),
            (execnet.HostNotFound, (ConnectionError, OSError)),
            (execnet.ChannelClosed, (OSError,)),
            (execnet.GatewayGone, (OSError, EOFError)),
            (execnet.TimeoutError, (builtins.TimeoutError, OSError)),
            (execnet.ExecnetStateError, (RuntimeError,)),
        ],
    )
    def test_each_error_is_catchable_as_what_it_means(
        self, error: type[BaseException], bases: tuple[type[BaseException], ...]
    ) -> None:
        for base in bases:
            assert issubclass(error, base), f"{error.__name__} is not a {base.__name__}"

    def test_api_misuse_is_not_a_connection_failure(self) -> None:
        # the point of the whole exercise: something retrying on connection
        # loss must not also retry on its own bug
        assert not issubclass(execnet.ExecnetStateError, OSError)

    def test_every_way_a_connection_can_be_gone_is_an_oserror(self) -> None:
        for error in (
            execnet.ChannelClosed,
            execnet.GatewayGone,
            execnet.HostNotFound,
            execnet.TimeoutError,
            execnet._errors.ForkedResourceError,
        ):
            assert issubclass(error, OSError), error.__name__


class TestTimeoutErrorIsTheBuiltin:
    """The bug this shape was written to fix.

    ``execnet.TimeoutError`` shadowed the builtin without subclassing it, so
    the obvious ``except TimeoutError:`` caught nothing and only
    ``except OSError`` worked -- and since 3.11 ``asyncio.TimeoutError`` *is*
    the builtin, so async callers' instincts were actively wrong.
    """

    @pytest.mark.parametrize("catcher", [builtins.TimeoutError, OSError, IOError])
    def test_it_is_caught_by_every_spelling(self, catcher: type[BaseException]) -> None:
        with pytest.raises(catcher):
            raise execnet.TimeoutError("nothing arrived")

    def test_a_real_receive_timeout_is_caught_by_the_builtin(self) -> None:
        group = execnet.Group()
        try:
            channel = group.makegateway("popen").remote_exec("channel.receive()")
            with pytest.raises(builtins.TimeoutError):
                channel.receive(timeout=0.05)
            channel.send(None)
        finally:
            group.terminate(timeout=10.0)


class TestWhatXdistNeeds:
    """Released pytest-xdist reaches for these, and 3.0 keeps it working.

    Each is an accommodation with a removal note in ROADMAP-3.0.md; none of
    them may quietly stop being true in the meantime.
    """

    def test_dumperror_is_reachable_for_the_serializability_probe(self) -> None:
        # xdist/remote.py: `try: execnet.dumps(x) / except execnet.DumpError`
        assert issubclass(execnet.DumpError, Exception)
        with pytest.raises(execnet.DumpError):
            execnet.dumps(object())

    @pytest.mark.parametrize("name", ["RemoteError", "TimeoutError"])
    def test_the_error_types_are_class_attributes_on_channel(self, name: str) -> None:
        # xdist/looponfail.py writes `except self.channel.RemoteError`
        assert getattr(Channel, name) is getattr(execnet, name)

    def test_a_send_to_a_dead_peer_is_an_oserror(self) -> None:
        # xdist/workermanage.py swallows exactly this around its shutdown
        # send; anything not an OSError makes every teardown raise
        group = execnet.Group()
        gateway = group.makegateway("popen")
        channel = gateway.remote_exec("channel.send(1)")
        assert channel.receive(TESTTIMEOUT) == 1
        gateway.exit()
        gateway.join(TESTTIMEOUT)
        with pytest.raises(OSError):
            for _ in range(100):  # the close takes a send or two to surface
                channel.send("into the void")
        group.terminate(timeout=10.0)


class TestTheInternalBoundary:
    """Nothing from ``_async`` may reach user code.

    It is the neutral spelling of what a *backend* raises, and one already
    escaped: ``open_tcp_stream`` translated a connect failure into
    ``BrokenResource``, which silently lost ``HostNotFound``.
    """

    def test_the_backend_vocabulary_is_not_exported(self) -> None:
        import importlib

        for namespace in ("", ".sync", ".trio", ".raw_trio", ".aio"):
            module = importlib.import_module(f"execnet{namespace}")
            exported = set(module.__all__)
            for leaked in ("ClosedResource", "BrokenResource", "EndOfChannel"):
                assert leaked not in exported, f"execnet{namespace} exports {leaked}"

    def test_an_unreachable_socket_is_a_hostnotfound(self) -> None:
        # the exact case that leaked: a connect failure is "could not reach",
        # not "the stream broke"
        group = execnet.Group()
        try:
            with pytest.raises(execnet.HostNotFound):
                group.makegateway("socket=localhost:1")
        finally:
            group.terminate(timeout=10.0)
