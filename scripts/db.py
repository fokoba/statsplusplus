"""
db.py — SQLite connection and schema initialization.

MIGRATION NOTE: Delegates to statsplusplus.data.db for all functionality.
Maintains backward compatibility for the `get_conn()` function name and
the individual migration functions used by tests.
"""

import sqlite3
from pathlib import Path

from statsplusplus.data.db import (
    get_connection,
    init_schema,
    SCHEMA,
)


def get_conn(league_dir: Path | None = None):
    """Get a DB connection. Legacy name for get_connection()."""
    return get_connection(league_dir)


# --- Legacy migration functions (for test backward compat) ---

def _migrate_ratings(conn: sqlite3.Connection):
    """Add columns introduced after the initial ratings schema."""
    existing = {row[1] for row in conn.execute("PRAGMA table_info(ratings)").fetchall()}
    new_cols = [
        "ctrl", "p", "pot_p", "stl_rt", "run", "sac_bunt", "bunt_hit", "hold",
        "babip", "babip_l", "babip_r", "pot_babip",
        "hra", "hra_l", "hra_r", "pot_hra",
        "pbabip", "pbabip_l", "pbabip_r", "pot_pbabip", "prone",
        "composite_score", "ceiling_score", "tool_only_score", "secondary_composite",
        "true_ceiling", "positional_percentile", "positional_median",
        "offensive_grade", "baserunning_value", "defensive_value",
        "durability_score", "offensive_ceiling",
    ]
    for col in new_cols:
        if col not in existing:
            typ = "REAL" if col == "positional_percentile" else (
                "TEXT" if col == "prone" else "INTEGER"
            )
            conn.execute(f"ALTER TABLE ratings ADD COLUMN {col} {typ}")


def _migrate_ratings_history(conn: sqlite3.Connection):
    """Add columns introduced after the initial ratings_history schema."""
    existing = {row[1] for row in conn.execute("PRAGMA table_info(ratings_history)").fetchall()}
    new_cols = [
        "composite_score", "ceiling_score",
        "offensive_grade", "baserunning_value", "defensive_value",
        "durability_score", "offensive_ceiling",
    ]
    for col in new_cols:
        if col not in existing:
            conn.execute(f"ALTER TABLE ratings_history ADD COLUMN {col} INTEGER")


def _migrate_ratings_components(conn: sqlite3.Connection):
    """Add component score columns to ratings and ratings_history."""
    new_cols = ["offensive_grade", "baserunning_value", "defensive_value",
                "durability_score", "offensive_ceiling"]
    for table in ("ratings", "ratings_history"):
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        for col in new_cols:
            if col not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} INTEGER")


__all__ = ["get_conn", "init_schema", "SCHEMA",
           "_migrate_ratings", "_migrate_ratings_history", "_migrate_ratings_components"]
