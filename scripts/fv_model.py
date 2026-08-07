"""
fv_model.py — Prospect FV grade calculation.

MIGRATION NOTE: Pure computation delegates to statsplusplus.evaluation.fv.
The legacy `calc_fv(p)` interface (takes a player dict, mutates it) is
maintained here as a wrapper around the package's explicit-parameter version.

Public API:
  calc_fv(p)            → (fv_base: int, risk: str)
  dev_weight(age, norm_age, level) → float
  age_development_mult(age) → float
  defensive_score(p, bucket) → float
"""

from statsplusplus.evaluation.constants import RP_POT_DISCOUNT
from statsplusplus.evaluation.fv import (
    dev_weight,
    age_development_mult,
    calc_fv as _calc_fv_pure,
    GAP_CLOSURE_HITTER_DEFAULT,
    GAP_CLOSURE_PITCHER_DEFAULT,
    EXPECTED_GAP_HITTER_DEFAULT,
    EXPECTED_GAP_PITCHER_DEFAULT,
    AGE_RUNWAY_HITTER_DEFAULT,
    AGE_RUNWAY_PITCHER_DEFAULT,
)
from ratings import norm, norm_floor


def _dev_curve(key, default):
    """Load a league-calibrated development curve, falling back to default."""
    from constants import _load_weights
    w = _load_weights()
    if not w or key not in w:
        return default
    raw = w[key]
    if isinstance(raw, dict):
        return {int(k): v for k, v in raw.items()}
    return raw


# Development curves (calibrated or default)
_GAP_CLOSURE_HITTER = _dev_curve("gap_closure_hitter", GAP_CLOSURE_HITTER_DEFAULT)
_GAP_CLOSURE_PITCHER = _dev_curve("gap_closure_pitcher", GAP_CLOSURE_PITCHER_DEFAULT)
_EXPECTED_GAP_HITTER = _dev_curve("expected_gap_hitter", EXPECTED_GAP_HITTER_DEFAULT)
_EXPECTED_GAP_PITCHER = _dev_curve("expected_gap_pitcher", EXPECTED_GAP_PITCHER_DEFAULT)

# Positional access premium parameters
POSITIONAL_ACCESS = {
    "SS": {"access_threshold": 50, "base_premium": 2.0, "offense_scale": 0.06},
    "C":  {"access_threshold": 50, "base_premium": 1.5, "offense_scale": 0.05},
    "CF": {"access_threshold": 50, "base_premium": 1.5, "offense_scale": 0.05},
}

# Positional defensive weights
DEFENSIVE_WEIGHTS = {
    "C":      {"CFrm": 0.45, "CBlk": 0.35, "CArm": 0.20},
    "SS":     {"IFR": 0.40, "IFE": 0.20, "IFA": 0.20, "TDP": 0.20},
    "2B":     {"IFR": 0.35, "TDP": 0.30, "IFE": 0.20, "IFA": 0.15},
    "3B":     {"IFA": 0.35, "IFE": 0.30, "IFR": 0.25, "TDP": 0.10},
    "CF":     {"OFR": 0.55, "OFE": 0.25, "OFA": 0.20},
    "COF_LF": {"OFR": 0.50, "OFE": 0.30, "OFA": 0.20},
    "COF_RF": {"OFR": 0.40, "OFA": 0.35, "OFE": 0.25},
}

# Level norm ages
LEVEL_NORM_AGE = {
    "draft": 18, "aaa": 26, "aa": 24, "a": 22, "a-short": 21,
    "usl": 19, "dsl": 18, "intl": 17,
}


def defensive_score(p, bucket):
    """Weighted defensive score on 20-80 scale for a position bucket."""
    def _n(val): return norm(val) or 0
    if bucket == "COF":
        lf = sum(_n(p.get(f, 0) or 0) * w for f, w in DEFENSIVE_WEIGHTS["COF_LF"].items())
        rf = sum(_n(p.get(f, 0) or 0) * w for f, w in DEFENSIVE_WEIGHTS["COF_RF"].items())
        return max(lf, rf)
    weights = DEFENSIVE_WEIGHTS.get(bucket)
    if not weights:
        return 0
    return sum(_n(p.get(f, 0) or 0) * w for f, w in weights.items())


def positional_access_premium(bucket, offensive_grade, defensive_value, access_threshold=50):
    """Compute positional value premium for premium positions."""
    params = POSITIONAL_ACCESS.get(bucket)
    if params is None:
        return 0.0
    if defensive_value < access_threshold:
        return 0.0
    base_premium = params["base_premium"]
    offense_scale = params["offense_scale"]
    return base_premium + (offensive_grade - 40) * offense_scale


def calc_fv(p):
    """Compute FV for a prospect. Legacy interface — takes player dict.

    Returns (fv_grade: int, risk: str).
    Mutates p by setting p["_fv_continuous"].
    """
    ovr = p.get("Ovr") or 0
    pot = p.get("Pot") or 0
    age = p["Age"]
    bucket = p["_bucket"]
    is_pitcher = bool(p.get("_is_pitcher"))

    # Extract optional parameters from the player dict
    accuracy = p.get("Acc", "N")
    contact_l = norm_floor(p.get("Cntct_L", 0))
    contact_r = norm_floor(p.get("Cntct_R", 0))
    stuff_l = norm_floor(p.get("Stf_L", 0))
    stuff_r = norm_floor(p.get("Stf_R", 0))
    offensive_ceiling = p.get("_offensive_ceiling")
    stat_risk_modifier = p.get("_stat_risk_modifier", 0.0)
    work_ethic = p.get("WrkEthic", "N")
    intelligence = p.get("Int", "N")
    norm_age = p.get("_norm_age", 22)

    # Use calibrated gap closure tables
    gap_closure = _GAP_CLOSURE_PITCHER if is_pitcher else _GAP_CLOSURE_HITTER
    expected_gap = _EXPECTED_GAP_PITCHER if is_pitcher else _EXPECTED_GAP_HITTER

    fv_grade, risk, fv_continuous = _calc_fv_pure(
        ovr=ovr, pot=pot, age=age, bucket=bucket, norm_age=norm_age,
        is_pitcher=is_pitcher, accuracy=accuracy,
        contact_l=contact_l, contact_r=contact_r,
        stuff_l=stuff_l, stuff_r=stuff_r,
        offensive_ceiling=offensive_ceiling,
        stat_risk_modifier=stat_risk_modifier,
        work_ethic=work_ethic, intelligence=intelligence,
        gap_closure_table=gap_closure,
        expected_gap_table=expected_gap,
    )

    # Mutate the player dict (legacy behavior required by fv_calc.py)
    p["_fv_continuous"] = fv_continuous

    return fv_grade, risk


def compute_performance_adjusted_ceiling(
    true_ceiling, stat_2080, player_age, norm_age, effective_pa, tool_only_score
):
    """Compute performance-adjusted ceiling from MiLB stat signal."""
    from statsplusplus.evaluation.fv import compute_performance_adjusted_ceiling as _pac
    # Package version doesn't exist yet — import from itself (no-op redirect)
    # This function is defined in the legacy fv_model.py and called by fv_calc.py
    if effective_pa < 30 or true_ceiling <= 0:
        return true_ceiling

    signal = (stat_2080 - tool_only_score) / 30.0
    signal = max(-1.0, min(1.0, signal))
    age_context = norm_age - player_age

    if signal > 0:
        age_mult = 1.0 + age_context * 0.15 if age_context > 0 else max(0.4, 1.0 + age_context * 0.20)
    else:
        age_mult = max(0.25, 1.0 - age_context * 0.25) if age_context > 0 else 1.0 + abs(age_context) * 0.15

    sample_confidence = min(1.0, (effective_pa - 30) / 270.0)
    max_adjustment = 6.0
    adjustment = signal * age_mult * sample_confidence * max_adjustment
    adjustment = max(-max_adjustment, min(max_adjustment, adjustment))

    result = true_ceiling + round(adjustment)
    return max(20, min(80, result))


def compute_stat_risk_modifier(stat_2080, player_age, norm_age, effective_pa, tool_only_score):
    """Compute risk modifier based on MiLB stat performance."""
    if effective_pa < 50:
        return 0.0

    perf_delta = stat_2080 - tool_only_score
    normalized = max(-1.0, min(1.0, perf_delta / 15.0))
    age_context = norm_age - player_age
    sample_factor = min(1.0, (effective_pa - 50) / 200.0)
    base_mod = normalized * 0.08 * sample_factor

    if normalized > 0 and age_context > 0:
        base_mod *= (1.0 + age_context * 0.25)
    elif normalized < 0 and age_context < 0:
        base_mod *= (1.0 + abs(age_context) * 0.20)
    elif normalized < 0 and age_context > 0:
        base_mod *= max(0.2, 1.0 - age_context * 0.30)

    return max(-0.12, min(0.12, base_mod))
