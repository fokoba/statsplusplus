"""
war_model.py — WAR projection and stat history utilities.

MIGRATION NOTE: Pure computation functions delegate to
statsplusplus.evaluation.war. The stat history loader (load_stat_history)
remains here as it performs DB I/O which doesn't belong in the pure
evaluation layer.

Public API:
  peak_war_from_ovr(ovr, bucket)                    → float
  peak_war_from_score(score, bucket)                → float
  aging_mult(age, bucket)                            → float
  load_stat_history(conn, game_date)                 → (bat_hist, pit_hist, two_way)
  stat_peak_war(pid, bucket, bat_hist, pit_hist, ...) → float | None
"""

# Re-export pure computation from the package
from statsplusplus.evaluation.war import (
    peak_war_from_score,
    aging_mult,
    stat_peak_war,
    _interp_table,
    _interp_dict,
)

# Backward-compatible alias
peak_war_from_ovr = peak_war_from_score

# Keep _interp as legacy alias used by tests
_interp = _interp_table


# ---------------------------------------------------------------------------
# Stat history (DB I/O — not in the pure evaluation layer)
# ---------------------------------------------------------------------------

def load_stat_history(conn, game_date):
    """Load season stats into memory for WAR projection.

    This function performs DB I/O and stays in the scripts layer until
    the data access patterns are fully migrated.
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
        season_pct = min((max_g[0] or 0) / 162.0, 1.0) if max_g and max_g[0] else 0.0

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


# Weighting scheme for stat_peak_war kept for any direct importers
_STAT_WEIGHTS = [3, 3, 2, 1]
_RP_FROM_SP_MULT = 0.46
_SP_FROM_RP_MULT = 2.15
