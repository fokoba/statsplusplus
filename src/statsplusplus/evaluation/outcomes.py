"""Career outcome probability distribution for prospects.

Pure computation — no DB access. Used by player page and prospect list
displays to show probability of reaching various WAR/season tiers.

Public API:
    career_outcome_probs(fv, age, level, bucket, ...) -> dict
    ceiling_fv(pot) -> int
"""

from __future__ import annotations

from math import exp
from typing import Any, Optional

from statsplusplus.evaluation.surplus import (
    age_adjusted_discount,
    peak_war_from_fv,
)
from statsplusplus.evaluation.war import peak_war_from_score


# Premium defensive positions where defense aids development reliability
_PREMIUM_DEF_POSITIONS = {"SS", "C", "CF"}


def ceiling_fv(pot: int) -> int:
    """Map Pot/ceiling to a ceiling FV grade (discounted — not everyone maxes out)."""
    raw = pot - 5
    return max(40, 5 * round(raw / 5))


def _adjust_scenario_probs(
    p_base: float, p_mid: float, p_ceil: float, bucket: str,
    offensive_grade: Optional[int], offensive_ceiling: Optional[int],
    defensive_value: Optional[int], durability_score: Optional[int],
) -> tuple[float, float, float]:
    """Adjust scenario probabilities based on component profile shape."""
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
            shift = min(0.08, (gap - 15) * 0.01)
            p_ceil += shift
            p_base += shift * 0.5
            p_mid -= shift * 1.5
        elif gap <= 8:
            shift = min(0.08, (12 - gap) * 0.02)
            p_mid += shift
            p_ceil -= shift * 0.6
            p_base -= shift * 0.4

    # SP durability risk
    if bucket == "SP" and durability_score and durability_score < 45:
        penalty = min(0.10, (50 - durability_score) * 0.02)
        p_base += penalty
        p_ceil -= penalty

    # Renormalize
    total = p_base + p_mid + p_ceil
    if total > 0:
        p_base /= total
        p_mid /= total
        p_ceil /= total
    return p_base, p_mid, p_ceil


def career_outcome_probs(
    fv: float,
    age: int,
    level: str,
    bucket: str,
    ovr: Optional[int] = None,
    pot: Optional[int] = None,
    def_rating: Optional[int] = None,
    offensive_grade: Optional[int] = None,
    offensive_ceiling: Optional[int] = None,
    defensive_value: Optional[int] = None,
    durability_score: Optional[int] = None,
    weights=None,
) -> dict[str, Any]:
    """Compute cumulative probability of reaching each WAR/season tier.

    Returns dict with:
        tiers: list of {war, prob, label, zone} — cumulative P(prime WAR >= threshold)
        thresholds: {label: prob} for key WAR thresholds
        confidence: 0.0-1.0 meter value
        likely_range: (low_war, high_war) for middle 50%
        pos_avg_war: positional average WAR for context
        bucket: positional bucket
    """
    cfv = ceiling_fv(pot) if pot else int(fv)
    mid_fv = 5 * round(((fv + cfv) / 2) / 5) if cfv > fv else int(fv)

    # Scenario probabilities
    youth_bonus = max(0, (20 - age)) * 0.05
    gap_factor = min(1.0, ((pot or fv) - fv) / 25) if pot and pot > fv else 0
    p_mid = min(0.45, 0.30 + youth_bonus + gap_factor * 0.15) if cfv > fv else 0.0
    p_ceil = min(0.25, 0.10 + youth_bonus * 0.5 + gap_factor * 0.10) if cfv > fv else 0.0
    p_base = 1.0 - p_mid - p_ceil

    p_base, p_mid, p_ceil = _adjust_scenario_probs(
        p_base, p_mid, p_ceil, bucket,
        offensive_grade, offensive_ceiling, defensive_value, durability_score)

    # Development probability
    dev = age_adjusted_discount(level, age, ovr=ovr)
    if bucket in _PREMIUM_DEF_POSITIONS and defensive_value and defensive_value >= 60:
        dev = min(1.0, dev * (1.0 + (defensive_value - 55) * 0.01))
    if bucket == "SP" and durability_score and durability_score < 45:
        dev *= max(0.80, 1.0 - (50 - durability_score) * 0.015)

    # WAR for each scenario
    war_base = peak_war_from_fv(fv, bucket, weights)
    war_mid = peak_war_from_fv(float(mid_fv), bucket, weights) if cfv > fv else war_base
    war_ceil = peak_war_from_fv(float(cfv), bucket, weights) if cfv > fv else war_base

    # Logistic CDF with elite compression
    is_rp = (bucket == "RP")
    compress_center = 1.8 if is_rp else 3.0
    realization = (ovr / pot) if (ovr and pot and pot > 0) else 0.3
    spread_factor = 0.40 - 0.20 * min(1.0, realization)

    def _p_above(mu: float, threshold: float) -> float:
        s = max(0.5, mu * spread_factor)
        base_p = 1.0 / (1.0 + exp((threshold - mu) / s))
        compress = 0.35 + 0.65 / (1.0 + exp((threshold - compress_center) / 1.2))
        return base_p * compress

    # Build tier list
    max_war = 3.0 if is_rp else 5.0
    _TIERS: list[tuple[float, str]] = []
    w = 0.125
    while w <= max_war:
        if is_rp:
            label = "Contributor" if w <= 0.5 else ("Quality" if w <= 1.0 else ("Elite" if w <= 1.5 else ""))
        else:
            label = "Contributor" if w <= 1.0 else ("Regular" if w <= 2.0 else ("All-Star" if w <= 3.0 else ""))
        _TIERS.append((round(w, 3), label))
        w += 0.125

    if is_rp:
        _THRESHOLD_WARS = {0.5: "Contributor", 1.0: "Quality", 1.5: "Elite"}
    else:
        _THRESHOLD_WARS = {1.0: "Contributor", 2.0: "Regular", 3.0: "All-Star"}

    tiers: list[dict[str, Any]] = []
    thresholds: dict[str, float] = {}
    for threshold, label in _TIERS:
        p = (p_base * _p_above(war_base, threshold)
             + p_mid * _p_above(war_mid, threshold)
             + p_ceil * _p_above(war_ceil, threshold))
        p *= dev
        prob = round(min(1.0, p), 2)
        tiers.append({"war": threshold, "prob": prob, "label": label})
        if threshold in _THRESHOLD_WARS:
            thresholds[_THRESHOLD_WARS[threshold]] = prob

    # Confidence meter
    level_conf = {"MLB": 0.95, "AAA": 0.85, "AA": 0.70, "A": 0.55,
                  "A-Short": 0.40, "USL": 0.30, "DSL": 0.25, "Intl": 0.20}
    conf = level_conf.get(level, 0.40)
    if ovr and pot and pot > 0:
        conf *= 0.5 + 0.5 * min(1.0, ovr / pot)

    # Middle 50% range
    total_area = sum(t["prob"] for t in tiers)
    cum = 0.0
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
    pos_avg_war = round(peak_war_from_score(52, bucket), 1)

    return {
        "tiers": tiers, "thresholds": thresholds,
        "confidence": round(conf, 2),
        "likely_range": (likely_lo, likely_hi),
        "pos_avg_war": pos_avg_war, "bucket": bucket,
    }
