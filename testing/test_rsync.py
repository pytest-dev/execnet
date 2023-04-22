from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import execnet
import pytest
from execnet import RSync
from test_serializer import _find_version


def readlink(path: str | Path):
    link = os.readlink(os.fspath(path))
    return Path(link)


skip_on_windows = pytest.mark.skipif(
    sys.platform == "win32" or getattr(os, "_name", "") == "nt",
    reason="broken on windows",
)


@pytest.fixture(scope="module")
def group(request):
    group = execnet.Group()
    request.addfinalizer(group.terminate)
    return group


@pytest.fixture(scope="module")
def gw1(request, group):
    gw = group.makegateway("popen//id=gw1")
    request.addfinalizer(gw.exit)
    return gw


@pytest.fixture(scope="module")
def gw2(request, group):
    gw = group.makegateway("popen//id=gw2")
    request.addfinalizer(gw.exit)
    return gw


class _dirs(argparse.Namespace):
    source: Path
    dest1: Path
    dest2: Path


@pytest.fixture
def dirs(request, tmp_path) -> _dirs:

    return _dirs(
        source=tmp_path / "source",
        dest1=tmp_path / "dest1",
        dest2=tmp_path / "dest2",
    )


class TestRSync:
    def test_notargets(self, dirs):
        rsync = RSync(dirs.source)
        with pytest.raises(IOError):
            rsync.send()
        assert rsync.send(raises=False) is None

    def test_dirsync(self, dirs, gw1, gw2):
        dest = dirs.dest1
        dest2 = dirs.dest2
        source = dirs.source
        source.joinpath("subdir").mkdir(parents=True)
        for s in ("content1", "content2", "content2-a-bit-longer"):
            source.joinpath("subdir", "file1").write_text(s)
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
        assert not dest.joinpath("subdir", "file1").is_file()
        assert dest2.joinpath("subdir", "file1").is_file()

    def test_dirsync_twice(self, dirs, gw1, gw2):
        source = dirs.source
        source.mkdir()
        source.joinpath("hello").touch()
        rsync = RSync(source)
        rsync.add_target(gw1, dirs.dest1)
        rsync.send()
        assert dirs.dest1.joinpath("hello").is_file()
        with pytest.raises(IOError):
            rsync.send()
        assert rsync.send(raises=False) is None
        rsync.add_target(gw1, dirs.dest2)
        rsync.send()
        assert dirs.dest2.joinpath("hello").is_file()
        with pytest.raises(IOError):
            rsync.send()
        assert rsync.send(raises=False) is None

    def test_rsync_default_reporting(self, capsys, dirs, gw1):
        source = dirs.source
        source.mkdir()
        source.joinpath("hello").touch()
        rsync = RSync(source)
        rsync.add_target(gw1, dirs.dest1)
        rsync.send()
        out, err = capsys.readouterr()
        assert out.find("hello") != -1

    def test_rsync_non_verbose(self, capsys, dirs, gw1):
        source = dirs.source
        source.mkdir()
        source.joinpath("hello").touch()
        rsync = RSync(source, verbose=False)
        rsync.add_target(gw1, dirs.dest1)
        rsync.send()
        out, err = capsys.readouterr()
        assert not out
        assert not err

    @skip_on_windows
    def test_permissions(self, dirs, gw1, gw2):
        source = dirs.source
        dest = dirs.dest1
        onedir = dirs.source.joinpath("one")
        onedir.mkdir(parents=True)
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
        os.utime(onefile, (-1, onefile_mtime))

        rsync = RSync(source)
        rsync.add_target(gw1, dest)
        rsync.send()

        mode = destfile.stat().st_mode
        assert mode & 511 == 448, mode
        mode = destdir.stat().st_mode
        assert mode & 511 == 504

    @skip_on_windows
    def test_read_only_directories(self, dirs, gw1):
        source = dirs.source
        dest = dirs.dest1
        source.joinpath("sub", "subsub").mkdir(parents=True)
        source.joinpath("sub").chmod(0o500)
        source.joinpath("sub", "subsub").chmod(0o500)

        # The destination directories should be created with the write
        # permission forced, to avoid raising an EACCES error.
        rsync = RSync(source)
        rsync.add_target(gw1, dest)
        rsync.send()

        assert dest.joinpath("sub").stat().st_mode & 0o700
        assert dest.joinpath("sub", "subsub").stat().st_mode & 0o700

    @skip_on_windows
    def test_symlink_rsync(self, dirs, gw1):

        source = dirs.source
        dest = dirs.dest1

        file_Path = Path("subdir", "existent")

        sourcefile = source.joinpath(file_Path)
        sourcefile.parent.mkdir(parents=True)
        sourcefile.touch()

        source.joinpath("rellink").symlink_to(file_Path)
        source.joinpath("abslink").symlink_to(sourcefile)

        rsync = RSync(source)
        rsync.add_target(gw1, dest)
        rsync.send()

        expected = dest.joinpath(file_Path)
        assert readlink(dest / "rellink") == file_Path
        assert readlink(dest / "abslink") == expected

    @skip_on_windows
    def test_symlink2_rsync(self, dirs, gw1):
        source = dirs.source
        dest = dirs.dest1
        subdir = dirs.source.joinpath("subdir")
        subdir.mkdir(parents=True)
        sourcefile = subdir.joinpath("somefile")
        sourcefile.touch()
        subdir.joinpath("link1").symlink_to("link2")
        subdir.joinpath("link2").symlink_to(sourcefile)
        subdir.joinpath("link3").symlink_to(
            source.parent,
            target_is_directory=True,
        )
        rsync = RSync(source)
        rsync.add_target(gw1, dest)
        rsync.send()
        expected = dest.joinpath(sourcefile.relative_to(dirs.source))
        destsub = dest.joinpath("subdir")
        assert destsub.exists()
        assert readlink(destsub / "link1") == Path("link2")
        # resolve for windows quirk
        assert readlink(destsub / "link2").resolve(strict=True) == expected
        assert readlink(destsub / "link3") == source.parent

    def test_callback(self, dirs, gw1):
        dest = dirs.dest1
        source = dirs.source
        source.mkdir()
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

    def test_file_disappearing(self, dirs, gw1):
        dest = dirs.dest1
        source = dirs.source
        source.mkdir()
        source.joinpath("ex").write_text("a" * 100)
        source.joinpath("ex2").write_text("a" * 100)

        class DRsync(RSync):
            def filter(self, x):
                assert x != source
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
