"""Debug tracing, configured once from ``EXECNET_DEBUG``.

:EXECNET_DEBUG=1: write per-process trace files to ``execnet-debug-PID``
:EXECNET_DEBUG=2: trace to stderr (popen workers forward this to their
    instantiator)

Unset, ``trace`` is a no-op lambda so tracing costs a call and nothing else.
"""

from __future__ import annotations

import os
import sys

DEBUG = os.environ.get("EXECNET_DEBUG")
pid = os.getpid()

if DEBUG == "2":

    def trace(*msg: object) -> None:
        try:
            line = " ".join(map(str, msg))
            sys.stderr.write(f"[{pid}] {line}\n")
            sys.stderr.flush()
        except Exception:
            pass  # nothing we can do, likely interpreter-shutdown

elif DEBUG:
    import tempfile

    fn = os.path.join(tempfile.gettempdir(), "execnet-debug-%d" % pid)
    # sys.stderr.write("execnet-debug at %r" % (fn,))
    debugfile = open(fn, "w")

    def trace(*msg: object) -> None:
        try:
            line = " ".join(map(str, msg))
            debugfile.write(line + "\n")
            debugfile.flush()
        except Exception as exc:
            try:
                sys.stderr.write(f"[{pid}] exception during tracing: {exc!r}\n")
            except Exception:
                pass  # nothing we can do, likely interpreter-shutdown

else:
    notrace = trace = lambda *msg: None
