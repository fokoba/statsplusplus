#!/usr/bin/env python3
"""refresh.py — Thin shim delegating to the package.

All refresh pipeline logic lives in:
    statsplusplus.data.refresh
"""

import os, sys, runpy

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_BASE, "scripts"))
sys.path.insert(0, _BASE)

# Run the package module as __main__
runpy.run_module("statsplusplus.data.refresh", run_name="__main__", alter_sys=True)
