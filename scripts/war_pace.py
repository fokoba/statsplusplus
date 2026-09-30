"""
war_pace.py — Current-season WAR pace and "on pace for a historic season" tag.

2026-09-30: built after the user flagged two PPL hitters (Danny Lacefield,
Mark Joseph) on pace for 9.4/9.2 WAR. Prorates a player's actual
current-season WAR to a full-season equivalent, using the league's real
games_per_season (not a blind 162 — see league_config.games_per_season).

Usage:
  python3 scripts/war_pace.py <player_id>
"""

import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from statsplusplus.config.league_context import get_league_dir, get_active_league_slug
from statsplusplus.config.league_config import games_per_season as _games_per_season_pkg
from statsplusplus.data.db import get_connection
from statsplusplus.evaluation.war import (
    war_pace as _war_pace_pkg, HISTORIC_PACE_WAR, MIN_PACE_SAMPLE_G, MIN_PACE_SAMPLE_IP,
    pace_tier as _pace_tier_pkg, pace_confidence as _pace_confidence_pkg,
)

_PITCHER_ROLES = (11, 12, 13)


def _tier_fields(pace_war):
    css, label = _pace_tier_pkg(pace_war)
    return {"pace_tier": css, "pace_tier_label": label}


def _min_sample_for(bucket):
    return MIN_PACE_SAMPLE_IP if bucket in ("SP", "RP") else MIN_PACE_SAMPLE_G


def _bucket_for(pos, role):
    """Coarse SP/RP-vs-hitter split — the pace calc only needs to pick the
    right full-season denominator (games vs. IP, SP vs. RP target), not the
    fine fielding bucket contract_value.py uses elsewhere."""
    if role in _PITCHER_ROLES:
        return "RP" if role in (12, 13) else "SP"
    return "HIT"


def get_war_pace(player_id, league_dir=None, conn=None):
    """Current-season WAR pace for one player, or None if no current-season
    MLB sample exists or the sample is below the minimum-trust threshold.

    Returns a dict: {war_to_date, sample, sample_unit, pace_war, is_historic,
    games_per_season, year} or None.
    """
    league_dir = league_dir or get_league_dir(get_active_league_slug())
    own_conn = conn is None
    conn = conn or get_connection(league_dir)
    try:
        row = conn.execute(
            "SELECT pos, role FROM players WHERE player_id=?", (player_id,)
        ).fetchone()
        if not row:
            return None
        bucket = _bucket_for(row["pos"], row["role"])
        gps = _games_per_season_pkg(league_dir)

        if bucket in ("SP", "RP"):
            cy = conn.execute("SELECT MAX(year) FROM mlb_pitching_stats").fetchone()[0]
            if cy is None:
                return None
            s = conn.execute(
                "SELECT ip, war FROM mlb_pitching_stats WHERE player_id=? AND year=? AND split_id=1",
                (player_id, cy),
            ).fetchone()
            if not s or s["ip"] is None or s["ip"] < MIN_PACE_SAMPLE_IP:
                return None
            sample, unit = s["ip"], "IP"
        else:
            cy = conn.execute("SELECT MAX(year) FROM mlb_batting_stats").fetchone()[0]
            if cy is None:
                return None
            s = conn.execute(
                "SELECT g, war FROM mlb_batting_stats WHERE player_id=? AND year=? AND split_id=1",
                (player_id, cy),
            ).fetchone()
            if not s or s["g"] is None or s["g"] < MIN_PACE_SAMPLE_G:
                return None
            sample, unit = s["g"], "G"

        if s["war"] is None:
            return None
        pace = _war_pace_pkg(float(s["war"]), float(sample), bucket, games_per_season=gps)
        if pace is None:
            return None
        return {
            "war_to_date": round(float(s["war"]), 2),
            "sample": sample,
            "sample_unit": unit,
            "pace_war": pace,
            "is_historic": pace >= HISTORIC_PACE_WAR,
            "games_per_season": gps,
            "year": cy,
            "pace_confidence": _pace_confidence_pkg(float(sample), _min_sample_for(bucket)),
            **_tier_fields(pace),
        }
    finally:
        if own_conn:
            conn.close()


def get_all_war_paces(league_dir=None, conn=None):
    """Bulk version — {player_id: pace_dict} for every MLB player in the
    league with a current-season sample above the trust threshold. Used by
    the All Minor Leagues (40-man) page and any other multi-player list
    that needs the historic-pace tag without one query per player."""
    league_dir = league_dir or get_league_dir(get_active_league_slug())
    own_conn = conn is None
    conn = conn or get_connection(league_dir)
    try:
        gps = _games_per_season_pkg(league_dir)
        out = {}

        cy_h = conn.execute("SELECT MAX(year) FROM mlb_batting_stats").fetchone()[0]
        if cy_h is not None:
            rows = conn.execute(
                "SELECT player_id, g, war FROM mlb_batting_stats "
                "WHERE year=? AND split_id=1 AND g >= ? AND war IS NOT NULL",
                (cy_h, MIN_PACE_SAMPLE_G),
            ).fetchall()
            for r in rows:
                pace = _war_pace_pkg(float(r["war"]), float(r["g"]), "HIT", games_per_season=gps)
                if pace is None:
                    continue
                out[r["player_id"]] = {
                    "war_to_date": round(float(r["war"]), 2), "sample": r["g"], "sample_unit": "G",
                    "pace_war": pace, "is_historic": pace >= HISTORIC_PACE_WAR,
                    "games_per_season": gps, "year": cy_h,
                    "pace_confidence": _pace_confidence_pkg(float(r["g"]), MIN_PACE_SAMPLE_G),
                    **_tier_fields(pace),
                }

        cy_p = conn.execute("SELECT MAX(year) FROM mlb_pitching_stats").fetchone()[0]
        if cy_p is not None:
            role_map = dict(conn.execute("SELECT player_id, role FROM players").fetchall())
            rows = conn.execute(
                "SELECT player_id, ip, war FROM mlb_pitching_stats "
                "WHERE year=? AND split_id=1 AND ip >= ? AND war IS NOT NULL",
                (cy_p, MIN_PACE_SAMPLE_IP),
            ).fetchall()
            for r in rows:
                bucket = "RP" if role_map.get(r["player_id"]) in (12, 13) else "SP"
                pace = _war_pace_pkg(float(r["war"]), float(r["ip"]), bucket, games_per_season=gps)
                if pace is None:
                    continue
                out[r["player_id"]] = {
                    "war_to_date": round(float(r["war"]), 2), "sample": r["ip"], "sample_unit": "IP",
                    "pace_war": pace, "is_historic": pace >= HISTORIC_PACE_WAR,
                    "games_per_season": gps, "year": cy_p,
                    "pace_confidence": _pace_confidence_pkg(float(r["ip"]), MIN_PACE_SAMPLE_IP),
                    **_tier_fields(pace),
                }

        return out
    finally:
        if own_conn:
            conn.close()


def get_all_teams_combined_pace(league_dir=None, conn=None):
    """{team_id: {combined_pace, n_qualifying}} — sum of pace_war across
    each team's qualifying active (MLB-level) roster players. Powers the
    team-vs-team combined-pace comparison box on the team page. A team with
    no qualifying players is simply absent from the returned dict (0
    combined pace, not a meaningful comparison point)."""
    league_dir = league_dir or get_league_dir(get_active_league_slug())
    own_conn = conn is None
    conn = conn or get_connection(league_dir)
    try:
        paces = get_all_war_paces(league_dir=league_dir, conn=conn)
        if not paces:
            return {}
        pid_qs = ",".join("?" * len(paces))
        rows = conn.execute(
            f"SELECT player_id, team_id FROM players WHERE level='1' AND player_id IN ({pid_qs})",
            list(paces.keys()),
        ).fetchall()
        out = {}
        for r in rows:
            tid = r["team_id"]
            if not tid:
                continue
            pace = paces[r["player_id"]]["pace_war"]
            entry = out.setdefault(tid, {"combined_pace": 0.0, "n_qualifying": 0})
            entry["combined_pace"] = round(entry["combined_pace"] + pace, 2)
            entry["n_qualifying"] += 1
        return out
    finally:
        if own_conn:
            conn.close()


def get_team_combined_pace(team_id, league_dir=None, conn=None):
    """Single-team convenience wrapper around get_all_teams_combined_pace."""
    return get_all_teams_combined_pace(league_dir=league_dir, conn=conn).get(
        team_id, {"combined_pace": 0.0, "n_qualifying": 0}
    )


if __name__ == "__main__":
    import json
    pid = int(sys.argv[1])
    print(json.dumps(get_war_pace(pid), indent=2))
