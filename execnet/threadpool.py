"""
dispatching execution to threads or greenlets

(c) 2013, holger krekel
"""
from __future__ import with_statement

try:
    from execnet.gateway_base import get_execmodel, WorkerPool
except ImportError:
    from __main__ import get_execmodel, WorkerPool

if __name__ == '__channelexec__':
    size = channel.receive()  # noqa
    execpool = WorkerPool(get_execmodel("thread"), size)
    gw = channel.gateway # noqa
    channel.send("ok") # noqa
    gw._trace("instantiated thread work pool size=%s" %(size,))
    while 1:
        gw._trace("waiting for new exec task")
        task = gw._execqueue.get()
        if task is None:
            gw._trace("thread-dispatcher got None, exiting")
            execpool.waitall()
            raise gw._StopExecLoop
        gw._trace("dispatching exec task to thread pool")
        execpool.spawn(gw.executetask, task)
