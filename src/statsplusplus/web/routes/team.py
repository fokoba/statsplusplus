"""Team page routes.

Handles /team/<tid> and /team/<tid>/minors routes.
"""

from __future__ import annotations

from flask import Blueprint, render_template, g

bp = Blueprint("team", __name__)


def _get_cfg():
    """Get config from request context."""
    return g.league_config if hasattr(g, "league_config") else None


@bp.route("/team/<int:tid>")
def team(tid: int):
    """Main team page — roster, depth chart, contracts, farm, stats."""
    import sys
    from pathlib import Path
    _root = Path(__file__).resolve().parent.parent.parent.parent
    if str(_root / "scripts") not in sys.path:
        sys.path.insert(0, str(_root / "scripts"))
    if str(_root / "web") not in sys.path:
        sys.path.insert(0, str(_root / "web"))

    import queries
    from web_league_context import league_averages

    cfg = _get_cfg()
    name = cfg.team_names_map.get(tid)
    if not name:
        minor_info = queries.get_minor_league_team(tid)
        if minor_info:
            return _render_minor_league_team(minor_info, cfg)
        return "Team not found", 404

    summary = queries.get_summary(tid)
    div_standings, div_name = queries.get_division_standings(tid)
    hitters, pitchers = queries.get_roster(tid)
    roster_hitters = queries.get_roster_hitters(tid)
    roster_pitchers = queries.get_roster_pitchers(tid)

    _la = league_averages()
    league_avg = {
        "avg": _la["batting"]["avg"], "obp": _la["batting"]["obp"],
        "slg": _la["batting"]["slg"], "ops": _la["batting"]["ops"],
        "bb_pct": _la["batting"]["bb_pct"], "k_pct": _la["batting"]["k_pct"],
        "era": _la["pitching"]["era"], "p_k_pct": _la["pitching"]["k_pct"],
        "p_bb_pct": _la["pitching"]["bb_pct"],
    }

    farm = queries.get_farm(tid)
    team_stats = queries.get_team_stats(tid)
    contracts, payroll = queries.get_contracts(tid)
    roster_summary = queries.get_roster_summary(tid)
    upcoming_fa = queries.get_upcoming_fa(tid)
    surplus_leaders = queries.get_surplus_leaders(tid)
    age_dist = queries.get_age_distribution(tid)
    farm_depth = queries.get_farm_depth(tid)
    stat_leaders = queries.get_stat_leaders(tid)
    recent_games = queries.get_recent_games(tid)
    payroll_summary = queries.get_payroll_summary(tid)
    record = queries.get_record_breakdown(tid)
    depth_chart = queries.get_depth_chart(tid)
    org_overview = queries.get_org_overview(tid)
    affiliates = queries.get_affiliates(tid)
    my_abbr = queries.get_my_team_abbr()

    return render_template("team.html",
                           tid=tid, team_name=name,
                           breadcrumbs=[
                               {"label": cfg.settings.get("league", "League"), "url": "/league"},
                               {"label": name, "url": f"/team/{tid}"},
                           ],
                           summary=summary, standings=div_standings,
                           div_name=div_name, my_abbr=my_abbr,
                           hitters=hitters, pitchers=pitchers, farm=farm,
                           team_stats=team_stats, contracts=contracts,
                           payroll=payroll, roster_summary=roster_summary,
                           upcoming_fa=upcoming_fa,
                           surplus_leaders=surplus_leaders,
                           age_dist=age_dist, farm_depth=farm_depth,
                           stat_leaders=stat_leaders,
                           recent_games=recent_games,
                           payroll_summary=payroll_summary,
                           record=record, depth_chart=depth_chart,
                           roster_hitters=roster_hitters,
                           roster_pitchers=roster_pitchers,
                           league_avg=league_avg,
                           org_overview=org_overview,
                           affiliates=affiliates)


@bp.route("/team/<int:tid>/minors")
def team_minors_all(tid: int):
    """All minor leaguers across all levels for a given MLB org."""
    import sys
    from pathlib import Path
    _root = Path(__file__).resolve().parent.parent.parent.parent
    if str(_root / "scripts") not in sys.path:
        sys.path.insert(0, str(_root / "scripts"))
    if str(_root / "web") not in sys.path:
        sys.path.insert(0, str(_root / "web"))

    import queries
    from web_league_context import get_db

    cfg = _get_cfg()
    name = cfg.team_names_map.get(tid)
    if not name:
        return "Team not found", 404

    roster = queries.get_org_minor_league_roster(tid)
    conn = get_db()
    conn.row_factory = None
    lmap = cfg.level_map
    aff_rows = conn.execute("""
        SELECT DISTINCT t.team_id, t.name, p.level
        FROM teams t
        JOIN players p ON p.team_id = t.team_id
        WHERE t.parent_team_id = ? AND p.level != '1'
        GROUP BY t.team_id
        ORDER BY p.level
    """, (tid,)).fetchall()
    affiliates = [{"team_id": a[0], "name": a[1],
                   "level": lmap.get(str(a[2]), str(a[2])),
                   "level_num": a[2]} for a in aff_rows]

    return render_template("team_minors_all.html",
                           team_name=name, team_id=tid,
                           roster=roster, affiliates=affiliates)


def _render_minor_league_team(info: dict, cfg) -> str:
    """Render a minor league team page."""
    import sys
    from pathlib import Path
    _root = Path(__file__).resolve().parent.parent.parent.parent
    if str(_root / "web") not in sys.path:
        sys.path.insert(0, str(_root / "web"))
    import queries

    tid = info["team_id"]
    notables = queries.get_minor_league_notables(tid)
    roster = queries.get_minor_league_roster(tid)
    league_name = cfg.settings.get("league", "League")
    breadcrumbs = [{"label": league_name, "url": "/league"}]
    if info["parent_id"]:
        breadcrumbs.append({"label": info["parent_name"], "url": f"/team/{info['parent_id']}"})
    breadcrumbs.append({"label": f"{info['level']} {info['name']}", "url": f"/team/{tid}"})

    return render_template("team_minor.html",
                           info=info, notables=notables, roster=roster,
                           breadcrumbs=breadcrumbs)
