"""Composite score computation for hitters and pitchers.

Pure functions: take tool ratings + weights, return scores on 20-80 scale.
No DB access, no global state.

Public API:
    compute_composite_hitter(tools, weights, defense, def_weights) -> int
    compute_composite_pitcher(tools, weights, arsenal, stamina, role) -> int
    compute_tool_only_score(player_type, tools, weights, ...) -> int
    compute_composite_mlb(tool_score, stat_seasons, ...) -> int
    compute_offensive_grade(tools, weights) -> int | None
    compute_baserunning_value(tools, weights) -> int | None
    compute_defensive_value(defense, def_weights) -> int | None
    compute_combined_value(primary, secondary) -> int
    offensive_grade_raw(tools, weights) -> float | None
    baserunning_value_raw(tools, weights) -> float | None
    defensive_value_raw(defense, def_weights) -> float | None
    tool_transform(val) -> float
    sub_mlb_floor_penalty(tools) -> float
    stat_to_2080(stat_plus) -> float
    pitcher_stat_to_2080(stat_plus) -> float
"""

from __future__ import annotations

from typing import Any, Optional

from statsplusplus.evaluation.constants import (
    TOOL_TRANSFORM_LOW_THRESHOLD,
    TOOL_TRANSFORM_HIGH_THRESHOLD,
    TOOL_TRANSFORM_LOW_PENALTY,
    TOOL_TRANSFORM_HIGH_BONUS,
    MLB_TOOL_FLOOR,
    FLOOR_PENALTY_RATE,
    HITTER_IMBALANCE_SPREAD_THRESHOLD,
    HITTER_IMBALANCE_SPREAD_RATE,
    HITTER_IMBALANCE_WEAKNESS_THRESHOLD,
    HITTER_IMBALANCE_WEAKNESS_RATE,
    PITCHER_IMBALANCE_SPREAD_THRESHOLD,
    PITCHER_IMBALANCE_SPREAD_RATE,
    PITCHER_IMBALANCE_WEAKNESS_THRESHOLD,
    PITCHER_IMBALANCE_WEAKNESS_RATE,
)

# ---------------------------------------------------------------------------
# Tool keys
# ---------------------------------------------------------------------------

OFFENSIVE_TOOL_KEYS: tuple[str, ...] = ("contact", "gap", "power", "eye")
BASERUNNING_TOOL_KEYS: tuple[str, ...] = ("speed", "steal", "stl_rt")
PITCHER_TOOL_KEYS: tuple[str, ...] = ("stuff", "movement", "control", "hra", "pbabip")
# Core keys for imbalance/floor penalties (extended ratings excluded)
PITCHER_CORE_KEYS: tuple[str, ...] = ("stuff", "movement", "control")

# A "Specialist" carries composite on one standout tool; a "Generalist"
# spreads it across several. Threshold on compute_specialist_score()'s 0-100
# scale — 50 means at least half the max possible 20-80 spread (a 30-point
# gap between best and worst core tool, e.g. an 80 next to a 50).
SPECIALIST_SCORE_THRESHOLD: int = 50


def compute_specialist_score(tools: dict[str, float | int | None], is_pitcher: bool) -> int:
    """0-100: how much of a player's carrying skill comes from one standout
    tool vs being spread across several core tools.

    Reuses the exact same tool_spread concept (max - min across core tools)
    that compute_composite_hitter/pitcher already use for the tool-imbalance
    penalty below — this just exposes it as a standalone rating instead of a
    composite deduction, so "specialist" here means the same thing it
    already means to the composite formula.

    0 = perfectly balanced (every core tool at the same grade).
    100 = maximally specialized (widest possible 20-80 spread, e.g. one
    tool at 80 and another at 20).

    Args:
        tools: Tool ratings on the 20-80 canonical scale (same dict passed
            to compute_composite_hitter/pitcher).
        is_pitcher: Whether to use pitcher core tools (stuff/movement/
            control) or hitter core tools (contact/gap/power/eye).

    Returns:
        Integer 0-100.
    """
    keys = PITCHER_CORE_KEYS if is_pitcher else OFFENSIVE_TOOL_KEYS
    vals = [v for k, v in tools.items() if k in keys and v]
    if len(vals) < 2:
        return 0
    spread = max(vals) - min(vals)
    return round(min(100.0, (spread / 60.0) * 100))


def specialist_label(score: int) -> str:
    """'Specialist' or 'Generalist' from a compute_specialist_score() value."""
    return "Specialist" if score >= SPECIALIST_SCORE_THRESHOLD else "Generalist"


# ---------------------------------------------------------------------------
# Tool transform
# ---------------------------------------------------------------------------

def tool_transform(val: float) -> float:
    """Apply non-linear piecewise transformation to a tool rating.

    Linear in the middle (40-60), with 1.5× penalty below 40 and 1.3× bonus
    above 60. Matches empirical marginal WAR data.

    Args:
        val: Tool value on the 20-80 scale.

    Returns:
        Transformed value on the 20-80 scale.
    """
    if val >= TOOL_TRANSFORM_HIGH_THRESHOLD:
        return TOOL_TRANSFORM_HIGH_THRESHOLD + (val - TOOL_TRANSFORM_HIGH_THRESHOLD) * TOOL_TRANSFORM_HIGH_BONUS
    elif val <= TOOL_TRANSFORM_LOW_THRESHOLD:
        return TOOL_TRANSFORM_LOW_THRESHOLD - (TOOL_TRANSFORM_LOW_THRESHOLD - val) * TOOL_TRANSFORM_LOW_PENALTY
    else:
        return float(val)


def sub_mlb_floor_penalty(tools: dict[str, float | int | None]) -> float:
    """Compute composite penalty for tools below the MLB floor (35).

    Players with tools below 35 underperform their OVR-predicted WAR.
    Penalty captures the nonlinear cost of a disqualifying weakness.

    Args:
        tools: Tool ratings on the 20-80 scale.

    Returns:
        Penalty as a positive float to subtract from the composite.
    """
    penalty = 0.0
    for val in tools.values():
        if val is not None and val < MLB_TOOL_FLOOR:
            penalty += (MLB_TOOL_FLOOR - val) * FLOOR_PENALTY_RATE
    return penalty


# ---------------------------------------------------------------------------
# Tool compensation
# ---------------------------------------------------------------------------

def _compensated_transform(val: float, compensators: list[tuple[float, float]]) -> float:
    """Transform a tool with compensation that pulls below-average toward 50.

    Applies tool_transform first, then pulls toward 50 proportionally to
    compensating tools' surplus above 50. Capped at 0.75 pull fraction.
    """
    transformed = tool_transform(val)
    deficit = 50.0 - transformed
    if deficit <= 0:
        return transformed

    pull_fraction = 0.0
    for comp_val, strength in compensators:
        if comp_val > 50.0:
            surplus = comp_val - 50.0
            pull_fraction += surplus * strength

    pull_fraction = min(pull_fraction, 0.75)
    return transformed + deficit * pull_fraction


def _apply_hitter_compensation(tools: dict[str, float | int | None]) -> dict[str, float | int | None]:
    """Apply compensation for hitter tools below average."""
    cnt = float(tools.get("contact") or 0)
    pow_ = float(tools.get("power") or 0)
    eye = float(tools.get("eye") or 0)

    effective: dict[str, float | int | None] = dict(tools)

    if pow_ < 50 and pow_ > 0:
        compensators = []
        if cnt > 50:
            compensators.append((cnt, 0.020))
        if eye > 50:
            compensators.append((eye, 0.012))
        if compensators:
            effective["_power_transformed"] = _compensated_transform(pow_, compensators)

    if eye < 50 and eye > 0:
        compensators = []
        if cnt > 50:
            compensators.append((cnt, 0.020))
        if compensators:
            effective["_eye_transformed"] = _compensated_transform(eye, compensators)

    return effective


def _apply_pitcher_compensation(tools: dict[str, float | int | None]) -> dict[str, float | int | None]:
    """Apply compensation for pitcher tools below average."""
    stf = float(tools.get("stuff") or 0)
    mov = float(tools.get("movement") or 0)
    ctrl = float(tools.get("control") or 0)

    effective: dict[str, float | int | None] = dict(tools)

    if stf < 50 and stf > 0:
        compensators = []
        if mov > 50:
            compensators.append((mov, 0.020))
        if ctrl > 50:
            compensators.append((ctrl, 0.012))
        if compensators:
            effective["_stuff_transformed"] = _compensated_transform(stf, compensators)

    if ctrl < 50 and ctrl > 0:
        compensators = []
        if stf > 50:
            compensators.append((stf, 0.018))
        if mov > 50:
            compensators.append((mov, 0.012))
        if compensators:
            effective["_control_transformed"] = _compensated_transform(ctrl, compensators)

    return effective


# ---------------------------------------------------------------------------
# Component raw scores (unclamped)
# ---------------------------------------------------------------------------

def offensive_grade_raw(
    tools: dict[str, float | int | None],
    weights: dict[str, float],
) -> Optional[float]:
    """Unclamped offensive weighted average with tool compensation."""
    effective = _apply_hitter_compensation(tools)
    _COMPENSATED_KEYS = {"power": "_power_transformed", "eye": "_eye_transformed"}

    available: list[tuple[float, float]] = []
    for key in OFFENSIVE_TOOL_KEYS:
        val = effective.get(key)
        w = weights.get(key, 0.0)
        if val is not None and w > 0:
            comp_key = _COMPENSATED_KEYS.get(key)
            comp_val = effective.get(comp_key) if comp_key else None
            if comp_val is not None:
                transformed = float(comp_val)
            else:
                transformed = tool_transform(float(val))
            available.append((transformed, w))

    if not available:
        return None

    total_weight = sum(w for _, w in available)
    if total_weight <= 0:
        return None

    return sum(val * (w / total_weight) for val, w in available)


def baserunning_value_raw(
    tools: dict[str, float | int | None],
    weights: dict[str, float],
) -> Optional[float]:
    """Unclamped baserunning weighted average."""
    available: list[tuple[float, float]] = []
    for key in BASERUNNING_TOOL_KEYS:
        val = tools.get(key)
        w = weights.get(key, 0.0)
        if val is not None and w > 0:
            available.append((float(val), w))

    if not available:
        return None

    total_weight = sum(w for _, w in available)
    if total_weight <= 0:
        return None

    return sum(val * (w / total_weight) for val, w in available)


def defensive_value_raw(
    defense: dict[str, float | int | None],
    def_weights: dict[str, float],
) -> Optional[float]:
    """Unclamped defensive weighted average."""
    available: list[tuple[float, float]] = []
    for key, w in def_weights.items():
        val = defense.get(key)
        if val is not None and w > 0:
            available.append((float(val), w))

    if not available:
        return None

    total_weight = sum(w for _, w in available)
    if total_weight <= 0:
        return None

    return sum(val * (w / total_weight) for val, w in available)


# ---------------------------------------------------------------------------
# Public component functions (clamped to 20-80)
# ---------------------------------------------------------------------------

def compute_offensive_grade(
    tools: dict[str, float | int | None],
    weights: dict[str, float],
) -> Optional[int]:
    """Compute offensive component from hitting tools only.

    Uses contact, gap, power, eye with piecewise tool transform and
    calibrated weights. Returns integer on 20-80 scale.
    """
    raw = offensive_grade_raw(tools, weights)
    if raw is None:
        return None
    return max(20, min(80, round(raw)))


def compute_baserunning_value(
    tools: dict[str, float | int | None],
    weights: dict[str, float],
) -> Optional[int]:
    """Compute baserunning component from speed/steal tools.

    Returns integer on 20-80 scale.
    """
    raw = baserunning_value_raw(tools, weights)
    if raw is None:
        return None
    return max(20, min(80, round(raw)))


def compute_defensive_value(
    defense: dict[str, float | int | None],
    def_weights: dict[str, float],
) -> Optional[int]:
    """Compute defensive component from positional defensive tools.

    Returns integer on 20-80 scale.
    """
    raw = defensive_value_raw(defense, def_weights)
    if raw is None:
        return None
    return max(20, min(80, round(raw)))


# ---------------------------------------------------------------------------
# Hitter composite
# ---------------------------------------------------------------------------

def compute_composite_hitter(
    tools: dict[str, float | int | None],
    weights: dict[str, float],
    defense: dict[str, float | int | None],
    def_weights: dict[str, float],
) -> int:
    """Compute hitter Composite_Score from tool ratings and weights.

    Decomposes into offensive, baserunning, and defensive components,
    then recombines using shares derived from the weight profile.

    Args:
        tools: Hitter tool ratings on 20-80 scale (contact, gap, power, eye,
            speed, steal, stl_rt). None values are skipped with weight renorm.
        weights: Positional weight profile summing to ~1.0.
        defense: Defensive tool ratings (IFR, OFR, CArm, etc.).
        def_weights: Positional defensive importance weights.

    Returns:
        Integer composite score in [20, 80].
    """
    off_raw = offensive_grade_raw(tools, weights)
    br_raw = baserunning_value_raw(tools, weights)
    def_raw = defensive_value_raw(defense, def_weights)

    if off_raw is None and br_raw is None and def_raw is None:
        return 20

    # Derive recombination shares from weight profile
    defense_weight = weights.get("defense", 0.0)
    off_w = sum(weights.get(k, 0.0) for k in OFFENSIVE_TOOL_KEYS)
    br_w = sum(weights.get(k, 0.0) for k in BASERUNNING_TOOL_KEYS)
    tool_w_total = off_w + br_w

    if tool_w_total > 0:
        offense_share = off_w / tool_w_total * (1.0 - defense_weight)
        baserunning_share = br_w / tool_w_total * (1.0 - defense_weight)
    else:
        offense_share = 1.0 - defense_weight
        baserunning_share = 0.0

    # Contact-scaled baserunning boost
    cnt = float(tools.get("contact") or 0)
    if cnt > 50 and baserunning_share > 0:
        br_boost_factor = min(1.0, (cnt - 50.0) / 30.0)
        br_addition = baserunning_share * br_boost_factor
        baserunning_share += br_addition
        offense_share -= br_addition

    # Elite defense boost
    if def_raw is not None and defense_weight > 0 and defense:
        primary_def = max((v for v in defense.values() if v is not None), default=0)
        if primary_def > 50:
            def_boost_factor = min(1.0, (primary_def - 50.0) / 30.0)
            def_addition = defense_weight * def_boost_factor
            defense_weight += def_addition
            offense_share -= def_addition

    # Recombine
    raw = 0.0
    if off_raw is not None:
        raw += off_raw * offense_share
    if br_raw is not None:
        raw += br_raw * baserunning_share
    if def_raw is not None:
        raw += def_raw * defense_weight

    # Sub-MLB floor penalty (offensive tools only)
    hitting_tools = {k: v for k, v in tools.items() if k in OFFENSIVE_TOOL_KEYS}
    raw -= sub_mlb_floor_penalty(hitting_tools)

    # Speed × contact synergy: fast players with good contact produce more
    # value than the linear sum suggests (infield hits, extra bases, pressure).
    # Additive bonus when both speed and contact exceed thresholds.
    spd = float(tools.get("speed") or 0)
    if spd > 45 and cnt > 50:
        synergy = 0.10 * (spd - 45) * (cnt / 60.0)
        raw += synergy

    # Tool imbalance penalty for one-tool hitters
    _hit_vals = [v for v in hitting_tools.values() if v]
    if len(_hit_vals) >= 3:
        tool_spread = max(_hit_vals) - min(_hit_vals)
        if tool_spread > HITTER_IMBALANCE_SPREAD_THRESHOLD:
            spread_penalty = (tool_spread - HITTER_IMBALANCE_SPREAD_THRESHOLD) * HITTER_IMBALANCE_SPREAD_RATE
            weakness_penalty = sum(
                max(0, HITTER_IMBALANCE_WEAKNESS_THRESHOLD - v) * HITTER_IMBALANCE_WEAKNESS_RATE
                for v in _hit_vals if v < HITTER_IMBALANCE_WEAKNESS_THRESHOLD
            )
            raw -= spread_penalty + weakness_penalty

    return max(20, min(80, round(raw)))


# ---------------------------------------------------------------------------
# Pitcher composite
# ---------------------------------------------------------------------------

def compute_composite_pitcher(
    tools: dict[str, float | int | None],
    weights: dict[str, float],
    arsenal: dict[str, float | int],
    stamina: float | int,
    role: str,
) -> int:
    """Compute pitcher Composite_Score from tools, arsenal, and role.

    Args:
        tools: Pitcher tool ratings (stuff, movement, control) on 20-80 scale.
        weights: Pitcher weight profile (stuff, movement, control, arsenal).
        arsenal: Pitch arsenal {pitch_name: rating}.
        stamina: Stamina rating on 20-80 scale.
        role: "SP" or "RP".

    Returns:
        Integer composite score in [20, 80].
    """
    arsenal_weight = weights.get("arsenal", 0.0)

    # Apply compensation
    effective_tools = _apply_pitcher_compensation(tools)
    _COMPENSATED_KEYS = {"stuff": "_stuff_transformed", "control": "_control_transformed"}

    available: list[tuple[float, float]] = []
    for key in PITCHER_TOOL_KEYS:
        val = effective_tools.get(key)
        w = weights.get(key, 0.0)
        if val is not None and w > 0:
            comp_key = _COMPENSATED_KEYS.get(key)
            comp_val = effective_tools.get(comp_key) if comp_key else None
            if comp_val is not None:
                transformed = float(comp_val)
            else:
                transformed = tool_transform(float(val))
            available.append((transformed, w))

    if not available:
        return 20

    total_tool_weight = sum(w for _, w in available)
    tool_share = 1.0 - arsenal_weight

    if total_tool_weight > 0:
        tool_sum = sum(
            val * (w / total_tool_weight) * tool_share
            for val, w in available
        )
    else:
        tool_sum = 0.0

    # Arsenal depth bonus
    pitches_45_plus = sum(1 for r in arsenal.values() if r >= 45)
    depth_bonus = min(3, max(0, pitches_45_plus - 3))

    # Top-pitch quality bonus
    best_pitch = max(arsenal.values()) if arsenal else 0
    if best_pitch >= 70:
        quality_bonus = 2
    elif best_pitch >= 65:
        quality_bonus = 1
    else:
        quality_bonus = 0

    arsenal_score = 50.0 + (depth_bonus + quality_bonus) * 5.0
    arsenal_score = max(20.0, min(80.0, arsenal_score))

    raw = tool_sum + arsenal_score * arsenal_weight

    # Stamina penalty for SP
    if role == "SP" and stamina < 40:
        penalty = min(5.0, (40 - stamina) * 0.15)
        raw -= penalty

    # SP innings-volume adjustment
    if role == "SP" and stamina > 45:
        bonus = min(4.0, (stamina - 45) * 0.12)
        raw += bonus

    # Platoon balance penalty
    stuff_l = tools.get("stuff_l")
    stuff_r = tools.get("stuff_r")
    if stuff_l is not None and stuff_r is not None:
        weak_side = min(stuff_l, stuff_r)
        gap = abs(stuff_l - stuff_r)
        if weak_side < 35 and gap >= 15:
            raw -= 3 if weak_side <= 25 else 2

    # Sub-MLB floor penalty for core tools
    core_tools = {k: v for k, v in tools.items() if k in PITCHER_CORE_KEYS}
    raw -= sub_mlb_floor_penalty(core_tools)

    # Tool imbalance penalty for one-dimensional pitchers
    _core_vals = [v for k, v in tools.items() if k in PITCHER_CORE_KEYS and v]
    if len(_core_vals) >= 2:
        tool_spread = max(_core_vals) - min(_core_vals)
        if tool_spread > PITCHER_IMBALANCE_SPREAD_THRESHOLD:
            spread_penalty = (tool_spread - PITCHER_IMBALANCE_SPREAD_THRESHOLD) * PITCHER_IMBALANCE_SPREAD_RATE
            weakness_penalty = sum(
                max(0, PITCHER_IMBALANCE_WEAKNESS_THRESHOLD - v) * PITCHER_IMBALANCE_WEAKNESS_RATE
                for v in _core_vals if v < PITCHER_IMBALANCE_WEAKNESS_THRESHOLD
            )
            raw -= spread_penalty + weakness_penalty

    return max(20, min(80, round(raw)))


# ---------------------------------------------------------------------------
# Tool-only score
# ---------------------------------------------------------------------------

def compute_tool_only_score(
    player_type: str,
    tools: dict[str, float | int | None],
    weights: dict[str, float],
    defense: Optional[dict[str, float | int | None]] = None,
    def_weights: Optional[dict[str, float]] = None,
    arsenal: Optional[dict[str, float | int]] = None,
    stamina: int = 50,
    role: str = "SP",
) -> int:
    """Compute the pre-stat-blend score for a player.

    Delegates to compute_composite_hitter or compute_composite_pitcher.
    """
    if player_type == "hitter":
        return compute_composite_hitter(tools, weights, defense or {}, def_weights or {})
    else:
        return compute_composite_pitcher(tools, weights, arsenal or {}, stamina, role)


# ---------------------------------------------------------------------------
# MLB stat blending
# ---------------------------------------------------------------------------

def compute_composite_mlb(
    tool_score: int,
    stat_seasons: list[float],
    peak_age: int = 28,
    player_age: int = 28,
    is_pitcher: bool = False,
    bucket: str = "",
) -> int:
    """Blend tool-based score with stat performance for MLB players.

    Args:
        tool_score: Pre-blend tool-only score (20-80).
        stat_seasons: Normalized stat values on 20-80 scale (most recent first).
        peak_age: Expected peak age for position.
        player_age: Player's current age.
        is_pitcher: Whether the player is a pitcher.
        bucket: Positional bucket (SS, CF get reduced blend because OPS+
            doesn't capture their defensive WAR contribution).

    Returns:
        Integer composite score in [20, 80].
    """
    if not stat_seasons:
        return tool_score

    # Recency weighting
    recency_weights = [3.0, 2.0, 1.0]
    weighted_sum = 0.0
    total_weight = 0.0
    for i, stat_val in enumerate(stat_seasons[:3]):
        w = recency_weights[i] if i < len(recency_weights) else 1.0
        weighted_sum += stat_val * w
        total_weight += w

    stat_signal = weighted_sum / total_weight if total_weight > 0 else tool_score

    # Blend weight based on seasons available
    seasons_available = min(len(stat_seasons), 3)
    blend_weight = {1: 0.20, 2: 0.35, 3: 0.60}[seasons_available]

    # Defense-first position adjustment: OPS+ doesn't capture defensive WAR,
    # so stat blending at these positions pulls the composite away from the
    # defensive value that actually drives their production. Reduce blend.
    _DEFENSE_POSITIONS = {"SS": 0.50, "CF": 0.50, "2B": 0.75, "C": 0.75}
    if bucket in _DEFENSE_POSITIONS:
        blend_weight *= _DEFENSE_POSITIONS[bucket]

    # Young player adjustment
    if player_age < peak_age and tool_score > stat_signal:
        age_factor = max(0.3, 1.0 - (peak_age - player_age) * 0.1)
        blend_weight *= age_factor

    composite = tool_score * (1.0 - blend_weight) + stat_signal * blend_weight
    return max(20, min(80, round(composite)))


# ---------------------------------------------------------------------------
# Stat conversion
# ---------------------------------------------------------------------------

def stat_to_2080(stat_plus: float) -> float:
    """Convert a league-normalized rate stat (OPS+) to the 20-80 scale.

    Formula: 20 + (stat_plus / 200) * 60, clamped to [20, 80].
    """
    raw = 20.0 + (stat_plus / 200.0) * 60.0
    return max(20.0, min(80.0, raw))


def pitcher_stat_to_2080(stat_plus: float) -> float:
    """Convert inverted FIP- to 20-80 scale with asymmetric mapping.

    Above-average (stat_plus > 100): steeper slope (0.45/point) rewards excellence.
    Below-average (stat_plus < 100): standard slope (0.30/point) avoids over-penalizing.
    """
    if stat_plus >= 100:
        raw = 50.0 + (stat_plus - 100.0) * 0.45
    else:
        raw = 50.0 + (stat_plus - 100.0) * 0.30
    return max(20.0, min(80.0, raw))


def compute_combined_value(primary_composite: int, secondary_composite: int) -> int:
    """Compute the combined value for a two-way player.

    Formula: ``primary + min(8, max(0, (secondary - 35) * 0.3))``

    The secondary bonus reflects partial additional value from the secondary
    role. Only applies when the secondary score exceeds replacement level (35).
    Capped at +8 to prevent unrealistically high combined scores.

    Args:
        primary_composite: The higher of the two role scores (20-80).
        secondary_composite: The lower of the two role scores (20-80).

    Returns:
        Combined value as an integer. Always >= primary_composite.
    """
    secondary_bonus = min(8, max(0, (secondary_composite - 35) * 0.3))
    return min(80, round(primary_composite + secondary_bonus))


def defensive_score(
    p: dict[str, Any],
    bucket: str,
    scale: str,
    defensive_weights: dict[str, dict[str, float]] | None = None,
) -> float:
    """Compute weighted defensive score on 20-80 scale for a position bucket.

    Args:
        p: Player dict with defensive tool ratings (IFR, IFA, IFE, TDP, OFR, etc.).
        bucket: Positional bucket (C, SS, 2B, 3B, CF, COF, 1B, SP, RP).
        scale: Ratings scale string ("1-100", "20-80").
        defensive_weights: Weight dicts per position. If None, uses defaults.

    Returns:
        Weighted defensive score. 0.0 if bucket has no defensive component.
    """
    from statsplusplus.config.ratings import norm as _norm
    from statsplusplus.evaluation.constants import DEFENSIVE_WEIGHTS

    if defensive_weights is None:
        defensive_weights = DEFENSIVE_WEIGHTS

    def _n(val: Any) -> float:
        return float(_norm(val, scale) or 0)

    if bucket == "COF":
        lf_weights = defensive_weights.get("COF_LF", {})
        rf_weights = defensive_weights.get("COF_RF", {})
        lf = sum(_n(p.get(f, 0) or 0) * w for f, w in lf_weights.items())
        rf = sum(_n(p.get(f, 0) or 0) * w for f, w in rf_weights.items())
        return max(lf, rf)

    weights = defensive_weights.get(bucket)
    if not weights:
        return 0.0
    return sum(_n(p.get(f, 0) or 0) * w for f, w in weights.items())
