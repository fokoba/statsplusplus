"""CLI: MLB roster scaffold generator.

Usage:
    spp-roster
    python3 -m statsplusplus.cli.roster_analysis
"""

from __future__ import annotations

import sys
from pathlib import Path


def main() -> None:
    """Entry point — delegates to the existing roster_analysis script."""
    _root = Path(__file__).resolve().parent.parent.parent.parent
    scripts_dir = str(_root / "scripts")
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)

    import runpy
    runpy.run_path(str(_root / "scripts" / "roster_analysis.py"), run_name="__main__")


if __name__ == "__main__":
    main()
