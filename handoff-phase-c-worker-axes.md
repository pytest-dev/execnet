# Handoff: Phase C — worker config axes (`loop=` × `exec=`) + Phase D

For a fresh session on branch `feat/trio-host-thread-io`.  Phase B (the
inversion onto the async-native Trio core) is **complete**; this doc
supersedes `handoff-phase-b-async-core.md` (kept for the B record).
Plan context lives in session memory (`trio-port-plan`), but everything
needed is restated here.

The rework is up as **draft PR pytest-dev/execnet#422** (branch pushed
to the `origin` fork).  Keep it updated as C/D land; undraft once the
docs overhaul (D.1) at least covers migration notes.

Run checks with `uv run pytest testing/` and `uv run pre-commit run -a`
(never grep-filter pre-commit output).  The suite is xdist-clean: `uv run
pytest testing/ -n 12` passes (~7s) since the session-attach race fix
(`51b9053`) — keep it that way.  ssh paths have a real local harness in
`testing/test_ssh_local.py` (asyncssh server; system ssh client needed).
Known flake: `test_socket_installvia` EOFs rarely under load.

## Where the repo stands (2026-07-25, after `a69b844`)

One protocol engine: `AsyncGateway` (`_trio_gateway.py`).  The sync API
is a facade over it — coordinator and worker sessions are
`SyncBridgeGateway` (an `AsyncGateway` subclass in `_trio_host.py`) whose
`_dispatch` runs the classic sync `Message.received` handlers under
`_receivelock`; `SyncBridgeGateway.__init__` attaches itself to the sync
gateway *before* serving starts (the fix for the startup race where a
STATUS/CHANNEL_EXEC arriving first replied through the IO stub and killed
the session).  `multi.Group` owns a `FacadeAsyncGroup` task on its
`TrioHost`; makegateway and bounded process shutdown delegate to
`AsyncGroup`, while `terminate` keeps the member `exit()`/`join()`
contract (pinned by `test_basic_group`).  `AsyncGroup` supports every
transport (popen, `python=`, ssh, vagrant_ssh, socket with installvia,
`via=`).  Public namespaces: `execnet.sync` (top-level `execnet.*`
aliases into it), `execnet.trio`, `execnet.portal` (trio/portal lazy via
module `__getattr__`; `import execnet` must not import trio — pinned by
`testing/test_namespaces.py`).

No source is shipped over the wire: workers run
`python -m execnet._trio_worker <config-json>`; foreign/remote
interpreters are uv-provisioned via `_provision.py`; dev coordinators
ship a wheel.

File map (src/execnet/):

| file | role |
|---|---|
| `gateway_base.py` | `Message` wire protocol + sans-IO `FrameDecoder`, serializer (CHANNEL opcode incl. duck-typed `save_AsyncChannel`), sync `Channel`/`ChannelFactory` (raw-receiver registry), `BaseGateway`/`WorkerGateway` (`_send` via bridge, `_send_nonblocking` for GC), `ExecModel`, `WorkerPool`, `HostNotFound` |
| `_trio_gateway.py` | async core: `ByteStream` Protocol, `RawChannel`/`AsyncChannel`, `AsyncGateway` (outbound queue of `(frame, on_written)`; `_finalize` hook), `AsyncGroup` (all transports, overridable `_make_gateway`/`_open_via_stream`/`_resolve_socket_address`, reapers, bounded terminate), stream/argv helpers |
| `_trio_host.py` | `TrioHost` (loop thread), `SyncBridgeGateway`, `FacadeAsyncGroup`, `makegateway_trio`, `SyncIOHandle`, `RawTunnelStream` (via tunnel; `aclose` feeds own reader EOF), `GATEWAY_START_*` handlers |
| `_trio_worker.py` | worker entry (`_main`/`serve_popen_trio`/`serve_socket_trio`), `TrioWorkerExec` (FIFO `_pump` admission; `integrate_as_primary_thread`), `_prepare_protocol_fds`, `_check_version` |
| `gateway.py` / `multi.py` | sync `Gateway` / sync `Group` facade, `MultiChannel`, `safe_terminate` (WorkerPool-based, kept for tests) |
| `sync.py` / `trio.py` / `portal.py` | the three public namespaces |
| `_exec_source.py`, `_provision.py`, `xspec.py`, `rsync.py` | source normalization, uv provisioning + argv builders, spec parsing, rsync |

## Semantics that MUST survive (xdist depends on them)

- Sends from non-loop threads block until the frame hit the OS write
  (120s → OSError) so abrupt `os._exit` cannot drop "sent" data; loop
  thread sends only enqueue.  All sends go through one portal-posted FIFO.
- After close: `OSError("cannot send (already closed?)")`;
  `trio.RunFinishedError` maps to the same.
- exec admission order = message arrival order (`TrioWorkerExec._pump`;
  `test_main_thread_only_concurrent_remote_exec_deadlock` guards this —
  trio shuffles its run batch, so never rely on task-spawn order).
- Channel callbacks run on the receiver (loop) thread — keep for now
  (open decision: revisit in D).
- `Group.terminate(timeout)` never hangs (~2×timeout bound, issues
  #43/#221).
- Sync blocking waits (send-ack, receive, waitclose, join) stay on
  `threading.Event`/`queue.Queue` so KeyboardInterrupt can interrupt
  them; `portal.run` (KI-deferred) is only for management ops.

## Phase C — LANDED: use-case worker profiles on `execmodel=` (2026-07-26)

Where this conflicts with anything below or elsewhere, this section
wins.  The axes framing (`loop=`/`exec=` as spec keys, a reserved
`backend=` axis) was **dropped** in the final rethink with Ronny:
different use-cases get named profiles, `execmodel=` is the public mode
key (xdist already passes it), and `wait=` is the only other public
knob.  Implemented in commits `b2f43c3..cbce183`:

| `execmodel=` | loop thread | exec'd code runs | channel | worker wait | extra deps |
|---|---|---|---|---|---|
| `thread` (default) | side thread | **classic hybrid restored**: primary on the main thread, overflow on pool threads (claim decided during FIFO admission) | sync | thread | — |
| `main_thread_only` | side thread | main thread, serialized (deadlock guard) | sync | thread | — |
| `trio` (new) | **main thread** | async sources as tasks — one single thread total; top-level await or async def; sync sources rejected; termination cancels tasks | AsyncChannel | (loop) | — |
| `gevent` (revived) | side thread | greenlets on a main-thread hub, one per remote_exec | sync | gevent (derived) | `execnet[gevent]`, auto-added by uv provisioning |

Architecture: `TrioWorkerExec` is a pure FIFO admission pump delegating
to strategy objects (`WORKER_EXEC_STRATEGIES` in `_trio_worker.py`:
PoolExec building block, MainExec, HybridExec, GreenletExec; TaskExec
serves a plain AsyncGateway via its pluggable `_exec_handler` — no sync
bridge at all in the trio profile).  Subinterpreters: future strategy
slot, not built.  `EXECMODEL_PROFILES` (gateway_base) validates
coordinator-side in makegateway.

**Native info/setup** (the pytest fix): `Message.GATEWAY_INFO` (code 10)
answers `_rinfo()` from the dispatch loop; chdir/nice/env ship in the
worker config JSON and apply at startup.  Coordinator bookkeeping can no
longer claim an exec slot (previously an info call could occupy the main
thread so pytest landed on a worker thread) — `rinfo_source` and the
post-start remote_exec setup block are gone.

Coordinator-gevent integration: `TrioHost.call_pending` (OneShot from a
host task) backs makegateway / `SyncIOHandle.wait/kill` /
`Group.terminate` whenever the wait backend is not `thread`, so a gevent
app's management ops park only the calling greenlet.  `wait=thread`
keeps the KI-deferred `portal.run` path.

Still decided/standing: core stays trio-only (anyio/asyncio-core port
rejected; asyncio apps use `execnet.aio`); eventlet stays dead; exec'd
code may start its own loop in every profile except `trio`.

## Phase C — original gap analysis (pre-decision record, superseded)

## Phase C — original gap analysis (pre-decision record)

Target axes: `loop=thread|main` × `exec=thread|main|task`.
Compat mapping: `execmodel=thread` → `loop=main` + `exec=thread`;
`execmodel=main_thread_only` → `loop=thread` + `exec=main`.

1. **Spec surface**: `xspec.py` only knows `execmodel=`.  Add `loop=` /
   `exec=` keys, combination validation, and the compat mapping.  Same
   for the worker CLI config JSON (`_provision.worker_cli_arg` still
   ships `{"id", "execmodel", "coordinator_version"}`).
2. **`loop=main` does not exist**: the worker always starts `TrioHost`
   on a dedicated thread (`serve_popen_trio`), main thread parked in
   `gateway.join()` or integrated as exec primary.  The compat mapping
   means plain `execmodel=thread` workers should end up with `trio.run`
   on the main thread — a structural change to `_run_worker`, not a
   flag.
3. **`exec=main`** is today's `main_thread_only` machinery
   (`integrate_as_primary_thread` + `_executetask_complete` deadlock
   guard) — keep behavior, re-home under the new naming.
4. **`exec=task` (async remote_exec)**: `CHANNEL_EXEC` on a pure
   `AsyncGateway` is still rejected ("unsupported message" in
   `_trio_gateway._dispatch`).  Needs a task-based exec scheduler
   handing exec'd code an `AsyncChannel`, preserving FIFO admission,
   plus explicit cancellation semantics.
5. **STATUS / `remote_status()`** reports `execmodel`; must speak the
   new axes compatibly.
6. **Retire `ExecModel`/`WorkerPool`** (deferred from B): replace
   internals with plain `threading`/`queue`; deprecated shims for
   `group.execmodel`/`set_execmodel`/`spec.execmodel`.  Pinned today by
   `multi.safe_terminate`, `testing/test_threadpool.py`, conftest's
   `pool` fixture, `Channel._items` (`execmodel.queue`), and the worker
   STATUS duck-type — gated on the compat mapping landing first.
7. **Wheel-on-demand for `GATEWAY_START_SUB`**: recorded TODO in
   `_provision.spawn_request` (currently ships eagerly whenever the
   sub-spec has `python=`/`ssh=`).

## Phase D — what is missing

1. **Docs are entirely stale**: `doc/` still describes source
   bootstrapping and execmodels; nothing on uv provisioning, the
   no-source-shipping/version-compat policy, the three namespaces, or
   `execnet.trio`/`execnet.portal` APIs.  `doc/implnotes.rst` references
   pre-inversion internals.  Changelog entries for the whole branch.
2. **Trio-surface test gaps**: async ssh/socket/vagrant transport tests
   (the asyncssh harness only exercises the sync facade; both share
   `connect_command_worker`, so a direct `AsyncGroup` ssh test is
   cheap), cancellation mid-`remote_exec`/`receive`, `send_eof` and
   reconfigure against real workers, B.5 engine paths (write-ack
   failure, `enqueue_message` after close).
3. **pytest-xdist verification**: run pytest-xdist's own suite against
   this branch plus real `-n` smoke runs (crash/endmarker tests,
   `main_thread_only` GUI case).  Our own suite running `-n 12` green is
   necessary but not sufficient.
4. **Close the open decision**: channel callbacks on the loop thread —
   keep or move.
5. **xfail markers audit (done 2026-07-26, keep as-is)**: the 11
   consistent XPASSes were investigated — trio's single-loop dispatch +
   FIFO admission makes them pass reliably when idle, but under
   sustained load `test_gateway_status_busy` (numexecuting race:
   `_track_start` runs in a separately scheduled task) and
   `test_popen_stderr_tracing` (capfd race) still fail, so the
   `flakytest` marks stay.  `test_safe_terminate2`'s xpass is CPython
   dummy-thread accounting, unrelated to trio.  To retire the status
   marks for real: retry-poll for `numexecuting == 2` like the tests
   already do for `== 0`.

Recommended order: C.1+C.2 first (spec axes + loop placement) since the
compat mapping unblocks the ExecModel retirement, then `exec=task`; run
the xdist verification (D.3) mid-C as an early canary rather than only
at the end.

## Invariants (do not regress)

- No source shipping, ever.  Workers import installed execnet+trio;
  rough major/minor version check (`_trio_worker._check_version`) warns.
- Worker teardown: `_trio_worker._run_worker` ends with `os._exit(0)`
  because trio's `to_thread` cache uses non-daemon threads.
- `import execnet` must not import the trio event loop.
- Keep async-core idioms anyio-portable (neutral `ByteStream`, sans-IO
  `FrameDecoder`) — `execnet.anyio` is deferred Phase E.
