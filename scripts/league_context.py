"""
league_context.py — resolves the active league directory.

MIGRATION NOTE: Delegates to statsplusplus.config.league_context.
All functions maintain their original signatures for backward compatibility.
"""

from statsplusplus.config.league_context import (
    get_active_league_slug,
    get_league_dir,
    get_statsplus_cookie,
    set_statsplus_cookie,
    APP_CONFIG_PATH,
)

__all__ = [
    "get_active_league_slug",
    "get_league_dir",
    "get_statsplus_cookie",
    "set_statsplus_cookie",
    "APP_CONFIG_PATH",
]
