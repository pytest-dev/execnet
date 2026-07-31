# What landed on `feat/trio-host-thread-io`, and what it taught us

The record, compressed.  Current state is `HANDOFF.md`; open work is
`ROADMAP-3.0.md`.  This file exists for two reasons: commit messages do
not carry the *why*, and several decisions were made, unmade and remade —
the last section says which ones are dead so nobody resurrects them.

## The phases

**A — transports and demolition** (`92969c5`, `48d328c`, `3d1d31e`,
2026-07-24).  `via=` generalized to a `GATEWAY_START_SUB` protocol message
covering popen/python/ssh/vagrant sub-specs; `vagrant_ssh` ported;
`gateway_io`, `gateway_bootstrap`, `Popen2IO`, the thread receiver and
`WorkerGateway.serve` deleted.  `HostNotFound` became a `ConnectionError`
subclass.  This is where the pattern was set: **infra operations are
protocol messages, not `remote_exec`'d source** — possible only because
execnet is now always installed on the worker.

**B — the inversion** (`0eb6cc5`..`4f84335`).  One protocol engine
(`AsyncGateway`), with the sync API as a facade over it; `ProtocolSession`
deleted.  Along the way: transports unified on a neutral stream protocol;
the **sans-IO `FrameDecoder`** (receivers pump bytes, a `feed()` state
machine yields messages) which is what made the frame-native `via` relay
and IO-free tests possible; the two-level `RawChannel` / `AsyncChannel`
model that killed the double-framed `ChannelByteIO` tunnel; and the
namespace split.

Two fixes from B worth keeping in mind:

- The bridge must attach itself to the sync gateway in `__init__`, *before*
  serving starts (`51b9053`).  Otherwise a first message replies through
  the IO stub and kills the fresh gateway — invisible until `pytest -n 12`.
- A stream-closing `aclose` on the via tunnel must feed EOF to its *own*
  reader, or the serve task never finishes and `terminate` hangs.

**Boundary kit — P1..P5** (`2c78416`..`b01d009`, 2026-07-26).  Consumer→loop
was already universal (`LoopPortal.post` on `TrioToken.run_sync_soon`);
loop→consumer was hardwired to threading primitives, which was the only
reason `ExecModel` still existed.  The fix: the loop never blocks and never
knows who listens — it fires a thread-safe wakeup the consumer supplied.
`Wakener` / `Mailbox` / `OneShot` / `Flag` live in the trio-free
`execnet._boundary`.  The sync `Channel` became a genuine facade over
(raw id, mailbox, portal): deserialization moved to the `receive()` call
site, `AsyncGateway._dispatch` became the only router, and `_receivelock`,
the classic `ChannelFactory` dispatch, `WorkerPool` and `Reply` all went.

One fix there was subtle: an unconsumed, remotely-closed `RawChannel` must
stay registered until a consumer claims it.  Call-site deserialization
means a *passed* channel can bind after both its data and its close have
arrived — a payload-loss race that was latent in the async core too.

`ExecModel` survived as a deprecated preset **because pytest-xdist's remote
worker calls `channel.gateway.execmodel.RLock()`**.

**C — worker profiles** (`b2f43c3`..`cbce183`).  The `loop=` × `exec=` axes
framing was dropped in favour of named use-case profiles on one key.
`TrioWorkerExec` became a pure FIFO admission pump over strategy objects.
The pytest-relevant fix landed here too: `Message.GATEWAY_INFO` answers
`rinfo()` from the dispatch loop and chdir/nice/env ship in the worker
config, so coordinator bookkeeping can no longer *claim an exec slot* —
previously an info call could occupy the main thread, and pytest ended up
on a worker thread.

**Callbacks off the loop thread** (2026-07-26).  `setcallback` starts a
per-channel consumer *task* that drains an inbox and runs each callback via
`trio.to_thread` under a bounded limiter, holding a strong reference to the
channel (which replaced the old `_callback_channels` registry — lifecycle
is now the task's).  `waitclose()` waits on `_consumer_done`, so it still
returns only after every callback including the endmarker.

**Surface review** (`7aa17fe`..`e75cd0a`, 2026-07-29).  The public surface
had settled commit by commit and was never looked at whole; doing that
before the docs froze it retired several earlier decisions (see below).
Result: one namespace per concurrency library you drive execnet from, one
shared `Host` per process, `profile=` as the spec key, and blocking calls
inside a running loop raising instead of hanging.

**CLI and transports** (`5710eed`, `a84380f`, `2fc4013`, 2026-07-30).  The
protocol stopped being the worker's stdin/stdout.  `execnet worker` names
the transport and is the launch contract; `execnet info` replaced the
`import execnet, trio` probe.  Three things this fixed, each verified while
doing it: remote `print()` used to go to `/dev/null`; the ssh worker config
carried `env:` values in the remote argv, readable by every user on that
host via `ps`; and the dev-coordinator wheel used to be framed in-band on
the protocol pipe with `head -c N`, where it now travels on its own ssh
connection into a remote cache.

**Windows** (`9413a2e`..`f484d14`).  It had never been tested and every
worker died at startup on `trio.lowlevel.FdStream`, which is POSIX-only;
`ThreadedFdStream` does those reads and writes in the thread pool.
`pass_fds` does not exist there, so popen hands the socket over with
`socket.share(pid)` — the flag in argv, the blob in the config on stdin,
because the pid does not exist until the child is spawned.  The blob is
bound to that one pid and inert to anything else, which beats handle
inheritance (that would need `close_fds=False` and leak every inheritable
handle to the child *and its grandchildren*).

**xdist in CI** (`320c89e`, `17e7c7a`, 2026-07-30).  See "The xdist
contract" in `ROADMAP-3.0.md`.  It found 16 regressions on the first run.

## Lessons that cost a debugging round

- **CI was lying.**  Until `1839800`, CI had executed *zero tests* since
  2026-07-26: `testing/test_ssh_local.py` imported `asyncssh` at module
  level while `tox.ini` carried its own hand-written `deps` list that had
  drifted from the `testing` extra, so collection errored and every job
  reported `2 skipped, 1 error` while looking like an ordinary failure.
  There must never be a second list of test requirements — `tox.ini` uses
  `extras = testing`.  Relatedly, `[tool.uv] default-groups = ["testing"]`
  had been *replacing* uv's `dev` default, so `uv sync` silently
  uninstalled pytest-xdist.
- **Hand a socket over as a socket, not as an fd.**  `socket.socket(fileno=fd)`
  re-derives family/type/proto by querying the handle; PyPy on Windows
  raises `WinError 10014` doing that to a `WSADuplicateSocket` handle.  No
  in-process probe catches it — `share()` and `fromshare()` both work
  in-process.  What cracked it was noticing which test *passed*:
  `popen//transport=socket` was green while the server path failed, which
  isolated the difference to one line.
- **`execnet server :0` reported a port nothing listened on.**  A wildcard
  bind with an ephemeral port gives *each* address family its own random
  port and only the first was reported — and which family comes first is
  platform-dependent (IPv4 on Linux, IPv6 on Windows).
- **A worker warning can livelock a pytest run.**  `execnet.dumps` warned on
  every access and xdist calls it from `serialize_warning_message`, i.e.
  from inside pytest's warning-recording hook: one `DeprecationWarning` in
  a worker recorded a warning that recorded a warning, unbounded.
- **Do not rewrite a caller's spec.**  `makegateway` wrote the normalized
  profile back onto the caller's `XSpec`; xdist reuses one spec object and
  re-reads it to decide whether to prefix again, so it prefixed twice and
  built `execmodel=…//execmodel=…//popen`.  Every crashed-worker-replacement
  test failed.  Filling in a *missing* value is idempotent and fine.
- **A killed worker resets a socket where a pipe reaches EOF** — the reader
  has to map `BrokenResourceError` to `EOFError` (`f484d14`).  Applies to
  `socket=` on POSIX too; only nobody had looked.
- `shlex.quote("~/…")` creates a directory literally named `~`; use
  `"$HOME"`.  And a cached-wheel skip branch must still drain stdin, or the
  coordinator gets EPIPE.
- **Generated source is read as UTF-8** (PEP 3120) regardless of locale;
  `test_basics` wrote it in the locale encoding and one em-dash in
  `_message` broke it off UTF-8 locales.
- Hypothesis found a real serializer bug while stress-testing channels:
  `_save_integral` only bounds-checked the *upper* int4 limit, so an int
  below `-2**31` overflowed `struct.pack('!i', …)` instead of taking the
  long path.
- The doc examples had not been collectable since pytest 7 (a
  `pytest_plugins` line in a non-top-level conftest), which is why so much
  of them had rotted.  `tox -e docs` now runs them as doctests with `-W`.
- The 11 consistent XPASSes were investigated and the `flakytest` marks
  kept deliberately: trio's single-loop dispatch plus FIFO admission makes
  them pass when idle, but `test_gateway_status_busy` (a `_track_start`
  scheduling race) and `test_popen_stderr_tracing` (capfd) still fail under
  sustained load.  To retire the status marks for real, retry-poll for
  `numexecuting == 2` the way those tests already poll for `== 0`.
- The hybrid main-thread claim is **best-effort**, and that surfaced as an
  `-n 12` flake rather than by reasoning: `main_thread_only` serialized, so
  every sequential `remote_exec` got the main thread, whereas `thread`
  releases its claim just after the channel close that lets the coordinator
  send the next request — so an immediate re-exec can rarely land on a pool
  thread.  The *first* request is still deterministic.

## Decisions that were made and then unmade

Do not resurrect these; each was tried on paper or in code and dropped.

| dropped | replaced by | why |
|---|---|---|
| `loop=` / `exec=` spec axes | named worker profiles on one key | use-cases, not axes; the combinations were not all meaningful |
| `wait=` spec key | the namespace you import | it described the *caller's* concurrency library, which the namespace already says |
| a `Wakener` registry (`register_wakener`, lazy backend modules) | a two-branch `make_wakener("thread"\|"gevent")` | exactly two backends exist and there was never a plan to let third parties add event loops |
| `execnet.portal` as public API | private `execnet._portal` + `execnet._boundary` | it published the kit but not the registration hook, so the advertised extension point was unreachable |
| an `AsyncioWakener` + `Mailbox` for `execnet.aio` | a per-call `_HostBridge` (host task + `call_soon_threadsafe` future) | real awaitables over the trio-native objects; simpler and semantically exact.  Cancellation was made real in the surface review |
| `main_thread_only`'s concurrent-exec deadlock guard | nothing | it was a 1s-timeout guard whose window false-fired under CPU contention; the restored hybrid `thread` profile already gives the first exec the main thread |
| `aio.Group` / `Gateway` / `Channel` | `aio.AsyncGroup` / … | matches `execnet.trio`, so swapping the import ports the code |
| `open_popen_gateway` | `open_gateway` | it always accepted any spec |
| one `TrioHost` per `Group` | one shared `Host` per process, `Group(host=)` | a host is a thread and a loop, not something groups need isolated |
| a trampoline process for Windows stdio | `ThreadedFdStream` | see the rejection note in `ROADMAP-3.0.md` |
| an anyio/asyncio *core* port | `execnet.aio` over the trio host | rejected for now; the portability invariants keep the door open |
| eventlet | — | dead, deliberately |
