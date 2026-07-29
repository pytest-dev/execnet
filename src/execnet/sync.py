"""The blocking execnet API.

A facade over the trio-native core in :mod:`execnet.trio`: gateways run
their protocol IO on a dedicated Trio host thread while this surface
blocks the calling thread.  The top-level ``execnet.*`` names are aliases
into this module.
"""

from ._channel import Channel
from ._errors import DataFormatError
from ._errors import DumpError
from ._errors import HostNotFound
from ._errors import LoadError
from ._errors import RemoteError
from ._errors import TimeoutError
from ._gateway import Gateway
from ._host import Host
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
    "DumpError",
    "Gateway",
    "Group",
    "Host",
    "HostNotFound",
    "LoadError",
    "MultiChannel",
    "RSync",
    "RemoteError",
    "TimeoutError",
    "XSpec",
    "default_group",
    "makegateway",
    "set_execmodel",
    "set_profile",
]
