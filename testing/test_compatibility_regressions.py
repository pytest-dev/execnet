# -*- coding: utf-8 -*-
from execnet import gateway_base


def test_opcodes():
    data = vars(gateway_base.opcode)
    computed = {k: v for k, v in data.items() if "__" not in k}
    assert computed == {
        "BUILDTUPLE": "@".encode("ascii"),
        "BYTES": "A".encode("ascii"),
        "CHANNEL": "B".encode("ascii"),
        "FALSE": "C".encode("ascii"),
        "FLOAT": "D".encode("ascii"),
        "FROZENSET": "E".encode("ascii"),
        "INT": "F".encode("ascii"),
        "LONG": "G".encode("ascii"),
        "LONGINT": "H".encode("ascii"),
        "LONGLONG": "I".encode("ascii"),
        "NEWDICT": "J".encode("ascii"),
        "NEWLIST": "K".encode("ascii"),
        "NONE": "L".encode("ascii"),
        "PY2STRING": "M".encode("ascii"),
        "PY3STRING": "N".encode("ascii"),
        "SET": "O".encode("ascii"),
        "SETITEM": "P".encode("ascii"),
        "STOP": "Q".encode("ascii"),
        "TRUE": "R".encode("ascii"),
        "UNICODE": "S".encode("ascii"),
        # added in 1.4
        # causes a regression since it was ordered in
        # between CHANNEL and FALSE as "C" moving the other items
        "COMPLEX": "T".encode("ascii"),
    }
