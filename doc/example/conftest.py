import pathlib
import sys


# Make execnet and example code importable.
cand = pathlib.Path(__file__).parent.parent.parent
if cand.joinpath("execnet", "__init__.py").exists():
    if str(cand) not in sys.path:
        sys.path.insert(0, str(cand))
cand = pathlib.Path(__file__).parent
if str(cand) not in sys.path:
    sys.path.insert(0, str(cand))

pytest_plugins = ["doctest"]
