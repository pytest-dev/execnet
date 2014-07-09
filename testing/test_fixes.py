from execnet import Group
from execnet.gateway_bootstrap import fix_pid_for_jython_popen


def test_jython_bootstrap_not_on_remote():
    group = Group()
    try:
        group.makegateway('popen//id=via')
        group.makegateway('popen//via=via')
    finally:
        group.terminate(timeout=1.0)



def test_jython_bootstrap_fix():
    group = Group()
    gw = group.makegateway('popen')
    popen = gw._io.popen
    real_pid = popen.pid
    try:
        # nothing happens when calling it on a normal seyup
        fix_pid_for_jython_popen(gw)
        assert popen.pid == real_pid

        # if there is no pid for a popen gw, restore
        popen.pid = None
        fix_pid_for_jython_popen(gw)
        assert popen.pid == real_pid

        # if there is no pid for other gw, ignore - they are remote
        gw.spec.popen = False
        popen.pid = None
        fix_pid_for_jython_popen(gw)
        assert popen.pid is None

    finally:
        popen.pid = real_pid
        group.terminate(timeout=1)
