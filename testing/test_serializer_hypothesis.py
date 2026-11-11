"""Property-based roundtrip tests for the execnet serializer."""

from __future__ import annotations

import struct
from typing import Any

from hypothesis import given
from hypothesis import strategies as st

import execnet

hashable_scalars = (
    st.none()
    | st.booleans()
    | st.integers()
    | st.floats()
    | st.complex_numbers()
    | st.text(max_size=20)
    | st.binary(max_size=20)
)

hashables = st.recursive(
    hashable_scalars,
    lambda children: (
        st.tuples(children, children) | st.frozensets(children, max_size=4)
    ),
    max_leaves=8,
)

values = st.recursive(
    hashables,
    lambda children: (
        st.lists(children, max_size=4)
        | st.lists(children, max_size=4).map(tuple)
        | st.dictionaries(hashables, children, max_size=4)
        | st.sets(hashables, max_size=4)
    ),
    max_leaves=16,
)


def canonical(obj: Any) -> object:
    """Map a value to a structure that compares equal only for identical input.

    Plain ``==`` is not enough for a roundtrip check: ``1 == 1.0 == True``,
    ``0.0 == -0.0`` and ``nan != nan``. Floats are compared by bit pattern
    and every node carries its exact type.
    """
    tp = type(obj).__name__
    if isinstance(obj, float):
        return (tp, struct.pack("!d", obj))
    if isinstance(obj, complex):
        return (tp, struct.pack("!dd", obj.real, obj.imag))
    if isinstance(obj, (list, tuple)):
        return (tp, [canonical(x) for x in obj])
    if isinstance(obj, dict):
        return (tp, [(canonical(k), canonical(v)) for k, v in obj.items()])
    if isinstance(obj, (set, frozenset)):
        return (tp, sorted(repr(canonical(x)) for x in obj))
    return (tp, obj)


@given(values)
def test_roundtrip(value: object) -> None:
    assert canonical(execnet.loads(execnet.dumps(value))) == canonical(value)


@given(st.integers())
def test_int_roundtrip(value: int) -> None:
    loaded = execnet.loads(execnet.dumps(value))
    assert type(loaded) is int
    assert loaded == value
