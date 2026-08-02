"""The transfer service and the seam it reaches workers through.

``execnet._deploy`` is meant to be liftable out of the core: it reaches a
worker through one generic ``GATEWAY_SERVICE`` request and a registry, and
nothing in the protocol core names it.  These tests cover both halves --
the registry as an extension point, and the transfer built on it.
"""

from __future__ import annotations

import os
import pathlib
import sys
from collections.abc import Callable
from collections.abc import Iterator
from typing import Any

import pytest
import trio

import execnet
import execnet.trio
from execnet import _services
from execnet._deploy._manifest import Entry
from execnet._deploy._manifest import walk

TESTTIMEOUT = 60.0

needssymlink = pytest.mark.skipif(
    not hasattr(os, "symlink"), reason="os.symlink not available"
)


@pytest.fixture
def tree(tmp_path: pathlib.Path) -> pathlib.Path:
    source = tmp_path / "source"
    (source / "sub").mkdir(parents=True)
    (source / "top.txt").write_text("top")
    (source / "sub" / "nested.txt").write_text("nested")
    (source / "sub" / "empty.txt").write_text("")
    return source


@pytest.fixture
def group() -> Iterator[execnet.Group]:
    group = execnet.Group()
    try:
        yield group
    finally:
        group.terminate(timeout=30.0)


class TestManifest:
    """Describing a tree is pure enough to test without a gateway."""

    def test_entries_are_relative_and_parents_first(self, tree) -> None:
        manifest = walk(str(tree))
        paths = [entry.path for entry in manifest.entries]
        assert paths == ["sub", "sub/empty.txt", "sub/nested.txt", "top.txt"]
        assert manifest.files()["top.txt"].size == 3

    def test_the_root_itself_is_never_filtered(self, tree) -> None:
        seen: list[str] = []

        def keep(path: str) -> bool:
            seen.append(path)
            return True

        walk(str(tree), keep)
        assert str(tree) not in seen

    def test_a_filter_excludes_whole_subtrees(self, tree) -> None:
        manifest = walk(str(tree), lambda path: not path.endswith("sub"))
        assert [entry.path for entry in manifest.entries] == ["top.txt"]

    def test_a_file_that_vanishes_mid_walk_is_left_out(self, tree) -> None:
        # a filter with side effects is a thing people write, and a walk
        # cannot hold a tree still
        def delete_as_we_go(path: str) -> bool:
            if path.endswith("nested.txt"):
                os.unlink(path)
            return True

        manifest = walk(str(tree), delete_as_we_go)
        assert "sub/nested.txt" not in [entry.path for entry in manifest.entries]

    @needssymlink
    def test_a_link_inside_the_tree_is_made_relative_to_it(self, tree) -> None:
        (tree / "sub" / "inside").symlink_to(tree / "top.txt")
        (tree / "outside").symlink_to(tree.parent / "elsewhere")
        entries = {entry.path: entry for entry in walk(str(tree)).entries}
        assert entries["sub/inside"] == Entry(
            "sub/inside",
            "link",
            entries["sub/inside"].mode,
            target="top.txt",
            internal=True,
        )
        # pointing out of the tree: copied as-is, wherever the tree lands
        assert entries["outside"].internal is False
        assert entries["outside"].target.endswith("elsewhere")


class TestServiceSeam:
    """The core reaches a service by name, and knows nothing else about it."""

    def test_the_core_does_not_name_any_feature(self) -> None:
        # the whole point: grep the protocol core for the features built on
        # it and find nothing
        core = pathlib.Path(execnet.__file__).parent
        sources = [
            (core / name).read_text()
            for name in (
                "_message.py",
                "_trio_gateway.py",
                "_gateway.py",
                "_trio_worker.py",
            )
        ]
        for text in sources:
            assert "rsync" not in text.lower()
            assert "deploy" not in text.lower()

    def test_an_unknown_service_is_refused_on_its_channel(self, group) -> None:
        # a worker that does not have a service the coordinator asked for is
        # usually a version skew, and should say so rather than go quiet
        gateway = group.makegateway("popen")
        reply = _request(gateway, "no.such.service", {})
        with pytest.raises(execnet.RemoteError, match="no execnet service"):
            reply()
        assert gateway.remote_exec("channel.send(1)").receive(TESTTIMEOUT) == 1

    def test_registering_is_how_a_service_is_added(self) -> None:
        _services.register("test.thing", "some.module:handler")
        # idempotent, so importing a registering module twice is fine
        _services.register("test.thing", "some.module:handler")
        with pytest.raises(ValueError, match="already registered"):
            _services.register("test.thing", "other.module:handler")
        del _services._REGISTRY["test.thing"]


def _request(
    gateway: execnet.Gateway, name: str, request: object
) -> Callable[[], Any]:
    """Make a raw service request from the blocking surface, for tests."""
    from execnet._deploy._facade import run_blocking

    async def run(targets: Any) -> Any:
        return await targets[0].request(name, request)

    def call() -> Any:
        return run_blocking([gateway], run)

    return call


class TestTransfer:
    def test_a_tree_arrives(self, tree, tmp_path, group) -> None:
        gateway = group.makegateway("popen")
        destination = tmp_path / "dest"
        execnet.transfer(gateway, tree, str(destination))
        assert (destination / "top.txt").read_text() == "top"
        assert (destination / "sub" / "nested.txt").read_text() == "nested"
        assert (destination / "sub" / "empty.txt").read_text() == ""

    def test_only_what_changed_is_sent_again(self, tree, tmp_path, group) -> None:
        gateway = group.makegateway("popen")
        destination = tmp_path / "dest"
        execnet.transfer(gateway, tree, str(destination))

        sent: list[str] = []
        execnet.transfer(
            gateway, tree, str(destination), progress=lambda path, size: sent.append(path)
        )
        assert sent == []

        (tree / "top.txt").write_text("changed")
        execnet.transfer(
            gateway, tree, str(destination), progress=lambda path, size: sent.append(path)
        )
        assert sent == ["top.txt"]
        assert (destination / "top.txt").read_text() == "changed"

    def test_same_size_different_mtime_is_settled_by_checksum(
        self, tree, tmp_path, group
    ) -> None:
        # the case a rebuild produces: the receiver hands back a digest and
        # the sender skips the body when it matches
        gateway = group.makegateway("popen")
        destination = tmp_path / "dest"
        execnet.transfer(gateway, tree, str(destination))
        os.utime(tree / "top.txt", (0, 0))

        sent: list[str] = []
        execnet.transfer(
            gateway, tree, str(destination), progress=lambda path, size: sent.append(path)
        )
        assert sent == []

    def test_a_big_file_is_chunked(self, tmp_path, group) -> None:
        from execnet._deploy._transfer import CHUNK_SIZE

        source = tmp_path / "source"
        source.mkdir()
        payload = os.urandom(CHUNK_SIZE * 2 + 17)
        (source / "big.bin").write_bytes(payload)
        gateway = group.makegateway("popen")
        destination = tmp_path / "dest"
        execnet.transfer(gateway, source, str(destination))
        assert (destination / "big.bin").read_bytes() == payload

    def test_delete_prunes_what_the_source_lost(self, tree, tmp_path, group) -> None:
        gateway = group.makegateway("popen")
        destination = tmp_path / "dest"
        execnet.transfer(gateway, tree, str(destination))
        (tree / "top.txt").unlink()

        execnet.transfer(gateway, tree, str(destination))
        assert (destination / "top.txt").exists()  # left alone by default
        execnet.transfer(gateway, tree, str(destination), delete=True)
        assert not (destination / "top.txt").exists()
        assert (destination / "sub" / "nested.txt").exists()

    @pytest.mark.skipif(sys.platform == "win32", reason="posix modes")
    def test_modes_survive(self, tree, tmp_path, group) -> None:
        (tree / "top.txt").chmod(0o640)
        (tree / "sub").chmod(0o750)
        gateway = group.makegateway("popen")
        destination = tmp_path / "dest"
        execnet.transfer(gateway, tree, str(destination))
        assert (destination / "top.txt").stat().st_mode & 0o777 == 0o640
        assert (destination / "sub").stat().st_mode & 0o777 == 0o750

    @needssymlink
    def test_links_are_rebuilt(self, tree, tmp_path, group) -> None:
        (tree / "inside").symlink_to(tree / "top.txt")
        gateway = group.makegateway("popen")
        destination = tmp_path / "dest"
        execnet.transfer(gateway, tree, str(destination))
        assert (destination / "inside").is_symlink()
        assert (destination / "inside").read_text() == "top"

    def test_it_works_against_a_worker_that_refuses_sync_sources(
        self, tree, tmp_path, group
    ) -> None:
        # profile=trio runs exec'd sources as tasks and rejects sync ones; a
        # service is not exec'd code, so it does not care
        gateway = group.makegateway("popen//profile=trio")
        destination = tmp_path / "dest"
        execnet.transfer(gateway, tree, str(destination))
        assert (destination / "top.txt").read_text() == "top"

    def test_a_failure_reports_on_its_channel(self, tree, tmp_path, group) -> None:
        gateway = group.makegateway("popen")
        with pytest.raises(execnet.RemoteError):
            execnet.transfer(gateway, tree, str(tmp_path / "dest" / "\0bad"))
        # and the gateway is still usable: the service is a task on the
        # worker's root nursery and has to contain what it raises
        assert gateway.remote_exec("channel.send(1)").receive(TESTTIMEOUT) == 1


class TestTrioSurface:
    def test_targets_are_transferred_to_concurrently(self, tree, tmp_path) -> None:
        # the reason the driver is async: N hosts should cost one transfer,
        # not N of them
        async def main() -> None:
            async with execnet.trio.AsyncGroup() as group:
                gateways = [await group.makegateway("popen") for _ in range(3)]
                destinations = [str(tmp_path / f"dest{n}") for n in range(3)]
                from execnet._deploy._transfer import transfer_tree_to_all
                from execnet._services import ServiceTarget

                await transfer_tree_to_all(
                    [
                        (ServiceTarget(gateway), destination)
                        for gateway, destination in zip(gateways, destinations)
                    ],
                    tree,
                )
                for destination in destinations:
                    assert (pathlib.Path(destination) / "top.txt").read_text() == "top"

        trio.run(main)

    def test_cancelling_a_transfer_leaves_the_gateway_usable(
        self, tmp_path
    ) -> None:
        source = tmp_path / "source"
        source.mkdir()
        for index in range(40):
            (source / f"file{index}.bin").write_bytes(os.urandom(200_000))

        async def main() -> None:
            async with execnet.trio.open_gateway("popen") as gateway:
                with trio.move_on_after(0.05):
                    await execnet.trio.transfer(
                        gateway, source, str(tmp_path / "dest")
                    )
                channel = await gateway.remote_exec("channel.send(1)")
                assert await channel.receive() == 1

        trio.run(main)
