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


def load_retired_ids(league_dir) -> list[int]:
    """`retired_ids` in the same file: players who retired in-game but whom
    StatsPlus still lists (e.g. as a free agent). Kept in the DB, marked retired."""
    path = Path(league_dir) / "config" / "excluded_players.json"
    try:
        return [int(x) for x in json.loads(path.read_text()).get("retired_ids", [])]
    except (json.JSONDecodeError, OSError, ValueError):
        return []


def purge_excluded(league_dir, conn: sqlite3.Connection | None = None) -> int:
    """Delete every excluded player from all tables with a player_id column,
    and mark `retired_ids` retired. Returns the number of rows deleted."""
    ids = load_excluded_ids(league_dir)
    retired = load_retired_ids(league_dir)
    if not ids and not retired:
        return 0
    own = conn is None
    if own:
        conn = sqlite3.connect(str(Path(league_dir) / "league.db"), timeout=30)
    try:
        if retired:
            conn.execute(
                f"UPDATE players SET retired=1, free_agent=0, team_id=0, parent_team_id=0 "
                f"WHERE player_id IN ({','.join('?' * len(retired))})", retired)
            conn.commit()
        if not ids:
            return 0
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
