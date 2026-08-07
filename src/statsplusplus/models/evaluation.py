"""Evaluation result and context models.

Types for the evaluation engine's inputs and outputs. The evaluation engine
operates on these types exclusively — no raw dicts, no DB rows.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from statsplusplus.models.player import PositionalBucket


@dataclass(slots=True)
class CompositeWeights:
    """Per-position tool weight configuration for composite scoring.

    Loaded from tool_weights.json (calibrated) or DEFAULT_TOOL_WEIGHTS (fallback).
    """

    contact: float = 0.0
    gap: float = 0.0
    power: float = 0.0
    eye: float = 0.0
    speed: float = 0.0
    steal: float = 0.0
    steal_rate: float = 0.0
    defense: float = 0.0
    # Pitcher weights
    stuff: float = 0.0
    movement: float = 0.0
    control: float = 0.0
    arsenal: float = 0.0


@dataclass(slots=True)
class RecombinationWeights:
    """Offense/defense/baserunning recombination weights for composite."""

    offense: float = 0.90
    defense: float = 0.05
    baserunning: float = 0.05


@dataclass(slots=True)
class StatSeason:
    """A single season of stat history for WAR projection."""

    year: int
    war: float
    season_pct: float = 1.0  # Fraction of season completed (0-1)
    incomplete: bool = False  # Mid-season trade (only partial with one team)
    is_sp: bool = False  # Pitcher: was this a starting role?
    ab: int = 0
    ip: float = 0.0
    gs: int = 0


@dataclass(slots=True)
class EvaluationContext:
    """Context required for FV calculation beyond raw ratings.

    This packages the "extra state" that calc_fv needs: age-for-level context,
    evaluation settings, and pre-computed scores from the evaluation engine.
    """

    bucket: PositionalBucket
    norm_age: int  # Expected age at this level for on-track prospect
    level: str  # Level string key (e.g., "aaa", "aa", "a")
    is_pitcher: bool = False

    # Pre-computed evaluation engine outputs
    composite_score: int = 0
    ceiling_score: int = 0
    offensive_grade: Optional[int] = None
    offensive_ceiling: Optional[int] = None
    defensive_value: Optional[int] = None
    durability_score: Optional[int] = None

    # MiLB stat context (when available)
    stat_risk_modifier: float = 0.0

    # Ratings scale for this league
    ratings_scale: str = "1-100"


@dataclass(slots=True)
class EvaluationResult:
    """Complete output from evaluating a single player.

    Replaces the existing EvaluationResult in evaluation_engine.py with
    a cleaner interface. The old one is preserved during migration.
    """

    player_id: int
    composite_score: int  # 20-80, current ability (stat-blended for MLB)
    ceiling_score: int  # 20-80, projected peak
    tool_only_score: int  # 20-80, pre-stat-blend composite

    # Component scores
    offensive_grade: Optional[int] = None
    baserunning_value: Optional[int] = None
    defensive_value: Optional[int] = None
    durability_score: Optional[int] = None

    # Component ceilings
    offensive_ceiling: Optional[int] = None
    baserunning_ceiling: Optional[int] = None
    defensive_ceiling: Optional[int] = None

    # Two-way
    secondary_composite: Optional[int] = None
    secondary_ceiling: Optional[int] = None
    is_two_way: bool = False
    combined_value: Optional[int] = None

    # Classification
    archetype: str = ""
    carrying_tools: list[str] = field(default_factory=list)
    red_flag_tools: list[str] = field(default_factory=list)

    # Metadata
    confidence: str = "full"  # "full" or "partial"
    divergence: Optional[dict[str, Any]] = None

    # Carrying tool context
    carrying_tool_bonus: float = 0.0
    carrying_tool_breakdown: list[dict[str, Any]] = field(default_factory=list)
    ceiling_carrying_tool_bonus: float = 0.0
    ceiling_carrying_tool_breakdown: list[dict[str, Any]] = field(default_factory=list)

    # Positional context
    positional_percentile: Optional[float] = None
    positional_median: Optional[int] = None
