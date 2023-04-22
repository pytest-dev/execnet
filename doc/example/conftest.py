import os
import sys
from pathlib import Path


def _add_path(path: Path):
    strpath = os.fspath(path)
    if strpath not in sys.path:
        sys.path.insert(0, strpath)


mydir = Path(__file__).parent
# make execnet and example code importable

cand = mydir.parent.parent
if cand.joinpath("execnet", "__init__.py").is_file():
    _add_path(cand)
_add_path(mydir)
pytest_plugins = ["doctest"]
