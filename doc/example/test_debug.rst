
Debugging execnet / wire messages
===============================================================

By setting the environment variable ``EXECNET_DEBUG`` you can
configure the execnet tracing mechanism:

:EXECNET_DEBUG=1:  write per-process trace-files to ``execnet-debug-PID`` in the system temp directory
:EXECNET_DEBUG=2:  perform tracing to stderr (popen-gateway workers will send this to their instantiator)

Here is a simple example to see what goes on with a simple execution::

    EXECNET_DEBUG=2  # or "set EXECNET_DEBUG=2" on windows

    python -c 'import execnet ; execnet.makegateway().remote_exec("42")'

which will show PID-prefixed trace entries -- the coordinator and its
worker write to the same stream, so their lines interleave::

    [3451876] creating workergateway on trio id='gw0-worker'
    [3451876] integrating as primary thread (trio worker)
    [3451876] gw0-worker received <Message CHANNEL_EXEC channel=1 lendata=16>
    [3451872] gw0 sent <Message CHANNEL_EXEC channel=1 lendata=16>
    [3451872] gw0 1 channel.__del__
    [3451872] === atexit cleanup <Group ['gw0']> ===
    [3451872] gw0 gateway.exit() called
    [3451872] gw0 --> sending GATEWAY_TERMINATE
    [3451876] gw0-worker received <Message CHANNEL_CLOSE channel=1 lendata=0>
    [3451872] gw0 sent <Message GATEWAY_TERMINATE channel=0 lendata=0>
    [3451872] gw0 --> io.close_write
    [3451876] gw0-worker execution starts[1]: '42'
    [3451876] gw0-worker execution finished
    [3451876] gw0-worker received <Message GATEWAY_TERMINATE channel=0 lendata=0>
    [3451876] gw0-worker received GATEWAY_TERMINATE
    [3451872] gw0 [trio-bridge] finishing channels
    [3451872] gw0 [trio-bridge] terminating execution
    [3451876] gw0-worker [trio-bridge] finishing channels
    [3451876] gw0-worker shutting down execution pool
    [3451876] gw0-worker waiting for receiver to finish
    [3451872] gw0 waiting for receiver to finish

Because the worker leaves its own stderr alone, a remote ``print()`` and a
remote traceback arrive the same way -- see :ref:`worker output
<worker-output>`.
