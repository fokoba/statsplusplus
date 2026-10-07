"""EMLB Dashboard — Flask app."""

import os
import sys
from pathlib import Path

# Ensure imports resolve even when the package isn't pip-installed:
#   - src/          → `import statsplusplus...` (src/ layout, incl. the API client)
#   - project root  → legacy web-module imports
# `pip install -e .` normally handles src/, but this makes the app robust if the
# editable install didn't take (e.g. a locked-down environment).
_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
_SRC = str(Path(__file__).resolve().parent.parent / "src")
for _p in (_SRC, _PROJECT_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# Launched with the bare framework interpreter (the Python.app binary, so macOS
# applies the user's Full Disk Access grant for "Python" to the server and its
# background OOTP ingest) rather than through the venv's python3 stub: the venv
# isn't activated then, so add its packages to the path.
_VENV_SITE = Path(_PROJECT_ROOT) / ".venv" / "lib" / f"python{sys.version_info.major}.{sys.version_info.minor}" / "site-packages"
if _VENV_SITE.is_dir() and str(_VENV_SITE) not in sys.path:
    sys.path.append(str(_VENV_SITE))

# Reloader back on (2026-10-03, Forrest's call). It was off from 2026-10-01
# because background Claude Code tasks edit source files in this same working
# tree while the dev server runs, and Werkzeug's debug-mode reloader restarts
# the whole worker on every watched-file change — a restart mid-request looks
# identical to a crash from the browser's side (Forrest saw "crashes" several
# times in a day). Trade-off: code changes now apply without a manual
# "relaunch localhost", at the cost of those restarts. Set this to False to
# go back. The USE_RELOADER check below keeps the auto-ingest background
# thread starting correctly either way — WERKZEUG_RUN_MAIN is only set when
# the reloader spawns its watcher/worker split, so without the reloader that
# guard alone would never fire.
USE_RELOADER = True

from flask import Flask, render_template, redirect, request, g, session
import werkzeug.exceptions
import queries
from statsplusplus.config.league_config import LeagueConfig, games_per_season
from statsplusplus.config.league_context import get_league_dir, get_active_league_slug, APP_CONFIG_PATH
from statsplusplus.utils.logging import get_logger

log = get_logger("web")

app = Flask(__name__)
app.json.sort_keys = False
app.jinja_env.policies["json.dumps_kwargs"] = {"sort_keys": False}

# Secret key for signed session cookies — persisted in app_config.json so it
# survives restarts (a fresh key on every restart would invalidate every
# open browser's session, including their per-tab active-league choice).
def _load_or_create_secret_key() -> str:
    import json as _json, secrets
    from statsplusplus.config.league_context import APP_CONFIG_PATH
    cfg = _json.loads(APP_CONFIG_PATH.read_text()) if APP_CONFIG_PATH.exists() else {}
    key = cfg.get("flask_secret_key")
    if not key:
        key = secrets.token_hex(32)
        cfg["flask_secret_key"] = key
        APP_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        APP_CONFIG_PATH.write_text(_json.dumps(cfg, indent=2) + "\n")
    return key

app.secret_key = _load_or_create_secret_key()

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


# Background auto-ingest: pick up fresh OOTP exports (ratings sync, FA asks,
# park factors, Rule 5, draft pool, team salary) from the local folders
# Forrest's OOTP app writes to, with no manual upload step. Runs in a daemon
# thread so it never blocks requests; each pass is cheap when nothing's
# changed (per-category file-mtime check in local_ingest.py). Guarded by
# WERKZEUG_RUN_MAIN so the Flask debug reloader's parent/watcher process
# (which also executes this module's top-level code once, before spawning
# the real worker subprocess) doesn't also start a duplicate thread — only
# the actual worker process (marked WERKZEUG_RUN_MAIN=true) should.
if os.environ.get("WERKZEUG_RUN_MAIN") == "true" or not USE_RELOADER:
    def _local_ingest_loop():
        import time
        sys.path.insert(0, str(Path(_PROJECT_ROOT, "scripts")))
        from local_ingest import ingest_all_leagues
        while True:
            try:
                results = ingest_all_leagues(Path(_PROJECT_ROOT, "data"))
                changed = {
                    lg: {k: v for k, v in cats.items() if not v.startswith(("unchanged", "not found"))}
                    for lg, cats in results.items()
                }
                changed = {lg: cats for lg, cats in changed.items() if cats}
                if changed:
                    log.info("local_ingest: %s", changed)
            except Exception as e:
                log.error("local_ingest loop error: %s", e, exc_info=True)
            time.sleep(90)

    import threading
    threading.Thread(target=_local_ingest_loop, daemon=True, name="local-ingest").start()


# Background player-photo sync (weekly refresh): mirrors StatsPlus portraits
# into data/<league>/photos/. Throttled and resumable; each pass only touches
# photos older than a week, so it is a no-op most of the time.
if os.environ.get("WERKZEUG_RUN_MAIN") == "true" or not USE_RELOADER:
    def _photo_sync_loop():
        import json as _json
        import time
        from statsplusplus.data.photos import sync_photos
        from statsplusplus.data.logos import sync_logos, sync_team_page_logos
        from statsplusplus.config.league_context import get_statsplus_cookie
        time.sleep(60)  # let startup settle
        while True:
            try:
                for ld in sorted(Path(_PROJECT_ROOT, "data").iterdir()):
                    if not (ld / "league.db").exists():
                        continue
                    try:
                        slug = _json.loads((ld / "config" / "league_settings.json").read_text()).get("statsplus_slug")
                    except (OSError, ValueError):
                        slug = None
                    if slug:
                        sync_logos(ld, slug)
                        sync_team_page_logos(ld, slug, cookie=get_statsplus_cookie(ld))
                        res = sync_photos(ld, slug)
                        if res["downloaded"] or res["missing"]:
                            log.info("photo_sync %s: %s", ld.name, res)
            except Exception as e:
                log.error("photo sync loop error: %s", e, exc_info=True)
            time.sleep(3600)

    import threading as _threading
    _threading.Thread(target=_photo_sync_loop, daemon=True, name="photo-sync").start()


_EXEMPT_PREFIXES = ("/settings", "/onboard", "/switch-league", "/refresh",
                    "/static", "/api/test-connection", "/api/game-date",
                    "/api/wipe-league")


@app.before_request
def _set_league_context():
    """Populate Flask g with league-scoped config.

    Active league is resolved per-session (Flask's signed session cookie)
    first, so switching leagues in one browser tab doesn't yank the active
    league out from under any other open tab/session — falls back to the
    global app_config.json default for brand-new sessions.
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
    money_unit, money_divisor = "M", 1_000_000.0
    if getattr(g, "league_ready", False):
        try:
            from web_league_context import money_unit as _mu, money_divisor as _md
            money_unit, money_divisor = _mu(), _md()
        except Exception:
            pass
    # Manual Offseason-mode flag (Phase A). Read from the active league's state.
    offseason_mode = False
    try:
        if getattr(g, "league_dir", None):
            _sp = Path(g.league_dir) / "config" / "state.json"
            if _sp.exists():
                offseason_mode = bool(_json.loads(_sp.read_text()).get("offseason_mode", False))
    except Exception:
        offseason_mode = False
    my_team_name = "My Team"
    if getattr(g, "league_ready", False):
        try:
            my_team_name = cfg.team_names_map.get(cfg.my_team_id, "My Team")
        except Exception:
            pass
    return {
        "statsplus_base": f"https://statsplus.net/{slug}",
        "all_teams": sorted(cfg.team_names_map.items(), key=lambda x: x[1]) if getattr(g, "league_ready", False) else [],
        "league_name": cfg.settings.get("league", "League"),
        "my_team_name": my_team_name,
        "league_list": league_list,
        "active_league_slug": g.league_slug if hasattr(g, "league_slug") else "",
        "league_ready": getattr(g, "league_ready", False),
        "money_unit": money_unit,
        "money_divisor": money_divisor,
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
    # HEAD / is the preview tool's readiness probe. It never counts a redirect
    # as "ready", so every server was flagged neverBecameReady and stopped by
    # the tool after 30 minutes — the recurring "crash". Answer it directly.
    if request.method == "HEAD":
        return "", 200
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
    intl_complex = queries.get_intl_complex(tid)
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
    cut_candidates = queries.get_cut_candidates(tid)
    waiver_candidates = queries.get_waiver_candidates(tid)
    fa_candidates = queries.get_free_agent_candidates(tid)
    defense = queries.get_defense_page(tid)
    import custom_upload as _cu
    last_fa_ask_upload = _cu.get_last_fa_ask_upload(league_dir=cfg.league_dir)
    return render_template("team.html",
                           tid=tid, team_name=name,
                           breadcrumbs=[{"label": cfg.settings.get("league", "League"), "url": "/league"},
                                        {"label": name, "url": f"/team/{tid}"}],
                           summary=summary, standings=div_standings,
                           div_name=div_name, my_abbr=my_abbr,
                           hitters=hitters, pitchers=pitchers, farm=farm, intl_complex=intl_complex,
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
                           affiliates=affiliates,
                           cut_candidates=cut_candidates,
                           waiver_candidates=waiver_candidates,
                           fa_candidates=fa_candidates,
                           last_fa_ask_upload=last_fa_ask_upload,
                           defense=defense, has_dh=cfg.has_dh)


@app.route("/team/<int:tid>/moneyball")
def team_moneyball(tid):
    """League-wide spend-efficiency comparison: $/WAR ranking, contract
    efficiency (% of deals generating positive surplus), and this team's
    own best/worst-value contracts."""
    cfg = _get_cfg()
    name = cfg.team_names_map.get(tid)
    if not name:
        return "Team not found", 404
    import moneyball_queries as _mq
    data = _mq.get_moneyball(tid)
    import projected_war_queries as _pwq
    pw_data = _pwq.get_projected_war()
    team_proj = _pwq.get_team_projection_table(pw_data)
    war_projection = None
    war_emp = {}
    if cfg.league_dir.name.lower() == "ppl" and tid == 6:
        from war_projection_queries import build_projection, load_overrides
        war_projection = build_projection(tid, load_overrides(cfg.league_dir))
        # Cross-reference each hand-planned player against the empirical,
        # observed-performance projection above — lets Forrest sanity-check
        # his preseason workload plan against what's actually happening
        # in-season. Only the 25 seeded players, not the full league list,
        # to avoid embedding pw_data's ~700-player table twice on one page.
        seed_pids = {r["pid"] for r in war_projection.get("rows", [])}
        war_emp = {p["pid"]: p for p in pw_data["players"] if p["pid"] in seed_pids}
    return render_template("moneyball.html", tid=tid, team_name=name, data=data, pw_data=pw_data, team_proj=team_proj,
                           war_projection=war_projection, war_emp=war_emp,
                           breadcrumbs=[{"label": cfg.settings.get("league", "League"), "url": "/league"},
                                        {"label": name, "url": f"/team/{tid}"},
                                        {"label": "Moneyball", "url": f"/team/{tid}/moneyball"}])


@app.route("/team/<int:tid>/coaching")
def team_coaching(tid):
    """Org-wide coaching staff: every coach/exec grouped by role, staff
    chemistry vs the league, optional side-by-side vs another team and a
    league-wide ranking for one role (?vs=<team_id>&role=<job>)."""
    cfg = _get_cfg()
    name = cfg.team_names_map.get(tid)
    if not name:
        return "Team not found", 404
    import coaching_queries as _cq
    data = _cq.get_org_coaching(tid)
    vs_tid = request.args.get("vs", type=int)
    vs_data = _cq.get_org_coaching(vs_tid) if vs_tid and vs_tid != tid else None
    role = request.args.get("role") or ""
    role_options = [j for g in _cq.ROLE_GROUPS.values() for j in g["jobs"]]
    role_ranking = _cq.get_role_league_ranking(role) if role in role_options else None
    compare_teams = {t: n for t, n in _cq.get_all_team_names().items() if t != tid}
    return render_template("coaching.html", tid=tid, team_name=name, data=data,
                           chemistry=_cq.get_staff_chemistry(tid),
                           league_chemistry=_cq.get_league_chemistry(),
                           compare_teams=compare_teams, vs_tid=vs_tid, vs_data=vs_data,
                           role=role, role_options=role_options, role_ranking=role_ranking,
                           breadcrumbs=[{"label": cfg.settings.get("league", "League"), "url": "/league"},
                                        {"label": name, "url": f"/team/{tid}"},
                                        {"label": "Coaching", "url": f"/team/{tid}/coaching"}])


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
                           roster=roster, affiliates=affiliates,
                           affiliate_names={**dict(_get_cfg().team_names_map),
                                            **{a["team_id"]: a["name"] for a in affiliates}})


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
    # Postseason odds badge — replaces the old "WC" (wildcard) label, which
    # only made sense for multi-division leagues with a wildcard round. PPL
    # is a flat single-division-per-league, pennant-winner-only format (no
    # wildcard concept at all), so every non-leader was wrongly tagged "WC".
    # PO% (postseason odds) from StatsPlus's own Playoff Odds page means the
    # right thing in either format — pennant probability here, real
    # division-or-wildcard probability in a league like eMLB — so one
    # treatment covers both without per-format branching.
    _scripts_dir = os.path.join(_PROJECT_ROOT, "scripts")
    if _scripts_dir not in sys.path:
        sys.path.insert(0, _scripts_dir)
    from playoff_odds import get_playoff_odds_cached
    po = get_playoff_odds_cached(cfg.league_dir)
    _SEASON_GAMES = games_per_season(cfg.league_dir)
    for lg_group in league_groups:
        all_rows_in_lg = [r for d in lg_group["divisions"] for r in d["rows"]]
        best_w = max((r["w"] for r in all_rows_in_lg), default=0)
        for r in all_rows_in_lg:
            po_row = po.get(r["tid"]) if po else None
            if po_row:
                r["po_pct"] = po_row["po_pct"]
                # A team StatsPlus's own sims already round to ~0% AND whose
                # mathematical ceiling (MaxW — best case if they won every
                # remaining game) still can't reach the league's current
                # best record is a safe, conservative "no shot" call — not
                # every team below this line is truly eliminated in a
                # wildcard format, but every team flagged here definitely is.
                r["eliminated"] = po_row["po_pct"] < 0.05 and po_row["max_w"] < best_w
            else:
                # Live scrape unavailable (cookie expired, site down, no
                # cache yet) — fall back to a local win-ceiling check so the
                # page still distinguishes "eliminated" from "alive" even
                # without a real percentage. Ignores head-to-head/tiebreak
                # nuance; good enough for a binary fallback flag.
                remaining = max(0, _SEASON_GAMES - r["g"])
                r["po_pct"] = None
                r["eliminated"] = (r["w"] + remaining) < best_w
            r["is_wc"] = False  # retired field — kept so a stray template ref can't crash

    from web_league_context import mlb_team_ids, my_team_id
    _tam = cfg.team_abbr_map
    _tnm = cfg.team_names_map
    trade_orgs = sorted([{"tid": t, "abbr": _tam.get(t, "?"), "name": _tnm.get(t, _tam.get(t, "?"))}
                         for t in mlb_team_ids()], key=lambda x: x["name"])
    avg_gp = sum(r["w"] + r["l"] for r in standings) / max(len(standings), 1)
    season_remaining = max(0, (_SEASON_GAMES - avg_gp) / _SEASON_GAMES)

    draft_pool = queries.get_draft_pool()
    draft_depth = queries.get_draft_org_depth(my_team_id()) if draft_pool else {}

    pos_rankings = queries.get_positional_rankings()

    farm_rankings = queries.get_farm_system_rankings()

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
                           farm_rankings=farm_rankings,
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
    options = osq.get_option_decisions(tid)
    rule5 = osq.get_rule5(tid)
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
    # Finance panel — budget pools + derived available-to-spend (Component 3).
    from api_routes import finance_payload
    finance = finance_payload(tid)
    return render_template(
        "offseason.html",
        breadcrumbs=[{"label": ln, "url": "/league"},
                     {"label": "Offseason", "url": "/offseason"}],
        team_name=team_name, tid=tid,
        arbitration=arbitration,
        market=market, extensions=extensions,
        options=options,
        rule5=rule5,
        phases=phases, current_phase=phase, show=show,
        finance=finance,
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


@app.route("/scouting")
def scouting():
    import scouting_queries as _sq
    from web_league_context import my_team_id as _my_team_id
    roster_view = request.args.get("roster") if request.args.get("roster") in ("mlb", "org") else None
    data = _sq.get_scouting_targets(team_id=_my_team_id(), roster_view=roster_view)
    return render_template("scouting.html",
                           breadcrumbs=[{"label": "Scouting Targets", "url": "/scouting"}],
                           **data)


@app.route("/best-available")
def best_available():
    import scouting_queries as _sq
    from web_league_context import my_team_id as _my_team_id
    roster_view = request.args.get("roster") if request.args.get("roster") in ("mlb", "org") else None
    data = _sq.get_scouting_targets(high_confidence=True, team_id=_my_team_id(), roster_view=roster_view)
    return render_template("best_available.html",
                           breadcrumbs=[{"label": "Best Available", "url": "/best-available"}],
                           **data)


@app.route("/lineup-optimizer")
def lineup_optimizer():
    import lineup_optimizer_queries as _loq
    opponent_id = request.args.get("opponent", type=int)
    is_home = request.args.get("venue", "home") != "away"
    roster_scope = request.args.get("roster_scope", "mlb")
    if roster_scope not in ("mlb", "40man", "org"):
        roster_scope = "mlb"
    top_n = request.args.get("top_n", type=int)
    data = _loq.get_lineup_optimizer(opponent_id=opponent_id, is_home=is_home)
    park_data = _loq.get_best_by_park(roster_scope=roster_scope, top_n=top_n,
                                       opponent_id=opponent_id, is_home=is_home)
    merged = {**data, **park_data}
    return render_template("lineup_optimizer.html",
                           breadcrumbs=[{"label": "Lineup Optimizer", "url": "/lineup-optimizer"}],
                           **merged)


@app.route("/park-factors-upload", methods=["POST"])
def park_factors_upload():
    import lineup_optimizer_queries as _loq
    f = request.files.get("park_file")
    if not f or not f.filename:
        return redirect("/lineup-optimizer?park_error=1")
    try:
        count = _loq.import_league_park_factors(f.read(), league_dir=_get_cfg().league_dir)
        return redirect(f"/lineup-optimizer?park_count={count}")
    except Exception:
        return redirect("/lineup-optimizer?park_error=1")


@app.route("/team-compare")
def team_compare():
    import scouting_queries as _sq
    from web_league_context import my_team_id as _my_team_id, team_names_map as _team_names_map
    my_tid = _my_team_id()
    my_scope = request.args.get("my") if request.args.get("my") in ("mlb", "org") else "mlb"
    their_tid = request.args.get("their_team", type=int)
    their_scope = request.args.get("their_scope") if request.args.get("their_scope") in ("mlb", "org") else "mlb"

    data = {}
    if their_tid and their_tid != my_tid:
        data = _sq.get_team_compare(my_tid, my_scope, their_tid, their_scope)

    other_teams = sorted(
        ((tid, name) for tid, name in _team_names_map().items() if tid != my_tid),
        key=lambda x: x[1],
    )
    return render_template("team_compare.html",
                           breadcrumbs=[{"label": "Team Compare", "url": "/team-compare"}],
                           my_scope=my_scope, their_team=their_tid, their_scope=their_scope,
                           other_teams=other_teams, **data)


@app.route("/rule5-upload", methods=["POST"])
def rule5_upload():
    import scouting_queries as _sq
    f = request.files.get("rule5_file")
    dest = request.form.get("dest") or "/scouting"
    if not f or not f.filename:
        return redirect(f"{dest}?rule5_error=1")
    try:
        count = _sq.import_rule5_eligible(f.read(), league_dir=_get_cfg().league_dir)
        return redirect(f"{dest}?rule5_count={count}")
    except Exception:
        return redirect(f"{dest}?rule5_error=1")


@app.route("/custom-upload", methods=["GET", "POST"])
def custom_upload():
    import custom_upload as _cu

    results = None
    error = None
    under_24_only = request.form.get("under_24_only") == "on"
    if request.method == "POST":
        f = request.files.get("csv_file")
        if not f or not f.filename:
            error = "Choose a CSV file to upload."
        else:
            try:
                results = _cu.evaluate_csv(f.read(), league_dir=_get_cfg().league_dir)
                results = [r for r in results if "error" not in r]
                if under_24_only:
                    results = [r for r in results if r.get("age") is not None and r["age"] <= 24]
                results.sort(key=lambda r: -(r.get("fv") or 0))
            except Exception as e:
                error = f"Couldn't process that file: {e}"

    last_personality_upload = _cu.get_last_personality_upload(league_dir=_get_cfg().league_dir)

    return render_template("custom_upload.html", results=results, error=error,
                           under_24_only=under_24_only,
                           last_personality_upload=last_personality_upload,
                           breadcrumbs=[{"label": "Custom Upload", "url": "/custom-upload"}])


@app.route("/sync-ratings", methods=["POST"])
def sync_ratings():
    import custom_upload as _cu
    f = request.files.get("ratings_csv_file")
    if not f or not f.filename:
        return redirect("/custom-upload?sync_error=1")
    try:
        summary = _cu.import_ratings_sync(f.read(), league_dir=_get_cfg().league_dir)
        return redirect(
            f"/custom-upload?sync_updated={summary['updated']}&sync_inserted={summary['inserted']}"
            f"&sync_skipped={summary['skipped']}&sync_total={summary['total_rows']}"
        )
    except Exception as e:
        return redirect(f"/custom-upload?sync_error=1&sync_error_msg={e}")


@app.route("/team/<int:tid>/upload-fa-asks", methods=["POST"])
def upload_fa_asks(tid):
    import custom_upload as _cu
    f = request.files.get("fa_csv_file")
    if not f or not f.filename:
        return redirect(f"/team/{tid}?fa_ask_error=1#tab-adds")
    try:
        summary = _cu.import_fa_asking_prices(f.read(), league_dir=_get_cfg().league_dir)
        return redirect(f"/team/{tid}?fa_ask_count={summary['count']}&fa_ask_changed={summary['changed']}#tab-adds")
    except Exception:
        return redirect(f"/team/{tid}?fa_ask_error=1#tab-adds")


@app.route("/team/<int:tid>/upload-salary", methods=["POST"])
def upload_salary(tid):
    import custom_upload as _cu
    f = request.files.get("salary_html_file")
    if not f or not f.filename:
        return redirect(f"/team/{tid}?salary_error=1#tab-contracts")
    try:
        count = _cu.import_team_salary(f.read(), league_dir=_get_cfg().league_dir)
        return redirect(f"/team/{tid}?salary_count={count}#tab-contracts")
    except Exception:
        return redirect(f"/team/{tid}?salary_error=1#tab-contracts")


if __name__ == "__main__":
    app.run(debug=True, port=5001, threaded=True, use_reloader=USE_RELOADER)
