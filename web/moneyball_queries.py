"""Moneyball page — league-wide spend-efficiency comparison.

Answers three questions with real per-player contract_value() breakdowns
(not approximations): how does my team's $/WAR compare to every other team
in the league, what fraction of my contracts are actually generating
positive surplus vs. dragging value down, and where's that concentrated
(which specific deals are carrying/costing the team).

Computing contract_value() for every MLB player in the league is fast
(~0.6ms/player, ~1s for a full league) since it's pure ratings/stat-history
math with no per-player network or heavy I/O, so this runs live on every
page load rather than needing a cached/precomputed table.
"""

import os, sys
from collections import defaultdict

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "scripts"))
from web_league_context import (get_db, get_cfg, team_abbr_map, team_names_map,
                                 mlb_team_ids, my_team_id, money_divisor as _money_divisor)


def get_moneyball(team_id=None):
    """League-wide $/WAR + contract-efficiency comparison, from one team's
    point of view (defaults to the user's own team).

    Returns:
        {
          "team_id": int, "teams": [ {team_id, name, abbr, payroll, war,
              dollars_per_war, surplus_yr1, pos_count, neg_count, n,
              pos_pct, pos_dollars, neg_dollars, rank}, ... ] sorted by
              dollars_per_war ascending (most efficient first),
          "league_avg_dpw": float | None,
          "my_team": the entry from `teams` matching team_id (or None),
          "n_teams_ranked": int (teams with war > 0, i.e. a real $/WAR),
          "contracts": [ {pid, name, bucket, age, salary, war, surplus,
              market_value}, ... ] for team_id's own roster, sorted by
              surplus descending,
        }
    """
    import queries as _q
    from contract_value import contract_value as _cv
    from statsplusplus.evaluation.war import load_stat_history

    conn = get_db()
    tid = team_id or my_team_id()
    league_dir = get_cfg().league_dir
    state = _q.get_state()
    hist = load_stat_history(conn, state["game_date"])
    mtd = _money_divisor()

    tids = mlb_team_ids()
    if not tids:
        return {"team_id": tid, "teams": [], "league_avg_dpw": None,
                "my_team": None, "n_teams_ranked": 0, "contracts": []}

    qs = ",".join("?" * len(tids))
    rows = conn.execute(
        f"SELECT player_id, name, team_id FROM players WHERE level='1' AND team_id IN ({qs})",
        list(tids),
    ).fetchall()

    by_team = defaultdict(list)
    for r in rows:
        by_team[r["team_id"]].append((r["player_id"], r["name"]))

    names = team_names_map()
    abbrs = team_abbr_map()

    team_stats = {}
    my_contracts = []
    total_payroll_lg = 0.0
    total_war_lg = 0.0

    for t in tids:
        players = by_team.get(t, [])
        payroll = war = surplus_yr1 = pos_dollars = neg_dollars = 0.0
        pos_count = neg_count = 0
        for pid, name in players:
            cv = _cv(pid, _conn=conn, _hist=hist, league_dir=league_dir)
            if not cv or not cv.get("breakdown"):
                continue
            bd0 = cv["breakdown"][0]
            sal = bd0["salary_full"] or 0
            w = bd0["war_base"] or 0.0
            s = bd0["surplus"] or 0
            payroll += sal
            war += w
            surplus_yr1 += s
            if s > 0:
                pos_dollars += sal
                pos_count += 1
            else:
                neg_dollars += sal
                neg_count += 1
            if t == tid:
                my_contracts.append({
                    "pid": pid, "name": name, "bucket": cv.get("bucket"),
                    "age": cv.get("age"), "salary": sal, "war": round(w, 1),
                    "surplus": s, "market_value": bd0.get("market_value", 0),
                })

        n = pos_count + neg_count
        team_stats[t] = {
            "team_id": t, "name": names.get(t, f"Team {t}"), "abbr": abbrs.get(t, "?"),
            "payroll": payroll, "war": war,
            "dollars_per_war": (payroll / war) if war > 0 else None,
            "surplus_yr1": surplus_yr1,
            "pos_count": pos_count, "neg_count": neg_count, "n": n,
            "pos_pct": round(pos_count / n * 100, 1) if n else 0.0,
            "pos_dollars": pos_dollars, "neg_dollars": neg_dollars,
        }
        total_payroll_lg += payroll
        total_war_lg += war

    ranked = sorted(
        (t for t in team_stats.values() if t["dollars_per_war"] is not None),
        key=lambda x: x["dollars_per_war"],
    )
    unranked = [t for t in team_stats.values() if t["dollars_per_war"] is None]
    for i, t in enumerate(ranked):
        t["rank"] = i + 1
    for t in unranked:
        t["rank"] = None
    teams_out = ranked + unranked

    my_contracts.sort(key=lambda c: -c["surplus"])

    return {
        "team_id": tid,
        "teams": teams_out,
        "league_avg_dpw": (total_payroll_lg / total_war_lg) if total_war_lg > 0 else None,
        "my_team": team_stats.get(tid),
        "n_teams_ranked": len(ranked),
        "contracts": my_contracts,
        "money_divisor": mtd,
    }
