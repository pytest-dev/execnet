from execnet import gateway_base


def test_opcodes():
    data = vars(gateway_base.opcode)
    computed = dict((k, v) for k, v in data.items() if '__' not in k)
    assert computed == {
        'BUILDTUPLE': '@',
        'BYTES': 'A',
        'CHANNEL': 'B',
        'FALSE': 'C',
        'FLOAT': 'D',
        'FROZENSET': 'E',
        'INT': 'F',
        'LONG': 'G',
        'LONGINT': 'H',
        'LONGLONG': 'I',
        'NEWDICT': 'J',
        'NEWLIST': 'K',
        'NONE': 'L',
        'PY2STRING': 'M',
        'PY3STRING': 'N',
        'SET': 'O',
        'SETITEM': 'P',
        'STOP': 'Q',
        'TRUE': 'R',
        'UNICODE': 'S',

        # added in 1.4
        # causes a regression since it was ordered in
        # between CHANNEL and FALSE as "C" moving the other items
        'COMPLEX': 'T',
    }
