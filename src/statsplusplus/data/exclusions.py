"""Persistent per-league player exclusions (e.g. career-ending injuries).

A player deleted from the DB is re-inserted by the next OOTP ratings import or
API refresh, since they still exist in the game. `config/excluded_players.json`
({"player_ids": [...]}) is the durable record; purge_excluded() deletes those
players from every table with a player_id column and is called after each
import/refresh so they stay gone.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path


def load_excluded_ids(league_dir) -> list[int]:
    path = Path(league_dir) / "config" / "excluded_players.json"
    if not path.exists():
        return []
    try:
        return [int(x) for x in json.loads(path.read_text()).get("player_ids", [])]
    except (json.JSONDecodeError, OSError, ValueError):
        return []


def purge_excluded(league_dir, conn: sqlite3.Connection | None = None) -> int:
    """Delete every excluded player from all tables with a player_id column.
    Returns the number of rows deleted (0 when nothing was present)."""
    ids = load_excluded_ids(league_dir)
    if not ids:
        return 0
    own = conn is None
    if own:
        conn = sqlite3.connect(str(Path(league_dir) / "league.db"), timeout=30)
    try:
        qs = ",".join("?" * len(ids))
        total = 0
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        for t in tables:
            cols = {r[1] for r in conn.execute(f'PRAGMA table_info("{t}")').fetchall()}
            if "player_id" in cols:
                total += conn.execute(
                    f'DELETE FROM "{t}" WHERE player_id IN ({qs})', ids).rowcount
        conn.commit()
        return total
    finally:
        if own:
            conn.close()
