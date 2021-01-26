# -*- coding: utf-8 -*-
import os
import subprocess
import sys
import tempfile

import execnet
import py
import pytest

MINOR_VERSIONS = {"3": "543210", "2": "76"}


def _find_version(suffix=""):
    name = "python" + suffix
    executable = py.path.local.sysfind(name)
    if executable is None:
        if sys.platform == "win32" and suffix == "3":
            for name in ("python31", "python30"):
                executable = py.path.local(r"c:\\{}\python.exe".format(name))
                if executable.check():
                    return executable
        for tail in MINOR_VERSIONS.get(suffix, ""):
            path = py.path.local.sysfind("{}.{}".format(name, tail))
            if path:
                return path

        else:
            py.test.skip("can't find a {!r} executable".format(name))
    return executable


TEMPDIR = _py2_wrapper = _py3_wrapper = None


def setup_module(mod):
    mod.TEMPDIR = py.path.local(tempfile.mkdtemp())
    if sys.version_info > (3, 0):
        mod._py3_wrapper = PythonWrapper(py.path.local(sys.executable))
        mod._py2_wrapper = PythonWrapper(_find_version("2"))
    else:
        mod._py3_wrapper = PythonWrapper(_find_version("3"))
        mod._py2_wrapper = PythonWrapper(py.path.local(sys.executable))


def teardown_module(mod):
    TEMPDIR.remove(True)


# we use the execnet folder in order to avoid tiggering a missing apipkg
pyimportdir = str(py.path.local(execnet.__file__).dirpath())


class PythonWrapper(object):
    def __init__(self, executable):
        self.executable = executable

    def dump(self, obj_rep):
        script_file = TEMPDIR.join("dump.py")
        script_file.write(
            """
import sys
sys.path.insert(0, %r)
import gateway_base as serializer
if sys.version_info > (3, 0): # Need binary output
    sys.stdout = sys.stdout.detach()
sys.stdout.write(serializer.dumps_internal(%s))
"""
            % (pyimportdir, obj_rep)
        )
        popen = subprocess.Popen(
            [str(self.executable), str(script_file)],
            stdin=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdout=subprocess.PIPE,
        )
        stdout, stderr = popen.communicate()
        ret = popen.returncode
        if ret:
            raise py.process.cmdexec.Error(
                ret, ret, str(self.executable), stdout, stderr
            )
        return stdout

    def load(self, data, option_args="__class__"):
        script_file = TEMPDIR.join("load.py")
        script_file.write(
            r"""
import sys
sys.path.insert(0, %r)
import gateway_base as serializer
if sys.version_info > (3, 0):
    sys.stdin = sys.stdin.detach()
loader = serializer.Unserializer(sys.stdin)
loader.%s
obj = loader.load()
sys.stdout.write(type(obj).__name__ + "\n")
sys.stdout.write(repr(obj))"""
            % (pyimportdir, option_args)
        )
        popen = subprocess.Popen(
            [str(self.executable), str(script_file)],
            stdin=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdout=subprocess.PIPE,
        )
        stdout, stderr = popen.communicate(data)
        ret = popen.returncode
        if ret:
            raise py.process.cmdexec.Error(
                ret, ret, str(self.executable), stdout, stderr
            )
        return [s.decode("ascii") for s in stdout.splitlines()]

    def __repr__(self):
        return "<PythonWrapper for {}>".format(self.executable)


@pytest.fixture
def py2(request):
    return _py2_wrapper


@pytest.fixture
def py3(request):
    return _py3_wrapper


@pytest.fixture(params=["py2", "py3"])
def dump(request):
    return request.getfixturevalue(request.param).dump


@pytest.fixture(params=["py2", "py3"])
def load(request):
    return request.getfixturevalue(request.param).load


simple_tests = [
    # type: expected before/after repr
    ("int", "4"),
    ("float", "3.25"),
    ("complex", "(1.78+3.25j)"),
    ("list", "[1, 2, 3]"),
    ("tuple", "(1, 2, 3)"),
    ("dict", "{(1, 2, 3): 32}"),
]


@py.test.mark.parametrize(["tp_name", "repr"], simple_tests)
def test_simple(tp_name, repr, dump, load):
    if (
        sys.platform.startswith("win")
        and os.environ.get("GITHUB_ACTIONS", "") == "true"
    ):
        pytest.skip(
            "GitHub Actions on Windows doesn't support Python 2 and 3 at the same time."
        )

    p = dump(repr)
    tp, v = load(p)
    assert tp == tp_name
    assert v == repr


def test_set(py2, py3, dump):
    p = dump("set((1, 2, 3))")
    tp, v = py2.load(p)
    assert tp == "set"
    # assert v == "set([1, 2, 3])" # ordering prevents this assertion
    assert v.startswith("set([") and v.endswith("])")
    assert "1" in v and "2" in v and "3" in v

    tp, v = py3.load(p)
    assert tp == "set"
    # assert v == "{1, 2, 3}" # ordering prevents this assertion
    assert v.startswith("{") and v.endswith("}")
    assert "1" in v and "2" in v and "3" in v
    p = dump("set()")
    tp, v = py2.load(p)
    assert tp == "set"
    assert v == "set([])"
    tp, v = py3.load(p)
    assert tp == "set"
    assert v == "set()"


def test_frozenset(py2, py3, dump):
    p = dump("frozenset((1, 2, 3))")
    tp, v = py2.load(p)
    assert tp == "frozenset"
    assert v == "frozenset([1, 2, 3])"
    tp, v = py3.load(p)
    assert tp == "frozenset"
    assert v == "frozenset({1, 2, 3})"
    p = dump("frozenset()")
    tp, v = py2.load(p)
    assert tp == "frozenset"
    assert v == "frozenset([])"
    tp, v = py3.load(p)
    assert tp == "frozenset"
    assert v == "frozenset()"


def test_long(py2, py3):
    really_big = "9223372036854775807324234"
    p = py2.dump(really_big)
    tp, v = py2.load(p)
    assert tp == "long"
    assert v == really_big + "L"
    tp, v = py3.load(p)
    assert tp == "int"
    assert v == really_big
    p = py3.dump(really_big)
    tp, v == py3.load(p)
    assert tp == "int"
    assert v == really_big
    tp, v = py2.load(p)
    assert tp == "long"
    assert v == really_big + "L"


def test_small_long(py2, py3):
    p = py2.dump("123L")
    tp, s = py2.load(p)
    assert s == "123L"
    tp, s = py3.load(p)
    assert s == "123"


def test_bytes(py2, py3):
    p = py3.dump("b'hi'")
    tp, v = py2.load(p)
    assert tp == "str"
    assert v == "'hi'"
    tp, v = py3.load(p)
    assert tp == "bytes"
    assert v == "b'hi'"


def test_str(py2, py3):
    p = py2.dump("'xyz'")
    tp, s = py2.load(p)
    assert tp == "str"
    assert s == "'xyz'"
    tp, s = py3.load(p, "py2str_as_py3str=True")
    assert tp == "str"
    assert s == "'xyz'"
    tp, s = py3.load(p, "py2str_as_py3str=False")
    assert s == "b'xyz'"
    assert tp == "bytes"


def test_unicode(py2, py3):
    p = py2.dump("u'hi'")
    tp, s = py2.load(p)
    assert tp == "unicode"
    assert s == "u'hi'"
    tp, s = py3.load(p)
    assert tp == "str"
    assert s == "'hi'"
    p = py3.dump("'hi'")
    tp, s = py3.load(p)
    assert tp == "str"
    assert s == "'hi'"
    tp, s = py2.load(p)
    # depends on unserialization defaults
    assert tp == "unicode"
    assert s == "u'hi'"


def test_bool(py2, py3):
    p = py2.dump("True")
    tp, s = py2.load(p)
    assert tp == "bool"
    assert s == "True"
    tp, s = py3.load(p)
    assert s == "True"
    assert tp == "bool"
    p = py2.dump("False")
    tp, s = py2.load(p)
    assert s == "False"


def test_none(dump, load):
    p = dump("None")
    tp, s = load(p)
    assert s == "None"


def test_tuple_nested_with_empty_in_between(py2):
    p = py2.dump("(1, (), 3)")
    tp, s = py2.load(p)
    assert tp == "tuple"
    assert s == "(1, (), 3)"
