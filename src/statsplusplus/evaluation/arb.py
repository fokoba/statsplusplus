"""Arbitration salary projection.

Pure computation of arbitration salary estimates from player metrics.
No DB access in the core calculation functions.

Public API:
    arb_salary(ovr, bucket, arb_year, prior_salary, min_sal) -> int
    arb_salary_perpetual(age, projected_war, dpw, min_sal, ...) -> int
"""

from __future__ import annotations

import math
from typing import Any, Optional

from statsplusplus.evaluation.constants import (
    ARB_HITTER_BASE,
    ARB_HITTER_EXP,
    ARB_RP_BASE,
    ARB_RP_EXP,
    ARB_RAISE_INTERCEPT,
    ARB_RAISE_SLOPE,
    ARB_RAISE_MIN,
    ARB_DEEP_SALARY_THRESHOLD,
    DEFAULT_MINIMUM_SALARY,
    SERVICE_GAMES_HITTER,
    SERVICE_STARTS_SP,
    SERVICE_GAMES_RP,
)

# Default perpetual arb model parameters.
# Calibrated from PPL cross-sectional data (1-year contracts, WAR >= 1.0).
_DEFAULT_PERP_K: float = 2400.0
_DEFAULT_PERP_EXP: float = 0.72
_DEFAULT_PERP_DISCOUNT: float = 7.0
_DEFAULT_PERP_CEILING_PCT: float = 0.35


def arb_salary(
    ovr: int | float,
    bucket: str,
    arb_year: int,
    prior_salary: int,
    min_sal: int,
) -> int:
    """Project arb salary for a given arb year (1-indexed).

    Accepts either OVR or composite_score (both on 20-80 scale).
    Uses RP-specific model for RPs, hitter/SP model for all others.

    Args:
        ovr: OVR or composite score (20-80).
        bucket: Positional bucket.
        arb_year: 1, 2, or 3.
        prior_salary: Previous year's salary (for raise calc).
        min_sal: League minimum salary.

    Returns:
        Projected salary as integer.
    """
    # Scale for drastically different salary environments
    ratio = min_sal / DEFAULT_MINIMUM_SALARY if min_sal and DEFAULT_MINIMUM_SALARY else 1.0
    scale = ratio if ratio < 0.5 else 1.0

    if bucket == "RP":
        rp_base = ARB_RP_BASE * math.exp(ARB_RP_EXP * ovr)
        return round(rp_base * (0.75 + 0.25 * (arb_year - 1)) * scale)

    if arb_year == 1:
        return round(ARB_HITTER_BASE * math.exp(ARB_HITTER_EXP * ovr) * scale)

    raise_amt = max(ARB_RAISE_MIN, round(ARB_RAISE_INTERCEPT + ARB_RAISE_SLOPE * ovr))
    return prior_salary + round(raise_amt * scale)


def arb_salary_perpetual(
    age: int,
    projected_war: float,
    dpw: int,
    min_sal: int,
    career_war: float = 0.0,
    model: Optional[dict[str, Any]] = None,
) -> int:
    """Project salary for a perpetual arb league.

    In perpetual arb, salary is recalculated yearly based on accumulated
    track record (career WAR) with a ceiling tied to current production.

    Model: salary = min(growth, ceiling), floored at league minimum.
      growth  = min_sal + k × max(0, career_WAR - discount)^exp
      ceiling = ceiling_pct × current_WAR × $/WAR

    Args:
        age: Player age for the projected year.
        projected_war: WAR projection for that year.
        dpw: League dollars-per-WAR.
        min_sal: League minimum salary.
        career_war: Cumulative career WAR entering this year.
        model: Calibrated model dict (k, exp, discount, ceiling_pct).

    Returns:
        Projected salary as integer.
    """
    if model:
        k = model.get("k", _DEFAULT_PERP_K)
        exp = model.get("exp", _DEFAULT_PERP_EXP)
        discount = model.get("discount", _DEFAULT_PERP_DISCOUNT)
        ceiling_pct = model.get("ceiling_pct", _DEFAULT_PERP_CEILING_PCT)
    else:
        k = _DEFAULT_PERP_K
        exp = _DEFAULT_PERP_EXP
        discount = _DEFAULT_PERP_DISCOUNT
        ceiling_pct = _DEFAULT_PERP_CEILING_PCT

    # Growth: ramps with accumulated career production
    effective_war = max(0.0, career_war - discount)
    growth_sal = min_sal + k * (effective_war ** exp) if effective_war > 0 else min_sal

    # Ceiling: can't exceed ceiling_pct of current market value
    ceiling_sal = ceiling_pct * projected_war * dpw if projected_war > 0 else float(min_sal)

    salary = max(min_sal, min(growth_sal, ceiling_sal))
    return int(round(salary))


# ---------------------------------------------------------------------------
# Service time estimation (requires DB access)
# ---------------------------------------------------------------------------


def estimate_service_time(conn: Any, player_id: int) -> float:
    """Get MLB service time as fractional years.

    Prefers exact values from the players table (mlb_service_days).
    Falls back to games-based estimation.

    Args:
        conn: SQLite connection.
        player_id: Player ID.

    Returns:
        Fractional years of MLB service (e.g. 3.5 = 3 years, 86 days).
    """
    row = conn.execute(
        "SELECT mlb_service_years, mlb_service_days FROM players WHERE player_id=?",
        (player_id,)
    ).fetchone()
    if row and row[0] is not None:
        days = row[1] or 0
        return days / 172.0

    return _estimate_service_time_from_games(conn, player_id)


def _estimate_service_time_from_games(conn: Any, player_id: int) -> float:
    """Estimate fractional MLB service years from games played."""
    bat_by_year: dict[int, int] = {row[0]: row[1] for row in conn.execute(
        "SELECT year, SUM(g) FROM mlb_batting_stats WHERE player_id=? AND split_id=1 GROUP BY year",
        (player_id,)).fetchall()}

    pit_by_year: dict[int, tuple[int, int]] = {row[0]: (row[1], row[2]) for row in conn.execute(
        "SELECT year, SUM(g), SUM(gs) FROM mlb_pitching_stats WHERE player_id=? AND split_id=1 GROUP BY year",
        (player_id,)).fetchall()}

    total = 0.0
    for yr in set(bat_by_year) | set(pit_by_year):
        bat_frac = bat_by_year.get(yr, 0) / SERVICE_GAMES_HITTER
        pg, pgs = pit_by_year.get(yr, (0, 0))
        pit_frac = (pgs / SERVICE_STARTS_SP if pgs >= pg * 0.5 else pg / SERVICE_GAMES_RP) if pg else 0.0
        total += min(1.0, max(bat_frac, pit_frac))
    return total


def estimate_control(
    conn: Any,
    player_id: int,
    age: int,
    salary: int,
    min_sal: int,
    perpetual_arb: bool = False,
    bucket: Optional[str] = None,
) -> tuple[Optional[int], Optional[list[None]], int]:
    """Estimate remaining team control years and salary schedule.

    Args:
        conn: SQLite connection.
        player_id: Player ID.
        age: Player's current age.
        salary: Current salary.
        min_sal: League minimum salary.
        perpetual_arb: Whether the league uses perpetual arb (no free agency).
        bucket: Positional bucket (unused currently, reserved for future).

    Returns:
        Tuple of (remaining_years, salary_schedule, pre_arb_years_left).
        Returns (None, None, None) if player appears to be a free agent.
    """
    svc = estimate_service_time(conn, player_id)

    arb_flag = conn.execute(
        "SELECT has_received_arbitration FROM players WHERE player_id=?",
        (player_id,)
    ).fetchone()
    has_arb = arb_flag[0] if arb_flag and arb_flag[0] is not None else None

    if perpetual_arb:
        remaining = max(1, 38 - age)
        return remaining, [None] * remaining, 0

    if salary <= min_sal:
        if age >= 30 or (age >= 28 and svc >= 3) or svc >= 6:
            return None, None, None  # type: ignore[return-value]
        svc_years = int(svc)
        remaining = max(1, 6 - svc_years)
        pre_arb_left = max(0, 3 - svc_years)
        return remaining, [None] * remaining, pre_arb_left

    if age >= 30:
        return None, None, None  # type: ignore[return-value]

    est_svc = max(math.ceil(svc), 4 if salary > ARB_DEEP_SALARY_THRESHOLD else 3)
    remaining = max(1, 6 - est_svc)
    return remaining, [None] * remaining, 0
