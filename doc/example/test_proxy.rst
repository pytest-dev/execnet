Managing proxied gateways
==========================

Simple proxying
----------------

Using the ``via`` arg of specs we can create a gateway
whose io is created on a remote gateway and proxied to the master.

The simplest use case, is where one creates one master process
and uses it to control new workers and their environment

::

    >>> import execnet
    >>> group = execnet.Group()
    >>> group.defaultspec = 'popen//via=master'
    >>> master = group.makegateway('popen//id=master')
    >>> master
    <Gateway id='master' receive-live, thread model, 0 active channels>
    >>> worker = group.makegateway()
    >>> worker
    <Gateway id='gw0' receive-live, thread model, 0 active channels>
    >>> group
    <Group ['master', 'gw0']>
