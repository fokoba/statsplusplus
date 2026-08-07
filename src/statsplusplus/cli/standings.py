"""CLI: League standings via pythagorean expectation.

Usage:
    spp-standings [--year YEAR] [--actual] [--team ABBR]
    python3 -m statsplusplus.cli.standings [--year YEAR] [--actual] [--team ABBR]
"""

from __future__ import annotations

import sys
from pathlib import Path


def main() -> None:
    """Entry point — delegates to the existing standings script."""
    _root = Path(__file__).resolve().parent.parent.parent.parent
    scripts_dir = str(_root / "scripts")
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)

    # The existing script uses if __name__ == "__main__" to run
    import runpy
    runpy.run_path(str(_root / "scripts" / "standings.py"), run_name="__main__")


if __name__ == "__main__":
    main()
