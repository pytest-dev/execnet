import os


# contents of: remotecmd.py
def simple(arg):
    return arg + 1


def listdir(path):
    return os.listdir(path)


if __name__ == "__channelexec__":
    for item in channel:  # type: ignore[name-defined]
        channel.send(eval(item))  # type: ignore[name-defined]
