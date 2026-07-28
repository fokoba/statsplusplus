"""
arb_model.py — MLB arbitration and service time estimation.

Estimates player service time and remaining team control years from game
appearance data. Used by contract_value.py and team_queries.py.

Public API:
  arb_salary(ovr, bucket, arb_year, prior_salary, min_sal)  → int
  arb_salary_perpetual(age, projected_war, dpw, min_sal, model=None) → int
  estimate_service_time(conn, player_id)                    → float
  estimate_control(conn, player_id, age, salary, bucket)    → (ctrl_years, salaries, pre_arb_left)
"""

import math
from constants import (
    SERVICE_GAMES_HITTER, SERVICE_STARTS_SP, SERVICE_GAMES_RP,
    ARB_DEEP_SALARY_THRESHOLD,
    ARB_HITTER_BASE, ARB_HITTER_EXP, ARB_RP_BASE, ARB_RP_EXP,
    ARB_RAISE_INTERCEPT, ARB_RAISE_SLOPE, ARB_RAISE_MIN,
)

# Default perpetual arb model parameters.
# Calibrated from PPL cross-sectional data (1-year contracts, WAR >= 1.0).
# Replaced by model_weights.json ARB_SALARY_MODEL when available.
#
# Model: salary = min(growth, ceiling), floor at min_sal
#   growth  = min_sal + k × max(0, career_WAR - discount)^exp
#   ceiling = ceiling_pct × current_WAR × $/WAR
_DEFAULT_PERP_K = 2400              # $ per unit of effective career WAR^exp
_DEFAULT_PERP_EXP = 0.72            # diminishing returns on career WAR accumulation
_DEFAULT_PERP_DISCOUNT = 7.0        # career WAR threshold before salary moves ("proving it")
_DEFAULT_PERP_CEILING_PCT = 0.35    # max salary as fraction of current market value


def arb_salary_perpetual(age, projected_war, dpw, min_sal, career_war=0, model=None):
    """Project salary for a perpetual arb league.

    In perpetual arb, salary is recalculated yearly based on accumulated
    track record (career WAR) with a ceiling tied to current production.
    There is no pre-arb period and salaries can decrease if production declines.

    Model: salary = min(growth, ceiling), floored at league minimum.
      growth  = min_sal + k × max(0, career_WAR - discount)^exp
      ceiling = ceiling_pct × current_WAR × $/WAR

    The "discount" represents the career WAR threshold before salary moves
    significantly (the "proving it" effect — one good year barely moves the
    needle). The ceiling caps salary at a fraction of market value and
    naturally ties salary to current production (drops when WAR drops).

    Args:
        age: Player age for the projected year.
        projected_war: WAR projection for that year.
        dpw: League dollars-per-WAR.
        min_sal: League minimum salary.
        career_war: Cumulative career WAR entering this year.
        model: Optional calibrated model dict from model_weights.json
               with keys: k, exp, discount, ceiling_pct.

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

    # Growth formula: salary ramps with accumulated career production
    effective_war = max(0, career_war - discount)
    if effective_war > 0:
        growth_sal = min_sal + k * (effective_war ** exp)
    else:
        growth_sal = min_sal

    # Ceiling: salary can't exceed ceiling_pct of current market value
    ceiling_sal = ceiling_pct * projected_war * dpw if projected_war > 0 else min_sal

    # Final: minimum of growth and ceiling, floored at league minimum
    salary = max(min_sal, min(growth_sal, ceiling_sal))
    return int(round(salary))


def arb_salary(ovr, bucket, arb_year, prior_salary, min_sal):
    """Project arb salary for a given arb year (1-indexed).

    The first argument (ovr) accepts either OVR or composite_score — both are
    on the 20-80 scale and the exponential formula works identically with either.

    Uses RP-specific exponential model for RPs (calibrated from 35 RP arb contracts).
    Uses hitter/SP exponential base + annual raise model for all other positions.

    Scales output by league salary level (min_sal / default_min) so the model
    works for leagues with different financial scales.

    arb_year: 1 = first arb year, 2 = second, 3 = third
    prior_salary: previous year's salary (used for raise calculation)
    min_sal: league minimum salary
    """
    from constants import DEFAULT_MINIMUM_SALARY
    # Only scale for leagues with drastically different salary environments
    # (e.g. historical leagues at $6K vs modern $825K). Don't scale for
    # minor differences between modern leagues.
    ratio = min_sal / DEFAULT_MINIMUM_SALARY if min_sal and DEFAULT_MINIMUM_SALARY else 1.0
    scale = ratio if ratio < 0.5 else 1.0
    if bucket == "RP":
        rp_base = ARB_RP_BASE * math.exp(ARB_RP_EXP * ovr)
        return round(rp_base * (0.75 + 0.25 * (arb_year - 1)) * scale)
    if arb_year == 1:
        return round(ARB_HITTER_BASE * math.exp(ARB_HITTER_EXP * ovr) * scale)
    raise_amt = max(ARB_RAISE_MIN, round(ARB_RAISE_INTERCEPT + ARB_RAISE_SLOPE * ovr))
    return prior_salary + round(raise_amt * scale)


def estimate_service_time(conn, player_id):
    """Get MLB service time as fractional years.

    Prefers exact values from the players table (mlb_service_days).
    The API's mlb_service_days is cumulative total days of MLB service.
    One full service year = 172 days. So 6 years = 1032 days.

    Falls back to games-based estimation when exact data is not available.
    """
    # Try exact service time from expanded /players API fields
    row = conn.execute(
        "SELECT mlb_service_years, mlb_service_days FROM players WHERE player_id=?",
        (player_id,)
    ).fetchone()
    if row and row[0] is not None:
        # mlb_service_days is total cumulative days (not days beyond years)
        # mlb_service_years = floor(mlb_service_days / 172)
        # Use days directly for precision
        days = row[1] or 0
        return days / 172.0

    # Fallback: estimate from games played
    return _estimate_service_time_from_games(conn, player_id)


def _estimate_service_time_from_games(conn, player_id):
    """Estimate fractional MLB service years from games played (legacy fallback).

    Uses role-adjusted denominators per year:
      Hitters: g / SERVICE_GAMES_HITTER
      SP (gs >= g/2): gs / SERVICE_STARTS_SP
      RP (gs < g/2): g / SERVICE_GAMES_RP
    Caps each year at 1.0, sums across all years.
    """
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
    """Estimate remaining team control years and salary schedule for 1yr contracts.

    Returns (ctrl_years, salaries, pre_arb_left) or (None, None, None) for FA deals.

    Pre-arb: salary == league_min. Uses games-based service time.
    Arb: salary > league_min, age < 30. Rounds up service time (games undercount roster days).
    FA deal: age >= 30 and salary > league_min → return None.

    In perpetual_arb leagues (no free agency), all players remain under team
    control indefinitely. Returns control years until age 38.
    """
    from player_utils import league_minimum
    from league_config import config as _cfg
    min_sal = league_minimum()
    svc = estimate_service_time(conn, player_id)

    # Check has_received_arbitration flag for more precise arb detection
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
