"""Run-space facet evaluation spine (hitters) — pure computation.

The hitter's value is built additively in RUNS from three independently-
calibrated facets, then converted to WAR (OOTP-anchored) and to a 20-80
composite. This replaces the old grade-space share-weighted blend, where a
"70" in one facet was wrongly treated as equal in value to a "70" in another.

    bat_runs         = wRAA from projected wOBA (600-PA baseline)
    baserunning_runs = f_br(speed, steal)          # calibrated grade->UBR
    fielding_runs    = f_def(def_tools, position)  # calibrated grade->ZR-runs
    positional_adj   = POS_ADJ_RUNS[position]      # runs
    ---------------------------------------------------------------
    runs_above_avg   = sum(...)
    war              = runs_above_avg / runs_per_win + replacement_wins
    composite(20-80) = affine map calibrated to the current composite dist

Design ref: .kiro/specs/run-space-facet-model/. No DB, no I/O, no global state.
`facet_runs` is the TOOL-WAR source only; it does not touch war.py's
stat-history-WAR path.

Public API:
    bat_runs(proj_woba, lg_woba, woba_scale, pa=600) -> float
    baserunning_runs(tools, curve) -> float
    fielding_runs(def_tools, bucket, curve, models) -> float
    positional_adj_runs(bucket) -> float
    total_runs(...) -> dict            # facet breakdown + total
    runs_to_war(runs, anchor) -> float
    runs_to_composite(runs, mapping) -> int
"""

from __future__ import annotations

from typing import Any, Optional

from statsplusplus.utils.positions import (
    POSITIONAL_WAR_ADJUSTMENTS,
    estimate_all_positions,
)

# Runs-per-win default (OOTP-anchored override supplied via `anchor`).
DEFAULT_RUNS_PER_WIN: float = 9.5
# Replacement level in runs below average for a full-time position player
# (standard ~ -20 runs / 600 PA). OOTP-anchored override via `anchor`.
DEFAULT_REPLACEMENT_RUNS: float = 20.0

# Grade-band anchors (20-80) for the linear grade->runs facet curves' fallback
# priors. Baserunning: a 50-grade runner is ~league-average (0 UBR runs); an
# 80 runner ~ +6 runs over a full season; a 20 runner ~ -6. Slope ~ 0.2 runs/pt.
BASERUNNING_PRIOR_SLOPE: float = 0.20   # runs per grade point above 50
FIELDING_PRIOR_SLOPE: float = 0.50      # runs per grade point above 50 (range)


# ---------------------------------------------------------------------------
# Per-facet aging curves (literature-based priors; multipliers on peak runs).
# Facets age differently (spec §3.1a): baserunning peaks EARLIEST and declines
# steepest (raw speed is physical); bat peaks ~26-28 with a gentle decline;
# defense tracks speed but is partly offset by positioning/experience (moderate).
# These are strong priors — per-league longitudinal facet data is too thin to fit
# freely (small-sample concern), so we default to these and only shrink-adjust.
# ---------------------------------------------------------------------------

AGING_BAT: dict[int, float] = {
    24: 0.93, 25: 0.96, 26: 0.99, 27: 1.00, 28: 1.00, 29: 0.98, 30: 0.94,
    31: 0.89, 32: 0.83, 33: 0.76, 34: 0.68, 35: 0.59, 36: 0.50, 38: 0.34, 40: 0.18,
}
AGING_BASERUNNING: dict[int, float] = {  # peaks early, declines earliest/steepest
    22: 1.00, 23: 1.00, 24: 0.99, 25: 0.97, 26: 0.93, 27: 0.88, 28: 0.82,
    29: 0.75, 30: 0.67, 31: 0.58, 32: 0.49, 33: 0.40, 34: 0.31, 36: 0.15, 38: 0.0,
}
AGING_DEFENSE: dict[int, float] = {  # tracks speed but experience offsets => moderate
    23: 1.00, 24: 1.00, 25: 0.99, 26: 0.97, 27: 0.94, 28: 0.90, 29: 0.85,
    30: 0.79, 31: 0.72, 32: 0.64, 33: 0.56, 34: 0.47, 35: 0.38, 37: 0.22, 40: 0.05,
}

# ---------------------------------------------------------------------------
# Per-facet DEVELOPMENT (up-slope) curves — fraction of the current→ceiling gap
# closed by a given age (spec: per-facet-aging-projection, P2). Mirror of the
# aging curves for the pre-peak growth phase: the BAT develops most and latest
# (still gaining into the mid-20s); BASERUNNING is a near-fixed physical trait
# that barely "develops" (mostly realized young); DEFENSE develops moderately
# (reads/positioning improve with reps). Literature-prior shapes — per-league
# longitudinal facet data is too thin to fit freely (survivorship + small n).
# ---------------------------------------------------------------------------

DEV_BAT: dict[int, float] = {  # slow, extends latest — big gains through mid-20s
    17: 0.10, 18: 0.18, 19: 0.28, 20: 0.40, 21: 0.52, 22: 0.63,
    23: 0.73, 24: 0.82, 25: 0.90, 26: 0.96, 27: 1.00,
}
DEV_BASERUNNING: dict[int, float] = {  # near-fixed early; little to develop
    17: 0.55, 18: 0.65, 19: 0.75, 20: 0.84, 21: 0.91, 22: 0.96, 23: 1.00,
}
DEV_DEFENSE: dict[int, float] = {  # moderate; reps/positioning through early 20s
    17: 0.30, 18: 0.42, 19: 0.54, 20: 0.66, 21: 0.77, 22: 0.86,
    23: 0.93, 24: 0.98, 25: 1.00,
}


def _dev_progress(age: float, curve: dict[int, float]) -> float:
    """Fraction of the current→ceiling gap realized by ``age`` (0..1)."""
    ages = sorted(curve)
    if age <= ages[0]:
        return curve[ages[0]]
    if age >= ages[-1]:
        return 1.0
    for i in range(len(ages) - 1):
        a0, a1 = ages[i], ages[i + 1]
        if a0 <= age <= a1:
            t = (age - a0) / (a1 - a0)
            return curve[a0] + t * (curve[a1] - curve[a0])
    return 1.0


def project_facet_runs(
    facet: str,
    current_runs: float,
    ceiling_runs: float,
    age: float,
    dev_pace: float = 1.0,
) -> float:
    """Project a facet's runs at ``age`` (development up-slope only, pre-peak).

    Grows ``current_runs`` toward ``ceiling_runs`` by the per-facet development
    progress at ``age``, scaled by ``dev_pace`` (a clamped rate modifier from
    dev_speed; 1.0 = neutral). Only closes the gap when there IS room
    (ceiling > current); never projects below current.
    """
    curve = {"bat": DEV_BAT, "baserunning": DEV_BASERUNNING, "fielding": DEV_DEFENSE}.get(facet, DEV_BAT)
    if ceiling_runs <= current_runs:
        return current_runs
    progress = min(1.0, _dev_progress(age, curve) * max(0.0, dev_pace))
    return current_runs + (ceiling_runs - current_runs) * progress


def dev_pace_from_z(z: Optional[float], confidence: float = 1.0) -> float:
    """Map a dev_speed z-score to a growth-rate MODIFIER, clamped to [0.6, 1.4].

    A modifier, never a driver: a fast developer (z>0) closes the gap sooner, a
    stalled one (z<0) slower. Pulled toward 1.0 by ``confidence`` in [0,1] (low
    dev_speed confidence → little/no adjustment). dev_speed changes the TIMING of
    reaching the (already-determined) ceiling, never the ceiling itself.
    """
    if z is None:
        return 1.0
    raw = 1.0 + max(-1.0, min(1.0, z / 2.0)) * 0.4   # z=±2 -> ±0.4
    raw = max(0.6, min(1.4, raw))
    conf = max(0.0, min(1.0, confidence))
    return 1.0 + (raw - 1.0) * conf


def _aging_mult(age: float, curve: dict[int, float]) -> float:
    ages = sorted(curve)
    if age <= ages[0]:
        return curve[ages[0]]
    if age >= ages[-1]:
        return curve[ages[-1]]
    for i in range(len(ages) - 1):
        a0, a1 = ages[i], ages[i + 1]
        if a0 <= age <= a1:
            t = (age - a0) / (a1 - a0)
            return curve[a0] + t * (curve[a1] - curve[a0])
    return 1.0


def facet_aging_mult(age: float, bat_runs: float, br_runs: float, fld_runs: float) -> float:
    """Per-facet blended aging multiplier on peak WAR (P1 of the projection spec).

    Instead of one whole-player aging curve, age each facet by its own curve
    (baserunning earliest/steepest, bat latest, defense moderate) and recombine
    weighted by each facet's share of the player's POSITIVE run contribution.
    Positional adjustment is age-invariant and excluded from the weighting.

    Returns a single multiplier equivalent to the run-weighted average of the
    three facet aging multipliers — so it slots in where ``aging_mult`` is used
    without changing the surplus math shape. Falls back to the bat curve when
    the player has no meaningful positive facet runs.
    """
    m_bat = _aging_mult(age, AGING_BAT)
    m_br = _aging_mult(age, AGING_BASERUNNING)
    m_fld = _aging_mult(age, AGING_DEFENSE)
    # Weight by positive run contribution (a facet that costs runs doesn't get
    # "aged" as a value source — clamp negatives to 0 for weighting only).
    w_bat = max(0.0, bat_runs)
    w_br = max(0.0, br_runs)
    w_fld = max(0.0, fld_runs)
    total = w_bat + w_br + w_fld
    if total <= 0:
        return m_bat
    return (m_bat * w_bat + m_br * w_br + m_fld * w_fld) / total


def facet_stat_confidence(facet: str, pa_or_ip: float) -> float:
    """Per-facet stabilization ramp (spec §3.1a).

    Different metrics stabilize at different rates:
      bat (wRAA): SLOW  — full ~600 PA
      baserunning (UBR): FAST — low variance, full ~250 PA
      fielding (ZR): SLOWEST — noisy, full ~900 PA (~1.5 seasons)
    """
    full = {"bat": 600.0, "baserunning": 250.0, "fielding": 900.0}.get(facet, 600.0)
    cap = {"bat": 0.90, "baserunning": 0.95, "fielding": 0.80}.get(facet, 0.9)
    if pa_or_ip <= 0:
        return 0.0
    return min(cap, (pa_or_ip / full) ** 1.1)


def bat_runs(
    proj_woba: float,
    lg_woba: float,
    woba_scale: float,
    pa: float = 600.0,
) -> float:
    """wRAA = ((wOBA - lg_wOBA) / wOBA_scale) * PA. Runs above average."""
    if woba_scale <= 0:
        return 0.0
    return ((proj_woba - lg_woba) / woba_scale) * pa


def _linear_curve(grade: float, curve: Optional[dict[str, float]], prior_slope: float) -> float:
    """Apply a calibrated linear grade->runs curve, or a prior slope fallback.

    curve = {"intercept": a, "slope": b} → runs = a + b*grade.
    Fallback: prior_slope * (grade - 50).
    """
    if curve and "slope" in curve:
        return float(curve.get("intercept", 0.0)) + float(curve["slope"]) * grade
    return prior_slope * (grade - 50.0)


def baserunning_runs(
    tools: dict[str, float | int | None],
    curve: Optional[dict[str, float]] = None,
) -> float:
    """Baserunning runs (UBR-scale) from speed/steal grades.

    Uses a calibrated speed->UBR curve when available, else a prior slope.
    Steal contributes a small secondary adjustment.
    """
    spd = tools.get("speed")
    if spd is None:
        return 0.0
    runs = _linear_curve(float(spd), curve, BASERUNNING_PRIOR_SLOPE)
    # Secondary: aggressive/successful basestealing adds a touch on top of raw speed.
    stl = tools.get("steal")
    if stl is not None and curve and "steal_slope" in curve:
        runs += float(curve["steal_slope"]) * (float(stl) - 50.0)
    return runs


def fielding_runs(
    def_tools: dict[str, float | int | None],
    bucket: str,
    curve: Optional[dict[str, Any]] = None,
    positional_models: Optional[dict[str, Any]] = None,
) -> float:
    """Fielding runs (ZR-scale) at the given bucket from defensive tools.

    curve maps bucket -> {"intercept", "slope"} on the position's primary
    range grade. Falls back to a prior slope. 1B/DH get ~0 (little defensive
    spread captured by range tools).
    """
    if bucket in ("1B", "DH"):
        return 0.0
    # Primary range grade for the bucket (estimated positional rating if needed).
    grade = _primary_def_grade(def_tools, bucket, positional_models)
    if grade is None:
        return 0.0
    bcurve = (curve or {}).get(bucket) if curve else None
    runs = _linear_curve(float(grade), bcurve, FIELDING_PRIOR_SLOPE)
    # Clamp to the position's observed ZR range (prevent steep-slope extrapolation
    # to absurd runs far from the calibration sample's grade range).
    if bcurve and "clamp_lo" in bcurve and "clamp_hi" in bcurve:
        runs = max(bcurve["clamp_lo"], min(bcurve["clamp_hi"], runs))
    return runs


def _primary_def_grade(
    def_tools: dict[str, float | int | None],
    bucket: str,
    positional_models: Optional[dict[str, Any]],
) -> Optional[float]:
    """Best available range grade for the bucket.

    The fielding curves are calibrated on the position's RANGE tool (OFR for
    outfield, IFR for infield, CArm for catcher), so evaluation must use the
    same grade type. Prefer the actual range rating; the positional-model
    estimate is only a last resort (it under/over-estimates vs the true rating
    — e.g. an elite CF with OFR 70 was estimated at 58, wrongly costing runs).
    """
    # 1) Explicit per-bucket grade if the caller supplied one.
    direct = def_tools.get(bucket)
    if direct is not None:
        return float(direct)
    # 2) The position-appropriate RANGE tool (matches the calibration grade).
    if bucket in ("CF", "COF", "LF", "RF"):
        for k in ("OFR", "ofr"):
            if def_tools.get(k) is not None:
                return float(def_tools[k])
    elif bucket in ("SS", "2B", "3B"):
        for k in ("IFR", "ifr"):
            if def_tools.get(k) is not None:
                return float(def_tools[k])
    elif bucket == "C":
        for k in ("CArm", "c_arm"):
            if def_tools.get(k) is not None:
                return float(def_tools[k])
    # 3) Positional-model estimate (last resort).
    if positional_models:
        ests = estimate_all_positions(def_tools, positional_models)
        if bucket in ests:
            return ests[bucket]
    # 4) Any generic range tool.
    for k in ("ifr", "ofr", "IFR", "OFR"):
        if def_tools.get(k) is not None:
            return float(def_tools[k])
    return None


def positional_adj_runs(bucket: str, runs_per_win: float = DEFAULT_RUNS_PER_WIN) -> float:
    """Positional adjustment in runs (POSITIONAL_WAR_ADJUSTMENTS is in WAR)."""
    war_adj = POSITIONAL_WAR_ADJUSTMENTS.get(bucket, 0.0)
    return war_adj * runs_per_win


def best_position_fielding(
    def_tools: dict[str, float | int | None],
    candidate_buckets: list[str],
    curve: Optional[dict[str, Any]],
    positional_models: Optional[dict[str, Any]],
    runs_per_win: float = DEFAULT_RUNS_PER_WIN,
) -> tuple[str, float]:
    """Pick the bucket maximizing fielding_runs + positional_adj; return it + runs.

    Rewards defensive flexibility indirectly (a 2B/3B who can cover SS gets
    SS's positional credit). Returns (best_bucket, def_plus_pos_runs).
    """
    best_bucket = candidate_buckets[0] if candidate_buckets else "1B"
    best_val = float("-inf")
    for b in candidate_buckets:
        fr = fielding_runs(def_tools, b, curve, positional_models)
        pa = positional_adj_runs(b, runs_per_win)
        if fr + pa > best_val:
            best_val = fr + pa
            best_bucket = b
    return best_bucket, (best_val if best_val != float("-inf") else 0.0)


def total_runs(
    proj_woba: float,
    lg_woba: float,
    woba_scale: float,
    tools: dict[str, float | int | None],
    def_tools: dict[str, float | int | None],
    bucket: str,
    br_curve: Optional[dict[str, float]] = None,
    def_curve: Optional[dict[str, Any]] = None,
    positional_models: Optional[dict[str, Any]] = None,
    pa: float = 600.0,
    runs_per_win: float = DEFAULT_RUNS_PER_WIN,
    reliability_penalty_runs: float = 0.0,
) -> dict[str, float]:
    """Sum the facet runs into a total runs-above-average with breakdown."""
    b = bat_runs(proj_woba, lg_woba, woba_scale, pa)
    br = baserunning_runs(tools, br_curve)
    fr = fielding_runs(def_tools, bucket, def_curve, positional_models)
    pos = positional_adj_runs(bucket, runs_per_win)
    total = b + br + fr + pos - reliability_penalty_runs
    return {
        "bat_runs": b,
        "baserunning_runs": br,
        "fielding_runs": fr,
        "positional_adj": pos,
        "reliability_penalty": -reliability_penalty_runs,
        "total_runs": total,
    }


def runs_to_war(runs_above_avg: float, anchor: Optional[dict[str, float]] = None) -> float:
    """Convert runs-above-average to WAR (OOTP-anchored)."""
    rpw = (anchor or {}).get("runs_per_win", DEFAULT_RUNS_PER_WIN)
    repl_runs = (anchor or {}).get("replacement_runs", DEFAULT_REPLACEMENT_RUNS)
    if rpw <= 0:
        rpw = DEFAULT_RUNS_PER_WIN
    return (runs_above_avg + repl_runs) / rpw


def runs_to_composite(runs_above_avg: float, mapping: Optional[dict[str, float]] = None) -> int:
    """Map runs-above-average to a 20-80 composite via a calibrated affine map.

    mapping = {"runs_mean", "runs_sd", "comp_mean", "comp_sd"} calibrated to the
    current MLB composite distribution so the scale is preserved. Fallback maps
    ~0 runs → 50 with a modest slope.
    """
    if mapping and mapping.get("runs_sd", 0) > 0 and mapping.get("comp_sd", 0) > 0:
        z = (runs_above_avg - mapping["runs_mean"]) / mapping["runs_sd"]
        comp = mapping["comp_mean"] + z * mapping["comp_sd"]
    else:
        comp = 50.0 + runs_above_avg * 0.5
    return max(20, min(80, round(comp)))


# ---------------------------------------------------------------------------
# Calibration helpers (pure — fit curves from (grade, observed-runs) samples)
# ---------------------------------------------------------------------------

def _ols_1var(xs: list[float], ys: list[float]) -> tuple[float, float, float]:
    """1-var OLS -> (intercept, slope, r)."""
    n = len(xs)
    if n < 2:
        return 0.0, 0.0, 0.0
    mx = sum(xs) / n; my = sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    if sxx == 0:
        return my, 0.0, 0.0
    slope = sxy / sxx
    return my - slope * mx, slope, (sxy / (sxx * syy) ** 0.5 if syy > 0 else 0.0)


def calibrate_grade_runs_curve(
    grades: list[float],
    observed_runs: list[float],
    prior_slope: float,
    shrink: float = 0.75,
    min_n: int = 8,
    clamp_pad: float = 1.1,
    center_grade: Optional[float] = None,
) -> Optional[dict[str, float]]:
    """Fit a centered, clamped grade->runs curve from samples.

    Centers at ``center_grade`` if given (the POPULATION-average grade at the
    position — so a league-average fielder gets ~0 runs), else the calibration
    sample's mean grade. The distinction matters when the calibration sample
    (e.g. high-IP starters) is more selective than the population being scored:
    centering on the selective sample shifts the whole population negative.
    Shrinks the slope toward the prior and clamps to the observed run range.
    Returns None if the sample is too thin (caller falls back to the prior slope).
    """
    if len(grades) < min_n:
        return None
    _, slope, r = _ols_1var(grades, observed_runs)
    slope *= shrink
    sample_mean = sum(grades) / len(grades)
    center = center_grade if center_grade is not None else sample_mean
    # Clamp to ROBUST percentiles of observed runs (5th/95th), not min/max — a
    # single noisy extreme season otherwise sets the ceiling and lets the linear
    # slope over-extrapolate at the high end (observed fielding runs plateau,
    # e.g. corner-OF ZR tops out ~+6-7, but a steep slope reached +10.8).
    sr = sorted(observed_runs)
    lo_i = max(0, int(0.05 * (len(sr) - 1)))
    hi_i = min(len(sr) - 1, int(round(0.95 * (len(sr) - 1))))
    return {
        "intercept": -slope * center,
        "slope": slope,
        "r": round(r, 3),
        "n": len(grades),
        "mean_grade": round(center, 1),
        "clamp_lo": round(sr[lo_i], 1),
        "clamp_hi": round(sr[hi_i], 1),
    }


def solve_war_anchor(
    total_runs_list: list[float],
    ootp_wars: list[float],
) -> dict[str, float]:
    """Solve runs_per_win + replacement so our WAR matches OOTP's WAR dist.

    Matches mean and sd of OOTP qualified-hitter WAR over the pool.
    """
    import statistics as _st
    if len(ootp_wars) < 10:
        return {"runs_per_win": DEFAULT_RUNS_PER_WIN, "replacement_runs": DEFAULT_REPLACEMENT_RUNS}
    om, osd = _st.mean(ootp_wars), _st.pstdev(ootp_wars)
    rm, rsd = _st.mean(total_runs_list), _st.pstdev(total_runs_list)
    rpw = rsd / osd if osd > 0 else DEFAULT_RUNS_PER_WIN
    if rpw <= 0:
        rpw = DEFAULT_RUNS_PER_WIN
    return {"runs_per_win": round(rpw, 3), "replacement_runs": round(om * rpw - rm, 2)}


def calibrate_composite_mapping(
    total_runs_list: list[float],
    current_composites: list[float],
    pop_runs_mean: Optional[float] = None,
    pop_comp_mean: Optional[float] = None,
) -> dict[str, float]:
    """Affine map runs->20-80 calibrated to the current composite distribution.

    The SPREAD (runs_sd, comp_sd) is taken from the fit sample (qualified MLB
    regulars — a clean, high-PA population with good spread). The CENTER,
    however, must be anchored on the whole POPULATION, not just qualified
    starters: the run->composite map is applied to the entire player universe
    (prospects included), so if it's centered on the selective 300+ PA sample
    (whose mean composite is inflated), every player maps too high. Pass
    pop_runs_mean / pop_comp_mean (means over ALL MLB-level hitters) to anchor
    the center on the population. Falls back to the fit-sample means when the
    population anchors aren't supplied (backward compatible).
    """
    import statistics as _st
    if len(current_composites) < 10 or len(total_runs_list) < 10:
        return {}
    runs_mean = pop_runs_mean if pop_runs_mean is not None else _st.mean(total_runs_list)
    comp_mean = pop_comp_mean if pop_comp_mean is not None else _st.mean(current_composites)
    return {
        "runs_mean": runs_mean,
        "runs_sd": _st.pstdev(total_runs_list),
        "comp_mean": comp_mean,
        "comp_sd": _st.pstdev(current_composites),
    }
