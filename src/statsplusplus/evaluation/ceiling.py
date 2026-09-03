"""Ceiling score computation.

Pure functions for computing a player's ceiling (projected peak ability)
from potential tool ratings. No DB access, no global state.

Public API:
    compute_ceiling(potential_tools, weights, composite, ...) -> int
    compute_true_ceiling(potential_tools, weights, composite, ...) -> int
    compute_component_ceilings(potential_tools, weights, current_components, ...) -> dict
"""

from __future__ import annotations

from typing import Any, Optional

from statsplusplus.evaluation.composite import (
    compute_composite_hitter,
    compute_composite_pitcher,
    compute_offensive_grade,
    compute_baserunning_value,
    compute_defensive_value,
)


# ---------------------------------------------------------------------------
# Age-weighted potential blend
# ---------------------------------------------------------------------------

def _potential_weight(age: int) -> float:
    """Compute the potential-vs-current weight based on age.

    Younger players weight potential tools more heavily (reflecting upside),
    while veterans weight current composite more heavily.

    Ramps from 0.95 (age 16) to 0.30 (age 30+).
    """
    return max(0.30, min(0.95, 1.0 - (age - 16) * 0.05))


# ---------------------------------------------------------------------------
# Ceiling score
# ---------------------------------------------------------------------------

def compute_ceiling(
    potential_tools: dict[str, float | int | None],
    weights: dict[str, float],
    composite_score: int,
    accuracy: str = "A",
    work_ethic: str = "N",
    defense: Optional[dict[str, float | int | None]] = None,
    def_weights: Optional[dict[str, float]] = None,
    is_pitcher: bool = False,
    arsenal: Optional[dict[str, float | int]] = None,
    stamina: int = 50,
    role: str = "SP",
    age: int = 25,
    ratings_scale: str = "1-100",
    transforms: dict[str, list[float]] | None = None,
) -> int:
    """Compute Ceiling_Score from potential tool ratings.

    Uses the same positional weight formula as the composite, applied to
    potential ratings instead of current. The ceiling is age-weighted:
    younger players weight potential more heavily.

    Args:
        potential_tools: Potential tool ratings (20-80 scale).
        weights: Positional weight profile (same as composite).
        composite_score: Player's current Composite_Score (floor).
        accuracy: Scouting accuracy ("A" normal, "L" low).
        work_ethic: Work ethic code ("H"/"VH" high, "L" low, "N" normal).
        defense: Defensive potential tool ratings (hitters only).
        def_weights: Defensive weight profile (hitters only).
        is_pitcher: Whether the player is a pitcher.
        arsenal: Pitch arsenal dict (pitchers only).
        stamina: Stamina rating (pitchers only).
        role: Pitcher role "SP" or "RP" (pitchers only).
        age: Player's current age (for age-weighted blend).
        ratings_scale: League ratings scale for peak bonus cap.

    Returns:
        Integer ceiling score in [20, 80], never below composite_score.
    """
    # Compute raw potential composite
    if is_pitcher:
        raw_ceiling = compute_composite_pitcher(
            potential_tools, weights, arsenal or {}, stamina, role, transforms,
        )
    else:
        raw_ceiling = compute_composite_hitter(
            potential_tools, weights, defense or {}, def_weights or {}, transforms,
        )

    # Peak tool bonus: rewards uneven profiles with elite carrying tools.
    # +1 per potential tool point above 60, capped.
    if is_pitcher:
        ceiling_tools = [
            potential_tools.get(k) or 0
            for k in ("stuff", "movement", "control")
        ]
        if role == "SP" and stamina >= 55:
            ceiling_tools.append(min(stamina, 65))
    else:
        ceiling_tools = [
            potential_tools.get(k) or 0
            for k in ("contact", "gap", "power", "eye")
        ]

    peak_bonus = sum(max(0, int(t) - 60) for t in ceiling_tools)
    # Scale-aware cap: 1-100 scale normalizes higher so bonus accumulates faster
    peak_cap = 10 if ratings_scale == "1-100" else 15
    raw_ceiling += min(peak_bonus, peak_cap)

    # Age-weighted blend
    pot_weight = _potential_weight(age)
    raw_ceiling = round(raw_ceiling * pot_weight + composite_score * (1.0 - pot_weight))

    # Floor: never below composite
    raw_ceiling = max(raw_ceiling, composite_score)

    return max(20, min(80, raw_ceiling))


def compute_true_ceiling(
    potential_tools: dict[str, float | int | None],
    weights: dict[str, float],
    composite_score: int,
    accuracy: str = "A",
    work_ethic: str = "N",
    defense: Optional[dict[str, float | int | None]] = None,
    def_weights: Optional[dict[str, float]] = None,
    is_pitcher: bool = False,
    arsenal: Optional[dict[str, float | int]] = None,
    stamina: int = 50,
    role: str = "SP",
    transforms: dict[str, list[float]] | None = None,
) -> int:
    """Compute the true ceiling from potential tools with no age blend.

    Pure potential-driven score: what happens if every tool reaches its
    potential rating. No peak tool bonus (already reflected in uncompressed
    potential composite). No age-weighted blend.

    Use compute_ceiling() for the age-blended projected score.

    Returns:
        Integer ceiling score in [20, 80], never below composite_score.
    """
    if is_pitcher:
        raw = compute_composite_pitcher(
            potential_tools, weights, arsenal or {}, stamina, role, transforms,
        )
    else:
        raw = compute_composite_hitter(
            potential_tools, weights, defense or {}, def_weights or {}, transforms,
        )

    # Floor: never below composite
    raw = max(raw, composite_score)
    return max(20, min(80, raw))


def compute_component_ceilings(
    potential_tools: dict[str, float | int | None],
    weights: dict[str, float],
    current_components: dict[str, Optional[int]],
    defense: Optional[dict[str, float | int | None]] = None,
    def_weights: Optional[dict[str, float]] = None,
    is_pitcher: bool = False,
    arsenal: Optional[dict[str, float | int]] = None,
    stamina: int = 50,
    role: str = "SP",
    age: int = 25,
    ct_config: Optional[dict[str, Any]] = None,
    position: str = "",
    transforms: dict[str, list[float]] | None = None,
) -> dict[str, Any]:
    """Compute component-level ceilings from potential tool ratings.

    Applies the same component formulas to potential tools, with age-weighted
    blend per-component (each floored at its current value).

    When ct_config is provided with a non-empty position, carrying tool bonus
    is computed on potential offensive tools and added to the offensive ceiling.

    Args:
        potential_tools: Potential tool ratings (20-80 scale).
        weights: Positional weight profile.
        current_components: Current component scores (offensive_grade,
            baserunning_value, defensive_value, durability_score).
        defense: Defensive potential tool ratings (hitters only).
        def_weights: Defensive weight profile (hitters only).
        is_pitcher: Whether the player is a pitcher.
        arsenal: Pitch arsenal dict (pitchers only).
        stamina: Stamina rating (pitchers only).
        role: "SP" or "RP".
        age: Player's current age.
        ct_config: Carrying tool config dict (optional).
        position: Position bucket for carrying tool bonus.

    Returns:
        Dict with offensive_ceiling, baserunning_ceiling, defensive_ceiling,
        ceiling_carrying_tool_bonus, ceiling_carrying_tool_breakdown.
    """
    pot_weight = _potential_weight(age)

    result: dict[str, Any] = {
        "offensive_ceiling": None,
        "baserunning_ceiling": None,
        "defensive_ceiling": None,
        "ceiling_carrying_tool_bonus": 0.0,
        "ceiling_carrying_tool_breakdown": [],
    }

    if is_pitcher:
        raw_pitching = compute_composite_pitcher(
            potential_tools, weights, arsenal or {}, stamina, role, transforms,
        )
        current_off = current_components.get("offensive_grade")
        if current_off is not None:
            blended = round(raw_pitching * pot_weight + current_off * (1.0 - pot_weight))
            blended = max(blended, current_off)
            result["offensive_ceiling"] = max(20, min(80, blended))
        else:
            result["offensive_ceiling"] = max(20, min(80, raw_pitching))
    else:
        raw_offensive = compute_offensive_grade(potential_tools, weights, transforms)
        raw_baserunning = compute_baserunning_value(potential_tools, weights)
        raw_defensive = compute_defensive_value(defense or {}, def_weights or {})

        # Carrying tool bonus on potential offensive ceiling
        ceiling_ct_bonus = 0.0
        ceiling_ct_breakdown: list[dict[str, Any]] = []
        if raw_offensive is not None and ct_config and position:
            from statsplusplus.evaluation.carrying_tools import compute_carrying_tool_bonus
            ceiling_ct_bonus, ceiling_ct_breakdown = compute_carrying_tool_bonus(
                potential_tools, position, ct_config,
            )

        raw_off_adjusted: float = float(raw_offensive) + ceiling_ct_bonus if raw_offensive is not None else 0.0

        result["ceiling_carrying_tool_bonus"] = ceiling_ct_bonus
        result["ceiling_carrying_tool_breakdown"] = ceiling_ct_breakdown

        current_off = current_components.get("offensive_grade")
        current_br = current_components.get("baserunning_value")
        current_def = current_components.get("defensive_value")

        # Offensive ceiling
        if raw_offensive is not None:
            if current_off is not None:
                blended = round(raw_off_adjusted * pot_weight + current_off * (1.0 - pot_weight))
                blended = max(blended, current_off)
                result["offensive_ceiling"] = max(20, min(80, blended))
            else:
                result["offensive_ceiling"] = max(20, min(80, round(raw_off_adjusted)))

        # Baserunning ceiling
        if raw_baserunning is not None:
            if current_br is not None:
                blended = round(raw_baserunning * pot_weight + current_br * (1.0 - pot_weight))
                blended = max(blended, current_br)
                result["baserunning_ceiling"] = max(20, min(80, blended))
            else:
                result["baserunning_ceiling"] = max(20, min(80, raw_baserunning))

        # Defensive ceiling
        if raw_defensive is not None:
            if current_def is not None:
                blended = round(raw_defensive * pot_weight + current_def * (1.0 - pot_weight))
                blended = max(blended, current_def)
                result["defensive_ceiling"] = max(20, min(80, blended))
            else:
                result["defensive_ceiling"] = max(20, min(80, raw_defensive))

    return result
