"""Rating normalization — pure functions.

Converts raw OOTP ratings to the 20-80 scouting scale. Unlike the legacy
module (scripts/ratings.py), these functions accept the scale as a parameter
rather than reading from a module-level global. This makes them thread-safe
and testable without setup.

Public API:
    norm(raw, scale) -> int | None
    norm_continuous(raw, scale) -> float | None
    norm_floor(raw, scale, floor) -> int
"""

from __future__ import annotations

from typing import Any, Optional


def norm(raw: Any, scale: str = "1-100") -> Optional[int]:
    """Normalize a tool rating to 20-80 scouting scale, rounded to nearest 5.

    Args:
        raw: Raw rating value (int, str, or None).
        scale: Rating scale — "1-100", "20-80", or "1-20".

    Returns:
        Normalized grade (20-80 in steps of 5), or None for invalid input.
    """
    if raw is None:
        return None
    try:
        raw_int = int(raw)
    except (ValueError, TypeError):
        return None
    if raw_int <= 0:
        return None

    if scale == "20-80":
        return int(max(20, min(80, round(raw_int / 5) * 5)))
    if scale == "1-20":
        val = 20.0 + (min(raw_int, 20) - 1) / 19.0 * 60.0
        return int(max(20, min(80, round(val / 5) * 5)))
    # 1-100 (default)
    return int(round((20 + (min(raw_int, 100) / 100) * 60) / 5) * 5)


def norm_continuous(raw: Any, scale: str = "1-100") -> Optional[float]:
    """Normalize a tool rating to continuous 20-80 scale WITHOUT rounding.

    Preserves full granularity for evaluation engine calculations.

    Args:
        raw: Raw rating value (int, str, or None).
        scale: Rating scale — "1-100", "20-80", or "1-20".

    Returns:
        Continuous value on 20.0-80.0, or None for invalid input.
    """
    if raw is None:
        return None
    try:
        raw_int = int(raw)
    except (ValueError, TypeError):
        return None
    if raw_int <= 0:
        return None

    if scale == "20-80":
        return float(max(20, min(80, raw_int)))
    if scale == "1-20":
        return float(20.0 + (min(raw_int, 20) - 1) / 19.0 * 60.0)
    # 1-100
    return float(20.0 + (min(raw_int, 100) / 100.0) * 60.0)


def norm_floor(raw: Any, scale: str = "1-100", floor: int = 20) -> int:
    """norm() with a numeric fallback for call sites requiring a number.

    Use when the result feeds a comparison or numeric operation.

    Args:
        raw: Raw rating value.
        scale: Rating scale.
        floor: Value to return if norm() returns None.

    Returns:
        Normalized grade or floor value.
    """
    return norm(raw, scale) or floor
