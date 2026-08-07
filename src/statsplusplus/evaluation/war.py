"""WAR projection and aging curves.

Pure computation: takes scores/ages/stat history, returns WAR estimates.
No DB access, no global state.

Public API:
    peak_war_from_score(score, bucket, weights) -> float
    aging_mult(age, bucket) -> float
    stat_peak_war(pid, bucket, bat_hist, pit_hist, two_way) -> float | None
"""

from __future__ import annotations

from typing import Any, Optional

from statsplusplus.evaluation.constants import (
    AGING_HITTER,
    AGING_PITCHER,
    OVR_TO_WAR_DEFAULT,
    ModelWeights,
)


# ---------------------------------------------------------------------------
# Interpolation helpers
# ---------------------------------------------------------------------------

def _interp_table(table_rows: list[tuple[int, float, float, float]], value: int, col_idx: int) -> float:
    """Interpolate from the default OVR_TO_WAR table (descending OVR order)."""
    for i in range(len(table_rows) - 1):
        v0, v1 = table_rows[i][0], table_rows[i + 1][0]
        if v1 <= value <= v0:
            t = (value - v1) / (v0 - v1)
            return table_rows[i + 1][col_idx] + t * (table_rows[i][col_idx] - table_rows[i + 1][col_idx])
    if value >= table_rows[0][0]:
        return table_rows[0][col_idx]
    return table_rows[-1][col_idx]


def _interp_dict(tbl: dict[int, float], value: int | float) -> float:
    """Interpolate from a {score: war} dict with sorted integer keys."""
    pts = sorted(tbl.keys())
    if value >= pts[-1]:
        return tbl[pts[-1]]
    if value <= pts[0]:
        return tbl[pts[0]]
    for i in range(len(pts) - 1):
        if pts[i] <= value <= pts[i + 1]:
            t = (value - pts[i]) / (pts[i + 1] - pts[i])
            return tbl[pts[i]] + t * (tbl[pts[i + 1]] - tbl[pts[i]])
    return tbl[pts[0]]


# ---------------------------------------------------------------------------
# WAR projection
# ---------------------------------------------------------------------------

def peak_war_from_score(
    score: int | float,
    bucket: str,
    weights: Optional[ModelWeights] = None,
) -> float:
    """Project peak WAR/season from a composite score and positional bucket.

    Uses COMPOSITE_TO_WAR tables when available (from calibrated weights),
    falls back to OVR_TO_WAR tables, then to the hardcoded default table.

    Args:
        score: Composite score or OVR on the 20-80 scale.
        bucket: Positional bucket (e.g., "SS", "SP", "RP").
        weights: Calibrated model weights. If None, uses defaults only.

    Returns:
        Projected peak WAR per season.
    """
    if weights is not None:
        # Prefer COMPOSITE_TO_WAR
        comp_war = weights.composite_to_war
        if comp_war and bucket in comp_war:
            return _interp_dict(comp_war[bucket], score)
        # Fall back to calibrated OVR_TO_WAR
        ovr_war = weights.ovr_to_war
        if ovr_war and bucket in ovr_war:
            return _interp_dict(ovr_war[bucket], score)

    # Final fallback: default table
    col = 2 if bucket == "SP" else (3 if bucket == "RP" else 1)
    return _interp_table(OVR_TO_WAR_DEFAULT, int(score), col)


def aging_mult(age: int | float, bucket: str) -> float:
    """Aging curve multiplier on peak WAR.

    Interpolates between defined age points. Returns 1.0 for ages at or
    below peak, declines thereafter.

    Args:
        age: Player's current age.
        bucket: Positional bucket (pitcher aging is steeper).

    Returns:
        Multiplier in [0, 1.0].
    """
    table = AGING_PITCHER if bucket in ("SP", "RP") else AGING_HITTER
    ages = sorted(table)
    if age <= ages[0]:
        return 1.0
    if age >= ages[-1]:
        return table[ages[-1]]
    for i in range(len(ages) - 1):
        a0, a1 = ages[i], ages[i + 1]
        if a0 <= age <= a1:
            t = (age - a0) / (a1 - a0)
            return table[a0] + t * (table[a1] - table[a0])
    return 0.35


# ---------------------------------------------------------------------------
# Stat history WAR projection
# ---------------------------------------------------------------------------

# Weighting scheme: 4-year window, recent-heavy.
_STAT_WEIGHTS: list[float] = [3.0, 3.0, 2.0, 1.0]

# Role-convert discount factors
_RP_FROM_SP_MULT: float = 0.46
_SP_FROM_RP_MULT: float = 2.15


def stat_peak_war(
    pid: int,
    bucket: str,
    bat_hist: dict[int, list[dict[str, Any]]],
    pit_hist: dict[int, list[dict[str, Any]]],
    two_way: Optional[set[int]] = None,
) -> Optional[float]:
    """Weighted WAR average from stat history for peak WAR projection.

    Uses a 4-year window with weights [3, 3, 2, 1]. The most recent year's
    weight is scaled by its season_pct (partial-season proportional weighting).

    For pitchers who changed roles (SP↔RP), blends new-role and prior-role
    history with a discount factor.

    Args:
        pid: Player ID.
        bucket: Positional bucket.
        bat_hist: {player_id: [season_dicts]} for batting (most recent first).
        pit_hist: {player_id: [season_dicts]} for pitching.
        two_way: Set of player IDs identified as two-way players.

    Returns:
        Projected peak WAR, or None if no qualifying history.
    """
    if two_way and pid in two_way:
        return _two_way_peak_war(pid, bat_hist, pit_hist)

    if bucket in ("SP", "RP"):
        is_sp = bucket == "SP"
        new_role_seasons = [s for s in pit_hist.get(pid, []) if s.get("is_sp") == is_sp]
        old_role_seasons = [s for s in pit_hist.get(pid, []) if s.get("is_sp") != is_sp]

        if new_role_seasons:
            new_role_war = _weighted_war(new_role_seasons)

            new_role_full_seasons = sum(
                1 for s in new_role_seasons
                if float(s.get("season_pct") or 1.0) >= 0.8
            )
            if old_role_seasons and new_role_full_seasons < 2:
                old_role_war = _weighted_war(old_role_seasons)
                discount = _RP_FROM_SP_MULT if bucket == "RP" else _SP_FROM_RP_MULT
                old_role_war *= discount
                new_equiv = sum(
                    float(s.get("season_pct") or 1.0) for s in new_role_seasons[:4]
                )
                blend_weight = min(new_equiv / 2.0, 1.0)
                return blend_weight * new_role_war + (1 - blend_weight) * old_role_war
            return new_role_war

        elif old_role_seasons:
            result = _weighted_war(old_role_seasons)
            result *= _RP_FROM_SP_MULT if bucket == "RP" else _SP_FROM_RP_MULT
            return result

        return None
    else:
        seasons = bat_hist.get(pid, [])
        if not seasons:
            return None
        return _weighted_war(seasons)


def _weighted_war(seasons: list[dict[str, Any]]) -> float:
    """Compute weighted WAR from season list (most recent first)."""
    weights = list(_STAT_WEIGHTS[:len(seasons)])
    # Scale most recent year's weight by season completion fraction
    weights[0] = weights[0] * float(seasons[0].get("season_pct", 1.0))
    effective_wars = [
        float(s["war"]) / (0.5 if s.get("incomplete") else 1.0)
        for s in seasons[:len(weights)]
    ]
    total_weight = sum(weights)
    if total_weight == 0:
        return 0.0
    return sum(w * ew for w, ew in zip(weights, effective_wars)) / total_weight


def _two_way_peak_war(
    pid: int,
    bat_hist: dict[int, list[dict[str, Any]]],
    pit_hist: dict[int, list[dict[str, Any]]],
) -> Optional[float]:
    """WAR projection for two-way players (combined batting + pitching)."""
    bat_by_yr: dict[int, float] = {int(s["year"]): float(s["war"]) for s in bat_hist.get(pid, [])}
    pit_by_yr: dict[int, float] = {int(s["year"]): float(s["war"]) for s in pit_hist.get(pid, [])}
    years = sorted(set(bat_by_yr) | set(pit_by_yr), reverse=True)
    if not years:
        return None
    combined = [bat_by_yr.get(y, 0.0) + pit_by_yr.get(y, 0.0) for y in years[:4]]
    weights = list(_STAT_WEIGHTS[:len(combined)])
    total = sum(float(w) * c for w, c in zip(weights, combined))
    return total / sum(weights)
