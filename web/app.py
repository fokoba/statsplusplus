"""EMLB Dashboard — Flask app."""

import sys
from pathlib import Path

# Ensure imports resolve even when the package isn't pip-installed:
#   - project root  → `from statsplus import client`
#   - src/          → `import statsplusplus...` (src/ layout)
# `pip install -e .` normally handles src/, but this makes the app robust if the
# editable install didn't take (e.g. a locked-down environment).
_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
_SRC = str(Path(__file__).resolve().parent.parent / "src")
for _p in (_SRC, _PROJECT_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from flask import Flask, render_template, redirect, request, g, session
import werkzeug.exceptions
import queries
from statsplusplus.config.league_config import LeagueConfig
from statsplusplus.config.league_context import get_league_dir, get_active_league_slug, APP_CONFIG_PATH
from statsplusplus.utils.logging import get_logger

log = get_logger("web")

app = Flask(__name__)
app.json.sort_keys = False
app.jinja_env.policies["json.dumps_kwargs"] = {"sort_keys": False}

# Flask session secret key — generated once, persisted in app_config.json
import json as _json
_app_cfg = _json.loads(APP_CONFIG_PATH.read_text()) if APP_CONFIG_PATH.exists() else {}
if "flask_secret_key" not in _app_cfg:
    import secrets
    _app_cfg["flask_secret_key"] = secrets.token_hex(32)
    APP_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    APP_CONFIG_PATH.write_text(_json.dumps(_app_cfg, indent=2) + "\n")
app.secret_key = _app_cfg["flask_secret_key"]

# Register route blueprints
from settings_routes import settings_bp
from api_routes import api_bp
app.register_blueprint(settings_bp)
app.register_blueprint(api_bp)

# Run DB schema migration on startup for all leagues (adds new columns if missing).
# This is idempotent and fast (only ALTERs if columns are absent).
try:
    from statsplusplus.data import db as _db_mod
    for _ld in Path(_PROJECT_ROOT, "data").iterdir():
        if _ld.is_dir() and (_ld / "league.db").exists():
            _db_mod.init_schema(_ld)
except Exception:
    pass  # non-fatal on startup — queries will fail with clear error if schema is stale


_EXEMPT_PREFIXES = ("/settings", "/onboard", "/switch-league", "/refresh",
                    "/static", "/api/test-connection", "/api/game-date",
                    "/api/wipe-league")


@app.before_request
def _set_league_context():
    """Populate Flask g with league-scoped config.

    Active league priority:
      1. Flask session (per-browser, set by /switch-league)
      2. app_config.json (global default, set by onboarding)
    """
    slug = session.get("active_league") or get_active_league_slug()
    league_dir = get_league_dir(slug)
    settings_exist = (league_dir / "config" / "league_settings.json").exists()
    if not settings_exist and not request.path.startswith(("/onboard", "/static")):
        return redirect("/onboard")
    cfg = LeagueConfig(base_dir=league_dir)
    g.league_slug = slug
    g.league_dir = league_dir
    g.league_config = cfg
    g.league_ready = (league_dir / "league.db").exists() and (
        league_dir / "config" / "league_averages.json").exists()
    if not g.league_ready and not any(
            request.path.startswith(p) for p in _EXEMPT_PREFIXES):
        return redirect("/settings")


@app.teardown_request
def _close_db(exc):
    conn = getattr(g, "_db_conn", None)
    if conn is not None:
        conn.close()
        g._db_conn = None
    if exc and not isinstance(exc, werkzeug.exceptions.HTTPException):
        log.error("Request teardown error: %s", exc, exc_info=exc)


@app.errorhandler(Exception)
def _handle_exception(e):
    if isinstance(e, werkzeug.exceptions.HTTPException):
        return e
    log.error("Unhandled exception on %s %s: %s", request.method, request.path, e, exc_info=True)
    raise e


def _get_cfg():
    """Get config — works both in and out of request context."""
    if hasattr(g, "league_config"):
        return g.league_config
    from statsplusplus.config.league_config import LeagueConfig; config = LeagueConfig()
    return config


@app.context_processor
def _inject_globals():
    cfg = _get_cfg()
    slug = cfg.settings.get("statsplus_slug", "")
    from statsplusplus.config.league_context import APP_CONFIG_PATH
    import json as _json
    data_dir = APP_CONFIG_PATH.parent
    league_list = []
    if data_dir.exists():
        for d in sorted(data_dir.iterdir()):
            if d.is_dir() and (d / "config" / "league_settings.json").exists():
                ls = _json.loads((d / "config" / "league_settings.json").read_text())
                league_list.append({"slug": d.name, "name": ls.get("league", d.name)})
    # Manual Offseason-mode flag (Phase A). Read from the active league's state.
    offseason_mode = False
    try:
        if getattr(g, "league_dir", None):
            _sp = Path(g.league_dir) / "config" / "state.json"
            if _sp.exists():
                offseason_mode = bool(_json.loads(_sp.read_text()).get("offseason_mode", False))
    except Exception:
        offseason_mode = False
    return {
        "statsplus_base": f"https://statsplus.net/{slug}",
        "all_teams": sorted(cfg.team_names_map.items(), key=lambda x: x[1]) if getattr(g, "league_ready", False) else [],
        "league_name": cfg.settings.get("league", "League"),
        "league_list": league_list,
        "active_league_slug": g.league_slug if hasattr(g, "league_slug") else "",
        "league_ready": getattr(g, "league_ready", False),
        "offseason_mode": offseason_mode,
    }


# ── Template filters ──


def _fmt_ip(ip):
    """Format true decimal IP (33.333) as baseball display (33.1)."""
    if ip is None or isinstance(ip, str):
        return ip or "-"
    full = int(ip)
    frac = round((ip - full) * 3)
    return f"{full}.{frac}" if frac else f"{full}.0"


app.jinja_env.filters["fmt_ip"] = _fmt_ip


def _short_name(name):
    parts = name.split()
    if len(parts) < 2:
        return name
    suffix = ""
    if parts[-1].lower().rstrip(".") in {"jr", "sr", "ii", "iii", "iv"}:
        suffix = " " + parts[-1]
        parts = parts[:-1]
    return f"{parts[0][:1]}. {parts[-1]}{suffix}"


app.jinja_env.filters["short"] = _short_name


def _fmt_money(val):
    """Format a dollar amount: $1.2M for millions, $150K for thousands."""
    if val is None:
        return "—"
    if isinstance(val, str):
        return val
    if abs(val) >= 1_000_000:
        return f"${val / 1e6:.1f}M"
    if abs(val) >= 1_000:
        return f"${val / 1e3:.0f}K"
    return f"${val:,.0f}"


app.jinja_env.filters["money"] = _fmt_money


# ── Page routes ──


@app.route("/")
def index():
    return redirect(f"/team/{queries.get_my_team_id()}")


@app.route("/dashboard")
def dashboard():
    return redirect(f"/team/{queries.get_my_team_id()}")


def _render_minor_league_team(info):
    """Render a minor league team page."""
    tid = info["team_id"]
    notables = queries.get_minor_league_notables(tid)
    roster = queries.get_minor_league_roster(tid)
    cfg = _get_cfg()
    league_name = cfg.settings.get("league", "League")
    breadcrumbs = [{"label": league_name, "url": "/league"}]
    if info["parent_id"]:
        breadcrumbs.append({"label": info["parent_name"], "url": f"/team/{info['parent_id']}"})
    breadcrumbs.append({"label": f"{info['level']} {info['name']}", "url": f"/team/{tid}"})
    return render_template("team_minor.html",
                           info=info, notables=notables, roster=roster,
                           breadcrumbs=breadcrumbs)


@app.route("/team/<int:tid>")
def team(tid):
    cfg = _get_cfg()
    name = cfg.team_names_map.get(tid)
    if not name:
        minor_info = queries.get_minor_league_team(tid)
        if minor_info:
            return _render_minor_league_team(minor_info)
        return "Team not found", 404
    summary = queries.get_summary(tid)
    div_standings, div_name = queries.get_division_standings(tid)
    hitters, pitchers = queries.get_roster(tid)
    roster_hitters = queries.get_roster_hitters(tid)
    roster_pitchers = queries.get_roster_pitchers(tid)
    import json
    from web_league_context import league_averages
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
                           breadcrumbs=[{"label": cfg.settings.get("league", "League"), "url": "/league"},
                                        {"label": name, "url": f"/team/{tid}"}],
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


@app.route("/team/<int:tid>/minors")
def team_minors_all(tid):
    """All minor leaguers across all levels for a given MLB org."""
    cfg = _get_cfg()
    name = cfg.team_names_map.get(tid)
    if not name:
        return "Team not found", 404
    roster = queries.get_org_minor_league_roster(tid)
    from web_league_context import get_db
    conn = get_db()
    lmap = cfg.level_map
    aff_rows = conn.execute("""
        SELECT DISTINCT t.team_id, t.name, p.level
        FROM teams t
        JOIN players p ON p.team_id = t.team_id
        WHERE t.parent_team_id = ? AND p.level != '1'
        GROUP BY t.team_id
        ORDER BY p.level
    """, (tid,)).fetchall()
    affiliates = [{"team_id": a["team_id"], "name": a["name"],
                   "level": lmap.get(str(a["level"]), str(a["level"])),
                   "level_num": a["level"]} for a in aff_rows]
    return render_template("team_minors_all.html",
                           team_name=name, team_id=tid,
                           roster=roster, affiliates=affiliates)


@app.route("/league")
def league():
    standings = queries.get_standings()
    cfg = _get_cfg()
    div_teams = {}
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
    from web_league_context import league_averages as _load_la
    lg_avg = _load_la()
    wc_per_lg = cfg.settings.get("wild_cards_per_league", 3)
    wc_tids = set()
    for lg_group in league_groups:
        lg_divs = lg_group["divisions"]
        div_winners = {d["rows"][0]["tid"] for d in lg_divs if d["rows"]}
        non_winners = sorted(
            [r for d in lg_divs for r in d["rows"] if r["tid"] not in div_winners],
            key=lambda r: -r["pct"])
        if non_winners:
            cutoff_pct = non_winners[min(wc_per_lg - 1, len(non_winners) - 1)]["pct"]
            for r in non_winners:
                if r["pct"] >= cutoff_pct:
                    wc_tids.add(r["tid"])
    for lg_group in league_groups:
        for d in lg_group["divisions"]:
            for r in d["rows"]:
                r["is_wc"] = r["div_rank"] != 1 and r["tid"] in wc_tids

    from web_league_context import mlb_team_ids, my_team_id
    _tam = cfg.team_abbr_map
    _tnm = cfg.team_names_map
    trade_orgs = sorted([{"tid": t, "abbr": _tam.get(t, "?"), "name": _tnm.get(t, _tam.get(t, "?"))}
                         for t in mlb_team_ids()], key=lambda x: x["name"])
    avg_gp = sum(r["w"] + r["l"] for r in standings) / max(len(standings), 1)
    season_remaining = max(0, (162 - avg_gp) / 162)

    draft_pool = queries.get_draft_pool()
    draft_depth = queries.get_draft_org_depth(my_team_id()) if draft_pool else {}

    pos_rankings = queries.get_positional_rankings()

    h2h = queries.get_head_to_head_matrix()

    return render_template("league.html", league_groups=league_groups,
                           prospects=prospects, all_prospects=all_prospects,
                           bat_leaders=bat_leaders,
                           pit_leaders=pit_leaders, power=power,
                           summary=summary, my_abbr=my_abbr, lg_avg=lg_avg,
                           trade_orgs=trade_orgs, my_team_id=my_team_id(),
                           season_remaining=round(season_remaining, 3),
                           draft_pool=draft_pool, draft_depth=draft_depth,
                           pos_rankings=pos_rankings,
                           standings=standings, h2h=h2h,
                           num_teams=len(cfg.mlb_team_ids))


@app.route("/offseason")
def offseason():
    """Phase-aware Offseason page (Phase A). A manual sub-phase selector drives
    which panels are surfaced; Trades stays visible across all phases."""
    import offseason_queries as osq
    import json as _json
    cfg = _get_cfg()
    tid = queries.get_my_team_id()
    team_name = cfg.team_names_map.get(tid, "My Team")
    arbitration = osq.get_arbitration(tid)
    market = osq.get_market_board(tid)
    extensions = osq.get_extension_candidates(tid)
    ln = cfg.settings.get("league", "League")
    # Current manual sub-phase (empty = show all panels)
    phase = ""
    try:
        _sp = Path(g.league_dir) / "config" / "state.json"
        if _sp.exists():
            phase = (_json.loads(_sp.read_text()).get("offseason_phase") or "").lower()
    except Exception:
        phase = ""
    # Chronological phases + display labels + which panels each surfaces.
    phases = [
        {"key": "playoffs", "label": "Playoffs", "icon": "🏆"},
        {"key": "arbitration", "label": "Arbitration", "icon": "⚖️"},
        {"key": "options", "label": "Options", "icon": "📄"},
        {"key": "free_agency", "label": "Free Agency", "icon": "✍️"},
        {"key": "rule5", "label": "Rule 5", "icon": "🔒"},
        {"key": "spring", "label": "Spring", "icon": "🌱"},
    ]
    # Panel gating (pure helper — see offseason_queries.panels_for_phase).
    # Trades is always shown separately in the template.
    show = osq.panels_for_phase(phase)
    return render_template(
        "offseason.html",
        breadcrumbs=[{"label": ln, "url": "/league"},
                     {"label": "Offseason", "url": "/offseason"}],
        team_name=team_name, tid=tid,
        arbitration=arbitration,
        market=market, extensions=extensions,
        phases=phases, current_phase=phase, show=show,
    )


@app.route("/player/<int:pid>")
def player(pid):
    p = queries.get_player(pid)
    if not p:
        return "Player not found", 404
    my_abbr = queries.get_my_team_abbr()
    cfg = _get_cfg()
    team_name = cfg.team_names_map.get(p.get("tid"), p.get("team", ""))
    ln = cfg.settings.get("league", "League")
    bc = [{"label": ln, "url": "/league"}]
    if p.get("tid"):
        bc.append({"label": team_name, "url": f"/team/{p['tid']}"})
    actual_tid = p.get("actual_team_id")
    if actual_tid:
        from web_league_context import get_db as _get_db_bc
        _conn_bc = _get_db_bc()
        _affiliate = _conn_bc.execute(
            "SELECT name, level FROM teams WHERE team_id=?", (actual_tid,)).fetchone()
        if _affiliate:
            _level_name = cfg.level_map.get(str(_affiliate["level"]), "")
            _aff_label = f"{_level_name} {_affiliate['name']}".strip() if _level_name else _affiliate["name"]
            bc.append({"label": _aff_label, "url": f"/team/{actual_tid}"})
    bc.append({"label": p["name"], "url": f"/player/{pid}"})
    return render_template("player.html", p=p, my_abbr=my_abbr, breadcrumbs=bc)


if __name__ == "__main__":
    app.run(debug=True, port=5001)
