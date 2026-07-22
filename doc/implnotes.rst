gateway_base.py
----------------------

The code of this module is sent to the "other side"
as a means of bootstrapping a Gateway object
capable of receiving and executing code,
and routing data through channels.

Gateways operate on InputOutput objects offering
    a write and a read(n) method.

Once bootstrapped a higher level protocol
based on Messages is used.  Messages are serialized
to and from InputOutput objects.  The details of this protocol
are locally defined in this module.  There is no need
for standardizing or versioning the protocol.

Trio host-thread IO (popen / import bootstrap)
----------------------------------------------

For local same-interpreter ``popen`` gateways, Message protocol IO
runs inside a dedicated OS thread hosting a Trio event loop
(``execnet._trio_host.TrioHost``).  No source is sent over the wire:
the worker is launched as ``python -m execnet._trio_worker`` and
imports the installed ``execnet`` + ``trio`` (a rough major/minor
version check guards against an incompatible install):

* Coordinator: ``trio.lowlevel.open_process`` plus async framed
  reader/writer tasks per gateway (one host thread per ``Group``).
  It waits for the worker's ``b"1"`` handshake before starting the
  Message protocol.
* Worker: ``serve_popen_trio`` adopts stdio pipe fds into Trio
  streams and writes the handshake byte; ``remote_exec`` is
  scheduled from the Trio nursery (``trio.to_thread`` for ``thread``,
  main-thread handoff for ``main_thread_only``).

Sync ``Channel`` / ``Gateway`` APIs are unchanged.  Sends from
non-host threads wait until the frame is written (so abrupt
``os._exit`` cannot drop queued data).  Sends from the Trio host
thread (receiver callbacks) only enqueue, to avoid deadlocking
the writer task.

Disable with ``EXECNET_TRIO_HOST=0``.  Other gateway types
(``ssh``, ``socket``, ``via``, ``python=…``, greenlet execmodels)
still use the legacy thread receiver and sync ``Popen`` path.

Legacy thread model
-------------------

After bootstrapping, ``BaseGateway`` opens a receiver thread which
accepts encoded messages and triggers actions to interpret them.
Sending of channel data items happens directly through
write operations to InputOutput objects so there is no
separate send thread.

Code execution messages are scheduled on a WorkerPool.
On the worker, ``serve()`` integrates the main thread as the
primary executor when using the ``thread`` / ``main_thread_only``
models.

The receiver thread terminates if the remote side sends
a gateway termination message or if the IO-connection drops.
