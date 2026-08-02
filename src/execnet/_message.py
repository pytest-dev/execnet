"""The wire protocol: IO protocols, Message framing and its decoder.

A frame is a 9-byte header (``!bii``: message code, channel id, payload
length) followed by the payload.  Nothing here dispatches -- routing lives in
``AsyncGateway._dispatch`` and the sync bridge session; this module only packs,
unpacks and buffers.
"""

from __future__ import annotations

import os
import struct
import sys
from collections.abc import Iterator
from typing import TYPE_CHECKING
from typing import Protocol

if TYPE_CHECKING:
    from ._execmodel import ExecModel


class WriteIO(Protocol):
    def write(self, data: bytes, /) -> None: ...


class ReadIO(Protocol):
    def read(self, numbytes: int, /) -> bytes: ...


class IO(Protocol):
    """What a gateway still needs from the object it was built around.

    Reading and writing moved to the Trio session long ago; what is left is
    the write-side close behind ``Gateway.exit``.  Waiting for and killing a
    worker process belongs to whoever holds the process handle -- the async
    group -- not here.
    """

    execmodel: ExecModel

    def read(self, numbytes: int, /) -> bytes: ...

    def write(self, data: bytes, /) -> None: ...

    def close_read(self) -> None: ...

    def close_write(self) -> None: ...


class Message:
    """Encapsulates Messages and their wire protocol.

    Dispatch lives in the async core and the sync bridge session
    (``AsyncGateway._dispatch`` / ``SyncBridgeGateway._dispatch``); this
    class only carries the framing and the code constants.
    """

    STATUS = 0
    #: retired: the py2/py3 string coercion switch.  Nothing sends or
    #: handles it anymore, the code stays reserved for reuse.
    RECONFIGURE = 1
    GATEWAY_TERMINATE = 2
    CHANNEL_EXEC = 3
    CHANNEL_DATA = 4
    CHANNEL_CLOSE = 5
    CHANNEL_CLOSE_ERROR = 6
    CHANNEL_LAST_MESSAGE = 7
    GATEWAY_START_SOCKET = 8
    GATEWAY_START_SUB = 9
    GATEWAY_INFO = 10
    #: the worker handshake, both directions -- see :mod:`execnet._handshake`.
    #: Exchanged before either side starts serving, so it is never dispatched.
    GATEWAY_CONFIG = 11
    #: a request the worker serves *itself* rather than exec'ing: the
    #: payload names the service and carries its request, and what the name
    #: means is none of the core's business.  See :mod:`execnet._services`.
    GATEWAY_SERVICE = 12

    # message code -> name
    _types: dict[int, str] = {
        STATUS: "STATUS",
        RECONFIGURE: "RECONFIGURE",
        GATEWAY_TERMINATE: "GATEWAY_TERMINATE",
        CHANNEL_EXEC: "CHANNEL_EXEC",
        CHANNEL_DATA: "CHANNEL_DATA",
        CHANNEL_CLOSE: "CHANNEL_CLOSE",
        CHANNEL_CLOSE_ERROR: "CHANNEL_CLOSE_ERROR",
        CHANNEL_LAST_MESSAGE: "CHANNEL_LAST_MESSAGE",
        GATEWAY_START_SOCKET: "GATEWAY_START_SOCKET",
        GATEWAY_START_SUB: "GATEWAY_START_SUB",
        GATEWAY_INFO: "GATEWAY_INFO",
        GATEWAY_CONFIG: "GATEWAY_CONFIG",
        GATEWAY_SERVICE: "GATEWAY_SERVICE",
    }

    def __init__(self, msgcode: int, channelid: int = 0, data: bytes = b"") -> None:
        self.msgcode = msgcode
        self.channelid = channelid
        self.data = data

    def pack(self) -> bytes:
        """Return the full wire frame (9-byte header + payload)."""
        header = struct.pack("!bii", self.msgcode, self.channelid, len(self.data))
        return header + self.data

    @staticmethod
    def from_header(header: bytes) -> tuple[int, int, int]:
        """Unpack a 9-byte header into (msgtype, channelid, payload_len)."""
        if len(header) != 9:
            raise EOFError("couldn't load message header, short read")
        msgtype, channel, payload = struct.unpack("!bii", header)
        return msgtype, channel, payload

    @staticmethod
    def from_parts(msgtype: int, channel: int, data: bytes) -> Message:
        return Message(msgtype, channel, data)

    @staticmethod
    def from_io(io: ReadIO) -> Message:
        try:
            header = io.read(9)  # type 1, channel 4, payload 4
            if not header:
                raise EOFError("empty read")
        except EOFError as e:
            raise EOFError("couldn't load message header, " + e.args[0]) from None
        msgtype, channel, payload = Message.from_header(header)
        return Message(msgtype, channel, io.read(payload))

    def to_io(self, io: WriteIO) -> None:
        io.write(self.pack())

    def __repr__(self) -> str:
        name = self._types[self.msgcode]
        return f"<Message {name} channel={self.channelid} lendata={len(self.data)}>"


def gateway_info() -> dict[str, object]:
    """Payload for ``Message.GATEWAY_INFO``: sys/env facts about this side.

    Answered natively by the dispatch loop -- an info request never
    touches the exec machinery, so it cannot claim an exec slot (with
    main-thread profiles, an info call stealing the primary slot used to
    push the real workload onto a worker thread).
    """
    return {
        "executable": sys.executable,
        "version_info": tuple(sys.version_info[:5]),
        "platform": sys.platform,
        "cwd": os.getcwd(),
        "pid": os.getpid(),
    }


class FrameDecoder:
    """Incremental decoder for the 9-byte-header Message framing.

    ``feed(data)`` accepts arbitrary byte chunks and yields every complete
    Message; partial frames buffer internally until more bytes arrive.
    Pure computation — no IO, no awaits, no knowledge of streams — so
    receivers only ever stream bytes in (``receive_some`` loops) and the
    decoder owns framing.
    """

    def __init__(self) -> None:
        self._buffer = bytearray()

    def feed(self, data: bytes) -> Iterator[Message]:
        self._buffer += data
        return self._parse()

    def _parse(self) -> Iterator[Message]:
        while len(self._buffer) >= 9:
            msgtype, channelid, payload_len = Message.from_header(
                bytes(self._buffer[:9])
            )
            if len(self._buffer) < 9 + payload_len:
                return
            payload = bytes(self._buffer[9 : 9 + payload_len])
            del self._buffer[: 9 + payload_len]
            yield Message(msgtype, channelid, payload)

    def close(self) -> None:
        """Signal EOF; raises EOFError if the stream ended mid-frame."""
        if self._buffer:
            raise EOFError(
                "connection closed mid-frame (%d buffered bytes)" % len(self._buffer)
            )
