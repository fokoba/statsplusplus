"""FV (Future Value) grade calculation.

Pure computation of prospect FV grades and risk labels from composite/ceiling
scores. No DB access, no global state.

Public API:
    calc_fv(ovr, pot, age, bucket, norm_age, ...) -> tuple[int, str]
    dev_weight(age, norm_age, level, is_pitcher) -> float
    age_development_mult(age, is_pitcher) -> float
"""

from __future__ import annotations

from typing import Any, Optional

from statsplusplus.evaluation.constants import RP_POT_DISCOUNT


# ---------------------------------------------------------------------------
# Development curve defaults (overridable via model_weights)
# ---------------------------------------------------------------------------

# Age-based development runway multiplier.
# Derived from cross-sectional OVR/POT gap analysis. Normalized to age 21 = 1.0.
AGE_RUNWAY_HITTER_DEFAULT: dict[int, float] = {
    17: 1.60, 18: 1.43, 19: 1.26, 20: 1.16, 21: 1.00,
    22: 0.73, 23: 0.55, 24: 0.43, 25: 0.33, 26: 0.19,
}
AGE_RUNWAY_PITCHER_DEFAULT: dict[int, float] = {
    17: 1.56, 18: 1.42, 19: 1.30, 20: 1.16, 21: 1.00,
    22: 0.79, 23: 0.62, 24: 0.48, 25: 0.35, 26: 0.20,
}

# Gap closure rates by age.
GAP_CLOSURE_HITTER_DEFAULT: dict[int, float] = {
    17: 0.87, 18: 0.86, 19: 0.84, 20: 0.83, 21: 0.80,
    22: 0.68, 23: 0.48, 24: 0.38, 25: 0.38,
}
GAP_CLOSURE_PITCHER_DEFAULT: dict[int, float] = {
    17: 0.93, 18: 0.92, 19: 0.91, 20: 0.90, 21: 0.87,
    22: 0.83, 23: 0.72, 24: 0.59, 25: 0.49,
}

# Expected gap by age.
EXPECTED_GAP_HITTER_DEFAULT: dict[int, float] = {
    17: 20, 18: 17, 19: 13, 20: 12, 21: 10, 22: 6, 23: 4, 24: 3, 25: 3,
}
EXPECTED_GAP_PITCHER_DEFAULT: dict[int, float] = {
    17: 18, 18: 15, 19: 13, 20: 11, 21: 9, 22: 7, 23: 5, 24: 4, 25: 3,
}

# Bust discount targets by age (target_product / closure).
_TARGET_PRODUCT: dict[int, float] = {
    17: 0.47, 18: 0.47, 19: 0.46, 20: 0.48, 21: 0.52,
    22: 0.50, 23: 0.53, 24: 0.30, 25: 0.30,
}


# ---------------------------------------------------------------------------
# Development weight
# ---------------------------------------------------------------------------

def age_development_mult(
    age: int | float,
    is_pitcher: bool = False,
    runway_table: Optional[dict[int, float]] = None,
) -> float:
    """Multiplier on dev_weight reflecting remaining development runway.

    Returns 1.0 for age ≤ 21, decays based on empirical gap-closure rates.
    Linearly interpolates between defined age points.

    Args:
        age: Player's current age.
        is_pitcher: Pitchers retain more runway at each age.
        runway_table: Override table (from calibrated model_weights).
    """
    if runway_table is None:
        runway_table = AGE_RUNWAY_PITCHER_DEFAULT if is_pitcher else AGE_RUNWAY_HITTER_DEFAULT

    if age <= 17:
        return runway_table.get(17, 1.58)
    if age >= 26:
        return runway_table.get(26, 0.20)
    lo = int(age)
    hi = lo + 1
    frac = age - lo
    lo_val = runway_table.get(lo, 0.20)
    hi_val = runway_table.get(hi, 0.20)
    return lo_val * (1 - frac) + hi_val * frac


def dev_weight(
    age: int,
    norm_age: int,
    level: Optional[str] = None,
    is_pitcher: bool = False,
    runway_table: Optional[dict[int, float]] = None,
) -> float:
    """Development weight: how much to blend ceiling vs current based on age vs level norm.

    Args:
        age: Player's current age.
        norm_age: Expected age at this level for on-track prospect.
        level: Level string (e.g., "usl", "dsl", "intl").
        is_pitcher: Whether this is a pitcher.
        runway_table: Override age runway table.

    Returns:
        Float weight in [0, 1], higher = more weight on ceiling.
    """
    diff = norm_age - age
    if diff >= 3:
        w = 0.55 if age <= 17 else 0.65
    elif diff >= 2:
        w = 0.45 if age <= 17 else 0.60
    elif diff >= 1:
        w = 0.40 if age <= 17 else 0.50
    elif diff >= -1:
        w = 0.35
    elif diff >= -2:
        w = 0.20
    else:
        w = 0.10

    low_level = level and level.lower().replace(" ", "-") in (
        "usl", "dsl", "intl", "rookie", "a-short"
    )
    if low_level:
        w += 0.10
        if level and level.lower().replace(" ", "-") in ("usl", "dsl", "intl", "rookie"):
            w = min(w, 0.55)

    # Apply empirical age decay for prospects past peak development age
    if age > 21:
        w *= age_development_mult(age, is_pitcher=is_pitcher, runway_table=runway_table)

    return w


# ---------------------------------------------------------------------------
# FV grade calculation
# ---------------------------------------------------------------------------

def _interp_table(table: dict[int, float], age: int | float) -> float:
    """Interpolate from an age-keyed table."""
    if age <= 17:
        return table.get(17, table[min(table.keys())])
    if age >= 25:
        return table.get(25, table[max(table.keys())])
    lo = int(age)
    hi = lo + 1
    frac = age - lo
    lo_val = table.get(lo, 0.38)
    hi_val = table.get(hi, 0.38)
    return lo_val * (1 - frac) + hi_val * frac


def calc_fv(
    ovr: int,
    pot: int,
    age: int,
    bucket: str,
    norm_age: int,
    is_pitcher: bool = False,
    accuracy: str = "N",
    contact_l: int = 0,
    contact_r: int = 0,
    stuff_l: int = 0,
    stuff_r: int = 0,
    offensive_ceiling: Optional[int] = None,
    stat_risk_modifier: float = 0.0,
    work_ethic: str = "N",
    intelligence: str = "N",
    gap_closure_table: Optional[dict[int, float]] = None,
    expected_gap_table: Optional[dict[int, float]] = None,
) -> tuple[int, str, float]:
    """Compute FV grade and risk label for a prospect.

    WAR-based FV with risk label. FV reflects ceiling quality relative to
    MLB position median. Risk captures development probability.

    Args:
        ovr: Current composite score (20-80).
        pot: True ceiling score (20-80).
        age: Player's current age.
        bucket: Positional bucket ("SS", "SP", etc.).
        norm_age: Expected age at current level.
        is_pitcher: Whether this is a pitcher.
        accuracy: Scouting accuracy code ("L" = low).
        contact_l/contact_r: Split contact ratings (for platoon penalty).
        stuff_l/stuff_r: Split stuff ratings (for platoon penalty).
        offensive_ceiling: Offensive ceiling (caps FV for bat-limited players).
        stat_risk_modifier: MiLB stat-based risk adjustment.
        work_ethic: Work ethic code.
        intelligence: Intelligence code.
        gap_closure_table: Override gap closure rates (from model_weights).
        expected_gap_table: Override expected gap table (from model_weights).

    Returns:
        Tuple of (fv_grade: int, risk: str, fv_continuous: float).
        fv_grade is rounded to nearest 5 (20-80).
        risk is "Low", "Medium", "High", or "Extreme".
        fv_continuous is the pre-rounding value for surplus interpolation.
    """
    # Apply RP pot discount
    effective_pot = pot
    if bucket == "RP":
        effective_pot = round(pot * RP_POT_DISCOUNT)

    gap = max(0, effective_pot - ovr)

    # Compute expected peak composite
    if gap <= 3:
        fv = float(ovr)
    else:
        closure_tbl = gap_closure_table or (
            GAP_CLOSURE_PITCHER_DEFAULT if is_pitcher else GAP_CLOSURE_HITTER_DEFAULT
        )
        closure = _interp_table(closure_tbl, age)

        # Bust discount: derived from target product / closure
        age_key = max(17, min(25, int(age)))
        target = _TARGET_PRODUCT.get(age_key, 0.47)
        bust = min(0.85, target / closure) if closure > 0 else 0.55

        peak = ovr + gap * closure * bust
        ceil_weight = max(0.0, min(0.5, (effective_pot - 50) / 30.0))
        fv = peak * (1.0 - ceil_weight) + effective_pot * ceil_weight

    # Accuracy penalty
    if accuracy == "L":
        fv -= 5

    # Platoon split penalty
    if is_pitcher:
        if stuff_l and stuff_r:
            g = abs(stuff_l - stuff_r)
            weak = min(stuff_l, stuff_r)
            if weak <= 25 and g >= 10:
                fv -= 5
    else:
        if contact_l and contact_r:
            g = abs(contact_l - contact_r)
            weak = min(contact_l, contact_r)
            if weak <= 25 and g >= 10:
                fv -= 5

    # RP cap
    if bucket == "RP":
        fv = min(fv, 55)

    # Ceiling cap: FV cannot exceed true_ceiling - 3
    fv = min(fv, effective_pot - 3)

    # Offensive ceiling cap for bat-limited hitters
    if offensive_ceiling is not None and offensive_ceiling < 45 and bucket not in ("SP", "RP"):
        fv = min(fv, 50)

    fv = max(20.0, fv)

    # Snap to nearest 5
    if gap <= 3:
        fv_grade = round(fv / 5) * 5
    else:
        fv_grade = int(fv / 5) * 5

    # Continuous FV for surplus interpolation
    fv_continuous = fv

    # --- Risk Label ---
    gap = max(0, effective_pot - ovr)
    closure_tbl = gap_closure_table or (
        GAP_CLOSURE_PITCHER_DEFAULT if is_pitcher else GAP_CLOSURE_HITTER_DEFAULT
    )
    closure = _interp_table(closure_tbl, age)

    if age <= 19:
        base_discount = 0.30
    elif age <= 21:
        base_discount = 0.35
    elif age <= 23:
        base_discount = 0.45
    else:
        base_discount = 0.60

    eg_table = expected_gap_table or (
        EXPECTED_GAP_PITCHER_DEFAULT if is_pitcher else EXPECTED_GAP_HITTER_DEFAULT
    )
    expected_gap = eg_table.get(max(17, min(25, int(age))), 5)
    excess_gap = max(0, gap - expected_gap)
    if excess_gap >= 15:
        gap_scale = 0.70
    elif excess_gap >= 8:
        gap_scale = 0.85
    else:
        gap_scale = 1.00

    char_adj = 0.0
    if work_ethic in ("H", "VH"):
        char_adj += 0.03
    elif work_ethic == "L":
        char_adj -= 0.03
    if intelligence in ("H", "VH"):
        char_adj += 0.02
    elif intelligence == "L":
        char_adj -= 0.02

    dev_confidence = closure * base_discount * gap_scale + char_adj
    dev_confidence = max(0.0, min(1.0, dev_confidence))

    # MiLB stat-based risk modifier
    if stat_risk_modifier:
        dev_confidence = max(0.0, min(1.0, dev_confidence + stat_risk_modifier))

    if gap < 3:
        risk = "Low"
    elif dev_confidence >= 0.40:
        risk = "Low"
    elif dev_confidence >= 0.25:
        risk = "Medium"
    elif dev_confidence >= 0.15:
        risk = "High"
    else:
        risk = "Extreme"

    return fv_grade, risk, fv_continuous
