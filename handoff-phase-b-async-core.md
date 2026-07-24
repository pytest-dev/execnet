# Handoff: Phase B — invert execnet onto an async-native Trio core

For a fresh session on branch `feat/trio-host-thread-io`. B.1–B.5 are done;
work continues at **B.6**. Plan context lives in session memory
(`trio-port-plan`), but everything needed is restated here.

Run checks with `uv run pytest testing/` and `uv run pre-commit run -a`
(never grep-filter pre-commit output). ssh paths have a real local harness
in `testing/test_ssh_local.py` (asyncssh server; system ssh client needed).
Known flake: `test_socket_installvia` EOFs rarely under load.

## Where the repo stands (2026-07-24, after B.5)

Trio is the only IO path; no source is shipped over the wire (workers run
`python -m execnet._trio_worker <config-json>`, foreign/remote interpreters
are uv-provisioned via `_provision.py`, dev coordinators ship a wheel). All
transports work: popen, `python=`, ssh, vagrant_ssh, socket, and `via`
sub-gateways (`GATEWAY_START_SUB` spawn-request + byte relay on the master).

**The inversion landed (B.5)**: there is one protocol engine —
`AsyncGateway` — and the sync API is a facade over it.  `ProtocolSession`
is gone.  Coordinator and worker sessions are `SyncBridgeGateway`
(an `AsyncGateway` subclass) whose `_dispatch` runs the classic sync
`Message.received` handlers under `_receivelock`; the sync
`Channel`/`ChannelFactory` state machine in `gateway_base` is unchanged
and remains the semantic layer for blocking users (and the worker's
exec'd code).  `multi.Group` owns a `FacadeAsyncGroup` task on its
`TrioHost`; `makegateway` and the bounded process shutdown delegate to
`AsyncGroup`, while `terminate` still calls each member's
`exit()`/`join()` (pinned by `test_basic_group`).

File map (src/execnet/):

| file | role |
|---|---|
| `gateway_base.py` | `Message` wire protocol + **`FrameDecoder`** (sans-IO), serializer (incl. duck-typed `save_AsyncChannel` → CHANNEL opcode), sync `Channel`/`ChannelFactory` (raw-receiver registry: `register_raw_receiver`/`allocate_id`, verbatim CHANNEL_DATA routing), `BaseGateway`/`WorkerGateway` (`_send` via bridge session, `_send_nonblocking` for GC), `ExecModel`, `WorkerPool`, `HostNotFound` |
| `_trio_gateway.py` | **the async-native core**: `ByteStream` Protocol, `RawChannel`, `AsyncChannel`, `AsyncGateway` (single serve task; outbound queue items are `(frame, on_written)` so sync senders can wait for the OS write; writer fails pending frames on shutdown; `_finalize` hook for subclass shutdown), `AsyncGroup` (async CM owning the nursery; **all transports**: popen/`python=`/ssh/vagrant_ssh/socket(+installvia)/`via=`; overridable `_make_gateway`/`_open_via_stream`/`_resolve_socket_address`; per-process reaper tasks; terminate = tunneled first, GATEWAY_TERMINATE + timeout + kill, ~2×timeout bound), `RawChannelStream`, `open_popen_gateway`, transport helpers (`connect_command_worker` incl. ssh-255→HostNotFound, `connect_socket_worker`, `start_socketserver_via` (async), `ssh_transport_args`, `popen_worker_argv`) |
| `portal.py` | **`LoopPortal`** (trio-token holder) and **`SyncReceiver`** (loop→plain-thread queue, KI-interruptible `get()`) |
| `_trio_host.py` | sync-facade host: `TrioHost` (loop thread; `start_session` returns a bridge), **`SyncBridgeGateway`** (sync dispatch + portal-FIFO `enqueue_message`/`post_message`/`request_close_write`, threading-`Event` `wait_done`), **`FacadeAsyncGroup`** (bridge-building AsyncGroup; via/installvia through the sync master), `makegateway_trio`, `SyncIOHandle` (remoteaddress/wait/kill/close_write), `RawTunnelStream` (via tunnel; `aclose` feeds own reader EOF), `GATEWAY_START_*` handlers (`_start_sub_and_relay` frame-native) |
| `_trio_worker.py` | worker entry, `TrioWorkerExec` (FIFO `_pump` admission), `_prepare_protocol_fds`; serves on a `SyncBridgeGateway` |
| `gateway.py` | sync coordinator `Gateway` (remote_exec, exit, rinfo) |
| `_exec_source.py` | remote_exec source normalization shared by sync + async coordinators |
| `multi.py` | sync `Group` facade (`_ensure_async_group`, terminate via `AsyncGroup`), `MultiChannel`, `safe_terminate` (WorkerPool-based, kept for tests) |
| `_provision.py` | uv provisioning, wheel build/ship/materialize, argv builders |

Async-core tests live in `testing/test_trio_gateway.py` (memory_stream_pair
protocol tests + popen/via integration inside `trio.run`).

Semantics that MUST survive (xdist depends on them):

- Sends from non-loop threads block until the frame hit the OS write
  (120s → OSError) so abrupt `os._exit` cannot drop "sent" data; sends from
  the loop thread only enqueue. After close: `OSError("cannot send (already
  closed?)")`; `trio.RunFinishedError` maps to the same.
- exec admission order = message arrival order (see `TrioWorkerExec._pump`;
  `test_main_thread_only_concurrent_remote_exec_deadlock` guards this —
  trio shuffles its run batch, so never rely on task-spawn order).
- Channel callbacks run on the receiver (loop) thread — keep for now.

## Phase B goal

Three public namespaces, all pure Trio (**no anyio/asyncio** — an
`execnet.anyio` backend is deferred Phase E; keep idioms portable: neutral
stream protocol, sans-IO protocol logic, no gratuitous trio-only constructs):

```
execnet.sync    – today's blocking API, rebuilt as a facade (top-level
                  execnet.Group etc. stay as compatibility aliases)
execnet.trio    – trio-native AsyncGroup/AsyncGateway/AsyncChannel, awaited
                  directly inside the user's own trio.run
execnet.portal  – the communicating API (exists as portal.py; public
                  exposure happens in B.6)
```

## Remaining work

### B.4 Async-native core — DONE (commits `0eb6cc5`..`8286b77`)

Everything lives in `_trio_gateway.py` (see file map).  Landed:
RawChannel + AsyncChannel two-level model, AsyncGateway single-task
dispatch (no `_receivelock` on the async path), AsyncGroup with bounded
nursery-scoped termination, async popen/`python=`/`via=` gateways with
`remote_exec` inside the user's own `trio.run`, and the frame-native via
tunnel (raw-receiver registry in the sync `ChannelFactory`;
`ChannelByteStream` is gone).

Leftovers deliberately deferred:

- **ChannelFile / makefile over RawChannel** — do together with the B.5
  facade (the current `ChannelFileRead` is str-based; decide there whether
  a text wrapper stays for backward compat).
- rsync data plane / wheel shipping over raw channels — later.
- async ssh/socket gateways — the async `makegateway` only does
  popen-style and `via=`; other transports stay on the sync host path
  until B.5/B.6 need them.
- worker-side CHANNEL_EXEC on an AsyncGateway is rejected with a
  RemoteError ("unsupported message") — async exec is Phase C
  (`exec=task`).

### B.5 Rebuild the sync API as a facade — DONE (commit `f932084`)

Public surface stayed byte-for-byte; the whole sync suite passes
unchanged (508 passed).  What landed, and the decisions taken:

- One engine: `SyncBridgeGateway(AsyncGateway)` serves both coordinator
  and worker; `ProtocolSession` deleted.  Sync dispatch still runs the
  `gateway_base` Message handlers, so the sync `Channel`/`ChannelFactory`
  semantics (queues, callbacks, ENDMARKER, weakref drop) are untouched —
  and remain what the worker's exec'd code sees.
- Send invariant kept: `enqueue_message` posts every frame through the
  portal (one global FIFO); non-loop threads wait on the frame's
  `on_written` ack (120s → OSError), the loop thread only enqueues.
- `Channel.__del__` now goes through `_send_nonblocking` →
  `SyncBridgeGateway.post_message` (portal post, never waits; falls back
  to `_send` for duck-typed test gateways).
- KI decision: blocking data paths (send-ack, receive, waitclose, join)
  wait on `threading.Event`/`queue.Queue` — KI-interruptible as before.
  Management ops (makegateway, terminate) run inside `portal.run`
  (`trio.from_thread.run`) where KI is deferred; accepted for now.
- `Group.terminate` still calls member `exit()`/`join()` (contract pinned
  by `test_basic_group`), with process wait/kill delegated to
  `AsyncGroup.terminate` (tunneled-first, ~2×timeout bound).
- Gotcha fixed on the way: a stream-closing `aclose` on the via tunnel
  (`RawTunnelStream`) must feed EOF to its own reader, else the bridge's
  serve task never finishes and terminate hangs.
- `ChannelFile`/`makefile`: kept the existing str-based `gateway_base`
  classes as-is for backward compat (they only use channel send/receive).
- NOT yet retired: `ExecModel` internals and `WorkerPool` (still the
  worker STATUS duck-type shape and used by `multi.safe_terminate`,
  `testing/test_threadpool.py`, conftest's `pool` fixture).  Do this at
  the end of the phase (B.6 or later) if the tests pinning them move.

### B.6 Namespace split

Introduce `execnet.sync` / `execnet.trio` / `execnet.portal`; top-level
`execnet.*` aliases into `execnet.sync`. Existing suite pinned to the
facade; add trio-native tests (memory_stream_pair + FrameDecoder for
protocol-level, real popen gateways inside `trio.run` for integration).

## Invariants (do not regress)

- No source shipping, ever. Workers import installed execnet+trio; rough
  major/minor version check (`_trio_worker._check_version`) warns.
- Wheel-on-demand for `GATEWAY_START_SUB` is a recorded TODO in
  `_provision.spawn_request` (currently ships eagerly whenever the sub-spec
  has `python=`/`ssh=`).
- Worker teardown: `_trio_worker._run_worker` ends with `os._exit(0)`
  because trio's `to_thread` cache uses non-daemon threads.
- `Group.terminate(timeout)` must never hang (bounded even when kill is
  stuck).

## After Phase B (context, don't build now)

- **C**: worker config axes `loop=thread|main` × `exec=thread|main|task`
  (compat: `execmodel=thread` → loop=main+exec=thread; `main_thread_only` →
  loop=thread+exec=main); `exec=task` later enables async remote_exec.
- **D**: docs + trio-surface tests, pytest-xdist verification.
- **E** (deferred): `execnet.anyio` — coordinator core on anyio,
  `backend="asyncio"` knob for the facade. `FrameDecoder` and the B.4
  channel layers are IO-free by construction, so they port as-is; anyio's
  `StapledByteStream` satisfies the `ByteStream` protocol structurally.
