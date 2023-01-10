import sys

import register  # type: ignore[import]
import rlcompleter2  # type: ignore[import]

rlcompleter2.setup()

try:
    hostport = sys.argv[1]
except:
    hostport = ":8888"
gw = register.ServerGateway(hostport)
