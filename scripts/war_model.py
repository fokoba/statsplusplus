"""
war_model.py — WAR projection and stat history utilities.

Provides WAR estimation from Ovr ratings and aging curves, plus stat history
loading for the MLB contract surplus model. No DB schema knowledge — takes
connections as parameters.

Public API:
  peak_war_from_ovr(ovr, bucket)                    → float
  aging_mult(age, bucket)                            → float
  load_stat_history(conn, game_date)                 → (bat_hist, pit_hist, two_way)
  stat_peak_war(pid, bucket, bat_hist, pit_hist, ...) → float | None
"""

from constants import OVR_TO_WAR, OVR_TO_WAR_CALIBRATED, AGING_HITTER, AGING_PITCHER

# ---------------------------------------------------------------------------
# WAR interpolation helpers
# ---------------------------------------------------------------------------

def _interp(table_rows, value, col_idx):
    for i in range(len(table_rows) - 1):
        v0, v1 = table_rows[i][0], table_rows[i+1][0]
        if v1 <= value <= v0:
            t = (value - v1) / (v0 - v1)
            return table_rows[i+1][col_idx] + t * (table_rows[i][col_idx] - table_rows[i+1][col_idx])
    if value >= table_rows[0][0]: return table_rows[0][col_idx]
    return table_rows[-1][col_idx]


def _interp_dict(tbl, ovr):
    pts = sorted(tbl.keys())
    if ovr >= pts[-1]: return tbl[pts[-1]]
    if ovr <= pts[0]:  return tbl[pts[0]]
    for i in range(len(pts) - 1):
        if pts[i] <= ovr <= pts[i + 1]:
            t = (ovr - pts[i]) / (pts[i + 1] - pts[i])
            return tbl[pts[i]] + t * (tbl[pts[i + 1]] - tbl[pts[i]])
    return tbl[pts[0]]


# ---------------------------------------------------------------------------
# WAR projection
# ---------------------------------------------------------------------------

def peak_war_from_score(score, bucket):
    """Project peak WAR/season from a score (Composite_Score or OVR) and positional bucket.

    Uses COMPOSITE_TO_WAR tables when available, falls back to calibrated
    OVR_TO_WAR tables, then to default OVR_TO_WAR.

    This is the canonical WAR projection function. Both Composite_Score and OVR
    are on the 20-80 scale, so the same interpolation logic applies.
    """
    from constants import COMPOSITE_TO_WAR
    # Prefer COMPOSITE_TO_WAR when available
    if COMPOSITE_TO_WAR and bucket in COMPOSITE_TO_WAR:
        return _interp_dict(COMPOSITE_TO_WAR[bucket], score)
    # Fall back to calibrated OVR_TO_WAR
    if OVR_TO_WAR_CALIBRATED and bucket in OVR_TO_WAR_CALIBRATED:
        return _interp_dict(OVR_TO_WAR_CALIBRATED[bucket], score)
    col = 2 if bucket == "SP" else (3 if bucket == "RP" else 1)
    return _interp(OVR_TO_WAR, score, col)


def peak_war_from_ovr(ovr, bucket):
    """Backward-compatible alias for peak_war_from_score().

    Accepts either OVR or Composite_Score — both are on the 20-80 scale.
    """
    return peak_war_from_score(ovr, bucket)


def aging_mult(age, bucket):
    """Aging curve multiplier on peak WAR. Interpolated between defined age points."""
    table = AGING_PITCHER if bucket in ("SP", "RP") else AGING_HITTER
    ages  = sorted(table)
    if age <= ages[0]:  return 1.0
    if age >= ages[-1]: return table[ages[-1]]
    for i in range(len(ages) - 1):
        a0, a1 = ages[i], ages[i+1]
        if a0 <= age <= a1:
            t = (age - a0) / (a1 - a0)
            return table[a0] + t * (table[a1] - table[a0])
    return 0.35


# ---------------------------------------------------------------------------
# Stat history (used by contract_value.py and fv_calc.py)
# ---------------------------------------------------------------------------

def load_stat_history(conn, game_date):
    """Load season stats into memory for WAR projection.

    Includes the current year's stats with a season_pct field indicating
    completeness (games_played / 162). Completed prior seasons have
    season_pct = 1.0. Aggregates across teams for traded players.
    """
    game_year = int(game_date[:4])
    game_month = int(game_date[5:7])

    # Always include current year stats — partial-season weighting is handled
    # downstream in stat_peak_war via the season_pct field.
    cutoff_year = game_year + 1

    # Determine season completion fraction for the current year.
    # Offseason (Nov+): season is complete.
    # Mid-season: estimate from max games played by any team this year.
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
        season_pct = min((max_g[0] or 0) / 162.0, 1.0) if max_g and max_g[0] else 0.0

    bat_rows = conn.execute(
        """SELECT player_id, year, SUM(war) as war, SUM(ab) as ab,
                  MAX(stint) as max_stint, COUNT(team_id) as team_count
           FROM batting_stats WHERE split_id=1 AND year < ?
           GROUP BY player_id, year""", (cutoff_year,)
    ).fetchall()
    pit_rows = conn.execute(
        """SELECT player_id, year,
                  SUM((war + COALESCE(ra9war, war)) / 2.0) as war,
                  SUM(gs) as gs, SUM(ip) as ip,
                  MAX(stint) as max_stint, COUNT(team_id) as team_count
           FROM pitching_stats WHERE split_id=1 AND year < ?
           GROUP BY player_id, year""", (cutoff_year,)
    ).fetchall()

    bat_hist = {}
    for r in bat_rows:
        if (r["ab"] or 0) < 130:
            continue
        incomplete = (r["max_stint"] == 1 and r["team_count"] == 1)
        is_current = r["year"] == game_year
        bat_hist.setdefault(r["player_id"], []).append(
            {"year": r["year"], "war": r["war"] or 0, "incomplete": incomplete,
             "season_pct": season_pct if is_current else 1.0})

    pit_hist = {}
    for r in pit_rows:
        incomplete = (r["max_stint"] == 1 and r["team_count"] == 1)
        is_current = r["year"] == game_year
        pit_hist.setdefault(r["player_id"], []).append(
            {"year": r["year"], "war": r["war"] or 0,
             "is_sp": (r["gs"] or 0) >= 10, "incomplete": incomplete,
             "season_pct": season_pct if is_current else 1.0})

    two_way = set()
    bat_by_year = {}
    # In no-DH leagues, pitchers accumulate AB from batting in their lineup spot.
    # Use higher threshold to avoid flagging every SP as two-way.
    from league_config import config as _cfg
    ab_thresh = 250 if _cfg.settings.get("dh_rule") == "No DH" else 130
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


# Weighting scheme for stat_peak_war: 4-year window, recent-heavy.
_STAT_WEIGHTS = [3, 3, 2, 1]

# Role-convert discount: applied when blending prior-role seasons.
_RP_FROM_SP_MULT = 0.46
_SP_FROM_RP_MULT = 2.15


def stat_peak_war(pid, bucket, bat_hist, pit_hist, two_way=None):
    """Weighted WAR average from stat history for peak WAR projection.

    Uses a 4-year window with weights [3, 3, 2, 1]. The most recent year's
    weight is scaled by its season_pct (e.g., a half-season gets weight 1.5
    instead of 3). This allows partial current-year data to influence the
    projection proportionally to sample size.

    For pitchers who recently changed roles (SP↔RP), blends new-role and
    prior-role history rather than ignoring the prior role entirely.
    """
    if two_way and pid in two_way:
        return _two_way_peak_war(pid, bucket, bat_hist, pit_hist)

    if bucket in ("SP", "RP"):
        is_sp = bucket == "SP"
        new_role_seasons = [s for s in pit_hist.get(pid, []) if s["is_sp"] == is_sp]
        old_role_seasons = [s for s in pit_hist.get(pid, []) if s["is_sp"] != is_sp]

        if new_role_seasons:
            # Has data in current role — compute projection from it
            new_role_war = _weighted_war(new_role_seasons)

            # If there's also prior-role history AND limited new-role data (< 2 full seasons),
            # blend with discounted prior-role projection for stability.
            new_role_full_seasons = sum(1 for s in new_role_seasons if s.get("season_pct", 1.0) >= 0.8)
            if old_role_seasons and new_role_full_seasons < 2:
                old_role_war = _weighted_war(old_role_seasons)
                discount = _RP_FROM_SP_MULT if bucket == "RP" else _SP_FROM_RP_MULT
                old_role_war *= discount
                # Blend: weight new-role data by number of full-equivalent seasons
                new_equiv = sum(s.get("season_pct", 1.0) for s in new_role_seasons[:4])
                blend_weight = min(new_equiv / 2.0, 1.0)  # At 2 full seasons, fully trust new role
                return blend_weight * new_role_war + (1 - blend_weight) * old_role_war
            return new_role_war

        elif old_role_seasons:
            # No data in current role — fall back to prior role with discount
            result = _weighted_war(old_role_seasons)
            result *= _RP_FROM_SP_MULT if bucket == "RP" else _SP_FROM_RP_MULT
            return result

        return None
    else:
        seasons = bat_hist.get(pid, [])
        if not seasons:
            return None
        return _weighted_war(seasons)


def _weighted_war(seasons):
    """Compute weighted WAR from a list of seasons (most recent first).

    Uses _STAT_WEIGHTS [3, 3, 2, 1] for up to 4 seasons. The most recent
    season's weight is scaled by its season_pct field.
    """
    weights = list(_STAT_WEIGHTS[:len(seasons)])
    # Scale most recent year's weight by season completion fraction
    weights[0] = weights[0] * seasons[0].get("season_pct", 1.0)
    effective_wars = [s["war"] / (0.5 if s.get("incomplete") else 1.0) for s in seasons]
    total_weight = sum(weights)
    if total_weight == 0:
        return None
    return sum(w * ew for w, ew in zip(weights, effective_wars)) / total_weight


def _two_way_peak_war(pid, bucket, bat_hist, pit_hist):
    bat_by_yr = {s["year"]: s["war"] for s in bat_hist.get(pid, [])}
    pit_by_yr = {s["year"]: s["war"] for s in pit_hist.get(pid, [])}
    years = sorted(set(bat_by_yr) | set(pit_by_yr), reverse=True)
    if not years:
        return None
    combined = [bat_by_yr.get(y, 0) + pit_by_yr.get(y, 0) for y in years[:4]]
    weights = list(_STAT_WEIGHTS[:len(combined)])
    return sum(w * c for w, c in zip(weights, combined)) / sum(weights)
