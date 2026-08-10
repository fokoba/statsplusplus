"""Request-scoped context for the Flask web layer.

Provides a single DB connection per request (cached on Flask `g`) and
cached accessors for frequently-needed state (game date, year, eval date,
league averages). This eliminates the legacy pattern of opening 17+
connections per page load with repeated state file reads.

Usage in query modules:
    from statsplusplus.web.context import get_conn, get_state, get_eval_date, get_cfg

Design:
    - get_conn(): Returns the same connection for the entire request lifecycle.
      Closed automatically on teardown.
    - get_state(): Reads state.json once per request, caches on g.
    - get_eval_date(): Queries MAX(eval_date) once per request, caches on g.
    - get_cfg(): Returns the LeagueConfig for the current request's league.
    - All functions work outside Flask context (fallback to default league).
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Optional

from flask import g, has_request_context


def get_conn() -> sqlite3.Connection:
    """Get the request-scoped database connection.

    Returns the same connection for all queries within a single request.
    The connection is closed automatically by close_conn() on teardown.

    Outside Flask request context, opens a new connection (for CLI/test use).
    """
    if has_request_context():
        if not hasattr(g, "_db_conn") or g._db_conn is None:
            from statsplusplus.data.db import get_connection
            g._db_conn = get_connection(g.league_dir)
        return g._db_conn

    # Outside request context — fallback
    from statsplusplus.data.db import get_connection
    return get_connection()


def close_conn(exc: Optional[BaseException] = None) -> None:
    """Close the request-scoped connection. Called on request teardown."""
    if has_request_context():
        conn = getattr(g, "_db_conn", None)
        if conn is not None:
            conn.close()
            g._db_conn = None


def get_cfg() -> Any:
    """Get the LeagueConfig for the current request's league."""
    if has_request_context() and hasattr(g, "league_config"):
        return g.league_config
    # Fallback for non-request contexts
    from statsplusplus.config.league_context import get_league_dir
    # Import legacy LeagueConfig for now — will be replaced in later migration
    from statsplusplus.config.league_config import LeagueConfig
    return LeagueConfig()


def get_state() -> dict[str, Any]:
    """Get the current game state (game_date, year), cached per request.

    Reads state.json once per request. Also determines the stats_year
    (most recent year with data, for preseason handling).
    """
    if has_request_context() and hasattr(g, "_state_cache"):
        return g._state_cache

    cfg = get_cfg()
    state_path = cfg.state_path if hasattr(cfg, "state_path") else (
        cfg.league_dir / "config" / "state.json"
    )
    with open(state_path) as f:
        state = json.load(f)

    # Determine stats_year: most recent year with data
    conn = get_conn()
    row = conn.execute(
        "SELECT MAX(year) FROM mlb_batting_stats WHERE year <= ?", (state["year"],)
    ).fetchone()
    state["stats_year"] = row[0] if row and row[0] else state["year"]

    if has_request_context():
        g._state_cache = state
    return state


def get_eval_date() -> Optional[str]:
    """Get the most recent evaluation date, cached per request.

    This is the eval_date used for player_surplus and prospect_fv lookups.
    Queried once and reused for all queries in the request.
    """
    if has_request_context() and hasattr(g, "_eval_date_cache"):
        return g._eval_date_cache

    conn = get_conn()
    row = conn.execute("SELECT MAX(eval_date) FROM player_surplus").fetchone()
    eval_date = row[0] if row else None

    if has_request_context():
        g._eval_date_cache = eval_date
    return eval_date


def get_league_averages() -> dict[str, Any]:
    """Load league_averages.json, cached per request."""
    if has_request_context() and hasattr(g, "_la_cache"):
        return g._la_cache

    cfg = get_cfg()
    league_dir = cfg.league_dir if hasattr(cfg, "league_dir") else get_league_dir_from_g()
    path = league_dir / "config" / "league_averages.json"

    if path.exists():
        result = json.loads(path.read_text())
    else:
        result = {
            "year": cfg.year, "teams_in_sample": 0,
            "batting": {"avg": 0, "obp": 0, "slg": 0, "ops": 0, "woba": 0,
                        "babip": 0, "iso": 0, "k_pct": 0, "bb_pct": 0},
            "pitching": {"era": 0, "fip": 0, "x_fip": 0, "k_pct": 0, "bb_pct": 0,
                         "k_bb_pct": 0, "babip": 0, "avg": 0, "obp": 0},
            "dollar_per_war": 0,
        }

    if has_request_context():
        g._la_cache = result
    return result


def get_league_dir_from_g() -> Path:
    """Get league_dir from Flask g or fallback."""
    if has_request_context() and hasattr(g, "league_dir"):
        return g.league_dir
    from statsplusplus.config.league_context import get_league_dir
    return get_league_dir()


# ---------------------------------------------------------------------------
# Convenience accessors (replace web_league_context.py functions)
# ---------------------------------------------------------------------------

def team_abbr_map() -> dict[int, str]:
    return get_cfg().team_abbr_map


def team_names_map() -> dict[int, str]:
    return get_cfg().team_names_map


def team_div_map() -> dict[int, str]:
    return get_cfg().team_div_map


def mlb_team_ids() -> set[int]:
    return get_cfg().mlb_team_ids


def level_map() -> dict[str, str]:
    return get_cfg().level_map


def pos_map() -> dict[int, str]:
    return get_cfg().pos_map


def pos_order() -> dict[str, int]:
    return get_cfg().pos_order


def pyth_exp() -> float:
    return get_cfg().pyth_exp


def year() -> int:
    return get_cfg().year


def my_team_id() -> int:
    return get_cfg().my_team_id


def has_extended_ratings() -> bool:
    """Check if the ratings table has extended columns (babip, hra, etc.)."""
    if has_request_context() and hasattr(g, "_has_ext_ratings"):
        return g._has_ext_ratings

    conn = get_conn()
    cols = {row[1] for row in conn.execute("PRAGMA table_info(ratings)").fetchall()}
    result = "babip" in cols

    if has_request_context():
        g._has_ext_ratings = result
    return result
