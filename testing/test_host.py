"""The Trio host thread: sharing, explicit override, and loop-misuse guards.

``execnet.trio`` runs gateways directly in the caller's own nursery; every
other surface drives a :class:`execnet.Host`.  One is shared per process,
and blocking on it from inside a running event loop is an error rather
than a hang.
"""

from __future__ import annotations

import asyncio
import os
import sys
import threading

import pytest
import trio

import execnet
from execnet._host import Host
from execnet._host import default_host

TESTTIMEOUT = 30.0


def host_thread_names() -> list[str]:
    return [t.name for t in threading.enumerate() if t.name.startswith("execnet-host")]


class TestSharedHost:
    def test_groups_share_the_default_host(self) -> None:
        a = execnet.Group()
        b = execnet.Group()
        assert a.host is b.host is default_host()

    def test_many_groups_run_one_thread(self) -> None:
        groups = [execnet.Group() for _ in range(3)]
        try:
            for group in groups:
                group.makegateway("popen")
            assert len(host_thread_names()) == 1
            for group in groups:
                channel = group[0].remote_exec("channel.send(1)")
                assert channel.receive(TESTTIMEOUT) == 1
        finally:
            for group in groups:
                group.terminate(timeout=5.0)

    def test_explicit_host_is_isolated_and_closes(self) -> None:
        host = Host(name="execnet-host-isolated")
        group = execnet.Group(host=host)
        assert group.host is host
        assert group.host is not default_host()
        try:
            gateway = group.makegateway("popen")
            channel = gateway.remote_exec("channel.send(6 * 7)")
            assert channel.receive(TESTTIMEOUT) == 42
            assert "execnet-host-isolated" in host_thread_names()
        finally:
            group.terminate(timeout=5.0)
        host.close()
        assert not host.running
        assert "execnet-host-isolated" not in host_thread_names()

    def test_host_context_manager_closes(self) -> None:
        with Host(name="execnet-host-ctx") as host:
            group = execnet.Group(host=host)
            group.makegateway("popen")
            group.terminate(timeout=5.0)
        assert not host.running

    def test_starting_is_lazy(self) -> None:
        host = Host(name="execnet-host-lazy")
        execnet.Group(host=host)
        # constructing a group must not cost a thread
        assert not host.running
        assert "execnet-host-lazy" not in host_thread_names()

    @pytest.mark.skipif(not hasattr(os, "fork"), reason="requires os.fork")
    def test_forked_child_gets_a_fresh_default_host(self) -> None:
        # the child inherits a Host object whose thread does not exist
        # there, so the first use must build a new one
        default_host()._ensure_started()
        read_fd, write_fd = os.pipe()
        pid = os.fork()
        if pid == 0:  # pragma: no cover - runs in the child
            code = 1
            try:
                os.close(read_fd)
                child_host = default_host()
                group = execnet.Group()
                gateway = group.makegateway("popen")
                got = gateway.remote_exec("channel.send(3)").receive(TESTTIMEOUT)
                group.terminate(timeout=5.0)
                code = 0 if (got == 3 and child_host.running) else 1
            finally:
                os.write(write_fd, bytes([code]))
                os._exit(0)
        os.close(write_fd)
        try:
            result = os.read(read_fd, 1)
        finally:
            os.close(read_fd)
            os.waitpid(pid, 0)
        assert result == b"\x00"


class TestHostDestruction:
    """Closing a host breaks what it served -- loudly, and without hanging.

    A gateway's protocol IO lives on the host loop, so stopping that loop
    is not a resource being freed underneath a working object: it ends the
    connection.  Every operation that needs the loop must say so at the
    call site rather than hang, deliver nothing silently, or quietly start
    a second loop thread that none of the existing gateways are on.
    """

    def test_close_breaks_the_channels_it_served(self) -> None:
        host = Host(name="execnet-host-broken-channel")
        group = execnet.Group(host=host)
        gateway = group.makegateway("popen")
        channel = gateway.remote_exec("while 1: channel.send(channel.receive() + 1)")
        channel.send(1)
        assert channel.receive(TESTTIMEOUT) == 2

        host.close()

        assert not gateway.hasreceiver()
        with pytest.raises(EOFError):
            channel.receive(TESTTIMEOUT)
        with pytest.raises(OSError):
            channel.send(3)
        # closed for receiving, so this returns instead of timing out
        channel.waitclose(TESTTIMEOUT)
        group.terminate(timeout=5.0)

    def test_close_breaks_the_gateways_it_served(self) -> None:
        host = Host(name="execnet-host-broken-gateway")
        group = execnet.Group(host=host)
        gateway = group.makegateway("popen")
        assert gateway.remote_exec("channel.send(1)").receive(TESTTIMEOUT) == 1

        host.close()

        with pytest.raises(OSError):
            gateway.newchannel()
        with pytest.raises(OSError):
            gateway.remote_exec("channel.send(1)")
        # the receiver is finished, so this must not block
        gateway.join(TESTTIMEOUT)
        group.terminate(timeout=5.0)

    def test_close_breaks_the_group_and_starts_no_second_loop(self) -> None:
        host = Host(name="execnet-host-broken-group")
        group = execnet.Group(host=host)
        group.makegateway("popen")

        host.close()

        assert not host.running
        with pytest.raises(RuntimeError, match="was closed"):
            group.makegateway("popen")
        # the failed attempt must not have resurrected a loop thread: the
        # group's existing gateways could never be attached to it
        assert not host.running
        assert "execnet-host-broken-group" not in host_thread_names()
        # cleaning up a broken group still returns
        group.terminate(timeout=5.0)

    def test_closing_is_final_even_for_an_unused_host(self) -> None:
        host = Host(name="execnet-host-unused")
        host.close()
        with pytest.raises(RuntimeError, match="was closed"):
            execnet.Group(host=host).makegateway("popen")

    def test_aio_group_on_a_closed_host_raises(self) -> None:
        host = Host(name="execnet-host-closed-aio")
        host.close()

        async def main() -> None:
            with pytest.raises(RuntimeError, match="was closed"):
                await execnet.aio.AsyncGroup(host=host).start()

        asyncio.run(main())

    def test_setcallback_after_close_fails_without_wedging_the_channel(self) -> None:
        # the consumer task runs on the host loop, so with the loop gone
        # there is nothing to attach to -- but the failure must land on the
        # caller, not on the channel: a half-switched channel drops what it
        # had buffered, refuses receive(), and makes waitclose() wait for a
        # consumer that will never run
        host = Host(name="execnet-host-late-callback")
        group = execnet.Group(host=host)
        gateway = group.makegateway("popen")
        channel = gateway.remote_exec("channel.send(1); channel.send(2)")
        assert channel.receive(TESTTIMEOUT) == 1
        # everything the worker sent has arrived and is buffered by now
        channel.waitclose(TESTTIMEOUT)

        host.close()

        received: list[object] = []
        with pytest.raises(OSError, match="host loop"):
            channel.setcallback(received.append, endmarker="END")
        assert received == []
        # untouched: the buffered item is still there, then EOF
        assert channel.receive(TESTTIMEOUT) == 2
        with pytest.raises(EOFError):
            channel.receive(TESTTIMEOUT)
        channel.waitclose(TESTTIMEOUT)
        group.terminate(timeout=5.0)


class TestEventLoopGuard:
    """Blocking on the host from inside a running loop must not hang."""

    def test_makegateway_inside_asyncio_raises(self) -> None:
        async def main() -> None:
            with pytest.raises(RuntimeError, match=r"execnet\.aio"):
                execnet.Group().makegateway("popen")

        asyncio.run(main())

    def test_makegateway_inside_trio_raises(self) -> None:
        async def main() -> None:
            with pytest.raises(RuntimeError, match=r"execnet\.trio"):
                execnet.Group().makegateway("popen")

        trio.run(main)

    def test_channel_receive_inside_asyncio_raises(self) -> None:
        group = execnet.Group()
        try:
            gateway = group.makegateway("popen")
            channel = gateway.remote_exec("channel.send(1)")

            async def main() -> None:
                with pytest.raises(RuntimeError, match=r"execnet\.aio"):
                    channel.receive(TESTTIMEOUT)
                with pytest.raises(RuntimeError, match=r"execnet\.aio"):
                    channel.send(1)
                with pytest.raises(RuntimeError, match=r"execnet\.aio"):
                    channel.waitclose(TESTTIMEOUT)

            asyncio.run(main())
            # still usable from a plain thread afterwards
            assert channel.receive(TESTTIMEOUT) == 1
        finally:
            group.terminate(timeout=5.0)

    def test_worker_channels_are_not_guarded(self) -> None:
        # exec'd code may run its own event loop and talk to its channel
        # from inside it -- that is the caller's own loop to block
        group = execnet.Group()
        try:
            gateway = group.makegateway("popen")
            channel = gateway.remote_exec(
                """
                import asyncio

                async def main():
                    channel.send(channel.receive() + 1)

                asyncio.run(main())
                """
            )
            channel.send(41)
            assert channel.receive(TESTTIMEOUT) == 42
        finally:
            group.terminate(timeout=5.0)

    def test_a_worker_thread_is_still_fine(self) -> None:
        group = execnet.Group()
        result: list[object] = []

        def work() -> None:
            gateway = group.makegateway("popen")
            result.append(gateway.remote_exec("channel.send(5)").receive(TESTTIMEOUT))

        async def main() -> None:
            # inside a loop, but the blocking call happens off it
            await asyncio.to_thread(work)

        try:
            asyncio.run(main())
            assert result == [5]
        finally:
            group.terminate(timeout=5.0)

    def test_no_asyncio_import_no_cost(self) -> None:
        # the probe must not import asyncio/trio into a program that has
        # neither; it goes through sys.modules first
        code = (
            "import sys, execnet;"
            " execnet._host.check_not_in_event_loop('x');"
            " print('asyncio' in sys.modules, 'trio' in sys.modules)"
        )
        import subprocess

        out = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, check=True
        )
        assert out.stdout.split() == ["False", "False"]
