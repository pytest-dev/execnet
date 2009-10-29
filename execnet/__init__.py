"""
execnet: Elastic Python Deployment.
package for connecting to local and remote Python Interpreters.

(c) 2009, Holger Krekel and others
"""

__version__ = "1.0.0b1"
__author__ = "holger krekel <holger@merlinux.eu> and others"

from execnet.gateway import PopenGateway, SocketGateway, SshGateway
from execnet.gateway import HostNotFound
from execnet.xspec import makegateway, XSpec
from execnet.multi import MultiGateway,MultiChannel
from execnet.rsync import RSync
