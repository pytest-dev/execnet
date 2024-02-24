from execnet import gateway_base


def test_opcodes() -> None:
    data = vars(gateway_base.opcode)
    computed = {k: v for k, v in data.items() if "__" not in k}
    assert computed == {
        "BUILDTUPLE": b"@",
        "BYTES": b"A",
        "CHANNEL": b"B",
        "FALSE": b"C",
        "FLOAT": b"D",
        "FROZENSET": b"E",
        "INT": b"F",
        "LONG": b"G",
        "LONGINT": b"H",
        "LONGLONG": b"I",
        "NEWDICT": b"J",
        "NEWLIST": b"K",
        "NONE": b"L",
        "PY2STRING": b"M",
        "PY3STRING": b"N",
        "SET": b"O",
        "SETITEM": b"P",
        "STOP": b"Q",
        "TRUE": b"R",
        "UNICODE": b"S",
        # added in 1.4
        # causes a regression since it was ordered in
        # between CHANNEL and FALSE as "C" moving the other items
        "COMPLEX": b"T",
    }
