"""The wire serializer for execnet's simple builtin data format.

Internal: no public namespace re-exports ``dumps``/``loads``.  Callers that
need to know whether a value can cross a channel use
:func:`execnet.can_send`, which is exported from every public namespace.

The channel layer is *not* imported here -- ``Unserializer`` resolves a
channel or gateway argument by duck-typing -- so ``_channel`` may depend on
this module and not the other way round.
"""

from __future__ import annotations

import struct
from collections.abc import Callable
from io import BytesIO
from typing import TYPE_CHECKING
from typing import Any
from typing import Protocol
from typing import TypeAlias
from typing import cast

from ._errors import DumpError
from ._errors import LoadError

if TYPE_CHECKING:
    from collections.abc import Mapping
    from collections.abc import Sequence
    from collections.abc import Set as AbstractSet

    from typing_extensions import TypeIs

    from ._channel import Channel
    from ._message import ReadIO
    from ._trio_gateway import AsyncChannel

#: Everything execnet's wire format can carry, as one recursive alias.
#:
#: Deliberately spelled with the *abstract* containers rather than
#: ``list``/``dict``/``set``.  Those are invariant, so ``list[int]`` would
#: not satisfy ``list[Payload]`` and every honest ``channel.send([1, 2])``
#: would be an error -- the strict version is unusable as an argument type.
#: The abstract ones are covariant in their elements, so ordinary concrete
#: containers pass.
#:
#: The cost is a little over-acceptance: ``range`` is a ``Sequence[int]``
#: and ``memoryview`` a ``Sequence`` too, and neither has a wire
#: representation.  What this is for is the large class of mistakes --
#: functions, sockets, arbitrary instances -- and those it does catch.
#: :func:`can_send` remains the runtime answer.
Payload: TypeAlias = (
    "None | bool | int | float | complex | str | bytes"
    " | Sequence[Payload] | AbstractSet[Payload] | Mapping[Any, Payload]"
    " | Channel | AsyncChannel"
)


class ChannelFactory(Protocol):
    """Rebuilds the channel a wire ``CHANNEL`` opcode names."""

    def new(self, id: int, /) -> Any: ...


class FactoryOwner(Protocol):
    """A gateway: it owns the factory for its own channel ids."""

    @property
    def _channelfactory(self) -> ChannelFactory: ...


class ChannelLike(Protocol):
    """A channel -- sync or async -- which names the gateway that owns one.

    Spelled as a protocol rather than a ``Channel | AsyncChannel`` union to
    keep the promise in this module's docstring: the channel layer depends
    on the serializer and never the other way round.
    """

    @property
    def gateway(self) -> FactoryOwner: ...


def bchr(n: int) -> bytes:
    return bytes([n])


DUMPFORMAT_VERSION = bchr(2)

FOUR_BYTE_INT_MAX = 2147483647
FOUR_BYTE_INT_MIN = -2147483648

FLOAT_FORMAT = "!d"
FLOAT_FORMAT_SIZE = struct.calcsize(FLOAT_FORMAT)
COMPLEX_FORMAT = "!dd"
COMPLEX_FORMAT_SIZE = struct.calcsize(COMPLEX_FORMAT)


class _Stop(Exception):
    pass


class opcode:
    """Container for name -> num mappings."""

    BUILDTUPLE = b"@"
    BYTES = b"A"
    CHANNEL = b"B"
    FALSE = b"C"
    FLOAT = b"D"
    FROZENSET = b"E"
    INT = b"F"
    LONG = b"G"
    LONGINT = b"H"
    LONGLONG = b"I"
    NEWDICT = b"J"
    NEWLIST = b"K"
    NONE = b"L"
    STRING = b"N"
    SET = b"O"
    SETITEM = b"P"
    STOP = b"Q"
    TRUE = b"R"
    COMPLEX = b"T"


class Unserializer:
    num2func: dict[bytes, Callable[[Unserializer], None]] = {}

    def __init__(
        self,
        stream: ReadIO,
        channel_or_gateway: ChannelLike | FactoryOwner | None = None,
    ) -> None:
        # A channel -- sync or trio-native -- resolves through its gateway; a
        # gateway is already the right object.  Duck-typed so the serializer
        # stays independent of the channel layer.
        self.stream = stream
        self.channelfactory: ChannelFactory | None = None
        if channel_or_gateway is not None:
            # the two shapes cannot be told apart statically, which is the
            # point: neither name is imported here
            owner = cast(
                "FactoryOwner",
                getattr(channel_or_gateway, "gateway", channel_or_gateway),
            )
            self.channelfactory = owner._channelfactory

    def load(self, versioned: bool = False) -> Any:
        if versioned:
            ver = self.stream.read(1)
            if ver != DUMPFORMAT_VERSION:
                raise LoadError("wrong dumpformat version %r" % ver)
        self.stack: list[object] = []
        try:
            while True:
                opcode = self.stream.read(1)
                if not opcode:
                    raise EOFError
                try:
                    loader = self.num2func[opcode]
                except KeyError:
                    raise LoadError(
                        f"unknown opcode {opcode!r} - wire protocol corruption?"
                    ) from None
                loader(self)
        except _Stop:
            if len(self.stack) != 1:
                raise LoadError("internal unserialization error") from None
            return self.stack.pop(0)
        else:
            raise LoadError("didn't get STOP")

    def load_none(self) -> None:
        self.stack.append(None)

    num2func[opcode.NONE] = load_none

    def load_true(self) -> None:
        self.stack.append(True)

    num2func[opcode.TRUE] = load_true

    def load_false(self) -> None:
        self.stack.append(False)

    num2func[opcode.FALSE] = load_false

    def load_int(self) -> None:
        i = self._read_int4()
        self.stack.append(i)

    num2func[opcode.INT] = load_int

    def load_longint(self) -> None:
        s = self._read_byte_string()
        self.stack.append(int(s))

    num2func[opcode.LONGINT] = load_longint

    load_long = load_int
    num2func[opcode.LONG] = load_long
    load_longlong = load_longint
    num2func[opcode.LONGLONG] = load_longlong

    def load_float(self) -> None:
        binary = self.stream.read(FLOAT_FORMAT_SIZE)
        self.stack.append(struct.unpack(FLOAT_FORMAT, binary)[0])

    num2func[opcode.FLOAT] = load_float

    def load_complex(self) -> None:
        binary = self.stream.read(COMPLEX_FORMAT_SIZE)
        self.stack.append(complex(*struct.unpack(COMPLEX_FORMAT, binary)))

    num2func[opcode.COMPLEX] = load_complex

    def _read_int4(self) -> int:
        value: int = struct.unpack("!i", self.stream.read(4))[0]
        return value

    def _read_byte_string(self) -> bytes:
        length = self._read_int4()
        as_bytes = self.stream.read(length)
        return as_bytes

    def load_string(self) -> None:
        self.stack.append(self._read_byte_string().decode("utf-8"))

    num2func[opcode.STRING] = load_string

    def load_bytes(self) -> None:
        s = self._read_byte_string()
        self.stack.append(s)

    num2func[opcode.BYTES] = load_bytes

    def load_newlist(self) -> None:
        length = self._read_int4()
        self.stack.append([None] * length)

    num2func[opcode.NEWLIST] = load_newlist

    def load_setitem(self) -> None:
        if len(self.stack) < 3:
            raise LoadError("not enough items for setitem")
        value = self.stack.pop()
        key = self.stack.pop()
        self.stack[-1][key] = value  # type: ignore[index]

    num2func[opcode.SETITEM] = load_setitem

    def load_newdict(self) -> None:
        self.stack.append({})

    num2func[opcode.NEWDICT] = load_newdict

    def _load_collection(self, type_: type) -> None:
        length = self._read_int4()
        if length:
            res = type_(self.stack[-length:])
            del self.stack[-length:]
            self.stack.append(res)
        else:
            self.stack.append(type_())

    def load_buildtuple(self) -> None:
        self._load_collection(tuple)

    num2func[opcode.BUILDTUPLE] = load_buildtuple

    def load_set(self) -> None:
        self._load_collection(set)

    num2func[opcode.SET] = load_set

    def load_frozenset(self) -> None:
        self._load_collection(frozenset)

    num2func[opcode.FROZENSET] = load_frozenset

    def load_stop(self) -> None:
        raise _Stop

    num2func[opcode.STOP] = load_stop

    def load_channel(self) -> None:
        id = self._read_int4()
        assert self.channelfactory is not None
        newchannel = self.channelfactory.new(id)
        self.stack.append(newchannel)

    num2func[opcode.CHANNEL] = load_channel


def dumps(obj: Payload) -> bytes:
    """Serialize the given obj to a bytestring.

    The obj and all contained objects must be of a builtin
    Python type (so nested dicts, sets, etc. are all OK but
    not user-level instances).
    """
    return _Serializer().save(obj, versioned=True)  # type: ignore[return-value]


def dump(byteio, obj: object) -> None:
    """write a serialized bytestring of the given obj to the given stream."""
    _Serializer(write=byteio.write).save(obj, versioned=True)


def loads(bytestring: bytes) -> Any:
    """Deserialize the given bytestring to an object.

    If the bytestring was dumped with an incompatible protocol
    version or if the bytestring is corrupted, the
    ``execnet.DataFormatError`` will be raised.
    """
    return load(BytesIO(bytestring))


def load(io: ReadIO) -> Any:
    """Derserialize an object form the specified stream.

    Behaviour is otherwise the same as with ``loads``
    """
    return Unserializer(io).load(versioned=True)


def loads_internal(
    bytestring: bytes, channel_or_gateway: ChannelLike | FactoryOwner | None = None
) -> Any:
    io = BytesIO(bytestring)
    return Unserializer(io, channel_or_gateway).load()


def dumps_internal(obj: Payload) -> bytes:
    return _Serializer().save(obj)  # type: ignore[return-value]


def can_send(obj: object) -> TypeIs[Payload]:
    """Whether ``obj`` can cross a channel as-is.

    True for execnet's simple builtin wire data -- ``None``, ``bool``,
    ``int``, ``float``, ``complex``, ``bytes``, ``str`` and arbitrarily
    nested ``list``/``tuple``/``set``/``frozenset``/``dict`` of those --
    and for channel references.  False for anything execnet has no wire
    representation for, which ``channel.send`` would reject with
    :class:`~execnet.DumpError`.

    Use it to branch *before* sending, instead of sending and handling the
    error::

        channel.send(value if execnet.can_send(value) else repr(value))
    """
    try:
        _Serializer().save(obj)
    except DumpError:
        return False
    return True


class _Serializer:
    _dispatch: dict[type, Callable[[_Serializer, object], None]] = {}

    def __init__(self, write: Callable[[bytes], None] | None = None) -> None:
        if write is None:
            self._streamlist: list[bytes] = []
            write = self._streamlist.append
        self._write = write

    def save(self, obj: object, versioned: bool = False) -> bytes | None:
        # calling here is not re-entrant but multiple instances
        # may write to the same stream because of the common platform
        # atomic-write guarantee (concurrent writes each happen atomically)
        if versioned:
            self._write(DUMPFORMAT_VERSION)
        self._save(obj)
        self._write(opcode.STOP)
        try:
            streamlist = self._streamlist
        except AttributeError:
            return None
        return b"".join(streamlist)

    def _save(self, obj: object) -> None:
        tp = type(obj)
        try:
            dispatch = self._dispatch[tp]
        except KeyError:
            methodname = "save_" + tp.__name__
            meth: Callable[[_Serializer, object], None] | None = getattr(
                self.__class__, methodname, None
            )
            if meth is None:
                raise DumpError(f"can't serialize {tp}") from None
            dispatch = self._dispatch[tp] = meth
        dispatch(self, obj)

    def save_NoneType(self, non: None) -> None:
        self._write(opcode.NONE)

    def save_bool(self, boolean: bool) -> None:
        if boolean:
            self._write(opcode.TRUE)
        else:
            self._write(opcode.FALSE)

    def save_bytes(self, bytes_: bytes) -> None:
        self._write(opcode.BYTES)
        self._write_byte_sequence(bytes_)

    def save_str(self, s: str) -> None:
        self._write(opcode.STRING)
        self._write_unicode_string(s)

    def _write_unicode_string(self, s: str) -> None:
        try:
            as_bytes = s.encode("utf-8")
        except UnicodeEncodeError as e:
            raise DumpError("strings must be utf-8 encodable") from e
        self._write_byte_sequence(as_bytes)

    def _write_byte_sequence(self, bytes_: bytes) -> None:
        self._write_int4(len(bytes_), "string is too long")
        self._write(bytes_)

    def _save_integral(self, i: int, short_op: bytes, long_op: bytes) -> None:
        # The short op packs a signed 4-byte int; anything outside that range
        # (in either direction) goes through the arbitrary-precision long op.
        if FOUR_BYTE_INT_MIN <= i <= FOUR_BYTE_INT_MAX:
            self._write(short_op)
            self._write_int4(i)
        else:
            self._write(long_op)
            self._write_byte_sequence(str(i).rstrip("L").encode("ascii"))

    def save_int(self, i: int) -> None:
        self._save_integral(i, opcode.INT, opcode.LONGINT)

    def save_long(self, l: int) -> None:
        self._save_integral(l, opcode.LONG, opcode.LONGLONG)

    def save_float(self, flt: float) -> None:
        self._write(opcode.FLOAT)
        self._write(struct.pack(FLOAT_FORMAT, flt))

    def save_complex(self, cpx: complex) -> None:
        self._write(opcode.COMPLEX)
        self._write(struct.pack(COMPLEX_FORMAT, cpx.real, cpx.imag))

    def _write_int4(
        self, i: int, error: str = "int must be less than %i" % (FOUR_BYTE_INT_MAX,)
    ) -> None:
        if i > FOUR_BYTE_INT_MAX:
            raise DumpError(error)
        self._write(struct.pack("!i", i))

    def save_list(self, L: list[object]) -> None:
        self._write(opcode.NEWLIST)
        self._write_int4(len(L), "list is too long")
        for i, item in enumerate(L):
            self._write_setitem(i, item)

    def _write_setitem(self, key: object, value: object) -> None:
        self._save(key)
        self._save(value)
        self._write(opcode.SETITEM)

    def save_dict(self, d: dict[object, object]) -> None:
        self._write(opcode.NEWDICT)
        for key, value in d.items():
            self._write_setitem(key, value)

    def save_tuple(self, tup: tuple[object, ...]) -> None:
        for item in tup:
            self._save(item)
        self._write(opcode.BUILDTUPLE)
        self._write_int4(len(tup), "tuple is too long")

    def _write_set(self, s: set[object] | frozenset[object], op: bytes) -> None:
        for item in s:
            self._save(item)
        self._write(op)
        self._write_int4(len(s), "set is too long")

    def save_set(self, s: set[object]) -> None:
        self._write_set(s, opcode.SET)

    def save_frozenset(self, s: frozenset[object]) -> None:
        self._write_set(s, opcode.FROZENSET)

    def save_Channel(self, channel: Channel) -> None:
        self._write(opcode.CHANNEL)
        self._write_int4(channel.id)

    def save_AsyncChannel(self, channel: Any) -> None:
        # trio-native channel (execnet._trio_gateway); same wire opcode,
        # duck-typed here to avoid importing the async core.
        self._write(opcode.CHANNEL)
        self._write_int4(channel.id)
