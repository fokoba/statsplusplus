"""Salary another team keeps paying for a player we hold (OOTP "Retained Salary").

See the retained_salary table comment in db.py for why this lives in its own
table instead of being folded into the contract or salary_estimates rows.
"""

import datetime
from typing import Dict, Optional


def get_retention(conn, player_id: int) -> float:
    """Fraction (0-1) of this player's salary another team still pays."""
    try:
        row = conn.execute(
            "SELECT pct FROM retained_salary WHERE player_id=?", (int(player_id),)
        ).fetchone()
    except Exception:
        return 0.0
    return float(row[0]) if row and row[0] else 0.0


def get_retention_map(conn) -> Dict[int, float]:
    """{player_id: retained fraction} for every player with retention on file."""
    try:
        rows = conn.execute("SELECT player_id, pct FROM retained_salary").fetchall()
    except Exception:
        return {}
    return {int(r[0]): float(r[1]) for r in rows if r[1]}


def effective_retention(standing: float, extra: float = 0.0) -> float:
    """Combine retention already on the contract with a hypothetical extra cut.

    The two compound on what's left: if CIN already covers 50% and we then
    trade him on asking the next team to be covered for another 50% of what
    remains, the new team pays 25%, not 0%.
    """
    return 1.0 - (1.0 - standing) * (1.0 - extra)


def set_retention(conn, player_id: int, pct: Optional[float],
                  retained_by_team_id: Optional[int] = None,
                  note: Optional[str] = None) -> None:
    """Record (or clear, if pct is falsy) the retention on a player's contract."""
    if not pct:
        conn.execute("DELETE FROM retained_salary WHERE player_id=?", (int(player_id),))
    else:
        pct = float(pct)
        if not (0.0 < pct <= 1.0):
            raise ValueError(f"pct must be in (0, 1], got {pct!r}")
        conn.execute(
            """INSERT INTO retained_salary
                   (player_id, retained_by_team_id, pct, note, updated_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(player_id) DO UPDATE SET
                   retained_by_team_id=excluded.retained_by_team_id,
                   pct=excluded.pct, note=excluded.note,
                   updated_at=excluded.updated_at""",
            (int(player_id), retained_by_team_id, pct, note,
             datetime.datetime.now().isoformat()),
        )
    conn.commit()
