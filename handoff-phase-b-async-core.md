# Handoff: Phase B — invert execnet onto an async-native Trio core

For a fresh session on branch `feat/trio-host-thread-io`. B.1–B.4 are done;
work continues at **B.5**. Plan context lives in session memory
(`trio-port-plan`), but everything needed is restated here.

Run checks with `uv run pytest testing/` and `uv run pre-commit run -a`
(never grep-filter pre-commit output). ssh paths have a real local harness
in `testing/test_ssh_local.py` (asyncssh server; system ssh client needed).
Known flake: `test_socket_installvia` EOFs rarely under load.

## Where the repo stands (2026-07-24, after commit `8286b77`)

Trio is the only IO path; no source is shipped over the wire (workers run
`python -m execnet._trio_worker <config-json>`, foreign/remote interpreters
are uv-provisioned via `_provision.py`, dev coordinators ship a wheel). All
transports work: popen, `python=`, ssh, vagrant_ssh, socket, and `via`
sub-gateways (`GATEWAY_START_SUB` spawn-request + byte relay on the master).
The architecture is still sync-first: Trio hides behind the sync
`Channel`/`Gateway`/`Group`. Phase B inverts this.

File map (src/execnet/):

| file | role |
|---|---|
| `gateway_base.py` | `Message` wire protocol + **`FrameDecoder`** (sans-IO), serializer (incl. duck-typed `save_AsyncChannel` → CHANNEL opcode), sync `Channel`/`ChannelFactory` (now with **raw-receiver registry**: `register_raw_receiver`/`allocate_id`, verbatim CHANNEL_DATA routing), `BaseGateway`/`WorkerGateway`, `ExecModel`, `WorkerPool`, `HostNotFound` |
| `_trio_gateway.py` | **the async-native core (B.4)**: `ByteStream` Protocol, `RawChannel` (id-routed verbatim byte payloads, sync-Channel close semantics: OSError on closed sends, EOFError/RemoteError on receive, `send_eof` = LAST_MESSAGE/sendonly), `AsyncChannel` (dumps/loads per item, strconfig/RECONFIGURE, timeout via `fail_after`, `wait_closed`, channel-passing), `AsyncGateway` (single serve task: reader dispatches inline — no locks; writer drains unbounded queue, one `send_all` per frame), `AsyncGroup` (async CM owning the nursery; `makegateway` popen/`python=`/`via=`; terminate = tunneled first, then GATEWAY_TERMINATE + timeout + kill, ~2×timeout bound), `RawChannelStream` (RawChannel→ByteStream adapter), `open_popen_gateway`, transport helpers (`staple_*`, `read_handshake_ack`, `popen_worker_argv`) |
| `portal.py` | **`LoopPortal`** (trio-token holder) and **`SyncReceiver`** (loop→plain-thread queue, KI-interruptible `get()`) |
| `_trio_host.py` | sync-facade host: `TrioHost` (loop thread), `ProtocolSession`, `RawTunnelStream` (via tunnel over sync master, raw registry based), gateway factories, `GATEWAY_START_*` handlers (`_start_sub_and_relay` is frame-native: ready byte alone, then one whole frame per CHANNEL_DATA) |
| `_trio_worker.py` | worker entry, `TrioWorkerExec` (FIFO `_pump` admission), `_prepare_protocol_fds` |
| `gateway.py` | sync coordinator `Gateway` (remote_exec, exit, rinfo) |
| `_exec_source.py` | remote_exec source normalization shared by sync + async coordinators |
| `multi.py` | sync `Group`, `MultiChannel`, `safe_terminate` (WorkerPool-based) |
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

### B.5 Rebuild the sync API as a facade

Public surface stays byte-for-byte: `Group`, `makegateway` spec strings,
`Gateway.remote_exec/exit/reconfigure/remote_status/hasreceiver`, `Channel`
send/receive/setcallback/makefile/waitclose/reconfigure, `MultiChannel`,
`RSync`, `execnet.dumps/loads/dump/load`, `HostNotFound`, `TimeoutError`,
`RemoteError`, `DataFormatError`. Existing tests keep running against the
facade unchanged — that is the acceptance bar.

Known tricky spots:

- `Channel.__del__` sends CHANNEL_CLOSE/LAST_MESSAGE during GC — must go
  through the portal as a non-waiting post, and tolerate interpreter
  shutdown (today: `suppress(OSError, ValueError)` + `Message is not None`
  guard).
- KeyboardInterrupt lands on the main thread while blocked inside a portal
  call — decide propagation (today the sync side blocks in
  `threading.Event.wait`, which KI can interrupt).
- `group.execmodel` / `set_execmodel` / `spec.execmodel` stay as deprecated
  shims. `main_thread_only` must keep working throughout (pytest-xdist runs
  GUI-bound code on the worker main thread via
  `TrioWorkerExec.integrate_as_primary_thread`).
- Retire `ExecModel` internals and `WorkerPool` at the END of the phase
  (WorkerPool is still the worker exec duck-type target for STATUS and is
  used by `multi.safe_terminate` + `testing/test_threadpool.py` +
  `testing/conftest.py`'s `pool` fixture).

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
