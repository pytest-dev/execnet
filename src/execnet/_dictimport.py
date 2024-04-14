# incomplete, to be completed later
# this will replace concat based bootstrap
from __future__ import annotations

import base64
import json
import os
import pkgutil
import sys
import types
import zlib
from importlib import import_module
from importlib.abc import Loader
from importlib.metadata import Distribution
from importlib.metadata import DistributionFinder
from typing import IO
from typing import Any
from typing import Iterable
from typing import NamedTuple
from typing import NewType
from typing import Protocol
from typing import Sequence
from typing import cast
from typing import runtime_checkable

ModuleName = NewType("ModuleName", str)


class DictDistribution(Distribution):
    data: dict[str, str]

    def __init__(self, data: dict[str, str]) -> None:
        self.data = data

    def read_text(self, filename):
        return self.data[filename]

    def locate_file(self, path: str | os.PathLike[str]) -> os.PathLike[str]:
        raise FileNotFoundError(path)


class ModuleInfo(NamedTuple):
    name: ModuleName
    is_pkg: bool
    source: str


class DictImporter(DistributionFinder, Loader):
    """a limited loader/importer for distributins send via json-lines"""

    def __init__(
        self, sources: dict[ModuleName, ModuleInfo], distribution: DictDistribution
    ):
        self.sources = sources
        self.distribution = distribution

    def find_distributions(
        self, context: DistributionFinder.Context | None = None
    ) -> Iterable[Distribution]:
        # TODO: filter
        return [self.distribution]

    def find_module(
        self, fullname: str, path: Sequence[str | bytes] | None = None
    ) -> Loader | None:
        if ModuleName(fullname) in self.sources:
            return self
        return None

    def load_module(self, fullname: str) -> types.ModuleType:
        # print "load_module:",  fullname

        info = self.sources[ModuleName(fullname)]

        co = compile(info.source, fullname, "exec")
        module = sys.modules.setdefault(fullname, types.ModuleType(fullname))
        module.__loader__ = self
        if info.is_pkg:
            module.__path__ = [fullname]

        exec(co, module.__dict__)
        return sys.modules[fullname]

    def get_source(self, name: str) -> str | None:
        res = self.sources.get(ModuleName(name))
        if res is None:
            return None
        else:
            return res.source


def bootstrap(
    modules: dict[ModuleName, ModuleInfo],
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


def bootstrap_stdin(stream: IO[bytes] | IO[str]) -> None:
    bootstrap_args = decode_b85_zip_json(stream.readline())
    bootstrap(**bootstrap_args)


def decode_b85_zip_json(encoded: bytes | str):
    packed = base64.b85decode(encoded)
    unpacked = zlib.decompress(packed)
    return json.loads(unpacked)


@runtime_checkable
class SourceProvidingLoader(Protocol):
    def get_source(self, name: str) -> str:
        ...


def naive_pack_module(module: types.ModuleType, dist: Distribution) -> object:
    assert module.__file__ is not None
    assert module.__path__
    data: dict[ModuleName, ModuleInfo] = {}
    for info in pkgutil.walk_packages(module.__path__, f"{module.__name__}."):
        spec = info.module_finder.find_spec(info.name, None)
        assert spec is not None
        loader = cast(SourceProvidingLoader, spec.loader)

        source = loader.get_source(info.name)
        data[ModuleName(info.name)] = ModuleInfo(
            name=ModuleName(info.name), is_pkg=info.ispkg, source=source
        )
    return data


if __name__ == "__main__":
    bootstrap_stdin(sys.stdin)
