"""
league_context.py — resolves the active league directory.

Used by scripts (CLI) and web (Flask) to find the right data/<league>/ path.
Web layer overrides via Flask `g`; scripts use the default resolution.
"""

import json, os
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
APP_CONFIG_PATH = _ROOT / "data" / "app_config.json"

# Legacy paths (pre-migration)
_LEGACY_DB = _ROOT / "emlb.db"
_LEGACY_META = _ROOT / "config"


def _read_app_config() -> dict:
    if APP_CONFIG_PATH.exists():
        return json.loads(APP_CONFIG_PATH.read_text())
    return {}


def get_active_league_slug() -> str:
    return os.environ.get("STATSPP_LEAGUE") or _read_app_config().get("active_league", "emlb")


def get_league_dir(slug: str | None = None) -> Path:
    """Return the data directory for a league. Falls back to legacy layout."""
    if slug is None:
        slug = get_active_league_slug()
    league_dir = _ROOT / "data" / slug
    if league_dir.exists():
        return league_dir
    # Legacy fallback: pre-migration, everything is at project root
    if _LEGACY_DB.exists():
        return _ROOT
    return league_dir  # will fail downstream with clear path


def get_statsplus_cookie(league_dir: Path | None = None) -> str:
    """StatsPlus session cookie for a league.

    Different leagues (e.g. two OOTP leagues hosted under separate
    statsplus.net accounts) can require entirely different login sessions,
    so the cookie lives per-league in <league_dir>/config/state.json —
    not in the shared app_config.json — the same place other per-league
    runtime state (game_date, my_team_id) already lives.

    Falls back to the legacy global app_config.json key (pre-migration
    single-league installs), then statsplus/.env, if the per-league value
    isn't set.
    """
    if league_dir is None:
        league_dir = get_league_dir()
    state_path = league_dir / "config" / "state.json"
    if state_path.exists():
        state = json.loads(state_path.read_text())
        cookie = state.get("statsplus_cookie", "")
        if cookie:
            return cookie
    # Legacy fallback: shared global cookie (pre-per-league-cookie installs)
    cfg = _read_app_config()
    cookie = cfg.get("statsplus_cookie", "")
    if cookie:
        return cookie
    # Legacy fallback: read from statsplus/.env
    env_path = _ROOT / "statsplus" / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            if line.startswith("STATSPLUS_COOKIE="):
                return line.split("=", 1)[1].strip()
    return ""


def set_statsplus_cookie(cookie: str, league_dir: Path | None = None) -> None:
    """Persist the StatsPlus cookie for a specific league's own state.json."""
    if league_dir is None:
        league_dir = get_league_dir()
    config_dir = league_dir / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    state_path = config_dir / "state.json"
    state = json.loads(state_path.read_text()) if state_path.exists() else {}
    state["statsplus_cookie"] = cookie
    state_path.write_text(json.dumps(state, indent=2) + "\n")
