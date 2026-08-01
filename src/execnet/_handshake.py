"""The worker handshake: the config frame, and the reply to it.

Every worker execnet starts is configured the same way, over the protocol
stream it was given and before the Message protocol proper begins:

1. the coordinator sends one ``GATEWAY_CONFIG`` frame carrying the worker
   config as JSON;
2. the worker applies it (version check, ``chdir``/``nice``/``env``, stdio
   disposition) and answers with a ``GATEWAY_CONFIG`` frame of its own --
   ``{"ok": true, ...}`` when it is about to serve, ``{"ok": false,
   "error": ...}`` when it refuses;
3. both sides start framing normally on the same stream.

The config travels here rather than in argv because it carries ``env:``
values and ``/proc`` (like ``ps``) is world-readable -- to every user on
the machine, not only on remote ones.  It is a *frame* rather than a bare
line so a refusal has somewhere to go: a worker that will not serve says
why on the wire, instead of dying to a stderr nobody is reading and
leaving the coordinator to infer it from an exit status.

Two directions, deliberately in one module so they cannot drift.  The
worker's side is blocking and runs before it has an event loop -- which is
what lets the config decide the worker's *shape* (``profile=trio`` has no
side thread to read it on).  The coordinator's side is async and speaks
:class:`~execnet._trio_gateway.ByteStream`, without importing trio.
"""

from __future__ import annotations

import json
from typing import Any
from typing import Protocol

from ._message import Message

__all__ = [
    "BlockingChannel",
    "ConfigRefused",
    "read_config_frame",
    "read_ready",
    "send_config",
    "send_ready_frame",
]


class ConfigRefused(Exception):
    """The worker read its config and declined to serve."""


class BlockingChannel(Protocol):
    """The worker's pre-loop view of its protocol stream.

    Deliberately not fds: a socket handed to a Windows worker by
    ``socket.share()`` has no usable fd there, and ``os.read`` on it fails.
    Each transport supplies whichever pair of primitives it actually has.
    """

    def recv(self, max_bytes: int, /) -> bytes: ...

    def sendall(self, data: bytes, /) -> None: ...


def _recv_exactly(channel: BlockingChannel, count: int) -> bytes:
    """Read exactly ``count`` bytes, never one more.

    Over-reading is not an option: whatever follows the handshake frame is
    the peer's first protocol frames, and they belong to the loop that has
    not started yet.
    """
    chunks: list[bytes] = []
    remaining = count
    while remaining:
        chunk = channel.recv(remaining)
        if not chunk:
            raise EOFError(
                f"connection closed during the worker handshake "
                f"({count - remaining} of {count} bytes)"
            )
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _read_frame_blocking(channel: BlockingChannel) -> Message:
    header = _recv_exactly(channel, 9)
    msgcode, channelid, payload_len = Message.from_header(header)
    payload = _recv_exactly(channel, payload_len) if payload_len else b""
    return Message(msgcode, channelid, payload)


def _config_frame(payload: dict[str, Any]) -> bytes:
    return Message(
        Message.GATEWAY_CONFIG, 0, json.dumps(payload).encode("utf-8")
    ).pack()


def _decode(message: Message, what: str) -> dict[str, Any]:
    if message.msgcode != Message.GATEWAY_CONFIG:
        raise EOFError(f"expected a {what} frame, got {message!r}")
    payload = json.loads(message.data)
    if not isinstance(payload, dict):
        raise EOFError(f"{what} is not an object: {payload!r}")
    return payload


# -- worker side (blocking, before there is a loop) --


def read_config_frame(channel: BlockingChannel) -> dict[str, Any]:
    """Block until the coordinator's config frame arrives; return the config."""
    return _decode(_read_frame_blocking(channel), "worker config")


def send_ready_frame(
    channel: BlockingChannel, error: str | None = None, **info: Any
) -> None:
    """Answer the config frame: serving, or refusing and why."""
    if error is not None:
        channel.sendall(_config_frame({"ok": False, "error": error}))
        return
    channel.sendall(_config_frame({"ok": True, **info}))


# -- coordinator side (async, over the ByteStream) --


async def send_config(stream: Any, config: dict[str, Any]) -> None:
    """Send the worker config as the first frame on ``stream``."""
    await stream.send_all(_config_frame(config))


async def _receive_exactly(stream: Any, count: int) -> bytes:
    chunks: list[bytes] = []
    remaining = count
    while remaining:
        chunk = await stream.receive_some(remaining)
        if not chunk:
            raise EOFError(
                f"connection closed during the worker handshake "
                f"({count - remaining} of {count} bytes)"
            )
        chunks.append(bytes(chunk))
        remaining -= len(chunk)
    return b"".join(chunks)


async def read_ready(stream: Any, what: str) -> dict[str, Any]:
    """Await the worker's reply; raise :class:`ConfigRefused` if it declined.

    ``what`` names the transport, so an EOF here says which launch never
    got as far as answering.
    """
    header = await _receive_exactly(stream, 9)
    msgcode, channelid, payload_len = Message.from_header(header)
    payload = await _receive_exactly(stream, payload_len) if payload_len else b""
    try:
        reply = _decode(Message(msgcode, channelid, payload), f"{what} handshake reply")
    except ValueError as exc:  # malformed JSON from something that is not us
        raise EOFError(f"bad {what} handshake reply: {exc}") from None
    if not reply.get("ok"):
        raise ConfigRefused(reply.get("error") or f"worker refused the {what} config")
    return reply
