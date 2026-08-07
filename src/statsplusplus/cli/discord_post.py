"""CLI: Post changelog/updates to Discord webhook.

Usage:
    spp-discord message "Your message here"
    python3 scripts/discord_post.py message "text"
    python3 scripts/discord_post.py latest
    python3 scripts/discord_post.py preview
"""

from __future__ import annotations

import json
import sys
import urllib.request
import urllib.error
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent.parent.parent
CONFIG_PATH = BASE / "data" / "discord_config.json"


def main() -> None:
    """Entry point — runs the discord_post script logic."""
    # Add scripts/ to path so the full implementation can be imported
    scripts_dir = str(BASE / "scripts")
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)

    # Re-use the scripts/discord_post.py implementation directly
    import importlib.util
    spec = importlib.util.spec_from_file_location("discord_post_impl", str(BASE / "scripts" / "discord_post.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)


if __name__ == "__main__":
    main()
