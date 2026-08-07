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
    DEFAULT_MINIMUM_SALARY,
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
