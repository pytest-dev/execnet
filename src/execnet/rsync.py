"""
1:N rsync implementation on top of execnet.

(c) 2006-2009, Armin Rigo, Holger Krekel, Maciej Fijalkowski
"""
from __future__ import annotations

import os
import stat
from hashlib import md5
from queue import Queue
from typing import Callable
from typing import Type

import execnet.rsync_remote
from execnet.gateway_base import Channel
from execnet.multi import MultiChannel


class RSync:
    """This class allows to send a directory structure (recursively)
    to one or multiple remote filesystems.

    There is limited support for symlinks, which means that symlinks
    pointing to the sourcetree will be send "as is" while external
    symlinks will be just copied (regardless of existence of such
    a path on remote side).
    """

    def __init__(self, sourcedir: str | os.PathLike[str], callback=None, verbose=True):
        self._sourcedir = str(sourcedir)
        self._verbose = verbose
        assert callback is None or hasattr(callback, "__call__")
        self._callback = callback
        self._channels: dict[Channel, Callable[[], None] | None] = {}
        self._links: list[tuple[str, str, str]] = []

    def filter(self, path):
        return True

    def _end_of_channel(self, channel, data):
        if channel in self._channels:
            # too early!  we must have got an error
            channel.waitclose()
            # or else we raise one
            raise OSError(f"connection unexpectedly closed: {channel.gateway} ")

    def _process_link(self, channel, data):
        for link in self._links:
            channel.send(link)
        # completion marker, this host is done
        channel.send(42)

    def _done(self, channel, data):
        """Call all callbacks"""
        finishedcallback = self._channels.pop(channel)
        if finishedcallback:
            finishedcallback()
        channel.waitclose()

    def _ack(self, channel, data):
        if self._callback:
            self._callback("ack", self._paths[data], channel)

    def _list_done(self, channel, data):
        # sum up all to send
        if self._callback:
            s = sum(self._paths[i] for i in self._to_send[channel])
            self._callback("list", s, channel)

    def _send_item(self, channel, data):
        """Send one item"""
        modified_rel_path, checksum = data
        modifiedpath = os.path.join(self._sourcedir, *modified_rel_path)
        try:
            with open(modifiedpath, "rb") as fp:
                data = fp.read()
        except OSError:
            data = None

        # provide info to progress callback function
        modified_rel_path = "/".join(modified_rel_path)
        if data is not None:
            self._paths[modified_rel_path] = len(data)
        else:
            self._paths[modified_rel_path] = 0
        if channel not in self._to_send:
            self._to_send[channel] = []
        self._to_send[channel].append(modified_rel_path)
        # print "sending", modified_rel_path, data and len(data) or 0, checksum

        if data is not None:
            if checksum is not None and checksum == md5(data).digest():
                data = None  # not really modified
            else:
                self._report_send_file(channel.gateway, modified_rel_path)
        channel.send(data)

    def _report_send_file(self, gateway, modified_rel_path):
        if self._verbose:
            print(f"{gateway} <= {modified_rel_path}")

    def send(self, raises: bool = True) -> None:
        """Sends a sourcedir to all added targets. Flag indicates
        whether to raise an error or return in case of lack of
        targets
        """
        if not self._channels:
            if raises:
                raise OSError(
                    "no targets available, maybe you " "are trying call send() twice?"
                )
            return
        # normalize a trailing '/' away
        self._sourcedir = os.path.dirname(os.path.join(self._sourcedir, "x"))
        # send directory structure and file timestamps/sizes
        self._send_directory_structure(self._sourcedir)

        # paths and to_send are only used for doing
        # progress-related callbacks
        self._paths: dict[str, int] = {}
        self._to_send: dict[Channel, list[str]] = {}

        mch = MultiChannel(list(self._channels))
        rq = mch.make_receive_queue(endmarker=(None, None))

        # send modified file to clients
        commands: dict[str | None, Callable] = {
            None: self._end_of_channel,
            "links": self._process_link,
            "done": self._done,
            "ack": self._ack,
            "send": self._send_item,
            "list_done": self._list_done,
        }

        while self._channels:
            channel, (command, data) = rq.get()
            assert command in commands, "Unknown command %s" % command
            commands[command](channel, data)

    def add_target(self, gateway, destdir, finishedcallback=None, delete: bool = False):
        """Adds a remote target specified via a gateway
        and a remote destination directory.
        """
        channel = gateway.remote_exec(
            execnet.rsync_remote.serve_rsync, destdir=str(destdir), delete=delete
        )
        self._channels[channel] = finishedcallback

    def _broadcast(self, msg):
        for channel in self._channels:
            channel.send(msg)

    def _send_link(self, linktype, basename, linkpoint):
        self._links.append((linktype, basename, linkpoint))

    def _send_directory(self, path):
        # dir: send a list of entries
        names = []
        subpaths = []
        for name in os.listdir(path):
            p = os.path.join(path, name)
            if self.filter(p):
                names.append(name)
                subpaths.append(p)
        mode = os.lstat(path).st_mode
        self._broadcast([mode] + names)
        for p in subpaths:
            self._send_directory_structure(p)

    def _send_link_structure(self, path):
        linkpoint = os.readlink(path)
        basename = path[len(self._sourcedir) + 1 :]
        if linkpoint.startswith(self._sourcedir):
            self._send_link("linkbase", basename, linkpoint[len(self._sourcedir) + 1 :])
        else:
            # relative or absolute link, just send it
            self._send_link("link", basename, linkpoint)
        self._broadcast(None)

    def _send_directory_structure(self, path):
        try:
            st = os.lstat(path)
        except OSError:
            self._broadcast((None, 0, 0))
            return
        if stat.S_ISREG(st.st_mode):
            # regular file: send a mode/timestamp/size pair
            self._broadcast((st.st_mode, st.st_mtime, st.st_size))
        elif stat.S_ISDIR(st.st_mode):
            self._send_directory(path)
        elif stat.S_ISLNK(st.st_mode):
            self._send_link_structure(path)
        else:
            raise ValueError(f"cannot sync {path!r}")
