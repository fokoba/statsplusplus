"""Data layer — DB access, schema management, and write pipelines.

This is the only layer that writes to the database. All writes flow through
refresh.py or fv_calc.py. The web layer and evaluation layer are read-only.

Public API:
    get_connection(league_dir) -> sqlite3.Connection
    init_schema(league_dir) -> None
    fv_calc.run(league_dir) -> None
    milb.load_milb_averages(league_dir) -> dict
    milb.load_milb_stat_seasons(conn, player_id, is_pitcher, averages) -> list
"""

from statsplusplus.data.db import get_connection, init_schema

__all__ = ["get_connection", "init_schema"]
