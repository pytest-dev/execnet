# -*- coding: utf-8 -*-
import execnet

gw = execnet.makegateway("popen//python=python2")
channel = gw.remote_exec(
    """
    import numpy
    array = numpy.array([1,2,3])
    while 1:
        x = channel.receive()
        if x is None:
            break
        array = numpy.append(array, x)
    channel.send(repr(array))
"""
)
for x in range(10):
    channel.send(x)
channel.send(None)
print(channel.receive())
