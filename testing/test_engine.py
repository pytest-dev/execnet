"""The protocol engine: sharing, explicit override, and loop-misuse guards.

``execnet.raw_trio`` runs gateways directly in the caller's own nursery;
every other surface drives a :class:`execnet.ProtocolEngine`.  One is
shared per process, and blocking on it from inside a running event loop is
an error rather than a hang.
"""

from __future__ import annotations

import asyncio
import os
import select
import signal
import sys
import threading
import warnings
from collections.abc import Callable

import pytest
import trio

import execnet
from execnet import _trio_engine
from execnet._engine import ProtocolEngine
from execnet._engine import default_engine
from execnet._errors import ForkedResourceError

TESTTIMEOUT = 30.0


def engine_thread_names() -> list[str]:
    return [
        t.name for t in threading.enumerate() if t.name.startswith("execnet-engine")
    ]


class TestSharedEngine:
    def test_groups_share_the_default_engine(self) -> None:
        a = execnet.Group()
        b = execnet.Group()
        assert a.engine is b.engine is default_engine()

    def test_many_groups_run_one_thread(self) -> None:
        groups = [execnet.Group() for _ in range(3)]
        try:
            for group in groups:
                group.makegateway("popen")
            assert len(engine_thread_names()) == 1
            for group in groups:
                channel = group[0].remote_exec("channel.send(1)")
                assert channel.receive(TESTTIMEOUT) == 1
        finally:
            for group in groups:
                group.terminate(timeout=5.0)

    def test_explicit_engine_is_isolated_and_closes(self) -> None:
        engine = ProtocolEngine(name="execnet-engine-isolated")
        group = execnet.Group(engine=engine)
        assert group.engine is engine
        assert group.engine is not default_engine()
        try:
            gateway = group.makegateway("popen")
            channel = gateway.remote_exec("channel.send(6 * 7)")
            assert channel.receive(TESTTIMEOUT) == 42
            assert "execnet-engine-isolated" in engine_thread_names()
        finally:
            group.terminate(timeout=5.0)
        engine.close()
        assert not engine.running
        assert "execnet-engine-isolated" not in engine_thread_names()

    def test_engine_context_manager_closes(self) -> None:
        with ProtocolEngine(name="execnet-engine-ctx") as engine:
            group = execnet.Group(engine=engine)
            group.makegateway("popen")
            group.terminate(timeout=5.0)
        assert not engine.running

    def test_a_loop_that_cannot_start_says_why(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # the loop comes up on a thread nobody is watching, so a trio.run
        # that dies at once used to leave the caller waiting out the full
        # 30s start timeout for a message that named nothing
        async def boom(self: object) -> None:
            raise RuntimeError("no event loop for you")

        monkeypatch.setattr(_trio_engine.TrioEngine, "_main", boom)
        engine = ProtocolEngine(name="execnet-engine-doomed")
        with pytest.raises(RuntimeError, match="could not start") as excinfo:
            execnet.Group(engine=engine).makegateway("popen")
        assert "no event loop for you" in str(excinfo.value)

    def test_starting_is_lazy(self) -> None:
        engine = ProtocolEngine(name="execnet-engine-lazy")
        execnet.Group(engine=engine)
        # constructing a group must not cost a thread
        assert not engine.running
        assert "execnet-engine-lazy" not in engine_thread_names()


def run_in_fork(child: Callable[[], list[str]], timeout: float = 20.0) -> list[str]:
    """Run ``child`` in a forked process and return the problems it reports.

    The child reports rather than asserts, because an assertion there dies
    with the child.  A child that blocks fails the test instead of hanging
    the suite -- most of what can go wrong after a fork is a wait for a loop
    thread that does not exist in this process.
    """
    read_fd, write_fd = os.pipe()
    pid = os.fork()
    if pid == 0:  # pragma: no cover - runs in the child
        problems = ["the child died before reporting"]
        try:
            os.close(read_fd)
            problems = child()
        except BaseException as exc:
            problems = [f"the child raised {type(exc).__name__}: {exc}"]
        finally:
            with os.fdopen(write_fd, "wb") as report:
                report.write("\n".join(problems).encode())
            # not sys.exit: the parent's atexit handlers are not ours to run
            os._exit(0)
    os.close(write_fd)
    chunks: list[bytes] = []
    try:
        if not select.select([read_fd], [], [], timeout)[0]:
            os.kill(pid, signal.SIGKILL)
            pytest.fail(f"the forked child was still blocked after {timeout}s")
        while chunk := os.read(read_fd, 4096):
            chunks.append(chunk)
    finally:
        os.close(read_fd)
        os.waitpid(pid, 0)
    return [line for line in b"".join(chunks).decode().splitlines() if line]


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires os.fork")
class TestFork:
    """Nothing execnet builds survives a fork, and it says so.

    The engine's loop thread is not duplicated into the child and the worker
    connections belong to the parent, so every inherited object is dead
    there.  Dead has to mean "raises and names the fork": the token of the
    parent's loop still *accepts* work in the child, so without a check the
    child waits forever for a reply nobody will send.  Recovery is the
    child's to make explicitly, by building a new engine and group.
    """

    def test_a_new_group_in_the_child_works(self) -> None:
        # the recovery path: default_engine() hands a child its own Engine
        default_engine()._ensure_started()

        def child() -> list[str]:
            problems = []
            if default_engine().running:
                problems.append("the inherited default engine claims to run here")
            group = execnet.Group()
            gateway = group.makegateway("popen")
            got = gateway.remote_exec("channel.send(3)").receive(TESTTIMEOUT)
            if got != 3:
                problems.append(f"a fresh group returned {got!r}")
            if not group.engine.running:
                problems.append("the child's own engine is not running")
            group.terminate(timeout=5.0)
            return problems

        assert run_in_fork(child) == []

    def test_inherited_channels_and_gateways_are_dead(self) -> None:
        group = execnet.Group()
        gateway = group.makegateway("popen")
        channel = gateway.remote_exec("while 1: channel.send(channel.receive())")
        channel.send(1)
        assert channel.receive(TESTTIMEOUT) == 1

        def child() -> list[str]:
            problems: list[str] = []

            def expect_forked(what: str, call: Callable[[], object]) -> None:
                try:
                    call()
                except ForkedResourceError as exc:
                    if "fork" not in str(exc):
                        problems.append(f"{what}: does not mention the fork: {exc}")
                except BaseException as exc:
                    problems.append(f"{what}: {type(exc).__name__}: {exc}")
                else:
                    problems.append(f"{what}: did not raise")

            expect_forked("channel.send()", lambda: channel.send(2))
            expect_forked("channel.receive()", lambda: channel.receive(TESTTIMEOUT))
            expect_forked("channel.waitclose()", lambda: channel.waitclose(5.0))
            expect_forked("gateway.remote_exec()", lambda: gateway.remote_exec("pass"))
            expect_forked("gateway.join()", lambda: gateway.join(5.0))
            expect_forked("group.terminate()", lambda: group.terminate(timeout=5.0))
            return problems

        assert run_in_fork(child) == []
        # ... and the parent's own gateway is untouched by all of that
        channel.send(2)
        assert channel.receive(TESTTIMEOUT) == 2
        group.terminate(timeout=5.0)

    def test_the_inherited_default_group_is_dead(self) -> None:
        # the module-level convenience group is built at import time, so it
        # is always one of the objects a fork leaves behind
        execnet.makegateway("popen")

        def child() -> list[str]:
            try:
                execnet.makegateway("popen")
            except ForkedResourceError as exc:
                return [] if "fork" in str(exc) else [f"unclear message: {exc}"]
            except BaseException as exc:
                return [f"raised {type(exc).__name__}: {exc}"]
            return ["execnet.makegateway() did not raise"]

        assert run_in_fork(child) == []
        execnet.default_group.terminate(timeout=5.0)

    def test_the_child_does_not_run_the_parents_cleanup(self) -> None:
        group = execnet.Group()
        group.makegateway("popen")

        def child() -> list[str]:
            # what atexit would call in the child: the parent's gateways are
            # not ours to terminate, and trying would raise from an exit hook
            group._cleanup_atexit()
            if not len(group):
                return ["the child unregistered the parent's gateways"]
            return []

        assert run_in_fork(child) == []
        assert group[0].remote_exec("channel.send(4)").receive(TESTTIMEOUT) == 4
        group.terminate(timeout=5.0)


class TestEngineDestruction:
    """Closing an engine breaks what it served -- loudly, and without hanging.

    A gateway's protocol IO lives on the engine loop, so stopping that loop
    is not a resource being freed underneath a working object: it ends the
    connection.  Every operation that needs the loop must say so at the
    call site rather than hang, deliver nothing silently, or quietly start
    a second loop thread that none of the existing gateways are on.

    Each of these closes with a group still live, which is what the warning
    is for; the contract around that is :class:`TestEngineShutdownContract`.
    """

    pytestmark = pytest.mark.filterwarnings("ignore::execnet.ActiveGroupsWarning")

    def test_close_breaks_the_channels_it_served(self) -> None:
        engine = ProtocolEngine(name="execnet-engine-broken-channel")
        group = execnet.Group(engine=engine)
        gateway = group.makegateway("popen")
        channel = gateway.remote_exec("while 1: channel.send(channel.receive() + 1)")
        channel.send(1)
        assert channel.receive(TESTTIMEOUT) == 2

        engine.close()

        assert not gateway.hasreceiver()
        with pytest.raises(EOFError):
            channel.receive(TESTTIMEOUT)
        with pytest.raises(OSError):
            channel.send(3)
        # closed for receiving, so this returns instead of timing out
        channel.waitclose(TESTTIMEOUT)
        group.terminate(timeout=5.0)

    def test_close_breaks_the_gateways_it_served(self) -> None:
        engine = ProtocolEngine(name="execnet-engine-broken-gateway")
        group = execnet.Group(engine=engine)
        gateway = group.makegateway("popen")
        assert gateway.remote_exec("channel.send(1)").receive(TESTTIMEOUT) == 1

        engine.close()

        with pytest.raises(OSError):
            gateway.newchannel()
        with pytest.raises(OSError):
            gateway.remote_exec("channel.send(1)")
        # the receiver is finished, so this must not block
        gateway.join(TESTTIMEOUT)
        group.terminate(timeout=5.0)

    def test_close_breaks_the_group_and_starts_no_second_loop(self) -> None:
        engine = ProtocolEngine(name="execnet-engine-broken-group")
        group = execnet.Group(engine=engine)
        group.makegateway("popen")

        engine.close()

        assert not engine.running
        with pytest.raises(RuntimeError, match="was closed"):
            group.makegateway("popen")
        # the failed attempt must not have resurrected a loop thread: the
        # group's existing gateways could never be attached to it
        assert not engine.running
        assert "execnet-engine-broken-group" not in engine_thread_names()
        # cleaning up a broken group still returns
        group.terminate(timeout=5.0)

    def test_closing_is_final_even_for_an_unused_engine(self) -> None:
        engine = ProtocolEngine(name="execnet-engine-unused")
        engine.close()
        with pytest.raises(RuntimeError, match="was closed"):
            execnet.Group(engine=engine).makegateway("popen")

    def test_aio_group_on_a_closed_engine_raises(self) -> None:
        engine = ProtocolEngine(name="execnet-engine-closed-aio")
        engine.close()

        async def main() -> None:
            with pytest.raises(RuntimeError, match="was closed"):
                await execnet.aio.AsyncGroup(engine=engine).start()

        asyncio.run(main())

    def test_setcallback_after_close_fails_without_wedging_the_channel(self) -> None:
        # the consumer task runs on the engine loop, so with the loop gone
        # there is nothing to attach to -- but the failure must land on the
        # caller, not on the channel: a half-switched channel drops what it
        # had buffered, refuses receive(), and makes waitclose() wait for a
        # consumer that will never run
        engine = ProtocolEngine(name="execnet-engine-late-callback")
        group = execnet.Group(engine=engine)
        gateway = group.makegateway("popen")
        channel = gateway.remote_exec("channel.send(1); channel.send(2)")
        assert channel.receive(TESTTIMEOUT) == 1
        # everything the worker sent has arrived and is buffered by now
        channel.waitclose(TESTTIMEOUT)

        engine.close()

        received: list[object] = []
        with pytest.raises(OSError, match="engine loop"):
            channel.setcallback(received.append, endmarker="END")
        assert received == []
        # untouched: the buffered item is still there, then EOF
        assert channel.receive(TESTTIMEOUT) == 2
        with pytest.raises(EOFError):
            channel.receive(TESTTIMEOUT)
        channel.waitclose(TESTTIMEOUT)
        group.terminate(timeout=5.0)


class TestEngineShutdownContract:
    """What closing does about the groups still running on it.

    Breaking them and walking away was the old behaviour, and it left real
    worker processes behind: nothing else was going to reap them once the
    loop that spoke to them was gone.  So closing terminates -- and says so,
    because doing it at close time is doing the caller's job at the moment
    they can least act on the result.  :meth:`ProtocolEngine.terminate` is
    the half to call while they still can.
    """

    def test_close_terminates_live_groups_and_warns(self) -> None:
        engine = ProtocolEngine(name="execnet-engine-terminating-close")
        group = execnet.Group(engine=engine)
        gateway = group.makegateway("popen")
        pid = gateway.remote_exec("import os; channel.send(os.getpid())").receive(
            TESTTIMEOUT
        )

        with pytest.warns(execnet.ActiveGroupsWarning, match="still running"):
            engine.close()

        # the worker is gone, not orphaned: the whole point of terminating
        assert not _process_alive(pid)
        group.terminate(timeout=5.0)

    def test_the_warning_names_what_is_still_running(self) -> None:
        engine = ProtocolEngine(name="execnet-engine-named-in-warning")
        group = execnet.Group(engine=engine)
        group.makegateway("popen//id=stillhere")

        with pytest.warns(execnet.ActiveGroupsWarning, match="stillhere"):
            engine.close()
        group.terminate(timeout=5.0)

    def test_terminate_drains_without_closing(self) -> None:
        # the deliberate half: workers reaped, engine still usable, and the
        # group can be rebuilt on it afterwards
        engine = ProtocolEngine(name="execnet-engine-drained")
        group = execnet.Group(engine=engine)
        gateway = group.makegateway("popen")
        pid = gateway.remote_exec("import os; channel.send(os.getpid())").receive(
            TESTTIMEOUT
        )

        engine.terminate(timeout=5.0)

        assert not _process_alive(pid)
        assert engine.running
        group.terminate(timeout=5.0)
        second = execnet.Group(engine=engine)
        try:
            channel = second.makegateway("popen").remote_exec("channel.send(7)")
            assert channel.receive(TESTTIMEOUT) == 7
        finally:
            second.terminate(timeout=5.0)
            engine.close()

    def test_a_drained_engine_closes_quietly(self) -> None:
        engine = ProtocolEngine(name="execnet-engine-quiet-close")
        group = execnet.Group(engine=engine)
        group.makegateway("popen")
        group.terminate(timeout=5.0)

        with warnings.catch_warnings():
            warnings.simplefilter("error", execnet.ActiveGroupsWarning)
            engine.close()

    def test_terminating_an_idle_engine_is_a_no_op(self) -> None:
        engine = ProtocolEngine(name="execnet-engine-never-started")
        engine.terminate()
        assert not engine.running
        # and it is still usable afterwards, unlike close()
        group = execnet.Group(engine=engine)
        try:
            assert (
                group.makegateway("popen")
                .remote_exec("channel.send(1)")
                .receive(TESTTIMEOUT)
                == 1
            )
        finally:
            group.terminate(timeout=5.0)
            engine.close()

    def test_closing_an_idle_engine_is_final_and_quiet(self) -> None:
        engine = ProtocolEngine(name="execnet-engine-idle-close")
        with warnings.catch_warnings():
            warnings.simplefilter("error", execnet.ActiveGroupsWarning)
            engine.close()
        with pytest.raises(RuntimeError, match="was closed"):
            engine.start()

    def test_closing_from_the_loop_thread_is_refused(self) -> None:
        # it would park the loop waiting for work only that loop can run
        engine = ProtocolEngine(name="execnet-engine-self-close")
        trio_engine = engine.start()._ensure_started()
        errors: list[BaseException] = []

        def close_from_the_loop() -> None:
            try:
                engine.close()
            except BaseException as exc:
                errors.append(exc)

        try:
            trio_engine.call_sync(close_from_the_loop)
        finally:
            engine.close()
        assert len(errors) == 1
        assert "own loop thread" in str(errors[0])

    def test_a_thread_that_does_not_join_is_reported(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # a wedged loop is a leaked thread; close() used to return as though
        # it had stopped one.  Wedged for real here: the shutdown request
        # never reaches the loop, so the thread genuinely outlives close().
        engine = ProtocolEngine(name="execnet-engine-wedged")
        trio_engine = engine.start()._ensure_started()
        monkeypatch.setattr(
            type(trio_engine.portal), "run_sync", lambda self, fn, *args: None
        )

        try:
            with pytest.warns(execnet.ActiveGroupsWarning, match="did not stop"):
                engine.close(timeout=0.01)
            assert trio_engine._thread is not None
            assert trio_engine._thread.is_alive()
        finally:
            monkeypatch.undo()
            trio_engine._started = True  # close() cleared it; really stop now
            trio_engine.stop(timeout=5.0)


def _process_alive(pid: int) -> bool:
    """Whether ``pid`` is still a live process (not a zombie)."""
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    if not sys.platform.startswith("linux"):  # pragma: no cover - linux CI
        return True
    try:
        with open(f"/proc/{pid}/stat") as stat:
            return stat.read().rsplit(") ", 1)[1].split()[0] != "Z"
    except OSError:
        return False


class TestPostedCallbacks:
    """Work posted to the loop must never raise *on* the loop.

    Trio turns an exception from an entry-queue callback into a
    TrioInternalError and tears the whole run down -- so one call losing a
    race with shutdown would take every gateway in the process with it, and
    tell the user to file a trio bug.  An engine that is already going away is
    an ordinary failure of that one call.
    """

    def test_a_call_racing_shutdown_reports_instead_of_killing_the_loop(
        self,
    ) -> None:
        engine = ProtocolEngine(name="execnet-engine-late-call")
        group = execnet.Group(engine=engine)
        gateway = group.makegateway("popen")
        trio_engine = engine._ensure_started()

        async def never() -> None:  # pragma: no cover - never spawned
            raise AssertionError("should not run")

        nursery, trio_engine._nursery = trio_engine._nursery, None
        try:
            # the window between the root nursery closing and the run ending
            pending = trio_engine._call_pending(never)
            with pytest.raises(RuntimeError, match="shut down"):
                pending.wait(TESTTIMEOUT)
        finally:
            trio_engine._nursery = nursery

        assert trio_engine._thread is not None and trio_engine._thread.is_alive()
        assert gateway.remote_exec("channel.send(7)").receive(TESTTIMEOUT) == 7
        group.terminate(timeout=5.0)
        engine.close()

    def test_an_aio_call_racing_shutdown_reports_instead_of_killing_the_loop(
        self,
    ) -> None:
        engine = ProtocolEngine(name="execnet-engine-late-aio-call")

        async def main() -> None:
            async with execnet.aio.AsyncGroup(engine=engine) as group:
                gateway = await group.makegateway("popen")
                trio_engine = engine._ensure_started()
                nursery, trio_engine._nursery = trio_engine._nursery, None
                try:
                    with pytest.raises(RuntimeError, match="shut down"):
                        await gateway.remote_exec("channel.send(1)")
                finally:
                    trio_engine._nursery = nursery
                assert (
                    trio_engine._thread is not None and trio_engine._thread.is_alive()
                )
                channel = await gateway.remote_exec("channel.send(7)")
                assert await channel.receive() == 7

        asyncio.run(main())
        engine.close()


class TestEventLoopGuard:
    """Blocking on the engine from inside a running loop must not hang."""

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

    def test_terminate_and_join_inside_asyncio_raise(self) -> None:
        # both block on the engine with no bound worth waiting out: join()
        # until the worker dies, terminate() for the whole grace
        group = execnet.Group()
        try:
            gateway = group.makegateway("popen")

            async def main() -> None:
                with pytest.raises(RuntimeError, match=r"execnet\.aio"):
                    gateway.join(TESTTIMEOUT)
                with pytest.raises(RuntimeError, match=r"execnet\.aio"):
                    group.terminate(timeout=5.0)
                # an empty group has nothing to block on, so cleaning one up
                # from inside a loop stays allowed
                execnet.Group().terminate(timeout=5.0)

            asyncio.run(main())
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
            " execnet._engine.check_not_in_event_loop('x');"
            " print('asyncio' in sys.modules, 'trio' in sys.modules)"
        )
        import subprocess

        out = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, check=True
        )
        assert out.stdout.split() == ["False", "False"]


class TestGeventPatchedProcess:
    """A monkey-patched process cannot engine the loop, and is told so.

    The stub stands in for ``gevent.monkey`` because the real thing patches
    the interpreter irreversibly -- and the point of the check is that it
    reads ``sys.modules``, so a stub exercises exactly what runs.  The real
    behaviour it stands for was measured in every variant: ``patch_all()``
    removes ``select.epoll``, ``patch_all(select=False)`` gives trio a
    gevent socketpair (EBADF), and patching neither still leaves
    ``queue.SimpleQueue`` gevent's (``LoopExit``).
    """

    @staticmethod
    def fake_monkey(*patched: str) -> object:
        class FakeMonkey:
            @staticmethod
            def is_module_patched(name: str) -> bool:
                return name in patched

        return FakeMonkey()

    def test_start_refuses_and_names_what_was_patched(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setitem(
            sys.modules, "gevent.monkey", self.fake_monkey("select", "socket")
        )
        engine = _trio_engine.TrioEngine(name="execnet-engine-patched")
        with pytest.raises(RuntimeError) as excinfo:
            engine.start()
        message = str(excinfo.value)
        assert "gevent has monkey-patched select, socket" in message
        assert "execnet.gevent" in message
        # refused before the thread exists, so there is nothing to join
        assert engine._thread is None

    def test_a_group_in_a_patched_process_fails_at_makegateway(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setitem(sys.modules, "gevent.monkey", self.fake_monkey("queue"))
        group = execnet.Group(
            engine=ProtocolEngine(name="execnet-engine-patched-group")
        )
        try:
            with pytest.raises(RuntimeError, match="monkey-patched queue"):
                group.makegateway("popen")
        finally:
            group.terminate(timeout=5.0)

    def test_patching_something_else_is_none_of_our_business(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setitem(sys.modules, "gevent.monkey", self.fake_monkey("ssl"))
        engine = ProtocolEngine(name="execnet-engine-unpatched")
        group = execnet.Group(engine=engine)
        try:
            assert (
                group.makegateway("popen")
                .remote_exec("channel.send(1)")
                .receive(TESTTIMEOUT)
                == 1
            )
        finally:
            group.terminate(timeout=5.0)
            engine.close()


class TestExplicitStart:
    """Starting is lazy, but not compulsory to leave that way.

    Everything that can go wrong with bringing a loop thread up otherwise
    goes wrong at an arbitrary later ``makegateway()``, in whatever code
    path happened to need the first gateway.
    """

    @staticmethod
    def threads(name: str) -> int:
        return engine_thread_names().count(name)

    def test_start_brings_the_thread_up_now(self) -> None:
        engine = ProtocolEngine(name="execnet-engine-explicit")
        assert self.threads("execnet-engine-explicit") == 0
        try:
            assert engine.start() is engine
            assert self.threads("execnet-engine-explicit") == 1
            # idempotent: no second thread
            engine.start()
            assert self.threads("execnet-engine-explicit") == 1
        finally:
            engine.close()
        assert self.threads("execnet-engine-explicit") == 0

    def test_entering_an_engine_starts_it(self) -> None:
        with ProtocolEngine(name="execnet-engine-entered") as engine:
            assert self.threads("execnet-engine-entered") == 1
            assert engine.running
        assert self.threads("execnet-engine-entered") == 0

    def test_start_is_where_a_broken_environment_shows_up(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # the point of asking early: this is the gevent refusal, raised at
        # startup instead of at whatever first needed a gateway
        monkeypatch.setitem(
            sys.modules,
            "gevent.monkey",
            TestGeventPatchedProcess.fake_monkey("socket"),
        )
        with pytest.raises(RuntimeError, match="monkey-patched socket"):
            ProtocolEngine(name="execnet-engine-start-fails").start()
