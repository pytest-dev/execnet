import os
import pathlib
import platform
import sys
import types

import pytest

import execnet
from execnet import RSync
from execnet.gateway import Gateway


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
