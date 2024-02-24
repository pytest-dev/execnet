# incomplete, to be completed later
# this will replace concat based bootstrap
from __future__ import annotations

import base64
import json
import os
import sys
import types
import zlib
from importlib import import_module
from importlib.abc import Loader
from importlib.metadata import Distribution
from importlib.metadata import DistributionFinder
from typing import IO
from typing import TYPE_CHECKING
from typing import Any
from typing import Iterable
from typing import Sequence

if TYPE_CHECKING:
    pass


class DictDistribution(Distribution):
    data: dict[str, str]

    def __init__(self, data: dict[str, str]) -> None:
        self.data = data

    def read_text(self, filename):
        return self.data[filename]

    def locate_file(self, path: str | os.PathLike[str]) -> os.PathLike[str]:
        raise FileNotFoundError(path)


class DictImporter(DistributionFinder, Loader):
    """a limited loader/importer for distributins send via json-lines"""

    def __init__(self, sources: dict[str, str], distribution: DictDistribution):
        self.sources = sources
        self.distribution = distribution

    def find_distributions(
        self, context: DistributionFinder.Context = DistributionFinder.Context()
    ) -> Iterable[Distribution]:
        return [self.distribution]

    def find_module(
        self, fullname: str, path: Sequence[str | bytes] | None = None
    ) -> Loader | None:
        if fullname in self.sources:
            return self
        if fullname + ".__init__" in self.sources:
            return self
        return None

    def load_module(self, fullname):
        # print "load_module:",  fullname
        from types import ModuleType

        try:
            s = self.sources[fullname]
            is_pkg = False
        except KeyError:
            s = self.sources[fullname + ".__init__"]
            is_pkg = True

        co = compile(s, fullname, "exec")
        module = sys.modules.setdefault(fullname, ModuleType(fullname))
        module.__loader__ = self
        if is_pkg:
            module.__path__ = [fullname]

        exec(co, module.__dict__)
        return sys.modules[fullname]

    def get_source(self, name: str) -> str | None:
        res = self.sources.get(name)
        if res is None:
            res = self.sources.get(name + ".__init__")
        return res


def bootstrap(
    modules: dict[str, str],
    distribution: dict[str, str],
    entry: str,
    args: dict[str, Any],
    set_argv: list[str] | None,
) -> None:
    importer = DictImporter(modules, distribution=DictDistribution(distribution))
    sys.meta_path.append(importer)
    module, attr = entry.split(":")
    loaded_module = import_module(module)
    entry_func = getattr(loaded_module, entry)
    if set_argv is not None:
        sys.argv[1:] = set_argv
    entry_func(**args)


def bootstrap_stdin(stream: IO) -> None:
    bootstrap_args = decode_b85_zip_json(stream.readline())
    bootstrap(**bootstrap_args)


def decode_b85_zip_json(encoded: bytes | str):
    packed = base64.b85decode(encoded)
    unpacked = zlib.decompress(packed)
    return json.loads(unpacked)


def naive_pack_module(module: types.ModuleType, dist: Distribution):
    assert module.__file__ is not None
    assert module.__path__


if __name__ == "__main__":
    bootstrap_stdin(sys.stdin)
