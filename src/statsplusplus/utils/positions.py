"""Positional logic and constants.

Single source of truth for:
- Level hierarchy and mappings
- Role/position identification
- Positional bucket display
- Pitch field identifiers
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

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


# ---------------------------------------------------------------------------
# Positional model estimation
# ---------------------------------------------------------------------------


def load_positional_models(league_dir: Path) -> dict[str, Any]:
    """Load OLS positional models from model_weights.json.

    Args:
        league_dir: Path to the league data directory.

    Returns:
        Dict mapping position column (e.g. 'pot_ss') to model dict with
        'features' and 'coefficients' keys. Empty dict if unavailable.
    """
    mw_path = league_dir / "config" / "model_weights.json"
    if not mw_path.exists():
        return {}
    try:
        import json
        data = json.loads(mw_path.read_text())
        result: dict[str, Any] = data.get("POSITIONAL_MODELS", {})
        return result
    except (json.JSONDecodeError, OSError):
        return {}


def estimate_positional_rating(
    p: dict[str, Any],
    pos_col: str,
    models: dict[str, Any],
) -> float | None:
    """Estimate a positional rating from defensive tools using calibrated OLS model.

    Args:
        p: Player dict with defensive tool ratings (IFR, IFA, IFE, TDP, etc.).
        pos_col: Target position column (e.g. 'pot_ss', 'pot_cf').
        models: Positional models dict from load_positional_models().

    Returns:
        Estimated positional rating, or None if model unavailable or features missing.
    """
    model = models.get(pos_col)
    if not model:
        return None
    features = model["features"]
    coefficients = model["coefficients"]
    key_map = {
        "ifr": "IFR", "ifa": "IFA", "ife": "IFE", "tdp": "TDP",
        "ofr": "OFR", "ofa": "OFA", "ofe": "OFE",
        "c_arm": "CArm", "c_blk": "CBlk", "c_frm": "CFrm",
        "height": "Height",
    }
    vals: list[float] = []
    for feat in features:
        k = key_map.get(feat, feat)
        v = p.get(k) or 0
        if not v:
            return None
        vals.append(float(v))
    result: float = coefficients[0] + sum(coefficients[i + 1] * vals[i] for i in range(len(vals)))
    return max(0.0, result)


def estimate_all_positions(
    p: dict[str, Any],
    models: dict[str, Any],
) -> dict[str, float]:
    """Estimate ratings at all positions using calibrated models.

    Args:
        p: Player dict with defensive tool ratings.
        models: Positional models dict from load_positional_models().

    Returns:
        Dict mapping bucket name → estimated rating.
    """
    col_to_bucket = {
        "pot_ss": "SS", "pot_second_b": "2B", "pot_third_b": "3B",
        "pot_cf": "CF", "pot_lf": "LF", "pot_rf": "RF",
        "pot_first_b": "1B", "pot_c": "C",
    }
    estimates: dict[str, float] = {}
    for pos_col, bucket in col_to_bucket.items():
        est = estimate_positional_rating(p, pos_col, models)
        if est is not None:
            estimates[bucket] = est
    return estimates


def assign_bucket(
    p: dict[str, Any],
    role_map: dict[int, str] | None = None,
    positional_models: dict[str, Any] | None = None,
    use_pot: bool = True,
) -> str:
    """Assign positional evaluation bucket.

    Determines the most valuable defensive position for a player based on
    their positional ratings (or estimated ratings from defensive tools).

    Args:
        p: Player dict with ratings. Expected keys vary by player type:
            - Pos: game position code as string
            - _role: role string ("starter", "reliever", "closer", "position_player")
            - Positional grades: PotC, PotSS, Pot2B, PotCF, etc. (if use_pot=True)
              or C, SS, 2B, CF, etc. (if use_pot=False)
            - Stm: stamina (for pitchers)
            - PotFst, PotSnk, ...: pitch potentials (for SP/RP decision)
        role_map: Mapping of role code → role string. If None, uses a default.
        positional_models: OLS models for fallback estimation. If None, skips model fallback.
        use_pot: Whether to use potential ratings (True) or current ratings (False).

    Returns:
        Bucket string: "C", "SS", "2B", "3B", "CF", "COF", "1B", "SP", or "RP".
    """
    if role_map is None:
        role_map = {0: "position_player", 11: "starter", 12: "reliever", 13: "closer"}

    def pgrade(field: str) -> int:
        key = ("Pot" + field) if use_pot else field
        v = p.get(key, 0)
        if isinstance(v, (int, float)):
            return int(v)
        try:
            return int(v)
        except (ValueError, TypeError):
            return 0

    pos_str = str(p.get("Pos", ""))
    role_str = str(p.get("_role", ""))
    is_pitcher = (pos_str == "P" or role_str in ("starter", "reliever", "closer"))

    if is_pitcher:
        if not use_pot and role_str in ("reliever", "closer"):
            return "RP"
        if role_str == "starter" and (p.get("Stm") or 0) >= 20:
            return "SP"
        stm = p.get("Stm") or 0
        if stm >= 25 and ((p.get("PotKnbl") or 0) >= 45 or (p.get("PotKncrv") or 0) >= 45):
            return "SP"
        viable = sum(1 for f in PITCH_FIELDS if (p.get("Pot" + f) or 0) >= 45)
        return "RP" if (viable < 3 or stm < 30) else "SP"

    # Position players — check positional grades
    if pgrade("C") >= 45:
        return "C"
    if pgrade("SS") >= 50:
        ss_grade = pgrade("SS")
        if ss_grade <= 55:
            if pgrade("3B") >= ss_grade + 10:
                return "3B"
            if pgrade("2B") >= ss_grade + 10:
                return "2B"
        return "SS"
    if pgrade("2B") >= 50 or pgrade("SS") >= 50:
        return "2B"
    if pgrade("CF") >= 55:
        cf_grade = pgrade("CF")
        if cf_grade <= 55:
            best_cof = max(pgrade("LF"), pgrade("RF"))
            if best_cof >= cf_grade + 10:
                return "COF"
        return "CF"
    if pgrade("LF") >= 45 or pgrade("RF") >= 45:
        return "COF"
    if pgrade("3B") >= 45:
        return "3B"
    if pgrade("1B") >= 45:
        return "1B"

    # Fallback: calibrated model estimation
    if positional_models:
        estimates = estimate_all_positions(p, positional_models)
        if estimates:
            _EST_THRESHOLDS = {
                "C": 47, "SS": 52, "2B": 50, "CF": 53,
                "3B": 47, "LF": 45, "RF": 45, "1B": 40,
            }
            _PRIORITY = ["C", "SS", "CF", "2B", "3B", "LF", "RF", "1B"]
            for pos in _PRIORITY:
                if pos in estimates and estimates[pos] >= _EST_THRESHOLDS.get(pos, 50):
                    if pos in ("LF", "RF"):
                        return "COF"
                    return pos
            return "1B"

    # Last resort: use listed game position
    pos_fallback = {"2": "C", "3": "1B", "4": "2B", "5": "3B", "6": "SS",
                    "7": "COF", "8": "CF", "9": "COF", "10": "COF"}
    return pos_fallback.get(pos_str, "1B")
