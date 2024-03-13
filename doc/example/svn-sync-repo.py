#!/usr/bin/env python
"""

small utility for hot-syncing a svn repository through ssh.
uses execnet.

"""

import os
import pathlib
import subprocess
import sys

import execnet


def usage():
    arg0 = sys.argv[0]
    print(arg0, "[user@]remote-host:/repo/location localrepo [ssh-config-file]")


def main(args):
    remote = args[0]
    localrepo = pathlib.Path(args[1])
    if not localrepo.is_dir():
        raise SystemExit(f"localrepo {localrepo} does not exist")
    if len(args) == 3:
        configfile = args[2]
    else:
        configfile = None
    remote_host, path = remote.split(":", 1)
    print("ssh-connecting to", remote_host)
    gw = getgateway(remote_host, configfile)

    local_rev = get_svn_youngest(localrepo)

    # local protocol
    # 1. client sends rev/repo -> server
    # 2. server checks for newer revisions and sends dumps
    # 3. client receives dumps, updates local repo
    # 4. client goes back to step 1
    c = gw.remote_exec(
        """
        import os
        import subprocess
        import time

        remote_rev, repopath = channel.receive()
        while True:
            rev = subprocess.run(
                ["svnlook", "youngest", repopath],
                check=True,
                capture_output=True,
                text=True,
            ).stdout
            rev = int(rev)
            if rev > remote_rev:
                revrange = (remote_rev+1, rev)
                dumpchannel = channel.gateway.newchannel()
                channel.send(revrange)
                channel.send(dumpchannel)

                f = os.popen(
                        "svnadmin dump -q --incremental -r %s:%s %s"
                         % (revrange[0], revrange[1], repopath), 'r')
                try:
                    maxcount = dumpchannel.receive()
                    count = maxcount
                    while 1:
                        s = f.read(8192)
                        if not s:
                            raise EOFError
                        dumpchannel.send(s)
                        count = count - 1
                        if count <= 0:
                            ack = dumpchannel.receive()
                            count = maxcount

                except EOFError:
                    dumpchannel.close()
                remote_rev = rev
            else:
                # using svn-hook instead would be nice here
                time.sleep(30)
    """
    )

    c.send((local_rev, path))
    print("checking revisions from %d in %s" % (local_rev, remote))
    while 1:
        revstart, revend = c.receive()
        dumpchannel = c.receive()
        print("receiving revisions", revstart, "-", revend, "replaying...")
        svn_load(localrepo, dumpchannel)
        print("current revision", revend)


def svn_load(repo, dumpchannel, maxcount=100):
    # every maxcount we will send an ACK to the other
    # side in order to synchronise and avoid our side
    # growing buffers  (execnet does not control
    # RAM usage or receive queue sizes)
    dumpchannel.send(maxcount)
    f = os.popen(f"svnadmin load -q {repo}", "w")
    count = maxcount
    for x in dumpchannel:
        sys.stdout.write(".")
        sys.stdout.flush()
        f.write(x)
        count = count - 1
        if count <= 0:
            dumpchannel.send(maxcount)
            count = maxcount
    print()
    f.close()


def get_svn_youngest(repo):
    rev = subprocess.run(
        ["svnlook", "youngest", repo],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return int(rev)


def getgateway(host, configfile=None):
    xspec = "ssh=%s" % host
    if configfile is not None:
        xspec += "//ssh_config=%s" % configfile
    return execnet.makegateway(xspec)


if __name__ == "__main__":
    if len(sys.argv) < 3:
        usage()
        raise SystemExit(1)

    main(sys.argv[1:])
