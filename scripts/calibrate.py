#!/usr/bin/env python3
"""
calibrate.py — Per-league model calibration (thin shim).

Usage:
  python3 scripts/calibrate.py [--dry-run]

Delegates to statsplusplus.data.calibrate:main. See that module for details.
"""

import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "src"))
sys.path.insert(0, BASE)

from statsplusplus.data.calibrate import main

if __name__ == "__main__":
    main()
