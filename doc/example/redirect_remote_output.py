"""
redirect output from remote to a local function
showcasing features of the channel object:

- sending a channel over a channel
- adapting a channel to a file object
- setting a callback for receiving channel data

"""

import execnet

gw = execnet.makegateway()

outchan = gw.remote_exec(
    """
    import sys
    outchan = channel.gateway.newchannel()
    sys.stdout = outchan.makefile("w")
    channel.send(outchan)
"""
).receive()

# receive() promises this gateway's own channel type, so a plain isinstance
# is enough to get at the channel's methods
assert isinstance(outchan, execnet.Channel)


# note: callbacks execute in receiver thread!
def write(data):
    print("received:", repr(data))


outchan.setcallback(write)

gw.remote_exec(
    """
    print('hello world')
    print('remote execution ends')
"""
).waitclose()
