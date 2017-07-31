Managing Proxyed gateways
==========================

Simple Proxying
----------------

Using the via arg of specs we can create a gateway
whose io os created on a remote gateway and
proxyed to the master.

The simlest use case, is where one creates one master process
and uses it to controll new slaves and their environment

::

    >>> import execnet
    >>> group = execnet.Group()
    >>> group.defaultspec = 'popen//via=master'
    >>> master = group.makegateway('popen//id=master')
    >>> master
    <Gateway id='master' receive-live, thread model, 0 active channels>
    >>> slave = group.makegateway()
    >>> slave
    <Gateway id='gw0' receive-live, thread model, 0 active channels>
    >>> group
    <Group ['master', 'gw0']>


