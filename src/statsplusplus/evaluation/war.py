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


# ---------------------------------------------------------------------------
# Current-season WAR pace (2026-09-30)
# ---------------------------------------------------------------------------

HISTORIC_PACE_WAR = 8.0     # pace_war at/above this = "historic pace" tag
MIN_PACE_SAMPLE_G = 20      # hitters: minimum games this season to trust a pace
MIN_PACE_SAMPLE_IP = 30.0   # pitchers: minimum innings this season to trust a pace

# Pace tiers (2026-09-30) — standard sabermetric bands (same cutoffs both
# leagues, per the user's own choice — not per-league calibrated like
# PEAK_AGE_HITTER/PITCHER). Checked in order, first match wins; below the
# last threshold falls through to "replacement".
PACE_TIERS: list[tuple[float, str, str]] = [
    (HISTORIC_PACE_WAR, "historic", "Historic"),
    (5.0, "allstar", "All-Star"),
    (2.0, "everyday", "Everyday"),
    (0.0, "reserve", "Reserve"),
]
PACE_TIER_REPLACEMENT = ("replacement", "Replacement")


def pace_tier(pace_war: Optional[float]) -> tuple[Optional[str], Optional[str]]:
    """(css_class, label) for a prorated pace_war, or (None, None) if
    pace_war is None. See PACE_TIERS."""
    if pace_war is None:
        return None, None
    for threshold, css, label in PACE_TIERS:
        if pace_war >= threshold:
            return css, label
    return PACE_TIER_REPLACEMENT

# Pace confidence (2026-09-30) — MIN_PACE_SAMPLE_G/IP is a hard cutoff, not a
# smooth confidence signal: a hitter at exactly 20 games can produce a wildly
# unstable prorated pace (confirmed in PPL — two "historic pace" hitters sat
# right at the 20-21 game minimum with 12.97/14.65 WAR paces, almost
# certainly small-sample noise, displayed with the same visual weight as a
# much steadier 32-game, 9.4 WAR read). This doesn't move the floor — it
# grades how far *past* it the player is, mirroring dev_speed.py's
# confidence_tier() pattern (playing-time-in-window -> High/Medium/Low).
# Multiples of the minimum sample, not absolute games/IP, so hitters and
# pitchers share one scale despite different units/thresholds:
#   High   >= 2.0x minimum — pace has roughly doubled the trust floor
#   Medium >= 1.0x and < 2.0x minimum
#   Low    < 1.0x minimum (shouldn't normally reach here — get_war_pace already
#            gates below the floor — kept as a floor-case fallback, not a live band)
PACE_CONFIDENCE_HIGH_MULT = 2.0


def pace_confidence(sample_to_date: float, min_sample: float) -> str:
    """"High"/"Medium"/"Low" confidence in a war_pace reading, from how far
    past the minimum trusted sample (MIN_PACE_SAMPLE_G/IP) the player
    actually is. See PACE_CONFIDENCE_HIGH_MULT above for the reasoning."""
    if min_sample <= 0 or sample_to_date < min_sample:
        return "Low"
    ratio = sample_to_date / min_sample
    if ratio >= PACE_CONFIDENCE_HIGH_MULT:
        return "High"
    return "Medium"

# Full-season IP targets at a 162-game reference (SP/RP), scaled by the
# league's actual games_per_season elsewhere — mirrors scripts/projections.py's
# _SP_FULL_IP/_RP_FULL_IP, kept here too since this module has no import on
# that one (projections.py is the pure/no-DB layer; this is the reverse
# direction — DB-adjacent evaluation code importing a display constant would
# be a layering inversion).
FULL_SEASON_SP_IP_162 = 200.0
FULL_SEASON_RP_IP_162 = 65.0


def war_pace(
    war_to_date: float,
    sample_to_date: float,
    bucket: str,
    games_per_season: int = 162,
) -> Optional[float]:
    """Prorate a player's current-season WAR to a full-season pace.

    Pure function — sample_to_date is games played (hitters) or innings
    pitched (pitchers, use bucket in ("SP","RP") to select the right
    full-season target). Returns None if sample_to_date <= 0.

    Validated (2026-09-30) against two real PPL hitters mid-flagged by the
    user as "on pace for 9.4/9.2 WAR": war_to_date/games_played prorated
    over a 154-game season (not 162 — PPL is a real 1955-era 154-game
    schedule) landed at 9.39 and 9.22, matching almost exactly — 162 would
    have overshot to ~9.9/9.7. See games_per_season() in league_config.py.
    """
    if sample_to_date is None or sample_to_date <= 0:
        return None
    is_pitcher = bucket in ("SP", "RP")
    if is_pitcher:
        scale = games_per_season / 162.0
        full_target = (FULL_SEASON_RP_IP_162 if bucket == "RP" else FULL_SEASON_SP_IP_162) * scale
        return round((war_to_date / sample_to_date) * full_target, 2)
    return round((war_to_date / sample_to_date) * games_per_season, 2)


def aging_mult(age: int | float, bucket: str, weights: Optional[Any] = None) -> float:
    """Aging curve multiplier on peak WAR.

    Interpolates between defined age points. Returns 1.0 for ages at or
    below peak, declines thereafter.

    When `weights` (a ModelWeights instance) is provided, uses league-
    calibrated aging curves if available. Otherwise falls back to defaults.

    Args:
        age: Player's current age.
        bucket: Positional bucket (pitcher aging is steeper).
        weights: Optional ModelWeights with league-calibrated curves.

    Returns:
        Multiplier in [0, 1.0].
    """
    if weights is not None and hasattr(weights, "aging_curve_hitter"):
        if bucket in ("SP", "RP"):
            table = weights.aging_curve_pitcher
        else:
            table = weights.aging_curve_hitter
    else:
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


# ---------------------------------------------------------------------------
# Stat history loading (DB access)
# ---------------------------------------------------------------------------


def load_stat_history(
    conn: Any,
    game_date: str,
    dh_rule: str = "Universal DH",
    games_per_season: int = 162,
) -> tuple[dict[int, list[dict[str, Any]]], dict[int, list[dict[str, Any]]], set[int]]:
    """Load season stats into memory for WAR projection.

    Args:
        conn: SQLite connection with sqlite3.Row row_factory.
        game_date: Current game date string (YYYY-MM-DD).
        dh_rule: League DH rule ("No DH", "Universal DH", "AL Only DH").
            Affects the AB threshold for two-way player detection.
        games_per_season: League's full-season schedule length, e.g. from
            statsplusplus.config.league_config.games_per_season(league_dir).
            Defaults to 162; pass the league-specific value where available.

    Returns:
        Tuple of (bat_hist, pit_hist, two_way_pids):
        - bat_hist: {player_id: [{year, war, incomplete, season_pct}, ...]}
        - pit_hist: {player_id: [{year, war, is_sp, incomplete, season_pct}, ...]}
        - two_way_pids: set of player IDs who qualify as two-way
    """
    game_year = int(game_date[:4])
    game_month = int(game_date[5:7])

    if game_month >= 11:
        season_pct = 1.0
    else:
        max_g = conn.execute(
            """SELECT MAX(cnt) FROM (
                SELECT COUNT(*) as cnt FROM games
                WHERE date LIKE ? AND played=1 AND game_type=0
                GROUP BY home_team)""",
            (f"{game_year}%",)
        ).fetchone()
        season_pct = min((max_g[0] or 0) / float(games_per_season), 1.0) if max_g and max_g[0] else 0.0

    cutoff_year = game_year + 1

    bat_rows = conn.execute(
        """SELECT player_id, year, SUM(war) as war, SUM(ab) as ab,
                  MAX(stint) as max_stint, COUNT(team_id) as team_count
           FROM mlb_batting_stats WHERE split_id=1 AND year < ?
           GROUP BY player_id, year""", (cutoff_year,)
    ).fetchall()
    pit_rows = conn.execute(
        """SELECT player_id, year,
                  SUM((war + COALESCE(ra9war, war)) / 2.0) as war,
                  SUM(gs) as gs, SUM(ip) as ip,
                  MAX(stint) as max_stint, COUNT(team_id) as team_count
           FROM mlb_pitching_stats WHERE split_id=1 AND year < ?
           GROUP BY player_id, year""", (cutoff_year,)
    ).fetchall()

    bat_hist: dict[int, list[dict[str, Any]]] = {}
    for r in bat_rows:
        if (r["ab"] or 0) < 130:
            continue
        incomplete = (r["max_stint"] == 1 and r["team_count"] == 1)
        is_current = r["year"] == game_year
        bat_hist.setdefault(r["player_id"], []).append(
            {"year": r["year"], "war": r["war"] or 0, "incomplete": incomplete,
             "season_pct": season_pct if is_current else 1.0})

    pit_hist: dict[int, list[dict[str, Any]]] = {}
    for r in pit_rows:
        incomplete = (r["max_stint"] == 1 and r["team_count"] == 1)
        is_current = r["year"] == game_year
        pit_hist.setdefault(r["player_id"], []).append(
            {"year": r["year"], "war": r["war"] or 0,
             "is_sp": (r["gs"] or 0) >= 10, "incomplete": incomplete,
             "season_pct": season_pct if is_current else 1.0})

    # Two-way detection
    two_way: set[int] = set()
    bat_by_year: dict[int, set[int]] = {}
    ab_thresh = 250 if dh_rule == "No DH" else 130
    for r in bat_rows:
        if (r["ab"] or 0) >= ab_thresh:
            bat_by_year.setdefault(r["player_id"], set()).add(r["year"])
    for r in pit_rows:
        if (r["gs"] or 0) >= 10:
            pid = r["player_id"]
            if pid in bat_by_year and r["year"] in bat_by_year[pid]:
                two_way.add(pid)

    for d in (bat_hist, pit_hist):
        for pid in d:
            d[pid].sort(key=lambda x: x["year"], reverse=True)

    return bat_hist, pit_hist, two_way
