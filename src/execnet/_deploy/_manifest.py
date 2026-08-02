"""Describing a source tree, and deciding what a target is missing.

Split out because it is the only part of a transfer with no IO of its own
worth speaking of: a manifest is data, the comparison against a target is
a function of two manifests, and both are testable without a gateway.

The manifest is flat -- one entry per path, relative, ``/``-separated --
rather than the nested structure the pre-3.0 rsync streamed one message per
node.  A tree of ten thousand files is one message either way; the flat
form is one round trip instead of one per directory, and it can be
compared without walking anything.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Callable
from typing import Literal
from typing import NamedTuple

#: what a path is, as far as a transfer cares
Kind = Literal["dir", "file", "link"]


class Entry(NamedTuple):
    """One path in a manifest.

    ``mode`` is the raw ``st_mode``.  For a file ``mtime``/``size`` are its
    own; for a link ``target`` is what it points at, rebased onto the tree
    root when it was an absolute path pointing inside it (see
    :func:`_link_target`).
    """

    path: str
    kind: Kind
    mode: int
    mtime: float = 0.0
    size: int = 0
    target: str = ""
    #: for a link: whether ``target`` is relative to the transferred root
    internal: bool = False


class Manifest(NamedTuple):
    entries: tuple[Entry, ...]

    def files(self) -> dict[str, Entry]:
        return {entry.path: entry for entry in self.entries if entry.kind == "file"}

    def dump(self) -> list[tuple[object, ...]]:
        """As plain builtins, for the wire."""
        return [tuple(entry) for entry in self.entries]

    @classmethod
    def load(cls, data: list[tuple[object, ...]]) -> Manifest:
        return cls(tuple(Entry(*item) for item in data))  # type: ignore[arg-type]


#: ``(path) -> bool``: whether a path belongs in the transfer.  Called with
#: the absolute local path of every candidate *below* the root -- never the
#: root itself -- and may have side effects, so nothing may assume the tree
#: still looks the way it did when the walk passed by.
Filter = Callable[[str], bool]


def _link_target(root: str, target: str) -> tuple[str, bool]:
    """A link's target as it should be recreated, and whether it was rebased.

    A *relative* link is left exactly as it is: being relative is what
    makes it survive the move, and rewriting it would turn a link that
    means "my neighbour" into one naming a particular directory.

    An *absolute* link is rebased when it points inside the tree, so it
    points inside the copy rather than back at the original.  One pointing
    anywhere else is copied verbatim, whether or not the other end exists
    over there.
    """
    if not os.path.isabs(target):
        return target, False
    if os.path.__name__ == "ntpath" and target.startswith("\\\\?\\"):
        # Windows readlink gives an extended path for absolute links, and
        # relpath refuses to mix extended and non-extended
        if not root.startswith("\\\\?\\"):
            root = "\\\\?\\" + root
    try:
        relative = os.path.relpath(target, root)
    except ValueError:  # different drives on Windows
        return target, False
    if relative in (os.curdir, os.pardir) or relative.startswith(os.pardir + os.sep):
        return target, False
    return relative.replace(os.sep, "/"), True


def walk(root: str, filter: Filter | None = None) -> Manifest:
    """Describe the tree at ``root``; blocking, so run it in a thread.

    Entries come out parents-first, which is the order a receiver can
    create them in.  A path that disappears between being listed and being
    stat'd is simply left out -- a filter with side effects is a thing
    people write, and a transfer that raced one should still transfer the
    rest.
    """
    root = os.path.dirname(os.path.join(root, "x"))  # normalise a trailing /
    entries: list[Entry] = []

    def visit(path: str, relative: str) -> None:
        try:
            st = os.lstat(path)
        except OSError:
            return  # vanished since it was listed
        if stat.S_ISDIR(st.st_mode):
            if relative:
                entries.append(Entry(relative, "dir", st.st_mode))
            try:
                names = sorted(os.listdir(path))
            except OSError:
                return
            for name in names:
                child = os.path.join(path, name)
                if filter is not None and not filter(child):
                    continue
                visit(child, f"{relative}/{name}" if relative else name)
        elif stat.S_ISREG(st.st_mode):
            entries.append(Entry(relative, "file", st.st_mode, st.st_mtime, st.st_size))
        elif stat.S_ISLNK(st.st_mode):
            target, internal = _link_target(root, os.readlink(path))
            entries.append(
                Entry(relative, "link", st.st_mode, target=target, internal=internal)
            )
        else:
            raise ValueError(f"cannot transfer {path!r}: not a file, dir or symlink")

    visit(root, "")
    return Manifest(tuple(entries))


class Wanted(NamedTuple):
    """What a target needs, in reply to a manifest.

    ``checksums`` maps a path to the digest the target already has, for the
    files whose size matches but whose mtime does not: the sender compares
    it against its own and skips the body when they agree.  That is the
    check that makes a re-transfer of an unchanged tree nearly free.
    """

    paths: tuple[str, ...]
    checksums: dict[str, bytes]

    def dump(self) -> tuple[object, ...]:
        return (list(self.paths), self.checksums)

    @classmethod
    def load(cls, data: tuple[object, ...]) -> Wanted:
        paths, checksums = data
        return cls(tuple(paths), dict(checksums))  # type: ignore[arg-type, call-overload]
