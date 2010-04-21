"""
execnet: pure python lib for connecting to local and remote Python Interpreters.

(c) 2010, Holger Krekel and others
"""
__version__ = "1.0.6"

import execnet.apipkg

execnet.apipkg.initpkg(__name__, {
    'PopenGateway':     '.deprecated:PopenGateway',
    'SocketGateway':    '.deprecated:SocketGateway',
    'SshGateway':       '.deprecated:SshGateway',
    'makegateway':      '.multi:makegateway',
    'HostNotFound':     '.gateway:HostNotFound',
    'RemoteError':      '.gateway_base:RemoteError',
    'TimeoutError':     '.gateway_base:TimeoutError',
    'XSpec':            '.xspec:XSpec',
    'Group':            '.multi:Group',
    'MultiChannel':     '.multi:MultiChannel',
    'RSync':            '.rsync:RSync',
    'default_group':    '.multi:default_group',
})
