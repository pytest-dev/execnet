"""The blocking execnet API.

A facade over the trio-native core in :mod:`execnet.trio`: gateways run
their protocol IO on a :class:`~execnet.ProtocolEngine` while this surface
blocks the calling thread.  The top-level ``execnet.*`` names are aliases
into this module.
"""

from ._channel import Channel
from ._deploy import Deployed
from ._deploy import Deployment
from ._deploy import transfer
from ._errors import DataFormatError
from ._errors import DumpError
from ._errors import HostNotFound
from ._errors import LoadError
from ._errors import RemoteError
from ._errors import TimeoutError
from ._gateway import Gateway
from ._engine import ProtocolEngine
from ._multi import Group
from ._multi import MultiChannel
from ._multi import default_group
from ._multi import makegateway
from ._multi import set_execmodel
from ._multi import set_profile
from ._rsync import RSync
from ._xspec import XSpec

__all__ = [
    "Channel",
    "DataFormatError",
    "Deployed",
    "Deployment",
    "DumpError",
    "Gateway",
    "Group",
    "HostNotFound",
    "LoadError",
    "MultiChannel",
    "ProtocolEngine",
    "RSync",
    "RemoteError",
    "TimeoutError",
    "XSpec",
    "default_group",
    "makegateway",
    "set_execmodel",
    "set_profile",
    "transfer",
]
