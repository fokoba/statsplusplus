"""CLI: Positional needs vs league average.

Usage:
    spp-needs [--team ABBR]
    python3 -m statsplusplus.cli.team_needs
"""

from __future__ import annotations

import sys
from pathlib import Path


def main() -> None:
    """Entry point — delegates to the existing team_needs script."""
    _root = Path(__file__).resolve().parent.parent.parent.parent
    scripts_dir = str(_root / "scripts")
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)

    import runpy
    runpy.run_path(str(_root / "scripts" / "team_needs.py"), run_name="__main__")


if __name__ == "__main__":
    main()
