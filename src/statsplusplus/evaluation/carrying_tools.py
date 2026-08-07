"""Carrying tool bonus computation.

Pure functions for computing the additive carrying tool bonus based on
positional context. A "carrying tool" is an elite offensive tool at a
position where that tool has outsized WAR impact (e.g., power at SS).

Public API:
    compute_carrying_tool_bonus(tools, position, config) -> tuple[float, list[dict]]
    tool_scarcity_multiplier(tool_grade, schedule) -> float
"""

from __future__ import annotations

from typing import Any

# Only offensive tools can qualify for the carrying tool bonus.
CARRYING_TOOL_ELIGIBLE: frozenset[str] = frozenset({"contact", "gap", "power", "eye"})

# Default minimum tool grade to qualify.
CARRYING_TOOL_GRADE_THRESHOLD: int = 65


def tool_scarcity_multiplier(tool_grade: int | float, schedule: list[dict[str, Any]]) -> float:
    """Compute the scarcity multiplier for a tool grade via linear interpolation.

    The schedule is a sorted list of {"threshold": int, "multiplier": float}
    breakpoints. Grades between breakpoints are linearly interpolated.

    Args:
        tool_grade: Tool rating on the 20-80 scale.
        schedule: List of breakpoints with threshold and multiplier keys.

    Returns:
        Scarcity multiplier (higher for rarer tool grades).
    """
    if not schedule:
        return 1.0

    if tool_grade <= schedule[0]["threshold"]:
        return float(schedule[0]["multiplier"])

    if tool_grade >= schedule[-1]["threshold"]:
        return float(schedule[-1]["multiplier"])

    for i in range(len(schedule) - 1):
        lo = schedule[i]
        hi = schedule[i + 1]
        lo_thresh: float = float(lo["threshold"])
        hi_thresh: float = float(hi["threshold"])
        lo_mult: float = float(lo["multiplier"])
        hi_mult: float = float(hi["multiplier"])
        if lo_thresh <= tool_grade <= hi_thresh:
            span = hi_thresh - lo_thresh
            if span == 0:
                return lo_mult
            frac = (tool_grade - lo_thresh) / span
            return lo_mult + frac * (hi_mult - lo_mult)

    return float(schedule[-1]["multiplier"])


def compute_carrying_tool_bonus(
    tools: dict[str, float | int | None],
    position: str,
    config: dict[str, Any],
) -> tuple[float, list[dict[str, Any]]]:
    """Compute the additive carrying tool bonus for a hitter.

    For each offensive tool grading above the position-specific threshold,
    checks if the tool/position combination is defined as a carrying tool
    in the config. If so, computes:

        bonus = war_premium_factor × (tool_grade − threshold) × scarcity_multiplier

    Args:
        tools: Tool ratings dict (contact, gap, power, eye). None values skipped.
        position: Position bucket (e.g., "SS", "C", "CF").
        config: Carrying tool config dict with "positions" and "scarcity_schedule".

    Returns:
        (total_bonus, breakdown) where total_bonus is the sum of individual
        tool bonuses and breakdown is a list of dicts with "tool", "grade", "bonus".
    """
    positions = config.get("positions", {})
    pos_data = positions.get(position)
    if pos_data is None:
        return 0.0, []

    carrying_tools_cfg = pos_data.get("carrying_tools", {})
    schedule = config.get("scarcity_schedule", [])

    total_bonus = 0.0
    breakdown: list[dict[str, Any]] = []

    for tool_name in CARRYING_TOOL_ELIGIBLE:
        grade = tools.get(tool_name)
        if grade is None:
            continue

        tool_cfg = carrying_tools_cfg.get(tool_name)
        if tool_cfg is None:
            continue

        # Use calibrated threshold if available, else default
        threshold = tool_cfg.get("_calibration", {}).get(
            "threshold", CARRYING_TOOL_GRADE_THRESHOLD
        )
        if grade < threshold:
            continue

        wpf = tool_cfg.get("war_premium_factor", 0.0)
        scarcity = tool_scarcity_multiplier(int(grade), schedule)
        bonus = wpf * (grade - threshold) * scarcity

        if bonus > 0:
            total_bonus += bonus
            breakdown.append({"tool": tool_name, "grade": int(grade), "bonus": bonus})

    return total_bonus, breakdown
