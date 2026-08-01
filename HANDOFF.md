# Handoff: execnet on a Trio core

Branch `feat/trio-host-thread-io`, draft PR **pytest-dev/execnet#422**.
This is the doc to read first.  Two companions:

- **`ROADMAP-3.0.md`** — what this branch ships as, and what is still open.
  The work list lives there, not here.
- **`handoff-history.md`** — the compressed record of what landed, with
  commit ranges and the lessons that cost a debugging round each.

## How to work here

```
uv run pytest testing/          # 583 passed, 66 skipped
uv run pytest testing/ -n 12    # must stay green (~8s)
uv run pre-commit run -a        # never grep-filter its output
uv run tox -e docs              # sphinx -W, then doctests all of doc/
```

`tox -e docs` doctests the whole `doc/` tree, not just the examples:
`doc/basics.rst` is executed too, so a `>>>` block there is checked.  The
two files with no `>>>` in them — `doc/example/test_debug.rst` (the trace
transcript) and `test_ssh_fileserver.rst` — are prose nothing verifies.

ssh paths have a real local harness in `testing/test_ssh_local.py` (an
asyncssh server; needs a system ssh client).  Hypothesis stress coverage
is `testing/test_channel_stress.py` behind `--stress=N`.

Known flakes, all timing:

- `test_socket_installvia` EOFs rarely under load.
- `test_gateway_status_busy` (numexecuting race) and
  `test_popen_stderr_tracing` (capfd race) keep their `flakytest` marks
  and XPASS when idle — see `handoff-history.md`.
- `test_info_reports_what_a_coordinator_needs` failed once under `-n 12`
  (2026-07-31), green in isolation and on rerun; **not diagnosed**.

CI runs pytest-xdist's own suite against this execnet — see
"The xdist contract" in `ROADMAP-3.0.md`.  That job is the one that
catches what our suite structurally cannot.

## Where the repo stands

There is **one protocol engine**, the async-native `AsyncGateway`
(`_trio_gateway.py`), and everything else is a surface over it.  The wire
protocol (`Message` framing) is unchanged from 2.1.

**No source is shipped over the wire, ever.**  Workers are launched as
`execnet worker <transport>` and configured by a frame on it; foreign and
remote interpreters are uv-provisioned; a dev coordinator builds and ships
a wheel.  A major/minor version skew is **refused** by the worker as its
answer to that frame (`_trio_worker._version_refusal`), so the coordinator
gets the reason rather than an EOF; `EXECNET_IGNORE_VERSION_SKEW=1` in its
environment or its config `env:` downgrades that to a warning.

### Four namespaces, one per concurrency library you drive execnet from

| namespace | what it is |
|---|---|
| `execnet` / `execnet.sync` | the blocking API, a facade over the engine; top level aliases into `sync` |
| `execnet.trio` | trio-native `AsyncGroup`/`AsyncGateway`/`AsyncChannel`, awaited in your own `trio.run` |
| `execnet.aio` | the same surface for asyncio, bridged per call onto the host loop |
| `execnet.gevent` | the sync surface with gevent-parking waits |

`trio`/`aio`/`gevent` load lazily via module `__getattr__`, so
`import execnet` does not import an event loop (pinned by
`testing/test_namespaces.py`).

A `Host` is one thread running one Trio loop; there is **one shared host
per process**, `Group(host=...)` to override.  Blocking calls made from
inside a running event loop raise and name `execnet.aio` / `execnet.trio`
— worker-side channels are exempt, since exec'd code may run its own loop.

### The CLI is the launch contract

```
execnet worker  --protocol-stdio | --protocol-fd FD[,FD]
                | --protocol-connect ADDR | --protocol-listen ADDR
                | --protocol-share [--config-fd FD]
                [--stdin/--stdout/--stderr DISPOSITION]
execnet server  [HOST:PORT] [--once]
execnet info
```

Argv names a transport, nothing else.  What the worker *is* — id, profile,
chdir, nice, `env:`, stdio disposition — arrives as one `GATEWAY_CONFIG`
frame on that transport, and the worker answers with one
(`{"ok": true, …}` or `{"ok": false, "error": …}`); see
`_handshake.py`.  `--config-fd` is left only for the Windows `share` blob,
which describes the connection the frame would otherwise arrive on.

`ADDR` is `unix:/path` or `host:port`.  Everything that starts a worker
emits these tokens; there is no second launch path.  `execnet info`
answers JSON — keys `execnet`, `trio`, `python`, `executable`, `platform`,
`protocols` — so provisioning learns a remote's version *before*
connecting.  `protocols` is advisory today: nothing reads it, and it does
not list `share` (see roadmap item 1, which is about this payload).

`transport=socket|stdio` is a spec key; **`socket` is the default for
every worker execnet spawns**, which is why a worker's stdio is free for
the code it runs.

| gateway | handoff |
|---|---|
| popen, POSIX | `pass_fds` + `--protocol-fd` (socketpair) |
| popen, Windows | `socket.share(pid)` + `--protocol-share`, blob on stdin |
| `socket=` / `installvia=` | the same two, server-side; the server hands the accepted socket over without reading it, and the coordinator's config frame reaches the worker on it |
| `ssh=` / `vagrant_ssh=` | `ssh -R` unix socket, worker dials back (`--protocol-connect`); ssh's stdin is closed |
| `via=` | the sub's stdio, relayed over the coordinator's protocol |

`--protocol-listen` has no user today; it is what a trampoline or a
port-forwarded worker would use (see the Kubernetes section of the
roadmap).  ssh on Windows stays on stdio and cannot do otherwise: CPython
has never exposed `AF_UNIX` there (cpython#77589) and Win32-OpenSSH has no
`StreamLocal` forwarding.  `resolve_transport` raises for an impossible
request rather than letting a gateway hang.

### Worker profiles (`profile=`, spelled `execmodel=` before 3.0)

| profile | loop thread | exec'd code runs | channel | extra deps |
|---|---|---|---|---|
| `thread` (default) | side thread | hybrid: the first `remote_exec` claims the worker's main thread, further ones overflow to pool threads | sync | — |
| `trio` | **main thread** | async sources as tasks, one thread total; sync sources rejected | `AsyncChannel` | — |
| `gevent` | side thread | a greenlet per `remote_exec` on a main-thread hub | sync | `execnet[gevent]`, auto-added by uv provisioning |
| ~~`main_thread_only`~~ | deprecated alias for `thread` | | | |

`TrioWorkerExec` is a FIFO admission pump delegating to strategy objects
(`WORKER_EXEC_STRATEGIES`); subinterpreters are a future strategy slot, not
built.  Admission is **bounded** for the thread-shaped strategies
(`exec_capacity()`, half the trio thread limiter) and a request over the
line is refused on its channel, not queued — reported as
`remote_status().execcapacity`, `None` where execs are tasks or greenlets
and cost no thread.  Two ordering rules hold it together: nothing may wait
for an exec by parking a pool thread (that spends the budget it is
rationing), and the slot is released *before* the exec's channel close
goes out (that close is what tells a coordinator at capacity to send the
next request, so `waitclose(); remote_exec()` must not be refused for a
slot already freed).
`AsyncGroup.makegateway` defaults workers to `thread` — the coordinator's
shape does not dictate the worker's.

### File map (src/execnet/)

| file | role |
|---|---|
| `_message.py` / `_serialize.py` | wire protocol + sans-IO `FrameDecoder`; serializer (CHANNEL opcode incl. duck-typed `save_AsyncChannel`) |
| `_handshake.py` | the `GATEWAY_CONFIG` exchange, both directions: blocking for the worker (it runs before there is a loop), async over `ByteStream` for the coordinator |
| `_channel.py` / `_gateway_base.py` / `_errors.py` | sync `Channel`/`ChannelFactory`; `BaseGateway`/`WorkerGateway`; error types |
| `_trio_gateway.py` | **the engine**: `ByteStream` Protocol, `RawChannel`/`AsyncChannel`, `AsyncGateway` (outbound queue of `(frame, on_written)`, `_finalize` hook), `AsyncGroup` (all transports, reapers, bounded terminate), `ThreadedFdStream`, stream/argv helpers |
| `_trio_host.py` | `Host`'s loop thread, `SyncBridgeGateway`, `FacadeAsyncGroup`, `SyncIOHandle`, `RawTunnelStream`, the `GATEWAY_START_*` handlers |
| `_trio_worker.py` | worker entry, `TrioWorkerExec` + exec strategies, `_dup_protocol_fds`, the transports (blocking `connect()` + async `open()`), `_version_refusal` |
| `_boundary.py` / `_portal.py` | the (private) boundary kit: `Wakener`/`Mailbox`/`OneShot`/`Flag`, `LoopPortal` |
| `_host.py` / `_gateway.py` / `_multi.py` | shared `Host`; sync `Gateway`; sync `Group` + `MultiChannel` |
| `sync.py` / `trio.py` / `aio.py` / `gevent.py` | the four public namespaces |
| `_cli.py` / `_socketserver.py` / `_provision.py` | the CLI, `execnet server`, uv provisioning + argv builders |
| `_execmodel.py` | `WORKER_PROFILES`, `resolve_profile`, and the deprecated `ExecModel` xdist shim |
| `_rsync.py` / `_rsync_remote.py` / `_xspec.py` / `_exec_source.py` | rsync, spec parsing, remote_exec source normalization |
| `_trace.py` / `_gevent_support.py` | `EXECNET_DEBUG` tracing; the gevent wait backend's hub plumbing |
| `__main__.py` / `_version.py` | `python -m execnet` → `_cli.main`; the generated version |
| `_shim.py` + `gateway*.py`, `multi.py`, `rsync*.py`, `xspec.py` | the deprecated pre-Trio module names, warning and forwarding |

## Invariants — do not regress

**Protocol and lifecycle**

- Sends from non-loop threads block until the frame hit the OS write (120s
  → `OSError`), so an abrupt `os._exit` cannot drop "sent" data; loop-thread
  sends only enqueue.  All sends go through one portal-posted FIFO.
- After close: `OSError("cannot send (already closed?)")`;
  `trio.RunFinishedError` maps to the same.
- exec admission order == message arrival order (`TrioWorkerExec._pump`).
  Trio shuffles its run batch, so never rely on task-spawn order.
- Channel callbacks run in a threadpool thread driven by a per-channel
  consumer *task*: per-channel order is strict, a slow callback does not
  block the reader, and `waitclose()` still returns only after every
  callback including the endmarker has run.
- `Group.terminate(timeout)` never hangs (~2×timeout bound, issues
  #43/#221).
- Sync blocking waits (send-ack, receive, waitclose, join) stay on
  `threading.Event`/queue so KeyboardInterrupt can interrupt them;
  `portal.run` (KI-deferred) is only for management ops.
- A killed worker is `EOFError` on every transport — a dead peer *resets*
  a socket where a pipe reaches EOF, and the reader maps that.
- **Every `host.start_soon` entry point contains its own failures.**  These
  are tasks on the *root* nursery: an exception leaving one ends `trio.run`
  and takes the process's gateways with it — and in a worker the
  ExceptionGroup prints onto the user's stderr, which is theirs since 3.0.
  `TrioWorkerExec._run_exec` and the socket/via handlers all catch; a new
  entry point owes the same.  The failure that finds this is dull: an exec
  closing its channel after the connection went away.
- **Nothing posted through the portal may raise.**  Trio turns an exception
  from an entry-queue callback into `TrioInternalError` and tears the whole
  run down, so one call losing a race with shutdown takes every gateway in
  the process with it — and tells the user to file a trio bug.  A posted
  callback reports through its `OneShot`/future instead.
- A host that goes away breaks what it served, loudly.  `Host.close()` is
  final (no second loop thread the existing gateways are not on), and
  nothing survives `os.fork()`: the parent loop's token still *accepts*
  work in a child, so `LoopPortal` and `BaseGateway._check_usable` compare
  pids and raise `ForkedResourceError` rather than let the child wait for a
  reply nobody will send.  Recovery after a fork is the child's, explicitly.

**Launch and provisioning**

- No source shipping.  Workers import an installed execnet + trio.
- **The worker config never travels in an argv, local or remote.**  It is a
  `GATEWAY_CONFIG` frame on the protocol stream, the same on every
  transport (`_handshake.py`).  It carries `env:` values, and `/proc` is
  world-readable on the local machine exactly as `ps` is on a remote one.
  Two properties fall out and are pinned by tests: a `via=` intermediary
  never sees the config it relays, and a worker that refuses to serve
  answers *on the wire*, so the reason reaches the caller instead of a
  stderr that may be pointed anywhere.  The one exception is the Windows
  `share` blob, which describes the connection the frame would arrive on.
- **Nothing in `_provision` is called from the loop thread.**  Deciding
  *what* to launch runs subprocesses — an `execnet info` probe of a
  `python=` target (30s timeout) and a dev coordinator's `uv build`
  (seconds, cold) — and reads whole wheels off disk.  Inline, that stalls
  the loop: the caller's own `trio.run` for `execnet.trio`, and for every
  other surface the *shared* host, i.e. every gateway in the process
  including other groups'.  Every call site goes through
  `_trio_gateway.provision_sync`; measured at 0.76s before, pinned by
  `test_provisioning_does_not_stall_the_loop`.  The hop is deliberately
  not `abandon_on_cancel`: the build populates a version-keyed wheel cache
  that a half-written entry would poison for every later gateway.
- **Hand a socket over as a socket, never as an fd.**  Rebuilding one with
  `socket.socket(fileno=fd)` re-derives family/type/proto by querying the
  handle, which PyPy on Windows fails with `WinError 10014`.
- **The Windows `share()` handoff is currently broken on CPython 3.14/3.15**
  and nobody knows why yet.  The worker's `fromshare()` returns a socket
  whose handle its own process rejects (`WSAENOTSOCK`, 10038, raised by
  trio's `setblocking(False)` in `adopt_socket`), so it dies before the
  handshake; 3.10-3.13 on the same runner are fine, and so is every Linux
  job.  First seen in CI run 30685861676 (2026-08-01) after weeks of green
  Windows runs, so treat it as a race that those two jobs' timing exposes
  rather than a version feature.  **Holding the coordinator's copy of the
  socket open until the handshake does not fix it** — that theory (the blob
  is not a socket until the child calls `fromshare()`) was tried and only
  bought a 20s hang, because a worker that dies while we hold the pair open
  produces no EOF.  Needs a Windows box or a CI bisect; do not spend another
  round on a theory that CI can refute in six minutes.

**Failure modes that each cost a debugging round**

- A socket worker that cannot be spawned must not hang the coordinator.
  It is spawned by the *server*, so the exception dies there while the
  coordinator waits for a handshake byte.  A host that cannot hand a
  socket over refuses *before replying with an address* — the last moment
  a reason can reach the coordinator — and a spawn that fails anyway
  closes the connection so the wait ends.
- A failed socket gateway must not kill the gateway it was requested
  through.  It runs as a task on that coordinator's host; letting it
  propagate cost the coordinator too, which is how one unsupported
  gateway became 51 errors.
- `_check_usable` (fork, then event loop) runs *before* the channel-state
  check in `send`/`receive`.  All of them are caller bugs, but which one you
  were told about used to depend on whether the peer had closed yet.
- Anything that warns in a *worker* can livelock a pytest run: a warning
  raised inside pytest's warning-recording hook records a warning.  The
  `execnet.dumps` shim warns once per process for exactly this reason.
