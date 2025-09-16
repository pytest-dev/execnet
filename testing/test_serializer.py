from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

import execnet

# We use the execnet folder in order to avoid triggering a missing apipkg.
pyimportdir = os.fspath(Path(execnet.__file__).parent)


class PythonWrapper:
    def __init__(self, executable: str, tmp_path: Path) -> None:
        self.executable = executable
        self.tmp_path = tmp_path

    def dump(self, obj_rep: str) -> bytes:
        script_file = self.tmp_path.joinpath("dump.py")
        script_file.write_text(
            f"""
import sys
sys.path.insert(0, {pyimportdir!r})
import gateway_base as serializer
sys.stdout = sys.stdout.detach()
sys.stdout.write(serializer.dumps_internal({obj_rep}))
"""
        )
        res = subprocess.run(
            [str(self.executable), str(script_file)], capture_output=True, check=True
        )
        return res.stdout

    def load(self, data: bytes) -> list[str]:
        script_file = self.tmp_path.joinpath("load.py")
        script_file.write_text(
            rf"""
import sys
sys.path.insert(0, {pyimportdir!r})
import gateway_base as serializer
from io import BytesIO
data = {data!r}
io = BytesIO(data)
loader = serializer.Unserializer(io)
obj = loader.load()
sys.stdout.write(type(obj).__name__ + "\n")
sys.stdout.write(repr(obj))
"""
        )
        res = subprocess.run(
            [str(self.executable), str(script_file)],
            capture_output=True,
            check=True,
        )

        return res.stdout.decode("ascii").splitlines()

    def __repr__(self) -> str:
        return f"<PythonWrapper for {self.executable}>"


@pytest.fixture
def py3(tmp_path: Path) -> PythonWrapper:
    return PythonWrapper(sys.executable, tmp_path)


@pytest.fixture
def dump(py3: PythonWrapper):
    return py3.dump


@pytest.fixture
def load(py3: PythonWrapper):
    return py3.load


simple_tests = [
    # expected before/after repr
    ("int", "4"),
    ("float", "3.25"),
    ("complex", "(1.78+3.25j)"),
    ("list", "[1, 2, 3]"),
    ("tuple", "(1, 2, 3)"),
    ("dict", "{(1, 2, 3): 32}"),
]


@pytest.mark.parametrize(["tp_name", "repr"], simple_tests)
def test_simple(tp_name, repr, dump, load) -> None:
    p = dump(repr)
    tp, v = load(p)
    assert tp == tp_name
    assert v == repr


def test_set(load, dump) -> None:
    p = dump("set((1, 2, 3))")

    tp, v = load(p)
    assert tp == "set"
    # assert v == "{1, 2, 3}" # ordering prevents this assertion
    assert v.startswith("{") and v.endswith("}")
    assert "1" in v and "2" in v and "3" in v
    p = dump("set()")
    tp, v = load(p)
    assert tp == "set"
    assert v == "set()"


def test_frozenset(load, dump):
    p = dump("frozenset((1, 2, 3))")
    tp, v = load(p)
    assert tp == "frozenset"
    assert v == "frozenset({1, 2, 3})"


def test_long(load, dump) -> None:
    really_big = "9223372036854775807324234"
    p = dump(really_big)
    tp, v = load(p)
    assert tp == "int"
    assert v == really_big


def test_bytes(dump, load) -> None:
    p = dump("b'hi'")
    tp, v = load(p)
    assert tp == "bytes"
    assert v == "b'hi'"


def test_str(dump, load) -> None:
    p = dump("'xyz'")
    tp, s = load(p)
    assert tp == "str"
    assert s == "'xyz'"


def test_unicode(load, dump) -> None:
    p = dump("u'hi'")
    tp, s = load(p)
    assert tp == "str"
    assert s == "'hi'"


def test_bool(dump, load) -> None:
    p = dump("True")
    tp, s = load(p)
    assert s == "True"
    assert tp == "bool"


def test_none(dump, load) -> None:
    p = dump("None")
    _tp, s = load(p)
    assert s == "None"


def test_tuple_nested_with_empty_in_between(dump, load) -> None:
    p = dump("(1, (), 3)")
    tp, s = load(p)
    assert tp == "tuple"
    assert s == "(1, (), 3)"


def test_py2_string_loads() -> None:
    """Regression test for #267."""
    assert execnet.loads(b"\x02M\x00\x00\x00\x01aQ") == b"a"
