"""
execnet: Elastic Python Deployment.
package for connecting to local and remote Python Interpreters.

(c) 2009, Holger Krekel and others
"""
__version__ = "1.0.0b2"

import execnet.apipkg

execnet.apipkg.initpkg(__name__, {
    'PopenGateway':     '.gateway:PopenGateway',
    'SocketGateway':    '.gateway:SocketGateway',
    'SshGateway':       '.gateway:SshGateway',
    'HostNotFound':     '.gateway:HostNotFound',
    'makegateway':      '.xspec:makegateway',
    'XSpec':            '.xspec:XSpec',
    'MultiGateway':     '.multi:MultiGateway',
    'MultiChannel':     '.multi:MultiChannel',
    'RSync':            '.rsync:RSync',
})
