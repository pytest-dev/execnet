"""
dispatching execution to threads

(c) 2009, holger krekel
"""
from __future__ import with_statement

import threading
try:
    import thread
except ImportError:
    import _thread as thread

import sys

if sys.version_info >= (3,0):
    exec ("def reraise(cls, val, tb): raise val")
else:
    exec ("def reraise(cls, val, tb): raise cls, val, tb")

def Event():
    event = threading.Event()
    if sys.version_info < (2,7):
        # patch wait function to return event state instead of None
        real_wait = event.wait
        def wait(timeout=None):
            real_wait(timeout=timeout)
            return event.isSet()
        event.wait = wait
    return event


class Reply(object):
    """ reply instances provide access to the result
        of a function execution that got dispatched
        through WorkerPool.spawn()
    """
    def __init__(self, task):
        self.task = task
        self._result_ready = Event()

    def get(self, timeout=None):
        """ get the result object from an asynchronous function execution.
            if the function execution raised an exception,
            then calling get() will reraise that exception
            including its traceback.
        """
        if not self._result_ready.wait(timeout):
            raise IOError("timeout waiting for %r" %(self.task, ))
        try:
            return self._result
        except AttributeError:
            reraise(*(self._excinfo[:3]))

    def run(self):
        func, args, kwargs = self.task
        try:
            try:
                self._result = func(*args, **kwargs)
            except:
                self._excinfo = sys.exc_info()
        finally:
            self._result_ready.set()

class EmptySemaphore:
    acquire = release = lambda self: None

class WorkerPool(object):
    """ A WorkerPool allows to spawn function executions
        to threads, returning a reply object on which you
        can ask for the result (and get exceptions reraised)
    """
    def __init__(self, size=None):
        """ by default allow unlimited number of spawns. """
        self._size = size
        self._running_lock = threading.Lock()
        if size is not None:
            self._sem = threading.Semaphore(size)
        else:
            self._sem = EmptySemaphore()
        self._running = set()

    def spawn(self, func, *args, **kwargs):
        """ return Reply object for the asynchronous dispatch
            of the given func(*args, **kwargs).
        """
        reply = Reply((func, args, kwargs))
        def run_and_release():
            reply.run()
            with self._running_lock:
                self._running.remove(reply)
                self._sem.release()
                if not self._running:
                    try:
                        self._waitall_event.set()
                    except AttributeError:
                        pass
        self._sem.acquire()
        with self._running_lock:
            self._running.add(reply)
            thread.start_new_thread(run_and_release, ())
        return reply

    def waitall(self, timeout=None):
        """ wait until all previosuly spawns have terminated. """
        with self._running_lock:
            if not self._running:
                return
            # if a Reply still runs, we let run_and_release
            # signal us -- note that we are still holding the
            # _running_lock to avoid race conditions
            self._waitall_event = Event()
        if not self._waitall_event.wait(timeout=timeout):
            raise IOError("waitall TIMEOUT, still running: %s" % (self._running,))


if __name__ == '__channelexec__':
    size = channel.receive()  # noqa
    execpool = WorkerPool(size)
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
