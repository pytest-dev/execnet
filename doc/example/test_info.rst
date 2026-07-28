Basic local and remote communication
====================================

Execute source code in subprocess, communicate through a channel
-------------------------------------------------------------------

You can instantiate a subprocess gateway, execute code
in it and bidirectionally send messages::

    >>> import execnet
    >>> gw = execnet.makegateway()
    >>> channel = gw.remote_exec("channel.send(channel.receive()+1)")
    >>> channel.send(1)
    >>> channel.receive()
    2

The initiating and the remote execution happen concurrently.
``channel.receive()`` operations return when input is available.
``channel.send(data)`` operations return when the message could
be delivered to the IO system.

The initiating and the "other" process work use a `share-nothing
model`_ and ``channel.send|receive`` are means to pass basic data
messages between two processes.

.. _`share-nothing model`: http://en.wikipedia.org/wiki/Shared_nothing_architecture

Remote-exec a function (avoiding inlined source part I)
-------------------------------------------------------

You can send and remote execute parametrized pure functions like this:

.. include:: funcmultiplier.py
    :literal:

The ``multiplier`` function executes remotely and establishes
a loop multipliying incoming data with a constant factor passed
in via keyword arguments to ``remote_exec``.

Notes:

* unfortunately, you can not type this example interactively because
  ``inspect.getsource(func)`` fails for interactively defined
  functions.

* You will get an explicit error if you try to execute non-pure
  functions, i.e. functions that access any global state (which
  will not be available remotely as we have a share-nothing model
  between the nodes).


Remote-exec a module (avoiding inlined source part II)
------------------------------------------------------

You can pass a module object to ``remote_exec`` in which case
its source code will be sent.  No dependencies will be transferred
so the module must be self-contained or only use modules that are
installed on the "other" side.   Module code can detect if it is
running in a remote_exec situation by checking for the special
``__name__`` attribute.

.. include:: remote1.py
    :literal:

You can now remote-execute the module like this::

    >>> import execnet, remote1
    >>> gw = execnet.makegateway()
    >>> ch = gw.remote_exec(remote1)
    >>> print (ch.receive())
    initialization complete

which will print the 'initialization complete' string.


Compare current working directories
----------------------------------------

A local subprocess gateway has the same working directory as the instantiatior::

    >>> import execnet, os
    >>> gw = execnet.makegateway()
    >>> ch = gw.remote_exec("import os; channel.send(os.getcwd())")
    >>> res = ch.receive()
    >>> assert res == os.getcwd()

"ssh" gateways default to the login home directory.

Get information from remote SSH account
---------------------------------------

Use simple execution to obtain information from remote environments::

  >>> import execnet, os
  >>> gw = execnet.makegateway("ssh=codespeak.net")
  >>> channel = gw.remote_exec("""
  ...     import sys, os
  ...     channel.send((sys.platform, tuple(sys.version_info), os.getpid()))
  ... """)
  >>> platform, version_info, remote_pid = channel.receive()
  >>> platform
  'linux2'
  >>> version_info
  (2, 6, 6, 'final', 0)

Use a callback instead of receive() and wait for completion
-------------------------------------------------------------

Set a channel callback to immediately react on incoming data::

    >>> import execnet
    >>> gw = execnet.makegateway()
    >>> channel = gw.remote_exec("for i in range(10): channel.send(i)")
    >>> l = []
    >>> channel.setcallback(l.append, endmarker=None)
    >>> channel.waitclose() # waits for closing, i.e. remote exec finish
    >>> l
    [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, None]

Note that the callback function will execute in the receiver thread
so it should not block on IO or long to execute.

Sending channels over channels
------------------------------------------------------

You can create and transfer a channel over an existing channel
and use it to transfer information::

    >>> import execnet
    >>> gw = execnet.makegateway()
    >>> channel = gw.remote_exec("""
    ...        ch1, ch2 = channel.receive()
    ...        ch2.send("world")
    ...        ch1.send("hello")
    ... """)
    >>> c1 = gw.newchannel()    # create new channel
    >>> c2 = gw.newchannel()    # create another channel
    >>> channel.send((c1, c2))  # send them over
    >>> c1.receive()
    'hello'
    >>> c2.receive()
    'world'



A simple command loop pattern
--------------------------------------------------------------

If you want the remote side to serve a number
of synchronous function calls into your module
you can setup a serving loop and write a local protocol.

.. include:: remotecmd.py
    :literal:

Then on the local side you can do::

    >>> import execnet, remotecmd
    >>> gw = execnet.makegateway()
    >>> ch = gw.remote_exec(remotecmd)
    >>> ch.send('simple(10)') # execute func-call remotely
    >>> ch.receive()
    11

Our remotecmd module starts up remote serving
through the ``for item in channel`` loop which
will terminate when the channel closes. It evaluates
all incoming requests in the global name space and
sends back the results.


Instantiate gateways through sockets
-----------------------------------------------------

In cases where you do not have SSH-access to a machine you need a
bootstrapping-point that listens on a socket.  execnet ships one as the
``execnet-socketserver`` console command; run it on the target machine::

    execnet-socketserver :8888   # bind to all IPs, port 8888

If execnet is not installed there, uv_ can fetch and run it in one step
without leaving anything behind::

    uvx --from execnet execnet-socketserver :8888

.. _uv: https://docs.astral.sh/uv/

The server accepts connections in a loop and serves each one in its own
worker subprocess; pass ``--once`` to serve a single connection and exit.
Passing port ``0`` binds an ephemeral port -- the bound address is printed
on the first line of output either way.

You can then instruct execnet on your local machine to bootstrap
itself into the remote socket endpoint::

    import execnet
    gw = execnet.makegateway("socket=TARGET-IP:8888")

That's it, you can now use the gateway object just like
a popen- or SSH-based one.

.. warning::

    The socket server performs no authentication and its traffic is not
    encrypted -- anyone who can reach the port can execute code on that
    machine.  Bind it to a trusted network only, or tunnel it over SSH.

Keeping the socket server running
+++++++++++++++++++++++++++++++++

``execnet-socketserver`` is an ordinary long-running process, so restarts and
boot-time startup are the job of your platform's service manager.

On Linux, a systemd unit does it::

    # /etc/systemd/system/execnet-socketserver.service
    [Unit]
    Description=execnet socket server

    [Service]
    ExecStart=/usr/local/bin/execnet-socketserver :8888
    Restart=always

    [Install]
    WantedBy=multi-user.target

On Windows, wrap the console command with a service host such as NSSM_ or
WinSW_.  Use ``where execnet-socketserver`` to find the installed
``execnet-socketserver.exe`` (it lives in the ``Scripts`` directory of the
environment it was installed into), then::

    nssm install ExecNetSocketServer C:\path\to\Scripts\execnet-socketserver.exe :8888
    nssm set ExecNetSocketServer Start SERVICE_AUTO_START
    net start ExecNetSocketServer

NSSM restarts the process if it exits.  Note that ``sc.exe create`` on its own
is not enough: it expects a binary that implements the Windows service control
protocol, which a plain console program does not.  If you would rather not
install a service host, a Task Scheduler task with an *At startup* trigger
works too.

.. _NSSM: https://nssm.cc/
.. _WinSW: https://github.com/winsw/winsw

.. include:: test_ssh_fileserver.rst
