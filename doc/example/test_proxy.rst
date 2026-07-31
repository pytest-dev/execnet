Managing proxied gateways
==========================

Simple proxying
----------------

Using the ``via`` arg of specs we can create a gateway
whose io is created on a remote gateway and proxied to the coordinator.

The simplest use case, is where one creates one coordinator process
and uses it to control new workers and their environment

::

    >>> import execnet
    >>> group = execnet.Group()
    >>> group.defaultspec = 'popen//via=coordinator'
    >>> coordinator = group.makegateway('popen//id=coordinator')
    >>> coordinator
    <Gateway id='coordinator' receive-live, thread profile, 0 active channels>
    >>> worker = group.makegateway()
    >>> worker
    <Gateway id='gw0' receive-live, thread profile, 0 active channels>
    >>> group
    <Group ['coordinator', 'gw0']>
