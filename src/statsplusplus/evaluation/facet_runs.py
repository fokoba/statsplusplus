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
    runs_to_war_saturated(runs, anchor) -> float   # tail-capped for ceiling FV
    saturate_war(war, anchor) -> float
    runs_to_composite(runs, mapping, bucket) -> int
"""

from __future__ import annotations

import math
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
    bcurve = (curve or {}).get(bucket) if curve else None
    # Multi-tool positions (catcher: framing+blocking+arm; infield: range+error+
    # arm+DP) blend several tools via calibrated per-tool weights instead of a
    # single range grade. Any curve carrying a "weights" dict uses the blend.
    if bcurve and bcurve.get("weights") and (bcurve.get("multitool") or bcurve.get("catcher")):
        grade = _blended_def_grade(def_tools, bcurve["weights"])
        if grade is None:
            return 0.0
        runs = _linear_curve(float(grade), bcurve, FIELDING_PRIOR_SLOPE)
        if "clamp_lo" in bcurve and "clamp_hi" in bcurve:
            runs = max(bcurve["clamp_lo"], min(bcurve["clamp_hi"], runs))
        return runs
    # Single range-grade path (outfield; any position without a multi-tool curve).
    grade = _primary_def_grade(def_tools, bucket, positional_models)
    if grade is None:
        return 0.0
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


def saturate_war(war: float, anchor: Optional[dict[str, float]] = None) -> float:
    """Saturate a (possibly extrapolated) WAR toward data-grounded tail caps.

    The linear runs→WAR map is only valid in the fitted ~45-65 composite band;
    projecting a prospect's CEILING score linearly runs off to absurd WAR
    (13-18 at score 80) / absurd negatives at the bottom. This compresses the
    tails toward each league's REAL full-time-hitter WAR distribution using a
    tanh that is ~linear near the median (`sat_mid`) and asymptotes to the
    top/bottom caps (`sat_top`=p98, `sat_bot`=p02). Center/slope preserved;
    only the tails bend. No caps in `anchor` → returned unchanged (back-compat).
    """
    if not anchor:
        return war
    top = anchor.get("sat_top")
    bot = anchor.get("sat_bot")
    mid = anchor.get("sat_mid")
    if top is None or bot is None or mid is None:
        return war
    if war >= mid:
        span = max(0.1, top - mid)
        return mid + span * math.tanh((war - mid) / span)
    span = max(0.1, mid - bot)
    return mid + span * math.tanh((war - mid) / span)


def runs_to_war_saturated(runs_above_avg: float, anchor: Optional[dict[str, float]] = None) -> float:
    """runs→WAR then tail-saturated (for ceiling-anchored FV projection)."""
    return saturate_war(runs_to_war(runs_above_avg, anchor), anchor)


def runs_to_composite(
    runs_above_avg: float,
    mapping: Optional[dict[str, Any]] = None,
    bucket: Optional[str] = None,
) -> int:
    """Map runs-above-average to a 20-80 composite.

    Two mapping shapes are supported:

    **Hybrid (preferred)** — a WAR-anchored, per-position-centered map that
    decompresses the scale (a global ``runs_sd``/``comp_sd`` z-map packed ~1 WAR
    into ~2 composite points). ``mapping`` carries::

        {"slope": pts_per_run,          # = pts_per_war / runs_per_win
         "target_center": 50.0,         # a league-avg, avg-position hitter
         "pts_per_war": 4.0,
         "pos_centers": {bucket: population_runs_mean, ...},
         "global_center": float,        # fallback center
         "top_knee": 70.0, "cap": 80.0, # soft tanh above the knee
         "floor": 20.0}                 # display clamp (internal value may be <20)

        comp = target_center + pos_adj(bucket)*pts_per_war
               + (runs - pos_center[bucket]) * slope
        comp = soft-tanh toward cap above top_knee
        comp = clamp(comp, floor, 80)   # bottom is a DISPLAY clamp only

      Per-position centering makes a league-average player at EVERY position map
      to ~``target_center`` plus its positional credit, so composites are
      comparable across positions. The WAR-anchored slope fixes the compression.

    **Legacy affine (back-compat)** — ``{"runs_mean","runs_sd","comp_mean",
    "comp_sd"}``: z-score vs the MLB composite distribution. Used when the hybrid
    keys are absent.

    **Fallback** — ~0 runs → 50 with a modest slope.
    """
    if mapping and "slope" in mapping:
        tc = mapping.get("target_center", 50.0)
        ppw = mapping.get("pts_per_war", 4.0)
        slope = mapping["slope"]
        centers = mapping.get("pos_centers") or {}
        gc = mapping.get("global_center", 0.0)
        center = centers.get(bucket, gc) if bucket else gc
        pos_pts = POSITIONAL_WAR_ADJUSTMENTS.get(bucket or "", 0.0) * ppw
        comp = tc + pos_pts + (runs_above_avg - center) * slope
        # Soft-saturate the TOP tail toward the cap (a hard clip would re-compress
        # the handful of true elites that overshoot). The bottom is NOT soft-
        # saturated — a large, genuinely sub-replacement minor-league population
        # legitimately falls below 20; we clamp it to the floor for DISPLAY only.
        top_knee = mapping.get("top_knee", 70.0)
        cap = mapping.get("cap", 80.0)
        if comp > top_knee:
            span = cap - top_knee
            if span > 0:
                comp = top_knee + span * math.tanh((comp - top_knee) / span)
            else:
                comp = min(comp, cap)
        floor = mapping.get("floor", 20.0)
        return int(round(max(floor, min(cap, comp))))

    if mapping and mapping.get("runs_sd", 0) > 0 and mapping.get("comp_sd", 0) > 0:
        z = (runs_above_avg - mapping["runs_mean"]) / mapping["runs_sd"]
        comp = mapping["comp_mean"] + z * mapping["comp_sd"]
    else:
        comp = 50.0 + runs_above_avg * 0.5
    return max(20, min(80, round(comp)))


def composite_to_runs(
    comp: float,
    mapping: Optional[dict[str, Any]] = None,
    bucket: Optional[str] = None,
) -> float:
    """Invert ``runs_to_composite``: map a 20-80 composite/ceiling score back to
    runs-above-average. Used by the ceiling-anchored FV to recover the WAR a
    POTENTIAL (ceiling) score implies.

    Handles both mapping shapes. For the hybrid map it undoes the per-position
    affine (and reverses the top soft-saturation so an elite, saturated ceiling
    recovers its true pre-saturation run total). The bottom DISPLAY clamp is not
    invertible and is irrelevant here — the FV path never passes a floored score
    through this (ceilings are not sub-replacement).

    Returns runs-above-average (unclamped).
    """
    # --- Hybrid inverse ---
    if mapping and "slope" in mapping:
        slope = mapping["slope"]
        if not slope:
            return 0.0
        tc = mapping.get("target_center", 50.0)
        ppw = mapping.get("pts_per_war", 4.0)
        centers = mapping.get("pos_centers") or {}
        gc = mapping.get("global_center", 0.0)
        center = centers.get(bucket, gc) if bucket else gc
        pos_pts = POSITIONAL_WAR_ADJUSTMENTS.get(bucket or "", 0.0) * ppw
        # Reverse the top soft-saturation (tanh) so a saturated score recovers
        # its true pre-saturation value before the linear inverse.
        top_knee = mapping.get("top_knee", 70.0)
        cap = mapping.get("cap", 80.0)
        c = float(comp)
        if c > top_knee:
            span = cap - top_knee
            # forward: comp = knee + span*tanh((raw-knee)/span); invert via atanh.
            if span > 0:
                frac = (c - top_knee) / span
                frac = max(-0.999999, min(0.999999, frac))
                c = top_knee + span * math.atanh(frac)
        return center + (c - tc - pos_pts) / slope

    # --- Legacy affine inverse ---
    if mapping and mapping.get("comp_sd", 0) > 0 and mapping.get("runs_sd", 0) > 0:
        z = (comp - mapping["comp_mean"]) / mapping["comp_sd"]
        return mapping["runs_mean"] + z * mapping["runs_sd"]

    # Fallback inverse of comp = 50 + runs*0.5
    return (comp - 50.0) / 0.5


# ---------------------------------------------------------------------------
# Calibration helpers (pure — fit curves from (grade, observed-runs) samples)
# ---------------------------------------------------------------------------

def _pstdev(xs: list[float]) -> float:
    """Population standard deviation (0.0 for <2 points)."""
    n = len(xs)
    if n < 2:
        return 0.0
    m = sum(xs) / n
    return math.sqrt(sum((x - m) ** 2 for x in xs) / n)


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


# Multi-tool defensive curves: positions whose defensive VALUE is not captured
# by a single range grade blend several tools. The per-tool DB names (lowercase,
# used by calibration) and the engine's extracted CamelCase keys both resolve via
# the alias map. Catcher: framing + blocking + arm (no range/ZR). Infield: range
# + error + arm + double-play (range leads at SS/2B; error+arm lead at 3B — see
# the Session-97 infield-defense analysis; the Session-93 range-only result was
# IF/OF-approximate and is superseded here by per-position multi-tool weights).
CATCHER_DEF_TOOLS: tuple[str, ...] = ("c_frm", "c_blk", "c_arm")
INFIELD_DEF_TOOLS: tuple[str, ...] = ("ifr", "ife", "ifa", "tdp")
OUTFIELD_DEF_TOOLS: tuple[str, ...] = ("ofr", "ofe", "ofa")
_DEF_TOOL_ALIASES: dict[str, tuple[str, ...]] = {
    "c_frm": ("c_frm", "CFrm"),
    "c_blk": ("c_blk", "CBlk"),
    "c_arm": ("c_arm", "CArm"),
    "ifr": ("ifr", "IFR"),
    "ife": ("ife", "IFE"),
    "ifa": ("ifa", "IFA"),
    "tdp": ("tdp", "TDP"),
    "ofr": ("ofr", "OFR"),
    "ofe": ("ofe", "OFE"),
    "ofa": ("ofa", "OFA"),
}


def _def_tool(def_tools: dict[str, float | int | None], key: str):
    """Fetch a defensive tool value under either the lowercase or CamelCase key."""
    for k in _DEF_TOOL_ALIASES.get(key, (key,)):
        v = def_tools.get(k)
        if v is not None:
            return v
    return None


def _blended_def_grade(def_tools: dict[str, float | int | None],
                       weights: dict[str, float]) -> Optional[float]:
    """Weighted-blend defensive grade (20-80) from a multi-tool `weights` dict.

    `weights` is the calibrated per-tool blend (keyed by canonical lowercase tool
    names). Falls back to equal weights over the tools present. Returns None if no
    weighted tool is available. Accepts either key casing (see aliases).
    """
    vals = [(_def_tool(def_tools, k), w) for k, w in weights.items()]
    present = [(float(v), w) for v, w in vals if v is not None]
    if not present:
        return None
    tw = sum(w for _, w in present)
    if tw <= 0:  # no calibrated weights for the present tools — equal-weight them
        return sum(v for v, _ in present) / len(present)
    return sum(v * (w / tw) for v, w in present)


def calibrate_multitool_curve(
    tools: tuple[str, ...],
    tool_rows: list[dict[str, float]],
    observed_runs: list[float],
    shrink: float = 0.6,
    min_n: int = 8,
    center_grade: Optional[float] = None,
    weight_prior: Optional[dict[str, float]] = None,
    prior_strength: float = 40.0,
) -> Optional[dict[str, Any]]:
    """Fit a multi-tool defensive-runs curve (catcher / infield).

    Generalizes the single-range-tool curve to a weighted blend of several tools:
      1) derive per-tool blend weights from each tool's standalone correlation
         with observed defensive runs (negatives floored to 0),
      2) SHRINK those weights toward `weight_prior` by sample size
         (n/(n+prior_strength)) so thin per-position/per-league cells fall back to
         the cross-league prior instead of fitting noise,
      3) build the blended grade and fit a centered/clamped linear grade->runs
         curve on it (reusing calibrate_grade_runs_curve).

    `tool_rows`: list of normalized-grade dicts keyed by `tools` (aligned with
    `observed_runs`). Returns a curve dict shaped for ``fielding_runs`` (with a
    "weights" sub-dict + "multitool": True), or None if the sample is too thin.
    """
    if len(observed_runs) < min_n:
        return None
    n = len(observed_runs)
    # 1) data-driven weights from each tool's regression SLOPE (runs per rating
    # point) — NOT correlation. Correlation measures how much of the run VARIANCE
    # a tool explains, which conflates importance with noise: a position where a
    # tool drives large but noisy run swings gets a LOW correlation despite the
    # tool mattering more (e.g. CF range vs corner — equal runs-per-point, but CF
    # outcomes are noisier, so correlation wrongly down-weighted CF range). Slope
    # is the right quantity: "how many runs does a point of this tool buy." Scaled
    # by the tool's rating SD so the weight reflects realized run spread, then
    # floored at 0 (a tool with no positive marginal value gets no weight).
    data_w: dict[str, float] = {}
    for k in tools:
        pairs = [(r[k], observed_runs[i]) for i, r in enumerate(tool_rows)
                 if r.get(k) is not None]
        if len(pairs) >= min_n:
            xs = [p[0] for p in pairs]
            ys = [p[1] for p in pairs]
            _, slope, _ = _ols_1var(xs, ys)
            sd = _pstdev(xs)
            data_w[k] = max(0.0, slope) * sd  # runs of spread this tool contributes
    dsum = sum(data_w.values())
    if dsum > 0:
        data_w = {k: v / dsum for k, v in data_w.items()}
    # 2) shrink toward the prior by sample size.
    if weight_prior:
        alpha = n / (n + prior_strength)  # 0..1 weight on the data fit
        merged = {}
        for k in tools:
            dv = data_w.get(k, 0.0)
            pv = weight_prior.get(k, 0.0)
            merged[k] = alpha * dv + (1 - alpha) * pv
        weights = merged
    elif data_w:
        weights = data_w
    else:
        weights = {k: 1.0 / len(tools) for k in tools}
    wsum = sum(weights.values()) or 1.0
    weights = {k: round(v / wsum, 3) for k, v in weights.items()}
    # 3) blended grade + centered/clamped linear fit.
    blended = [_blended_def_grade(r, weights) for r in tool_rows]
    gx = [g for g in blended if g is not None]
    gy = [observed_runs[i] for i, g in enumerate(blended) if g is not None]
    if len(gx) < min_n:
        return None
    curve = calibrate_grade_runs_curve(gx, gy, FIELDING_PRIOR_SLOPE,
                                       shrink=shrink, min_n=min_n,
                                       center_grade=center_grade)
    if curve is None:
        return None
    curve["weights"] = weights
    curve["multitool"] = True
    return curve


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


def calibrate_hybrid_mapping(
    pos_run_means: dict[str, float],
    global_run_mean: float,
    runs_per_win: float,
    pts_per_war: float = 4.0,
    target_center: float = 50.0,
    top_knee: float = 70.0,
    cap: float = 80.0,
    floor: float = 20.0,
) -> dict[str, Any]:
    """Build the hybrid runs->composite mapping (per-position center + WAR slope).

    Decouples the composite SPREAD from the (self-referential, compressed) prior
    composite distribution. The slope is anchored to ``pts_per_war`` grade points
    per 1 WAR, so one win of value is worth a chosen, honest number of composite
    points (default 4.0 ~ doubles the old ~2 pts/WAR). Each position is centered
    on its own population run-mean so a league-average player at every position
    maps to ``target_center`` plus its positional credit (comparable across
    positions). The top tail soft-saturates toward ``cap``; the bottom is a
    display clamp at ``floor`` (a large sub-replacement MiLB population genuinely
    falls below 20). Consumed by ``runs_to_composite`` (hybrid branch).

    Args:
        pos_run_means: {bucket: population tool-projected runs-mean}.
        global_run_mean: fallback center for buckets without enough sample.
        runs_per_win: the league's runs->WAR anchor (slope = pts_per_war/rpw).
        pts_per_war: target composite points per 1 WAR of value.
    """
    rpw = runs_per_win if runs_per_win and runs_per_win > 0 else DEFAULT_RUNS_PER_WIN
    return {
        "slope": pts_per_war / rpw,
        "pts_per_war": pts_per_war,
        "target_center": target_center,
        "pos_centers": {k: round(v, 2) for k, v in pos_run_means.items()},
        "global_center": round(global_run_mean, 2),
        "top_knee": top_knee,
        "cap": cap,
        "floor": floor,
    }
