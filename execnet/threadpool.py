"""
dispatching execution to threads

(c) 2009, holger krekel
"""
import threading
try:
    import thread
except ImportError:
    import _thread as thread

import time
import sys

# py2/py3 compatibility
try:
    import queue
except ImportError:
    import Queue as queue
if sys.version_info >= (3,0):
    exec ("def reraise(cls, val, tb): raise val")
else:
    exec ("def reraise(cls, val, tb): raise cls, val, tb")

ERRORMARKER = object()

class Reply(object):
    """ reply instances provide access to the result
        of a function execution that got dispatched
        through WorkerPool.spawn()
    """
    _excinfo = None
    def __init__(self, task):
        self.task = task
        self._queue = queue.Queue()

    def _set(self, result):
        self._queue.put(result)

    def _setexcinfo(self, excinfo):
        self._excinfo = excinfo
        self._queue.put(ERRORMARKER)

    def get(self, timeout=None):
        """ get the result object from an asynchronous function execution.
            if the function execution raised an exception,
            then calling get() will reraise that exception
            including its traceback.
        """
        if self._queue is None:
            raise EOFError("reply has already been delivered")
        try:
            result = self._queue.get(timeout=timeout)
        except queue.Empty:
            raise IOError("timeout waiting for %r" %(self.task, ))
        if result is ERRORMARKER:
            self._queue = None
            excinfo = self._excinfo
            reraise(excinfo[0], excinfo[1], excinfo[2]) # noqa
        return result

class WorkerThread:
    def __init__(self, pool):
        self._queue = queue.Queue()
        self._pool = pool
        self._finishevent = threading.Event()

    def _run_once(self):
        reply = self._queue.get()
        if reply is SystemExit:
            return False
        assert self not in self._pool._ready
        task = reply.task
        try:
            func, args, kwargs = task
            result = func(*args, **kwargs)
        except (SystemExit, KeyboardInterrupt):
            return False
        except:
            reply._setexcinfo(sys.exc_info())
        else:
            reply._set(result)
        # at this point, reply, task and all other local variables go away
        return True

    def start(self):
        self.id = thread.start_new_thread(self.run, ())

    @property
    def dead(self):
        return self._finishevent.isSet()

    def run(self):
        try:
            while self._run_once():
                self._pool._ready.add(self)
        finally:
            try:
                self._pool._ready.remove(self)
            except KeyError:
                pass
            self._finishevent.set()

    def send(self, task):
        reply = Reply(task)
        self._queue.put(reply)
        return reply

    def stop(self):
        self._queue.put(SystemExit)

    def join(self, timeout=None):
        self._finishevent.wait(timeout)

class WorkerPool(object):
    """ A WorkerPool allows to spawn function executions
        to threads.  Each Worker Thread is reused for multiple
        function executions. The dispatching operation
        takes care to create and dispatch to existing
        threads.

        You need to call shutdown() to signal
        the WorkerThreads to terminate and join()
        in order to wait until all worker threads
        have terminated.
    """
    _shuttingdown = False
    def __init__(self, maxthreads=None):
        """ init WorkerPool instance which may
            create up to `maxthreads` worker threads.
        """
        self.maxthreads = maxthreads
        self._running = set()
        self._ready = set()

    def spawn(self, func, *args, **kwargs):
        """ return Reply object for the asynchronous dispatch
            of the given func(*args, **kwargs) in a
            separate worker thread.
        """
        if self._shuttingdown:
            raise IOError("WorkerPool is already shutting down")
        try:
            thread = self._ready.pop()
        except KeyError: # pop from empty list
            if self.maxthreads and len(self._running) >= self.maxthreads:
                raise IOError("maximum of %d threads are busy, "
                              "can't create more." %
                              (self.maxthreads,))
            thread = self._newthread()
        return thread.send((func, args, kwargs))

    def _newthread(self):
        thread = WorkerThread(self)
        thread.start()
        self._running.add(thread)
        return thread

    def shutdown(self):
        """ signal all worker threads to terminate.
            call join() to wait until all threads termination.
        """
        if not self._shuttingdown:
            self._shuttingdown = True
            for t in self._running:
                t.stop()

    def waitall(self, timeout=None):
        """ wait until all worker threads have terminated. """
        deadline = delta = None
        if timeout is not None:
            deadline = time.time() + timeout
        while self._running:
            thread = self._running.pop()
            if deadline:
                delta = deadline - time.time()
                if delta <= 0:
                    raise IOError("timeout while joining threads")
            thread.join(timeout=delta)
            if not thread.dead:
                raise IOError("timeout while joining thread %s" % thread.id)

if __name__ == '__channelexec__':
    maxthreads = channel.receive()  # noqa
    execpool = WorkerPool(maxthreads=maxthreads)
    gw = channel.gateway # noqa
    channel.send("ok") # noqa
    gw._trace("instantiated thread work pool maxthreads=%s" %(maxthreads,))
    while 1:
        gw._trace("waiting for new exec task")
        task = gw._execqueue.get()
        if task is None:
            gw._trace("thread-dispatcher got None, exiting")
            execpool.shutdown()
            execpool.waitall()
            raise gw._StopExecLoop
        gw._trace("dispatching exec task to thread pool")
        execpool.spawn(gw.executetask, task)
