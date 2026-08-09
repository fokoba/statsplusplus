"""Unified player evaluation — single surplus model for all players.

Replaces the dual-model approach (prospect_surplus + contract_value) with a
single function that smoothly transitions from tool-based projection to stat-based
evidence as MLB track record accumulates.

Design reference: docs/unified_evaluation_design.md
Implementation plan: docs/unified_evaluation_implementation.md

Public API:
    stat_confidence(career_pa, career_ip) -> float
    unified_surplus(fv_continuous, bucket, age, level, ...) -> dict
"""

from __future__ import annotations

from typing import Any, Optional

from statsplusplus.evaluation.arb import arb_salary, arb_salary_perpetual
from statsplusplus.evaluation.constants import (
    PROSPECT_DISCOUNT_RATE,
    ModelWeights,
)
from statsplusplus.evaluation.surplus import (
    age_adjusted_discount,
    certainty_multiplier,
    market_value,
    peak_war_from_fv,
    scarcity_multiplier,
)
from statsplusplus.evaluation.war import aging_mult, peak_war_from_score
from statsplusplus.utils.positions import (
    POSITIONAL_WAR_ADJUSTMENTS,
    YEARS_TO_MLB,
)


# ---------------------------------------------------------------------------
# Stat confidence
# ---------------------------------------------------------------------------

# Thresholds for full confidence (stat projection fully trusted)
_PA_FULL_CONFIDENCE: float = 400.0
_IP_FULL_CONFIDENCE: float = 120.0

# Minimum PA/IP to register any stat signal at all
_PA_MIN_SIGNAL: int = 50
_IP_MIN_SIGNAL: float = 15.0


def stat_confidence(career_pa: int, career_ip: float) -> float:
    """Compute confidence in MLB stat-based projection.

    Returns a value between 0.0 (pure tool-based projection) and 1.0
    (pure stat-based projection) based on accumulated MLB playing time.

    Uses the larger of the PA-based and IP-based confidence (for two-way
    players or players who have both, the stronger signal wins).

    The ramp is linear above the minimum signal threshold. Below the minimum,
    confidence is 0.0 — tiny samples are noise, not signal.

    Args:
        career_pa: Total career MLB plate appearances.
        career_ip: Total career MLB innings pitched.

    Returns:
        Float in [0.0, 1.0].
    """
    # PA-based confidence (hitters, or pitchers who also bat)
    if career_pa >= _PA_MIN_SIGNAL:
        pa_conf = min(1.0, career_pa / _PA_FULL_CONFIDENCE)
    else:
        pa_conf = 0.0

    # IP-based confidence (pitchers)
    if career_ip >= _IP_MIN_SIGNAL:
        ip_conf = min(1.0, career_ip / _IP_FULL_CONFIDENCE)
    else:
        ip_conf = 0.0

    return max(pa_conf, ip_conf)


# ---------------------------------------------------------------------------
# WAR ramp for pre-peak players in the unified model
# ---------------------------------------------------------------------------

# Year-by-year ramp for players still developing. Represents the fraction of
# peak WAR they're expected to produce in each year of team control.
# For established players (stat_confidence ~1.0), the aging curve alone applies.
# For prospects, this ramp models the typical development arc from debut.
_UNIFIED_WAR_RAMP: dict[int, float] = {
    1: 0.60,  # Debut year — partial, adjusting
    2: 0.80,  # Second year — establishing
    3: 0.90,  # Third year — approaching peak
    4: 1.00,  # Peak
    5: 1.00,  # Peak
    6: 1.00,  # Peak (aging curve handles decline)
}


# ---------------------------------------------------------------------------
# Level aliases for years-to-MLB lookup
# ---------------------------------------------------------------------------

_LEVEL_ALIAS: dict[str, str] = {
    "aaa": "AAA", "aa": "AA", "a": "A", "a-short": "A-Short",
    "usl": "USL", "dsl": "DSL", "intl": "Intl", "mlb": "MLB",
    "draft": "DSL", "rookie": "USL", "college": "DSL", "hs": "DSL",
}


# ---------------------------------------------------------------------------
# Unified surplus calculation
# ---------------------------------------------------------------------------

def unified_surplus(
    # Tool-based inputs
    fv_continuous: float,
    bucket: str,
    age: int,
    level: str,
    composite: int,
    ceiling: int,
    # Stat-based inputs
    career_pa: int = 0,
    career_ip: float = 0.0,
    stat_war: Optional[float] = None,
    # Control/contract inputs
    years_control: int = 6,
    salaries: Optional[list[int]] = None,
    pre_arb_years: int = 3,
    # League context
    dpw: int = 7_000_000,
    min_sal: int = 840_000,
    perpetual_arb: bool = False,
    perp_model: Optional[dict[str, Any]] = None,
    weights: Optional[ModelWeights] = None,
    # Evaluation modifiers
    def_rating: Optional[int] = None,
    scarcity_table: Optional[dict[int, float]] = None,
) -> dict[str, Any]:
    """Compute unified surplus for any player.

    Single entry point that handles pure prospects, crossover players, and
    established MLB veterans through a smooth stat_confidence gradient.

    Salary projection:
        If `salaries` is provided, uses those directly (for players on known contracts).
        If `salaries` is None, estimates salary schedule: pre-arb years at min_sal,
        then arb escalation based on projected WAR.

    Args:
        fv_continuous: Continuous FV grade (pre-rounding). Drives tool-based WAR.
        bucket: Positional bucket ("SS", "SP", "RP", etc.).
        age: Player's current age.
        level: Current level string ("MLB", "AAA", "AA", etc.).
        composite: Current composite score (20-80).
        ceiling: True ceiling score (20-80).
        career_pa: Career MLB plate appearances.
        career_ip: Career MLB innings pitched.
        stat_war: Peak WAR from stat history (None if no qualifying MLB stats).
        years_control: Estimated remaining years of team control.
        salaries: Known salary schedule (len >= years_control). If None, estimated.
        pre_arb_years: Years of pre-arb control remaining (for salary estimation).
        dpw: Dollars per WAR for this league.
        min_sal: League minimum salary.
        perpetual_arb: Whether this is a perpetual arb league.
        perp_model: Perpetual arb model parameters.
        weights: Calibrated model weights.
        def_rating: Defensive potential rating (for scarcity adjustment).
        scarcity_table: Override scarcity table.

    Returns:
        Dict with stat_confidence, tool_war, stat_war, peak_war, surplus,
        surplus_yr1, breakdown, dev_discount, certainty_mult, scarcity_mult,
        years_control, years_to_mlb.
    """
    sc = stat_confidence(career_pa, career_ip)

    # --- Step 1: Tool-based WAR projection ---
    # For prospects (low stat_confidence), FV-based projection is correct —
    # FV encodes development probability and ceiling quality.
    # For established players (high stat_confidence), composite-based projection
    # is more appropriate — it reflects current proven ability.
    # Blend the two tool projections by stat_confidence.
    fv_war = peak_war_from_fv(fv_continuous, bucket, weights)
    composite_war = peak_war_from_score(composite, bucket, weights)
    tool_war = _lerp(fv_war, composite_war, sc)

    # --- Step 2: Blend WAR projections ---
    if stat_war is not None and sc > 0.0:
        peak_war = (1.0 - sc) * tool_war + sc * stat_war
    else:
        peak_war = tool_war

    # --- Step 3: Determine years-to-MLB (for prospects not yet at MLB) ---
    if level.upper() == "MLB":
        years_out = 0.0
    else:
        lookup = _LEVEL_ALIAS.get(level.lower(), level)
        years_out = YEARS_TO_MLB.get(lookup.lower(), 3.0)

    # --- Step 4: Compute evaluation discounts (fading with stat_confidence) ---
    raw_dev_discount = age_adjusted_discount(level, age, ovr=composite)
    raw_cert_mult = certainty_multiplier(composite, ceiling)
    scar_mult = scarcity_multiplier(
        float(ceiling), bucket=bucket,
        def_rating=def_rating, scarcity_table=scarcity_table,
    )

    # Fade prospect-style discounts as stat evidence accumulates
    effective_dev_discount = _lerp(raw_dev_discount, 1.0, sc)
    effective_cert_mult = _lerp(raw_cert_mult, 1.0, sc)

    # --- Step 5: Project year-by-year WAR and surplus ---
    rows: list[dict[str, Any]] = []
    total_surplus = 0.0
    use_known_salaries = salaries is not None and len(salaries) >= years_control

    for yr in range(years_control):
        ctrl_year = yr + 1
        player_age = age + years_out + yr
        time_discount = (1 - PROSPECT_DISCOUNT_RATE) ** (years_out + yr)

        # WAR projection for this year
        ramp = _UNIFIED_WAR_RAMP.get(ctrl_year, 1.0)
        effective_ramp = _lerp(ramp, 1.0, sc)  # Established players skip the ramp

        war = peak_war * effective_ramp * aging_mult(player_age, bucket)
        war = max(0.0, war)

        # Market value (time-discounted)
        mkt_val = market_value(war, dpw, min_sal) * time_discount

        # Salary estimation
        if use_known_salaries:
            salary = salaries[yr]
        elif perpetual_arb:
            cum_war = sum(r["war"] for r in rows) + war
            salary = arb_salary_perpetual(
                int(player_age), war, dpw, min_sal,
                career_war=cum_war, model=perp_model,
            )
        elif yr < pre_arb_years:
            salary = min_sal
        else:
            # Arb salary estimation from projected WAR
            arb_yr = yr - pre_arb_years + 1
            arb_ovr = max(40, min(75, int(peak_war / 0.19 + 50)))
            if arb_yr == 1:
                salary = arb_salary(arb_ovr, bucket, 1, min_sal, min_sal)
            else:
                prior_sal = rows[-1]["salary"] if rows else min_sal
                salary = arb_salary(arb_ovr, bucket, arb_yr, prior_sal, min_sal)

        surplus = mkt_val - salary * time_discount
        total_surplus += surplus

        rows.append({
            "control_year": ctrl_year,
            "player_age": round(player_age, 1),
            "war": round(war, 2),
            "market_value": round(mkt_val),
            "salary": round(salary),
            "surplus": round(surplus),
        })

    # --- Step 6: Apply evaluation multipliers ---
    combined_mult = effective_dev_discount * effective_cert_mult * scar_mult
    final_surplus = max(0, round(total_surplus * combined_mult))

    # Apply multipliers to per-year breakdown for display
    for r in rows:
        r["surplus"] = round(r["surplus"] * combined_mult)

    surplus_yr1 = rows[0]["surplus"] if rows else 0

    return {
        "stat_confidence": round(sc, 3),
        "tool_war": round(tool_war, 2),
        "stat_war": round(stat_war, 2) if stat_war is not None else None,
        "peak_war": round(peak_war, 2),
        "surplus": final_surplus,
        "surplus_yr1": surplus_yr1,
        "breakdown": rows,
        "dev_discount": round(effective_dev_discount, 3),
        "certainty_mult": round(effective_cert_mult, 3),
        "scarcity_mult": round(scar_mult, 3),
        "years_control": years_control,
        "years_to_mlb": round(years_out, 1),
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _lerp(a: float, b: float, t: float) -> float:
    """Linear interpolation from a to b by factor t (0=a, 1=b)."""
    return a + (b - a) * t
