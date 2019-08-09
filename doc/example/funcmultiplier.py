# -*- coding: utf-8 -*-
import execnet


def multiplier(channel, factor):
    while not channel.isclosed():
        param = channel.receive()
        channel.send(param * factor)


gw = execnet.makegateway()
channel = gw.remote_exec(multiplier, factor=10)

for i in range(5):
    channel.send(i)
    result = channel.receive()
    assert result == i * 10

gw.exit()
