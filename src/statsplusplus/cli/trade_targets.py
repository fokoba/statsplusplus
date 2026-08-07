"""CLI: Trade target finder by position.

Usage:
    spp-targets --bucket SP [--sellers-only] [--min-ovr 50] [--vs-hand R]
    python3 -m statsplusplus.cli.trade_targets --bucket SP
"""

from __future__ import annotations

import sys
from pathlib import Path


def main() -> None:
    """Entry point — delegates to the existing trade_targets script."""
    _root = Path(__file__).resolve().parent.parent.parent.parent
    scripts_dir = str(_root / "scripts")
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)

    import runpy
    runpy.run_path(str(_root / "scripts" / "trade_targets.py"), run_name="__main__")


if __name__ == "__main__":
    main()
