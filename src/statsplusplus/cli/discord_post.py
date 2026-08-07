"""CLI: Post changelog/updates to Discord webhook.

Usage:
    python3 -m statsplusplus.cli.discord_post message "Title" "Content"
    python3 -m statsplusplus.cli.discord_post latest
"""

from __future__ import annotations

import sys
from pathlib import Path


def main() -> None:
    """Entry point — delegates to the existing discord_post script."""
    _root = Path(__file__).resolve().parent.parent.parent.parent
    scripts_dir = str(_root / "scripts")
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)

    import runpy
    runpy.run_path(str(_root / "scripts" / "discord_post.py"), run_name="__main__")


if __name__ == "__main__":
    main()
