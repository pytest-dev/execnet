

# contents of: remotecmd.py
def simple(arg):
    return arg + 1

if __name__ == '__channelexec__':
    for item in channel:
        funcname = item[0]
        func = globals()[funcname]
        args = item[1:]
        result = func(*args)
        channel.send(result)

