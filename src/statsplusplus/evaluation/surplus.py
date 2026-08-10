"""Prospect and contract surplus computation.

Pure functions for computing dollar surplus value. No DB access.

Public API:
    prospect_surplus(fv, age, level, bucket, ...) -> dict
    peak_war_from_fv(fv, bucket, weights) -> float
    scarcity_multiplier(ceiling, bucket, def_rating) -> float
    age_adjusted_discount(level, age, ovr) -> float
    market_value(war, dpw, min_sal) -> float
"""

from __future__ import annotations

from typing import Any, Optional

from statsplusplus.evaluation.arb import arb_salary, arb_salary_perpetual
from statsplusplus.evaluation.constants import (
    FV_TO_PEAK_WAR_DEFAULT,
    FV_TO_PEAK_WAR_RP_DEFAULT,
    PROSPECT_DISCOUNT_RATE,
    PROSPECT_WAR_RAMP,
    SCARCITY_MULT_DEFAULT,
    ModelWeights,
)
from statsplusplus.evaluation.war import aging_mult, peak_war_from_score
from statsplusplus.utils.positions import (
    DEVELOPMENT_DISCOUNT,
    LEVEL_NORM_AGE,
    YEARS_TO_MLB,
    POSITIONAL_WAR_ADJUSTMENTS,
)


# ---------------------------------------------------------------------------
# FV → peak WAR interpolation
# ---------------------------------------------------------------------------

def _interp_dict(tbl: dict[int, float], value: float) -> float:
    """Interpolate from a sorted {key: value} dict."""
    pts = sorted(tbl.keys())
    if value >= pts[-1]:
        return tbl[pts[-1]]
    if value <= pts[0]:
        return tbl[pts[0]]
    for i in range(len(pts) - 1):
        if pts[i] <= value <= pts[i + 1]:
            t = (value - pts[i]) / (pts[i + 1] - pts[i])
            return tbl[pts[i]] + t * (tbl[pts[i + 1]] - tbl[pts[i]])
    return tbl[pts[0]]


def peak_war_from_fv(
    fv: float,
    bucket: str,
    weights: Optional[ModelWeights] = None,
) -> float:
    """Map FV grade to expected peak WAR/year.

    Uses position-specific tables when calibrated (SP, RP, per-hitter-bucket).

    Args:
        fv: FV grade (continuous, supports interpolation).
        bucket: Positional bucket.
        weights: Calibrated model weights (for per-position tables).

    Returns:
        Expected peak WAR per season at this FV.
    """
    if weights is not None:
        if bucket == "RP":
            return _interp_dict(weights.fv_to_peak_war_rp, fv)
        elif bucket == "SP":
            return _interp_dict(weights.fv_to_peak_war_sp, fv)
        else:
            by_pos = weights.fv_to_peak_war_by_pos
            if by_pos and bucket in by_pos:
                return _interp_dict(by_pos[bucket], fv)
            return _interp_dict(weights.fv_to_peak_war, fv)

    # Defaults
    if bucket == "RP":
        return _interp_dict(FV_TO_PEAK_WAR_RP_DEFAULT, fv)
    return _interp_dict(FV_TO_PEAK_WAR_DEFAULT, fv)


# ---------------------------------------------------------------------------
# Discount and scarcity
# ---------------------------------------------------------------------------

_LEVEL_ALIAS: dict[str, str] = {
    "aaa": "AAA", "aa": "AA", "a": "A", "a-short": "A-Short",
    "usl": "USL", "dsl": "DSL", "intl": "Intl", "mlb": "MLB",
    "draft": "DSL", "rookie": "USL",
}

_LEVEL_AGE_DISCOUNT_RATE: float = 0.04


def age_adjusted_discount(
    level: str,
    age: int,
    ovr: Optional[int] = None,
) -> float:
    """Development discount adjusted for age vs level norm.

    +4% per year younger than norm, -4% per year older. Clamped [0.15, 0.95].
    For amateur/draft prospects, uses composite score to estimate effective level.

    Args:
        level: Current level string (e.g., "AAA", "AA", "Draft").
        age: Player's current age.
        ovr: Composite score (for estimating effective level of amateurs).

    Returns:
        Float discount in [0.15, 0.95].
    """
    effective_level = level
    if level.lower() in ("draft", "dsl", "intl", "college", "hs") and ovr is not None:
        if ovr >= 50:
            effective_level = "AAA"
        elif ovr >= 42:
            effective_level = "AA"
        elif ovr >= 35:
            effective_level = "A"
        else:
            effective_level = "A-Short"

    lookup_level = _LEVEL_ALIAS.get(effective_level.lower(), effective_level)
    base = DEVELOPMENT_DISCOUNT.get(lookup_level.lower(), 0.45)
    norm_key = effective_level.lower().replace(" ", "-")
    if norm_key == "rookie":
        norm_key = "usl"
    norm_age = LEVEL_NORM_AGE.get(norm_key)
    if norm_age is None:
        return base
    return max(0.15, min(0.95, base + (norm_age - age) * _LEVEL_AGE_DISCOUNT_RATE))


def scarcity_multiplier(
    ceiling: float,
    bucket: Optional[str] = None,
    def_rating: Optional[int] = None,
    scarcity_table: Optional[dict[int, float]] = None,
) -> float:
    """Interpolate scarcity multiplier from ceiling score.

    Position-adjusted: premium positions get a ceiling shift. For defense-
    dependent positions, the shift scales with defensive ability.

    Args:
        ceiling: Ceiling score (or pot) for lookup.
        bucket: Position bucket for positional adjustment.
        def_rating: Defensive rating for defense-dependent adjustment.
        scarcity_table: Override table (from model_weights).

    Returns:
        Multiplier in [0.0, 1.0].
    """
    _BASE_SHIFT: dict[str, int] = {
        "SS": 4, "CF": 2, "SP": 2, "C": 1, "2B": 1, "3B": 1,
        "COF": -2, "RP": -2, "1B": -3,
    }
    _DEF_SCALED: set[str] = {"CF", "SS", "C", "2B", "3B"}

    shift = _BASE_SHIFT.get(bucket or "", 0)
    if bucket and bucket in _DEF_SCALED:
        dr = def_rating or 0
        scale = max(0.0, min(1.0, (dr - 50) / 20.0)) if dr >= 50 else 0.0
        shift = int(shift * scale)

    effective_ceiling = ceiling + shift
    table = scarcity_table or SCARCITY_MULT_DEFAULT
    pts = sorted(table.keys())

    if effective_ceiling <= pts[0]:
        return table[pts[0]]
    if effective_ceiling >= pts[-1]:
        return table[pts[-1]]
    for i in range(len(pts) - 1):
        if pts[i] <= effective_ceiling <= pts[i + 1]:
            t = (effective_ceiling - pts[i]) / (pts[i + 1] - pts[i])
            return table[pts[i]] + t * (table[pts[i + 1]] - table[pts[i]])
    return 1.0


def certainty_multiplier(ovr: int, pot: int) -> float:
    """Adjust surplus based on how much ceiling is already realized.

    Realization ~1.0 = neutral. Realization ~0.3 = -8% to -15%.
    Capped at 1.0 to avoid double-counting.
    """
    if not ovr or not pot or pot <= 0:
        return 1.0
    realization = ovr / pot
    return max(0.85, min(1.0, 0.8 + 0.4 * realization))


def market_value(war: float, dpw: int, min_sal: int) -> float:
    """Smooth market value: linear ramp from min_sal at 0 WAR to war×dpw at 1.0 WAR.

    Above 1.0 WAR uses standard war×dpw. Below 0 returns min_sal.
    Eliminates the cliff at replacement level.
    """
    if war <= 0:
        return float(min_sal)
    if war >= 1.0:
        return war * dpw
    return min_sal + war * (dpw - min_sal)


# ---------------------------------------------------------------------------
# Main prospect surplus calculation
# ---------------------------------------------------------------------------

def prospect_surplus(
    fv: float,
    age: int,
    level: str,
    bucket: str,
    dpw: int,
    min_sal: int,
    ovr: Optional[int] = None,
    pot: Optional[int] = None,
    def_rating: Optional[int] = None,
    positional_adjust: bool = False,
    perpetual_arb: bool = False,
    perp_model: Optional[dict[str, Any]] = None,
    weights: Optional[ModelWeights] = None,
    scarcity_table: Optional[dict[int, float]] = None,
) -> dict[str, Any]:
    """Compute surplus value for a prospect over their 6-year control period.

    Args:
        fv: Continuous FV grade (supports fractional for interpolation).
        age: Player's current age.
        level: Current level string (e.g., "AAA", "AA").
        bucket: Positional bucket.
        dpw: Dollars per WAR.
        min_sal: League minimum salary.
        ovr: Current composite score (optional, for realization blend).
        pot: Ceiling score (optional, for scarcity).
        def_rating: Defensive rating (optional, for scarcity adjustment).
        positional_adjust: Whether to add positional WAR adjustment.
        perpetual_arb: Whether this is a perpetual arb league.
        perp_model: Perpetual arb model parameters.
        weights: Calibrated model weights.
        scarcity_table: Override scarcity table.

    Returns:
        Dict with total_surplus, breakdown, and component multipliers.
    """
    # Determine years to MLB
    effective_level = level
    if level.lower() in ("draft", "dsl", "intl", "college", "hs") and ovr is not None:
        if ovr >= 50:
            effective_level = "AAA"
        elif ovr >= 42:
            effective_level = "AA"
        elif ovr >= 35:
            effective_level = "A"
        else:
            effective_level = "A-Short"

    ytm_key = _LEVEL_ALIAS.get(effective_level.lower(), effective_level)
    years_out = YEARS_TO_MLB.get(ytm_key.lower(), 3.5)
    debut_age = age + years_out

    pw = peak_war_from_fv(fv, bucket, weights)
    pos_adj = POSITIONAL_WAR_ADJUSTMENTS.get(bucket, 0.0) if positional_adjust else 0.0

    # Near-maxed prospect blend
    if ovr and pot and pot > 0:
        realization = ovr / pot
        ovr_war = peak_war_from_score(ovr, bucket, weights)
        if realization > 0.7 and ovr_war < pw:
            blend_w = max(0.0, (realization - 0.7) / 0.3) ** 2
            pw = pw * (1 - blend_w) + ovr_war * blend_w

    rows: list[dict[str, Any]] = []
    total_surplus = 0.0
    dev_discount = age_adjusted_discount(level, age, ovr=ovr)

    for yr in range(6):
        ctrl_year = yr + 1
        player_age = debut_age + yr
        discount = (1 - PROSPECT_DISCOUNT_RATE) ** (years_out + yr)
        ramp = PROSPECT_WAR_RAMP.get(ctrl_year, 1.0)

        war = (pw + pos_adj) * aging_mult(player_age, bucket) * ramp
        mkt_val = market_value(war, dpw, min_sal) * discount

        # Salary projection
        if perpetual_arb:
            cum_war = sum(r["war"] for r in rows) + war
            salary = arb_salary_perpetual(
                int(player_age), war, dpw, min_sal,
                career_war=cum_war, model=perp_model,
            )
        elif ctrl_year <= 3:
            salary = min_sal
        else:
            arb_yr = ctrl_year - 3
            arb_ovr = max(40, min(75, int((pw + pos_adj) / 0.19 + 50)))
            if arb_yr == 1:
                salary = arb_salary(arb_ovr, bucket, 1, min_sal, min_sal)
            else:
                prior_sal = rows[-1]["salary"]
                salary = arb_salary(arb_ovr, bucket, arb_yr, prior_sal, min_sal)

        surplus = mkt_val - salary * discount
        total_surplus += surplus

        rows.append({
            "control_year": ctrl_year,
            "player_age": round(player_age, 1),
            "war": round(war, 2),
            "market_value": round(mkt_val),
            "salary": round(salary),
            "surplus": round(surplus),
        })

    cert_mult = certainty_multiplier(ovr or 0, pot or 0)
    scar_mult = scarcity_multiplier(
        float(pot) if pot else fv, bucket=bucket,
        def_rating=def_rating, scarcity_table=scarcity_table,
    )
    combined = dev_discount * cert_mult * scar_mult
    base_surplus = max(0, round(total_surplus * combined))

    for r in rows:
        r["surplus"] = round(r["surplus"] * combined)

    return {
        "fv": fv, "bucket": bucket, "level": level, "age": age,
        "years_to_mlb": years_out, "debut_age": round(debut_age, 1),
        "dev_discount": dev_discount, "certainty_mult": cert_mult,
        "scarcity_mult": scar_mult,
        "total_surplus": base_surplus, "breakdown": rows,
    }


def calc_pap(
    war: float | None,
    salary: int | float,
    team_games: int | None,
    dpw: int | float,
    pap_scale: float | None = None,
) -> float | None:
    """Compute Payroll-Adjusted Performance score (1-10 scale).

    PAP measures how much surplus value a player is producing relative to
    their salary. 5.0 = break-even, >5 = surplus, <5 = overpaid.

    Args:
        war: Player's WAR (None → returns None).
        salary: Player's salary in dollars.
        team_games: Games played by the team (for annualization).
        dpw: Dollars per WAR for this league.
        pap_scale: Scaling factor for the tanh curve. Defaults to
            dpw scaled by the same ratio as the original fixed $25M/$9M
            (roughly 2.78x dpw) — a vintage/low-salary league's dpw can be
            in the tens of thousands, and a fixed $25M scale swamps every
            real surplus figure to ~0, tanh(~0) ~= 0, collapsing PAP to
            5.0 for every player regardless of actual performance.

    Returns:
        PAP score rounded to 2 decimal places, or None if inputs invalid.
    """
    from math import tanh

    if war is None or team_games is None or team_games < 5:
        return None
    if pap_scale is None:
        pap_scale = (dpw or 9_000_000) * (25_000_000 / 9_000_000)
    annualized = war * (162 / team_games)
    surplus = annualized * dpw - salary
    return round(5 + 5 * tanh(surplus / pap_scale), 2)
