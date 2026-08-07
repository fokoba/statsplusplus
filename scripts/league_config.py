"""
league_config.py — single source of truth for all league-specific settings.

MIGRATION NOTE: LeagueConfig class now lives in statsplusplus.config.league_config.
This file re-exports it and maintains the module-level singleton for backward compat.
"""

from statsplusplus.config.league_config import LeagueConfig

# Module-level singleton — legacy code imports `from league_config import config`
config = LeagueConfig()
