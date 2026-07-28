==============================================================================
API in a nutshell
==============================================================================

execnet ad-hoc instantiates local and remote Python interpreters.
Each interpreter is accessible through a **Gateway** which manages
code and data communication.  **Channels** allow to exchange
data between the local and the remote end.  **Groups**
help to manage creation and termination of sub-interpreters.

.. image:: _static/basic1.png

.. currentmodule:: execnet

Gateways: bootstrapping Python interpreters
===================================================

All Gateways are instantiated via a call to ``makegateway()``
passing it a gateway specification or URL.

.. _xspec:

.. autofunction:: execnet.makegateway(spec)

Here is an example which instantiates a simple Python subprocess::

    >>> gateway = execnet.makegateway()

Gateways allow to `remote execute code`_ and
`exchange data`_ bidirectionally.

Examples for valid gateway specifications
-------------------------------------------

* ``ssh=wyvern//python=python3.3//chdir=mycache`` specifies a Python3.3
  interpreter on the host ``wyvern``.  The remote process will have
  ``mycache`` as its current working directory.

* ``ssh=-p 5000 myhost`` makes execnet pass "-p 5000 myhost" arguments
  to the underlying ssh client binary, effectively specifying a custom port.

* ``vagrant_ssh=default`` makes execnet connect to a Vagrant VM named
  ``default`` via SSH through Vagrant's ``vagrant ssh`` command. It supports
  the same additional parameters as regular SSH connections.

* ``popen//python=python3.13//nice=20`` specification of
  a python subprocess using the ``python3.13`` executable which must be
  discoverable through the system ``PATH``; running with the lowest
  CPU priority ("nice" level).  By default current dir will be the
  current dir of the instantiator.

* ``popen//dont_write_bytecode`` uses the same executable as the current
  Python, and also passes the ``-B`` flag on startup, which tells Python not
  write ``.pyc`` or ``.pyo`` files.

* ``popen//env:NAME=value`` specifies a subprocess that uses the
  same interpreter as the one it is initiated from and additionally
  remotely sets an environment variable ``NAME`` to ``value``.

* ``socket=192.168.1.4:8888`` specifies a Python Socket server
  process that listens on ``192.168.1.4:8888``.  Such a server can be
  started with the ``execnet-socketserver`` console command, e.g. run
  anywhere with ``uvx --from execnet execnet-socketserver :8888``.

.. versionadded:: 1.5

* ``vagarant_ssh`` opens a python interpreter via the vagarant ssh command


.. _`remote execute code`:

remote_exec: execute source code remotely
===================================================

.. currentmodule:: execnet.gateway

All gateways offer a simple method to execute source code
in the instantiated subprocess-interpreter:

.. automethod:: Gateway.remote_exec(source)

It is allowed to pass a module object as source code
in which case its source code will be obtained and
get sent for remote execution.  ``remote_exec`` returns
a channel object whose symmetric counterpart channel
is available to the remotely executing source.


.. _`Channel`:
.. _`channel-api`:

.. _`exchange data`:

Channels: exchanging data with remote code
=======================================================

.. currentmodule:: execnet.gateway_base

A channel object allows to send and receive data between
two asynchronously running programs.

   .. automethod:: Channel.send(item)
   .. automethod:: Channel.receive(timeout)
   .. automethod:: Channel.setcallback(callback, endmarker=_NOENDMARKER)
   .. automethod:: Channel.makefile(mode, proxyclose=False)
   .. automethod:: Channel.close(error)
   .. automethod:: Channel.waitclose(timeout)
   .. autoattribute:: Channel.RemoteError
   .. autoattribute:: Channel.TimeoutError


.. _Group:

Grouped Gateways and robust termination
===============================================

.. currentmodule:: execnet.multi

All created gateway instances are part of a group.  If you
call ``execnet.makegateway`` it actually is forwarded to
the ``execnet.default_group``. Group objects are container
objects (see :doc:`group examples <example/test_group>`)
and manage the final termination procedure:

.. automethod:: Group.terminate(timeout=None)

This method is implicitly called for each gateway group at
process-exit, using a small timeout.  This is fine
for interactive sessions or random scripts which
you rather like to error out than hang.  If you start many
processes then you often want to call ``group.terminate()``
yourself and specify a larger or not timeout.


threading models: thread, main_thread_only
====================================================================

.. versionadded:: 1.2 (status: experimental!)

execnet supports "thread" and "main_thread_only" as thread models on
each of the two sides.  You need to decide which model to use before
you create any gateways::

    # content of threadmodel.py
    import execnet
    # locally use "thread", remotely use "main_thread_only" model
    execnet.set_execmodel("thread", "main_thread_only")
    gw = execnet.makegateway()
    print (gw)
    print (gw.remote_status())
    print (gw.remote_exec("channel.send(1)").receive())

You can execute this little test file::

    $ python threadmodel.py
    <Gateway id='gw0' receive-live, thread model, 0 active channels>
    <RInfo 'numchannels=0, numexecuting=0, execmodel=main_thread_only'>
    1

How to execute in the main thread
------------------------------------------------

When the remote side of a gateway uses the "thread" model, execution
will preferably run in the main thread.  This allows GUI loops
or other code to behave correctly.  If you, however, start multiple
executions concurrently, they will run in non-main threads.


remote_status: get low-level execution info
===================================================

.. currentmodule:: execnet.gateway

All gateways offer a simple method to obtain some status
information from the remote side.

.. automethod:: Gateway.remote_status(source)

Calling this method tells you e.g. how many execution
tasks are queued, how many are executing and how many
channels are active.

rsync: synchronise filesystem with remote
===============================================================

.. currentmodule:: execnet


``execnet`` implements a simple efficient rsyncing protocol.
Here is a basic example for using RSync::

    rsync = execnet.RSync('/tmp/source')
    gw = execnet.makegateway()
    rsync.add_target(gw, '/tmp/dest')
    rsync.send()


And here is API info about the RSync class.

.. autoclass:: RSync
    :members: add_target,send

Debugging execnet
===============================================================

By setting the environment variable ``EXECNET_DEBUG`` you can
configure a tracing mechanism:

:EXECNET_DEBUG=1:  write per-process trace-files to ``execnet-debug-PID``
:EXECNET_DEBUG=2:  perform tracing to stderr (popen-gateway workers will send this to their instantiator)


.. _`dumps/loads`:
.. _`dumps/loads API`:
.. _`serialization`:

Sending objects over a channel
=======================================================

A channel carries only **simple builtin data**: ``None``, ``bool``,
``int``, ``float``, ``complex``, ``bytes``, ``str`` and arbitrarily nested
``list`` / ``tuple`` / ``set`` / ``frozenset`` / ``dict`` of those -- plus
**channel references**, which arrive as channels on the peer.  That is the
entire contract.

execnet does **not** pickle and does **not** encode rich objects for you:
arbitrary instances, functions, ``datetime``, dataclasses, pydantic models,
numpy arrays, enums, etc. have no wire representation.  This is deliberate;
encoded / rich-object channels are out of scope for execnet.

Sending an unsupported value raises ``DumpError`` (a subclass of
``DataFormatError``); a corrupt or protocol-mismatched payload on receive
raises ``LoadError``.  These signal a **caller error to resolve** -- reduce
the value to simple data before sending -- not a transport failure.  The
standalone serializer itself is an internal implementation detail
(``execnet.gateway_base``) and is not part of the public API.

Encode rich objects yourself
-------------------------------------------------------

Turning a rich object into simple data (and back) is the caller's job.  Use
an established encoding mechanism rather than expecting the channel to do it:

- **pydantic**: ``model.model_dump(mode="json")`` reduces a model to simple
  data (``datetime`` -> ISO string, ``UUID`` / ``Enum`` / ``Decimal`` ->
  primitives); ``Model.model_validate(...)`` rebuilds it on the other side.
  ``TypeAdapter`` covers non-model types.

  ::

      channel.send(model.model_dump(mode="json"))
      # peer:
      model = MyModel.model_validate(channel.receive())

- **pytest** does exactly this above execnet: pytest-xdist ships
  ``TestReport`` objects with the ``pytest_report_to_serializable`` /
  ``pytest_report_from_serializable`` hooks (rich report <-> simple dict)
  around ``channel.send`` / ``channel.receive``.

- **stdlib**: ``dataclasses.asdict(obj)``, ``dt.isoformat()`` /
  ``datetime.fromisoformat``, or ``json`` with a ``default=`` hook.

Channels are the one non-builtin you *can* send: nested channel references
pass through intact, so callbacks and sub-streams need no encoding.
