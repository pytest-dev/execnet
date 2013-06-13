
gateway_base.py
----------------------

the code of this module is sent to the "other side"
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

After bootstrapping the BaseGateway opens a receiver thread which
accepts encoded messages and triggers actions to interpret them.
Sending of channel data items happens directly through
write operations to InputOutput objects so there is no
separate thread.

Code execution messages are put into an execqueue from
which they will be taken for execution.  gateway.serve()
will take and execute such items, one by one.  This means
that by incoming default execution is single-threaded.

The receiver thread terminates if the remote side sends
a gateway termination message or if the IO-connection drops.
It puts an end symbol into the execqueue so
that serve() can cleanly finish as well.

