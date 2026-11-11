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


# expression evaluated by the dumping interpreter, loaded type name, loaded repr
roundtrip_expectations = [
    pytest.param("4", "int", "4", id="int"),
    pytest.param("-4", "int", "-4", id="negative-int"),
    pytest.param("2**31 - 1", "int", "2147483647", id="int32-max"),
    pytest.param("-(2**31)", "int", "-2147483648", id="int32-min"),
    pytest.param("2**31", "int", "2147483648", id="above-int32-max"),
    pytest.param("-(2**31) - 1", "int", "-2147483649", id="below-int32-min"),
    pytest.param(
        "9223372036854775807324234", "int", "9223372036854775807324234", id="big-int"
    ),
    pytest.param(
        "-9223372036854775807324234",
        "int",
        "-9223372036854775807324234",
        id="big-negative-int",
    ),
    pytest.param("3.25", "float", "3.25", id="float"),
    pytest.param("(1.78+3.25j)", "complex", "(1.78+3.25j)", id="complex"),
    pytest.param("True", "bool", "True", id="bool"),
    pytest.param("None", "NoneType", "None", id="none"),
    pytest.param("b'hi'", "bytes", "b'hi'", id="bytes"),
    pytest.param("'xyz'", "str", "'xyz'", id="str"),
    pytest.param("u'hi'", "str", "'hi'", id="unicode-prefix-str"),
    pytest.param("[1, 2, 3]", "list", "[1, 2, 3]", id="list"),
    pytest.param("(1, 2, 3)", "tuple", "(1, 2, 3)", id="tuple"),
    pytest.param("(1, (), 3)", "tuple", "(1, (), 3)", id="tuple-nested-empty"),
    pytest.param("{(1, 2, 3): 32}", "dict", "{(1, 2, 3): 32}", id="dict-tuple-key"),
    # one element only: multi-element set order is not part of the format
    pytest.param("{1}", "set", "{1}", id="set-single"),
    pytest.param("set()", "set", "set()", id="set-empty"),
    pytest.param(
        "frozenset((1, 2, 3))", "frozenset", "frozenset({1, 2, 3})", id="frozenset"
    ),
]


@pytest.mark.parametrize(["expr", "tp_name", "expected"], roundtrip_expectations)
def test_roundtrip(expr: str, tp_name: str, expected: str, dump, load) -> None:
    tp, v = load(dump(expr))
    assert tp == tp_name
    assert v == expected


def test_py2_string_loads() -> None:
    """Regression test for #267."""
    assert execnet.loads(b"\x02M\x00\x00\x00\x01aQ") == b"a"
