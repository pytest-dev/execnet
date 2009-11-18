
# contents of: remotecmd.py
def simple(arg):
    return arg + 1

if __name__ == '__channelexec__':
    for item in channel:
        channel.send(eval(item))
