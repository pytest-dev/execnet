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



popen_bootstrapline = "import sys;exec(eval(sys.stdin.readline()))"


def popen_args(spec):
    python = spec.python or sys.executable
    args = [str(python), '-u']
    if spec is not None and spec.dont_write_bytecode:
        args.append("-B")
    # Slight gymnastics in ordering these arguments because CPython (as of
    # 2.7.1) ignores -B if you provide `python -c "something" -B`
    args.extend(['-c', popen_bootstrapline])
    return args

def ssh_args(spec):
    remotepython = spec.python or 'python'
    args = ['ssh', '-C' ]
    if spec.ssh_config is not None:
        args.extend(['-F', str(spec.ssh_config)])
    remotecmd = '%s -c "%s"' %(remotepython, popen_bootstrapline)
    args.extend([spec.ssh, remotecmd])
    return args



def create_io(spec):
    if spec.popen:
        args = popen_args(spec)
        return Popen2IOMaster(args)
    if spec.ssh:
        args = ssh_args(spec)
        io = Popen2IOMaster(args)
        io.remoteaddress = spec.ssh
        return io

