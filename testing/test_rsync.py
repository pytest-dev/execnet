import os
import pathlib
import platform
import sys
import types

import pytest

import execnet
from execnet import Gateway
from execnet import RSync


@pytest.fixture(scope="module")
def group(request: pytest.FixtureRequest) -> execnet.Group:
    group = execnet.Group()
    request.addfinalizer(group.terminate)
    return group


@pytest.fixture(scope="module")
def gw1(request: pytest.FixtureRequest, group: execnet.Group) -> Gateway:
    gw = group.makegateway("popen//id=gw1")
    request.addfinalizer(gw.exit)
    return gw


@pytest.fixture(scope="module")
def gw2(request: pytest.FixtureRequest, group: execnet.Group) -> Gateway:
    gw = group.makegateway("popen//id=gw2")
    request.addfinalizer(gw.exit)
    return gw


needssymlink = pytest.mark.skipif(
    not hasattr(os, "symlink")
    or (platform.python_implementation() == "PyPy" and sys.platform == "win32"),
    reason="os.symlink not available",
)


class _dirs(types.SimpleNamespace):
    source: pathlib.Path
    dest1: pathlib.Path
    dest2: pathlib.Path


@pytest.fixture
def dirs(tmp_path: pathlib.Path) -> _dirs:
    dirs = _dirs(
        source=tmp_path / "source",
        dest1=tmp_path / "dest1",
        dest2=tmp_path / "dest2",
    )
    dirs.source.mkdir()
    dirs.dest1.mkdir()
    dirs.dest2.mkdir()
    return dirs


def are_paths_equal(path1: pathlib.Path, path2: pathlib.Path) -> bool:
    if os.path.__name__ == "ntpath":
        # On Windows, os.readlink returns an extended path (\\?\)
        # for absolute symlinks. However, extended does not compare
        # equal to non-extended, even when they refer to the same
        # path otherwise. So we have to fix it up ourselves...
        is_extended1 = str(path1).startswith("\\\\?\\")
        is_extended2 = str(path2).startswith("\\\\?\\")
        if is_extended1 and not is_extended2:
            path2 = pathlib.Path("\\\\?\\" + str(path2))
        if not is_extended1 and is_extended2:
            path1 = pathlib.Path("\\\\?\\" + str(path1))
    return path1 == path2


class TestRSync:
    def test_notargets(self, dirs: _dirs) -> None:
        rsync = RSync(dirs.source)
        with pytest.raises(IOError):
            rsync.send()
        assert rsync.send(raises=False) is None  # type: ignore[func-returns-value]

    def test_dirsync(self, dirs: _dirs, gw1: Gateway, gw2: Gateway) -> None:
        dest = dirs.dest1
        dest2 = dirs.dest2
        source = dirs.source

        for s in ("content1", "content2", "content2-a-bit-longer"):
            subdir = source / "subdir"
            subdir.mkdir(exist_ok=True)
            subdir.joinpath("file1").write_text(s)
            rsync = RSync(dirs.source)
            rsync.add_target(gw1, dest)
            rsync.add_target(gw2, dest2)
            rsync.send()
            assert dest.joinpath("subdir").is_dir()
            assert dest.joinpath("subdir", "file1").is_file()
            assert dest.joinpath("subdir", "file1").read_text() == s
            assert dest2.joinpath("subdir").is_dir()
            assert dest2.joinpath("subdir", "file1").is_file()
            assert dest2.joinpath("subdir", "file1").read_text() == s
            for x in dest, dest2:
                fn = x.joinpath("subdir", "file1")
                os.utime(fn, (0, 0))

        source.joinpath("subdir", "file1").unlink()
        rsync = RSync(source)
        rsync.add_target(gw2, dest2)
        rsync.add_target(gw1, dest)
        rsync.send()
        assert dest.joinpath("subdir", "file1").is_file()
        assert dest2.joinpath("subdir", "file1").is_file()
        rsync = RSync(source)
        rsync.add_target(gw1, dest, delete=True)
        rsync.add_target(gw2, dest2)
        rsync.send()
        assert not dest.joinpath("subdir", "file1").exists()
        assert dest2.joinpath("subdir", "file1").exists()

    def test_dirsync_twice(self, dirs: _dirs, gw1: Gateway, gw2: Gateway) -> None:
        source = dirs.source
        source.joinpath("hello").touch()
        rsync = RSync(source)
        rsync.add_target(gw1, dirs.dest1)
        rsync.send()
        assert dirs.dest1.joinpath("hello").exists()
        with pytest.raises(IOError):
            rsync.send()
        assert rsync.send(raises=False) is None  # type: ignore[func-returns-value]
        rsync.add_target(gw1, dirs.dest2)
        rsync.send()
        assert dirs.dest2.joinpath("hello").exists()
        with pytest.raises(IOError):
            rsync.send()
        assert rsync.send(raises=False) is None  # type: ignore[func-returns-value]

    def test_rsync_default_reporting(
        self, capsys: pytest.CaptureFixture[str], dirs: _dirs, gw1: Gateway
    ) -> None:
        source = dirs.source
        source.joinpath("hello").touch()
        rsync = RSync(source)
        rsync.add_target(gw1, dirs.dest1)
        rsync.send()
        out, _err = capsys.readouterr()
        assert out.find("hello") != -1

    def test_rsync_non_verbose(
        self, capsys: pytest.CaptureFixture[str], dirs: _dirs, gw1: Gateway
    ) -> None:
        source = dirs.source
        source.joinpath("hello").touch()
        rsync = RSync(source, verbose=False)
        rsync.add_target(gw1, dirs.dest1)
        rsync.send()
        out, err = capsys.readouterr()
        assert not out
        assert not err

    @pytest.mark.skipif(
        sys.platform == "win32" or getattr(os, "_name", "") == "nt",
        reason="irrelevant on windows",
    )
    def test_permissions(self, dirs: _dirs, gw1: Gateway, gw2: Gateway) -> None:
        source = dirs.source
        dest = dirs.dest1
        onedir = dirs.source / "one"
        onedir.mkdir()
        onedir.chmod(448)
        onefile = dirs.source / "file"
        onefile.touch()
        onefile.chmod(504)
        onefile_mtime = onefile.stat().st_mtime

        rsync = RSync(source)
        rsync.add_target(gw1, dest)
        rsync.send()

        destdir = dirs.dest1 / onedir.name
        destfile = dirs.dest1 / onefile.name
        assert destfile.stat().st_mode & 511 == 504
        mode = destdir.stat().st_mode
        assert mode & 511 == 448

        # transfer again with changed permissions
        onedir.chmod(504)
        onefile.chmod(448)
        os.utime(onefile, (onefile_mtime, onefile_mtime))

        rsync = RSync(source)
        rsync.add_target(gw1, dest)
        rsync.send()

        mode = destfile.stat().st_mode
        assert mode & 511 == 448, mode
        mode = destdir.stat().st_mode
        assert mode & 511 == 504

    @pytest.mark.skipif(
        sys.platform == "win32" or getattr(os, "_name", "") == "nt",
        reason="irrelevant on windows",
    )
    def test_read_only_directories(self, dirs: _dirs, gw1: Gateway) -> None:
        source = dirs.source
        dest = dirs.dest1
        sub = source / "sub"
        sub.mkdir()
        subsub = sub / "subsub"
        subsub.mkdir()
        sub.chmod(0o500)
        subsub.chmod(0o500)

        # The destination directories should be created with the write
        # permission forced, to avoid raising an EACCES error.
        rsync = RSync(source)
        rsync.add_target(gw1, dest)
        rsync.send()

        assert dest.joinpath("sub").stat().st_mode & 0o700
        assert dest.joinpath("sub", "subsub").stat().st_mode & 0o700

    @needssymlink
    def test_symlink_rsync(self, dirs: _dirs, gw1: Gateway) -> None:
        source = dirs.source
        dest = dirs.dest1
        subdir = dirs.source / "subdir"
        subdir.mkdir()
        sourcefile = subdir / "existent"
        sourcefile.touch()
        source.joinpath("rellink").symlink_to(sourcefile.relative_to(source))
        source.joinpath("abslink").symlink_to(sourcefile)

        rsync = RSync(source)
        rsync.add_target(gw1, dest)
        rsync.send()

        rellink = pathlib.Path(os.readlink(str(dest / "rellink")))
        assert rellink == pathlib.Path("subdir/existent")

        abslink = pathlib.Path(os.readlink(str(dest / "abslink")))
        expected = dest.joinpath(sourcefile.relative_to(source))
        assert are_paths_equal(abslink, expected)

    @needssymlink
    def test_symlink2_rsync(self, dirs: _dirs, gw1: Gateway) -> None:
        source = dirs.source
        dest = dirs.dest1
        subdir = dirs.source / "subdir"
        subdir.mkdir()
        sourcefile = subdir / "somefile"
        sourcefile.touch()
        subdir.joinpath("link1").symlink_to(
            subdir.joinpath("link2").relative_to(subdir)
        )
        subdir.joinpath("link2").symlink_to(sourcefile)
        subdir.joinpath("link3").symlink_to(source.parent)
        rsync = RSync(source)
        rsync.add_target(gw1, dest)
        rsync.send()
        expected = dest.joinpath(sourcefile.relative_to(dirs.source))
        destsub = dest.joinpath("subdir")
        assert destsub.exists()
        link1 = pathlib.Path(os.readlink(str(destsub / "link1")))
        assert are_paths_equal(link1, pathlib.Path("link2"))
        link2 = pathlib.Path(os.readlink(str(destsub / "link2")))
        assert are_paths_equal(link2, expected)
        link3 = pathlib.Path(os.readlink(str(destsub / "link3")))
        assert are_paths_equal(link3, source.parent)

    def test_callback(self, dirs: _dirs, gw1: Gateway) -> None:
        dest = dirs.dest1
        source = dirs.source
        source.joinpath("existent").write_text("a" * 100)
        source.joinpath("existant2").write_text("a" * 10)
        total = {}

        def callback(cmd, lgt, channel):
            total[(cmd, lgt)] = True

        rsync = RSync(source, callback=callback)
        # rsync = RSync()
        rsync.add_target(gw1, dest)
        rsync.send()

        assert total == {("list", 110): True, ("ack", 100): True, ("ack", 10): True}

    def test_file_disappearing(self, dirs: _dirs, gw1: Gateway) -> None:
        dest = dirs.dest1
        source = dirs.source
        source.joinpath("ex").write_text("a" * 100)
        source.joinpath("ex2").write_text("a" * 100)

        class DRsync(RSync):
            def filter(self, x: str) -> bool:
                assert x != str(source)
                if x.endswith("ex2"):
                    self.x = 1
                    source.joinpath("ex2").unlink()
                return True

        rsync = DRsync(source)
        rsync.add_target(gw1, dest)
        rsync.send()
        assert rsync.x == 1
        assert len(list(dest.iterdir())) == 1
        assert len(list(source.iterdir())) == 1


class TestRsyncIsAProtocolService:
    """rsync is served by the worker, not exec'd into it.

    Before 3.0 an rsync target was a ``remote_exec`` of the receiver's
    source: the last thing execnet shipped its own code over the wire to
    do, and one that spent an exec slot on infrastructure.
    """

    def test_it_works_against_a_worker_that_refuses_sync_sources(
        self, dirs: _dirs, group: execnet.Group
    ) -> None:
        # profile=trio runs exec'd sources as tasks and rejects sync ones,
        # so the old source-shipping receiver could not run there at all
        gateway = group.makegateway("popen//id=trio-rsync//profile=trio")
        (dirs.source / "hello.txt").write_text("hi")
        rsync = RSync(dirs.source, verbose=False)
        rsync.add_target(gateway, dirs.dest1)
        rsync.send()
        assert (dirs.dest1 / "hello.txt").read_text() == "hi"

    def test_it_claims_no_exec_slot(self, dirs: _dirs, gw1: Gateway) -> None:
        # infrastructure must not compete with the work a worker is for
        (dirs.source / "hello.txt").write_text("hi")
        rsync = RSync(dirs.source, verbose=False)
        rsync.add_target(gw1, dirs.dest1)
        rsync.send()
        assert gw1.remote_status().numexecuting == 0

    def test_a_failing_rsync_reports_on_its_channel(
        self, dirs: _dirs, gw1: Gateway
    ) -> None:
        # and does not take the worker down with it: the receiver is a task
        # on the worker's root nursery
        rsync = RSync(dirs.source, verbose=False)
        rsync.add_target(gw1, dirs.dest1 / "nested" / "\0bad")
        with pytest.raises(execnet.RemoteError):
            rsync.send()
        assert gw1.remote_exec("channel.send(1)").receive() == 1


class TestXdistContract:
    """What pytest-xdist actually does to RSync, as a local tripwire.

    ``xdist.workermanage.HostRSync`` subclasses ``execnet.RSync``, overrides
    ``filter`` and the *private* ``_report_send_file``, reads ``_sourcedir``
    and ``_verbose``, and calls ``add_target(gateway, relative_path,
    finishedcallback=..., delete=True)``.  Its own suite is the real
    tripwire and only runs in CI; this is the shape of it, here, so a
    reimplementation finds out before CI does.
    """

    class HostRSyncLike(RSync):
        """A stand-in for xdist's subclass, doing what it does."""

        def __init__(self, sourcedir, *, ignores=(), verbose=True) -> None:
            self._ignores = [str(item) for item in ignores]
            super().__init__(sourcedir=pathlib.Path(sourcedir), verbose=verbose)
            self.reported: list[str] = []

        def filter(self, path) -> bool:
            name = pathlib.Path(path).name
            return name not in self._ignores

        def add_target_host(self, gateway, finished=None) -> None:
            remotepath = os.path.basename(self._sourcedir)
            super().add_target(
                gateway, remotepath, finishedcallback=finished, delete=True
            )

        def _report_send_file(self, gateway, modified_rel_path) -> None:
            # xdist reads gateway.spec.chdir here -- so this must be handed
            # the sync Gateway facade, not anything from the async core
            if self._verbose > 0:
                path = os.path.basename(self._sourcedir) + "/" + modified_rel_path
                self.reported.append(f"{gateway.spec}:{gateway.spec.chdir} <= {path}")

    def test_the_xdist_shape_works(self, dirs: _dirs, tmp_path) -> None:
        source = dirs.source
        source.joinpath("keep.txt").write_text("keep")
        source.joinpath("skip.pyc").write_text("skip")
        source.joinpath("sub").mkdir()
        source.joinpath("sub", "nested.txt").write_text("nested")

        # a relative destination, resolved against the worker's chdir --
        # which is how xdist places a synced root
        workdir = tmp_path / "remote-cwd"
        workdir.mkdir()
        group = execnet.Group()
        try:
            gateway = group.makegateway(f"popen//chdir={workdir}")
            finished: list[bool] = []
            rsync = self.HostRSyncLike(source, ignores=["skip.pyc"])
            rsync.add_target_host(gateway, finished=lambda: finished.append(True))
            rsync.send()

            landed = workdir / source.name
            assert (landed / "keep.txt").read_text() == "keep"
            assert (landed / "sub" / "nested.txt").read_text() == "nested"
            assert not (landed / "skip.pyc").exists()
            assert finished == [True]
            assert any("keep.txt" in line for line in rsync.reported)
            assert all("skip.pyc" not in line for line in rsync.reported)
        finally:
            group.terminate(timeout=30.0)

    def test_delete_prunes_the_remote_root(self, dirs: _dirs, tmp_path) -> None:
        # xdist passes delete=True: a file removed locally must go remotely
        source = dirs.source
        source.joinpath("gone.txt").write_text("here for now")
        workdir = tmp_path / "remote-cwd"
        workdir.mkdir()
        group = execnet.Group()
        try:
            gateway = group.makegateway(f"popen//chdir={workdir}")
            rsync = self.HostRSyncLike(source, verbose=False)
            rsync.add_target_host(gateway)
            rsync.send()
            assert (workdir / source.name / "gone.txt").exists()

            source.joinpath("gone.txt").unlink()
            rsync = self.HostRSyncLike(source, verbose=False)
            rsync.add_target_host(gateway)
            rsync.send()
            assert not (workdir / source.name / "gone.txt").exists()
        finally:
            group.terminate(timeout=30.0)
