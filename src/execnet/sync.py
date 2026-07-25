"""The blocking execnet API.

A facade over the trio-native core in :mod:`execnet.trio`: gateways run
their protocol IO on a dedicated Trio host thread while this surface
blocks the calling thread.  The top-level ``execnet.*`` names are aliases
into this module.
"""

from .gateway import Gateway
from .gateway_base import Channel
from .gateway_base import DataFormatError
from .gateway_base import DumpError
from .gateway_base import HostNotFound
from .gateway_base import LoadError
from .gateway_base import RemoteError
from .gateway_base import TimeoutError
from .gateway_base import dump
from .gateway_base import dumps
from .gateway_base import load
from .gateway_base import loads
from .multi import Group
from .multi import MultiChannel
from .multi import default_group
from .multi import makegateway
from .multi import set_execmodel
from .rsync import RSync
from .xspec import XSpec

__all__ = [
    "Channel",
    "DataFormatError",
    "DumpError",
    "Gateway",
    "Group",
    "HostNotFound",
    "LoadError",
    "MultiChannel",
    "RSync",
    "RemoteError",
    "TimeoutError",
    "XSpec",
    "default_group",
    "dump",
    "dumps",
    "load",
    "loads",
    "makegateway",
    "set_execmodel",
]
