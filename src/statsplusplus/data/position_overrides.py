"""Position ratings kept from the game's own exports (see db.SCHEMA's
position_overrides comment).

The StatsPlus API leaves a pitcher's position-player ratings at 0, so a
pitcher with premium defensive/hitting position potential (an elite outfielder
listed as P) was invisible. The game's "All Columns" exports carry them for
every player; they are stored by player_id here and re-applied onto the latest
ratings after every API refresh wherever the API's value is blank or 0 — never
over a real API value.
"""
from __future__ import annotations

import datetime
import sqlite3

POS_COLS = (
    "p", "pot_p", "c", "pot_c", "first_b", "pot_first_b",
    "second_b", "pot_second_b", "third_b", "pot_third_b",
    "ss", "pot_ss", "lf", "pot_lf", "cf", "pot_cf", "rf", "pot_rf",
)


def save_position_overrides(conn: sqlite3.Connection, player_id: int, values: dict) -> bool:
    """Upsert the position ratings found in ``values`` (positive ints only).
    Columns absent from ``values`` keep whatever is already stored."""
    vals = {c: values[c] for c in POS_COLS if values.get(c) and values[c] > 0}
    if not vals:
        return False
    cols = list(vals)
    set_clause = ", ".join(f"{c}=excluded.{c}" for c in cols + ["updated_at"])
    conn.execute(
        f"INSERT INTO position_overrides (player_id, {', '.join(cols)}, updated_at) "
        f"VALUES (?, {', '.join('?' * len(cols))}, ?) "
        f"ON CONFLICT(player_id) DO UPDATE SET {set_clause}",
        [player_id] + [vals[c] for c in cols] + [datetime.datetime.now().isoformat()],
    )
    return True


def apply_position_overrides(conn: sqlite3.Connection, snapshot_date: str | None = None) -> int:
    """Fill blank/0 position ratings on ratings rows from position_overrides.

    Restricted to ``snapshot_date`` when given (a refresh only just inserted
    that snapshot); otherwise applies to every snapshot. Returns the number of
    column updates that touched at least one row.
    """
    try:
        if not conn.execute("SELECT 1 FROM position_overrides LIMIT 1").fetchone():
            return 0
    except sqlite3.Error:
        return 0
    where_date = " AND snapshot_date = ?" if snapshot_date else ""
    touched = 0
    for col in POS_COLS:
        params = [snapshot_date] if snapshot_date else []
        cur = conn.execute(
            f"UPDATE ratings SET {col} = (SELECT po.{col} FROM position_overrides po "
            f"WHERE po.player_id = ratings.player_id) "
            f"WHERE ({col} IS NULL OR {col} = 0){where_date} "
            f"AND player_id IN (SELECT player_id FROM position_overrides "
            f"WHERE {col} IS NOT NULL AND {col} > 0)",
            params,
        )
        if cur.rowcount and cur.rowcount > 0:
            touched += 1
    return touched
