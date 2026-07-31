# Public API review, and what pins us to Trio

Branch `feat/trio-host-thread-io`, after `98eb08d` (the Phase D docs).
Companion to the "Surface review" section in
`handoff-phase-c-worker-axes.md`, which decided the *shape* of the
namespaces; this one takes inventory of what that shape actually publishes
now that the docs freeze it, and asks a second question: **if the host
thread ever ran plain asyncio instead of Trio, what in today's public
surface would we regret having published?**

Nothing here has been applied.  It is a list of decisions, with a
recommendation each.

## 1. The surface as it stands

`execnet` == `execnet.sync` plus `__version__` and `can_send`
(`test_namespaces.py` asserts exactly that):

```
Channel  Gateway  Group  Host  MultiChannel  RSync  XSpec
DataFormatError  DumpError  LoadError  RemoteError  TimeoutError  HostNotFound
default_group  makegateway  set_profile  set_execmodel
can_send  __version__
```

| namespace | exports |
|---|---|
| `execnet.sync` | the list above minus `can_send`/`__version__` |
| `execnet.trio` | `AsyncGroup`, `AsyncGateway`, `AsyncChannel`, `open_gateway`, `XSpec`, the six error types |
| `execnet.aio` | the same, **plus `Host` and `default_host`** |
| `execnet.gevent` | the sync surface, with its own `Group`/`default_group`/`makegateway` |

Class surfaces (public attributes, inherited included):

| class | members |
|---|---|
| `Gateway` | `id`, `remoteaddress`, `remote_exec`, `remote_status`, `newchannel`, `hasreceiver`, `exit`, `join`, `remote_init_threads`\* |
| `Channel` | `send`, `receive`, `setcallback`, `makefile`, `close`, `waitclose`, `isclosed`, `next`, `RemoteError`, `TimeoutError` |
| `Group` | `makegateway`, `remote_exec`, `terminate`, `allocate_id`, `defaultspec`, `host`, `profile`, `set_profile`, `execmodel`\*, `remote_execmodel`\*, `set_execmodel`\* |
| `Host` | `name`, `running`, `close` (context manager) |
| `MultiChannel` | `send_each`, `receive_each`, `make_receive_queue`, `waitclose` |
| `trio.AsyncGateway` | `remote_exec`, `terminate`, `remoteaddress`, `aclose`, `wait_closed`, `closed`, **`open_channel`, `open_raw_channel`, `enqueue_frame`** |
| `aio.AsyncGateway` | `id`, `remoteaddress`, `remote_exec`, `terminate` |
| `*.AsyncChannel` | `send`, `receive`, `send_eof`, `aclose`, `wait_closed`, `isclosed`, `id` |
| `aio.AsyncGroup` | `start`, `aclose`, `makegateway`, `host` |
| `trio.AsyncGroup` | `makegateway`, `terminate` |

\* deprecated.

Non-Python surface, equally public and harder to change once released: the
`execnet` CLI (`worker` / `server` / `info`), the spec keys, and the
`execnet info` JSON.

## 2. What changed since 2.1

**Added**

- Four namespaces (`sync` / `trio` / `aio` / `gevent`); the top level is
  an alias surface over `sync`.
- `execnet.Host`, `Group(host=)`, `Group.host`, `AsyncGroup(host=)`.
- `execnet.can_send` — the supported replacement for probing with
  `dumps`/`DumpError`.
- `set_profile`, `Group(profile=)`, `Group.profile`, `Group.set_profile`.
- Async gateways: `AsyncGroup` / `AsyncGateway` / `AsyncChannel` /
  `open_gateway` on both async namespaces.
- Spec keys `profile=`, `transport=`, `stdin=`, `stdout=`, `stderr=`.
- The `execnet` console script and `python -m execnet`.

**Removed**

- `execnet.dump` / `load` / `loads`; `dumps` survives *only* as the
  undocumented, non-warning xdist probe (`_XDIST_COMPAT`), scheduled for
  deletion once xdist moves to `can_send`.
- `execnet.script.*` (`shell`, `quitserver`, `loop_socketserver`).
- The pre-Trio modules as real modules: `gateway`, `gateway_base`,
  `multi`, `rsync`, `rsync_remote`, `xspec` now warn and forward,
  removal in 3.0.  `gateway_bootstrap`, `gateway_io`, `gateway_socket`
  are simply gone.
- `execnet.portal`, and the `wait=` spec key — both existed only on this
  branch.

**Deprecated**

- `set_execmodel`, `Group.execmodel`, `Group.remote_execmodel`,
  `Group.set_execmodel`, the `execmodel=` spec key (permanent alias),
  the `main_thread_only` profile, `Gateway.remote_init_threads`,
  the `execnet-socketserver` script.

**Behaviour changes that are API in practice**: worker stdio is no longer
swallowed; a blocking call inside a running event loop raises; a killed
worker is `EOFError` on every transport.

## 3. What pins us to Trio

Two different futures get conflated under "run on plain asyncio", and they
have different answers:

- **(A) A non-Trio engine** — the host thread runs `asyncio.run`, or
  execnet drops the hard `trio` dependency.  This is an internals port
  (`_trio_gateway` is the engine); the invariant that protects it is the
  one already recorded: neutral `ByteStream`, sans-IO `FrameDecoder`.
- **(B) A native asyncio surface** — `execnet.aio` runs gateways on the
  *caller's* loop with no host thread at all, symmetric with
  `execnet.trio`.  This one is visible in the public API today.

Ranked by how much it would cost to undo after release:

### 3.1 `execnet info` reports `"trio": "<version>"` — HIGHEST

`_provision.target_has_execnet()` decides whether a `python=` interpreter
can host a worker directly by asking whether `info["trio"]` is non-null.
That is a cross-version contract: a 2.2 coordinator will keep asking a 2.5
worker that question forever, and the answer names our engine.

**Recommend (before release)**: add a neutral key — `"worker": true` or
`"engines": ["trio"]` — have the coordinator prefer it and fall back to
`"trio"` only for a 2.2-vintage remote.  Keep emitting `"trio"`
indefinitely; it costs one line and buys the freedom to answer honestly
from an engine that is not Trio.

### 3.2 `execnet.trio` publishes the engine objects themselves — HIGH

`execnet.trio.AsyncGateway` *is* `_trio_gateway.AsyncGateway`, so
`open_raw_channel`, `enqueue_frame` and `closed` are public API by
accident.  They are the routing layer `_trio_host` and `_trio_worker`
drive; `test_trio_namespace_hides_raw_plumbing` already keeps `RawChannel`
and `ByteStream` out of `__all__`, but the methods that hand them out are
reachable on a documented class.

That is also the shape that makes (A) expensive: an asyncio engine would
have to reproduce those exact methods to keep `execnet.trio` importable.

**Recommend**: underscore-prefix `enqueue_frame` and `open_raw_channel`
(callers are `_trio_host`, `_trio_worker` and `testing/`, all ours), keep
`open_channel` public as the async `newchannel()`, and add a namespace
test that pins `trio.AsyncGateway`'s public method set.  A facade like
`execnet.aio`'s would be cleaner still, but costs an allocation per
channel on the surface whose selling point is that it has none.

### 3.3 `Host` and `default_host` on `execnet.aio` — MEDIUM

Under (B) the asyncio surface has no host thread, so `aio.AsyncGroup(host=)`
and `aio.AsyncGroup.host` become meaningless — and `default_host` is
exported *only* from `aio`, which reads as an asyncio-specific concept when
it is the opposite.

`host=` is honest today and can be deprecated later (a `None` default keeps
every caller working), so it stays.  `default_host()` is different: nobody
needs it.  Isolation is `Host()`, sharing is the default.

**Recommend**: drop `default_host` from `execnet.aio.__all__` (keep the
name importable), and document `host=` as "which host thread serves this
group", i.e. as an implementation-shaped knob rather than part of the
asyncio model.

### 3.4 `execnet.aio.trio` — LOW

`aio.py` imports trio at module level for `trio.CancelScope`, so
`execnet.aio.trio` is the trio module.  Harmless, but under (A) an
asyncio-only install must be able to `import execnet.aio`.

**Recommend**: nothing now; note it as a tripwire for the engine port.

### 3.5 `Host`'s own documentation — LOW

The class is already engine-neutral in name and members (`name`,
`running`, `close`).  Only its docstring says Trio.  That is accurate
today and the right thing to write; it just means (A) is a docs change
here, not an API change.  No action.

## 4. Not about Trio, but found while looking

1. **Deprecated names are in `__all__`.**  `set_execmodel` is advertised
   as supported API by `execnet.__all__` and `execnet.sync.__all__`.
   Recommend removing from both (it stays importable and warning), and
   updating `test_top_level_all_matches_sync_surface`, which compares the
   two lists.
2. **`ExecModel` is still reachable** as `channel.gateway.execmodel`, and
   hands out `RLock`/`Event`/`queue`/`subprocess`/`socket`.  It exists
   solely because xdist's remote builds its test queue on it.  It is the
   one place where a third party holds thread-shaped primitives of ours.
   No action until xdist ports; delete with `_XDIST_COMPAT` in 3.0.
3. **`Gateway.remote_init_threads`** is a no-op that warns.  Delete in
   3.0 with the shims.
4. **Async surfaces still lack** `remote_status()`, `MultiChannel`, group
   iteration and `RSync` (carried over from the Phase C review — still a
   deliberate gap, worth a line in the docs rather than silence).
5. **`Gateway.join`/`exit`** are public lifecycle methods that
   `Group.terminate` supersedes.  Harmless; leave.

## 5. Suggested order

1. `execnet info` neutral capability key — **before 2.2 ships**, because
   it is the only item here that cannot be changed afterwards.
2. `set_execmodel` out of `__all__`; `default_host` out of `aio.__all__`.
   One commit, two test updates.
3. Underscore the two engine methods on `AsyncGateway`, plus the pinning
   test.
4. Everything else: 3.0, or never.
