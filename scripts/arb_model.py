"""
arb_model.py — MLB arbitration and service time estimation.

MIGRATION NOTE: Pure salary computation delegates to
statsplusplus.evaluation.arb. Service time estimation (DB I/O)
remains here.

Public API:
  arb_salary(ovr, bucket, arb_year, prior_salary, min_sal)  → int
  arb_salary_perpetual(age, projected_war, dpw, min_sal, model=None) → int
  estimate_service_time(conn, player_id)                    → float
  estimate_control(conn, player_id, age, salary, bucket)    → (ctrl_years, salaries, pre_arb_left)
"""

import math

# Re-export pure computation from the package
from statsplusplus.evaluation.arb import (
    arb_salary,
    arb_salary_perpetual,
)
from statsplusplus.evaluation.constants import (
    SERVICE_GAMES_HITTER,
    SERVICE_STARTS_SP,
    SERVICE_GAMES_RP,
    ARB_DEEP_SALARY_THRESHOLD,
)


def estimate_service_time(conn, player_id):
    """Get MLB service time as fractional years.

    Prefers exact values from the players table (mlb_service_days).
    Falls back to games-based estimation.
    """
    row = conn.execute(
        "SELECT mlb_service_years, mlb_service_days FROM players WHERE player_id=?",
        (player_id,)
    ).fetchone()
    if row and row[0] is not None:
        days = row[1] or 0
        return days / 172.0

    return _estimate_service_time_from_games(conn, player_id)


def _estimate_service_time_from_games(conn, player_id):
    """Estimate fractional MLB service years from games played."""
    bat_by_year = {row[0]: row[1] for row in conn.execute(
        "SELECT year, SUM(g) FROM mlb_batting_stats WHERE player_id=? AND split_id=1 GROUP BY year",
        (player_id,)).fetchall()}

    pit_by_year = {row[0]: (row[1], row[2]) for row in conn.execute(
        "SELECT year, SUM(g), SUM(gs) FROM mlb_pitching_stats WHERE player_id=? AND split_id=1 GROUP BY year",
        (player_id,)).fetchall()}

    total = 0.0
    for yr in set(bat_by_year) | set(pit_by_year):
        bat_frac = bat_by_year.get(yr, 0) / SERVICE_GAMES_HITTER
        pg, pgs = pit_by_year.get(yr, (0, 0))
        pit_frac = (pgs / SERVICE_STARTS_SP if pgs >= pg * 0.5 else pg / SERVICE_GAMES_RP) if pg else 0.0
        total += min(1.0, max(bat_frac, pit_frac))
    return total


def estimate_control(conn, player_id, age, salary, bucket=None):
    """Estimate remaining team control years and salary schedule."""
    from player_utils import league_minimum
    from league_config import config as _cfg
    min_sal = league_minimum()
    svc = estimate_service_time(conn, player_id)

    arb_flag = conn.execute(
        "SELECT has_received_arbitration FROM players WHERE player_id=?",
        (player_id,)
    ).fetchone()
    has_arb = arb_flag[0] if arb_flag and arb_flag[0] is not None else None

    if _cfg.perpetual_arb:
        remaining = max(1, 38 - age)
        return remaining, [None] * remaining, 0

    if salary <= min_sal:
        if age >= 30 or (age >= 28 and svc >= 3) or svc >= 6:
            return None, None, None
        svc_years = int(svc)
        remaining = max(1, 6 - svc_years)
        pre_arb_left = max(0, 3 - svc_years)
        return remaining, [None] * remaining, pre_arb_left

    if age >= 30:
        return None, None, None

    est_svc = max(math.ceil(svc), 4 if salary > ARB_DEEP_SALARY_THRESHOLD else 3)
    remaining = max(1, 6 - est_svc)
    return remaining, [None] * remaining, 0
