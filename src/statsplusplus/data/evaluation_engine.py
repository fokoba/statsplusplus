"""
evaluation_engine.py — Batch player evaluation pipeline.

Computes Composite_Score, Ceiling_Score, and Tool_Only_Score for every player
from individual tool ratings, replacing the system's dependency on OOTP's
OVR/POT ratings.

Pure computation functions (composite, ceiling, stat conversion) are defined
in ``statsplusplus.evaluation.composite`` and ``statsplusplus.evaluation.ceiling``
and re-exported here for convenience. This module adds:

- Batch ``run()`` pipeline (DB reads/writes)
- Tool weight and carrying tool config loading
- Two-way player detection and scoring
- Archetype classification, divergence detection
- Positional median/percentile computation

Public API (re-exported from evaluation package):
    compute_composite_hitter, compute_composite_pitcher,
    compute_tool_only_score, compute_composite_mlb,
    compute_ceiling, compute_true_ceiling, compute_component_ceilings,
    compute_offensive_grade, compute_baserunning_value, compute_defensive_value,
    compute_combined_value, stat_to_2080, pitcher_stat_to_2080

Pipeline-specific:
    run(league_dir, conn) — batch evaluation with DB I/O
    load_tool_weights(league_dir) -> dict
    validate_tool_weights(weights) -> bool
    detect_divergence, classify_archetype, identify_carrying_tools
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import statistics
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Project root on path for statsplus.client (used indirectly via refresh)
_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent.parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

log = logging.getLogger("statspp.evaluation_engine")


# ---------------------------------------------------------------------------
# Stat-blend staleness handling
# ---------------------------------------------------------------------------
# A qualifying season this many years old or more is dropped from the stat
# blend entirely — production that far in the past does not describe a player's
# current ability (e.g. a 39-year-old whose newest qualifying season is 5 years
# old should be evaluated on his current tools, not stale results).
STAT_STALE_DROP_YEARS: int = 4
# Within the retained window, each year of age shrinks the season's deviation
# from league-average (50 on the 20-80 scale) by this fraction, so recent
# seasons dominate the signal even before position-based recency weighting.
STAT_STALE_DECAY_PER_YEAR: float = 0.15


# ---------------------------------------------------------------------------
# Two-way player detection thresholds (tool-based path)
# ---------------------------------------------------------------------------
# These thresholds gate the tool-based two-way detection for prospects without
# stat history. Deliberately high — on 20-80 scale leagues, the median pitcher
# has contact=20 and power=20. Lower thresholds (e.g. 35/30) flagged ~64% of
# players as two-way on VMLB. See is_two_way_player() docstring for history.
TWO_WAY_CONTACT_THRESHOLD = 45
TWO_WAY_POWER_THRESHOLD = 40


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class EvaluationResult:
    """Result of evaluating a single player."""
    player_id: int
    composite_score: int              # 20-80, current ability (primary role)
    ceiling_score: int                # 20-80, projected peak (primary role)
    tool_only_score: int              # 20-80, pre-stat-blend

    # Component scores (new)
    offensive_grade: int | None = None       # hitting for hitters, pitching for pitchers
    baserunning_value: int | None = None     # hitters only
    defensive_value: int | None = None       # hitters only
    durability_score: int | None = None      # SP only

    # Component ceilings (new)
    offensive_ceiling: int | None = None
    baserunning_ceiling: int | None = None
    defensive_ceiling: int | None = None

    secondary_composite: int | None = None   # two-way players only
    secondary_ceiling: int | None = None     # two-way players only
    is_two_way: bool = False
    combined_value: int | None = None
    archetype: str = ""
    carrying_tools: list[str] = field(default_factory=list)
    red_flag_tools: list[str] = field(default_factory=list)
    divergence: dict[str, Any] | None = None
    confidence: str = "full"          # "full" or "partial"

    # Carrying tool bonus (positional context enhancement)
    carrying_tool_bonus: float = 0.0
    carrying_tool_breakdown: list[dict] = field(default_factory=list)
    ceiling_carrying_tool_bonus: float = 0.0
    ceiling_carrying_tool_breakdown: list[dict] = field(default_factory=list)

    # Positional context (positional context enhancement)
    positional_percentile: float | None = None
    positional_median: int | None = None


# ---------------------------------------------------------------------------
# Default tool weights — fallback when no calibrated config exists
# ---------------------------------------------------------------------------

DEFAULT_TOOL_WEIGHTS: dict[str, Any] = {
    "version": 1,
    "source": "default",
    "hitter": {
        # avoid_k excluded: Contact is a composite of BABIP + K-avoidance.
        # Speed excluded from hitting regression: contributes via baserunning only.
        # Defense shares derived from WAR regression (see recombination section).
        "C":   {"contact": 0.30, "gap": 0.16, "power": 0.24, "eye": 0.16, "speed": 0.02, "steal": 0.01, "stl_rt": 0.01, "defense": 0.15},
        "SS":  {"contact": 0.278, "gap": 0.166, "power": 0.203, "eye": 0.148, "speed": 0.028, "steal": 0.018, "stl_rt": 0.009, "defense": 0.15},
        "2B":  {"contact": 0.278, "gap": 0.166, "power": 0.203, "eye": 0.148, "speed": 0.028, "steal": 0.018, "stl_rt": 0.009, "defense": 0.15},
        "3B":  {"contact": 0.270, "gap": 0.155, "power": 0.252, "eye": 0.155, "speed": 0.039, "steal": 0.019, "stl_rt": 0.010, "defense": 0.10},
        "CF":  {"contact": 0.255, "gap": 0.156, "power": 0.195, "eye": 0.156, "speed": 0.039, "steal": 0.029, "stl_rt": 0.020, "defense": 0.15},
        "COF": {"contact": 0.29, "gap": 0.18, "power": 0.28, "eye": 0.17, "speed": 0.02, "steal": 0.01, "stl_rt": 0.01, "defense": 0.00},
        "1B":  {"contact": 0.30, "gap": 0.18, "power": 0.32, "eye": 0.19, "speed": 0.02, "steal": 0.00, "stl_rt": 0.00, "defense": 0.00},
    },
    "pitcher": {
        "SP": {"stuff": 0.35, "movement": 0.25, "control": 0.30, "arsenal": 0.10},
        "RP": {"stuff": 0.40, "movement": 0.25, "control": 0.25, "arsenal": 0.10},
    },
    "recombination": {
        # Derived empirically from WAR regression across EMLB + VMLB (AB >= 200).
        # Grid search for offense/defense/baserunning split that maximizes
        # Pearson r with WAR per position. Defense contributes far less to WAR
        # than the original design spec assumed — WAR already includes positional
        # adjustment, so the composite doesn't need to separately reward defense.
        "C":   {"offense": 0.80, "defense": 0.15, "baserunning": 0.05},
        "SS":  {"offense": 0.80, "defense": 0.15, "baserunning": 0.05},
        "2B":  {"offense": 0.80, "defense": 0.15, "baserunning": 0.05},
        "3B":  {"offense": 0.80, "defense": 0.10, "baserunning": 0.10},
        "CF":  {"offense": 0.75, "defense": 0.15, "baserunning": 0.10},
        "COF": {"offense": 0.95, "defense": 0.00, "baserunning": 0.05},
        "1B":  {"offense": 0.95, "defense": 0.00, "baserunning": 0.05},
    },
}


# ---------------------------------------------------------------------------
# Default carrying tool config — fallback when no calibrated config exists
# ---------------------------------------------------------------------------

DEFAULT_CARRYING_TOOL_CONFIG: dict[str, Any] = {
    "version": 1,
    "source": "calibrated",
    "positions": {
        "SS": {
            "carrying_tools": {
                "contact": {"war_premium_factor": 0.30},
                "power":   {"war_premium_factor": 0.35},
                "eye":     {"war_premium_factor": 0.22},
            }
        },
        "C": {
            "carrying_tools": {
                "contact": {"war_premium_factor": 0.37},
                "power":   {"war_premium_factor": 0.40},
            }
        },
        "CF": {
            "carrying_tools": {
                "contact": {"war_premium_factor": 0.23},
                "power":   {"war_premium_factor": 0.30},
            }
        },
        "2B": {
            "carrying_tools": {
                "power":   {"war_premium_factor": 0.18},
                "contact": {"war_premium_factor": 0.10},
            }
        },
        "3B": {
            "carrying_tools": {
                "power":   {"war_premium_factor": 0.09},
                "contact": {"war_premium_factor": 0.12},
                "eye":     {"war_premium_factor": 0.12},
                "gap":     {"war_premium_factor": 0.12},
            }
        },
        "COF": {
            "carrying_tools": {
                "contact": {"war_premium_factor": 0.13},
            }
        },
        "1B": {
            "carrying_tools": {
                "contact": {"war_premium_factor": 0.16},
            }
        },
    },
    "scarcity_schedule": [
        {"threshold": 65, "multiplier": 1.0},
        {"threshold": 70, "multiplier": 1.5},
        {"threshold": 75, "multiplier": 2.0},
        {"threshold": 80, "multiplier": 3.0},
    ],
}


# ---------------------------------------------------------------------------
# Configuration loading
# ---------------------------------------------------------------------------

def load_carrying_tool_config(league_dir: Path) -> dict:
    """Load carrying tool configuration from the league config directory.

    Reads ``data/<league>/config/carrying_tool_config.json``. Falls back to
    ``DEFAULT_CARRYING_TOOL_CONFIG`` when the file doesn't exist or contains
    malformed JSON (logs a warning for malformed files).

    Validates the config on load:
    - Raises ``ValueError`` for negative ``war_premium_factor`` values.
    - Raises ``ValueError`` for non-positive ``scarcity_multiplier`` values.
    - Uses the default scarcity schedule when the ``scarcity_schedule`` key
      is missing from the loaded config.

    Args:
        league_dir: Path to the league data directory (e.g. ``data/emlb``).

    Returns:
        A validated carrying tool config dict.

    Raises:
        ValueError: If any ``war_premium_factor`` is negative or any
            ``scarcity_multiplier`` value is non-positive.
    """
    config_path = league_dir / "config" / "carrying_tool_config.json"
    config: dict

    if not config_path.exists():
        log.info("No carrying_tool_config.json found at %s — using default config", config_path)
        config = _deep_copy_config(DEFAULT_CARRYING_TOOL_CONFIG)
    else:
        try:
            with open(config_path) as f:
                config = json.load(f)
        except (json.JSONDecodeError, OSError) as exc:
            log.warning(
                "Failed to read carrying_tool_config.json at %s (%s) — using default config",
                config_path, exc,
            )
            config = _deep_copy_config(DEFAULT_CARRYING_TOOL_CONFIG)

    # Fill in default scarcity schedule when missing from loaded config
    if "scarcity_schedule" not in config:
        config["scarcity_schedule"] = list(DEFAULT_CARRYING_TOOL_CONFIG["scarcity_schedule"])

    # Merge: fill missing positions from defaults so sparse calibrations
    # (e.g. VMLB with tight distributions) still get reasonable coverage.
    default_positions = DEFAULT_CARRYING_TOOL_CONFIG.get("positions", {})
    config_positions = config.setdefault("positions", {})
    for pos, pos_data in default_positions.items():
        if pos not in config_positions:
            config_positions[pos] = _deep_copy_config(pos_data)

    _validate_carrying_tool_config(config)
    return config


def _deep_copy_config(config: dict) -> dict:
    """Return a deep copy of a carrying tool config dict."""
    return json.loads(json.dumps(config))


def _validate_carrying_tool_config(config: dict) -> None:
    """Validate a carrying tool config, raising ValueError on invalid entries.

    Checks:
    - All ``war_premium_factor`` values must be non-negative.
    - All ``scarcity_schedule`` multiplier values must be positive (> 0).

    Raises:
        ValueError: With a descriptive message identifying the invalid entry.
    """
    positions = config.get("positions", {})
    for pos, pos_data in positions.items():
        carrying_tools = pos_data.get("carrying_tools", {})
        for tool, tool_data in carrying_tools.items():
            wpf = tool_data.get("war_premium_factor", 0)
            if wpf < 0:
                raise ValueError(
                    f"Negative war_premium_factor ({wpf}) for {pos}/{tool}"
                )

    for entry in config.get("scarcity_schedule", []):
        mult = entry.get("multiplier", 1.0)
        if mult <= 0:
            raise ValueError(
                f"Non-positive scarcity_multiplier ({mult}) at threshold "
                f"{entry.get('threshold', '?')}"
            )


# ---------------------------------------------------------------------------
# Carrying tool bonus computation
# ---------------------------------------------------------------------------

# Only these offensive tools can qualify for the carrying tool bonus.
# Speed and all defensive tools are excluded.
_CARRYING_TOOL_ELIGIBLE = frozenset({"contact", "gap", "power", "eye"})

# Minimum tool grade to qualify for a carrying tool bonus.
# Used as fallback when config doesn't specify a calibrated threshold.
_CARRYING_TOOL_GRADE_THRESHOLD = 65


def _scarcity_multiplier(tool_grade: int, schedule: list[dict]) -> float:
    """Compute the scarcity multiplier for a tool grade via linear interpolation.

    The *schedule* is a sorted list of ``{"threshold": int, "multiplier": float}``
    breakpoints.  For grades between breakpoints the multiplier is linearly
    interpolated.  Grades at or below the first breakpoint get the first
    multiplier; grades at or above the last breakpoint get the last multiplier.
    """
    if not schedule:
        return 1.0

    # Below or at the first breakpoint
    if tool_grade <= schedule[0]["threshold"]:
        return schedule[0]["multiplier"]

    # Above or at the last breakpoint
    if tool_grade >= schedule[-1]["threshold"]:
        return schedule[-1]["multiplier"]

    # Find the surrounding breakpoints and interpolate
    for i in range(len(schedule) - 1):
        lo = schedule[i]
        hi = schedule[i + 1]
        if lo["threshold"] <= tool_grade <= hi["threshold"]:
            span = hi["threshold"] - lo["threshold"]
            if span == 0:
                return lo["multiplier"]
            frac = (tool_grade - lo["threshold"]) / span
            return lo["multiplier"] + frac * (hi["multiplier"] - lo["multiplier"])

    # Fallback (should not be reached with a well-formed schedule)
    return schedule[-1]["multiplier"]


def compute_carrying_tool_bonus(
    tools: dict[str, float | int | None],
    position: str,
    config: dict,
) -> tuple[float, list[dict]]:
    """Compute the additive carrying tool bonus for a hitter.

    For each offensive tool grading 65+, checks if the tool/position
    combination is defined as a carrying tool in the config.  If so,
    computes:

        bonus = war_premium_factor × (tool_grade − 60) × scarcity_multiplier(tool_grade)

    Args:
        tools: Tool ratings dict with keys like ``"contact"``, ``"gap"``,
            ``"power"``, ``"eye"`` (values may be ``None``).
        position: Position bucket (e.g. ``"SS"``, ``"C"``, ``"CF"``).
        config: Carrying tool config dict (as returned by
            ``load_carrying_tool_config``).

    Returns:
        ``(total_bonus, breakdown)`` where *total_bonus* is the sum of
        individual tool bonuses and *breakdown* is a list of dicts, each
        with keys ``"tool"``, ``"grade"``, ``"bonus"``.
    """
    positions = config.get("positions", {})
    pos_data = positions.get(position)
    if pos_data is None:
        return 0.0, []

    carrying_tools_cfg = pos_data.get("carrying_tools", {})
    schedule = config.get("scarcity_schedule", [])

    total_bonus = 0.0
    breakdown: list[dict] = []

    for tool_name in _CARRYING_TOOL_ELIGIBLE:
        grade = tools.get(tool_name)
        if grade is None:
            continue

        tool_cfg = carrying_tools_cfg.get(tool_name)
        if tool_cfg is None:
            continue

        # Use calibrated threshold if available, else default
        threshold = tool_cfg.get("_calibration", {}).get(
            "threshold", _CARRYING_TOOL_GRADE_THRESHOLD
        )
        if grade < threshold:
            continue

        wpf = tool_cfg.get("war_premium_factor", 0.0)
        scarcity = _scarcity_multiplier(grade, schedule)
        bonus = wpf * (grade - threshold) * scarcity

        if bonus > 0:
            total_bonus += bonus
            breakdown.append({"tool": tool_name, "grade": grade, "bonus": bonus})

    return total_bonus, breakdown


def apply_carrying_tool_bonus(
    base_offensive_grade: float,
    tools: dict[str, float | int | None],
    position: str,
    config: dict,
) -> tuple[int, float, list[dict]]:
    """Apply carrying tool bonus to a base offensive grade.

    Computes the bonus via ``compute_carrying_tool_bonus``, adds it to
    *base_offensive_grade*, and clamps the result to [20, 80].

    Args:
        base_offensive_grade: The unclamped offensive grade from
            ``offensive_grade_raw()``.
        tools: Tool ratings dict.
        position: Position bucket.
        config: Carrying tool config dict.

    Returns:
        ``(enhanced_grade, bonus_amount, breakdown)`` where
        *enhanced_grade* is an ``int`` on the 20-80 scale.
    """
    bonus, breakdown = compute_carrying_tool_bonus(tools, position, config)
    enhanced = max(20, min(80, round(base_offensive_grade + bonus)))
    return enhanced, bonus, breakdown


# ---------------------------------------------------------------------------
# Positional median / percentile helpers
# ---------------------------------------------------------------------------


def compute_positional_medians(
    offensive_grades: dict[str, list[int]],
    min_sample_size: int = 15,
) -> dict[str, dict]:
    """Compute per-position offensive grade medians and percentile thresholds.

    Args:
        offensive_grades: Dict mapping position bucket to list of offensive
            grades for MLB players at that position.
        min_sample_size: Minimum number of players per bucket.  Buckets with
            fewer players are excluded.

    Returns:
        Dict mapping position bucket to ``{"median": int, "p25": int,
        "p75": int, "count": int}``.  Buckets with insufficient data are
        omitted.
    """
    result: dict[str, dict] = {}
    for bucket, grades in offensive_grades.items():
        if len(grades) < min_sample_size:
            continue
        med = int(round(statistics.median(grades)))
        # statistics.quantiles with n=4 gives [Q1, Q2, Q3]
        # Requires at least 2 data points
        if len(grades) >= 2:
            q1, _, q3 = statistics.quantiles(grades, n=4)
        else:
            # Single element: all percentiles equal the sole value
            q1 = float(grades[0])
            q3 = float(grades[0])
        result[bucket] = {
            "median": med,
            "p25": int(round(q1)),
            "p75": int(round(q3)),
            "count": len(grades),
        }
    return result


def compute_positional_percentile(
    offensive_grade: int,
    position: str,
    medians: dict[str, dict],
    offensive_grades: dict[str, list[int]],
) -> float | None:
    """Compute a player's offensive grade percentile within their position.

    Percentile = (count of grades in the position bucket that are ≤
    *offensive_grade*) / total count × 100.

    Args:
        offensive_grade: The player's offensive grade.
        position: Position bucket.
        medians: Output from ``compute_positional_medians()``.  Used only to
            check whether the position has sufficient data.
        offensive_grades: The raw grade lists (needed for percentile rank).

    Returns:
        Percentile as a float in [0, 100], or ``None`` if position data is
        unavailable (not in *medians* or not in *offensive_grades*).
    """
    if position not in medians:
        return None
    grades = offensive_grades.get(position)
    if not grades:
        return None
    count_le = sum(1 for g in grades if g <= offensive_grade)
    return count_le / len(grades) * 100.0


def load_tool_weights(league_dir: Path) -> dict:
    """Load tool weights from a league's config directory.

    Reads ``data/<league>/config/tool_weights.json``. Falls back to
    ``DEFAULT_TOOL_WEIGHTS`` when the file is missing, unreadable, or invalid.

    Args:
        league_dir: Path to the league data directory (e.g. ``data/emlb``).

    Returns:
        A validated tool-weights dict matching the ``DEFAULT_TOOL_WEIGHTS``
        schema.
    """
    config_path = league_dir / "config" / "tool_weights.json"
    if not config_path.exists():
        log.info("No tool_weights.json found at %s — using default weights", config_path)
        return dict(DEFAULT_TOOL_WEIGHTS)

    try:
        with open(config_path) as f:
            weights = json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("Failed to read tool_weights.json at %s (%s) — using default weights",
                     config_path, exc)
        return dict(DEFAULT_TOOL_WEIGHTS)

    if not validate_tool_weights(weights):
        log.warning("Invalid tool_weights.json at %s (validation failed) — using default weights",
                     config_path)
        return dict(DEFAULT_TOOL_WEIGHTS)

    return weights


def validate_tool_weights(weights: dict) -> bool:
    """Validate a tool-weights configuration dict.

    Checks:
    - ``hitter`` and ``pitcher`` top-level keys exist and are dicts.
    - Each positional bucket's weights are numeric and sum to 1.0 (±0.01).
    - Logs warnings for expected buckets that are missing (non-fatal).

    Returns:
        ``True`` if the config is valid, ``False`` otherwise.
    """
    if not isinstance(weights, dict):
        return False

    _EXPECTED_HITTER_BUCKETS = {"C", "SS", "2B", "3B", "CF", "COF", "1B"}
    _EXPECTED_PITCHER_BUCKETS = {"SP", "RP"}

    for section in ("hitter", "pitcher"):
        if section not in weights or not isinstance(weights[section], dict):
            return False
        for bucket, bucket_weights in weights[section].items():
            if not isinstance(bucket_weights, dict):
                return False
            values = []
            for v in bucket_weights.values():
                if not isinstance(v, (int, float)):
                    return False
                values.append(float(v))
            if not values:
                return False
            if abs(sum(values) - 1.0) > 0.01:
                return False

    # Warn about missing expected buckets (non-fatal — defaults will fill gaps)
    if "hitter" in weights:
        missing_h = _EXPECTED_HITTER_BUCKETS - set(weights["hitter"].keys())
        if missing_h:
            log.warning("tool_weights.json missing hitter buckets: %s — defaults will be used for those positions",
                        ", ".join(sorted(missing_h)))
    if "pitcher" in weights:
        missing_p = _EXPECTED_PITCHER_BUCKETS - set(weights["pitcher"].keys())
        if missing_p:
            log.warning("tool_weights.json missing pitcher buckets: %s — defaults will be used for those roles",
                        ", ".join(sorted(missing_p)))

    return True


# ---------------------------------------------------------------------------
# Composite / ceiling functions — imported from canonical evaluation package
# ---------------------------------------------------------------------------

from statsplusplus.evaluation.composite import (
    tool_transform,
    sub_mlb_floor_penalty,
    OFFENSIVE_TOOL_KEYS as _OFFENSIVE_TOOL_KEYS,
    BASERUNNING_TOOL_KEYS as _BASERUNNING_TOOL_KEYS,
    PITCHER_TOOL_KEYS as _PITCHER_TOOL_KEYS,
    PITCHER_CORE_KEYS as _PITCHER_CORE_KEYS,
    compute_composite_hitter,
    compute_composite_pitcher,
    compute_tool_only_score,
    compute_composite_mlb,
    compute_offensive_grade,
    compute_baserunning_value,
    compute_defensive_value,
    compute_combined_value,
    stat_to_2080,
    pitcher_stat_to_2080,
    offensive_grade_raw,
    baserunning_value_raw,
    defensive_value_raw,
)
from statsplusplus.evaluation.ceiling import (
    compute_ceiling,
    compute_true_ceiling,
    compute_component_ceilings,
)

# Hitter tool key tuple (used in batch pipeline)
_HITTER_TOOL_KEYS = ("contact", "gap", "power", "eye", "speed", "steal", "stl_rt")

def compute_durability_score(stamina: int | None, role: str) -> int | None:
    """Compute durability component for pitchers.

    For SP: returns stamina on the 20-80 scale (already normalized).
    For RP: returns ``None`` (stamina is not a meaningful differentiator).

    Args:
        stamina: Stamina rating on the 20-80 scale, or ``None``.
        role: Pitcher role — ``"SP"`` or ``"RP"``.

    Returns:
        Integer on 20-80 scale for SP, ``None`` for RP or when stamina is
        missing.
    """
    if role != "SP" or stamina is None:
        return None
    return max(20, min(80, stamina))


def derive_composite_from_components(
    offensive_grade: int | float,
    baserunning_value: int | float | None,
    defensive_value: int | float | None,
    recombination: dict[str, float],
) -> int:
    """Derive composite_score from component scores using recombination weights.

    This is the inverse of the current flow: instead of computing composite
    directly from tools, we compute it from the component scores using the
    same offense/defense/baserunning shares.

    When called with the **raw unclamped** component values (as floats from
    ``offensive_grade_raw``, ``baserunning_value_raw``,
    ``defensive_value_raw``), the result is identical to
    ``compute_composite_hitter`` for the same inputs — the decomposition is
    lossless.

    When called with clamped integer component scores (the public 20-80
    values), the result may differ by up to ±1 at the scale boundaries due
    to per-component clamping.  For display purposes this is acceptable;
    for round-trip verification, pass raw floats.

    When a component is ``None``, its share is redistributed proportionally
    to the remaining components.

    Args:
        offensive_grade: Offensive component score. Pass the raw float from
            ``offensive_grade_raw`` for exact round-trip, or the clamped
            int (20-80) for display derivation.
        baserunning_value: Baserunning component score (raw float or clamped
            int), or ``None``.
        defensive_value: Defensive component score (raw float or clamped
            int), or ``None``.
        recombination: Position-specific domain shares dict with keys
            ``"offense"``, ``"defense"``, ``"baserunning"`` summing to 1.0.

    Returns:
        Integer on 20-80 scale.
    """
    offense_share = recombination.get("offense", 0.0)
    defense_share = recombination.get("defense", 0.0)
    baserunning_share = recombination.get("baserunning", 0.0)

    # Build list of (score, share) for available components
    components: list[tuple[float, float]] = [(float(offensive_grade), offense_share)]
    if baserunning_value is not None:
        components.append((float(baserunning_value), baserunning_share))
    if defensive_value is not None:
        components.append((float(defensive_value), defense_share))

    total_share = sum(s for _, s in components)
    if total_share <= 0:
        return max(20, min(80, round(float(offensive_grade))))

    # Re-normalize shares over available components
    raw = sum(score * (share / total_share) for score, share in components)
    return max(20, min(80, round(raw)))


# Two-way player handling
# ---------------------------------------------------------------------------

def is_two_way_player(
    tools: dict,
    is_pitcher: bool = False,
    batting_stats: list | None = None,
    pitching_stats: list | None = None,
    stat_two_way_set: set | None = None,
    player_id: int | None = None,
) -> bool:
    """Determine whether a player qualifies as a two-way player.

    Uses a tiered approach:

    1. **Stat-based (ground truth)**: If ``stat_two_way_set`` is provided and
       the player's ID is in it, they are two-way. This set comes from
       ``war_model.load_stat_history()`` which identifies players with
       qualifying seasons in both batting (AB ≥ 130) and pitching (GS ≥ 10)
       in the same year.

    2. **Stat-based (local)**: If ``batting_stats`` and ``pitching_stats``
       are provided, checks for overlapping qualifying years (AB ≥ 130 and
       IP ≥ 40 in the same season).

    3. **Tool-based (prospects)**: For players without stat history, requires
       the player to be a pitcher (``is_pitcher=True``) AND have hitting
       tools well above pitcher norms: ``contact >= 45`` AND ``power >= 40``.
       These thresholds are deliberately high — on a 20-80 scale league,
       the median pitcher has contact=20 and power=20. The old thresholds
       (contact ≥ 35, power ≥ 30) flagged ~36% of pitchers as two-way
       because most pitchers have non-zero hitting ratings.

    .. note:: Design decision (2026-04-19)

       The original tool-based threshold (contact ≥ 35, power ≥ 30) was too
       permissive on 20-80 scale leagues where every player has all tools
       populated at 20+. On the VMLB league, this flagged 9,649 of 15,012
       players as two-way (64%). The fix adds ``is_pitcher`` as a required
       precondition for the tool path and raises thresholds to contact ≥ 45,
       power ≥ 40, which yields ~50-100 realistic two-way candidates.

    Args:
        tools: Tool ratings dict with keys like ``"contact"``, ``"power"``,
            ``"stuff"``, ``"movement"``, ``"control"`` (values on 20-80 scale).
        is_pitcher: Whether the player's primary position is pitcher.
            Required for the tool-based path — hitters with non-zero pitcher
            ratings are NOT two-way.
        batting_stats: Optional list of season dicts with ``"ab"`` key.
        pitching_stats: Optional list of season dicts with ``"ip"`` key.
        stat_two_way_set: Optional set of player IDs identified as two-way
            by ``war_model.load_stat_history()``. Takes precedence over
            tool-based detection.
        player_id: The player's ID, used for lookup in ``stat_two_way_set``.

    Returns:
        ``True`` if the player qualifies as two-way, ``False`` otherwise.
    """
    # Tier 1: stat-based ground truth from war_model
    if stat_two_way_set is not None and player_id is not None:
        if player_id in stat_two_way_set:
            return True

    # Tier 2: stat-based from provided season lists
    if batting_stats and pitching_stats:
        # In no-DH leagues, all pitchers accumulate AB from batting in their
        # lineup spot. Require much higher AB threshold to distinguish true
        # two-way players from pitchers who simply bat because there's no DH.
        from statsplusplus.config.league_config import LeagueConfig; _cfg = LeagueConfig()
        ab_threshold = 250 if _cfg.settings.get("dh_rule") == "No DH" and is_pitcher else 130
        batting_years = {s.get("year") for s in batting_stats if s.get("ab", 0) >= ab_threshold}
        pitching_years = {s.get("year") for s in pitching_stats if s.get("ip", 0) >= 40}
        if batting_years & pitching_years:
            return True

    # Tier 3: tool-based for prospects — pitcher position required
    if not is_pitcher:
        return False

    contact = tools.get("contact")
    power = tools.get("power")

    if contact is not None and power is not None:
        if contact >= TWO_WAY_CONTACT_THRESHOLD and power >= TWO_WAY_POWER_THRESHOLD:
            return True

    return False


def compute_two_way_scores(
    hitting_tools: dict,
    pitching_tools: dict,
    hitter_weights: dict,
    pitcher_weights: dict,
    defense: dict | None = None,
    def_weights: dict | None = None,
    arsenal: dict | None = None,
    stamina: int = 50,
    role: str = "SP",
) -> dict:
    """Compute separate hitter and pitcher Composite_Scores for a two-way player."""
    hitter_composite = compute_composite_hitter(
        hitting_tools, hitter_weights,
        defense or {}, def_weights or {},
    )
    pitcher_composite = compute_composite_pitcher(
        pitching_tools, pitcher_weights,
        arsenal or {}, stamina, role,
    )

    if hitter_composite >= pitcher_composite:
        primary = hitter_composite
        secondary = pitcher_composite
    else:
        primary = pitcher_composite
        secondary = hitter_composite

    return {
        "hitter_composite": hitter_composite,
        "pitcher_composite": pitcher_composite,
        "primary_composite": primary,
        "secondary_composite": secondary,
    }


# ---------------------------------------------------------------------------
# Divergence detection
# ---------------------------------------------------------------------------

def detect_divergence(
    tool_only_score: int,
    ovr: int | None,
    components: dict[str, int | None] | None = None,
    positional_context: dict | None = None,
) -> dict | None:
    """Compare Tool_Only_Score against OVR to detect evaluation divergence.

    When components are provided and divergence exists, includes a
    ``component_context`` key identifying which dimensions are strongest/
    weakest, sorted by value descending.

    When *positional_context* is provided, adds a ``"positional_context"``
    annotation for specific divergence/percentile combinations:

    * **landmine** with percentile > 60 → annotation present
    * **hidden_gem** with percentile < 25 → annotation present

    The existing ±5 threshold logic is unchanged — positional context is
    additive annotation only.

    Args:
        tool_only_score: The pre-stat-blend score from tool ratings (20-80).
        ovr: The game engine's OVR rating, or ``None`` if unavailable.
        components: Optional dict with component names as keys and scores
            (int or None) as values. When provided and divergence exists,
            non-None entries are included in ``component_context`` sorted
            by value descending.
        positional_context: Optional dict with keys ``"percentile"``
            (float 0-100), ``"position"`` (str), ``"median"`` (int).
            When provided, annotations are added for landmine/hidden_gem
            cases that meet the percentile thresholds.

    Returns:
        ``None`` when OVR is ``None``.
        Otherwise a dict with:
        - ``"type"``: ``"hidden_gem"`` (tool_only >= ovr + 5),
          ``"landmine"`` (ovr >= tool_only + 5), or ``"agreement"``.
        - ``"magnitude"``: ``tool_only_score - ovr``.
        - ``"tool_only_score"``: the tool-only score.
        - ``"ovr"``: the OVR value.
        - ``"component_context"``: (only when components provided and
          divergence exists) list of dicts with ``"component"`` and
          ``"value"`` keys, sorted by value descending.
        - ``"positional_context"``: (only when positional_context provided
          and annotation criteria met) dict with percentile, position,
          and median info.
    """
    if ovr is None:
        return None

    diff = tool_only_score - ovr

    if diff >= 5:
        divergence_type = "hidden_gem"
    elif diff <= -5:
        divergence_type = "landmine"
    else:
        divergence_type = "agreement"

    result = {
        "type": divergence_type,
        "magnitude": diff,
        "tool_only_score": tool_only_score,
        "ovr": ovr,
    }

    # Add component context when divergence exists and components are provided
    if divergence_type != "agreement" and components is not None:
        context = [
            {"component": name, "value": val}
            for name, val in components.items()
            if val is not None
        ]
        context.sort(key=lambda entry: entry["value"], reverse=True)
        result["component_context"] = context

    # Add positional context annotation when criteria are met
    if positional_context is not None and divergence_type != "agreement":
        percentile = positional_context.get("percentile")
        if percentile is not None:
            annotate = False
            if divergence_type == "landmine" and percentile > 60:
                annotate = True
            elif divergence_type == "hidden_gem" and percentile < 25:
                annotate = True
            if annotate:
                result["positional_context"] = {
                    "percentile": percentile,
                    "position": positional_context.get("position"),
                    "median": positional_context.get("median"),
                }

    return result


# ---------------------------------------------------------------------------
# Tool profile analysis
# ---------------------------------------------------------------------------

def classify_archetype(
    tools: dict,
    composite: int,
    is_pitcher: bool = False,
    arsenal: dict | None = None,
) -> str:
    """Classify a player's tool profile into an archetype.

    Hitter archetypes (checked in order):
    - ``"speed-first"``: speed >= composite + 15
    - ``"contact-first"``: contact >= composite + 10 AND power < composite
    - ``"power-over-hit"``: power >= composite + 10 AND contact < composite
    - ``"elite-defender"``: defensive score >= 65 AND offensive tools < composite
    - ``"balanced"``: all tools within ±8 of composite
    - ``"unclassified"``: none of the above

    Pitcher archetypes (checked in order):
    - ``"pitch-mix-specialist"``: arsenal depth >= 4 pitches at 50+ AND no
      single pitch >= 65
    - ``"stuff-over-command"``: stuff >= composite + 10 AND control < composite
    - ``"command-over-stuff"``: control >= composite + 10 AND stuff < composite
    - ``"balanced"``: all tools within ±8 of composite
    - ``"unclassified"``: none of the above

    Args:
        tools: Tool ratings dict (20-80 scale).
        composite: The player's Composite_Score.
        is_pitcher: Whether the player is a pitcher.
        arsenal: Pitch arsenal dict (pitchers only, for pitch-mix specialist).

    Returns:
        Archetype string label.
    """
    if is_pitcher:
        # Pitch-mix specialist: 4+ pitches at 50+, no single pitch >= 65
        if arsenal:
            pitches_at_50_plus = sum(1 for r in arsenal.values() if r >= 50)
            max_pitch = max(arsenal.values()) if arsenal else 0
            if pitches_at_50_plus >= 4 and max_pitch < 65:
                return "pitch-mix-specialist"

        stuff = tools.get("stuff", 0) or 0
        control = tools.get("control", 0) or 0
        movement = tools.get("movement", 0) or 0

        # Stuff-over-command
        if stuff >= composite + 10 and control < composite:
            return "stuff-over-command"

        # Command-over-stuff
        if control >= composite + 10 and stuff < composite:
            return "command-over-stuff"

        # Balanced: all pitcher tools within ±8
        pitcher_tools = [stuff, movement, control]
        if all(abs(t - composite) <= 8 for t in pitcher_tools):
            return "balanced"

        return "unclassified"

    # Hitter archetypes
    contact = tools.get("contact", 0) or 0
    power = tools.get("power", 0) or 0
    speed = tools.get("speed", 0) or 0

    # Speed-first (checked first — most distinctive)
    if speed >= composite + 15:
        return "speed-first"

    # Contact-first
    if contact >= composite + 10 and power < composite:
        return "contact-first"

    # Power-over-hit
    if power >= composite + 10 and contact < composite:
        return "power-over-hit"

    # Elite defender: defensive score >= 65 AND offensive tools < composite
    # We check if a "defense" key is present in tools for the defensive score
    defense_score = tools.get("defense_score", 0) or 0
    offensive_keys = ["contact", "gap", "power", "eye"]
    offensive_vals = [tools.get(k, 0) or 0 for k in offensive_keys]
    if defense_score >= 65 and all(v < composite for v in offensive_vals):
        return "elite-defender"

    # Balanced: all tools within ±8
    all_tool_keys = ["contact", "gap", "power", "eye", "speed"]
    all_vals = [tools.get(k, 0) or 0 for k in all_tool_keys if tools.get(k) is not None]
    if all_vals and all(abs(v - composite) <= 8 for v in all_vals):
        return "balanced"

    return "unclassified"


def identify_carrying_tools(tools: dict, composite: int) -> list[str]:
    """Identify tools rated 15+ points above the Composite_Score.

    Args:
        tools: Tool ratings dict (20-80 scale).
        composite: The player's Composite_Score.

    Returns:
        List of tool names that are carrying tools.
    """
    carrying = []
    for key, val in tools.items():
        if val is not None and isinstance(val, (int, float)):
            if val >= composite + 15:
                carrying.append(key)
    return sorted(carrying)


def identify_red_flag_tools(tools: dict, composite: int) -> list[str]:
    """Identify tools rated 15+ points below the Composite_Score.

    Args:
        tools: Tool ratings dict (20-80 scale).
        composite: The player's Composite_Score.

    Returns:
        List of tool names that are red-flag tools.
    """
    red_flags = []
    for key, val in tools.items():
        if val is not None and isinstance(val, (int, float)):
            if val <= composite - 15:
                red_flags.append(key)
    return sorted(red_flags)


# ---------------------------------------------------------------------------
# Snapshot delta computation
# ---------------------------------------------------------------------------


def compute_snapshot_deltas(
    current: dict[str, int | None],
    previous: dict[str, int | None],
) -> dict:
    """Compute tool-level deltas and flags between two rating snapshots.

    Takes two snapshot dicts with keys like ``"composite_score"``,
    ``"ceiling_score"``, and tool names (e.g. ``"contact"``, ``"power"``).
    Returns a dict describing the changes.

    Args:
        current: Most recent snapshot dict. Values are integer ratings or
            ``None`` for missing tools.
        previous: Earlier snapshot dict with the same key structure.

    Returns:
        Dict with:
        - ``"tool_deltas"``: dict of tool name → delta value (current - previous)
          for all keys present in both snapshots with non-None values.
          Component score keys (offensive_grade, baserunning_value,
          defensive_value) are excluded from tool_deltas.
        - ``"composite_delta"``: int (current - previous composite_score),
          or 0 if either is None.
        - ``"ceiling_delta"``: int (current - previous ceiling_score),
          or 0 if either is None.
        - ``"is_riser"``: bool — ``True`` when composite_delta >= 3.
        - ``"reduced_ceiling"``: bool — ``True`` when ceiling_delta <= -3.
        - ``"offensive_delta"``: int (current - previous offensive_grade),
          or 0 if either is None.
        - ``"baserunning_delta"``: int (current - previous baserunning_value),
          or 0 if either is None.
        - ``"defensive_delta"``: int (current - previous defensive_value),
          or 0 if either is None.
        - ``"top_component_change"``: str — name of the component with the
          largest absolute delta ("offensive", "baserunning", or "defensive").
          Empty string if all component deltas are 0.
    """
    # Compute tool-level deltas for all shared non-None keys
    tool_deltas: dict[str, int] = {}
    # Exclude meta keys and component score keys — only compute deltas for tool keys
    _meta_keys = {"player_id", "snapshot_date"}
    _component_keys = {"offensive_grade", "baserunning_value", "defensive_value"}
    for key in current:
        if key in _meta_keys or key in _component_keys:
            continue
        cur_val = current.get(key)
        prev_val = previous.get(key)
        if cur_val is not None and prev_val is not None:
            tool_deltas[key] = cur_val - prev_val

    # Composite and ceiling deltas
    cur_comp = current.get("composite_score")
    prev_comp = previous.get("composite_score")
    composite_delta = (cur_comp - prev_comp) if (cur_comp is not None and prev_comp is not None) else 0

    cur_ceil = current.get("ceiling_score")
    prev_ceil = previous.get("ceiling_score")
    ceiling_delta = (cur_ceil - prev_ceil) if (cur_ceil is not None and prev_ceil is not None) else 0

    # Component deltas
    cur_off = current.get("offensive_grade")
    prev_off = previous.get("offensive_grade")
    offensive_delta = (cur_off - prev_off) if (cur_off is not None and prev_off is not None) else 0

    cur_br = current.get("baserunning_value")
    prev_br = previous.get("baserunning_value")
    baserunning_delta = (cur_br - prev_br) if (cur_br is not None and prev_br is not None) else 0

    cur_def = current.get("defensive_value")
    prev_def = previous.get("defensive_value")
    defensive_delta = (cur_def - prev_def) if (cur_def is not None and prev_def is not None) else 0

    # Top component change
    component_deltas = {
        "offensive": abs(offensive_delta),
        "baserunning": abs(baserunning_delta),
        "defensive": abs(defensive_delta),
    }
    max_delta = max(component_deltas.values())
    top_component_change = "" if max_delta == 0 else max(component_deltas, key=component_deltas.get)

    return {
        "tool_deltas": tool_deltas,
        "composite_delta": composite_delta,
        "ceiling_delta": ceiling_delta,
        "is_riser": composite_delta >= 3,
        "reduced_ceiling": ceiling_delta <= -3,
        "offensive_delta": offensive_delta,
        "baserunning_delta": baserunning_delta,
        "defensive_delta": defensive_delta,
        "top_component_change": top_component_change,
    }


# ---------------------------------------------------------------------------
# Tool weight derivation (pure functions)
# ---------------------------------------------------------------------------


def derive_tool_weights(
    tool_ratings: list[dict],
    target_values: list[float],
    min_n: int = 40,
) -> dict | None:
    """Derive tool weights from tool rating vectors and target stat values.

    Uses per-feature Pearson correlation with the target. Each feature's
    weight is proportional to its r² (squared correlation), which measures
    how much variance in the target that feature explains. This approach
    avoids multicollinearity issues that plague multivariate OLS with
    correlated features (e.g., contact and eye) and is robust with small
    samples.

    Args:
        tool_ratings: List of dicts, each mapping tool names to numeric
            ratings (e.g. ``[{"contact": 55, "power": 60}, ...]``).
            All dicts must have the same keys.
        target_values: List of target stat values (e.g. OPS+), same length
            as ``tool_ratings``.
        min_n: Minimum sample size. Returns ``None`` if N < min_n.

    Returns:
        Dict mapping tool names to raw (un-normalized) correlation-based
        coefficients, or ``None`` if N < min_n or the best R² < 0.05
        (no feature explains meaningful variance).
    """
    n = len(tool_ratings)
    if n < min_n or n != len(target_values):
        return None
    if n == 0:
        return None

    # Identify feature keys from the first record
    keys = sorted(tool_ratings[0].keys())
    if not keys:
        return None

    # Extract target stats
    ys = target_values
    my = sum(ys) / n
    ss_yy = sum((y - my) ** 2 for y in ys)
    if ss_yy == 0:
        return None  # no variance in target

    # For each feature, compute Pearson r² with the target
    coefficients = {}
    best_r2 = 0.0

    for key in keys:
        xs = [rec.get(key, 0) or 0 for rec in tool_ratings]
        mx = sum(xs) / n
        ss_xx = sum((x - mx) ** 2 for x in xs)
        if ss_xx == 0:
            coefficients[key] = 0.0
            continue
        ss_xy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
        r = ss_xy / (ss_xx * ss_yy) ** 0.5
        r2 = r * r
        # Use signed r² to preserve direction: positive correlation → positive weight
        # Negative correlations will be clamped to zero by normalize_coefficients()
        coefficients[key] = r2 if r >= 0 else -r2
        best_r2 = max(best_r2, r2)

    # Quality gate: if no feature explains >= 5% of variance, bail out
    if best_r2 < 0.05:
        return None

    return coefficients


def normalize_coefficients(coefficients: dict, min_weight: float = 0.0) -> dict:
    """Clamp negative coefficients to zero and normalize to sum to 1.0.

    Negative coefficients are clamped because a negative weight would imply
    that a higher tool rating predicts worse performance, which is
    nonsensical for weight derivation.

    Args:
        coefficients: Dict mapping tool names to raw numeric coefficients.
        min_weight: Minimum weight per feature after normalization. Use > 0
            to prevent degenerate single-variable solutions (e.g. 0.05 ensures
            no feature is completely zeroed out). Default 0.0 preserves
            backward compatibility.

    Returns:
        Dict with all values >= 0 and summing to 1.0.
        If all coefficients are zero or negative, returns equal weights.
    """
    # Clamp negatives to zero
    clamped = {k: max(0.0, v) for k, v in coefficients.items()}

    total = sum(clamped.values())
    if total == 0:
        # All zeros → equal weights
        n = len(clamped)
        if n == 0:
            return {}
        equal = 1.0 / n
        return {k: equal for k in clamped}

    # Normalize to sum to 1.0
    normalized = {k: v / total for k, v in clamped.items()}

    # Apply minimum weight floor if specified
    if min_weight > 0 and len(normalized) > 1:
        # Ensure no feature is below the floor
        n_features = len(normalized)
        max_floor = 1.0 / n_features  # can't have floor > equal weight
        floor = min(min_weight, max_floor)

        below_floor = {k: v for k, v in normalized.items() if v < floor}
        if below_floor:
            # Redistribute: set below-floor features to floor, reduce others proportionally
            deficit = sum(floor - v for v in below_floor.values())
            above_floor = {k: v for k, v in normalized.items() if v >= floor}
            above_total = sum(above_floor.values())
            if above_total > deficit:
                scale = (above_total - deficit) / above_total
                normalized = {
                    k: (floor if k in below_floor else v * scale)
                    for k, v in normalized.items()
                }
            else:
                # Edge case: can't redistribute enough — use equal weights
                equal = 1.0 / n_features
                normalized = {k: equal for k in normalized}

    return normalized


def shrink_weights_toward_prior(
    weights: dict,
    prior: dict,
    n: int,
    n_full: int = 200,
    min_lambda: float = 0.25,
) -> dict:
    """Blend regression-derived weights toward a prior (ridge-style shrinkage).

    ``final = lambda * prior + (1 - lambda) * weights``, where ``lambda``
    (the weight given to the prior) shrinks as the sample size grows:

        lambda = min_lambda + (1 - min_lambda) * (1 - min(n, n_full) / n_full)

    With a full sample (``n >= n_full``) the prior still contributes
    ``min_lambda`` so a sound baseball prior always tempers extreme
    single-feature solutions (e.g. per-feature r² weighting can starve a
    primary tool like pitcher Stuff when a correlated tool has a marginally
    higher r²). With a tiny sample the prior dominates. Only keys present in
    ``weights`` are blended; the result is renormalized to sum to 1.0.

    Args:
        weights: Regression-derived weights (should already sum to ~1.0).
        prior: Prior weights (e.g. the hand-tuned defaults) for the same keys.
        n: Sample size the regression was fit on.
        n_full: Sample size at which shrinkage reaches its floor.
        min_lambda: Minimum prior weight, even at full sample.

    Returns:
        Blended weights summing to 1.0.
    """
    if not weights:
        return dict(weights)
    frac = min(max(n, 0), n_full) / n_full
    lam = min_lambda + (1.0 - min_lambda) * (1.0 - frac)
    lam = max(0.0, min(1.0, lam))

    blended = {}
    for k, w in weights.items():
        p = prior.get(k, w)
        blended[k] = lam * p + (1.0 - lam) * w
    total = sum(blended.values())
    if total > 0:
        blended = {k: v / total for k, v in blended.items()}
    return blended


def recombine_component_weights(
    hitting_coeffs: dict,
    baserunning_coeffs: dict,
    defense_coeff: float,
    recombination: dict,
) -> dict:
    """Recombine component-level regression weights into a unified profile.

    Scales each component's normalized coefficients by the position-specific
    domain share (offense, defense, baserunning), then normalizes the final
    weights to sum to 1.0.

    Speed appears in both the hitting and baserunning components — its total
    weight is the sum of its contribution from each domain.

    Args:
        hitting_coeffs: Normalized hitting regression coefficients
            (e.g. ``{"contact": 0.25, "gap": 0.13, ..., "speed": 0.15}``).
            Must sum to 1.0.
        baserunning_coeffs: Normalized baserunning regression coefficients
            (e.g. ``{"speed": 0.50, "steal": 0.30, "stl_rt": 0.20}``).
            Must sum to 1.0.
        defense_coeff: Defense coefficient (always 1.0 for a single-feature
            regression). Scaled by the defense share.
        recombination: Position-specific domain shares dict with keys
            ``"offense"``, ``"defense"``, ``"baserunning"`` summing to 1.0.

    Returns:
        Dict with unified weights for all tools plus ``"defense"``,
        all non-negative and summing to 1.0 (±0.01).
    """
    offense_share = recombination.get("offense", 0.0)
    defense_share = recombination.get("defense", 0.0)
    baserunning_share = recombination.get("baserunning", 0.0)

    # Start with hitting coefficients scaled by offense share
    final: dict[str, float] = {}
    for key, coeff in hitting_coeffs.items():
        final[key] = coeff * offense_share

    # Add baserunning coefficients scaled by baserunning share
    # Speed appears in both — accumulate
    for key, coeff in baserunning_coeffs.items():
        final[key] = final.get(key, 0.0) + coeff * baserunning_share

    # Defense gets the defense share directly
    final["defense"] = defense_coeff * defense_share

    # Normalize to sum to 1.0
    total = sum(final.values())
    if total > 0:
        final = {k: v / total for k, v in final.items()}
    else:
        # Fallback: equal weights
        n = len(final)
        if n > 0:
            equal = 1.0 / n
            final = {k: equal for k in final}

    return final

# ---------------------------------------------------------------------------
# Batch pipeline entry point — ONLY function with side effects (DB reads/writes)
# ---------------------------------------------------------------------------

# DB column → player_utils expected key mapping (mirrors calibrate._KEY_MAP)
_KEY_MAP = {
    "pot_c": "PotC", "pot_ss": "PotSS", "pot_second_b": "Pot2B",
    "pot_third_b": "Pot3B", "pot_first_b": "Pot1B", "pot_lf": "PotLF",
    "pot_cf": "PotCF", "pot_rf": "PotRF",
    "c": "C", "ss": "SS", "second_b": "2B", "third_b": "3B",
    "first_b": "1B", "lf": "LF", "cf": "CF", "rf": "RF",
    "stm": "Stm", "ovr": "Ovr", "pot": "Pot",
    "pot_fst": "PotFst", "pot_snk": "PotSnk", "pot_crv": "PotCrv",
    "pot_sld": "PotSld", "pot_chg": "PotChg", "pot_splt": "PotSplt",
    "pot_cutt": "PotCutt", "pot_cir_chg": "PotCirChg", "pot_scr": "PotScr",
    "pot_frk": "PotFrk", "pot_kncrv": "PotKncrv", "pot_knbl": "PotKnbl",
}

# Pitch DB columns and their corresponding potential columns
_PITCH_COLS = ["fst", "snk", "crv", "sld", "chg", "splt", "cutt",
               "cir_chg", "scr", "frk", "kncrv", "knbl"]

# Role ID → role string for assign_bucket
_ROLE_MAP = {11: "starter", 12: "reliever", 13: "closer"}


def _build_player_dict(row: dict) -> dict:
    """Build a player dict suitable for assign_bucket() from a DB row.

    Maps DB column names to the keys expected by player_utils functions
    (PotC, PotSS, Stm, etc.) and sets Pos, _role, Age.
    """
    p = dict(row)
    p["Pos"] = str(p.get("pos") or "")
    role_int = p.get("role") or 0
    p["_role"] = _ROLE_MAP.get(role_int, "")
    p["Age"] = p.get("age", 99)
    for db_key, api_key in _KEY_MAP.items():
        if db_key in p:
            v = p[db_key]
            if isinstance(v, (int, float)):
                p[api_key] = v
            elif v is not None and str(v).lstrip("-").isdigit():
                p[api_key] = int(v)
            else:
                p[api_key] = 0
    return p


def _extract_hitter_tools(row: dict, norm_fn) -> dict[str, float | None]:
    """Extract and normalize hitter tool ratings from a DB row.

    Handles L/R splits: when both _l and _r variants exist, computes
    weighted average (60% vs-RHP, 40% vs-LHP). Falls back to overall.

    Returns dict with keys: contact, gap, power, eye, speed, steal, stl_rt.
    """
    tool_map = {
        "contact": ("cntct", "cntct_r", "cntct_l"),
        "gap": ("gap", "gap_r", "gap_l"),
        "power": ("pow", "pow_r", "pow_l"),
        "eye": ("eye", "eye_r", "eye_l"),
    }
    tools: dict[str, float | None] = {}
    for key, (overall_col, r_col, l_col) in tool_map.items():
        r_val = norm_fn(row.get(r_col))
        l_val = norm_fn(row.get(l_col))
        if r_val is not None and l_val is not None:
            tools[key] = r_val * 0.6 + l_val * 0.4
        else:
            tools[key] = norm_fn(row.get(overall_col))

    tools["speed"] = norm_fn(row.get("speed"))
    tools["steal"] = norm_fn(row.get("steal"))
    tools["stl_rt"] = norm_fn(row.get("stl_rt"))
    return tools


def _extract_potential_hitter_tools(row: dict, norm_fn) -> dict[str, float | None]:
    """Extract potential hitter tool ratings from a DB row."""
    return {
        "contact": norm_fn(row.get("pot_cntct")),
        "gap": norm_fn(row.get("pot_gap")),
        "power": norm_fn(row.get("pot_pow")),
        "eye": norm_fn(row.get("pot_eye")),
        "speed": norm_fn(row.get("speed")),  # speed doesn't have a pot_ variant
        "steal": norm_fn(row.get("steal")),
        "stl_rt": norm_fn(row.get("stl_rt")),
    }


def _extract_pitcher_tools(row: dict, norm_fn) -> dict[str, float | None]:
    """Extract and normalize pitcher tool ratings from a DB row.

    When extended ratings are available (hra column), uses HRA as the
    "movement" value. HRA correlates r=0.927 with movement but is a cleaner
    signal — it directly measures home run prevention, which is the primary
    WAR driver that movement represents. The composite movement rating mixes
    HRA with ground ball tendency (PBABIP), diluting the signal.

    Includes stuff L/R splits for platoon balance detection.
    """
    tools: dict[str, float | None] = {}
    tools["stuff"] = norm_fn(row.get("stf"))
    tools["control"] = norm_fn(row.get("ctrl"))

    # Movement: use HRA when available (more predictive component)
    hra_val = norm_fn(row.get("hra"))
    if hra_val and hra_val > 20:
        tools["movement"] = hra_val
    else:
        tools["movement"] = norm_fn(row.get("mov"))

    # L/R splits for platoon balance penalty
    tools["stuff_l"] = norm_fn(row.get("stf_l"))
    tools["stuff_r"] = norm_fn(row.get("stf_r"))
    return tools


def _extract_potential_pitcher_tools(row: dict, norm_fn) -> dict[str, float | None]:
    """Extract potential pitcher tool ratings from a DB row."""
    tools = {
        "stuff": norm_fn(row.get("pot_stf")),
        "movement": norm_fn(row.get("pot_mov")),
        "control": norm_fn(row.get("pot_ctrl")),
    }
    # Extended ratings potential (pot_ when available, else current)
    for ext_key, pot_col, cur_col in [("hra", "pot_hra", "hra"), ("pbabip", "pot_pbabip", "pbabip")]:
        val = norm_fn(row.get(pot_col))
        if not val or val <= 20:
            val = norm_fn(row.get(cur_col))
        if val and val > 20:
            tools[ext_key] = val
    return tools


def _extract_arsenal(row: dict, norm_fn) -> dict[str, float]:
    """Extract pitch arsenal from a DB row. Returns {pitch_name: normalized_rating}."""
    arsenal: dict[str, float] = {}
    for col in _PITCH_COLS:
        raw = row.get(col)
        val = norm_fn(raw)
        if val is not None:
            arsenal[col] = val
    return arsenal


def _extract_potential_arsenal(row: dict, norm_fn) -> dict[str, float]:
    """Extract potential pitch arsenal. Uses pot_ columns, falls back to current."""
    arsenal: dict[str, float] = {}
    for col in _PITCH_COLS:
        pot_val = norm_fn(row.get("pot_" + col))
        if pot_val is not None:
            arsenal[col] = pot_val
        else:
            cur_val = norm_fn(row.get(col))
            if cur_val is not None:
                arsenal[col] = cur_val
    return arsenal


def _extract_defense_tools(row: dict, norm_fn=None) -> dict[str, float | None]:
    """Extract defensive tool ratings from a DB row, normalized to 20-80.

    Maps DB column names to the keys expected by DEFENSIVE_WEIGHTS
    (CFrm, CBlk, CArm, IFR, IFE, IFA, TDP, OFR, OFE, OFA).
    """
    raw = {
        "CFrm": row.get("c_frm"),
        "CBlk": row.get("c_blk"),
        "CArm": row.get("c_arm"),
        "IFR": row.get("ifr"),
        "IFE": row.get("ife"),
        "IFA": row.get("ifa"),
        "TDP": row.get("tdp"),
        "OFR": row.get("ofr"),
        "OFE": row.get("ofe"),
        "OFA": row.get("ofa"),
        "LF": row.get("lf"),
        "RF": row.get("rf"),
    }
    if norm_fn is None:
        return raw
    return {k: norm_fn(v) for k, v in raw.items()}


def _get_def_weights_for_bucket(bucket: str) -> dict[str, float]:
    """Return DEFENSIVE_WEIGHTS for a bucket, importing from fv_model."""
    from statsplusplus.evaluation.constants import DEFENSIVE_WEIGHTS
    if bucket == "COF":
        # Use the better of LF/RF weights — compute both in compute_composite_hitter
        # For COF, we pass both COF_LF and COF_RF and let the caller pick max
        # But for weight selection, we use COF_LF as the representative set
        return DEFENSIVE_WEIGHTS.get("COF_LF", {})
    return DEFENSIVE_WEIGHTS.get(bucket, {})


def _compute_defensive_score_for_bucket(
    defense_tools: dict, bucket: str, norm_fn
) -> float:
    """Compute weighted defensive score for a bucket using fv_model.defensive_score.

    This mirrors the approach in fv_model.py but works with our extracted tools.
    """
    from statsplusplus.evaluation.constants import DEFENSIVE_WEIGHTS

    def _n(val):
        return norm_fn(val) or 0

    if bucket == "COF":
        lf = sum(
            _n(defense_tools.get(f, 0) or 0) * w
            for f, w in DEFENSIVE_WEIGHTS.get("COF_LF", {}).items()
        )
        rf = sum(
            _n(defense_tools.get(f, 0) or 0) * w
            for f, w in DEFENSIVE_WEIGHTS.get("COF_RF", {}).items()
        )
        return max(lf, rf)

    weights = DEFENSIVE_WEIGHTS.get(bucket)
    if not weights:
        return 0.0
    return sum(_n(defense_tools.get(f, 0) or 0) * w for f, w in weights.items())


def _load_qualifying_stat_seasons(
    conn: sqlite3.Connection,
    player_id: int,
    is_pitcher: bool,
) -> list[dict]:
    """Load qualifying stat seasons for an MLB player, most recent first.

    Season totals are aggregated **by year** before the qualifying threshold
    is applied. A player traded mid-season has multiple ``mlb_*_stats`` rows in
    the same year (one per team stint); summing them first prevents (a) one
    calendar year double-counting in the recency-weighted blend and (b) a full
    season being disqualified because neither individual stint clears the
    threshold on its own. Rate stats (ERA, OBP, SLG) are recomputed from the
    summed component counts, not averaged from the per-stint rate columns.

    Hitters: full-year AB >= 130, split_id=1
    Pitchers: full-year IP >= 40 (SP) or IP >= 20 (RP), split_id=1

    Returns list of dicts with year + era (pitchers) or obp, slg (hitters).
    """
    if is_pitcher:
        rows = conn.execute("""
            SELECT year,
                   SUM(ip) AS ip, SUM(gs) AS gs,
                   SUM(er) AS er, SUM(outs) AS outs,
                   SUM(k) AS k, SUM(bb) AS bb, SUM(hra) AS hra, SUM(hp) AS hp
            FROM mlb_pitching_stats
            WHERE player_id = ? AND split_id = 1
            GROUP BY year
            HAVING ((SUM(gs) > 3 AND SUM(ip) >= 40) OR (SUM(gs) <= 3 AND SUM(ip) >= 20))
            ORDER BY year DESC
        """, (player_id,)).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            outs = d.get("outs") or 0
            er = d.get("er") or 0
            # Recompute ERA from summed components (er * 27 / outs), matching
            # the true-decimal-innings storage rule. Fall back to NULL if outs
            # is unavailable so _compute_stat_signal skips the season.
            d["era"] = (er * 27.0 / outs) if outs > 0 else None
            result.append(d)
        return result
    else:
        rows = conn.execute("""
            SELECT year,
                   SUM(ab) AS ab, SUM(h) AS h, SUM(d) AS d, SUM(t) AS t,
                   SUM(hr) AS hr, SUM(bb) AS bb, SUM(hbp) AS hbp, SUM(sf) AS sf
            FROM mlb_batting_stats
            WHERE player_id = ? AND split_id = 1
            GROUP BY year
            HAVING SUM(ab) >= 130
            ORDER BY year DESC
        """, (player_id,)).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            ab = d.get("ab") or 0
            h = d.get("h") or 0
            bb = d.get("bb") or 0
            hbp = d.get("hbp") or 0
            sf = d.get("sf") or 0
            dbl = d.get("d") or 0
            trp = d.get("t") or 0
            hr = d.get("hr") or 0
            ob_denom = ab + bb + hbp + sf
            d["obp"] = ((h + bb + hbp) / ob_denom) if ob_denom > 0 else None
            # Total bases = singles + 2·2B + 3·3B + 4·HR = h + d + 2t + 3hr.
            total_bases = h + dbl + 2 * trp + 3 * hr
            d["slg"] = (total_bases / ab) if ab > 0 else None
            result.append(d)
        return result


def _load_milb_stat_seasons(
    conn: sqlite3.Connection,
    player_id: int,
    is_pitcher: bool,
    milb_averages: dict,
) -> list[dict]:
    """Load MiLB stat seasons for a player, normalized to level-relative OPS+/ERA-.

    Args:
        conn: DB connection.
        player_id: Player ID.
        is_pitcher: Whether to load pitching or batting stats.
        milb_averages: Dict mapping league_id → {batting_avg: {obp, slg}, pitching_avg: {era}}.
            Loaded from league_settings.json.

    Returns:
        List of dicts, each containing:
            year: Season year
            league_id: The MiLB league ID
            level: Integer level (2=AAA, 3=AA, 4=A, 6=Rookie)
            ops_plus/era_minus_inv: Level-relative production (100 = average at level)
            pa/ip: Sample size
            stat_2080: The production value converted to 20-80 scale
    """
    if is_pitcher:
        rows = conn.execute("""
            SELECT year, league_id, SUM(ip) as ip, SUM(er) as er, SUM(outs) as outs,
                   SUM(k) as k, SUM(bb) as bb, SUM(hra) as hra, SUM(g) as g, SUM(gs) as gs
            FROM pitching_stats
            WHERE player_id = ? AND split_id = 1 AND league_id IS NOT NULL
            GROUP BY year, league_id
            HAVING SUM(ip) >= 20
            ORDER BY year DESC
        """, (player_id,)).fetchall()
    else:
        rows = conn.execute("""
            SELECT year, league_id, SUM(pa) as pa, SUM(ab) as ab,
                   SUM(h) as h, SUM(d) as d, SUM(t) as t, SUM(hr) as hr,
                   SUM(bb) as bb, SUM(hbp) as hbp
            FROM batting_stats
            WHERE player_id = ? AND split_id = 1 AND league_id IS NOT NULL
            GROUP BY year, league_id
            HAVING SUM(pa) >= 50
            ORDER BY year DESC
        """, (player_id,)).fetchall()

    results = []
    for row in rows:
        lid = row["league_id"]
        lg_info = milb_averages.get(lid)
        if not lg_info:
            continue

        if is_pitcher:
            lg_era = (lg_info.get("pitching_avg") or {}).get("era")
            if not lg_era or lg_era <= 0:
                continue
            outs = row["outs"] or 0
            er = row["er"] or 0
            ip = row["ip"] or 0
            if outs <= 0:
                continue
            era = er * 27.0 / outs
            era_minus = (era / lg_era) * 100.0
            # Invert: lower ERA = better, so 200 - era_minus gives higher=better
            stat_plus = 200.0 - era_minus
            stat_2080 = pitcher_stat_to_2080(stat_plus)
            results.append({
                "year": row["year"],
                "league_id": lid,
                "level": lg_info.get("level", 0),
                "era_minus_inv": stat_plus,
                "ip": ip,
                "stat_2080": stat_2080,
            })
        else:
            lg_obp = (lg_info.get("batting_avg") or {}).get("obp")
            lg_slg = (lg_info.get("batting_avg") or {}).get("slg")
            if not lg_obp or not lg_slg or lg_obp <= 0 or lg_slg <= 0:
                continue
            pa = row["pa"] or 0
            ab = row["ab"] or 0
            h = row["h"] or 0
            bb = row["bb"] or 0
            hbp = row["hbp"] or 0
            d = row["d"] or 0
            t = row["t"] or 0
            hr = row["hr"] or 0
            if pa <= 0 or ab <= 0:
                continue
            obp = (h + bb + hbp) / pa
            slg = (h + d + 2 * t + 3 * hr) / ab
            ops_plus = 100.0 * (obp / lg_obp + slg / lg_slg - 1.0)
            stat_2080 = stat_to_2080(ops_plus)
            results.append({
                "year": row["year"],
                "league_id": lid,
                "level": lg_info.get("level", 0),
                "ops_plus": ops_plus,
                "pa": pa,
                "stat_2080": stat_2080,
            })

    return results


def _load_milb_averages(league_dir: Path) -> dict:
    """Load MiLB league averages from league_settings.json.

    Returns dict mapping league_id → {batting_avg: {...}, pitching_avg: {...}, level: int}.
    """
    settings_path = league_dir / "config" / "league_settings.json"
    if not settings_path.exists():
        return {}
    try:
        s = json.load(open(settings_path))
        result = {}
        for ml in s.get("minor_leagues", []):
            result[ml["lid"]] = {
                "batting_avg": ml.get("batting_avg"),
                "pitching_avg": ml.get("pitching_avg"),
                "level": ml.get("level", 0),
            }
        return result
    except (json.JSONDecodeError, OSError):
        return {}


def _compute_stat_signal(
    stat_seasons: list[dict],
    is_pitcher: bool,
    lg_obp: float,
    lg_slg: float,
    lg_era: float,
    current_year: int | None = None,
) -> list[float]:
    """Convert qualifying stat seasons to 20-80 scale values.

    Hitters: OPS+ = 100 × (OBP/lgOBP + SLG/lgSLG - 1) → stat_to_2080()
    Pitchers: ERA- = ERA / lgERA × 100, inverted (200 - ERA-) → pitcher_stat_to_2080()

    When ``current_year`` is provided, seasons older than
    ``STAT_STALE_DROP_YEARS`` are dropped, and seasons within the retained
    window have their deviation from average (50) shrunk by
    ``STAT_STALE_DECAY_PER_YEAR`` per year of age. This prevents a player's old
    production from driving his current composite.

    Returns list of 20-80 values, most recent first.
    """
    result: list[float] = []
    for season in stat_seasons:
        years_ago = 0
        if current_year is not None:
            years_ago = max(0, current_year - (season.get("year") or current_year))
            if years_ago >= STAT_STALE_DROP_YEARS:
                continue  # too stale to describe current ability
        if is_pitcher:
            era = season.get("era")
            if era is None or lg_era <= 0:
                continue
            # ERA- = ERA / lgERA × 100; invert so higher = better
            era_minus = (era / lg_era) * 100.0
            stat_plus = 200.0 - era_minus
        else:
            obp = season.get("obp") or 0
            slg = season.get("slg") or 0
            if lg_obp <= 0 or lg_slg <= 0:
                continue
            stat_plus = 100.0 * (obp / lg_obp + slg / lg_slg - 1.0)

        val = pitcher_stat_to_2080(stat_plus) if is_pitcher else stat_to_2080(stat_plus)
        # Age-decay: shrink the deviation from 50 for older-but-retained seasons.
        if years_ago > 0:
            decay = max(0.0, 1.0 - years_ago * STAT_STALE_DECAY_PER_YEAR)
            val = 50.0 + (val - 50.0) * decay
        result.append(val)
    return result


def _load_league_averages(league_dir: Path) -> tuple[float, float, float]:
    """Load league averages for stat normalization.

    Returns (lg_obp, lg_slg, lg_era).
    """
    try:
        with open(league_dir / "config" / "league_averages.json") as f:
            avgs = json.load(f)
        lg_obp = avgs.get("batting", {}).get("obp", 0.320)
        lg_slg = avgs.get("batting", {}).get("slg", 0.420)
        lg_era = avgs.get("pitching", {}).get("era", 4.50)
        return lg_obp, lg_slg, lg_era
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return 0.320, 0.420, 4.50


def run(
    league_dir: Path | None = None,
    conn: sqlite3.Connection | None = None,
) -> None:
    """Compute Composite_Score, Ceiling_Score, and Tool_Only_Score for all players.

    This is the **only** function in the module with side effects (DB reads/writes).
    All computation is delegated to pure functions.

    Args:
        league_dir: Path to the league data directory. When ``None``, resolved
            via ``league_context.get_league_dir()``.
        conn: SQLite connection. When ``None``, created via ``db.get_conn()``.

    Side effects:
        - Reads from ``ratings``, ``players``, ``batting_stats``, ``pitching_stats``
        - Writes ``composite_score``, ``ceiling_score``, ``tool_only_score``,
          ``secondary_composite`` to the ``ratings`` table
        - Writes ``composite_score``, ``ceiling_score`` to ``ratings_history``
          for the current snapshot date

    Error handling:
        - Players with no tool ratings are skipped
        - Players with partial tools get partial scores (confidence="partial")
        - On failure, all writes are rolled back
    """
    # -- Resolve dependencies via injection or defaults --
    if league_dir is None:
        from statsplusplus.config.league_context import get_league_dir as _get_league_dir
        league_dir = _get_league_dir()

    own_conn = False
    if conn is None:
        from statsplusplus.data import db as _db
        conn = _db.get_connection(league_dir)
        own_conn = True

    try:
        _run_impl(conn, league_dir)
    except Exception:
        conn.rollback()
        raise
    finally:
        if own_conn:
            conn.close()


def _run_impl(conn: sqlite3.Connection, league_dir: Path) -> None:
    """Internal implementation of the batch evaluation pipeline."""
    from statsplusplus.config.ratings import norm_continuous as _norm_pkg
    from statsplusplus.config.ratings import norm as _norm_display_pkg
    from statsplusplus.utils.positions import assign_bucket as _assign_bucket
    from statsplusplus.evaluation.constants import DEFENSIVE_WEIGHTS
    from statsplusplus.config.league_config import LeagueConfig as _LC
    _scale = _LC(base_dir=league_dir).ratings_scale
    def _norm(val): return _norm_pkg(val, _scale)
    def _norm_display(val): return _norm_display_pkg(val, _scale)

    # Apply league-calibrated model parameters to composite module constants
    import statsplusplus.evaluation.constants as _constants
    from statsplusplus.evaluation.constants import load_model_weights as _load_mw
    _mw = _load_mw(league_dir)
    import statsplusplus.evaluation.composite as _comp_mod
    _comp_mod.HITTER_IMBALANCE_SPREAD_THRESHOLD = int(_mw.get_param(
        "HITTER_IMBALANCE_SPREAD_THRESHOLD", _constants.HITTER_IMBALANCE_SPREAD_THRESHOLD))
    _comp_mod.PITCHER_IMBALANCE_SPREAD_THRESHOLD = int(_mw.get_param(
        "PITCHER_IMBALANCE_SPREAD_THRESHOLD", _constants.PITCHER_IMBALANCE_SPREAD_THRESHOLD))

    # -- Load tool weights --
    weights = load_tool_weights(league_dir)
    hitter_weights = weights.get("hitter", DEFAULT_TOOL_WEIGHTS["hitter"])
    pitcher_weights = weights.get("pitcher", DEFAULT_TOOL_WEIGHTS["pitcher"])

    # -- Load per-tool transform curves (per player-type/role). Absent → the
    #    composite falls back to the global tool_transform (legacy behavior). --
    _tool_transforms = weights.get("tool_transforms", {}) or {}
    hitter_transforms = _tool_transforms.get("hitter")
    sp_transforms = _tool_transforms.get("SP")
    rp_transforms = _tool_transforms.get("RP")

    # -- Load carrying tool config --
    ct_config = load_carrying_tool_config(league_dir)

    # -- Load league averages for MLB stat blending --
    lg_obp, lg_slg, lg_era = _load_league_averages(league_dir)

    # Current season year — used to age-discount stale qualifying seasons in
    # the stat blend (a season 4+ years old should not drive today's composite).
    _cy_row = conn.execute("SELECT MAX(year) FROM mlb_pitching_stats").fetchone()
    current_year = (_cy_row[0] if _cy_row and _cy_row[0] else None)
    if current_year is None:
        _cy_row = conn.execute("SELECT MAX(year) FROM mlb_batting_stats").fetchone()
        current_year = (_cy_row[0] if _cy_row and _cy_row[0] else 0)

    # -- Load MiLB averages and level discounts for unified stat blending --
    milb_averages = _load_milb_averages(league_dir)
    milb_discounts = {"hitter": {}, "pitcher": {}}
    milb_norm_ages = {}
    try:
        mw_path = league_dir / "config" / "model_weights.json"
        if mw_path.exists():
            _mw_data = json.load(open(mw_path))
            milb_discounts = _mw_data.get("MILB_LEVEL_DISCOUNTS", milb_discounts)
            milb_norm_ages = _mw_data.get("MILB_NORM_AGES", milb_norm_ages)
    except (json.JSONDecodeError, OSError):
        pass

    # -- Load stat-based two-way set from war_model --
    # This is the ground truth for players with qualifying seasons in both
    # batting and pitching. Falls back to empty set if unavailable.
    stat_two_way: set = set()
    try:
        from statsplusplus.config.league_config import LeagueConfig; _lc = LeagueConfig()
        game_date = _lc.game_date
        if game_date:
            from statsplusplus.evaluation.war import load_stat_history as _load_stat_history
            _, _, stat_two_way = _load_stat_history(conn, game_date)
    except Exception as exc:
        log.warning("Could not load stat-based two-way set: %s — falling back to tool-based detection only", exc)

    # -- Query all players with their latest ratings --
    rows = conn.execute("""
        SELECT r.*, p.age, p.pos, p.role, p.level, p.player_id as pid
        FROM ratings r
        JOIN players p ON r.player_id = p.player_id
        WHERE r.snapshot_date = (SELECT MAX(snapshot_date) FROM ratings)
    """).fetchall()

    if not rows:
        return

    snapshot_date = rows[0]["snapshot_date"]

    # -- Ensure positional context columns exist (idempotent migration) --
    _existing_cols = {r[1] for r in conn.execute("PRAGMA table_info(ratings)").fetchall()}
    for _col, _typ in [("positional_percentile", "REAL"), ("positional_median", "INTEGER"), ("true_ceiling", "INTEGER")]:
        if _col not in _existing_cols:
            conn.execute(f"ALTER TABLE ratings ADD COLUMN {_col} {_typ}")

    # -- Pass 1: Compute all scores and collect MLB hitter offensive grades --
    ratings_updates: list[tuple] = []
    history_updates: list[tuple] = []

    # Collect MLB hitter offensive grades by position bucket for median computation
    mlb_offensive_grades: dict[str, list[int]] = {}

    # Store per-player info needed for Pass 2 (positional context enrichment)
    # Each entry: (index_into_ratings_updates, bucket, offensive_grade, is_mlb,
    #              is_pitcher, tool_only_score, ovr, components_dict)
    pass2_hitter_info: list[dict] = []

    for row in rows:
        row_dict = dict(row)
        player_id = row_dict["player_id"]

        # Build player dict for assign_bucket
        p = _build_player_dict(row_dict)
        pos = p.get("pos") or ""
        role_int = p.get("role") or 0
        is_pitcher = (
            str(pos) == "1"
            or p.get("_role") in ("starter", "reliever", "closer")
        )

        # Determine bucket
        try:
            bucket = _assign_bucket(p, use_pot=False)
        except Exception:
            bucket = "COF" if not is_pitcher else "SP"

        # Extract tools
        hitter_tools = _extract_hitter_tools(row_dict, _norm)
        pitcher_tools = _extract_pitcher_tools(row_dict, _norm)
        potential_hitter_tools = _extract_potential_hitter_tools(row_dict, _norm)
        potential_pitcher_tools = _extract_potential_pitcher_tools(row_dict, _norm)
        defense_tools = _extract_defense_tools(row_dict, _norm)
        arsenal = _extract_arsenal(row_dict, _norm)
        potential_arsenal = _extract_potential_arsenal(row_dict, _norm)
        stamina = _norm(row_dict.get("stm")) or 50

        # Check if player has any tool ratings at all
        if is_pitcher:
            has_tools = any(
                v is not None
                for v in (pitcher_tools.get("stuff"), pitcher_tools.get("movement"),
                          pitcher_tools.get("control"))
            )
        else:
            has_tools = any(
                v is not None
                for k, v in hitter_tools.items()
                if k in ("contact", "gap", "power", "eye")
            )

        if not has_tools:
            continue  # skip players with no tool ratings

        # Determine confidence
        if is_pitcher:
            all_present = all(
                pitcher_tools.get(k) is not None
                for k in ("stuff", "movement", "control")
            )
        else:
            all_present = all(
                hitter_tools.get(k) is not None
                for k in ("contact", "gap", "power", "eye")
            )
        confidence = "full" if all_present else "partial"

        # -- Detect two-way player --
        two_way_hitting = {
            k: v for k, v in hitter_tools.items()
            if v is not None
        }
        two_way_pitching = {
            k: v for k, v in pitcher_tools.items()
            if k in ("stuff", "movement", "control") and v is not None
        }
        two_way = is_two_way_player(
            {**two_way_hitting, **two_way_pitching},
            is_pitcher=is_pitcher,
            stat_two_way_set=stat_two_way,
            player_id=player_id,
        )

        # -- Compute scores --
        composite_score: int
        ceiling_score: int
        tool_only_score: int
        secondary_composite: int | None = None

        # Component scores (populated per branch)
        offensive_grade: int | None = None
        baserunning_value: int | None = None
        defensive_value: int | None = None
        durability_score: int | None = None

        # Component ceilings (populated per branch)
        offensive_ceiling: int | None = None
        baserunning_ceiling: int | None = None
        defensive_ceiling: int | None = None

        # True ceiling (no age blend -- theoretical maximum)
        true_ceiling: int | None = None

        # Carrying tool bonus (populated per branch for hitters)
        ct_bonus: float = 0.0
        ct_breakdown: list[dict] = []
        ceiling_ct_bonus: float = 0.0
        ceiling_ct_breakdown: list[dict] = []

        if two_way:
            # Get weights for hitter bucket
            hitter_bucket = bucket if bucket not in ("SP", "RP") else "COF"
            h_weights = hitter_weights.get(hitter_bucket, hitter_weights.get("COF", {}))
            # Get weights for pitcher role
            pitcher_role = bucket if bucket in ("SP", "RP") else "SP"
            p_weights = pitcher_weights.get(pitcher_role, pitcher_weights.get("SP", {}))

            def_weights = _get_def_weights_for_bucket(hitter_bucket)

            two_way_result = compute_two_way_scores(
                hitting_tools=hitter_tools,
                pitching_tools=pitcher_tools,
                hitter_weights=h_weights,
                pitcher_weights=p_weights,
                defense=defense_tools,
                def_weights=def_weights,
                arsenal=arsenal,
                stamina=stamina,
                role=pitcher_role,
            )

            composite_score = two_way_result["primary_composite"]
            secondary_composite = two_way_result["secondary_composite"]
            tool_only_score = composite_score  # pre-blend for two-way

            # Ceiling: compute for both roles, take higher
            player_age = row_dict.get("age") or 25
            _p_tf = rp_transforms if pitcher_role == "RP" else sp_transforms
            h_ceiling = compute_ceiling(
                potential_hitter_tools, h_weights, two_way_result["hitter_composite"],
                accuracy=row_dict.get("acc") or "A",
                work_ethic=row_dict.get("wrk_ethic") or "N",
                defense=defense_tools,
                def_weights=def_weights,
                age=player_age,
                ratings_scale=_scale,
                transforms=hitter_transforms,
            )
            p_ceiling = compute_ceiling(
                potential_pitcher_tools, p_weights, two_way_result["pitcher_composite"],
                accuracy=row_dict.get("acc") or "A",
                work_ethic=row_dict.get("wrk_ethic") or "N",
                is_pitcher=True,
                arsenal=arsenal,
                stamina=stamina,
                role=pitcher_role,
                age=player_age,
                ratings_scale=_scale,
                transforms=_p_tf,
            )

            ceiling_score = max(h_ceiling, p_ceiling)

            # True ceiling (no age blend)
            h_true_ceil = compute_true_ceiling(
                potential_hitter_tools, h_weights, two_way_result["hitter_composite"],
                accuracy=row_dict.get("acc") or "A",
                work_ethic=row_dict.get("wrk_ethic") or "N",
                defense=defense_tools, def_weights=def_weights,
                transforms=hitter_transforms,
            )
            p_true_ceil = compute_true_ceiling(
                potential_pitcher_tools, p_weights, two_way_result["pitcher_composite"],
                accuracy=row_dict.get("acc") or "A",
                work_ethic=row_dict.get("wrk_ethic") or "N",
                is_pitcher=True, arsenal=potential_arsenal, stamina=stamina, role=pitcher_role,
                transforms=_p_tf,
            )
            true_ceiling = max(h_true_ceil, p_true_ceil)

            # Component scores for two-way: compute hitter components
            offensive_grade = compute_offensive_grade(hitter_tools, h_weights, hitter_transforms)
            baserunning_value = compute_baserunning_value(hitter_tools, h_weights)
            defensive_value = compute_defensive_value(defense_tools, def_weights)

            # Apply carrying tool bonus to offensive grade (two-way hitter side)
            # DISABLED: redundant with tool transform + interaction terms + position weights
            ct_bonus = 0.0
            ct_breakdown = []

            # Recompute hitter composite with enhanced offensive grade for two-way
            if ct_bonus > 0:
                recombo = weights.get("recombination", DEFAULT_TOOL_WEIGHTS["recombination"])
                bucket_recombo = recombo.get(hitter_bucket, recombo.get("COF", {}))
                enhanced_hitter_composite = derive_composite_from_components(
                    offensive_grade, baserunning_value, defensive_value,
                    bucket_recombo,
                )
                # Re-derive primary/secondary with enhanced hitter composite
                pitcher_composite = two_way_result["pitcher_composite"]
                if enhanced_hitter_composite >= pitcher_composite:
                    composite_score = enhanced_hitter_composite
                    secondary_composite = pitcher_composite
                else:
                    composite_score = pitcher_composite
                    secondary_composite = enhanced_hitter_composite
                tool_only_score = composite_score

            # Component ceilings for two-way (hitter side)
            current_components = {
                "offensive_grade": offensive_grade,
                "baserunning_value": baserunning_value,
                "defensive_value": defensive_value,
            }
            ceilings = compute_component_ceilings(
                potential_hitter_tools, h_weights, current_components,
                defense=defense_tools, def_weights=def_weights,
                age=player_age,
                ct_config=None, position=hitter_bucket,
                transforms=hitter_transforms,
            )
            offensive_ceiling = ceilings.get("offensive_ceiling")
            baserunning_ceiling = ceilings.get("baserunning_ceiling")
            defensive_ceiling = ceilings.get("defensive_ceiling")
            ceiling_ct_bonus = 0.0
            ceiling_ct_breakdown = []

            # Carrying tool bonus on ceiling disabled — redundant with tool
            # transform (1.3× above 60) + peak tool bonus (capped +10).
            # Was inflating ceiling_score by +5 to +31 for elite prospects
            # without affecting FV or surplus (those use true_ceiling).

        elif is_pitcher:
            role = bucket if bucket in ("SP", "RP") else "SP"
            p_weights = pitcher_weights.get(role, pitcher_weights.get("SP", {}))
            p_transforms = rp_transforms if role == "RP" else sp_transforms

            tool_only_score = compute_composite_pitcher(
                pitcher_tools, p_weights, arsenal, stamina, role, p_transforms,
            )
            composite_score = tool_only_score

            # Ceiling
            player_age = row_dict.get("age") or 25
            ceiling_score = compute_ceiling(
                potential_pitcher_tools, p_weights,
                composite_score,
                accuracy=row_dict.get("acc") or "A",
                work_ethic=row_dict.get("wrk_ethic") or "N",
                is_pitcher=True,
                arsenal=arsenal,
                stamina=stamina,
                role=role,
                age=player_age,
                ratings_scale=_scale,
                transforms=p_transforms,
            )
            true_ceiling = compute_true_ceiling(
                potential_pitcher_tools, p_weights, composite_score,
                accuracy=row_dict.get("acc") or "A",
                work_ethic=row_dict.get("wrk_ethic") or "N",
                is_pitcher=True, arsenal=potential_arsenal, stamina=stamina, role=role,
                transforms=p_transforms,
            )

            # Component scores for pitchers:
            # pitching composite stored as offensive_grade (primary skill component)
            offensive_grade = tool_only_score
            # durability for SP only
            durability_score = compute_durability_score(stamina, role)

            # Component ceilings for pitchers
            current_components = {"offensive_grade": offensive_grade}
            ceilings = compute_component_ceilings(
                potential_pitcher_tools, p_weights, current_components,
                is_pitcher=True, arsenal=arsenal, stamina=stamina, role=role,
                age=player_age,
                transforms=p_transforms,
            )
            offensive_ceiling = ceilings.get("offensive_ceiling")
            baserunning_ceiling = ceilings.get("baserunning_ceiling")
            defensive_ceiling = ceilings.get("defensive_ceiling")

        else:
            # Hitter
            h_weights = hitter_weights.get(bucket, hitter_weights.get("COF", {}))
            def_weights = _get_def_weights_for_bucket(bucket)

            tool_only_score = compute_composite_hitter(
                hitter_tools, h_weights, defense_tools, def_weights, hitter_transforms,
            )
            composite_score = tool_only_score

            # Ceiling
            player_age = row_dict.get("age") or 25
            ceiling_score = compute_ceiling(
                potential_hitter_tools, h_weights,
                composite_score,
                accuracy=row_dict.get("acc") or "A",
                work_ethic=row_dict.get("wrk_ethic") or "N",
                defense=defense_tools,
                def_weights=def_weights,
                age=player_age,
                ratings_scale=_scale,
                transforms=hitter_transforms,
            )
            true_ceiling = compute_true_ceiling(
                potential_hitter_tools, h_weights, composite_score,
                accuracy=row_dict.get("acc") or "A",
                work_ethic=row_dict.get("wrk_ethic") or "N",
                defense=defense_tools, def_weights=def_weights,
                transforms=hitter_transforms,
            )

            # Component scores for hitters
            offensive_grade = compute_offensive_grade(hitter_tools, h_weights, hitter_transforms)
            baserunning_value = compute_baserunning_value(hitter_tools, h_weights)
            defensive_value = compute_defensive_value(defense_tools, def_weights)

            # Apply carrying tool bonus to offensive grade
            # DISABLED: redundant with tool transform + interaction terms + position weights
            ct_bonus = 0.0
            ct_breakdown = []

            # Recompute composite with enhanced offensive grade (carrying tool
            # bonus flows through the offensive component only — Req 7.1, 7.2)
            if ct_bonus > 0:
                recombo = weights.get("recombination", DEFAULT_TOOL_WEIGHTS["recombination"])
                bucket_recombo = recombo.get(bucket, recombo.get("COF", {}))
                composite_score = derive_composite_from_components(
                    offensive_grade, baserunning_value, defensive_value,
                    bucket_recombo,
                )
                tool_only_score = composite_score

            # Component ceilings for hitters
            current_components = {
                "offensive_grade": offensive_grade,
                "baserunning_value": baserunning_value,
                "defensive_value": defensive_value,
            }
            ceilings = compute_component_ceilings(
                potential_hitter_tools, h_weights, current_components,
                defense=defense_tools, def_weights=def_weights,
                age=player_age,
                ct_config=None, position=bucket,
                transforms=hitter_transforms,
            )
            offensive_ceiling = ceilings.get("offensive_ceiling")
            baserunning_ceiling = ceilings.get("baserunning_ceiling")
            defensive_ceiling = ceilings.get("defensive_ceiling")
            ceiling_ct_bonus = 0.0
            ceiling_ct_breakdown = []

        # -- Unified stat blending (MLB + MiLB) --
        level = row_dict.get("level")
        is_mlb = (level == "1" or level == 1)

        # Step 1: MLB stat blending (preserves existing behavior exactly)
        mlb_stat_2080_values = []
        if is_mlb:
            stat_seasons = _load_qualifying_stat_seasons(conn, player_id, is_pitcher)
            if stat_seasons:
                mlb_stat_2080_values = _compute_stat_signal(
                    stat_seasons, is_pitcher, lg_obp, lg_slg, lg_era,
                    current_year=current_year,
                )
                if mlb_stat_2080_values:
                    peak_age = 27 if is_pitcher else 28
                    player_age = row_dict.get("age") or 28
                    composite_score = compute_composite_mlb(
                        tool_only_score, mlb_stat_2080_values,
                        peak_age=peak_age, player_age=player_age,
                        is_pitcher=is_pitcher,
                        bucket=bucket,
                    )

        # Step 2: MiLB stat blending (additive for all players with MiLB data)
        # Only applies when MLB stat blending hasn't already provided a strong signal.
        # The MiLB contribution is scaled DOWN as MLB sample grows.
        if milb_averages:
            milb_seasons = _load_milb_stat_seasons(conn, player_id, is_pitcher, milb_averages)
            if milb_seasons:
                disc_key = "pitcher" if is_pitcher else "hitter"
                milb_weighted_sum = 0.0
                milb_total_weight = 0.0
                # Determine current year for recency decay
                _cur_year = milb_seasons[0].get("year", 2034) if milb_seasons else 2034
                for ms in milb_seasons[:3]:
                    lv = str(ms.get("level", 0))
                    discount = float(milb_discounts.get(disc_key, {}).get(lv, 0.0))
                    if discount <= 0:
                        continue
                    pa = ms.get("pa", 0) if not is_pitcher else ms.get("ip", 0) * 4.3
                    # Recency decay: current year = 1.0, -1yr = 0.7, -2yr = 0.4
                    years_ago = _cur_year - ms.get("year", _cur_year)
                    recency = max(0.2, 1.0 - years_ago * 0.3)
                    weight = pa * discount * recency
                    milb_weighted_sum += ms["stat_2080"] * weight
                    milb_total_weight += weight

                if milb_total_weight > 0:
                    milb_signal = milb_weighted_sum / milb_total_weight
                    milb_effective_pa = milb_total_weight

                    # MiLB blend weight: scales with sample, capped at 25% for MiLB alone
                    # (MLB can already do up to 60% via compute_composite_mlb)
                    milb_blend = min(0.25, milb_effective_pa / 800.0)

                    # Fade MiLB as MLB sample grows: if MLB already blended heavily,
                    # reduce MiLB contribution to avoid double-counting
                    mlb_seasons_count = len(mlb_stat_2080_values)
                    if mlb_seasons_count >= 3:
                        milb_blend *= 0.10  # MLB dominates — MiLB nearly irrelevant
                    elif mlb_seasons_count == 2:
                        milb_blend *= 0.35  # MLB moderate — MiLB still contributes
                    elif mlb_seasons_count == 1:
                        milb_blend *= 0.65  # MLB limited — MiLB is important context

                    # Young player discount for negative MiLB signal
                    player_age = row_dict.get("age") or 28
                    peak_age = 27 if is_pitcher else 28
                    if player_age < peak_age and tool_only_score > milb_signal:
                        age_factor = max(0.3, 1.0 - (peak_age - player_age) * 0.12)
                        milb_blend *= age_factor

                    # Apply MiLB blend on top of whatever composite we have
                    if milb_blend >= 0.01:
                        composite_score = max(20, min(80, round(
                            composite_score * (1.0 - milb_blend) + milb_signal * milb_blend
                        )))

        # Ensure ceiling >= composite after stat blending
        ceiling_score = max(ceiling_score, composite_score)

        # -- Divergence detection with component context (Pass 1 — no positional context yet) --
        ovr = row_dict.get("ovr")
        components_dict = {
            "offensive_grade": offensive_grade,
            "baserunning_value": baserunning_value,
            "defensive_value": defensive_value,
        }
        divergence = detect_divergence(tool_only_score, ovr, components=components_dict)

        # Collect MLB hitter offensive grades for positional median computation
        is_hitter = not is_pitcher
        if is_mlb and is_hitter and offensive_grade is not None:
            mlb_offensive_grades.setdefault(bucket, []).append(offensive_grade)

        # Collect update tuples (positional_percentile and positional_median
        # are None initially — Pass 2 will fill them in for qualifying hitters)
        positional_percentile: float | None = None
        positional_median: int | None = None

        ratings_updates.append((
            composite_score, ceiling_score, tool_only_score, secondary_composite,
            offensive_grade, baserunning_value, defensive_value,
            durability_score, offensive_ceiling, true_ceiling,
            positional_percentile, positional_median,
            player_id, snapshot_date,
        ))
        history_updates.append((
            composite_score, ceiling_score,
            offensive_grade, baserunning_value, defensive_value,
            durability_score, offensive_ceiling,
            player_id, snapshot_date,
        ))

        # Store info for Pass 2 divergence enrichment (hitters only)
        if is_hitter and offensive_grade is not None:
            pass2_hitter_info.append({
                "index": len(ratings_updates) - 1,
                "bucket": bucket,
                "offensive_grade": offensive_grade,
                "is_mlb": is_mlb,
                "tool_only_score": tool_only_score,
                "ovr": ovr,
                "components_dict": components_dict,
            })

    # -- Median computation (between Pass 1 and Pass 2) --
    positional_medians = compute_positional_medians(mlb_offensive_grades)

    # -- Pass 2: Enrich divergence reports with positional context --
    for info in pass2_hitter_info:
        idx = info["index"]
        bucket = info["bucket"]
        off_grade = info["offensive_grade"]
        ovr = info["ovr"]

        # Compute positional percentile for this hitter
        pct = compute_positional_percentile(
            off_grade, bucket, positional_medians, mlb_offensive_grades,
        )

        # Get the median for this bucket (if available)
        bucket_stats = positional_medians.get(bucket)
        pos_median = bucket_stats["median"] if bucket_stats else None

        # Store positional context in the ratings update tuple
        if pct is not None or pos_median is not None:
            old_tuple = ratings_updates[idx]
            # Replace positional_percentile (index 10) and positional_median (index 11)
            ratings_updates[idx] = (
                old_tuple[0], old_tuple[1], old_tuple[2], old_tuple[3],
                old_tuple[4], old_tuple[5], old_tuple[6],
                old_tuple[7], old_tuple[8], old_tuple[9],
                pct, pos_median,
                old_tuple[12], old_tuple[13],
            )

        # Re-run divergence detection with positional context for hitters
        # that had divergence in Pass 1
        divergence = detect_divergence(
            info["tool_only_score"], ovr, components=info["components_dict"],
        )
        if divergence and divergence.get("type") != "agreement" and pct is not None:
            positional_ctx = {
                "percentile": pct,
                "position": bucket,
                "median": pos_median,
            }
            detect_divergence(
                info["tool_only_score"], ovr,
                components=info["components_dict"],
                positional_context=positional_ctx,
            )

    # -- Batch write to ratings table --
    conn.executemany("""
        UPDATE ratings
        SET composite_score = ?, ceiling_score = ?, tool_only_score = ?,
            secondary_composite = ?,
            offensive_grade = ?, baserunning_value = ?, defensive_value = ?,
            durability_score = ?, offensive_ceiling = ?, true_ceiling = ?,
            positional_percentile = ?, positional_median = ?
        WHERE player_id = ? AND snapshot_date = ?
    """, ratings_updates)

    # -- Write to ratings_history for current snapshot --
    # First check if ratings_history has the composite_score column
    try:
        hist_cols = {r[1] for r in conn.execute("PRAGMA table_info(ratings_history)").fetchall()}
        if "composite_score" in hist_cols:
            conn.executemany("""
                UPDATE ratings_history
                SET composite_score = ?, ceiling_score = ?,
                    offensive_grade = ?, baserunning_value = ?, defensive_value = ?,
                    durability_score = ?, offensive_ceiling = ?
                WHERE player_id = ? AND snapshot_date = ?
            """, history_updates)
    except Exception:
        pass  # ratings_history may not exist or may not have the columns yet

    conn.commit()
