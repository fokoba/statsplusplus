"""Active league directory resolution.

Resolves which league is active and provides the path to its data directory.
Used by both CLI (scripts) and web (Flask) to find data/<league>/.

The web layer overrides via Flask `g`; scripts use the default resolution
(from app_config.json or STATSPP_LEAGUE environment variable).
"""

from __future__ import annotations

import json
import os
from pathlib import Path


def _project_root() -> Path:
    """Resolve the project root directory.

    Walks up from this file's location to find the directory containing 'data/'.
    """
    # This file is at src/statsplusplus/config/league_context.py
    # Project root is 4 levels up: config -> statsplusplus -> src -> root
    candidate = Path(__file__).resolve().parent.parent.parent.parent
    if (candidate / "data").exists():
        return candidate
    # Fallback: try cwd
    cwd = Path.cwd()
    if (cwd / "data").exists():
        return cwd
    return candidate


APP_CONFIG_PATH: Path = _project_root() / "data" / "app_config.json"


def _read_app_config() -> dict[str, str]:
    """Read the global app config (active league, cookie)."""
    if APP_CONFIG_PATH.exists():
        try:
            data = json.loads(APP_CONFIG_PATH.read_text())
            return {str(k): str(v) for k, v in data.items()}
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def get_active_league_slug() -> str:
    """Get the active league slug.

    Priority:
      1. STATSPP_LEAGUE environment variable
      2. app_config.json active_league key
      3. Default "emlb"
    """
    return os.environ.get("STATSPP_LEAGUE") or _read_app_config().get("active_league", "emlb")


def get_league_dir(slug: str | None = None) -> Path:
    """Return the data directory for a league.

    Args:
        slug: League slug (e.g., "emlb", "vmlb"). If None, uses active league.

    Returns:
        Path to data/<slug>/ directory.
    """
    if slug is None:
        slug = get_active_league_slug()
    root = _project_root()
    league_dir = root / "data" / slug
    if league_dir.exists():
        return league_dir
    # Legacy fallback: pre-migration single-league installs
    legacy_db = root / "emlb.db"
    if legacy_db.exists():
        return root
    return league_dir  # Will fail downstream with clear path


def get_statsplus_cookie(league_dir: Path | None = None) -> str:
    """Get the StatsPlus session cookie for a league.

    Priority:
      1. Per-league state.json (statsplus_cookie key)
      2. Global app_config.json (legacy single-league installs)
      3. statsplus/.env file (oldest legacy format)

    Args:
        league_dir: League data directory. If None, resolves from active league.

    Returns:
        Cookie string, or empty string if not configured.
    """
    if league_dir is None:
        league_dir = get_league_dir()
    # Per-league cookie
    state_path = league_dir / "config" / "state.json"
    if state_path.exists():
        try:
            state = json.loads(state_path.read_text())
            cookie = str(state.get("statsplus_cookie", ""))
            if cookie:
                return cookie
        except (json.JSONDecodeError, OSError):
            pass
    # Global fallback
    cfg = _read_app_config()
    cookie = cfg.get("statsplus_cookie", "")
    if cookie:
        return str(cookie)
    # Legacy .env fallback
    root = _project_root()
    env_path = root / "statsplus" / ".env"
    if env_path.exists():
        try:
            for line in env_path.read_text().splitlines():
                if line.startswith("STATSPLUS_COOKIE="):
                    return line.split("=", 1)[1].strip()
        except OSError:
            pass
    return ""


def set_statsplus_cookie(cookie: str, league_dir: Path | None = None) -> None:
    """Persist the StatsPlus cookie for a specific league.

    Stores in the league's state.json file.

    Args:
        cookie: Session cookie string.
        league_dir: Target league directory. If None, uses active league.
    """
    if league_dir is None:
        league_dir = get_league_dir()
    config_dir = league_dir / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    state_path = config_dir / "state.json"
    state: dict[str, object] = {}
    if state_path.exists():
        try:
            state = json.loads(state_path.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    state["statsplus_cookie"] = cookie
    state_path.write_text(json.dumps(state, indent=2) + "\n")


def get_statsplus_token(league_dir: Path | None = None) -> str:
    """Get the StatsPlus team API token for a league — the sanctioned
    authentication method (see https://wiki.statsplus.net/web-tools/statsplus-api),
    a per-team token from that league's Preferences page on the StatsPlus
    site, passed as a ?token= query param. Preferred over the session
    cookie when present; the cookie remains the fallback for leagues that
    haven't configured a token yet.

    Priority: per-league state.json (statsplus_token key), then the
    global app_config.json (legacy single-league installs).
    """
    if league_dir is None:
        league_dir = get_league_dir()
    state_path = league_dir / "config" / "state.json"
    if state_path.exists():
        try:
            state = json.loads(state_path.read_text())
            token = str(state.get("statsplus_token", ""))
            if token:
                return token
        except (json.JSONDecodeError, OSError):
            pass
    cfg = _read_app_config()
    token = cfg.get("statsplus_token", "")
    return str(token) if token else ""


def set_statsplus_token(token: str, league_dir: Path | None = None) -> None:
    """Persist the StatsPlus team API token for a specific league.

    Stores in the league's state.json file, same pattern as
    set_statsplus_cookie().
    """
    if league_dir is None:
        league_dir = get_league_dir()
    config_dir = league_dir / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    state_path = config_dir / "state.json"
    state: dict[str, object] = {}
    if state_path.exists():
        try:
            state = json.loads(state_path.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    state["statsplus_token"] = token
    state_path.write_text(json.dumps(state, indent=2) + "\n")
