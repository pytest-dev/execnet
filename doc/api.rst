==============================================================================
Namespace reference
==============================================================================

One namespace per concurrency library you drive execnet *from*.  They all
speak the same protocol to the same kind of worker; see :doc:`basics` for
gateway specifications, channels and groups, which are common to all of
them.

Four of them put protocol IO on a :class:`~execnet.ProtocolEngine` -- one
thread running a loop of its own, shared by the whole process -- and
differ only in how the caller waits for it: blocking the thread
(:mod:`execnet.sync`), parking a greenlet (:mod:`execnet.gevent`), or
awaiting on the caller's own loop (:mod:`execnet.trio`,
:mod:`execnet.aio`).  :mod:`execnet.raw_trio` is the exception: it has no
engine, and runs the gateways as tasks in your own nursery.  See
:ref:`trio-or-raw-trio` for what that buys and costs.

.. _execnet-sync:

execnet.sync -- blocking
==============================================================================

.. module:: execnet.sync

The blocking API for plain threads, and the surface ``import execnet``
gives you: the top-level ``execnet.*`` names are aliases into this module.
It is what :doc:`basics` documents.

Calls block the calling thread while a protocol engine does the protocol
IO, so calling one from inside a running asyncio or trio event loop raises
``RuntimeError`` rather than stalling every task on that loop.  Use
:mod:`execnet.aio` or :mod:`execnet.trio` there.

.. autoclass:: execnet.ProtocolEngine
   :members: start, running, terminate, close

Getting a project onto a host
------------------------------------------------------------------------------

A worker on a machine that shares no filesystem with the coordinator needs
the project before it can run anything -- and because a worker *is* the
process that runs the tests, it has to already be inside the environment
the project was installed into.  Provisioning therefore happens through a
gateway of its own, and the workers come afterwards -- usually spawned
*through* that same gateway, so there is one connection per machine::

    host = group.makegateway("ssh=host//id=h1")
    target = execnet.Deployment(".", roots=["testing"]).deploy(host)

    for index in range(4):
        group.makegateway(f"via=h1//{target.spec}//id=w{index}")

.. autofunction:: execnet.transfer

.. autoclass:: execnet.Deployment
   :members: deploy, deploy_all

.. autoclass:: execnet.Deployed
   :members: spec, translate, workspace, python, paths

:class:`execnet.RSync` still works and is what pytest-xdist uses, but it is
deprecated in favour of :func:`execnet.transfer`: it is now a thin adapter
over the same transfer, and only its optional ``callback`` behaves
differently (it is handed the gateway rather than a channel, and reports a
file when it is sent rather than when the far side confirms it).


.. _execnet-trio:

execnet.trio -- trio
==============================================================================

.. automodule:: execnet.trio

.. autoclass:: execnet.trio.AsyncGroup
   :members: start, aclose, makegateway, engine

.. autoclass:: execnet.trio.AsyncGateway
   :members: remote_exec, terminate

.. autoclass:: execnet.trio.AsyncChannel
   :members: send, receive, send_eof, aclose, wait_closed, isclosed

.. autofunction:: execnet.trio.open_gateway

Transfers and deployments are awaited here rather than blocking, and a
fan-out across gateways runs concurrently:

.. autofunction:: execnet.trio.transfer
.. autofunction:: execnet.trio.deploy
.. autofunction:: execnet.trio.deploy_all


.. _execnet-raw-trio:

execnet.raw_trio -- trio, without an engine
==============================================================================

.. automodule:: execnet.raw_trio

.. autoclass:: execnet.raw_trio.AsyncGroup
   :members: makegateway

.. autoclass:: execnet.raw_trio.AsyncGateway
   :members: remote_exec, terminate

.. autoclass:: execnet.raw_trio.AsyncChannel
   :members: send, receive, send_eof, aclose, wait_closed, isclosed

.. autofunction:: execnet.raw_trio.open_gateway
.. autofunction:: execnet.raw_trio.transfer
.. autofunction:: execnet.raw_trio.deploy
.. autofunction:: execnet.raw_trio.deploy_all

.. _trio-or-raw-trio:

Which trio surface
------------------------------------------------------------------------------

Both are trio and both are awaited in your own ``trio.run``.  The
difference is where the gateways live, and it is not a detail:

.. list-table::
   :header-rows: 1
   :widths: 22 39 39

   * -
     - ``execnet.raw_trio``
     - ``execnet.trio``
   * - Gateway lifetime
     - a task in *your* nursery; cannot outlive the ``async with`` that
       made it
     - owned by the engine; a handle you can store and close from
       anywhere
   * - Cancelling ``receive``
     - exact -- no item is ever taken and dropped
     - cancels the engine-side receive too, but an item taken in the
       window before it reaches you is lost
   * - ``shield``\ ed calls
     - not applicable -- there is nothing to shield across
     - ``send``, ``send_eof``, ``aclose``, ``terminate``: the wait is
       uncancellable and returns once done
   * - A stalled caller loop
     - stalls protocol IO for every gateway on it
     - the engine keeps reading
   * - An execnet failure
     - lands in your nursery and cancels its siblings
     - stays on the engine
   * - ``trio.to_thread`` budget
     - shared: a transfer's file reads compete with your own thread work
     - separate; execnet's threads are the engine's
   * - Cost per operation
     - a direct await
     - a hop to the engine and back, per call
   * - Other surfaces in the process
     - none: this run is the only place these gateways exist
     - one engine also serves ``sync``, ``gevent`` and ``aio``

Reach for ``execnet.raw_trio`` when execnet is most of what your loop does
and you want exact cancellation with no hop.  Reach for ``execnet.trio``
for an application that happens to use execnet -- which is also the one to
pick if you are not sure.


.. _execnet-aio:

execnet.aio -- asyncio-native
==============================================================================

.. automodule:: execnet.aio

.. autoclass:: execnet.aio.AsyncGroup
   :members: start, aclose, makegateway, engine

.. autoclass:: execnet.aio.AsyncGateway
   :members: remote_exec, terminate

.. autoclass:: execnet.aio.AsyncChannel
   :members: send, receive, send_eof, aclose, wait_closed, isclosed

.. autofunction:: execnet.aio.open_gateway
.. autofunction:: execnet.aio.transfer
.. autofunction:: execnet.aio.deploy
.. autofunction:: execnet.aio.deploy_all


.. _execnet-gevent:

execnet.gevent -- blocking, greenlet-parking
==============================================================================

.. module:: execnet.gevent

Identical to :mod:`execnet.sync` except that every blocking wait parks the
calling *greenlet* rather than its OS thread, so a slow ``receive`` no
longer stalls the whole hub::

    import execnet.gevent

    group = execnet.gevent.Group()
    gateway = group.makegateway("popen")
    channel = gateway.remote_exec("channel.send(6 * 7)")
    print(channel.receive())          # parks this greenlet, not the hub

Requires ``execnet[gevent]``.  ``Group``, ``default_group`` and
``makegateway`` are this module's own; the remaining names (``Channel``,
``Gateway``, ``RSync``, ``Deployment``, ``transfer``, the error types) are
the ones from :mod:`execnet.sync`.

Importing it monkey-patches nothing, and the process must not have
monkey-patched either: protocol IO is a Trio loop on its own OS thread and
needs the real ``select`` (for ``epoll``), ``socket``, ``thread`` and
``queue``, which ``gevent.monkey`` replaces process-wide.  You do not need
patching here -- the waits above park the calling greenlet because they
wait on a gevent primitive.  Starting a host in a patched process is
refused up front, with an error naming what was patched, rather than
failing later somewhere inside trio.

This is about the *caller*.  Whether the worker itself runs greenlets is
the independent ``profile=gevent`` spec key -- see
:ref:`worker profiles <worker-profiles>`.


Errors
==============================================================================

The same types are raised by every namespace, and are re-exported from each
of them.

.. autoexception:: execnet.ActiveGroupsWarning
.. autoexception:: execnet.RemoteError
.. autoexception:: execnet.TimeoutError
.. autoexception:: execnet.HostNotFound
.. autoexception:: execnet.DataFormatError
.. autoexception:: execnet.DumpError
.. autoexception:: execnet.LoadError
