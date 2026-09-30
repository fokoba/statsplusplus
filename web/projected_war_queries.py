"""Projected end-of-season WAR — league-wide, empirically-calibrated.

Answers: given a player's observed performance this season (however much of
it has been played) plus their real season-by-season track record, what does
their FULL SEASON end up looking like — baseline, ceiling (top ~10% outcome),
and floor (bottom ~10% outcome) — and how does that compare in dollars to
what they're actually being paid this year?

This is a *separate* metric from the site's existing multi-year forward
surplus/market_value (contract_value.py) — it does not touch that
calculation. It only projects the CURRENT season's outcome from CURRENT
observed performance, for in-season trade-value comparisons.

Baseline: reuses the site's existing recency-weighted stat-history
projector (statsplusplus.evaluation.war.stat_peak_war) — a 4-year window
[3,3,2,1] weighted average, with the current (partial) season's weight
scaled by how much of the season has been played. Same function already
powering contract_value()'s player-value calc, so a player's baseline here
is consistent with their surplus/market-value elsewhere on the site.

Floor/ceiling: NOT a guessed multiplier. Backtested against every completed
player-season in this league's own database: for each such season, take the
same [3,3,2,1] weighted projection built ONLY from seasons before it, and
compare to what that player actually did. Bucket the resulting errors by
hitter/pitcher and by projected-WAR tier, and use the 10th/90th percentile
of the real error distribution as the floor/ceiling offset. Requires ~20+
historical seasons in a bucket to be used at all; falls back to a wider
neighboring tier, or is omitted (baseline still shown) if the league simply
doesn't have enough history yet.

Players are excluded outright (not just flagged) if either:
  - this season's own sample (AB for hitters, IP for pitchers) is still too
    small to mean anything, or
  - they have no qualifying prior season on record at all (true rookies) —
    there's nothing to backtest a floor/ceiling from.
"""

import os
import sys
from collections import defaultdict

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "scripts"))
from web_league_context import (get_db, get_cfg, team_abbr_map, team_names_map,
                                 mlb_team_ids, money_divisor as _money_divisor)

# Mirrors statsplusplus.evaluation.war._STAT_WEIGHTS exactly (kept as a
# local copy rather than importing a private name) — recency-weighted
# 4-year window, most-recent year's weight scaled by season completion.
_STAT_WEIGHTS = [3.0, 3.0, 2.0, 1.0]

# Hard minimum THIS season's sample to be included in the table at all.
# Below this, a player's current-season signal is close to pure noise.
_MIN_CURRENT_AB = 20
_MIN_CURRENT_IP = 8.0

# Minimum prior-season sample to count as a real "track record" — both for
# a live player's most recent prior season, and for a historical season
# used as backtest input.
_MIN_PRIOR_AB = 100
_MIN_PRIOR_IP = 15.0

# Tier edges bucket players by their weighted-history baseline WAR before
# looking up an empirical floor/ceiling offset — a 0.5-WAR bench bat and a
# 6-WAR star do not have the same realistic spread in raw WAR terms.
_HITTER_TIER_EDGES = [0.5, 1.5, 3.0, 5.0]     # -> 5 tiers
_PITCHER_TIER_EDGES = [0.0, 1.0, 2.5, 4.0]    # -> 5 tiers

# Minimum sample of historical player-seasons in a (group, tier) bucket
# before its empirical p10/p90 is trusted.
_MIN_BACKTEST_N = 20

# Sanity clamp only — guards against a pathological tier lookup, not meant
# to bind in normal use (real league WAR in this DB runs roughly -3 to +15).
_WAR_CLAMP = (-5.0, 20.0)


def _weighted_war(seasons):
    """seasons: most-recent-first list of {"war","season_pct","incomplete"}.
    Identical formula to war.py's private _weighted_war — duplicated here
    (not imported) so this module stays self-contained; keep in sync if
    that weighting scheme ever changes."""
    if not seasons:
        return None
    weights = list(_STAT_WEIGHTS[:len(seasons)])
    weights[0] = weights[0] * float(seasons[0].get("season_pct", 1.0))
    eff = [float(s["war"]) / (0.5 if s.get("incomplete") else 1.0)
           for s in seasons[:len(weights)]]
    total = sum(weights)
    if total == 0:
        return 0.0
    return sum(w * e for w, e in zip(weights, eff)) / total


def _load_full_history(conn):
    """Every player-year WAR total in the DB (all years), with AB/IP —
    used both for the historical backtest and to read a live player's
    current-season sample size and most recent prior season."""
    bat_rows = conn.execute(
        """SELECT player_id, year, SUM(war) as war, SUM(ab) as ab,
                  MAX(stint) as max_stint, COUNT(team_id) as team_count
           FROM mlb_batting_stats WHERE split_id=1
           GROUP BY player_id, year"""
    ).fetchall()
    pit_rows = conn.execute(
        """SELECT player_id, year,
                  SUM((war + COALESCE(ra9war, war)) / 2.0) as war,
                  SUM(ip) as ip, SUM(gs) as gs,
                  MAX(stint) as max_stint, COUNT(team_id) as team_count
           FROM mlb_pitching_stats WHERE split_id=1
           GROUP BY player_id, year"""
    ).fetchall()

    bat = defaultdict(list)
    for r in bat_rows:
        bat[r["player_id"]].append({
            "year": r["year"], "war": r["war"] or 0.0, "ab": r["ab"] or 0,
            "incomplete": (r["max_stint"] == 1 and r["team_count"] == 1),
        })
    pit = defaultdict(list)
    for r in pit_rows:
        pit[r["player_id"]].append({
            "year": r["year"], "war": r["war"] or 0.0, "ip": r["ip"] or 0.0,
            "gs": r["gs"] or 0,
            "incomplete": (r["max_stint"] == 1 and r["team_count"] == 1),
        })
    for d in (bat, pit):
        for pid in d:
            d[pid].sort(key=lambda x: x["year"], reverse=True)
    return bat, pit


def _tier(war, edges):
    for i, e in enumerate(edges):
        if war < e:
            return i
    return len(edges)


def _build_empirical_spread(bat_all, pit_all):
    """Backtest every completed player-season in this league's history:
    project it from ONLY the seasons before it (same weighting as the live
    baseline), compare to what actually happened. Returns
    {(group, tier): {"p10", "p90", "n"}} — additive WAR offsets to apply to
    a live baseline in that same tier."""
    resid = defaultdict(list)
    for group, hist, min_prior, key, edges in (
        ("hitter", bat_all, _MIN_PRIOR_AB, "ab", _HITTER_TIER_EDGES),
        ("pitcher", pit_all, _MIN_PRIOR_IP, "ip", _PITCHER_TIER_EDGES),
    ):
        for pid, seasons in hist.items():
            for i, s in enumerate(seasons):
                prior = seasons[i + 1:i + 5]
                if not prior or prior[0].get(key, 0) < min_prior:
                    continue
                pred = _weighted_war([dict(p, season_pct=1.0) for p in prior])
                if pred is None:
                    continue
                tier = _tier(pred, edges)
                resid[(group, tier)].append(s["war"] - pred)

    spread = {}
    for k, vals in resid.items():
        if len(vals) < _MIN_BACKTEST_N:
            continue
        vals = sorted(vals)
        n = len(vals)
        spread[k] = {
            "p10": vals[max(0, int(n * 0.10))],
            "p90": vals[min(n - 1, int(n * 0.90))],
            "n": n,
        }
    return spread


def _spread_for(spread, group, tier):
    """Look up (group, tier); widen outward one tier at a time if that
    exact bucket doesn't have enough historical seasons yet."""
    for alt in (tier, tier - 1, tier + 1, tier - 2, tier + 2):
        if (group, alt) in spread:
            return spread[(group, alt)]
    return None


def _season_pct(conn, game_date, games_per_season=162):
    """Fraction of the current season played so far, by games-played count
    (mirrors statsplusplus.evaluation.war.load_stat_history's own formula —
    duplicated rather than imported since that function bundles it with the
    AB-gated history load this module deliberately avoids for current-year
    rows; see the note in get_projected_war)."""
    game_year = int(game_date[:4])
    game_month = int(game_date[5:7])
    if game_month >= 11:
        return 1.0
    row = conn.execute(
        """SELECT MAX(cnt) FROM (
            SELECT COUNT(*) as cnt FROM games
            WHERE date LIKE ? AND played=1 AND game_type=0
            GROUP BY home_team)""",
        (f"{game_year}%",),
    ).fetchone()
    return min((row[0] or 0) / float(games_per_season), 1.0) if row and row[0] else 0.0


def _confidence_label(season_pct):
    if season_pct < 0.25:
        return "Early"
    if season_pct < 0.6:
        return "Developing"
    return "Established"


def get_projected_war(team_id=None):
    """League-wide projected end-of-season WAR, empirically calibrated.

    Args:
        team_id: if given, restricts the returned `players` list to that
            team (the backtest itself always uses the full league).

    Returns:
        {
          "players": [ {pid, name, team_id, team_abbr, bucket, group, age,
              season_pct, confidence, current_war, current_sample,
              prior_war, prior_year, baseline_war, floor_war, ceiling_war,
              baseline_value, floor_value, ceiling_value, salary,
              surplus, observed_dpw, spread_n}, ... ]
              sorted by surplus descending,
          "dollars_per_war": int,
          "money_divisor": int,
          "n_excluded_small_sample": int,
          "n_excluded_no_track_record": int,
          "backtest_buckets": [ {group, tier, n, p10, p90}, ... ],
        }
    """
    import queries as _q
    from contract_value import contract_value as _cv
    from statsplusplus.evaluation.war import load_stat_history
    from statsplusplus.config.league_config import dollars_per_war as _dpw_fn
    from statsplusplus.config.league_config import games_per_season as _gps_fn

    conn = get_db()
    league_dir = get_cfg().league_dir
    state = _q.get_state()
    game_date = state["game_date"]
    game_year = int(game_date[:4])
    _gps = _gps_fn(league_dir)

    # NOTE: deliberately NOT using statsplusplus.evaluation.war.stat_peak_war
    # here. That function reads from load_stat_history()'s in-memory dicts,
    # which silently DROP a player's current-season row entirely below a
    # 130-AB (hitters) reporting threshold — a filter that makes sense for a
    # *completed* season but means, for most of a season, most hitters would
    # get no current-season signal at all in their baseline (the whole point
    # of this feature). Baseline is computed directly below from the
    # ungated full history instead, using the same [3,3,2,1] weighting
    # scheme and the same real season-completion fraction.
    season_pct = _season_pct(conn, game_date, games_per_season=_gps)
    # Still used to feed contract_value() (its OWN separate stat_war calc,
    # for the existing multi-year surplus/market_value figures) — not for
    # this module's own baseline, computed independently above.
    cv_hist = load_stat_history(conn, game_date, games_per_season=_gps)

    bat_all, pit_all = _load_full_history(conn)
    spread = _build_empirical_spread(bat_all, pit_all)

    dpw = _dpw_fn(league_dir)
    mtd = _money_divisor()

    tids = mlb_team_ids()
    if not tids:
        return {"players": [], "dollars_per_war": dpw, "money_divisor": mtd,
                "n_excluded_small_sample": 0, "n_excluded_no_track_record": 0,
                "backtest_buckets": []}

    qs = ",".join("?" * len(tids))
    rows = conn.execute(
        f"""SELECT p.player_id, p.name, p.team_id FROM players p
            JOIN contracts c ON c.player_id = p.player_id
            WHERE p.level='1' AND p.team_id IN ({qs}) AND c.is_major=1""",
        list(tids),
    ).fetchall()

    names = team_names_map()
    abbrs = team_abbr_map()

    n_small_sample = 0
    n_no_track_record = 0
    players_out = []

    for r in rows:
        pid, name, tid = r["player_id"], r["name"], r["team_id"]
        if team_id and tid != team_id:
            # Still need the row processed for exclusion accounting only
            # when scoped — skip entirely for a scoped view for speed.
            continue

        cv = _cv(pid, _conn=conn, _hist=cv_hist, league_dir=league_dir)
        if not cv or not cv.get("breakdown"):
            continue
        bucket = cv.get("bucket")
        age = cv.get("age")
        is_pitcher = bucket in ("SP", "RP")
        group = "pitcher" if is_pitcher else "hitter"

        # Two-way players (real batting AND pitching sample this season) are
        # excluded for now — their combined-role value isn't something this
        # metric's per-group empirical backtest models correctly yet, and
        # `bucket` alone (their primary defensive/role slot) would silently
        # throw away half their production either way.
        cur_bat_row = next((s for s in bat_all.get(pid, []) if s["year"] == game_year), None)
        cur_pit_row = next((s for s in pit_all.get(pid, []) if s["year"] == game_year), None)
        is_two_way = bool(cur_bat_row and cur_bat_row.get("ab", 0) >= _MIN_CURRENT_AB
                          and cur_pit_row and cur_pit_row.get("ip", 0) >= _MIN_CURRENT_IP)
        if is_two_way:
            continue

        my_h = pit_all.get(pid, []) if is_pitcher else bat_all.get(pid, [])
        cur_row = cur_pit_row if is_pitcher else cur_bat_row
        sample_key = "ip" if is_pitcher else "ab"
        min_current = _MIN_CURRENT_IP if is_pitcher else _MIN_CURRENT_AB
        cur_sample = cur_row.get(sample_key, 0) if cur_row else 0
        if not cur_row or cur_sample < min_current:
            n_small_sample += 1
            continue

        prior_seasons = [s for s in my_h if s["year"] < game_year]
        min_prior = _MIN_PRIOR_IP if is_pitcher else _MIN_PRIOR_AB
        if not prior_seasons or prior_seasons[0].get(sample_key, 0) < min_prior:
            n_no_track_record += 1
            continue
        prior_full = prior_seasons[0]

        # Baseline: [3,3,2,1]-weighted current + up to 3 prior seasons, with
        # the current season's weight scaled by how much of it has been
        # played (my_h[0] is the current year since cur_row was confirmed
        # present above and my_h is sorted most-recent-first).
        window = [dict(s, season_pct=(season_pct if s["year"] == game_year else 1.0))
                  for s in my_h[:4]]
        baseline = _weighted_war(window)
        if baseline is None:
            n_no_track_record += 1
            continue

        edges = _PITCHER_TIER_EDGES if is_pitcher else _HITTER_TIER_EDGES
        tier = _tier(baseline, edges)
        sp = _spread_for(spread, group, tier)
        if sp:
            floor_war = baseline + sp["p10"]
            ceil_war = baseline + sp["p90"]
            spread_n = sp["n"]
        else:
            # No historical bucket large enough yet — fall back to a
            # conservative flat +/-40% band rather than showing nothing.
            floor_war = baseline * 0.6
            ceil_war = baseline * 1.4
            spread_n = 0
        floor_war = max(_WAR_CLAMP[0], min(floor_war, baseline))
        ceil_war = min(_WAR_CLAMP[1], max(ceil_war, baseline))

        bd0 = cv["breakdown"][0]
        salary = bd0.get("salary_full") or 0

        baseline_value = baseline * dpw
        floor_value = floor_war * dpw
        ceil_value = ceil_war * dpw
        surplus = baseline_value - salary
        observed_dpw = (salary / baseline) if baseline > 0 else None

        players_out.append({
            "pid": pid, "name": name, "team_id": tid,
            "team_abbr": abbrs.get(tid, "?"), "team_name": names.get(tid, f"Team {tid}"),
            "bucket": bucket, "group": group, "age": age,
            "season_pct": round(season_pct * 100, 1),
            "confidence": _confidence_label(season_pct),
            "current_war": round(cur_row["war"], 2),
            "current_sample": round(cur_sample, 1),
            "prior_war": round(prior_full["war"], 2),
            "prior_year": prior_full["year"],
            "baseline_war": round(baseline, 2),
            "floor_war": round(floor_war, 2),
            "ceiling_war": round(ceil_war, 2),
            "baseline_value": round(baseline_value),
            "floor_value": round(floor_value),
            "ceiling_value": round(ceil_value),
            "salary": round(salary),
            "surplus": round(surplus),
            "observed_dpw": round(observed_dpw) if observed_dpw is not None else None,
            "spread_n": spread_n,
        })

    players_out.sort(key=lambda p: -p["surplus"])

    buckets_out = [
        {"group": g, "tier": t, "n": v["n"], "p10": round(v["p10"], 2), "p90": round(v["p90"], 2)}
        for (g, t), v in sorted(spread.items())
    ]

    return {
        "players": players_out,
        "dollars_per_war": dpw,
        "money_divisor": mtd,
        "n_excluded_small_sample": n_small_sample,
        "n_excluded_no_track_record": n_no_track_record,
        "backtest_buckets": buckets_out,
    }
