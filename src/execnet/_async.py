"""The async vocabulary the protocol core is written against.

The core needs about a dozen things from whatever async library it is
running on: a task scope, a way to shield cleanup, two kinds of deadline,
an event, a limiter, an unbounded queue, a thread hop, and a handful of
exception types.  Both trio and asyncio can provide all of them; naming
them here once is what lets one implementation of the core run on either.

Which one you get is decided by the loop you are already in --
:func:`current_async` -- and captured by objects at construction, so the
detection is not paid per call.

**The two do not agree about cancellation, and this is where that is
handled.**  Trio is level-triggered: inside a cancelled scope every later
``await`` raises again, so cleanup needs an explicit shield.  asyncio is
edge-triggered: a cancel is delivered once, and cleanup after catching it
simply runs.  So :meth:`AsyncLib.shielded` is a real cancel scope on trio
and a no-op on asyncio -- both spell "this cleanup completes".  What
asyncio cannot promise is completion against a *second* cancel, which in
execnet only the engine's own shutdown can send; it sends one, then waits
out a grace, which is what makes the two equivalent in practice.

Everything here is a context manager or a small object, and the trio side
returns trio's own objects wherever it can, so the abstraction costs
nothing on the path that has always existed.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from contextlib import contextmanager
from types import TracebackType
from typing import TYPE_CHECKING
from typing import Any
from typing import TypeVar

if TYPE_CHECKING:
    from collections.abc import Iterator

    from typing_extensions import Self

T = TypeVar("T")

#: the smallest Python the asyncio backend runs on: ``TaskGroup``,
#: ``BaseExceptionGroup`` and ``Task.uncancel`` are all 3.11.
MIN_ASYNCIO_PYTHON = (3, 11)


class AsyncLibUnavailable(RuntimeError):
    """This interpreter cannot run the requested async backend."""


# -- trio -------------------------------------------------------------------


class TrioAsync:
    """The vocabulary on trio, which mostly means trio's own objects."""

    name = "trio"

    def __init__(self) -> None:
        import trio

        self._trio = trio
        self.Cancelled = trio.Cancelled
        self.BrokenResource = trio.BrokenResourceError
        self.ClosedResource = trio.ClosedResourceError
        self.TooSlow = trio.TooSlowError
        #: a stream that is gone, either end
        self.STREAM_GONE = (trio.BrokenResourceError, trio.ClosedResourceError)
        #: an inbox with nothing more coming
        self.CHANNEL_EMPTY = (trio.EndOfChannel, trio.ClosedResourceError)
        #: ...plus "nothing right now", for the drain loops
        self.CHANNEL_UNUSABLE = (
            trio.WouldBlock,
            trio.EndOfChannel,
            trio.ClosedResourceError,
        )

    def event(self) -> Any:
        return self._trio.Event()

    def limiter(self, total: int) -> Any:
        return self._trio.CapacityLimiter(total)

    def thread_budget(self) -> int:
        """How many threads this loop will run at once."""
        limiter = self._trio.to_thread.current_default_thread_limiter()
        return int(limiter.total_tokens)

    def queue(self) -> tuple[Any, Any]:
        return self._trio.open_memory_channel[Any](float("inf"))

    def task_scope(self) -> Any:
        return _TrioTaskScope(self._trio)

    @contextmanager
    def shielded(self) -> Iterator[None]:
        with self._trio.CancelScope(shield=True):
            yield

    def move_on_after(self, seconds: float) -> Any:
        return self._trio.move_on_after(seconds)

    def fail_after(self, seconds: float) -> Any:
        return self._trio.fail_after(seconds)

    async def to_thread(
        self,
        fn: Callable[..., T],
        *args: Any,
        limiter: Any = None,
        abandon_on_cancel: bool = False,
    ) -> T:
        result: T = await self._trio.to_thread.run_sync(
            fn, *args, limiter=limiter, abandon_on_cancel=abandon_on_cancel
        )
        return result

    async def checkpoint(self) -> None:
        await self._trio.lowlevel.checkpoint()

    async def sleep_forever(self) -> None:
        await self._trio.sleep_forever()

    # -- IO: the streams, processes and listeners the transports build --

    async def open_process(self, argv: list[str], **kwargs: Any) -> Any:
        return await self._trio.lowlevel.open_process(argv, **kwargs)

    def staple_process(self, process: Any) -> Any:
        return self._trio.StapledStream(process.stdin, process.stdout)

    async def wrap_socket(self, sock: Any) -> Any:
        return self._trio.SocketStream(self._trio.socket.from_stdlib_socket(sock))

    async def staple_fds(self, read_fd: int, write_fd: int) -> Any:
        return self._trio.StapledStream(
            self._trio.lowlevel.FdStream(write_fd),
            self._trio.lowlevel.FdStream(read_fd),
        )

    async def open_tcp_stream(self, host: str, port: int) -> Any:
        return await self._trio.open_tcp_stream(host, port)

    async def open_tcp_listeners(self, port: int, host: str | None = None) -> Any:
        return await self._trio.open_tcp_listeners(port, host=host)

    async def unix_listener(self, path: str) -> Any:
        sock = self._trio.socket.socket(
            self._trio.socket.AF_UNIX, self._trio.socket.SOCK_STREAM
        )
        await sock.bind(path)
        sock.listen(1)
        return self._trio.SocketListener(sock)

    async def serve_listeners(self, handler: Any, listeners: Any) -> None:
        await self._trio.serve_listeners(handler, listeners)


class _TrioTaskScope:
    """A nursery, behind the neutral name."""

    def __init__(self, trio_module: Any) -> None:
        self._trio = trio_module
        self._manager: Any = None
        self._nursery: Any = None

    async def __aenter__(self) -> Self:
        self._manager = self._trio.open_nursery()
        self._nursery = await self._manager.__aenter__()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None:
        manager, self._manager = self._manager, None
        self._nursery = None
        exited: bool | None = await manager.__aexit__(exc_type, exc_value, traceback)
        return exited

    def start_soon(self, async_fn: Callable[..., Any], *args: Any) -> None:
        self._nursery.start_soon(async_fn, *args)

    async def start(self, async_fn: Callable[..., Any], *args: Any) -> Any:
        return await self._nursery.start(async_fn, *args)

    def cancel(self) -> None:
        self._nursery.cancel_scope.cancel()


# -- asyncio ----------------------------------------------------------------


class AsyncioAsync:
    """The same vocabulary on asyncio.

    Every difference from trio that the core can see is confined to this
    class and the small objects below it.
    """

    name = "asyncio"

    def __init__(self) -> None:
        if sys.version_info < MIN_ASYNCIO_PYTHON:
            want = ".".join(str(part) for part in MIN_ASYNCIO_PYTHON)
            raise AsyncLibUnavailable(
                f"the asyncio backend needs Python {want} or newer:"
                " it is written against asyncio.TaskGroup, asyncio.timeout"
                " and Task.uncancel, and execnet carries no backport."
            )
        import asyncio

        self._asyncio = asyncio
        self.Cancelled = asyncio.CancelledError
        self.BrokenResource = BrokenResource
        self.ClosedResource = ClosedResource
        self.TooSlow = TimeoutError
        self.STREAM_GONE = (BrokenResource, ClosedResource, ConnectionError)
        self.CHANNEL_EMPTY = (EndOfChannel, ClosedResource)
        self.CHANNEL_UNUSABLE = (WouldBlock, EndOfChannel, ClosedResource)

    def event(self) -> Any:
        return self._asyncio.Event()

    def limiter(self, total: int) -> Any:
        return _Limiter(self._asyncio.Semaphore(total), total)

    def thread_budget(self) -> int:
        """How many threads this loop will run at once.

        asyncio builds its default executor lazily, so before the first
        ``to_thread`` there is nothing to read and this falls back to the
        same default CPython would have chosen.  Where execnet owns the loop
        the engine installs an executor of a known size, and then this is
        simply that number.
        """
        import os

        executor = getattr(self._asyncio.get_running_loop(), "_default_executor", None)
        workers = getattr(executor, "_max_workers", None)
        if workers is None:
            workers = min(32, (os.cpu_count() or 1) + 4)
        return int(workers)

    def queue(self) -> tuple[Any, Any]:
        shared = _Inbox(self._asyncio)
        return _InboxSender(shared), _InboxReceiver(shared)

    def task_scope(self) -> Any:
        return _AsyncioTaskScope(self._asyncio)

    @contextmanager
    def shielded(self) -> Iterator[None]:
        # Nothing to do: asyncio delivers a cancel once, so the cleanup this
        # wraps runs on its own.  The name stays for the reader, and for the
        # trio side where the shield is mandatory.
        yield

    def move_on_after(self, seconds: float) -> Any:
        return _Deadline(self._asyncio, seconds, raising=False)

    def fail_after(self, seconds: float) -> Any:
        return _Deadline(self._asyncio, seconds, raising=True)

    async def to_thread(
        self,
        fn: Callable[..., T],
        *args: Any,
        limiter: Any = None,
        abandon_on_cancel: bool = False,
    ) -> T:
        # asyncio.to_thread is always abandon-on-cancel: a cancelled caller
        # stops waiting and the thread runs on.  Every execnet caller that
        # names the flag asks for exactly that.
        if limiter is None:
            result: T = await self._asyncio.to_thread(fn, *args)
            return result
        async with limiter:
            return await self._asyncio.to_thread(fn, *args)

    async def checkpoint(self) -> None:
        await self._asyncio.sleep(0)

    async def sleep_forever(self) -> None:
        await self._asyncio.Event().wait()

    # -- IO: see :mod:`execnet._aio_io` for the implementations --

    async def open_process(self, argv: list[str], **kwargs: Any) -> Any:
        from ._aio_io import open_process

        return await open_process(argv, **kwargs)

    def staple_process(self, process: Any) -> Any:
        from ._aio_io import staple_process

        return staple_process(process)

    async def wrap_socket(self, sock: Any) -> Any:
        from ._aio_io import wrap_socket

        return await wrap_socket(sock)

    async def staple_fds(self, read_fd: int, write_fd: int) -> Any:
        from ._aio_io import staple_fds

        return await staple_fds(read_fd, write_fd)

    async def open_tcp_stream(self, host: str, port: int) -> Any:
        from ._aio_io import open_tcp_stream

        return await open_tcp_stream(host, port)

    async def open_tcp_listeners(self, port: int, host: str | None = None) -> Any:
        from ._aio_io import open_tcp_listeners

        return await open_tcp_listeners(port, host=host)

    async def unix_listener(self, path: str) -> Any:
        from ._aio_io import unix_listener

        return await unix_listener(path)

    async def serve_listeners(self, handler: Any, listeners: Any) -> None:
        from ._aio_io import serve_listeners

        await serve_listeners(handler, listeners)


class ClosedResource(Exception):
    """This end was closed locally (asyncio's spelling of trio's)."""


class BrokenResource(Exception):
    """The other end went away (asyncio's spelling of trio's)."""


class EndOfChannel(Exception):
    """Nothing more is coming on this inbox."""


class WouldBlock(Exception):
    """Nothing available right now."""


class _Shutdown(BaseException):
    """Raised out of a TaskGroup body to cancel every child."""


class _Limiter:
    """A semaphore that also reports its size, like trio's CapacityLimiter."""

    def __init__(self, semaphore: Any, total: int) -> None:
        self._semaphore = semaphore
        self.total_tokens = total

    async def __aenter__(self) -> None:
        await self._semaphore.acquire()

    async def __aexit__(self, *exc_info: object) -> None:
        self._semaphore.release()


class _Deadline:
    """``move_on_after`` / ``fail_after`` as a *synchronous* context manager.

    ``asyncio.timeout`` is an async context manager, which would make every
    deadline in the core read differently from trio's.  Nothing it does on
    entry or exit actually needs to await, so this does the same work
    synchronously: arm a timer that cancels the current task, and on the way
    out decide whether the cancellation that arrived was ours.

    That decision is the delicate part, and it follows CPython's own
    ``asyncio.timeouts`` exactly: remember how many cancellations the task
    had been asked for on entry, and only swallow one if ``uncancel()``
    brings the count back to that number.  Anything more means an outer
    cancel is also in flight and must not be eaten.
    """

    def __init__(self, asyncio_module: Any, seconds: float, *, raising: bool) -> None:
        self._asyncio = asyncio_module
        self._seconds = seconds
        self._raising = raising
        self._task: Any = None
        self._handle: Any = None
        self._cancelling = 0
        self._expired = False
        #: mirrors ``trio.CancelScope.cancelled_caught``
        self.cancelled_caught = False

    def _fire(self) -> None:
        self._expired = True
        self._task.cancel()

    def __enter__(self) -> Self:
        self._task = self._asyncio.current_task()
        self._cancelling = self._task.cancelling()
        loop = self._asyncio.get_running_loop()
        self._handle = loop.call_later(self._seconds, self._fire)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        self._handle.cancel()
        if not self._expired or exc_type is None:
            return False
        if not issubclass(exc_type, self._asyncio.CancelledError):
            return False
        if self._task.uncancel() > self._cancelling:
            # an outer cancellation is in flight too; it is not ours to eat
            return False
        self.cancelled_caught = True
        if self._raising:
            raise TimeoutError(f"no result within {self._seconds} seconds") from None
        return True


class _AsyncioTaskScope:
    """A ``TaskGroup`` with trio's two extras: ``start`` and ``cancel``."""

    def __init__(self, asyncio_module: Any) -> None:
        self._asyncio = asyncio_module
        self._group: Any = None

    async def __aenter__(self) -> Self:
        self._group = self._asyncio.TaskGroup()
        await self._group.__aenter__()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        group, self._group = self._group, None
        try:
            await group.__aexit__(exc_type, exc_value, traceback)
        except BaseExceptionGroup as raised:  # type: ignore[name-defined]  # noqa: F821
            _, remaining = raised.split(_Shutdown)
            if remaining is not None:
                raise remaining from None
            return True
        return False

    def start_soon(self, async_fn: Callable[..., Any], *args: Any) -> None:
        self._group.create_task(async_fn(*args))

    async def start(self, async_fn: Callable[..., Any], *args: Any) -> Any:
        """Start ``async_fn`` and wait until it reports itself ready.

        ``TaskGroup`` has no equivalent, so the protocol trio uses is built
        here: the task is handed a ``task_status`` to call ``started()`` on.
        A failure *before* that call goes to whoever called ``start`` and
        nowhere else -- trio removes such a task from the nursery, and since
        a ``TaskGroup`` child cannot be un-enrolled the failure is caught
        instead and the task ends quietly.
        """
        status = TaskStatus(self._asyncio)
        self._group.create_task(_until_ready(async_fn, args, status))
        return await status.wait()

    def cancel(self) -> None:
        """Cancel every child, the way ``nursery.cancel_scope.cancel()`` does."""
        self._group.create_task(_raise_shutdown())


async def _raise_shutdown() -> None:
    raise _Shutdown


class TaskStatus:
    """The ``task_status`` object a started task reports through."""

    def __init__(self, asyncio_module: Any) -> None:
        self._event = asyncio_module.Event()
        self._value: Any = None
        self._error: BaseException | None = None

    def started(self, value: Any = None) -> None:
        if self._event.is_set():
            raise RuntimeError("task_status.started() called more than once")
        self._value = value
        self._event.set()

    def fail(self, error: BaseException) -> None:
        self._error = error
        self._event.set()

    def is_set(self) -> bool:
        return bool(self._event.is_set())

    async def wait(self) -> Any:
        await self._event.wait()
        if self._error is not None:
            raise self._error
        return self._value


async def _until_ready(
    async_fn: Callable[..., Any], args: tuple[Any, ...], status: TaskStatus
) -> None:
    try:
        await async_fn(*args, task_status=status)
    except BaseException as exc:
        if not status.is_set():
            status.fail(exc)
            return
        raise
    if not status.is_set():
        status.fail(
            RuntimeError(f"{async_fn!r} ended without calling task_status.started()")
        )


class _Inbox:
    """The shared state behind an unbounded queue pair."""

    def __init__(self, asyncio_module: Any) -> None:
        self.queue = asyncio_module.Queue()
        self.closed = False


class _InboxSender:
    def __init__(self, shared: _Inbox) -> None:
        self._shared = shared

    def send_nowait(self, item: Any) -> None:
        if self._shared.closed:
            raise ClosedResource("inbox is closed")
        self._shared.queue.put_nowait(item)

    def close(self) -> None:
        if self._shared.closed:
            return
        self._shared.closed = True
        # wake a waiting receiver so it can see the close
        self._shared.queue.put_nowait(_EOF)


class _InboxReceiver:
    def __init__(self, shared: _Inbox) -> None:
        self._shared = shared

    def _unwrap(self, item: Any) -> Any:
        if item is _EOF:
            # put it back: every later receive must see the end too
            self._shared.queue.put_nowait(_EOF)
            raise EndOfChannel("inbox is closed")
        return item

    def receive_nowait(self) -> Any:
        try:
            item = self._shared.queue.get_nowait()
        except Exception as exc:  # asyncio.QueueEmpty
            raise WouldBlock("nothing available") from exc
        return self._unwrap(item)

    async def receive(self) -> Any:
        return self._unwrap(await self._shared.queue.get())


#: the end-of-inbox marker; a private object so no payload can be mistaken for it
_EOF = object()


# -- picking one ------------------------------------------------------------

_TRIO: TrioAsync | None = None
_ASYNCIO: AsyncioAsync | None = None


def for_backend(name: str) -> Any:
    """The vocabulary for a named backend, built once per process."""
    global _TRIO, _ASYNCIO
    if name == "trio":
        if _TRIO is None:
            _TRIO = TrioAsync()
        return _TRIO
    if name == "asyncio":
        if _ASYNCIO is None:
            _ASYNCIO = AsyncioAsync()
        return _ASYNCIO
    raise ValueError(f"unknown async backend {name!r}")


def current_async() -> Any:
    """The vocabulary for the loop running in *this* thread.

    Objects capture this once, at construction, so nothing pays for the
    detection per operation.
    """
    trio = sys.modules.get("trio")
    if trio is not None:
        try:
            trio.lowlevel.current_trio_token()
        except RuntimeError:
            pass
        else:
            return for_backend("trio")
    asyncio = sys.modules.get("asyncio")
    if asyncio is not None:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            return for_backend("asyncio")
    raise RuntimeError(
        "no running event loop: execnet's protocol core has to be built"
        " inside the loop it will run on"
    )
