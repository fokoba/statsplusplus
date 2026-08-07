"""Positional logic and constants.

Single source of truth for:
- Level hierarchy and mappings
- Role/position identification
- Positional bucket display
- Pitch field identifiers
"""

from __future__ import annotations

from statsplusplus.models.player import PositionalBucket


# ---------------------------------------------------------------------------
# Role and position mappings
# ---------------------------------------------------------------------------

# OOTP role codes → bucket string
ROLE_MAP: dict[int, str] = {11: "SP", 12: "RP", 13: "CL"}

# Game position number → display string
GAME_POS_MAP: dict[int, str] = {
    1: "P", 2: "C", 3: "1B", 4: "2B", 5: "3B",
    6: "SS", 7: "LF", 8: "CF", 9: "RF", 10: "DH",
}

# Positional sort order for display (lower = higher in lineup card)
POS_SORT_ORDER: dict[str, int] = {
    "SP": 1, "RP": 2, "CL": 3, "P": 1,
    "C": 4, "1B": 5, "2B": 6, "3B": 7, "SS": 8,
    "LF": 9, "CF": 10, "RF": 11, "OF": 10, "DH": 12,
}

# WAR-based positional adjustment (runs above average position player)
POSITIONAL_WAR_ADJUSTMENTS: dict[str, float] = {
    "C": 1.2, "SS": 0.7, "2B": 0.3, "CF": 0.25, "3B": 0.2,
    "COF": -0.7, "1B": -1.2, "DH": -1.7, "SP": 0.0, "RP": -1.0,
}


# ---------------------------------------------------------------------------
# Level hierarchy
# ---------------------------------------------------------------------------

# Level integer → canonical string key (for config lookups)
LEVEL_KEY_MAP: dict[int, str] = {
    0: "draft", 1: "mlb", 2: "aaa", 3: "aa", 4: "a",
    5: "a-short", 6: "rookie", 8: "intl", 10: "college", 11: "hs",
}

# Level integer → display label
LEVEL_DISPLAY_MAP: dict[int, str] = {
    0: "Draft", 1: "MLB", 2: "AAA", 3: "AA", 4: "A",
    5: "A-Short", 6: "Rookie", 8: "International", 10: "College", 11: "HS",
}

# Level sort order (lower = higher level)
LEVEL_ORDER: list[int] = [1, 2, 3, 4, 5, 6, 8]

# Normal age at each level for an on-track prospect
LEVEL_NORM_AGE: dict[str, int] = {
    "draft": 18, "mlb": 26, "aaa": 26, "aa": 24, "a": 22,
    "a-short": 21, "rookie": 19, "usl": 19, "dsl": 18, "intl": 17,
}

# Development discount by level (bust probability only)
DEVELOPMENT_DISCOUNT: dict[str, float] = {
    "mlb": 1.00, "aaa": 0.88, "aa": 0.78, "a": 0.68,
    "a-short": 0.55, "rookie": 0.45, "usl": 0.45, "dsl": 0.45, "intl": 0.35,
}

# Estimated years until MLB debut by current level
YEARS_TO_MLB: dict[str, float] = {
    "mlb": 0.0, "aaa": 0.5, "aa": 1.5, "a": 2.5,
    "a-short": 3.5, "rookie": 4.5, "usl": 4.5, "dsl": 4.5, "intl": 5.0,
}


# ---------------------------------------------------------------------------
# Pitch fields
# ---------------------------------------------------------------------------

# Pitch type field names as stored in DB/ratings
PITCH_FIELDS: list[str] = [
    "Fst", "Snk", "Crv", "Sld", "Chg", "Splt",
    "Cutt", "CirChg", "Scr", "Frk", "Kncrv", "Knbl",
]

# Pitch field → human-readable name
PITCH_NAMES: dict[str, str] = {
    "Fst": "Fastball", "Snk": "Sinker", "Crv": "Curveball",
    "Sld": "Slider", "Chg": "Changeup", "Splt": "Splitter",
    "Cutt": "Cutter", "CirChg": "Circle Change", "Scr": "Screwball",
    "Frk": "Forkball", "Kncrv": "Knuckle Curve", "Knbl": "Knuckleball",
}


# ---------------------------------------------------------------------------
# Display helpers
# ---------------------------------------------------------------------------

def display_pos(bucket: str | PositionalBucket, listed_pos: str | None = None) -> str:
    """Convert internal bucket to display position.

    COF → OF for display. All other buckets display as-is.
    """
    val = bucket.value if isinstance(bucket, PositionalBucket) else bucket
    return "OF" if val == "COF" else val


def next_level(current_level: int) -> int | None:
    """Return the next level up (lower number) in the hierarchy.

    Returns None if already at MLB (level 1).
    """
    try:
        idx = LEVEL_ORDER.index(current_level)
        return LEVEL_ORDER[idx - 1] if idx > 0 else None
    except ValueError:
        return None


def level_key(level_int: int) -> str:
    """Convert level integer to canonical string key."""
    return LEVEL_KEY_MAP.get(level_int, "draft")


def level_display(level_int: int) -> str:
    """Convert level integer to human-readable display string."""
    return LEVEL_DISPLAY_MAP.get(level_int, str(level_int))
