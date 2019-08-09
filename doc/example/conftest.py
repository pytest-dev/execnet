# -*- coding: utf-8 -*-
import sys

import py

# make execnet and example code importable
cand = py.path.local(__file__).dirpath().dirpath().dirpath()
if cand.join("execnet", "__init__.py").check():
    if str(cand) not in sys.path:
        sys.path.insert(0, str(cand))
cand = py.path.local(__file__).dirpath()
if str(cand) not in sys.path:
    sys.path.insert(0, str(cand))

pytest_plugins = ["doctest"]
