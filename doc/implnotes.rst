==============================================================================
Implementation notes
==============================================================================

How a gateway is actually built.  Everything here is internal: no name in
this document is part of the public API, and the on-wire protocol is
deliberately unversioned and unstandardised.

The Message protocol
----------------------

Both sides speak a stream of Messages: a 9-byte header (type, channel id,
payload length) followed by the payload.  ``execnet._message.FrameDecoder``
turns arbitrary byte chunks into Messages and is sans-IO -- it never reads,
writes or awaits -- so a receiver only has to stream bytes into it.  Payloads
carrying channel items are encoded by ``execnet._serialize``, which handles
builtin data plus channel references and nothing else (see
:ref:`serialization`).

No source shipping
----------------------

A worker is not sent its own source.  It is launched as a command that runs
the ``execnet`` (and ``trio``) installed in its own environment.  This is what
makes provisioning a separate concern from connecting, and it is an
invariant: nothing may reintroduce shipping the core over the wire.

Because the two ends are now installed independently, the worker checks the
coordinator version it is handed and refuses one whose major/minor differs
from its own -- the protocol is unversioned, so a skew has no defined
behaviour.  It refuses before it touches its stdio, which is the last moment
a reason can reach the user: after that the coordinator only ever learns EOF.
A patch-level difference is tolerated, and
``EXECNET_IGNORE_VERSION_SKEW=1`` in the worker's environment (which
``env:EXECNET_IGNORE_VERSION_SKEW=1`` in the spec reaches) downgrades the
refusal to a warning.

The launch contract: ``execnet worker``
----------------------------------------

Every launcher emits the same command line, so there is exactly one way a
worker starts::

    execnet worker  --protocol-stdio | --protocol-fd FD[,FD]
                    | --protocol-connect ADDR | --protocol-listen ADDR
                    | --protocol-share [--config-fd FD]
                    [--stdin/--stdout/--stderr DISPOSITION]

``ADDR`` is ``unix:/path`` or ``host:port``.  Provisioning emits ``python -m
execnet worker ...`` for a direct interpreter launch (where the console
script's location is not knowable) and ``execnet worker ...`` under ``uv
run``; both are the same CLI.

Which is to say: argv names a transport and nothing else.  What the worker
*is* -- its gateway id, worker profile, working directory, ``nice`` level,
``env:`` values, stdio disposition -- arrives as the first frame on that
transport, before the protocol proper begins:

.. code-block:: text

    coordinator --> GATEWAY_CONFIG  {"id": ..., "profile": ..., "env": {...}}
    worker      --> GATEWAY_CONFIG  {"ok": true, "execnet": ..., "pid": ...}
                or  GATEWAY_CONFIG  {"ok": false, "error": "version mismatch: ..."}

That keeps the config out of argv everywhere rather than only remotely --
``/proc`` is world-readable on the local machine exactly as ``ps`` is on a
remote one, and ``env:`` values are secrets often enough.  It also gives a
worker that refuses to serve somewhere to say so: the reason reaches
whoever asked for the gateway instead of a stderr that may be pointed
anywhere.  Do not regress either property.

The single exception is ``--protocol-share`` on Windows, where the socket
is duplicated into the worker with ``WSADuplicateSocket``: that blob
describes the very connection a config frame would arrive on, so it goes to
the worker's stdin as a one-key JSON object (``--config-fd``).  Nothing
else may travel that way.

An intermediary never sees a config it is only relaying: a ``via=``
coordinator is asked to *spawn* a sub-worker, and the sub's config comes
down the tunnel from the coordinator that wants the gateway.

Naming the transport explicitly is what frees the worker's stdio.  Once the
protocol has a stream of its own, fd 0/1/2 belong to the code the worker
runs, and the spec's ``stdin=``/``stdout=``/``stderr=`` keys say what to do
with them (defaults come from the transport: leave them alone for a socket,
close stdin and fold stdout onto stderr for stdio).  The matching CLI flags
override the config, for a worker started by hand.

Two more subcommands round it out: ``execnet server [HOST:PORT] [--once]``
accepts coordinator connections and hands each to a fresh worker (no code
runs in the server process itself), and ``execnet info`` prints version,
trio availability, executable, platform and supported transports as JSON --
which is how a coordinator decides whether a ``python=`` interpreter can
host a worker directly, *before* connecting to it.

Transports
----------------------

``transport=socket`` is the default for every worker execnet spawns; the
protocol only rides on stdin/stdout when asked to, or when nothing else can
work.

How the worker gets its protocol stream, by gateway:

``popen``, POSIX
    an inherited socketpair (``pass_fds``, ``--protocol-fd``)

``popen``, Windows
    a socket duplicated into the child pid with ``socket.share()``, the blob
    travelling on the child's stdin (``--protocol-share``)

``socket=`` / ``installvia=``
    the server accepts the connection, then hands that socket to the worker
    it spawns, by whichever of the two mechanisms the platform has.  The
    coordinator's config frame arrives on that same socket, so it reaches
    the worker directly: the server neither reads nor relays it, and has no
    configuration of its own to merge in.

``ssh=`` / ``vagrant_ssh=``
    an ``ssh -R`` forwarded unix socket the worker dials back on (POSIX
    only)

Windows has no ``pass_fds``, hence ``socket.share()`` (``WSADuplicateSocket``),
which duplicates into a *named pid* -- and the pid does not exist until the
child does, which is why the flag is in argv while the blob follows on
stdin.  The blob is bound to that one pid, so it is inert to anything else;
that beats handle inheritance, which would need ``close_fds=False`` and leak
every inheritable handle to the child and its grandchildren.

Hand a socket over **as a socket, never as an fd**.  Rebuilding one with
``socket.socket(fileno=fd)`` makes the constructor re-derive family, type and
proto by querying the handle, and PyPy on Windows cannot do that to a handle
produced by ``WSADuplicateSocket``.

ssh on Windows stays on stdio and cannot do otherwise: CPython has never
exposed ``AF_UNIX`` there and Win32-OpenSSH does not implement
``StreamLocal`` forwarding.  Asking for ``transport=socket`` anyway is an
error at ``makegateway`` time rather than a gateway that waits forever.

Whether a socket can be handed over at all is settled by *doing* it once --
sharing to our own pid and rebuilding the result -- not by looking for
``socket.share``: an implementation with the name but not a working call
would pass the check and fail later, at the point where the only thing left
to tell the coordinator is a closed socket.

Provisioning the worker environment
------------------------------------

``execnet._provision`` decides what command to run, by target:

* Same-interpreter ``popen`` -> ``sys.executable -m execnet worker``.
* A ``python=`` interpreter whose ``execnet info`` answers -> that
  interpreter directly, so ``sys.executable`` is preserved.
* A bare ``python=`` interpreter or an ``ssh`` remote -> uv_:
  ``uv run --with <req> execnet worker``, where ``<req>`` is
  ``execnet==<version>`` for a released coordinator and a locally built,
  version-cached wheel for a development one.  A dev coordinator's wheel is
  not on the remote filesystem, so it is copied over its own ssh connection
  and cached there by name before the worker is launched -- the protocol
  stream never carries a payload.
* ``EXECNET_PROVISION_WHEEL`` names a prebuilt wheel to use instead, which
  is how a built artifact gets tested by the suite that built it.

.. _uv: https://docs.astral.sh/uv/

The Trio host thread
----------------------

Protocol IO is a Trio program.  :mod:`execnet.trio` runs it in the caller's
own nursery; every other surface has no loop to put it on, so it runs on a
``Host``: one OS thread running ``trio.run``, shared per process
(``execnet._host.Host`` -> ``execnet._trio_host.TrioHost``).  ``execnet._host``
deliberately does not ``import trio``, so ``import execnet`` does not load the
event loop machinery.

Blocking calls cross into it through ``execnet._portal`` and park on a
wakener from ``execnet._boundary`` -- which is what makes
:mod:`execnet.gevent` possible: same host, same tasks, a different primitive
to park on.  Calling a blocking API from inside a running asyncio or trio
loop would stall that loop, so it raises instead.

Sends from a non-host thread wait until the frame is written, so an abrupt
``os._exit`` cannot drop queued data.  Sends from the host thread itself
(inside a receiver callback) only enqueue, to avoid deadlocking the writer
task.  ``setcallback`` runs its callback on a bounded thread pool rather than
on the loop: a consumer task per channel keeps that channel's order strict
while a slow callback blocks nothing but its own thread.  The pool is shared
by every channel in the process and bounded (``Host(callback_threads=...)``,
40 by default), so callbacks that wait on *each other* can fill it and stall
the rest; work that waits belongs on a thread of its own.

The host itself is not a resource that can be taken away quietly.  Closing
it is final, and a fork leaves every inherited object dead in the child --
both raise, because the alternative is a wait on a loop that will never run
again (``execnet._errors.ForkedResourceError``).  For the same reason
nothing scheduled with ``portal.post`` may raise: trio turns an exception in
an entry-queue callback into a ``TrioInternalError`` that ends the whole
run, so a call that loses a race with shutdown reports through its own
result object instead.

Deploying a project
----------------------

Workers on a host that shares no filesystem with the coordinator need the
project before they can run anything -- and because a worker *is* the
process that runs the tests, it has to already be inside the environment
the project was installed into.  So provisioning is its own gateway, and
the workers come after it::

    bootstrap = group.makegateway("ssh=host")
    target = execnet.Deployment(project=".", roots=["testing"]).deploy(bootstrap)
    bootstrap.exit()
    worker = group.makegateway(f"ssh=host//{target.spec}")

Three steps in the one order that works: a frozen environment
(``uv sync --frozen`` from the project's own lockfile, so the remote
resolves nothing), the artifact (a wheel built here and installed there,
rather than a source tree), and everything a test run needs that the wheel
does not contain -- tests, ``conftest.py``, fixture data -- rsynced into
the workspace.

Both halves travel over the gateway's own protocol: the transfer is
``RSync``, and the install is a ``GATEWAY_DEPLOY`` request the worker
serves.  No second connection and no second set of credentials, which is
what lets the same code reach a container or a pod.

:class:`execnet.Deployed` reports where things landed, because the remote
layout is a provisioning fact and the caller knows only local paths.
Deployments sharing a ``name`` share a workspace on the host, so the second
gateway to a machine finds the environment the first one built.

One trap worth knowing about, since a worker inherits its coordinator's
environment: ``uv pip install`` honours ``VIRTUAL_ENV``, and a coordinator
is very often running inside one.  The deploy service scrubs that (and
``UV_PROJECT_ENVIRONMENT``, ``CONDA_PREFIX``) and names the target
interpreter explicitly -- otherwise the project is installed into the
*coordinator's* environment and the deployed one silently lacks it.

Inside the worker
----------------------

The worker opens its transport with blocking IO, reads its config frame,
answers it, and only then builds a Trio loop and serves the Message
protocol on it.  The handshake happens before the loop deliberately: the
config is what decides the worker's *shape*, and ``profile=trio`` has no
side thread to read it on.  Where exec'd code
runs is the ``profile=`` axis (:ref:`worker profiles <worker-profiles>`),
implemented as an exec strategy per profile in ``execnet._trio_worker``:
``HybridExec`` (main thread while free, pool threads for overflow),
``GreenletExec`` (gevent hub on the main thread) and ``TaskExec`` (tasks on
the worker's own loop, for ``profile=trio``, which is the only profile whose
sources must be async).

Admission is FIFO and bounded (``execnet._trio_worker.exec_capacity``): the
thread-shaped strategies spend a thread per exec out of the same budget the
callback pool and the worker's internal ``to_thread`` work draw on, so exec
gets half of it and a request over the line is refused on its channel.  The
exec task itself contains whatever it raises -- it is a task on the worker's
*root* nursery, and an exception leaving it ends ``trio.run`` and prints an
ExceptionGroup onto the user's stderr.  That goes for every
``host.start_soon`` entry point; the socket and via handlers do the same.

Infrastructure that used to be expressed by ``remote_exec``-ing source is
now native protocol messages handled on the target's host:
``GATEWAY_START_SOCKET`` (``installvia=`` -- bind a one-shot listener and
reply with its address), ``GATEWAY_START_SUB`` (``via=`` -- spawn a
sub-worker and relay its protocol over the request channel) and
``GATEWAY_RSYNC`` (receive an rsync into a directory).  rsync was the last
thing execnet shipped its own source over the wire to do; as a service it
also claims no exec slot, and works against a ``profile=trio`` worker,
which rejects sync sources.  Its receiver body stays synchronous and runs
in a worker thread, reaching its channel through ``trio.from_thread`` --
rsync is file IO, and threading a loop through every ``lstat`` and
``chmod`` would buy nothing.  A sub-gateway
that fails to start must not take its coordinator down with it, and a
failure that cannot be reported must still close the connection, so the
requesting side sees EOF instead of waiting for a handshake reply nobody
will send.
