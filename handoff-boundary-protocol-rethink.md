# Handoff: host boundary protocol rethink (P1–P5, precedes Phase C proper)

Decided with Ronny 2026-07-26.  This supersedes the *ordering* of
`handoff-phase-c-worker-axes.md` (still valid for C content): the boundary
rethink lands first, then C's `loop=`/`exec=` work continues on the
smaller core.  The wire protocol (Message framing) is untouched — this is
about how things cross between the trio host loop and its consumers.

## Rationale

Consumer→loop is already universal: `LoopPortal.post` rides
`TrioToken.run_sync_soon`, thread-safe from any foreign context (plain
thread, gevent greenlet, asyncio callback, another trio loop).  One
ingress, strict FIFO — unchanged.

Loop→consumer is hardwired to threading primitives (`Channel._items`
execmodel queue, `threading.Event` for waitclose/join/write-acks,
`SyncReceiver`), which is the only reason `ExecModel` still exists.  The
fix: the loop never blocks and never knows who listens — it fires a
thread-safe wakeup callable the consumer supplied.  Every event loop has
exactly one such primitive:

| consumer | wakeup primitive |
|---|---|
| plain thread | `threading.Event.set` |
| asyncio | `loop.call_soon_threadsafe` |
| gevent | `hub.loop.async_()` watcher — `send()` is gevent's one documented thread-safe op |
| foreign trio loop | `TrioToken.run_sync_soon` |

## The boundary kit (grows in `execnet.portal`)

1. **`Wakener`** — protocol, one thread-safe method `notify()`.  The
   entire integration surface for a new event loop (eventlet, Qt, …)
   is implementing this; no more ExecModel.
2. **`Mailbox[T]`** — unbounded deque + Wakener.  `put()` from the loop
   (append + notify, never blocks the loop); `get(timeout)` blocking for
   sync backends — generalizes the KI-safe `SyncReceiver` drain pattern
   (Event.wait + drain, re-check after clear) — and `await`-able for
   asyncio/trio wakeners.  Replaces `Channel._items`, `SyncReceiver`,
   `MultiChannel`'s queue.
3. **`OneShot[T]`** — single-result future on the same wakener.
   Replaces the per-send write-ack `threading.Event`; adds
   `portal.start(async_fn) -> OneShot` so asyncio callers await
   management ops instead of blocking a thread in `portal.run`.

## Channel unification (decided: full rebase)

`AsyncGateway._dispatch` becomes the ONLY message router.  `RawChannel`
grows a consumer hook: a bound facade diverts payloads into a `Mailbox`
instead of the internal memory channel (callbacks: invoked on the loop
thread, preserving current semantics; moving them later is just posting
via the wakener).

Sync `Channel` becomes a genuine facade over (raw id, mailbox, portal):

- `receive()` = `mailbox.get(timeout)` + `loads_internal` at the call
  site — deserialization moves off the loop thread; `DataFormatError`
  now surfaces at `receive()`.
- `send()` = portal-posted frame + `OneShot` write-ack (non-loop threads
  keep block-until-written, 120s → OSError, KI-safe).
- `waitclose()`/`join()` = closed-events on the wakener.
- `__del__` best-effort close keeps `portal.post` (post_message path).
- Unknown inbound ids still auto-create (`_channel_for`).

Deleted outright: `_receivelock`, `ChannelFactory` dispatch paths
(`_local_receive`/`_local_close`), `SyncBridgeGateway._dispatch`, the
`Message.received` handler-table use, the duplicate close state machine.
`CHANNEL_EXEC`/`GATEWAY_START_*` become per-gateway hooks on the async
core.  `TrioWorkerExec` FIFO pump survives unchanged; exec'd code gets
facade channels.

## Execmodels: retired as machinery, preserved as presets

execmodel names become presets over three orthogonal axes (spec + worker
CLI config; decided: **both** coordinator and worker side):

| axis | values | meaning |
|---|---|---|
| `loop` | `main` \| `thread` | where the trio host runs (Phase C) |
| `exec` | `thread` \| `main` \| `task` | where remote_exec code runs (Phase C) |
| `wait` | `thread` \| `gevent` \| `asyncio` | which Wakener blocking facade waits park on (new) |

Presets: `execmodel=thread` → `loop=main, exec=thread, wait=thread`;
`main_thread_only` → `loop=thread, exec=main, wait=thread`; `gevent`
returns as `loop=thread, exec=thread, wait=gevent` (greenlet channel
waits park the greenlet, not the hub; sends already greenlet-safe via
the portal).  eventlet stays dead (third parties can implement Wakener).

Dies: `ExecModel` ABC, `WorkerPool`, `Reply` (`multi.safe_terminate`
moves to plain threads or delegates to `FacadeAsyncGroup.terminate`).
Stays as deprecated preset-mappers: `get_execmodel`, `set_execmodel`,
`group.execmodel`, `spec.execmodel` (xdist compat).

## asyncio (decided: land now)

`execnet.aio` namespace mirroring `execnet.trio`'s surface; every op is
portal ingress + `OneShot`/`Mailbox` on an `AsyncioWakener`.  Full
asyncio-native API over the trio host loop, all transports, no anyio
port.  Phase E (anyio-native core) becomes optional purity/perf work.

## Staging — ALL LANDED 2026-07-26 (commits 2c78416..b01d009)

1. **P1 — boundary kit** (`2c78416`): Wakener/Mailbox/OneShot +
   ThreadWakener; SyncReceiver, write-acks, `_done_sync` ported.
2. **P2 — channel unification** (`2a40a55`): sync Channel onto
   RawChannel(+set_consumer)+Mailbox, classic dispatch deleted.  The kit
   lives in trio-free `execnet._boundary` (portal re-exports) so
   `import execnet` stays trio-free.  Fix worth knowing: an unconsumed
   remotely-closed RawChannel stays registered until a consumer claims
   it — call-site deserialization means a passed channel can bind after
   its data AND close arrived (was a payload-loss race, latent in the
   async core's `open_channel(id)` too).
3. **P3 — retirement** (`811fd99`): ExecModel ABC/WorkerPool/Reply gone;
   one deprecated ExecModel preset KEEPS the stdlib-delegate members
   because pytest-xdist's remote worker calls
   `channel.gateway.execmodel.RLock()/Event()`; `wait=` axis end to end
   (spec validation, worker CLI config, `BaseGateway._new_wakener`,
   `_boundary` registry + Flag); safe_terminate on daemon threads;
   test_threadpool.py deleted.
4. **P4 — `execnet.aio`** (`845510e`): implemented as a per-call
   _HostBridge (host task + `loop.call_soon_threadsafe` future), NOT the
   originally sketched AsyncioWakener/Mailbox — real awaitables over the
   trio-native AsyncGroup/AsyncChannel, simpler and semantically exact.
   The "asyncio" wait= registry entry is therefore unused/unregistered.
   v1 limitation: cancelling a bridged await abandons it aio-side only.
5. **P5 — gevent wakener** (`b01d009`): `execnet._gevent_support`,
   lazily imported by `make_wakener("gevent")`; hub-bound pieces created
   in the first waiter's hub (carriers may be constructed on the host
   loop); tests behind the `gevent` dependency group.

Next: Phase C `loop=main` / `exec=task` on the smaller core
(handoff-phase-c-worker-axes.md), and Phase D docs cover the four
namespaces + the axes/presets.

## Invariants (unchanged, re-mapped)

- Non-loop-thread sends block until the frame hit the OS write (OneShot
  write-ack; 120s → OSError); loop-thread sends only enqueue; all sends
  one portal FIFO.
- After close: `OSError("cannot send (already closed?)")`.
- exec admission order = message arrival order (TrioWorkerExec._pump).
- Channel callbacks run on the loop thread (revisit-later stands; the
  kit makes moving them cheap).
- `Group.terminate(timeout)` never hangs (~2×timeout).
- Sync blocking waits stay KeyboardInterrupt-interruptible on the main
  thread (thread Wakener keeps the Event.wait + drain pattern).
- No source shipping; `import execnet` must not import trio.
