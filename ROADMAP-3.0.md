# Road to execnet 3.0

State and invariants live in `HANDOFF.md`; the landed record lives in
`handoff-history.md`.  This doc is what is *left*, and what the release is
for.

## Why 3.0, not 2.2

The branch was drafted as 2.2.  It is a major release:

- the launch contract changed — a worker is `execnet worker …`, and no
  source is bootstrapped over the wire;
- the default transport changed — the protocol is a socket, not the
  worker's stdin/stdout;
- a worker's stdio now belongs to the code it runs, so remote `print()`
  reaches the coordinator instead of the null device;
- `execnet.script.*`, `dump`/`load`/`loads` and the pre-Trio module names
  are gone or deprecated; a blocking call inside a running event loop now
  raises; a killed worker is uniformly `EOFError`.

Everything already in the tree that says "before execnet 3.0" (the
`execmodel=` → `profile=` rename, the worker config key) means this
release.

### What 3.0 is *not* allowed to break

**pytest-xdist keeps working, unmodified.**  That is the headline
compatibility goal, and it is stronger than "the sync API still exists":
the currently released xdist must drive an execnet 3.0 coordinator with
no changes on its side.  The deprecated shims therefore **ship in 3.0**.
They are removed later in the 3.x series, once the consumers that need
them have released a version that does not — not before.

## The xdist contract

Released pytest-xdist reaches into more of execnet than the documented
surface.  Everything here is load-bearing until xdist stops using it:

| what xdist does | where |
|---|---|
| `execnet.Group(execmodel="main_thread_only")` as a keyword | `workermanage.py` |
| prefixes specs with `execmodel=main_thread_only//`, **re-reading `spec.execmodel` to decide whether to prefix again** | `workermanage.py` |
| `execnet.gateway_base.ExecModel` → `RLock()`/`Event()` for the remote test queue | `remote.py` |
| `execnet.dumps` + `DumpError` to probe serializability, *inside pytest's warning-recording hook* | `remote.py` |
| subclasses `execnet.RSync`, touching `self._sourcedir` / `self._verbose` and overriding `filter` | `workermanage.py` |
| `gateway.spec.chdir`, and assigns `gateway.node` | `workermanage.py` |
| `execnet.makegateway("execmodel=main_thread_only//popen")` for looponfail | `looponfail.py` |

Two rules fall out of that list and are recorded as invariants in
`HANDOFF.md`: normalization must not rewrite a caller's spec, and nothing
in a worker may warn unboundedly.

**The tripwire is CI, not review.**  `.github/workflows/test.yml` runs
xdist's own suite against the branch in two variants: a pinned `release`
target (xdist `v3.8.0` + `pytest<9`) that blocks, and a floating
`default-branch` target that may fail.  Running it for the first time
found 16 real regressions our own suite could not see, because our suite
uses xdist as a *tool* and never exercises crash-replacement or report
serialization.  Bump the pin and the pytest pin together, and only after
re-checking the new pair against *released* execnet.

One known deselect (`.github/xdist-known-failures.txt`):
`test_remote_inner_argv` asserts `sys.argv == ["-c"]`, which the
no-source-shipping launch deliberately changed.  It needs an xdist PR.

## Before 3.0 ships

Ordered by how expensive they are to undo afterwards.

### 1. A neutral capability key in `execnet info` — the only irreversible one

`_provision.target_has_execnet()` decides whether a `python=` interpreter
can host a worker directly by asking whether `info["trio"]` is non-null.
That is a *cross-version* contract: a 3.0 coordinator will keep asking a
3.5 worker that question forever, and the answer names our engine.

Add `"worker": true` (or `"engines": ["trio"]`), have the coordinator
prefer it and fall back to `"trio"` only for a 2.x-vintage remote.  Keep
emitting `"trio"` indefinitely.  One line, and it buys the freedom to
answer honestly from an engine that is not Trio.

Settle the rest of the payload in the same pass, since it is the same
cross-version contract.  It is `execnet`, `trio`, `python`, `executable`,
`platform`, `protocols` today; `protocols` has no reader at all and does
not list `share`, so a Windows remote understates what it can do.  Either
give it a reader or say in the docs that it is informational.

### 2. Deprecated names out of `__all__`

`set_execmodel` is advertised as supported API by `execnet.__all__` and
`execnet.sync.__all__`.  Remove from both (it stays importable and
warning) and update `test_top_level_all_matches_sync_surface`.
`default_host` should leave `execnet.aio.__all__` too — it is exported
only from `aio`, which reads as an asyncio concept when it is the
opposite; isolation is `Host()`, sharing is the default.

### 3. Underscore the engine methods on `trio.AsyncGateway`

`execnet.trio.AsyncGateway` *is* `_trio_gateway.AsyncGateway`, so
`open_raw_channel` and `enqueue_frame` are public by accident — they are
the routing layer `_trio_host`/`_trio_worker` drive.  Underscore both
(every caller is ours), keep `open_channel` as the async `newchannel()`,
and add a namespace test pinning the public method set.

### 4. `execnet.gevent` and monkey-patching — decide what we claim

The facade works in a process that uses gevent *without* monkey-patching,
and its own promise holds there: blocking waits park the calling greenlet.
It does **not** work once `gevent.monkey` has patched the modules trio
reaches for from a side thread — verified in every variant:

| patched | where it dies |
|---|---|
| `patch_all()` | `select.epoll` is removed; trio's IO manager cannot be built |
| `patch_all(select=False)` | trio's wakeup socketpair is a gevent socket -> `EBADF` |
| `patch_all(thread=False, socket=False, select=False)` | `queue.SimpleQueue` is gevent's; `from_thread.run` -> `LoopExit` |

Which is a problem, because a real gevent application usually *does*
monkey-patch.  The failure is now immediate and names gevent
(`TrioHost.start` -> `_startup_hint`) rather than hanging for 30s, and the
docs no longer imply patching is fine, so nothing is silently broken.  But
"supported for gevent apps" is a bigger claim than "works if you drive
gevent explicitly", and only one of them is true today.

**Researched: can the host loop ignore the patches?**  For trio, only at a
price nobody should pay; for asyncio, it is already free.

Escaping gevent is harder than "use `monkey.get_original`", because the
originals are not self-contained:

- The saved `socket.socketpair` *is* the real function, but its body looks
  up `socket` in the module namespace it was defined in — which is the
  patched one — so it still returns gevent sockets.
- Holding a reference taken *before* patching does not help either:
  `gevent.monkey` sets `threading._CRLock = None` inside the real module,
  so a pre-patch `threading.RLock` starts returning the Python lock the
  moment the patch lands.
- Rebinding module globals inside trio (13 of them) misses everything
  captured when a class body ran — `attrs.Factory(threading.RLock)` in
  trio's entry queue is exactly that.
- And trio checks: `_entry_queue.task` asserts
  `self.lock.__class__.__module__ == "_thread"`, deliberately, because
  the alternative is "weird rare deadlocks".

What does work is a *private* stdlib: re-execute `threading`, `socket`,
`queue`, `selectors` and `subprocess` from source with their C
dependencies presented unpatched, install them in `sys.modules` while trio
imports, then restore.  Verified end to end — loop start, `from_thread`
run, `run_sync_soon`, `to_thread`, socketpair transport, `open_process`,
and the hub keeps ticking throughout.  The price is a second `threading`
in the process: the host thread does not appear in the application's
`threading.enumerate()`, `socket` identity splits in two (an
`isinstance(sock, socket.socket)` on a socket from user code no longer
means what it says, and `socket=`/`--protocol-share` take sockets from
user code), and interpreter-shutdown thread joining is split across two
registries.  That is a lot of hidden seam for one namespace.

**asyncio needs none of this.**  Where gevent *deletes* `select.epoll`, it
*replaces* `selectors.DefaultSelector` with a hub-backed one — so an
asyncio loop in a monkey-patched process gets a working selector, patched
sockets work in whichever hub they land in, and asyncio asserts nothing
about primitive identity.  Verified on a fully patched process, all six
paths execnet needs (call in, post, executor callbacks, socket IO,
`create_subprocess_exec`, hub not stalled), in **both** shapes: the loop
on a real OS thread, and the loop as a *greenlet on the application's own
hub* — no host thread at all, which is the shape `execnet.gevent` would
want anyway.

So the options are now: keep the honest limitation and document it (where
we are); adopt the private-stdlib trick and own its seams; or note that
"a non-Trio engine" below is not only an internals port — it is also what
makes `execnet.gevent` work in the environment gevent users actually have.
Decide before 3.0, because it is what the namespace promises.

## What pins us to Trio

Two futures get conflated and have different answers:

- **A non-Trio engine** — the host thread runs `asyncio.run`, or execnet
  drops the hard `trio` dependency.  This is an internals port; the
  invariant that protects it is already recorded (neutral `ByteStream`,
  sans-IO `FrameDecoder`).  Tripwire: `aio.py` imports trio at module
  level for `trio.CancelScope`, so `execnet.aio.trio` is the trio module
  and an asyncio-only install could not import `execnet.aio` today.

  It has a second payoff, measured rather than assumed (see the gevent
  item above): asyncio runs *unmodified* in a monkey-patched gevent
  process where trio cannot, on a thread or as a greenlet.  An asyncio
  engine would hand `execnet.gevent` the environment its users actually
  have, and could drop the host thread there entirely.
- **A native asyncio surface** — `execnet.aio` running gateways on the
  *caller's* loop with no host thread, symmetric with `execnet.trio`.
  This one is visible in the API: `aio.AsyncGroup(host=)` and
  `aio.AsyncGroup.host` would become meaningless.  `host=` is honest
  today and can be deprecated later behind its `None` default; document
  it as "which host thread serves this group", i.e. an
  implementation-shaped knob rather than part of the asyncio model.

`Host` itself is already engine-neutral in name and members; only its
docstring says Trio, which is accurate and would be a docs change.

## Provisioning and workspaces: what the next xdist should stand on

3.0's second goal is that the *next* pytest-xdist can stop hand-rolling
deployment.  Today xdist does it itself, crudely:

- one `execnet.Group`, specs prefixed by hand;
- `HostRSync` pushes each rsync root to `basename(root)` under the
  gateway's chdir;
- `make_reltoroot()` rewrites command-line args to `root.name + "/" + rel`
  and raises if an arg is not under a root;
- the remote interpreter is assumed to already have the project under test
  installed — which is why remote xdist is mostly used against a shared
  filesystem.

What execnet should own instead, so xdist's version becomes a few calls:

1. **Provision the environment, not just execnet.**  `_provision.py`
   already builds, caches and ships a wheel of a dev-version *execnet*
   (`provisioning_wheel`, `wheel_delivery_command`, remote cache under
   `"$HOME"/.cache/execnet/wheels`) and launches under `uv run --with`.
   Generalize that to arbitrary requirements, including "a wheel of the
   project I am running from" — that is the same code path with a
   different source directory.  Wanted: spec keys along the lines of
   `with=<requirement>` (repeatable) and `project=<path>`.
2. **Deploy a workspace, and hand back the mapping.**  One object that
   rsyncs a set of roots into a remote workspace directory and returns a
   local→remote path mapping, so the caller can translate paths in its own
   config instead of reconstructing the layout by convention.  The
   translation is execnet's to give because the remote root is a
   provisioning fact — the caller only knows local paths.
3. **rsync as a first-class operation.**  `RSync` still works by
   `remote_exec`-ing `_rsync_remote`'s source and tunnelling data as
   serialized channel messages.  Since execnet is now always *installed*
   on the worker, this should follow the pattern the branch established
   for `GATEWAY_START_SUB` and friends: a protocol-level request served by
   the worker, with the data plane on a raw channel.  That also gets the
   async surfaces an `RSync`, which they lack.

Open questions, none decided:

- Does the workspace API live on `Group` (one deploy fans out to every
  gateway, like `HostRSync.add_target_host` does per gateway) or is it a
  standalone object gateways are added to, as `RSync` is today?
- Is the mapping a dumb prefix swap, or does it need to survive symlinks,
  case-insensitive remotes, and roots that overlap?
- What is the remote workspace root — a per-gateway temp dir, a
  content-addressed cache keyed like the wheel cache, or caller-supplied?
  Reuse across gateways on the same host is the whole point on a cluster.
- Cleanup: who deletes a workspace, and what happens when the coordinator
  dies without terminating.
- How much of this is execnet's job at all versus a thin xdist layer over
  points 1 and 3.  Answer this one first.

Doing this well is also what makes the Kubernetes goal tractable — a pod
is just a remote with no shared filesystem and a short life.

## Kubernetes: test runs in a cluster over the protocol

The goal is to drive a test run across pods in a Kubernetes cluster using
execnet's protocol, rather than a bespoke agent.  Nothing here is built;
the design space, and what the branch already provides:

**Getting a stream to a pod.**  Three shapes, roughly in order of cost:

1. *A command transport.*  Generalize the ssh launcher to any argv that
   yields a process whose stdio is the worker's protocol — e.g.
   `kubectl exec -i POD -- execnet worker --protocol-stdio`.  Cheapest,
   works with any cluster access that `kubectl` has, and needs no new
   code in the worker.  Costs the worker's stdio and offers no dial-back.
2. *Listen plus a proxy.*  The worker runs
   `execnet worker --protocol-listen 0.0.0.0:0` and reports its address;
   the coordinator connects through a `kubectl port-forward` (or directly,
   where pod IPs are routable).  This is precisely what `--protocol-listen`
   was added for and it keeps the worker's stdio.  **This is where "an
   integrated Kubernetes proxy setup" belongs**: managing the forward's
   lifetime, learning the port, and tearing it down.
3. *API-native.*  Speak `pods/exec` (SPDY/WebSocket) from the coordinator,
   no `kubectl` binary.  A real dependency and a streaming adapter that
   has to satisfy the `ByteStream` protocol — which is the point of that
   protocol being neutral.

**Getting code into the pod** is the workspace story above, unchanged: an
image with `uv`, execnet provisioned exactly as it is over ssh, the
project under test installed from a shipped wheel, and the tests rsynced.
Once the gateway exists, rsync over the established protocol is strictly
better than a second connection — no extra credentials, no second
authorization path.

**The decision to take first**: does the pod and proxy machinery live in
execnet, or out of tree?  It needs a Kubernetes client and cluster
credentials, which core does not want; but `AsyncGroup._make_gateway`,
`_open_via_stream` and `_resolve_socket_address` are *already* overridable,
so an out-of-tree transport is nearly possible today.  Making that
extension point deliberate and documented may be the better 3.x
deliverable, with `execnet-kubernetes` on top.

Other open questions: pod lifecycle (does execnet create a Job or attach
to something that exists?); image and Python selection; how a `Group` of N
pods maps onto scheduling; and cleanup when the coordinator dies, since a
cluster needs an owner reference or TTL and cannot rely on `terminate`
arriving.

## Flow control: the channel has none

A `send` never blocks and a peer never pushes back.  The outbound queue is
unbounded, and the receiving end's reader drains the socket as fast as it
can into per-channel buffers that are unbounded too — so a fast producer
against a slow consumer is not throttled, it is *stored*.  Measured: 500
MiB sent in 0.33s with no blocking, all of it resident in the consumer's
mailbox (worker RSS 33 → 533 MiB).  Nothing in the API says so;
`Channel.send`'s "possibly blocking if the sender queue is full" describes
2.1's write lock and is now never true.

This is not new — 2.x's receiver thread queued just as eagerly — but the
async core is where it becomes fixable, and it is the same problem HTTP/2
and HTTP/3 solved: per-stream and per-connection windows, a `WINDOW_UPDATE`
equivalent as the consumer drains, and the sender parking when the window
is exhausted.  What that needs here:

- a credit field in the message header or a new opcode (the protocol is
  unversioned, so this is a wire change and belongs *before* the ecosystem
  has more than one implementation of it);
- the sender's park has to work on all four surfaces — a trio task
  awaiting, a blocking `send` on a wakener, gevent parking its greenlet;
- **reporting**, which is the part that makes it worth doing: how much a
  channel has outstanding, how long a send waited, which peer is the slow
  one.  A hang that is really a full window must say so.

Decide whether 3.0 ships the header space for it even if the mechanism
lands later; retrofitting a credit field into a shipped unversioned
protocol is the expensive version of this.

## Deferred, and one rejection

- **`execnet.aio` can drop an item when a `receive` is cancelled.**  The
  module docstring promises the opposite ("no item is consumed and
  dropped"), and it is right about the common case: the cancel posts a
  scope cancel to the host, which usually lands before the item is taken.
  What it does not cover is the cancel arriving *after* the host task
  produced the item — `_HostBridge.call` then sees `future.cancelled()` and
  drops the value it is holding.  Fixing it means being able to put the
  item back at the front of its channel, which the memory channel cannot
  do; the honest interim is to narrow the docstring.  Same shape as the
  flow-control work above, and probably wants the same buffer rework.

- **`execnet.anyio`** (the old Phase E) — a coordinator core on anyio with
  an asyncio backend.  Deferred, not cancelled; the portability invariants
  keep the door cheap.
- **Async-surface gaps**: no `remote_status()`, no `MultiChannel`, no
  group iteration, no `RSync` on `execnet.trio`/`execnet.aio`.  Deliberate;
  worth a documented line rather than silence.
- **`installvia` still needs a socket handoff at all.**  It would not, if
  the server spawned the worker as the *listener*
  (`--protocol-listen 127.0.0.1:0`) and reported its address back: no
  `pass_fds`, no `share()`, works on any interpreter, and the spawn happens
  *before* the reply so failures are diagnosable by construction.  The open
  question is how the server learns the port.  Standalone `execnet server`
  still needs the handoff — it has already accepted the connection.
- **eventlet** stays dead.  **Subinterpreters** are a strategy slot, not a
  plan.
- **A trampoline process was considered and rejected**: the design already
  frees the worker's stdio in-process, and a pump's own stdio side is still
  a blocking pipe, so it relocates the thread rather than removing it — at
  the cost of a process and two copies per message.  Do not revisit without
  a new reason.
- **Unverified**: whether the socket/`installvia` path works on the Windows
  CI job at all.  Assume any platform CI has not exercised is broken.
- **The server-side `share()` handoff still races** (the popen one is
  fixed; see the invariant in `HANDOFF.md`).  `serve_socket_connection`
  closes the accepted socket when the spawn returns, which can beat the
  worker's `fromshare()` — and it cannot wait for the handshake, which goes
  to the coordinator, nor simply close late, which would keep a dead
  worker's connection open and cost the coordinator its EOF.  The fix is a
  marker byte from the worker once it has adopted; the price is that a
  socket worker's stdout becomes a pipe to its server rather than the
  user's.  Decide before claiming Windows `socket=` works.

## Suggested order

1. `execnet info` capability key, `__all__` cleanups, underscore the engine
   methods — small, and item 1 cannot be changed after release.
2. Undraft PR #422.  (The changelog and docs are renumbered already.)
3. Decide the execnet/xdist split for provisioning + workspaces, then build
   points 1–3 of that section.  This is what the next xdist waits on.
4. Kubernetes: decide in-tree versus extension point, then the proxy.
5. Remove the shims — later in 3.x, gated on xdist having released without
   them, with the CI `release` target as the check.
