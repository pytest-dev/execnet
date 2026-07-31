# Handoff: transports off stdio, the CLI, and Windows

For a fresh session on branch `feat/trio-host-thread-io` (draft PR
pytest-dev/execnet#422).  Continues `handoff-phase-c-worker-axes.md`,
which covers the async core and the worker profile axes and is still
accurate for those; nothing here contradicts it.

Run checks with `uv run pytest testing/` and `uv run pre-commit run -a`
(never grep-filter pre-commit output).  Local state at handoff: **552
passed, 66 skipped**, pre-commit clean.

## Read this first: CI was lying

Until `1839800`, **CI had been executing zero tests since 2026-07-26**.
`testing/test_ssh_local.py` imported `asyncssh` at module level while
`tox.ini` carried its own hand-written `deps` list that had drifted from
the `testing` extra — so collection errored and every job reported
`2 skipped, 1 error` while looking like an ordinary failure.

Two lessons worth keeping:

- `tox.ini` now uses `extras = testing`; there must never be a second
  list of test requirements.
- `[tool.uv] default-groups = ["testing"]` had been *replacing* uv's
  `dev` default, so `uv sync` silently uninstalled pytest-xdist.  The
  `dev` group now absorbs `execnet[testing]` plus the tooling.

Turning the suite back on is what surfaced almost everything below.
Assume any platform CI has not actually exercised is broken.

## What landed this session

| commit | what |
|---|---|
| `320c89e`, `17e7c7a` | run pytest-xdist's own suite against this execnet: a pinned `release` target that blocks, a floating `default-branch` target that may fail |
| `5710eed` | the `execnet` CLI — it is now the launch contract |
| `a84380f` | ssh dial-back over `ssh -R`, popen over a socketpair |
| `e43db63` | one typed endmarker sentinel |
| `1839800` | the CI fix above |
| `cd42ce9` | `EXECNET_PROVISION_WHEEL` |
| `9413a2e` … `f484d14` | Windows: eight commits, see below |

## The CLI is the launch contract

```
execnet worker  --protocol-stdio | --protocol-fd FD[,FD]
                | --protocol-connect ADDR | --protocol-listen ADDR
                | --protocol-share
                --config JSON | --config-fd FD | --config-file PATH
                --stdin/--stdout/--stderr DISPOSITION
execnet server  [HOST:PORT] [--once]
execnet info
```

Everything that starts a worker emits these tokens; there is no second
launch path.  `execnet info` (JSON: version, trio, executable, platform,
protocols) replaced the `import execnet, trio` probe, so provisioning
learns the remote version *before* connecting.

**Config off argv**: `--config-fd 0` exists because the config carries
`env:` values and a remote argv is readable via `ps` by every user on
that host.  ssh uses it.  Do not regress this to `--config`.

## Transports

`transport=socket|stdio`.  **`socket` is now the default for every worker
execnet spawns**, on both platforms (`2fc4013`).  The worker's stdio is
then untouched, so remote `print()` reaches the coordinator.

| gateway | handoff | notes |
|---|---|---|
| popen, POSIX | `pass_fds` + `--protocol-fd` | socketpair |
| popen, Windows | `socket.share()` + `--protocol-share` | see below |
| `socket=`/`installvia=` | same two, server-side | server accepts, then hands over |
| `ssh=`/`vagrant_ssh=` | `ssh -R` unix socket, worker dials back | POSIX only |

ssh on Windows stays on stdio and **cannot** do otherwise: CPython has
never exposed `AF_UNIX` there (cpython#77589) and Win32-OpenSSH does not
implement `StreamLocal` forwarding.  `resolve_transport` raises for an
impossible request rather than letting the gateway hang.

### The share transport (Windows)

`subprocess` refuses `pass_fds` on Windows.  `socket.share(pid)`
(`WSADuplicateSocket`) duplicates the socket into a named pid instead —
but that needs the pid, which does not exist until the child is spawned.
Hence: **the flag goes in argv, the blob follows in the config on
stdin**.  The blob is bound to that one pid, so it is inert to anything
else; this beats handle inheritance, which would need `close_fds=False`
and leak every inheritable handle to the child *and its grandchildren*.

**The rule that took four CI rounds to learn: hand over a socket as a
socket, never as an fd.**  Rebuilding one with `socket.socket(fileno=fd)`
makes the constructor re-derive family/type/proto by querying the handle,
and PyPy on Windows raises `WinError 10014` doing that to a handle from
`WSADuplicateSocket`.  Both sites were wrong; both are fixed
(`5365105`, `f05f66d`).  `adopt_socket` now takes either.

No in-process probe can catch this: `share()` and `fromshare()` both work
in-process on PyPy.  What cracked it was noticing which test *passed* —
`popen//transport=socket` was green while the server path failed, which
isolated the difference to one line.

## Windows, which had never been tested

Every Windows worker died at startup on `trio.lowlevel.FdStream`, which
is POSIX-only.  Trio has Windows pipe streams but they need OVERLAPPED
handles registered with an IOCP, and inherited stdio is an ordinary
synchronous pipe — so `ThreadedFdStream` (`_trio_gateway.py`) does those
reads and writes in the thread pool.  It is now only reachable via
explicit `transport=stdio`.

Latent bugs that Linux was hiding, all found once Windows ran:

- **`execnet server :0` reported a port nothing listened on.**  A
  wildcard bind with an ephemeral port gives *each* address family its
  own random port (trio documents this) and only the first was reported.
  Which family comes first is platform-dependent — IPv4 on Linux, IPv6
  on Windows.  `_socketserver._one_port` re-binds them to one port.
- **`test_basics` wrote generated source in the locale encoding.**
  Python reads source as UTF-8 (PEP 3120); one em-dash in `_message` was
  enough to break it off UTF-8 locales.
- **A killed worker reported `BrokenResourceError`, not `EOFError`**, on
  a socket: a dead peer *resets* a socket where a pipe reaches EOF.  The
  reader maps it (`f484d14`).  Applies to `socket=` on POSIX too.
- **`test__rinfo` raced**: `receive()` returns when the *send* arrives,
  and the `os.chdir('..')` after it had not run yet.

## Failure modes to preserve

These were all real, and each cost a debugging round:

- **A socket worker that cannot be spawned must not hang the
  coordinator.**  It is spawned by the *server*, so the exception dies
  there while the coordinator waits for a handshake byte.  A host that
  cannot hand a socket over refuses *before replying with an address* —
  the last moment a reason can reach the coordinator — and a spawn that
  fails anyway closes the connection so the wait ends.
- **A failed socket gateway must not kill the gateway it was requested
  through.**  It runs as a task on that coordinator's host; letting it
  propagate cost the coordinator too, which is how one unsupported
  gateway became 51 errors.
- **`_check_event_loop` runs before the channel-state check** in
  `send`/`receive`.  Both are caller bugs, but which one you were told
  about depended on whether the peer had closed yet.

## Naming

`master` → `coordinator` throughout for the `via=`/`installvia=` gateway
(`3a8f182`): it spawns and relays for the sub-worker, which is what a
coordinator does.  Where one sentence covers both parties, only the
relaying one is named; the requesting side is "here"/"us".

## Open work

1. **`doc/basics.rst` still documents `set_execmodel` /
   `main_thread_only`** as the API, with a whole threading-models
   section.  Stale since the `execmodel=` → `profile=` rename; needs a
   rewrite, not a substitution.  Two doctests in `doc/example/` expected
   `thread model` in a gateway repr and were already broken — fixed, but
   note **docs are not built in CI**, only `tox -e py` runs.  Consider
   adding `tox -e docs` to the workflow.
2. **`installvia` still needs a socket handoff at all.**  It would not,
   if the server spawned the worker as the *listener*
   (`--protocol-listen 127.0.0.1:0`) and reported its address back: no
   `pass_fds`, no `share()`, works on any interpreter, and the spawn
   happens *before* the reply so failures are diagnosable by
   construction.  The open question is how the server learns the port
   (worker prints it, or writes it to a path given in the config).
   Standalone `execnet server` still needs the handoff — it has already
   accepted the connection.
3. **A trampoline process was considered and rejected** — see the
   analysis: the current design already frees the worker's stdio
   in-process (`_dup_protocol_fds` + `apply_stdio`), and a pump's own
   stdio side is still a blocking pipe, so it relocates the thread
   rather than removing it, at the cost of a process and two copies per
   message.  Do not revisit without a new reason.
4. `execnet.anyio` remains deferred (Phase E).

## Invariants (do not regress)

- No source shipping, ever.  Workers import installed execnet+trio.
- The worker config never travels in a remote argv.
- `import execnet` must not import the trio event loop.
- Hand sockets over as sockets, not fds.
- Keep async-core idioms anyio-portable (neutral `ByteStream`, sans-IO
  `FrameDecoder`).
