"""League page routes.

Handles /league (main league dashboard with standings, prospects, leaders, draft).
"""

from __future__ import annotations

from flask import Blueprint, render_template, g

bp = Blueprint("league", __name__)


def _get_cfg():
    return g.league_config if hasattr(g, "league_config") else None


@bp.route("/league")
def league():
    """League dashboard — standings, prospects, leaders, power rankings, draft."""
    import sys
    from pathlib import Path
    _root = Path(__file__).resolve().parent.parent.parent.parent
    if str(_root / "scripts") not in sys.path:
        sys.path.insert(0, str(_root / "scripts"))
    if str(_root / "web") not in sys.path:
        sys.path.insert(0, str(_root / "web"))

    import queries
    from web_league_context import league_averages, mlb_team_ids, my_team_id

    cfg = _get_cfg()
    standings = queries.get_standings()

    # Group standings by league/division
    div_teams: dict[str, list] = {}
    for r in standings:
        div_teams.setdefault(r["div"], []).append(r)
    for div_name in div_teams:
        rows = div_teams[div_name]
        leader_w = rows[0]["w"] if rows else 0
        leader_l = rows[0]["l"] if rows else 0
        for i, r in enumerate(rows):
            r["div_rank"] = i + 1
            gb = ((leader_w - leader_l) - (r["w"] - r["l"])) / 2
            r["div_gb"] = "-" if gb < 0.25 else f"{gb:.1f}"

    # Build league_groups structure
    league_groups = []
    for lg in cfg.leagues:
        lg_divs = []
        for div_name, _tids in lg["divisions"].items():
            full_name = f"{lg['short']} {div_name}".strip()
            if full_name in div_teams:
                lg_divs.append({"name": full_name, "rows": div_teams[full_name]})
            elif div_name in div_teams:
                lg_divs.append({"name": div_name, "rows": div_teams[div_name]})
        league_groups.append({
            "name": lg["name"], "short": lg["short"],
            "color": lg["color"], "divisions": lg_divs,
        })

    prospects = queries.get_top_prospects(100)
    all_prospects = queries.get_all_prospects()
    bat_leaders = queries.get_batting_leaders()
    pit_leaders = queries.get_pitching_leaders()
    power = queries.get_power_rankings()
    summary = queries.get_summary()
    my_abbr = queries.get_my_team_abbr()
    lg_avg = league_averages()

    # Wild card calculation
    wc_per_lg = cfg.settings.get("wild_cards_per_league", 3)
    wc_tids: set[int] = set()
    for lg_group in league_groups:
        lg_divs = lg_group["divisions"]
        div_winners = {d["rows"][0]["tid"] for d in lg_divs if d["rows"]}
        non_winners = sorted(
            [r for d in lg_divs for r in d["rows"] if r["tid"] not in div_winners],
            key=lambda r: -r["pct"],
        )
        if non_winners:
            cutoff_pct = non_winners[min(wc_per_lg - 1, len(non_winners) - 1)]["pct"]
            for r in non_winners:
                if r["pct"] >= cutoff_pct:
                    wc_tids.add(r["tid"])
    for lg_group in league_groups:
        for d in lg_group["divisions"]:
            for r in d["rows"]:
                r["is_wc"] = r["div_rank"] != 1 and r["tid"] in wc_tids

    # Trade tab data
    _tam = cfg.team_abbr_map
    _tnm = cfg.team_names_map
    trade_orgs = sorted(
        [{"tid": t, "abbr": _tam.get(t, "?"), "name": _tnm.get(t, _tam.get(t, "?"))}
         for t in mlb_team_ids()],
        key=lambda x: x["name"],
    )
    avg_gp = sum(r["w"] + r["l"] for r in standings) / max(len(standings), 1)
    season_remaining = max(0, (162 - avg_gp) / 162)

    # Draft
    draft_pool = queries.get_draft_pool()
    draft_depth = queries.get_draft_org_depth(my_team_id()) if draft_pool else {}

    # Positional rankings
    pos_rankings = queries.get_positional_rankings()

    # Head-to-head
    h2h = queries.get_head_to_head_matrix()

    return render_template("league.html",
                           league_groups=league_groups,
                           prospects=prospects, all_prospects=all_prospects,
                           bat_leaders=bat_leaders, pit_leaders=pit_leaders,
                           power=power, summary=summary, my_abbr=my_abbr,
                           lg_avg=lg_avg, trade_orgs=trade_orgs,
                           my_team_id=my_team_id(),
                           season_remaining=round(season_remaining, 3),
                           draft_pool=draft_pool, draft_depth=draft_depth,
                           pos_rankings=pos_rankings,
                           standings=standings, h2h=h2h,
                           num_teams=len(cfg.mlb_team_ids))
