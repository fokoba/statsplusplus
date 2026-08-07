#!/usr/bin/env python3
"""discord_post.py — Post patch notes to Discord webhook.

Usage:
  python3 scripts/discord_post.py message "Your message here"
  python3 scripts/discord_post.py latest
  python3 scripts/discord_post.py preview
"""

from statsplusplus.cli.discord_post import main

if __name__ == "__main__":
    main()
