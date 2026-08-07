"""CLI: Farm system report generator.

Usage:
    spp-farm
    python3 -m statsplusplus.cli.farm_analysis
"""

from __future__ import annotations

import sys
from pathlib import Path


def main() -> None:
    """Entry point — delegates to the existing farm_analysis script."""
    _root = Path(__file__).resolve().parent.parent.parent.parent
    scripts_dir = str(_root / "scripts")
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)

    import runpy
    runpy.run_path(str(_root / "scripts" / "farm_analysis.py"), run_name="__main__")


if __name__ == "__main__":
    main()
