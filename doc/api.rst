==============================================================================
Namespace reference
==============================================================================

One namespace per concurrency library you drive execnet *from*.  They all
speak the same protocol to the same kind of worker; see :doc:`basics` for
gateway specifications, channels and groups, which are common to all of
them.

.. _execnet-sync:

execnet.sync -- blocking
==============================================================================

.. module:: execnet.sync

The blocking API for plain threads, and the surface ``import execnet``
gives you: the top-level ``execnet.*`` names are aliases into this module.
It is what :doc:`basics` documents.

Calls block the calling thread while a Trio host thread does the protocol
IO, so calling one from inside a running asyncio or trio event loop raises
``RuntimeError`` rather than stalling every task on that loop.  Use
:mod:`execnet.aio` or :mod:`execnet.trio` there.

.. autoclass:: execnet.Host
   :members: start, running, close

.. autoclass:: execnet.Deployment
   :members: deploy

.. autoclass:: execnet.Deployed
   :members: spec, translate, workspace, python, paths


.. _execnet-trio:

execnet.trio -- trio-native
==============================================================================

.. automodule:: execnet.trio

.. autoclass:: execnet.trio.AsyncGroup
   :members: makegateway

.. autoclass:: execnet.trio.AsyncGateway
   :members: remote_exec, terminate

.. autoclass:: execnet.trio.AsyncChannel
   :members: send, receive, send_eof, aclose, wait_closed, isclosed

.. autofunction:: execnet.trio.open_gateway


.. _execnet-aio:

execnet.aio -- asyncio-native
==============================================================================

.. automodule:: execnet.aio

.. autoclass:: execnet.aio.AsyncGroup
   :members: start, aclose, makegateway, host

.. autoclass:: execnet.aio.AsyncGateway
   :members: remote_exec, terminate

.. autoclass:: execnet.aio.AsyncChannel
   :members: send, receive, send_eof, aclose, wait_closed, isclosed

.. autofunction:: execnet.aio.open_gateway


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
``Gateway``, ``RSync``, the error types) are the ones from
:mod:`execnet.sync`.

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

.. autoexception:: execnet.RemoteError
.. autoexception:: execnet.TimeoutError
.. autoexception:: execnet.HostNotFound
.. autoexception:: execnet.DataFormatError
.. autoexception:: execnet.DumpError
.. autoexception:: execnet.LoadError
