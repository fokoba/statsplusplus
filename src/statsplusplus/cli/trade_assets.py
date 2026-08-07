"""CLI: Tradeable assets for any team.

Usage:
    spp-assets [--team ABBR] [--min-surplus N]
    python3 -m statsplusplus.cli.trade_assets
"""

from __future__ import annotations

import sys
from pathlib import Path


def main() -> None:
    """Entry point — delegates to the existing trade_assets script."""
    _root = Path(__file__).resolve().parent.parent.parent.parent
    scripts_dir = str(_root / "scripts")
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)

    import runpy
    runpy.run_path(str(_root / "scripts" / "trade_assets.py"), run_name="__main__")


if __name__ == "__main__":
    main()
