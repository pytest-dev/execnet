"""
execnet io initialization code

creates io instances used for gateway io
"""
import os
import sys
from execnet.gateway_base import Popen2IO
from subprocess import Popen, PIPE

class Popen2IOMaster(Popen2IO):
    def __init__(self, args):
        self.popen = p = Popen(args, stdin=PIPE, stdout=PIPE)
        Popen2IO.__init__(self, p.stdin, p.stdout)

    def wait(self):
        return self.popen.wait()

    def kill(self):
        killpopen(self.popen)

def killpopen(popen):
    try:
        if hasattr(popen, 'kill'):
            popen.kill()
        else:
            killpid(popen.pid)
    except EnvironmentError:
        sys.stderr.write("ERROR killing: %s\n" %(sys.exc_info()[1]))
        sys.stderr.flush()

def killpid(pid):
    if hasattr(os, 'kill'):
        os.kill(pid, 15)
    elif sys.platform == "win32" or getattr(os, '_name', None) == 'nt':
        try:
            import ctypes
        except ImportError:
            import subprocess
            # T: treekill, F: Force
            cmd = ("taskkill /T /F /PID %d" %(pid)).split()
            ret = subprocess.call(cmd)
            if ret != 0:
                raise EnvironmentError("taskkill returned %r" %(ret,))
        else:
            PROCESS_TERMINATE = 1
            handle = ctypes.windll.kernel32.OpenProcess(
                        PROCESS_TERMINATE, False, pid)
            ctypes.windll.kernel32.TerminateProcess(handle, -1)
            ctypes.windll.kernel32.CloseHandle(handle)
    else:
        raise EnvironmentError("no method to kill %s" %(pid,))








