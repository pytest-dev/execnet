"""
code to initialize the remote side of a gateway once the io is created
"""
import os
import inspect
import execnet
from execnet import gateway_base
importdir = os.path.dirname(os.path.dirname(execnet.__file__))

def bootstrap_popen(io, spec):
    sendexec(io,
        "import sys",
        "sys.path.insert(0, %r)" % importdir,
        "from execnet.gateway_base import serve, init_popen_io",
        "sys.stdout.write('1')",
        "sys.stdout.flush()",
        "serve(init_popen_io(), id='%s-slave')" % spec.id,
    )
    s = io.read(1)
    assert s == "1".encode('ascii')


def bootstrap_ssh(io, spec):
    sendexec(io,
        inspect.getsource(gateway_base),
        'io = init_popen_io()',
        "io.write('1'.encode('ascii'))",
        "serve(io, id='%s-slave')" % spec.id,
    )
    s = io.read(1)
    assert s == "1".encode('ascii')
    
def bootstrap_socket(io, id):
    #XXX: switch to spec
    from execnet.gateway_socket import SocketIO

    sendexec(io,
        inspect.getsource(gateway_base),
        'import socket',
        inspect.getsource(SocketIO),
        "io = SocketIO(clientsock)",
        "io.write('1'.encode('ascii'))",
        "serve(io, id='%s-slave')" % id,
    )
    s = io.read(1)
    assert s == "1".encode('ascii')




def sendexec(io, *sources):
    source = "\n".join(sources)
    io.write((repr(source)+ "\n").encode('ascii'))

