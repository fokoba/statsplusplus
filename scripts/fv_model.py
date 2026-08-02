"""
fv_model.py — Prospect FV grade calculation.

Computes FV grades from player ratings dicts. Used by fv_calc.py and farm_analysis.py.
All functions are pure (no DB access).

Public API:
  calc_fv(p)            → (fv_base: int, fv_plus: bool)
  dev_weight(age, norm_age, level) → float
  age_development_mult(age) → float
  defensive_score(p, bucket) → float
"""

from constants import RP_POT_DISCOUNT, _load_weights
from ratings import norm, norm_floor


def _dev_curve(key, default):
    """Load a league-calibrated development curve, falling back to default."""
    w = _load_weights()
    if not w or key not in w:
        return default
    raw = w[key]
    if isinstance(raw, dict):
        return {int(k): v for k, v in raw.items()}
    return raw

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Positional access premium parameters — premium positions where adequate
# defense enables a positional value premium that scales with offensive grade.
POSITIONAL_ACCESS = {
    "SS": {"access_threshold": 50, "base_premium": 2.0, "offense_scale": 0.06},
    "C":  {"access_threshold": 50, "base_premium": 1.5, "offense_scale": 0.05},
    "CF": {"access_threshold": 50, "base_premium": 1.5, "offense_scale": 0.05},
}

# Positional defensive weights — position-specific importance of each tool.
DEFENSIVE_WEIGHTS = {
    "C":      {"CFrm": 0.45, "CBlk": 0.35, "CArm": 0.20},
    "SS":     {"IFR": 0.40, "IFE": 0.20, "IFA": 0.20, "TDP": 0.20},
    "2B":     {"IFR": 0.35, "TDP": 0.30, "IFE": 0.20, "IFA": 0.15},
    "3B":     {"IFA": 0.35, "IFE": 0.30, "IFR": 0.25, "TDP": 0.10},
    "CF":     {"OFR": 0.55, "OFE": 0.25, "OFA": 0.20},
    "COF_LF": {"OFR": 0.50, "OFE": 0.30, "OFA": 0.20},
    "COF_RF": {"OFR": 0.40, "OFA": 0.35, "OFE": 0.25},
}

# Level norm ages — expected age at each level for an on-track prospect.
LEVEL_NORM_AGE = {
    "draft": 18, "aaa": 26, "aa": 24, "a": 22, "a-short": 21,
    "usl": 19, "dsl": 18, "intl": 17,
}

# ---------------------------------------------------------------------------
# FV helpers
# ---------------------------------------------------------------------------

# Age-based development runway multiplier.
# Derived from cross-sectional OVR/POT gap analysis: at each age, what
# fraction of the ceiling-current gap remains unrealized? Normalized to
# age 21 = 1.0 (the inflection where level-based context gives way to
# age-based decay). Source: VMLB + EMLB 2033 average, N=50+ per bucket.
# Separate tables for hitters and pitchers — pitchers develop later
# (stuff/movement can improve into mid-20s, control peaks even later).
_AGE_RUNWAY_HITTER = _dev_curve("age_runway_hitter", {
    17: 1.60, 18: 1.43, 19: 1.26, 20: 1.16, 21: 1.00,
    22: 0.73, 23: 0.55, 24: 0.43, 25: 0.33, 26: 0.19,
})
_AGE_RUNWAY_PITCHER = _dev_curve("age_runway_pitcher", {
    17: 1.56, 18: 1.42, 19: 1.30, 20: 1.16, 21: 1.00,
    22: 0.79, 23: 0.62, 24: 0.48, 25: 0.35, 26: 0.20,
})
# Combined table kept for backward compatibility / external callers
_AGE_RUNWAY = {
    17: 1.58, 18: 1.42, 19: 1.28, 20: 1.16, 21: 1.00,
    22: 0.76, 23: 0.58, 24: 0.46, 25: 0.34, 26: 0.20,
}


def age_development_mult(age, is_pitcher=False):
    """Multiplier on dev_weight reflecting remaining development runway.

    Returns 1.0 for age ≤ 21 (full runway), decays based on empirical
    gap-closure rates for older prospects. Linearly interpolates between
    defined age points. Pitchers retain more runway at each age.
    """
    table = _AGE_RUNWAY_PITCHER if is_pitcher else _AGE_RUNWAY_HITTER
    if age <= 21:
        return table.get(age, 1.58)
    if age >= 26:
        return table[26]
    lo = int(age)
    hi = lo + 1
    frac = age - lo
    return table.get(lo, 0.20) * (1 - frac) + table.get(hi, 0.20) * frac


def dev_weight(age, norm_age, level=None, is_pitcher=False):
    """Development weight: how much to blend Pot vs Ovr based on age vs level norm."""
    diff = norm_age - age
    if diff >= 3:    w = 0.55 if age <= 17 else 0.65
    elif diff >= 2:  w = 0.45 if age <= 17 else 0.60
    elif diff >= 1:  w = 0.40 if age <= 17 else 0.50
    elif diff >= -1: w = 0.35
    elif diff >= -2: w = 0.20
    else:            w = 0.10
    low_level = level and level.lower().replace(" ", "-") in ("usl", "dsl", "intl", "rookie", "a-short")
    if low_level:
        w += 0.10
        if level.lower().replace(" ", "-") in ("usl", "dsl", "intl", "rookie"):
            w = min(w, 0.55)
    # Apply empirical age decay for prospects past peak development age
    if age > 21:
        w *= age_development_mult(age, is_pitcher=is_pitcher)
    return w


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


def _pos_composite(p, bucket, age):
    """Normalized positional composite for defensive bonus (uses Pot grades for age <= 23)."""
    if bucket == "COF":
        return norm(max(p.get("LF", 0), p.get("RF", 0)))
    pot_map = {"C": "PotC", "SS": "PotSS", "CF": "PotCF", "2B": "Pot2B", "3B": "Pot3B"}
    cur_map = {"C": "C", "SS": "SS", "CF": "CF", "2B": "2B", "3B": "3B"}
    field = pot_map.get(bucket) if age <= 23 else cur_map.get(bucket)
    if not field:
        return 0
    return norm(p.get(field, 0))


# ---------------------------------------------------------------------------
# Positional access premium
# ---------------------------------------------------------------------------

def positional_access_premium(bucket, offensive_grade, defensive_value, access_threshold=50):
    """Compute the positional value premium for premium positions.

    At premium positions (SS, C, CF), adequate defense (>= threshold) enables
    a positional value premium that scales with offensive grade. Higher offense
    at a premium position with adequate defense produces a larger premium.

    For non-premium positions, returns 0.

    Args:
        bucket: Position bucket.
        offensive_grade: The player's offensive grade (20-80).
        defensive_value: The player's defensive value (20-80).
        access_threshold: Minimum defensive value to qualify for positional access.

    Returns:
        Premium value as a float (added to FV before rounding).
    """
    params = POSITIONAL_ACCESS.get(bucket)
    if params is None:
        return 0.0
    if defensive_value < access_threshold:
        return 0.0
    base_premium = params["base_premium"]
    offense_scale = params["offense_scale"]
    return base_premium + (offensive_grade - 40) * offense_scale


# ---------------------------------------------------------------------------
# FV calculation
# ---------------------------------------------------------------------------

def calc_fv(p):
    """
    Compute FV for a prospect. Player dict must have:
      Ovr, Pot, Age, _is_pitcher, _bucket, _norm_age, _mlb_median
    Returns (fv_grade: int, risk: str).

    Delegates to calc_fv_v2: ceiling-credit FV with risk label.
    FV reflects ceiling quality relative to MLB position median.
    Risk captures development probability (Low/Medium/High/Extreme).
    """
    return calc_fv_v2(p)


# ---------------------------------------------------------------------------
# Probability-based FV (v2)
# ---------------------------------------------------------------------------

# Forward-looking gap closure rates by age.
# "What fraction of the remaining composite-to-ceiling gap closes between
# age X and peak (26)?" Derived from cross-sectional mean realization at
# each age vs age 26 terminal. Source: VMLB 2033, N=200+ per age bucket.
_GAP_CLOSURE_HITTER = _dev_curve("gap_closure_hitter", {
    17: 0.87, 18: 0.86, 19: 0.84, 20: 0.83, 21: 0.80,
    22: 0.68, 23: 0.48, 24: 0.38, 25: 0.38,
})
_GAP_CLOSURE_PITCHER = _dev_curve("gap_closure_pitcher", {
    17: 0.93, 18: 0.92, 19: 0.91, 20: 0.90, 21: 0.87,
    22: 0.83, 23: 0.72, 24: 0.59, 25: 0.49,
})

_EXPECTED_GAP_HITTER = _dev_curve("expected_gap_hitter", {
    17: 20, 18: 17, 19: 13, 20: 12, 21: 10, 22: 6, 23: 4, 24: 3, 25: 3,
})
_EXPECTED_GAP_PITCHER = _dev_curve("expected_gap_pitcher", {
    17: 18, 18: 15, 19: 13, 20: 11, 21: 9, 22: 7, 23: 5, 24: 4, 25: 3,
})

# Terminal (age 26) outcome distribution — what the final realization
# looks like at peak. Used to model variance around the expected closure.
# Buckets: (bust <75%, below 75-88%, meets 88-95%, exceeds 95-100%, full 100%)
_TERMINAL_DIST_HITTER = (0.01, 0.13, 0.24, 0.32, 0.29)
_TERMINAL_DIST_PITCHER = (0.00, 0.05, 0.14, 0.19, 0.62)

# Realization fractions for each terminal outcome bucket (midpoints).
_TERMINAL_REALIZATION = (0.65, 0.82, 0.92, 0.97, 1.00)

# Empirical outcome probabilities by age, from cross-sectional OVR/POT
# realization analysis (VMLB 2033). Each row: (bust, below, meets, exceeds, full).
# Bust: <60% realization. Below: 60-75%. Meets: 75-90%.
# Exceeds: 90-97%. Full: 97%+.
_OUTCOME_PROBS_HITTER = {
    17: (0.52, 0.45, 0.03, 0.00, 0.00),
    18: (0.46, 0.48, 0.06, 0.00, 0.00),
    19: (0.25, 0.57, 0.17, 0.00, 0.00),
    20: (0.25, 0.56, 0.19, 0.00, 0.00),
    21: (0.13, 0.43, 0.38, 0.06, 0.00),
    22: (0.03, 0.20, 0.50, 0.21, 0.06),
    23: (0.00, 0.07, 0.39, 0.33, 0.21),
    24: (0.00, 0.05, 0.28, 0.35, 0.31),
    25: (0.00, 0.07, 0.28, 0.29, 0.36),
}
_OUTCOME_PROBS_PITCHER = {
    17: (0.51, 0.45, 0.04, 0.00, 0.00),
    18: (0.31, 0.59, 0.10, 0.00, 0.00),
    19: (0.19, 0.61, 0.19, 0.01, 0.00),
    20: (0.07, 0.54, 0.35, 0.04, 0.00),
    21: (0.03, 0.37, 0.50, 0.08, 0.03),
    22: (0.00, 0.15, 0.61, 0.15, 0.08),
    23: (0.00, 0.06, 0.41, 0.23, 0.29),
    24: (0.00, 0.03, 0.24, 0.33, 0.39),
    25: (0.00, 0.02, 0.20, 0.31, 0.47),
}

# Realization fractions for each outcome bucket.
# These represent what fraction of the TRUE CEILING the player achieves
# as their peak OVR, not fraction of the gap. Derived from empirical
# OVR/POT ratios: bust median ~40% of POT, below ~68%, etc.
# Applied as: score_i = ceiling × realization_i, floored at 20.
_OUTCOME_REALIZATION = (0.40, 0.68, 0.83, 0.94, 1.00)


def _get_outcome_probs(age, is_pitcher):
    """Get outcome probabilities for a given age, with interpolation."""
    table = _OUTCOME_PROBS_PITCHER if is_pitcher else _OUTCOME_PROBS_HITTER
    if age <= 17:
        return table[17]
    if age >= 25:
        return table[25]
    lo = int(age)
    hi = lo + 1
    frac = age - lo
    lo_p = table.get(lo, table[25])
    hi_p = table.get(hi, table[25])
    return tuple(lo_p[i] * (1 - frac) + hi_p[i] * frac for i in range(5))


def calc_fv_v2(p):
    """WAR-based FV with risk label.

    FV = ceiling WAR mapped to FV tiers via league-calibrated thresholds.
    Risk captures development probability (Low/Medium/High/Extreme).

    Player dict must include:
      Ovr (composite), Pot (true_ceiling), Age, _is_pitcher, _bucket,
      _norm_age, _ceil_war (ceiling WAR from COMPOSITE_TO_WAR),
      _fv_thresholds (list of (war, fv) tuples, descending)

    Returns (fv_grade: int, risk: str).
    """
    ovr = p["Ovr"] or 0   # composite_score
    pot = p["Pot"] or 0   # true_ceiling
    age = p["Age"]
    bucket = p["_bucket"]
    is_pitcher = bool(p.get("_is_pitcher"))

    # FV = expected peak composite with ceiling blend.
    if bucket == "RP":
        pot = round(pot * RP_POT_DISCOUNT)
    gap = max(0, pot - ovr)
    if gap <= 3:
        fv = ovr
    else:
        closure_table = _GAP_CLOSURE_PITCHER if is_pitcher else _GAP_CLOSURE_HITTER
        if age <= 17: closure = closure_table[17]
        elif age >= 25: closure = closure_table[25]
        else:
            lo = int(age); hi = lo + 1; frac = age - lo
            closure = closure_table.get(lo, 0.85) * (1 - frac) + closure_table.get(hi, 0.85) * frac

        # Bust discount: target_product / closure, so leagues with higher
        # closure rates (more survivorship bias) get proportionally lower
        # bust credit. Target products derived from VMLB (well-calibrated).
        _TARGET_PRODUCT = {
            17: 0.47, 18: 0.47, 19: 0.46, 20: 0.48, 21: 0.52,
            22: 0.50, 23: 0.53, 24: 0.30, 25: 0.30,
        }
        age_key = max(17, min(25, int(age)))
        bust = min(0.85, _TARGET_PRODUCT[age_key] / closure) if closure > 0 else 0.55

        peak = ovr + gap * closure * bust
        ceil_weight = max(0.0, min(0.5, (pot - 50) / 30.0))
        fv = peak * (1.0 - ceil_weight) + pot * ceil_weight

    # Accuracy penalty
    if p.get("Acc") == "L":
        fv -= 5  # drop one tier

    # Platoon split penalty
    if is_pitcher:
        sl, sr = norm_floor(p.get("Stf_L", 0)), norm_floor(p.get("Stf_R", 0))
        if sl and sr:
            g, weak = abs(sl - sr), min(sl, sr)
            if weak <= 25 and g >= 15: fv -= 5
            elif weak <= 25 and g >= 10: fv -= 5
    else:
        cl, cr = norm_floor(p.get("Cntct_L", 0)), norm_floor(p.get("Cntct_R", 0))
        if cl and cr:
            g, weak = abs(cl - cr), min(cl, cr)
            if weak <= 25 and g >= 15: fv -= 5
            elif weak <= 25 and g >= 10: fv -= 5

    # RP cap
    if bucket == "RP":
        fv = min(fv, 55)

    # Ceiling cap: FV cannot exceed true_ceiling - 3. A player whose
    # best-case outcome is average (ceiling=50) should not grade as FV 50
    # (future average regular) — they're a 45 (future backup/platoon).
    fv = min(fv, pot - 3)

    # Offensive ceiling cap: if the bat can never be MLB-average (off_ceil < 45),
    # cap FV at 50 regardless of defense/speed. A player who projects as a
    # defensive replacement should not grade as a future regular.
    off_ceil = p.get("_offensive_ceiling")
    if off_ceil is not None and off_ceil < 45 and bucket not in ("SP", "RP"):
        fv = min(fv, 50)

    fv = max(20, fv)
    # Snap to nearest 5: developing prospects use floor rounding (must
    # actually project to 50 to get FV 50), maxed players use standard
    # rounding (they've already proven their level).
    if gap <= 3:
        fv_grade = round(fv / 5) * 5
    else:
        fv_grade = int(fv / 5) * 5

    # Expose continuous FV for surplus interpolation (avoids 5-point steps)
    p["_fv_continuous"] = fv

    # -- Risk Label --
    gap = max(0, pot - ovr)
    norm_age = p["_norm_age"]

    table = _GAP_CLOSURE_PITCHER if is_pitcher else _GAP_CLOSURE_HITTER
    if age <= 17:
        closure = table[17]
    elif age >= 25:
        closure = table[25]
    else:
        lo = int(age)
        hi = lo + 1
        frac = age - lo
        closure = table.get(lo, 0.38) * (1 - frac) + table.get(hi, 0.38) * frac

    if age <= 19:
        base_discount = 0.30
    elif age <= 21:
        base_discount = 0.35
    elif age <= 23:
        base_discount = 0.45
    else:
        base_discount = 0.60

    eg_table = _EXPECTED_GAP_PITCHER if is_pitcher else _EXPECTED_GAP_HITTER
    expected_gap = eg_table.get(max(17, min(25, age)), 5)
    excess_gap = max(0, gap - expected_gap)
    if excess_gap >= 15:
        gap_scale = 0.70
    elif excess_gap >= 8:
        gap_scale = 0.85
    else:
        gap_scale = 1.00

    we = p.get("WrkEthic", "N")
    intel = p.get("Int", "N")
    char_adj = 0.0
    if we in ("H", "VH"): char_adj += 0.03
    elif we == "L": char_adj -= 0.03
    if intel in ("H", "VH"): char_adj += 0.02
    elif intel == "L": char_adj -= 0.02

    dev_confidence = closure * base_discount * gap_scale + char_adj
    dev_confidence = max(0.0, min(1.0, dev_confidence))

    # MiLB stat-based risk modifier (when stat context is provided)
    _stat_risk_adj = p.get("_stat_risk_modifier", 0.0)
    if _stat_risk_adj:
        dev_confidence = max(0.0, min(1.0, dev_confidence + _stat_risk_adj))

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

    return fv_grade, risk


# ---------------------------------------------------------------------------
# Performance-Adjusted Ceiling (PAC)
# ---------------------------------------------------------------------------

def compute_performance_adjusted_ceiling(
    true_ceiling: int,
    stat_2080: float,
    player_age: int,
    norm_age: int,
    effective_pa: float,
    tool_only_score: int,
) -> int:
    """Compute performance-adjusted ceiling from MiLB stat signal.

    Adjusts the scouting-based ceiling up or down based on how the player
    is performing relative to expectations at their level, weighted by
    age-for-level context and sample size.

    Args:
        true_ceiling: Raw scouting ceiling (20-80 scale).
        stat_2080: Player's level-relative production on 20-80 scale
            (from MiLB OPS+ or inverted ERA-).
        player_age: Player's current age.
        norm_age: Normal age for the player's level (e.g., 24 for AAA).
        effective_pa: PA × level_discount — confidence in the stat signal.
        tool_only_score: Pure tool composite (no stat blend).

    Returns:
        Adjusted ceiling (clamped to 20-80).
    """
    if effective_pa < 30 or true_ceiling <= 0:
        return true_ceiling

    # Signal: how far above/below average the player is producing
    # stat_2080 of 50 = level average, 60 = plus, 40 = below average
    # Normalize relative to tool_only_score: are stats confirming or contradicting tools?
    signal = (stat_2080 - tool_only_score) / 30.0  # normalized to ~[-1, +1] range
    signal = max(-1.0, min(1.0, signal))

    # Age-for-level context
    age_context = norm_age - player_age  # positive = young for level

    # Asymmetric age multipliers
    if signal > 0:
        # Positive signal: amplify for young players, dampen for old
        if age_context > 0:
            age_mult = 1.0 + age_context * 0.15  # +15% per year young
        else:
            age_mult = max(0.4, 1.0 + age_context * 0.20)  # -20% per year old (AAAA dampening)
    else:
        # Negative signal: dampen for young players, amplify for old
        if age_context > 0:
            age_mult = max(0.25, 1.0 - age_context * 0.25)  # -25% per year young (they're developing)
        else:
            age_mult = 1.0 + abs(age_context) * 0.15  # +15% per year old

    # Sample confidence: ramps from 0 at 30 eff PA to 1.0 at 300 eff PA
    sample_confidence = min(1.0, (effective_pa - 30) / 270.0)

    # Maximum ceiling adjustment: ±6 points
    max_adjustment = 6.0
    adjustment = signal * age_mult * sample_confidence * max_adjustment
    adjustment = max(-max_adjustment, min(max_adjustment, adjustment))

    result = true_ceiling + round(adjustment)
    return max(20, min(80, result))


def compute_stat_risk_modifier(
    stat_2080: float,
    player_age: int,
    norm_age: int,
    effective_pa: float,
    tool_only_score: int,
) -> float:
    """Compute a risk modifier based on MiLB stat performance.

    Returns an additive adjustment to dev_confidence (the risk calculation's
    core variable). Positive = more confident (reduces risk), negative = less
    confident (increases risk).

    Magnitude is small (±0.05 to ±0.12) — enough to shift one risk band in
    strong cases, but not enough to override the fundamentals.

    Args:
        stat_2080: Player's level-relative production on 20-80 scale.
        player_age: Player's current age.
        norm_age: Normal age for the player's level.
        effective_pa: PA × level_discount.
        tool_only_score: Pure tool composite.

    Returns:
        Float adjustment to dev_confidence (typically -0.12 to +0.12).
    """
    if effective_pa < 50:
        return 0.0

    # Performance relative to tools
    perf_delta = stat_2080 - tool_only_score  # positive = outperforming tools

    # Normalize: +15 above tools = strong confirming signal
    normalized = perf_delta / 15.0
    normalized = max(-1.0, min(1.0, normalized))

    # Age context
    age_context = norm_age - player_age  # positive = young

    # Only strong signals with sufficient sample should modify risk
    sample_factor = min(1.0, (effective_pa - 50) / 200.0)

    # Base modifier: ±0.08 at full signal + full sample
    base_mod = normalized * 0.08 * sample_factor

    # Age amplification for confirming signals
    if normalized > 0 and age_context > 0:
        # Young + outperforming = strong confidence boost
        base_mod *= (1.0 + age_context * 0.25)
    elif normalized < 0 and age_context < 0:
        # Old + underperforming = strong confidence reduction
        base_mod *= (1.0 + abs(age_context) * 0.20)
    elif normalized < 0 and age_context > 0:
        # Young + underperforming = dampened (still developing)
        base_mod *= max(0.2, 1.0 - age_context * 0.30)

    return max(-0.12, min(0.12, base_mod))
