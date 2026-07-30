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

## Surface review — LANDED 2026-07-29 (`7aa17fe..e75cd0a`)

The public surface had settled commit by commit and was never reviewed as
a whole.  Doing that before the Phase D docs froze it produced the
following, which **wins over anything below or in
`handoff-boundary-protocol-rethink.md` that contradicts it**.

**Namespaces are now one per concurrency library you drive execnet from**:
`execnet.sync` (threads; the top-level aliases), `execnet.trio`,
`execnet.aio`, `execnet.gevent`.  `execnet.portal` is gone --
`execnet._portal` plus the trio-free `execnet._boundary`.

| was | is | why |
|---|---|---|
| `execnet.portal` public | `execnet._portal` private | it exported `Wakener`/`Mailbox`/`OneShot`/`LoopPortal` but *not* `register_wakener`, so the advertised extension point was unreachable -- and there is no plan to let third parties add event loops at all |
| Wakener registry (`register_wakener`, lazy module table) | two-branch `make_wakener(Literal["thread","gevent"])` | exactly two backends exist; every other library gets a facade |
| `wait=` spec key | gone; `Group._wait_backend`, set by the facade | it described the *caller*, which the namespace already says.  Worker-side wait was always derived from the profile |
| gevent via `wait=gevent` | `execnet.gevent.Group` | symmetric with the other surfaces |
| `execmodel=` spec key | `profile=` (`execmodel=` a permanent alias) | the key selects the *worker profile*; the local execution model it was named after no longer exists |
| `Group(execmodel=)`, `set_execmodel`, `group.execmodel` | deprecated; only the remote default survives | they had no behavioural effect.  **xdist passes `Group(execmodel=...)` as a keyword** -- that must keep working |
| `main_thread_only` profile | deprecated alias for `thread` | `HybridExec` already gives the first remote_exec the real main thread.  Its extra behaviour (refusing a second concurrent remote_exec) was a 1s-timeout deadlock guard, now deleted |
| one `TrioHost` per `Group` | one shared `execnet.Host` per process, `Group(host=...)` to override | a host is a thread and a loop, not something groups need isolated |
| blocking inside a running loop hangs | raises, naming `execnet.aio` / `execnet.trio` | worker-side channels stay exempt: exec'd code may run its own loop |
| `aio.Group`/`Gateway`/`Channel` | `aio.AsyncGroup`/`AsyncGateway`/`AsyncChannel` | matches `execnet.trio`; swapping the import ports the code |
| `open_popen_gateway` | `open_gateway` (both async surfaces) | it always accepted any spec |

Behaviour changes worth a changelog line:

- a second concurrent `remote_exec` under `main_thread_only` used to close
  the channel with `MAIN_THREAD_ONLY_DEADLOCK_TEXT`; it now runs on a pool
  thread.  `_executetask_complete`, `MAIN_THREAD_ONLY_ADMIT_TIMEOUT` and
  the error text are deleted; `MainExec` became `PrimaryThreadPump`.
- `execnet.aio` cancellation is now real: a cancelled `receive` cancels
  the host-side operation instead of consuming and discarding an item.
  `send`/`send_eof`/`aclose`/`terminate` are shielded instead.
- the boundary carriers raise `execnet.TimeoutError`, not the builtin;
  `OneShot` double-resolve is a `RuntimeError`, not an `assert`.
- `STATUS` answers both `profile` and (legacy) `execmodel`.
- **Latent livelock fixed**: `execnet.dumps` warned on *every* access, and
  xdist calls it from `serialize_warning_message` -- i.e. from inside
  pytest's warning-recording hook.  One DeprecationWarning in a worker
  therefore recorded a warning that recorded a warning, unbounded, and
  wedged the run.  The shim warns once per process
  (`execnet._xdist_compat_warned`).  Anything that warns in a worker can
  hit this class of bug; keep it in mind.

New tests: `testing/test_host.py` (sharing, explicit `Host`, fork, the
loop guards), `testing/test_boundary.py` (renamed from `test_portal.py`),
aio cancellation contracts in `testing/test_aio.py`.  The `execmodel`
fixture parametrization collapsed to a single `profile` fixture, so the
suite is ~540 items rather than ~765.

### D.3 done: the xdist suite runs in CI (2026-07-30)

The `xdist` job in `.github/workflows/test.yml` runs pytest-xdist's own
test suite against the execnet built from the branch.  **Doing this for
the first time found 16 real regressions** that our suite could not see
(it runs xdist as a *tool*, which exercises none of the crash-replacement
or report-serialization paths).  Baseline: released execnet 2.1.2 gives
220 passed / 0 failed.  Two root causes, both fixed:

1. `makegateway` wrote the *normalized* profile back onto the caller's
   XSpec.  xdist reuses one spec object and re-reads `spec.execmodel` to
   decide whether it still needs the `execmodel=main_thread_only//`
   prefix, so after normalization it prefixed a second time and built
   `execmodel=...//execmodel=...//popen` -> `ValueError: duplicate key`.
   Every crashed-worker-replacement test failed.  Fix: `resolve_profile`
   validates and warns but callers no longer assign its result to a
   caller-owned spec; `effective_profile` (no warning) maps at the
   consumption points.  **Rule: filling in a missing spec value is
   idempotent and fine; rewriting one the caller set is not.**
2. the `execnet.dumps` deprecation warning (see above).

Remaining known failure, deselected via `.github/xdist-known-failures.txt`
(the job is green otherwise): `test_remote_inner_argv` asserts
`sys.argv == ["-c"]` and documents "the behavior due to execnet using
`python -c`" -- which the no-source-shipping worker launch deliberately
changed.  That one needs an xdist PR.

Still open from the review, deliberately not done: the async surfaces have
no `remote_status()`, no `MultiChannel`, no group iteration, and no
`RSync`.  `AsyncGroup.makegateway` defaults workers to the `thread`
profile (the coordinator's shape does not dictate the worker's).

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
| `_message.py` / `_serialize.py` / `_channel.py` / `_gateway_base.py` / `_errors.py` / `_execmodel.py` | split by concern since this table was written: wire protocol + sans-IO `FrameDecoder`; serializer (CHANNEL opcode incl. duck-typed `save_AsyncChannel`); sync `Channel`/`ChannelFactory`; `BaseGateway`/`WorkerGateway`; error types; `WORKER_PROFILES` + `resolve_profile` + the deprecated `ExecModel` xdist shim.  `gateway_base.py` is now only a warning shim |
| `_trio_gateway.py` | async core: `ByteStream` Protocol, `RawChannel`/`AsyncChannel`, `AsyncGateway` (outbound queue of `(frame, on_written)`; `_finalize` hook), `AsyncGroup` (all transports, overridable `_make_gateway`/`_open_via_stream`/`_resolve_socket_address`, reapers, bounded terminate), stream/argv helpers |
| `_trio_host.py` | `TrioHost` (loop thread), `SyncBridgeGateway`, `FacadeAsyncGroup`, `makegateway_trio`, `SyncIOHandle`, `RawTunnelStream` (via tunnel; `aclose` feeds own reader EOF), `GATEWAY_START_*` handlers |
| `_trio_worker.py` | worker entry (`_main`/`serve_popen_trio`/`serve_socket_trio`), `TrioWorkerExec` (FIFO `_pump` admission; `integrate_as_primary_thread`), `_prepare_protocol_fds`, `_check_version` |
| `gateway.py` / `multi.py` | sync `Gateway` / sync `Group` facade, `MultiChannel`, `safe_terminate` (WorkerPool-based, kept for tests) |
| `sync.py` / `trio.py` / `aio.py` / `gevent.py` | the four public namespaces; `_host.py` holds the shared `Host`, `_portal.py`/`_boundary.py` the (private) boundary kit |
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
- Channel callbacks now run in a threadpool thread driven by a per-channel
  consumer *task* on the loop (RESOLVED 2026-07-26, was the "revisit in D"
  open decision — moved off the loop).  A slow callback no longer blocks the
  reader; per-channel order is still strict, and `waitclose()` still returns
  only after every callback (incl. the endmarker) has run.  See "Callback
  consumer tasks" below.
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

| `profile=` (was `execmodel=`) | loop thread | exec'd code runs | channel | worker wait | extra deps |
|---|---|---|---|---|---|
| `thread` (default) | side thread | **classic hybrid restored**: primary on the main thread, overflow on pool threads (claim decided during FIFO admission) | sync | thread | — |
| ~~`main_thread_only`~~ | *deprecated 2026-07-29, aliases to `thread`* | | | | |
| `trio` (new) | **main thread** | async sources as tasks — one single thread total; top-level await or async def; sync sources rejected; termination cancels tasks | AsyncChannel | (loop) | — |
| `gevent` (revived) | side thread | greenlets on a main-thread hub, one per remote_exec | sync | gevent (derived) | `execnet[gevent]`, auto-added by uv provisioning |

(The coordinator-side counterpart of the last row is now `execnet.gevent`,
not `wait=gevent`.)

Architecture: `TrioWorkerExec` is a pure FIFO admission pump delegating
to strategy objects (`WORKER_EXEC_STRATEGIES` in `_trio_worker.py`:
PoolExec building block, MainExec, HybridExec, GreenletExec; TaskExec
serves a plain AsyncGateway via its pluggable `_exec_handler` — no sync
bridge at all in the trio profile).  Subinterpreters: future strategy
slot, not built.  `WORKER_PROFILES` (`_execmodel.py`) validates
coordinator-side in makegateway, via `resolve_profile`.

**Native info/setup** (the pytest fix): `Message.GATEWAY_INFO` (code 10)
answers `_rinfo()` from the dispatch loop; chdir/nice/env ship in the
worker config JSON and apply at startup.  Coordinator bookkeeping can no
longer claim an exec slot (previously an info call could occupy the main
thread so pytest landed on a worker thread) — `rinfo_source` and the
post-start remote_exec setup block are gone.

Coordinator-gevent integration: `TrioHost.call_pending` (OneShot from a
host task) backs makegateway / `SyncIOHandle.wait/kill` /
`Group.terminate` whenever the group's wait backend is not `thread`, so a
gevent app's management ops park only the calling greenlet.  The default
`thread` backend keeps the KI-deferred `portal.run` path.

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
4. **Open decision CLOSED (2026-07-26)**: channel callbacks moved off the
   loop thread — see "Callback consumer tasks" below.
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

## Callback consumer tasks (`setcallback`, LANDED 2026-07-26)

`setcallback` no longer delivers inline on the loop thread and no longer
pins the channel in a `ChannelFactory._callback_channels` registry.  Instead
`Channel.setcallback` → `BaseGateway._start_channel_consumer` →
`SyncBridgeGateway.attach_consumer`, which on the loop:

- moves any already-buffered mailbox items into a fresh per-channel inbox
  (a `trio` memory channel), diverts future raw payloads there
  (`raw.set_consumer(inbox.send_nowait, on_close)`), and nulls `_mailbox`;
- starts a consumer *task* on the host root nursery (`_run_consumer`) that is
  handed a **strong** reference to the sync `Channel` — so the channel's
  lifecycle is now bound to the task (and to GC once the stream closes),
  replacing the strong-ref registry.

`_run_consumer` does `async for data in inbox:` and runs each callback via
`trio.to_thread.run_sync(..., limiter=host.callback_limiter)` — off the loop,
one thread of a bounded pool (`DEFAULT_CALLBACK_THREADS=40`), sequential per
channel (order preserved), concurrent across channels.  A raising callback
(or a `loads_internal` failure) → `CHANNEL_CLOSE_ERROR` + local close
(`_consumer_failed`).  On EOF/close the endmarker fires (shielded, bounded by
`CONSUMER_ENDMARKER_GRACE`) and the task sets `channel._consumer_done`.

Key invariant preserved: `waitclose()` waits on `_consumer_done` (not
`_receiveclosed`) for callback channels, so it still returns only after every
callback — including the endmarker — has run (`test_waiting_for_callbacks`,
`test_channel_endmarker_callback`).  Deleted along the way: `_callback`,
`_endmarker`, `_fire_endmarker`, `_callback_channels`,
`_register_callback_channel`, and the callback branch of `_deliver_payload`
(now uniformly `mailbox.put`).  `__del__`'s `CHANNEL_LAST_MESSAGE` case folds
away (a callback channel is never GC'd while open).

Stress coverage: `testing/test_channel_stress.py` (Hypothesis) with a
`--stress=N` pytest option (profiles registered in
`testing/conftest.py::pytest_configure`; default quick profile).  Hypothesis
also surfaced and we fixed a latent serializer bug: `_save_integral` only
bounds-checked the upper int4 limit, so a negative int below `-2**31`
overflowed `struct.pack('!i', …)` instead of taking the long path
(`FOUR_BYTE_INT_MIN` added; regression `test_serializer.test_int_boundaries`).

`hypothesis` was added to the `testing` extra in `pyproject.toml`.

## Invariants (do not regress)

- No source shipping, ever.  Workers import installed execnet+trio;
  rough major/minor version check (`_trio_worker._check_version`) warns.
- Worker teardown: `_trio_worker._run_worker` ends with `os._exit(0)`
  because trio's `to_thread` cache uses non-daemon threads.
- `import execnet` must not import the trio event loop.
- Keep async-core idioms anyio-portable (neutral `ByteStream`, sans-IO
  `FrameDecoder`) — `execnet.anyio` is deferred Phase E.
