# -*- coding: utf-8 -*-
import execnet
import py
import pytest
from execnet import RSync
from test_serializer import _find_version


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


needssymlink = pytest.mark.skipif(
    not hasattr(py.path.local, "mksymlinkto"),
    reason="py.path.local has no mksymlinkto() on this platform",
)


@pytest.fixture
def dirs(request, tmpdir):
    t = tmpdir

    class dirs:
        source = t.join("source")
        dest1 = t.join("dest1")
        dest2 = t.join("dest2")

    return dirs


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

        for s in ("content1", "content2", "content2-a-bit-longer"):
            source.ensure("subdir", "file1").write(s)
            rsync = RSync(dirs.source)
            rsync.add_target(gw1, dest)
            rsync.add_target(gw2, dest2)
            rsync.send()
            assert dest.join("subdir").check(dir=1)
            assert dest.join("subdir", "file1").check(file=1)
            assert dest.join("subdir", "file1").read() == s
            assert dest2.join("subdir").check(dir=1)
            assert dest2.join("subdir", "file1").check(file=1)
            assert dest2.join("subdir", "file1").read() == s
            for x in dest, dest2:
                fn = x.join("subdir", "file1")
                fn.setmtime(0)

        source.join("subdir").remove("file1")
        rsync = RSync(source)
        rsync.add_target(gw2, dest2)
        rsync.add_target(gw1, dest)
        rsync.send()
        assert dest.join("subdir", "file1").check(file=1)
        assert dest2.join("subdir", "file1").check(file=1)
        rsync = RSync(source)
        rsync.add_target(gw1, dest, delete=True)
        rsync.add_target(gw2, dest2)
        rsync.send()
        assert not dest.join("subdir", "file1").check()
        assert dest2.join("subdir", "file1").check()

    def test_dirsync_twice(self, dirs, gw1, gw2):
        source = dirs.source
        source.ensure("hello")
        rsync = RSync(source)
        rsync.add_target(gw1, dirs.dest1)
        rsync.send()
        assert dirs.dest1.join("hello").check()
        with pytest.raises(IOError):
            rsync.send()
        assert rsync.send(raises=False) is None
        rsync.add_target(gw1, dirs.dest2)
        rsync.send()
        assert dirs.dest2.join("hello").check()
        with pytest.raises(IOError):
            rsync.send()
        assert rsync.send(raises=False) is None

    def test_rsync_default_reporting(self, capsys, dirs, gw1):
        source = dirs.source
        source.ensure("hello")
        rsync = RSync(source)
        rsync.add_target(gw1, dirs.dest1)
        rsync.send()
        out, err = capsys.readouterr()
        assert out.find("hello") != -1

    def test_rsync_non_verbose(self, capsys, dirs, gw1):
        source = dirs.source
        source.ensure("hello")
        rsync = RSync(source, verbose=False)
        rsync.add_target(gw1, dirs.dest1)
        rsync.send()
        out, err = capsys.readouterr()
        assert not out
        assert not err

    @py.test.mark.skipif("sys.platform == 'win32' or getattr(os, '_name', '') == 'nt'")
    def test_permissions(self, dirs, gw1, gw2):
        source = dirs.source
        dest = dirs.dest1
        onedir = dirs.source.ensure("one", dir=1)
        onedir.chmod(448)
        onefile = dirs.source.ensure("file")
        onefile.chmod(504)
        onefile_mtime = onefile.stat().mtime

        rsync = RSync(source)
        rsync.add_target(gw1, dest)
        rsync.send()

        destdir = dirs.dest1.join(onedir.basename)
        destfile = dirs.dest1.join(onefile.basename)
        assert destfile.stat().mode & 511 == 504
        mode = destdir.stat().mode
        assert mode & 511 == 448

        # transfer again with changed permissions
        onedir.chmod(504)
        onefile.chmod(448)
        onefile.setmtime(onefile_mtime)

        rsync = RSync(source)
        rsync.add_target(gw1, dest)
        rsync.send()

        mode = destfile.stat().mode
        assert mode & 511 == 448, mode
        mode = destdir.stat().mode
        assert mode & 511 == 504

    @needssymlink
    def test_symlink_rsync(self, dirs, gw1):
        source = dirs.source
        dest = dirs.dest1
        sourcefile = dirs.source.ensure("subdir", "existant")
        source.join("rellink").mksymlinkto(sourcefile, absolute=0)
        source.join("abslink").mksymlinkto(sourcefile)

        rsync = RSync(source)
        rsync.add_target(gw1, dest)
        rsync.send()

        expected = dest.join(sourcefile.relto(dirs.source))
        assert dest.join("rellink").readlink() == "subdir/existant"
        assert dest.join("abslink").readlink() == expected

    @needssymlink
    def test_symlink2_rsync(self, dirs, gw1):
        source = dirs.source
        dest = dirs.dest1
        subdir = dirs.source.ensure("subdir", dir=1)
        sourcefile = subdir.ensure("somefile")
        subdir.join("link1").mksymlinkto(subdir.join("link2"), absolute=0)
        subdir.join("link2").mksymlinkto(sourcefile, absolute=1)
        subdir.join("link3").mksymlinkto(source.dirpath(), absolute=1)
        rsync = RSync(source)
        rsync.add_target(gw1, dest)
        rsync.send()
        expected = dest.join(sourcefile.relto(dirs.source))
        destsub = dest.join("subdir")
        assert destsub.check()
        assert destsub.join("link1").readlink() == "link2"
        assert destsub.join("link2").readlink() == expected
        assert destsub.join("link3").readlink() == source.dirpath()

    def test_callback(self, dirs, gw1):
        dest = dirs.dest1
        source = dirs.source
        source.ensure("existant").write("a" * 100)
        source.ensure("existant2").write("a" * 10)
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
        source.ensure("ex").write("a" * 100)
        source.ensure("ex2").write("a" * 100)

        class DRsync(RSync):
            def filter(self, x):
                assert x != source
                if x.endswith("ex2"):
                    self.x = 1
                    source.join("ex2").remove()
                return True

        rsync = DRsync(source)
        rsync.add_target(gw1, dest)
        rsync.send()
        assert rsync.x == 1
        assert len(dest.listdir()) == 1
        assert len(source.listdir()) == 1

    @py.test.mark.skipif("sys.version_info >= (3,)")
    def test_2_to_3_bridge_can_send_binary_files(self, tmpdir, makegateway):
        python = _find_version("3")
        gw = makegateway("popen//python=%s" % python)
        source = tmpdir.ensure("source", dir=1)
        for i, content in enumerate("foo bar baz \x10foo"):
            source.join(str(i)).write(content)
        rsync = RSync(source)

        target = tmpdir.join("target")
        rsync.add_target(gw, target)
        rsync.send()
