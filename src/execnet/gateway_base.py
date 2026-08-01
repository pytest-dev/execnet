"""Deprecated: the pre-Trio core module, now split by concern.

``execnet.gateway_base`` was never a supported public API.  Its contents live
in private modules -- ``_trace``, ``_errors``, ``_execmodel``, ``_message``,
``_serialize``, ``_channel`` and ``_gateway_base`` -- and this shim forwards
to them with a :class:`DeprecationWarning`.

Use :mod:`execnet` / :mod:`execnet.sync`, :mod:`execnet.trio`,
:mod:`execnet.aio` or :mod:`execnet.gevent` instead.  The standalone
serializer stays internal; :func:`execnet.can_send` answers "can this value
cross a channel?" without it.
"""

from __future__ import annotations

from ._shim import forwarder

_MOVED = {
    # tracing
    "DEBUG": "._trace",
    "pid": "._trace",
    "trace": "._trace",
    "notrace": "._trace",
    # errors and error texts
    "sysex": "._errors",
    "INTERRUPT_TEXT": "._errors",
    "GatewayReceivedTerminate": "._errors",
    "HostNotFound": "._errors",
    "geterrortext": "._errors",
    "RemoteError": "._errors",
    "TimeoutError": "._errors",
    "DataFormatError": "._errors",
    "DumpError": "._errors",
    "LoadError": "._errors",
    # execution model presets
    "ExecModel": "._execmodel",
    "get_execmodel": "._execmodel",
    # wire protocol
    "WriteIO": "._message",
    "ReadIO": "._message",
    "IO": "._message",
    "Message": "._message",
    "gateway_info": "._message",
    "FrameDecoder": "._message",
    # serializer
    "bchr": "._serialize",
    "DUMPFORMAT_VERSION": "._serialize",
    "FOUR_BYTE_INT_MAX": "._serialize",
    "FOUR_BYTE_INT_MIN": "._serialize",
    "FLOAT_FORMAT": "._serialize",
    "FLOAT_FORMAT_SIZE": "._serialize",
    "COMPLEX_FORMAT": "._serialize",
    "COMPLEX_FORMAT_SIZE": "._serialize",
    "opcode": "._serialize",
    "Unserializer": "._serialize",
    "_Serializer": "._serialize",
    "_Stop": "._serialize",
    "dumps": "._serialize",
    "dump": "._serialize",
    "loads": "._serialize",
    "load": "._serialize",
    "dumps_internal": "._serialize",
    "loads_internal": "._serialize",
    # channels
    "Channel": "._channel",
    "ChannelFactory": "._channel",
    "ChannelFile": "._channel",
    "ChannelFileWrite": "._channel",
    "ChannelFileRead": "._channel",
    "ENDMARKER": "._channel",
    "NO_ENDMARKER_WANTED": "._channel",
    # gateways
    "BaseGateway": "._gateway_base",
    "WorkerGateway": "._gateway_base",
}

__getattr__ = forwarder("gateway_base", _MOVED)
