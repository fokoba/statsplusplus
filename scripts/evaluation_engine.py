#!/usr/bin/env python3
"""evaluation_engine.py — Thin shim delegating to the package.

All computation and orchestration lives in:
    statsplusplus.data.evaluation_engine
"""

import os, sys

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_BASE, "scripts"))

# Import the package module and re-export all its attributes
import statsplusplus.data.evaluation_engine as _pkg

# Make all attributes accessible from this shim
from statsplusplus.data.evaluation_engine import *  # noqa: F401, F403

# Explicitly pull private/underscore names that tests or other scripts import
_private_names = [name for name in dir(_pkg) if not name.startswith("__")]
_g = globals()
for _name in _private_names:
    if _name not in _g:
        _g[_name] = getattr(_pkg, _name)
del _g, _private_names, _name

if __name__ == "__main__":
    from statsplusplus.data.evaluation_engine import run
    run()
