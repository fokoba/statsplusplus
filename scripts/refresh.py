#!/usr/bin/env python3
"""
refresh.py — Full league data refresh (thin shim).

Usage:
  python3 scripts/refresh.py [year]                    # Full league refresh
  python3 scripts/refresh.py state <game_date> [year]  # Update state only

Delegates to statsplusplus.data.refresh:main. See that module for details.
"""

import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "src"))
sys.path.insert(0, BASE)

from statsplusplus.data.refresh import main

if __name__ == "__main__":
    main()
