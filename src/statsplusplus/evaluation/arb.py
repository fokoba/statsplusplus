"""Arbitration salary projection.

Pure computation of arbitration salary estimates from player metrics.
No DB access in the core calculation functions.

Public API:
    arb_salary(ovr, bucket, arb_year, prior_salary, min_sal) -> int
    arb_salary_perpetual(age, projected_war, dpw, min_sal, ...) -> int
"""

from __future__ import annotations

import math
from dataclasses import dataclass
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
    SERVICE_DAYS_PER_YEAR,
    FREE_AGENCY_SERVICE_YEARS,
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


# ---------------------------------------------------------------------------
# Service time (single source of truth)
# ---------------------------------------------------------------------------
#
# Real MLB / OOTP rules (see docs/ootp/financial_model.md):
#   - A full service YEAR = 172 days on the active MLB roster (or MLB IL).
#   - Service is expressed as `years.days`, e.g. 4.070 = 4 full years + 70 days.
#     Only COMPLETED full years reduce team control / advance toward free agency.
#   - Arbitration eligibility: 3 completed years (Super Two aside).
#   - Free agency: 6 completed years.
#
# StatsPlus API field semantics (confirmed against live league data):
#   - `mlb_service_days`  = CUMULATIVE total days of MLB service (NOT a 0-171
#     remainder — an 18-year vet carries ~3183 days).
#   - `mlb_service_years` = completed full years = floor(days / 172); redundant
#     with days, kept by the API for convenience.
#
# Every consumer (arb detection, control estimation, FA classification, display)
# must go through `service_time()` so the interpretation lives in exactly one
# place.


@dataclass(frozen=True)
class ServiceTime:
    """MLB service time derived from cumulative service days.

    Attributes:
        total_days: Cumulative days of MLB service.
        years: Fractional service years (total_days / 172). Use for
            thresholds like "arb-eligible" (>= 3) and "free agent" (>= 6).
        completed_years: Whole completed years (floor). This is what reduces
            remaining team control — a partial year does not count until it
            reaches 172 days.
        remainder_days: Days into the current, not-yet-completed year (0-171),
            for `years.days` display.
        exact: True if derived from the API service fields; False if the caller
            should fall back to the games-based heuristic (no MLB service data).
    """

    total_days: int
    years: float
    completed_years: int
    remainder_days: int
    exact: bool

    @property
    def is_free_agent_eligible(self) -> bool:
        """True if service alone qualifies the player for free agency.

        NOTE: This is a service-time gate only. In OOTP a player becomes an
        actual free agent when he has >= 6 completed years AND his contract has
        expired (the game shows "free agent after contract expires"). A 6+ year
        player on a multi-year deal is still controlled by that contract. Callers
        must therefore gate this on contract state (e.g. only when the contract
        is in its final year) — it is not "is a free agent right now".
        """
        return self.completed_years >= FREE_AGENCY_SERVICE_YEARS

    def display(self) -> str:
        """`years.days` notation, e.g. '4.070'."""
        return f"{self.completed_years}.{self.remainder_days:03d}"


def _coerce_days(raw: Any) -> Optional[int]:
    """Normalize a raw service-days DB value to int days, or None if absent.

    The column is stored inconsistently (integer for MLB players, empty text
    for players who never reached MLB). Centralize the coercion so no caller
    trips over `'' / 172.0` or a text divide.
    """
    if raw is None:
        return None
    if isinstance(raw, int):
        return raw
    s = str(raw).strip()
    if not s:
        return None
    try:
        return int(float(s))
    except ValueError:
        return None


def service_time(conn: Any, player_id: int) -> ServiceTime:
    """Resolve a player's MLB service time — the single source of truth.

    Reads cumulative `mlb_service_days` from the players table and derives every
    representation callers need (fractional years, completed years, display
    days). Falls back to the games-based heuristic only when the API provides no
    MLB service data.

    Args:
        conn: SQLite connection.
        player_id: Player ID.

    Returns:
        A ``ServiceTime``. ``exact`` is False when the value came from the
        games-based fallback.
    """
    row = conn.execute(
        "SELECT mlb_service_days FROM players WHERE player_id=?",
        (player_id,),
    ).fetchone()
    days = _coerce_days(row[0]) if row else None

    if days is not None:
        completed = days // SERVICE_DAYS_PER_YEAR
        return ServiceTime(
            total_days=days,
            years=days / SERVICE_DAYS_PER_YEAR,
            completed_years=completed,
            remainder_days=days - completed * SERVICE_DAYS_PER_YEAR,
            exact=True,
        )

    # No MLB service data — estimate fractional years from games played.
    est = _estimate_service_time_from_games(conn, player_id)
    completed = int(est)
    total = int(round(est * SERVICE_DAYS_PER_YEAR))
    return ServiceTime(
        total_days=total,
        years=est,
        completed_years=completed,
        remainder_days=max(0, total - completed * SERVICE_DAYS_PER_YEAR),
        exact=False,
    )


def estimate_service_time(conn: Any, player_id: int) -> float:
    """Fractional MLB service years. Thin wrapper over ``service_time``.

    Retained for callers that only need the fractional-year scalar.
    """
    return service_time(conn, player_id).years


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
    svc = service_time(conn, player_id)
    svc_years = svc.completed_years  # only completed years reduce control

    # NOTE: Super Two is NOT modeled. Arb eligibility uses a flat 3 completed
    # years. In real MLB/OOTP the top ~17% of players with 2.xxx service become
    # arb-eligible a year early (a 4th arb year). Detecting it requires ranking
    # the whole 2.xxx-service cohort against a league-configurable cutoff each
    # offseason (it can't be read from one player's fields — `has_received_
    # arbitration` only flips *after* the first arb, too late to catch the
    # player whose upcoming offseason is his Super Two year). Consequence: a
    # minority of 2.xxx players get their first arb year projected one season
    # late (payroll slightly understated / surplus slightly overstated for
    # them). Tracked in docs/task_list.md. Low impact — revisit if it matters.

    if perpetual_arb:
        remaining = max(1, 38 - age)
        return remaining, [None] * remaining, 0

    if salary <= min_sal:
        if age >= 30 or (age >= 28 and svc_years >= 3) or svc_years >= FREE_AGENCY_SERVICE_YEARS:
            return None, None, None  # type: ignore[return-value]
        remaining = max(1, FREE_AGENCY_SERVICE_YEARS - svc_years)
        pre_arb_left = max(0, 3 - svc_years)
        return remaining, [None] * remaining, pre_arb_left

    if age >= 30:
        return None, None, None  # type: ignore[return-value]

    # Only completed years count toward the 6-year FA threshold. A player at
    # 4 years 70 days (completed_years=4) has 2 years of control left, not 1.
    # The salary-based floors guard against under-reported service on veterans
    # (a well-paid player has clearly been in the league a while).
    est_svc = max(svc_years, 4 if salary > ARB_DEEP_SALARY_THRESHOLD else 3)
    remaining = max(1, FREE_AGENCY_SERVICE_YEARS - est_svc)
    return remaining, [None] * remaining, 0
