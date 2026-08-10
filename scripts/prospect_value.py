"""
prospect_value.py — Prospect surplus value calculator.
Usage: python3 scripts/prospect_value.py --player <player_id>
       python3 scripts/prospect_value.py --fv <fv> --age <age> --level <level> --bucket <bucket>

Implements Step 3 of docs/trade_analysis_guide.md.
"""

import argparse, json, os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from statsplusplus.config.league_context import get_league_dir, get_active_league_slug
from statsplusplus.config.league_config import LeagueConfig
from statsplusplus.config.league_config import dollars_per_war as _dollars_per_war_pkg
from statsplusplus.config.league_config import league_minimum as _league_minimum_pkg
from statsplusplus.evaluation.war import peak_war_from_score as peak_war_from_ovr, aging_mult
from statsplusplus.evaluation.arb import arb_salary, arb_salary_perpetual as _arb_salary_perp
from statsplusplus.evaluation.constants import (
    load_model_weights,
    PROSPECT_DISCOUNT_RATE,
    NO_TRACK_RECORD_DISCOUNT,
)
from statsplusplus.utils.positions import (
    POSITIONAL_WAR_ADJUSTMENTS,
    LEVEL_NORM_AGE,
    DEVELOPMENT_DISCOUNT,
    YEARS_TO_MLB as _YEARS_TO_MLB_DEFAULT,
)

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Resolve league context
_league_dir = get_league_dir(get_active_league_slug())
_cfg = LeagueConfig(base_dir=_league_dir)
_weights = load_model_weights(_league_dir)

# Calibrated values from model_weights.json
ARB_PCT = _weights.arb_pct
FV_TO_PEAK_WAR = _weights.fv_to_peak_war
FV_TO_PEAK_WAR_SP = _weights.fv_to_peak_war_sp
FV_TO_PEAK_WAR_RP = _weights.fv_to_peak_war_rp
FV_TO_PEAK_WAR_BY_POS = _weights.fv_to_peak_war_by_pos
SCARCITY_MULT = _weights.scarcity_mult
YEARS_TO_MLB = _weights.years_to_mlb
LEVEL_AGE_DISCOUNT_RATE = 0.04
PROSPECT_WAR_RAMP = {1: 0.60, 2: 0.80}

# Local wrappers
def dollars_per_war():
    return _dollars_per_war_pkg(_league_dir)

def league_minimum():
    return _league_minimum_pkg(_league_dir)

# ---------------------------------------------------------------------------
# Core calculation
# ---------------------------------------------------------------------------

def peak_war(fv, bucket="SP"):
    """Map FV to expected peak WAR/year. Interpolate between defined points.
    Half-grade (+) is handled by the caller passing fv+0.5.
    RPs use a separate table calibrated to actual RP WAR output.
    SPs use a separate table when calibrated (pitchers produce less WAR per
    FV grade than hitters in most leagues).
    Hitters use per-position tables when calibrated (COF produces less WAR
    per FV grade than SS).
    """
    if bucket == "RP":
        table = FV_TO_PEAK_WAR_RP
    elif bucket == "SP":
        table = FV_TO_PEAK_WAR_SP
    elif FV_TO_PEAK_WAR_BY_POS and bucket in FV_TO_PEAK_WAR_BY_POS:
        table = FV_TO_PEAK_WAR_BY_POS[bucket]
    else:
        table = FV_TO_PEAK_WAR
    fv_points = sorted(table.keys())
    if fv >= max(fv_points):
        return table[max(fv_points)]
    if fv <= min(fv_points):
        return table[min(fv_points)]
    for i in range(len(fv_points) - 1):
        f0, f1 = fv_points[i], fv_points[i+1]
        if f0 <= fv <= f1:
            t = (fv - f0) / (f1 - f0)
            return table[f0] + t * (table[f1] - table[f0])
    return table[min(fv_points)]

def _age_adjusted_discount(level, age, ovr=None):
    """Development discount adjusted for age vs level norm.
    +4% per year younger than norm, -4% per year older. Clamped [0.15, 0.95].

    For amateur/draft prospects, uses composite score (ovr) to estimate what
    minor league level the player's tools correspond to, rather than using
    the literal 'Draft' label which has no meaningful development baseline.
    """
    # For draft/amateur levels, estimate effective level from present tools
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

    # Normalize level key for lookup (DEVELOPMENT_DISCOUNT uses title case)
    _LEVEL_ALIAS = {
        "aaa": "AAA", "aa": "AA", "a": "A", "a-short": "A-Short",
        "usl": "USL", "dsl": "DSL", "intl": "Intl", "mlb": "MLB",
        "draft": "DSL", "rookie": "USL",
    }
    lookup_level = _LEVEL_ALIAS.get(effective_level.lower(), effective_level)
    base = DEVELOPMENT_DISCOUNT.get(lookup_level, 0.45)
    norm_key = effective_level.lower().replace(" ", "-")
    # "Rookie" level label maps to USL in the norm age table
    if norm_key == "rookie":
        norm_key = "usl"
    norm_age = LEVEL_NORM_AGE.get(norm_key)
    if norm_age is None:
        return base
    return max(0.15, min(0.95, base + (norm_age - age) * LEVEL_AGE_DISCOUNT_RATE))


def _certainty_mult(ovr, pot):
    """Adjust surplus based on how much ceiling is already realized.

    Accepts composite_score/ceiling_score (same formula, different inputs).
    When called with composite_score and ceiling_score, realization measures
    how much of the ceiling is already achieved.

    Realization ~1.0 (maxed out) = neutral (no bonus — proximity already
    rewarded by dev discount and time value).
    Realization ~0.5 = neutral. Realization ~0.3 = -8% to -15% (raw, high variance).
    Capped at 1.0 to avoid double-counting AAA proximity (Session 33).
    """
    if not ovr or not pot or pot <= 0:
        return 1.0
    realization = ovr / pot
    return max(0.85, min(1.0, 0.8 + 0.4 * realization))


def _market_value(war, dpw, lg_min):
    """Smooth market value: linear ramp from lg_min at 0 WAR to war*dpw at 1.0 WAR.
    Above 1.0 WAR uses standard war*dpw. Below 0 WAR returns lg_min.
    Eliminates the cliff at REPLACEMENT_WAR that zeroed out sub-1.0 WAR players."""
    if war <= 0:
        return lg_min
    if war >= 1.0:
        return war * dpw
    return lg_min + war * (dpw - lg_min)


def _scarcity_mult(fv, bucket=None, def_rating=None):
    """Interpolate scarcity multiplier from SCARCITY_MULT table.

    Uses ceiling_score instead of pot for scarcity lookup when composite
    scores are active. The fv parameter receives pot (or ceiling_score)
    from the caller.
    
    Position-adjusted: premium positions get a Pot shift (higher effective Pot
    = scarcer). For defense-dependent positions (CF/SS/C/2B/3B), the shift
    scales with defensive ability — elite defenders get the full bump, poor
    defenders get none.
    """
    _BASE_SHIFT = {
        'SS': 4, 'CF': 2, 'SP': 2, 'C': 1, '2B': 1, '3B': 1,
        'COF': -2, 'RP': -2, '1B': -3,
    }
    _DEF_SCALED = {'CF', 'SS', 'C', '2B', '3B'}

    shift = _BASE_SHIFT.get(bucket, 0)
    if bucket in _DEF_SCALED:
        # Scale by defensive ability: full at 70+, linear 50-70, zero below 50
        dr = def_rating or 0
        scale = max(0.0, min(1.0, (dr - 50) / 20.0)) if dr >= 50 else 0.0
        shift = shift * scale
    fv = fv + shift

    pts = sorted(SCARCITY_MULT.keys())
    if fv <= pts[0]:  return SCARCITY_MULT[pts[0]]
    if fv >= pts[-1]: return SCARCITY_MULT[pts[-1]]
    for i in range(len(pts) - 1):
        if pts[i] <= fv <= pts[i + 1]:
            t = (fv - pts[i]) / (pts[i + 1] - pts[i])
            return SCARCITY_MULT[pts[i]] + t * (SCARCITY_MULT[pts[i + 1]] - SCARCITY_MULT[pts[i]])
    return 1.0


def prospect_surplus(fv, age, level, bucket, positional_adjust=False, fv_plus=False,
                     ovr=None, pot=None, def_rating=None):
    """
    Compute surplus value for a prospect over their 6-year control period.
    fv: integer FV grade (strip '+' before passing; use fv_plus=True for half-grades).

    WAR ramp: players don't immediately produce peak WAR on debut. Early control
    years are discounted to reflect MLB adjustment and development variance:
      Year 1: 60% of peak, Year 2: 80%, Year 3+: 100%
    """
    dpw       = dollars_per_war()
    lg_min    = league_minimum()

    # For draft/amateur levels, estimate years to MLB from present tools
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
    # Normalize case for lookup
    _YTM_ALIAS = {
        "aaa": "AAA", "aa": "AA", "a": "A", "a-short": "A-Short",
        "usl": "USL", "dsl": "DSL", "intl": "Intl", "mlb": "MLB",
    }
    ytm_key = _YTM_ALIAS.get(effective_level.lower(), effective_level)
    years_out = YEARS_TO_MLB.get(ytm_key, 3.5)
    debut_age = age + years_out
    fv_eff    = fv + (0.5 if fv_plus else 0)
    pw        = peak_war(fv_eff, bucket)
    pos_adj   = POSITIONAL_WAR_ADJUSTMENTS.get(bucket, 0.0) if positional_adjust else 0.0

    # For near-maxed prospects (Ovr close to Pot), blend FV-based peak WAR with
    # Ovr-based current WAR. A maxed player's peak IS their current production;
    # the FV table overestimates because it assumes further development.
    # Blend weight = realization^2 (gentle at low realization, strong near max).
    if ovr and pot and pot > 0:
        realization = ovr / pot
        ovr_war = peak_war_from_ovr(ovr, bucket)
        if realization > 0.7 and ovr_war < pw:
            blend_w = max(0, (realization - 0.7) / 0.3) ** 2  # 0 at 0.7, 1 at 1.0
            pw = pw * (1 - blend_w) + ovr_war * blend_w

    RAMP = PROSPECT_WAR_RAMP  # year 3+ = 1.0

    rows = []
    total_surplus = 0.0
    dev_discount  = _age_adjusted_discount(level, age, ovr=ovr)

    # Standard leagues control a player for 6 years (3 pre-arb + 3 arb) before
    # free agency, so a 6-year window is the right horizon. Perpetual-arb
    # leagues like PPL auto-renew every player's contract as a 1-year deal
    # indefinitely (25 years of team control granted per team) — there's no
    # free-agency exit, so truncating at 6 years cuts off a young prospect's
    # entire prime before the projection ever reaches it, while a near-ready
    # prospect's 6-year window happens to land right on his peak years. That
    # mismatch systematically overvalues "close but modest" prospects relative
    # to "far but high-ceiling" ones. Extend the horizon far enough to cover a
    # full career arc into decline; PROSPECT_DISCOUNT_RATE (5%/yr) already
    # tapers the value of distant years, so this doesn't need a hard cutoff.
    try:
        _is_perpetual_horizon = _cfg.perpetual_arb
    except Exception:
        _is_perpetual_horizon = False
    _control_years = 20 if _is_perpetual_horizon else 6

    for yr in range(_control_years):
        ctrl_year  = yr + 1
        player_age = debut_age + yr
        discount   = (1 - PROSPECT_DISCOUNT_RATE) ** (years_out + yr)
        ramp       = RAMP.get(ctrl_year, 1.0)

        war = (pw + pos_adj) * aging_mult(player_age, bucket) * ramp

        market_val = _market_value(war, dpw, lg_min) * discount

        # Arb salary: raise-based model from arb_model.py
        # Arb is a raise system: arb 1 based on quality, arb 2-3 raise from prior
        try:
            _is_perpetual = _cfg.perpetual_arb
        except Exception:
            _is_perpetual = False

        if _is_perpetual:
            # Perpetual arb: no pre-arb period. Salary = f(career_WAR, current_WAR).
            # Accumulate projected career WAR from debut.
            _cum_war = sum(r["war"] for r in rows) + war
            _perp_model = None
            try:
                from statsplusplus.config.league_context import get_league_dir
                import json as _json
                _mw_path = get_league_dir() / "config" / "model_weights.json"
                if _mw_path.exists():
                    _mw = _json.load(open(_mw_path))
                    _perp_model = _mw.get("ARB_SALARY_MODEL")
            except Exception:
                pass
            salary = _arb_salary_perp(player_age, war, dpw, lg_min,
                                      career_war=_cum_war, model=_perp_model)
        elif ctrl_year <= 3:
            salary = lg_min
        else:
            arb_yr = ctrl_year - 3
            # Estimate OVR at peak from peak WAR (invert the WAR→OVR relationship)
            _arb_ovr = max(40, min(75, (pw + pos_adj) / 0.19 + 50))
            if arb_yr == 1:
                salary = arb_salary(_arb_ovr, bucket, 1, lg_min, lg_min)
            else:
                prior_sal = rows[-1]["salary"]
                salary = arb_salary(_arb_ovr, bucket, arb_yr, prior_sal, lg_min)

        surplus = market_val - salary * discount
        total_surplus += surplus

        rows.append({
            "control_year": ctrl_year,
            "player_age":   round(player_age, 1),
            "war":          round(war, 2),
            "market_value": round(market_val),
            "salary":       round(salary),
            "surplus":      round(surplus),
        })

    cert_mult = _certainty_mult(ovr, pot)
    scar_mult = _scarcity_mult(pot if pot else fv_eff, bucket=bucket, def_rating=def_rating)
    combined = dev_discount * cert_mult * scar_mult
    base_surplus = max(0, round(total_surplus * combined))

    # Apply combined multiplier to surplus so breakdown matches total.
    # Market value and salary stay raw (real projected economics).
    # Surplus = trade value after development risk and scarcity adjustment.
    for r in rows:
        r["surplus"] = round(r["surplus"] * combined)

    return {"fv": fv, "bucket": bucket, "level": level, "age": age,
            "years_to_mlb": years_out, "debut_age": round(debut_age, 1),
            "dev_discount": dev_discount, "certainty_mult": cert_mult,
            "scarcity_mult": scar_mult,
            "total_surplus": base_surplus, "breakdown": rows}


_PREMIUM_DEF_POSITIONS = {"SS", "C", "CF"}


def _adjust_scenario_probs(p_base, p_mid, p_ceil, bucket,
                           offensive_grade=None, offensive_ceiling=None,
                           defensive_value=None, durability_score=None):
    """Adjust scenario probabilities based on component profile shape.

    Defense-heavy profiles at premium positions get a floor boost (higher p_base).
    Offense-heavy profiles get a ceiling boost (higher p_ceil).
    Balanced profiles get tighter distributions (higher p_mid).
    Low-durability SP get higher bust risk.
    """
    if not any((offensive_grade, offensive_ceiling, defensive_value, durability_score)):
        return p_base, p_mid, p_ceil

    # Floor boost: premium defense at premium position
    if bucket in _PREMIUM_DEF_POSITIONS and defensive_value and defensive_value >= 60:
        boost = min(0.12, (defensive_value - 55) * 0.02)
        p_base += boost
        p_ceil -= boost * 0.6
        p_mid -= boost * 0.4

    # Ceiling boost: elite offensive upside
    if offensive_ceiling and offensive_ceiling >= 60:
        boost = min(0.12, (offensive_ceiling - 55) * 0.02)
        p_ceil += boost
        p_base -= boost

    # Profile shape: balanced vs extreme
    if offensive_grade and defensive_value:
        gap = abs(offensive_grade - defensive_value)
        if gap >= 20:
            # Extreme: wider distribution
            shift = min(0.08, (gap - 15) * 0.01)
            p_ceil += shift
            p_base += shift * 0.5
            p_mid -= shift * 1.5
        elif gap <= 8:
            # Balanced: tighter distribution
            shift = min(0.08, (12 - gap) * 0.02)
            p_mid += shift
            p_ceil -= shift * 0.6
            p_base -= shift * 0.4

    # SP durability risk
    if bucket == "SP" and durability_score and durability_score < 45:
        penalty = min(0.10, (50 - durability_score) * 0.02)
        p_base += penalty
        p_ceil -= penalty

    # Renormalize to sum to 1.0
    total = p_base + p_mid + p_ceil
    if total > 0:
        p_base /= total
        p_mid /= total
        p_ceil /= total
    return p_base, p_mid, p_ceil


def career_outcome_probs(fv, age, level, bucket, ovr=None, pot=None, def_rating=None,
                         offensive_grade=None, offensive_ceiling=None,
                         defensive_value=None, durability_score=None):
    """Compute cumulative probability of reaching each WAR/season tier.

    Returns dict with:
      tiers: list of {war, prob, label} — cumulative P(prime WAR >= threshold)
      confidence: 0.0-1.0 meter value
    """
    from math import exp

    cfv = _ceiling_fv(pot) if pot else fv
    mid_fv = 5 * round(((fv + cfv) / 2) / 5) if cfv > fv else fv

    # Scenario probabilities (same logic as option value)
    youth_bonus = max(0, (20 - age)) * 0.05
    gap_factor = min(1.0, ((pot or fv) - fv) / 25) if pot and pot > fv else 0
    p_mid = min(0.45, 0.30 + youth_bonus + gap_factor * 0.15) if cfv > fv else 0
    p_ceil = min(0.25, 0.10 + youth_bonus * 0.5 + gap_factor * 0.10) if cfv > fv else 0
    p_base = 1.0 - p_mid - p_ceil

    # Adjust scenario probabilities based on component profile shape
    p_base, p_mid, p_ceil = _adjust_scenario_probs(
        p_base, p_mid, p_ceil, bucket,
        offensive_grade, offensive_ceiling, defensive_value, durability_score)

    # Bust probability = 1 - dev_discount
    dev = _age_adjusted_discount(level, age, ovr=ovr)

    # Component-based development adjustment
    # Premium defense = more reliable development (defense translates earlier)
    # Low durability SP = higher bust risk
    if bucket in _PREMIUM_DEF_POSITIONS and defensive_value and defensive_value >= 60:
        dev = min(1.0, dev * (1.0 + (defensive_value - 55) * 0.01))
    if bucket == "SP" and durability_score and durability_score < 45:
        dev *= max(0.80, 1.0 - (50 - durability_score) * 0.015)

    # WAR for each scenario
    war_base = peak_war(fv, bucket)
    war_mid = peak_war(mid_fv, bucket) if cfv > fv else war_base
    war_ceil = peak_war(cfv, bucket) if cfv > fv else war_base

    # Within each scenario, WAR has variance (not a point estimate).
    # Logistic CDF with wide spread + elite compression: sustaining
    # high WAR is much harder than just reaching the majors.
    # Spread narrows for players who have already realized their tools —
    # less development uncertainty means tighter outcome distribution.
    is_rp = (bucket == "RP")
    compress_center = 1.8 if is_rp else 3.0

    # Realization factor: how close is ovr to pot (0.0 = raw, 1.0 = maxed)
    realization = (ovr / pot) if (ovr and pot and pot > 0) else 0.3
    # Spread shrinks from 0.40 (raw prospect) to 0.20 (fully realized)
    spread_factor = 0.40 - 0.20 * min(1.0, realization)

    def _p_above(mu, threshold):
        s = max(0.5, mu * spread_factor)
        base = 1.0 / (1.0 + exp((threshold - mu) / s))
        compress = 0.35 + 0.65 / (1.0 + exp((threshold - compress_center) / 1.2))
        return base * compress

    max_war = 3.0 if is_rp else 5.0
    _TIERS = []
    war = 0.125
    while war <= max_war:
        if is_rp:
            label = "Contributor" if war <= 0.5 else ("Quality" if war <= 1.0 else ("Elite" if war <= 1.5 else ""))
        else:
            label = "Contributor" if war <= 1.0 else ("Regular" if war <= 2.0 else ("All-Star" if war <= 3.0 else ""))
        _TIERS.append((round(war, 3), label))
        war += 0.125
    # Mark threshold WAR values for display
    if is_rp:
        _THRESHOLD_WARS = {0.5: "Contributor", 1.0: "Quality", 1.5: "Elite"}
    else:
        _THRESHOLD_WARS = {1.0: "Contributor", 2.0: "Regular", 3.0: "All-Star"}
    tiers = []
    thresholds = {}
    for threshold, label in _TIERS:
        p = (p_base * _p_above(war_base, threshold)
             + p_mid * _p_above(war_mid, threshold)
             + p_ceil * _p_above(war_ceil, threshold))
        p *= dev  # scale by development probability
        prob = round(min(1.0, p), 2)
        tiers.append({"war": threshold, "prob": prob, "label": label})
        if threshold in _THRESHOLD_WARS:
            thresholds[_THRESHOLD_WARS[threshold]] = prob

    # Confidence: higher when closer to MLB, higher realization
    level_conf = {"MLB": 0.95, "AAA": 0.85, "AA": 0.70, "A": 0.55,
                  "A-Short": 0.40, "USL": 0.30, "DSL": 0.25, "Intl": 0.20}
    conf = level_conf.get(level, 0.40)
    if ovr and pot and pot > 0:
        realization = ovr / pot  # 0.0-1.0, how much potential is realized
        conf *= 0.5 + 0.5 * realization  # scales conf by 50%-100% based on gap

    # Find middle 50% by area (bars between p75 and p25 of the distribution)
    # Total area = sum of all probs; find WAR thresholds enclosing middle 50%
    total_area = sum(t["prob"] for t in tiers)
    cum = 0
    p25_idx, p75_idx = 0, len(tiers) - 1
    for i, t in enumerate(tiers):
        cum += t["prob"]
        if cum >= total_area * 0.25 and p25_idx == 0:
            p25_idx = i
        if cum >= total_area * 0.75:
            p75_idx = i
            break
    for i, t in enumerate(tiers):
        t["zone"] = "mid" if p25_idx <= i <= p75_idx else "tail"

    likely_lo = tiers[p25_idx]["war"]
    likely_hi = tiers[p75_idx]["war"]

    # Position average starter WAR (Ovr ~52)
    from statsplusplus.evaluation.war import peak_war_from_score as peak_war_from_ovr
    pos_avg_war = round(peak_war_from_ovr(52, bucket), 1)

    return {"tiers": tiers, "thresholds": thresholds, "confidence": round(conf, 2),
            "likely_range": (likely_lo, likely_hi), "pos_avg_war": pos_avg_war,
            "bucket": bucket}


def _ceiling_fv(pot):
    """Map Pot to a ceiling FV grade (discounted slightly — not everyone maxes out)."""
    raw = pot - 5
    return max(40, 5 * round(raw / 5))


def prospect_surplus_with_option(fv, age, level, bucket, ovr=None, pot=None,
                                  fv_plus=False, positional_adjust=False, def_rating=None,
                                  offensive_grade=None, offensive_ceiling=None,
                                  defensive_value=None, durability_score=None):
    """Compute surplus including option value from upside scenarios.
    Returns the higher of base surplus and probability-weighted blended surplus.
    
    Upside probabilities scale with two factors:
    - Youth: younger players have more development time (+5% per year under 20)
    - Pot-FV gap: wider gap = more development runway = higher upside probability
      (a Pot 80 / FV 50 player has much more upside than Pot 52 / FV 50)
    
    Component scores (offensive_grade, defensive_value, etc.) adjust scenario
    probabilities: defense-heavy profiles have higher floors, offense-heavy
    profiles have higher ceilings, balanced profiles have tighter distributions.
    """
    base = prospect_surplus(fv, age, level, bucket, positional_adjust=positional_adjust,
                            fv_plus=fv_plus, ovr=ovr, pot=pot, def_rating=def_rating)
    base_val = base["total_surplus"]

    cfv = _ceiling_fv(pot) if pot else fv
    if cfv <= fv:
        return base_val

    mid_fv = 5 * round(((fv + cfv) / 2) / 5)
    s_mid = prospect_surplus(mid_fv, age, level, bucket, ovr=ovr, pot=pot, def_rating=def_rating)["total_surplus"]
    s_ceil = prospect_surplus(cfv, age, level, bucket, ovr=ovr, pot=pot, def_rating=def_rating)["total_surplus"]

    youth_bonus = max(0, (20 - age)) * 0.05
    gap_factor = min(1.0, ((pot or fv) - fv) / 25)
    p_mid = min(0.45, 0.30 + youth_bonus + gap_factor * 0.15)
    p_ceil = min(0.25, 0.10 + youth_bonus * 0.5 + gap_factor * 0.10)
    p_base = 1.0 - p_mid - p_ceil

    # Component-based adjustments to scenario probabilities
    p_base, p_mid, p_ceil = _adjust_scenario_probs(
        p_base, p_mid, p_ceil, bucket,
        offensive_grade, offensive_ceiling, defensive_value, durability_score)

    blended = p_base * base_val + p_mid * s_mid + p_ceil * s_ceil
    return max(base_val, round(blended))

# ---------------------------------------------------------------------------
# Player lookup
# ---------------------------------------------------------------------------

def find_player(player_id):
    """Look up FV, level, bucket, and age from the DB (prospect_fv + players)."""
    from statsplusplus.data import db
    conn = db.get_conn()
    row = conn.execute("""
        SELECT pf.fv, pf.fv_str, pf.level, pf.bucket, p.age, pf.fv_continuous
        FROM prospect_fv pf
        JOIN players p ON p.player_id = pf.player_id
        WHERE pf.player_id = ?
        ORDER BY pf.eval_date DESC LIMIT 1
    """, (player_id,)).fetchone()
    conn.close()
    if row:
        fv_plus = str(row["fv_str"]).endswith("+")
        fv_cont = row["fv_continuous"] if row["fv_continuous"] is not None else row["fv"]
        return row["fv"], row["level"], row["bucket"], row["age"], fv_plus, fv_cont
    return None, None, None, None, False, None

# ---------------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------------

def print_result(result):
    print(f"\nProspect Surplus Value")
    print(f"  Player:      FV {result['fv']} | {result['bucket']} | {result['level']} | Age {result['age']}")
    print(f"  MLB debut:   ~{result['debut_age']} ({result['years_to_mlb']} yrs out)")
    print(f"  Dev discount: {result['dev_discount']:.0%}")
    cert = result.get('certainty_mult', 1.0)
    if cert != 1.0:
        print(f"  Certainty:    {cert:.2f}x")
    scar = result.get('scarcity_mult', 1.0)
    if scar < 1.0:
        print(f"  Scarcity:     {scar:.2f}x")
    combined = result['dev_discount'] * cert * scar
    print(f"\n  {'Yr':>2}  {'Age':>5}  {'WAR':>5}  {'Mkt Value':>12}  {'Salary':>10}  {'Raw Surp':>10}  {'Adj Surp':>10}")
    print(f"  {'--':>2}  {'---':>5}  {'---':>5}  {'---------':>12}  {'------':>10}  {'--------':>10}  {'--------':>10}")
    for r in result["breakdown"]:
        raw = r['market_value'] - r['salary']
        print(f"  {r['control_year']:>2}  {r['player_age']:>5}  {r['war']:>5.2f}  "
              f"${r['market_value']:>11,}  ${r['salary']:>9,}  ${raw:>9,}  ${r['surplus']:>9,}")
    raw_total = sum(r['market_value'] - r['salary'] for r in result['breakdown'])
    print(f"\n  Raw total: ${raw_total:,}  ×  {combined:.2f}  =  ${result['total_surplus']:,}")

# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Prospect surplus value calculator")
    parser.add_argument("--player", type=int, help="Player ID (looks up FV/level from history)")
    parser.add_argument("--fv",     type=int, help="FV grade (manual override)")
    parser.add_argument("--age",    type=float, help="Player age")
    parser.add_argument("--level",  help="Level (AAA, AA, A, A-Short, USL, DSL, Intl)")
    parser.add_argument("--bucket", help="Positional bucket (SP, RP, C, SS, 2B, CF, COF, 3B, 1B)")
    parser.add_argument("--pos-adjust", action="store_true",
                        help="Apply positional WAR adjustment (use for cross-position comparisons)")
    args = parser.parse_args()

    if args.player:
        fv, level, bucket, db_age, fv_plus, fv_continuous = find_player(args.player)
        if not fv:
            print(f"Player {args.player} not found in prospect_fv. Use --fv/--age/--level/--bucket.")
            sys.exit(1)
        fv     = args.fv     or fv
        level  = args.level  or level
        bucket = args.bucket or bucket
        age    = args.age    or db_age
        # Use fv_continuous for more accurate surplus (pre-rounding FV)
        if not args.fv and fv_continuous is not None:
            fv = fv_continuous
            fv_plus = False  # continuous already incorporates the half-grade
        if not age:
            print("--age required when player not found in DB")
            sys.exit(1)
    else:
        if not all([args.fv, args.age, args.level, args.bucket]):
            parser.print_help()
            sys.exit(1)
        fv, age, level, bucket = args.fv, args.age, args.level, args.bucket
        fv_plus = False

    result = prospect_surplus(fv, age, level, bucket,
                              positional_adjust=args.pos_adjust, fv_plus=fv_plus)
    print_result(result)
    return result

if __name__ == "__main__":
    main()
