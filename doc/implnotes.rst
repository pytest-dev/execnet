gateway_base.py
----------------------

The code of this module is sent to the "other side"
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

Trio host-thread IO
-------------------

``popen`` and ``ssh`` gateways run their Message protocol IO inside a
dedicated OS thread hosting a Trio event loop
(``execnet._trio_host.TrioHost``).  No source is sent over the wire; the
worker is launched as ``python -m execnet._trio_worker`` and imports the
installed ``execnet`` + ``trio`` (a rough major/minor version check guards
against an incompatible install).  How the worker environment is obtained
depends on the target:

* Same-interpreter ``popen`` -> ``sys.executable -m execnet._trio_worker``.
* A ``python=`` interpreter that already has execnet -> that interpreter
  directly (so ``sys.executable`` is preserved).
* A bare ``python=`` interpreter or an ``ssh`` remote -> provisioned via
  ``uv`` (``execnet._provision``): ``uv run --with <req>`` where ``<req>``
  is ``execnet==<ver>`` for a released coordinator or a locally-built,
  version-cached wheel for a dev coordinator.  For an ``ssh`` remote on a
  dev coordinator the wheel is not on the remote filesystem, so the remote
  command is a POSIX-sh prelude that reads the wheel bytes from stdin
  (``head -c N`` into a temp dir) and ``exec``s ``uv`` against it; the
  coordinator streams those bytes before the Message protocol.

The worker configuration (id, execmodel, coordinator version) is passed as a
single JSON CLI argument (``_provision.worker_cli_arg``), so every launcher
shares one contract instead of scattered positional args.

Coordinator and worker roles:

* Coordinator: ``trio.lowlevel.open_process`` (directly, or wrapped in
  ``ssh``) plus async framed reader/writer tasks per gateway (one host
  thread per ``Group``).  It waits for the worker's ``b"1"`` handshake
  before starting the Message protocol.
* Worker: ``serve_popen_trio`` adopts stdio pipe fds into Trio streams and
  writes the handshake byte; ``remote_exec`` is scheduled from the Trio
  nursery (``trio.to_thread`` for ``thread``, main-thread handoff for
  ``main_thread_only``).

Sync ``Channel`` / ``Gateway`` APIs are unchanged.  Sends from non-host
threads wait until the frame is written (so abrupt ``os._exit`` cannot drop
queued data).  Sends from the Trio host thread (receiver callbacks) only
enqueue, to avoid deadlocking the writer task.

``socket`` and ``via`` gateways run on the Trio host too.  ``socket``
connects a Trio TCP stream to an ``execnet-socketserver`` (itself a Trio
listener spawning ``python -m execnet._trio_worker --socket-fd`` subprocess
workers).  Infrastructure that used to be driven by ``remote_exec``-ing
source is now expressed as native protocol messages handled on the target's
Trio host: ``GATEWAY_START_SOCKET`` (installvia -> bind a one-shot listener,
reply with its address) and ``GATEWAY_START_SUB`` (``via`` -> spawn a
sub-worker — popen, foreign python, ssh, or vagrant — and relay its protocol
over the request channel).

The Trio host is the only IO path; the legacy thread receiver and the
source-shipping bootstrap have been removed.  The receiver task terminates
when the remote side sends a gateway termination message or the
IO-connection drops.
