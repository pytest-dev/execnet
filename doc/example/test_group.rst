Managing multiple gateways and clusters
==================================================

Usings Groups for managing multiple gateways
------------------------------------------------------

Use ``execnet.Group`` to manage membership and lifetime of
multiple gateways::

    >>> import execnet
    >>> group = execnet.Group(['popen'] * 2)
    >>> len(group)
    2
    >>> group
    <Group ['gw0', 'gw1']>
    >>> list(group)
    [<Gateway id='gw0' receive-live, thread model, 0 active channels>, <Gateway id='gw1' receive-live, thread model, 0 active channels>]
    >>> 'gw0' in group and 'gw1' in group
    True
    >>> group['gw0'] == group[0]
    True
    >>> group['gw1'] == group[1]
    True
    >>> group.terminate() # exit all member gateways
    >>> group
    <Group []>

Assigning gateway IDs
------------------------------------------------------

All gateways are created as part of a group and receive
a per-group unique ``id`` after successful initialization.
Pass an ``id=MYNAME`` part to ``group.makegateway``. Example::

    >>> import execnet
    >>> group = execnet.Group()
    >>> gw = group.makegateway("popen//id=sub1")
    >>> assert gw.id == "sub1"
    >>> group['sub1']
    <Gateway id='sub1' receive-live, thread model, 0 active channels>

Getting (auto) IDs before instantiation
------------------------------------------------------

Sometimes it's useful to know the gateway ID ahead
of instantiating it::

    >>> import execnet
    >>> group = execnet.Group()
    >>> spec = execnet.XSpec("popen")
    >>> group.allocate_id(spec)
    >>> allocated_id = spec.id
    >>> gw = group.makegateway(spec)
    >>> assert gw.id == allocated_id

execnet.makegateway uses execnet.default_group
------------------------------------------------------

Each time you create a gateway with ``execnet.makegateway()``
you actually use the ``execnet.default_group``::

    >>> import execnet
    >>> gw = execnet.makegateway()
    >>> gw in execnet.default_group
    True
    >>> execnet.default_group.defaultspec # used for empty makegateway() calls
    'popen'

Robust termination of SSH/popen processes
-----------------------------------------------

Use ``group.terminate(timeout)`` if you want to terminate
member gateways and ensure that no local subprocesses remain.
You can specify a ``timeout`` after which an attempt at killing
the related process is made::

    >>> import execnet
    >>> group = execnet.Group()
    >>> gw = group.makegateway("popen//id=sleeper")
    >>> ch = gw.remote_exec("import time ; time.sleep(2.0)")
    >>> group
    <Group ['sleeper']>
    >>> group.terminate(timeout=1.0)
    >>> group
    <Group []>

execnet aims to provide totally robust termination so if
you have left-over processes or other termination issues
please :doc:`report them <../support>`.  Thanks!


Using Groups to manage a certain type of gateway
------------------------------------------------------

Set ``group.defaultspec`` to determine the default gateway
specification used by ``group.makegateway()``:

    >>> import execnet
    >>> group = execnet.Group()
    >>> group.defaultspec = "ssh=localhost//chdir=mytmp//nice=20"
    >>> gw = group.makegateway()
    >>> ch = gw.remote_exec("""
    ...      import os.path
    ...      basename = os.path.basename(os.getcwd())
    ...      channel.send(basename)
    ... """)
    >>> ch.receive()
    'mytmp'

This way a Group object becomes kind of a Gateway factory where
the factory-caller does not need to know the setup.
