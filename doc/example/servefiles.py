# -*- coding: utf-8 -*-
# content of servefiles.py


def servefiles(channel):
    for fn in channel:
        f = open(fn, "rb")
        channel.send(f.read())
        f.close()


if __name__ == "__channelexec__":
    servefiles(channel)
