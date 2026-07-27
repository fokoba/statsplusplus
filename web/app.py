"""EMLB Dashboard — Flask app."""

import os, subprocess, sys, threading
from pathlib import Path

# Ensure project root is on sys.path for `from statsplus import client`
_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from flask import Flask, render_template, redirect, request, jsonify, g
import queries
from league_config import LeagueConfig
from league_context import get_league_dir, get_active_league_slug
from log_config import get_logger
from constants import DEFAULT_MINIMUM_SALARY

log = get_logger("web")

app = Flask(__name__)
app.json.sort_keys = False
app.jinja_env.policies["json.dumps_kwargs"] = {"sort_keys": False}

# Run DB schema migration on startup for all leagues (adds new columns if missing).
# This is idempotent and fast (only ALTERs if columns are absent).
try:
    import db as _db_mod
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
    """Populate Flask g with league-scoped config."""
    slug = get_active_league_slug()
    league_dir = get_league_dir(slug)
    settings_exist = (league_dir / "config" / "league_settings.json").exists()
    # No leagues at all — send to onboarding (unless already there)
    if not settings_exist and not request.path.startswith(("/onboard", "/static")):
        return redirect("/onboard")
    cfg = LeagueConfig(base_dir=league_dir)
    g.league_slug = slug
    g.league_dir = league_dir
    g.league_config = cfg
    # Set ratings scale from this league's config (not the module-level singleton)
    import ratings
    ratings._ratings_scale = cfg.ratings_scale
    # Check if league has enough data to render data pages
    g.league_ready = (league_dir / "league.db").exists() and (
        league_dir / "config" / "league_averages.json").exists()
    if not g.league_ready and not any(
            request.path.startswith(p) for p in _EXEMPT_PREFIXES):
        return redirect("/settings")


@app.teardown_request
def _close_db(exc):
    if exc:
        log.error("Request teardown error: %s", exc, exc_info=exc)


@app.errorhandler(Exception)
def _handle_exception(e):
    log.error("Unhandled exception on %s %s: %s", request.method, request.path, e, exc_info=True)
    raise e


def _get_cfg():
    """Get config — works both in and out of request context."""
    if hasattr(g, "league_config"):
        return g.league_config
    from league_config import config
    return config


# StatsPlus external link base — now dynamic per request
@app.context_processor
def _inject_globals():
    cfg = _get_cfg()
    slug = cfg.settings.get("statsplus_slug", "")
    # Discover all leagues for the switcher
    from league_context import APP_CONFIG_PATH
    import json as _json
    data_dir = APP_CONFIG_PATH.parent
    league_list = []
    if data_dir.exists():
        for d in sorted(data_dir.iterdir()):
            if d.is_dir() and (d / "config" / "league_settings.json").exists():
                ls = _json.loads((d / "config" / "league_settings.json").read_text())
                league_list.append({"slug": d.name, "name": ls.get("league", d.name)})
    return {
        "statsplus_base": f"https://statsplus.net/{slug}",
        "all_teams": sorted(cfg.team_names_map.items(), key=lambda x: x[1]) if getattr(g, "league_ready", False) else [],
        "league_name": cfg.settings.get("league", "League"),
        "league_list": league_list,
        "active_league_slug": g.league_slug if hasattr(g, "league_slug") else "",
        "league_ready": getattr(g, "league_ready", False),
    }

# --- Refresh state ---
_refresh_lock = threading.Lock()
_refresh_status = {"running": False, "result": None, "message": ""}


def _fmt_ip(ip):
    """Format true decimal IP (33.333) as baseball display (33.1)."""
    if ip is None:
        return "-"
    full = int(ip)
    frac = round((ip - full) * 3)
    return f"{full}.{frac}" if frac else f"{full}.0"


app.jinja_env.filters["fmt_ip"] = _fmt_ip

_SUFFIXES = {"jr", "jr.", "sr", "sr.", "ii", "iii", "iv"}
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


def _get_web_logger():
    from log_config import get_logger as _gl
    return _gl("web")


def _run_refresh():
    """Run refresh.py in background thread."""
    _log = _get_web_logger()
    _log.info("=== dashboard refresh started ===")
    try:
        from league_config import config as _bg_cfg
        script = Path(__file__).parent.parent / "scripts" / "refresh.py"
        result = subprocess.run(
            [sys.executable, str(script), str(_bg_cfg.year)],
            capture_output=True, text=True, timeout=600
        )
        if result.stdout:
            for line in result.stdout.strip().splitlines():
                _log.debug(line)
        if result.returncode == 0:
            _bg_cfg.reload()
            state = queries.get_state(force=True)
            # Post-refresh validation
            warnings = []
            try:
                from web_league_context import get_db
                conn = get_db()
                for tbl, minimum in [("players", 100), ("ratings", 100),
                                      ("teams", 10), ("contracts", 50)]:
                    n = conn.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
                    if n < minimum:
                        warnings.append(f"{tbl}: {n} rows (expected ≥{minimum})")
                conn.close()
            except Exception:
                pass
            msg = f"Refreshed to {state['game_date']}"
            if warnings:
                msg += " ⚠ " + "; ".join(warnings)
            _log.info("refresh ok: %s", msg)
            _refresh_status["result"] = "ok"
            _refresh_status["message"] = msg
        else:
            # Extract the final exception line from the traceback
            err = result.stderr.strip() or result.stdout.strip()
            _log.error("refresh failed (rc=%d):\n%s", result.returncode, err)
            lines = err.splitlines()
            last = lines[-1] if lines else "Unknown error"
            if "CookieExpiredError" in err or "requires user to be logged in" in err:
                last = "StatsPlus session expired — update your cookie in Settings."
            _refresh_status["result"] = "error"
            _refresh_status["message"] = last[:300]
    except subprocess.TimeoutExpired:
        _log.error("refresh timed out (10 min)")
        _refresh_status["result"] = "error"
        _refresh_status["message"] = "Refresh timed out (10 min)"
    except Exception as e:
        _log.exception("refresh failed")
        _refresh_status["result"] = "error"
        _refresh_status["message"] = str(e)[:200]
    finally:
        _refresh_status["running"] = False
        _refresh_lock.release()


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
        # Check if it's a minor league team
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


@app.route("/league")
def league():
    standings = queries.get_standings()
    cfg = _get_cfg()
    # Group standings by league/division using the leagues array
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

    # Build league_groups: [{name, short, color, divisions: [{name, rows}]}]
    league_groups = []
    for lg in cfg.leagues:
        lg_divs = []
        for div_name, _tids in lg["divisions"].items():
            # For multi-division leagues: "AL East", "NL Central", etc.
            # For single-division leagues: division name == league name, use as-is.
            full_name = f"{lg['short']} {div_name}".strip()
            # Try full_name first, then div_name alone (handles single-division leagues)
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
    # League averages for vitals cards
    from web_league_context import league_averages as _load_la
    lg_avg = _load_la()
    # Wild card spots — per league
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
    # Tag rows
    for lg_group in league_groups:
        for d in lg_group["divisions"]:
            for r in d["rows"]:
                r["is_wc"] = r["div_rank"] != 1 and r["tid"] in wc_tids

    # Trade tab data: org list + user's team
    from web_league_context import mlb_team_ids, my_team_id
    _tam = cfg.team_abbr_map
    _tnm = cfg.team_names_map
    trade_orgs = sorted([{"tid": t, "abbr": _tam.get(t, "?"), "name": _tnm.get(t, _tam.get(t, "?"))}
                         for t in mlb_team_ids()], key=lambda x: x["name"])
    # Season fraction remaining for pro-rating current year in trades
    avg_gp = sum(r["w"] + r["l"] for r in standings) / max(len(standings), 1)
    season_remaining = max(0, (162 - avg_gp) / 162)

    # Draft pool
    draft_pool = queries.get_draft_pool()
    draft_depth = queries.get_draft_org_depth(my_team_id()) if draft_pool else {}

    # Positional rankings
    pos_rankings = queries.get_positional_rankings()

    # Head-to-head matrix for expanded standings
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
    # For minor leaguers, show their actual affiliate team in the trail
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


@app.route("/api/prospect/<int:pid>")
def api_prospect(pid):
    data = queries.get_prospect_summary(pid)
    if not data:
        return jsonify({"error": "not found"}), 404
    return jsonify(data)


@app.route("/api/player-popup/<int:pid>")
def api_player_popup(pid):
    from player_queries import get_player_popup
    data = get_player_popup(pid)
    if not data:
        return jsonify({"error": "not found"}), 404
    return jsonify(data)


@app.route("/api/player-search")
def api_player_search():
    q = request.args.get("q", "").strip()
    if len(q) < 2:
        return jsonify([])
    return jsonify(queries.search_players(q))


@app.route("/api/draft-detail/<int:pid>")
def api_draft_detail(pid):
    """Compact grid data for draft prospect detail panel."""
    from web_league_context import get_db, level_map
    from player_utils import norm as _norm
    conn = get_db()
    from player_utils import norm as _pnorm
    n = lambda v: _pnorm(v) if v else None

    p = conn.execute("SELECT name, age, pos, role, level FROM players WHERE player_id=?", (pid,)).fetchone()
    if not p:
        return jsonify({"error": "not found"}), 404

    r = conn.execute("SELECT * FROM latest_ratings WHERE player_id=?", (pid,)).fetchone()
    if not r:
        return jsonify({"error": "no ratings"}), 404
    # Convert Row to dict for .get() access
    r = dict(zip([d[0] for d in conn.execute("SELECT * FROM latest_ratings LIMIT 0").description], r))

    is_pitcher = p["role"] in (11, 12, 13)
    pos_str = {11:"SP",12:"RP",13:"CL"}.get(p["role"]) or {1:'P',2:'C',3:'1B',4:'2B',5:'3B',6:'SS',7:'LF',8:'CF',9:'RF'}.get(p["pos"],"?")

    # For draft prospects, use assign_bucket to match the board's evaluation.
    # A player with role=RP but hitter tools gets evaluated as a hitter.
    from player_utils import assign_bucket
    from league_config import config as _cfg
    _p = dict(r)
    _p["pos"] = str(p["pos"]); _p["role"] = p["role"]
    _p["_role"] = {str(k): v for k, v in _cfg.role_map.items()}.get(str(p["role"] or 0), "position_player")
    _p["Pos"] = str(p["pos"])
    bucket = assign_bucket(_p)
    if bucket in ("SP", "RP"):
        is_pitcher = True
        pos_str = bucket
    else:
        is_pitcher = False
        if pos_str == "P":
            pos_str = {2:'C',3:'1B',4:'2B',5:'3B',6:'SS',7:'LF',8:'CF',9:'RF'}.get(p["pos"], bucket)

    from player_utils import height_str as _ht
    out = {
        "pid": pid, "name": p["name"], "age": p["age"], "pos": pos_str,
        "ovr": n(r["ovr"]) or r.get("composite_score") or 0,
        "pot": n(r["pot"]) or r.get("true_ceiling") or r.get("ceiling_score") or 0,
        "composite_score": r.get("composite_score"),
        "true_ceiling": r.get("true_ceiling") or r.get("ceiling_score"),
        "height": _ht(r["height"]) if r["height"] else None,
        "bats": r["bats"], "throws": r["throws"],
        "acc": r["acc"], "inj": r["prone"],
        "we": r["wrk_ethic"], "int": r["int_"],
        "lead": r["lead"], "loy": r["loy"], "greed": r["greed"],
        "is_pitcher": is_pitcher,
    }

    if is_pitcher:
        t_l, t_p, t_c = ["Stuff","Mov"], [n(r.get("pot_stf")),n(r.get("pot_mov"))], [n(r.get("stf")),n(r.get("mov"))]
        if r.get("hra") or r.get("pot_hra"):
            t_l.append("HRA"); t_p.append(n(r.get("pot_hra"))); t_c.append(n(r.get("hra")))
        if r.get("pbabip") or r.get("pot_pbabip"):
            t_l.append("BA"); t_p.append(n(r.get("pot_pbabip"))); t_c.append(n(r.get("pbabip")))
        t_l.append("Ctrl"); t_p.append(n(r.get("pot_ctrl"))); t_c.append(n(r.get("ctrl")))
        out["tools"] = {"labels": t_l, "pot": t_p, "cur": t_c}
        pitches = []
        for fld, nm in [("fst","FB"),("snk","SI"),("crv","CB"),("sld","SL"),("chg","CH"),
                          ("splt","SPL"),("cutt","CUT"),("cir_chg","CC"),("scr","SCR"),
                          ("frk","FRK"),("kncrv","KC"),("knbl","KN")]:
            cur = r.get(fld) or 0
            pot = r.get("pot_" + fld) or 0
            if cur >= 20 or pot >= 20:
                pitches.append({"name": nm, "cur": n(cur), "pot": n(pot)})
        pitches.sort(key=lambda x: x["pot"] or 0, reverse=True)
        out["pitches"] = pitches
        out["misc"] = {"vel": r.get("vel"), "stm": n(r.get("stm")), "hold": n(r.get("hold"))}
    else:
        t_l, t_p, t_c = ["Con"], [n(r.get("pot_cntct"))], [n(r.get("cntct"))]
        if r.get("babip") or r.get("pot_babip"):
            t_l.append("BA"); t_p.append(n(r.get("pot_babip"))); t_c.append(n(r.get("babip")))
        if r.get("ks") or r.get("pot_ks"):
            t_l.append("Ks"); t_p.append(n(r.get("pot_ks"))); t_c.append(n(r.get("ks")))
        t_l += ["Gap","Pow","Eye"]
        t_p += [n(r.get("pot_gap")),n(r.get("pot_pow")),n(r.get("pot_eye"))]
        t_c += [n(r.get("gap")),n(r.get("pow")),n(r.get("eye"))]
        out["tools"] = {"labels": t_l, "pot": t_p, "cur": t_c}
        out["running"] = {
            "labels": ["Spd", "Stl", "Run", "Sac", "Bunt"],
            "vals": [n(r.get("speed")), n(r.get("steal")), n(r.get("run")), n(r.get("sac_bunt")), n(r.get("bunt_hit"))],
        }
        out["fielding"] = {
            "cols": ["C", "IF", "OF"],
            "range": [None, n(r.get("ifr")), n(r.get("ofr"))],
            "error": [None, n(r.get("ife")), n(r.get("ofe"))],
            "arm":   [n(r.get("c_arm")), n(r.get("ifa")), n(r.get("ofa"))],
            "tdp": n(r.get("tdp")),
            "c_blk": n(r.get("c_blk")),
            "c_frm": n(r.get("c_frm")),
        }
        pos_grades = {}
        for col, label in [("c","C"),("first_b","1B"),("second_b","2B"),("third_b","3B"),
                           ("ss","SS"),("lf","LF"),("cf","CF"),("rf","RF")]:
            cur = n(r.get(col) or 0)
            pot = n(r.get("pot_" + col) or 0)
            if (cur and cur > 20) or (pot and pot > 20) or label == pos_str:
                pos_grades[label] = [cur or 20, pot or 20]
        out["positions"] = pos_grades

    return jsonify(out)


@app.route("/api/player-percentiles/<int:pid>")
def api_player_percentiles(pid):
    """Return percentile rankings for a specific year."""
    from percentiles import get_hitter_percentiles, get_pitcher_percentiles, get_fielding_percentiles
    from web_league_context import get_db
    year = request.args.get("year", type=int)
    split_id = request.args.get("split", 1, type=int)
    stat_type = request.args.get("type", "main")  # "main" or "fielding"
    if not year:
        return jsonify({"error": "year required"}), 400
    conn = get_db()
    role = conn.execute("SELECT role FROM players WHERE player_id=?", (pid,)).fetchone()
    conn.close()
    if not role:
        return jsonify({"error": "not found"}), 404

    if stat_type == "fielding":
        data = get_fielding_percentiles(pid, year=year)
        if not data:
            return jsonify({"error": "no data for year"}), 404
        return jsonify({"year": year, "positions": data})

    is_pitcher = role[0] in (11, 12, 13)
    if is_pitcher:
        data = get_pitcher_percentiles(pid, split_id=split_id, year=year)
    else:
        data = get_hitter_percentiles(pid, split_id=split_id, year=year)
    if not data:
        return jsonify({"error": "no data for year"}), 404
    return jsonify({"year": year, "stats": data})


@app.route("/api/player-percentile-history/<int:pid>")
def api_player_percentile_history(pid):
    """Return full percentile history for a split."""
    from percentiles import get_percentile_history
    from web_league_context import get_db
    split_id = request.args.get("split", 1, type=int)
    conn = get_db()
    role = conn.execute("SELECT role FROM players WHERE player_id=?", (pid,)).fetchone()
    conn.close()
    if not role:
        return jsonify({"error": "not found"}), 404
    is_pitcher = role[0] in (11, 12, 13)
    data = get_percentile_history(pid, is_pitcher=is_pitcher, split_id=split_id)
    if not data:
        return jsonify({"error": "no data"}), 404
    return jsonify(data)
    if not data:
        return jsonify({"error": "no data for year"}), 404
    return jsonify({"year": year, "stats": data})


@app.route("/api/player-card/<int:pid>")
def api_player_card(pid):
    data = queries.get_player_card(pid)
    if not data:
        return jsonify({"error": "not found"}), 404
    return jsonify(data)


@app.route("/api/draft-picks")
def api_draft_picks():
    """Fetch current draft picks from StatsPlus API."""
    try:
        from statsplus import client
        from league_context import get_statsplus_cookie
        cfg = _get_cfg()
        slug = cfg.settings.get("statsplus_slug", "")
        cookie = get_statsplus_cookie()
        if slug and cookie:
            client.configure(slug, cookie)
        raw = client.get_draft()
        picks = [{"pid": d["ID"], "name": d["Player Name"], "team": d["Team"],
                  "tid": d["Team ID"], "pos": d["Position"], "age": d["Age"],
                  "round": d["Round"], "pick": d["Pick In Round"],
                  "overall": d["Overall"], "college": d["College"]}
                 for d in raw if d.get("ID")]
        return jsonify({"picks": picks})
    except Exception as e:
        return jsonify({"picks": [], "error": str(e)})


@app.route("/api/draft-pool-upload", methods=["POST"])
def api_draft_pool_upload():
    """Upload a CSV of draft-eligible player IDs exported from OOTP."""
    import csv, io
    from league_context import get_league_dir
    f = request.files.get("file")
    if not f:
        return jsonify({"ok": False, "error": "No file uploaded"}), 400
    try:
        text = f.read().decode("utf-8-sig")
        reader = csv.DictReader(io.StringIO(text))
        # Find the player ID column (could be "ID", "Player ID", "PlayerID", etc.)
        headers = reader.fieldnames or []
        id_col = None
        for h in headers:
            if h.strip().lower().replace(" ", "").replace("_", "") in ("id", "playerid", "pid"):
                id_col = h
                break
        if not id_col:
            return jsonify({"ok": False, "error": f"No player ID column found. Headers: {headers[:10]}"}), 400
        pids = []
        for row in reader:
            val = row.get(id_col, "").strip()
            if val.isdigit():
                pids.append(int(val))
        if not pids:
            return jsonify({"ok": False, "error": "No valid player IDs found in file"}), 400
        # Store
        _json = __import__("json")
        pool_path = get_league_dir() / "config" / "draft_pool.json"
        pool_path.write_text(_json.dumps({"player_ids": pids}, indent=2))
        return jsonify({"ok": True, "total": len(pids)})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400


@app.route("/api/draft-sim", methods=["POST"])
def api_draft_sim():
    """Run a draft simulation."""
    data = request.get_json(silent=True) or {}

    try:
        from draft_board import (load_board, simulate_draft, draft_value)
        from draft_settings import load_settings
        from league_context import get_league_dir

        league_dir = get_league_dir()
        rows, adp, needs, num_teams, conn = load_board()
        pick_pos = data.get("pick", 30)
        num_rounds = data.get("rounds", 7)
        seed = data.get("seed")

        # Load settings: use request body override if provided, else saved file
        settings = data.get("settings") or load_settings(league_dir)

        our_picks, _ = simulate_draft(rows, adp, needs, num_teams, pick_pos,
                                      num_rounds, seed, settings=settings)

        picks_out = [{"round": rd, "overall": (rd - 1) * num_teams + slot,
                      "pid": r["player_id"], "name": r["name"],
                      "pos": r["bucket"], "fv": r["fv"],
                      "fv_str": r["fv_str"], "pot": r["pot"],
                      "ceiling": r["true_ceiling"],
                      "surplus": round(r["prospect_surplus"] / 1e6, 1),
                      "risk": r["risk"]}
                     for rd, slot, r in our_picks]

        need_strs = [f"{b}(+{v})" for b, v in sorted(needs.items(), key=lambda x: -x[1])] if needs else []
        return jsonify({"picks": picks_out, "needs": need_strs, "num_teams": num_teams})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/draft-upload-list", methods=["POST"])
def api_draft_upload_list():
    """Generate the auto-draft list using saved settings and return it."""
    data = request.get_json(silent=True) or {}
    limit = data.get("top", 500)
    exclude_pids = set(data.get("exclude", []))  # already-drafted player IDs

    try:
        from draft_board import load_board, build_pick_list, compute_adp
        from draft_settings import load_settings
        from league_context import get_league_dir

        league_dir = get_league_dir()
        rows, adp, needs, num_teams, conn = load_board()

        # Exclude already-drafted players
        if exclude_pids:
            rows = [r for r in rows if r["player_id"] not in exclude_pids]

        # Load settings: use request body override if provided, else saved file
        settings = data.get("settings") or load_settings(league_dir)

        ordered = build_pick_list(rows, adp, needs, num_teams, min(limit, 500),
                                  settings=settings)
        ranked_ids = [str(r["player_id"]) for r in ordered]

        out_path = league_dir / "tmp" / "draft_upload.txt"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text("\n".join(ranked_ids) + "\n")

        preview = [{"rank": i + 1, "pid": r["player_id"], "name": r["name"],
                    "pos": r["bucket"], "fv_str": r["fv_str"],
                    "exp_round": adp.get(r["player_id"], {}).get("exp_round")}
                   for i, r in enumerate(ordered[:30])]
        return jsonify({"ok": True, "count": len(ranked_ids),
                        "path": str(out_path), "preview": preview})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/draft-settings", methods=["GET"])
def api_draft_settings_get():
    """Return current draft board settings for the active league."""
    try:
        from draft_settings import load_settings, PRESETS
        from league_context import get_league_dir
        settings = load_settings(get_league_dir())
        return jsonify({"ok": True, "settings": settings, "presets": PRESETS})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/draft-settings", methods=["POST"])
def api_draft_settings_post():
    """Save draft board settings for the active league."""
    data = request.get_json(silent=True) or {}
    settings = data.get("settings")
    if not settings:
        return jsonify({"ok": False, "error": "Missing 'settings' in request body"}), 400
    try:
        from draft_settings import save_settings
        from league_context import get_league_dir
        save_settings(get_league_dir(), settings)
        return jsonify({"ok": True})
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/draft-settings/copy", methods=["POST"])
def api_draft_settings_copy():
    """Copy draft settings from another league to the active league."""
    data = request.get_json(silent=True) or {}
    from_league = data.get("from_league")
    if not from_league:
        return jsonify({"ok": False, "error": "Missing 'from_league'"}), 400
    try:
        from draft_settings import copy_settings, load_settings
        from league_context import get_league_dir
        from pathlib import Path
        import json as _json

        # Resolve source league directory
        app_config_path = Path("data/app_config.json")
        if app_config_path.exists():
            app_config = _json.loads(app_config_path.read_text())
        else:
            return jsonify({"ok": False, "error": "Cannot find app config"}), 500

        # Source league dir is data/<from_league>/
        source_dir = Path("data") / from_league
        if not source_dir.exists():
            return jsonify({"ok": False, "error": f"League '{from_league}' not found"}), 404

        copied = copy_settings(source_dir, get_league_dir())
        return jsonify({"ok": True, "settings": copied})
    except FileNotFoundError as e:
        return jsonify({"ok": False, "error": str(e)}), 404
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/open-file-location", methods=["POST"])
def api_open_file_location():
    """Open the containing folder of a file in the system file explorer."""
    import subprocess, platform, shutil
    data = request.get_json(silent=True) or {}
    path = data.get("path", "")
    if not path:
        return jsonify({"ok": False, "error": "No path"}), 400
    folder = str(Path(path).parent)
    try:
        system = platform.system()
        if system == "Linux":
            # Try multiple file managers
            for cmd in ["xdg-open", "nautilus", "dolphin", "thunar", "nemo", "pcmanfm"]:
                if shutil.which(cmd):
                    subprocess.Popen([cmd, folder])
                    return jsonify({"ok": True})
            return jsonify({"ok": False, "error": f"No file manager found. File is at: {folder}"}), 500
        elif system == "Darwin":
            subprocess.Popen(["open", folder])
        elif system == "Windows":
            subprocess.Popen(["explorer", folder])
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/org-players/<int:team_id>")
def api_org_players(team_id):
    import trade_queries
    return jsonify(trade_queries.get_org_players(team_id))


@app.route("/api/trade-value", methods=["POST"])
def api_trade_value():
    import trade_queries
    data = request.get_json(silent=True) or {}
    pid = data.get("player_id")
    if not pid:
        return jsonify({"error": "player_id required"}), 400
    result = trade_queries.get_trade_value(pid, data.get("retention_pct", 0.0))
    if not result:
        return jsonify({"error": "player not found"}), 404
    return jsonify(result)


@app.route("/api/save-structure", methods=["POST"])
def api_save_structure():
    """Auto-save league structure from the interactive editor."""
    import json as _json
    cfg = _get_cfg()
    data = request.get_json(silent=True)
    if not data or "leagues" not in data:
        return jsonify({"ok": False, "error": "Missing leagues data"}), 400
    leagues = data["leagues"]
    if not isinstance(leagues, list):
        return jsonify({"ok": False, "error": "leagues must be an array"}), 400
    s = cfg.settings
    s["leagues"] = leagues
    flat = {}
    for lg in leagues:
        for div_name, tids in lg.get("divisions", {}).items():
            flat[f"{lg['short']} {div_name}"] = tids
    s["divisions"] = flat
    settings_path = cfg.league_dir / "config" / "league_settings.json"
    settings_path.write_text(_json.dumps(s, indent=2) + "\n")
    cfg.reload()
    return jsonify({"ok": True})


@app.route("/api/wipe-league", methods=["POST"])
def api_wipe_league():
    """Delete one or all league data directories."""
    import json as _json, shutil
    from league_context import APP_CONFIG_PATH, get_league_dir

    data = request.get_json(silent=True) or {}
    slug = data.get("slug")          # specific league, or None for all
    data_dir = APP_CONFIG_PATH.parent

    # Discover existing leagues
    existing = [d.name for d in sorted(data_dir.iterdir())
                if d.is_dir() and (d / "config" / "league_settings.json").exists()]

    targets = [slug] if slug else list(existing)
    if slug and slug not in existing:
        return jsonify({"ok": False, "error": "League not found"}), 404

    for s in targets:
        shutil.rmtree(data_dir / s, ignore_errors=True)

    remaining = [d.name for d in sorted(data_dir.iterdir())
                 if d.is_dir() and (d / "config" / "league_settings.json").exists()]

    # Update active_league in app_config
    app_cfg = _json.loads(APP_CONFIG_PATH.read_text()) if APP_CONFIG_PATH.exists() else {}
    if remaining:
        app_cfg["active_league"] = remaining[0]
        redirect_to = "/"
    else:
        app_cfg.pop("active_league", None)
        redirect_to = "/onboard"
    APP_CONFIG_PATH.write_text(_json.dumps(app_cfg, indent=2) + "\n")

    return jsonify({"ok": True, "redirect": redirect_to})


@app.route("/settings", methods=["GET", "POST"])
def settings():
    import json as _json
    cfg = _get_cfg()

    if request.method == "POST":
        action = request.form.get("action", "")

        if action == "set_team":
            queries.set_my_team(int(request.form["team_id"]))

        elif action == "save_identity":
            s = cfg.settings
            s["league"] = request.form.get("league_name", s.get("league", ""))
            slug = request.form.get("statsplus_slug", "").strip()
            if slug:
                s["statsplus_slug"] = slug
            wc = request.form.get("wild_cards_per_league", "")
            if wc.isdigit():
                s["wild_cards_per_league"] = int(wc)
            dh = request.form.get("dh_rule", "")
            if dh in ("No DH", "Universal DH", "AL Only DH"):
                s["dh_rule"] = dh
            scale_changed = False
            rs = request.form.get("ratings_scale", "")
            if rs in ("1-100", "20-80"):
                scale_changed = rs != s.get("ratings_scale")
                s["ratings_scale"] = rs
            settings_path = cfg.league_dir / "config" / "league_settings.json"
            settings_path.write_text(_json.dumps(s, indent=2) + "\n")
            cfg.reload()
            if scale_changed:
                def _recalc():
                    import fv_calc
                    fv_calc.run()
                threading.Thread(target=_recalc, daemon=True).start()
                log.info("ratings_scale changed to %s — triggered fv_calc recalculation", rs)

        elif action == "save_financial":
            s = cfg.settings
            ms = request.form.get("minimum_salary", "")
            if ms.isdigit():
                s["minimum_salary"] = int(ms)
            pe = request.form.get("pyth_exp", "")
            try:
                s["pyth_exp"] = round(float(pe), 2)
            except (ValueError, TypeError):
                pass
            s["perpetual_arb"] = bool(request.form.get("perpetual_arb"))
            settings_path = cfg.league_dir / "config" / "league_settings.json"
            settings_path.write_text(_json.dumps(s, indent=2) + "\n")
            cfg.reload()

        elif action == "save_cookie":
            from league_context import APP_CONFIG_PATH
            app_cfg = _json.loads(APP_CONFIG_PATH.read_text()) if APP_CONFIG_PATH.exists() else {}
            sid = request.form.get("session_id", "").strip()
            csrf = request.form.get("csrf_token", "").strip()
            cookie = f"sessionid={sid}" if sid else ""
            if cookie and csrf:
                cookie += f";csrftoken={csrf}"
            app_cfg["statsplus_cookie"] = cookie
            APP_CONFIG_PATH.write_text(_json.dumps(app_cfg, indent=2) + "\n")

        elif action == "save_structure":
            try:
                leagues = _json.loads(request.form.get("leagues_json", "[]"))
                if not isinstance(leagues, list):
                    raise ValueError("Must be a JSON array")
                s = cfg.settings
                s["leagues"] = leagues
                # Rebuild flat divisions for backward compat
                flat = {}
                for lg in leagues:
                    for div_name, tids in lg.get("divisions", {}).items():
                        flat[f"{lg['short']} {div_name}"] = tids
                s["divisions"] = flat
                settings_path = cfg.league_dir / "config" / "league_settings.json"
                settings_path.write_text(_json.dumps(s, indent=2) + "\n")
                cfg.reload()
            except (ValueError, _json.JSONDecodeError, KeyError) as e:
                # Re-render with error instead of redirect
                current_team = queries.get_my_team_id()
                teams = sorted(cfg.team_names_map.items(), key=lambda x: x[1])
                state = queries.get_state()
                from league_context import APP_CONFIG_PATH as _acp
                _ac = _json.loads(_acp.read_text()) if _acp.exists() else {}
                from web_league_context import get_db as _gdb
                conn = _gdb()
                counts = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                          for t in ["players","ratings","batting_stats","pitching_stats","contracts","teams"]}
                conn.close()
                _ck = _ac.get("statsplus_cookie", "")
                _sid, _csrf = "", ""
                for _p in _ck.split(";"):
                    _p = _p.strip()
                    if _p.startswith("sessionid="): _sid = _p.split("=",1)[1]
                    elif _p.startswith("csrftoken="): _csrf = _p.split("=",1)[1]
                return render_template("settings.html",
                    current=current_team, teams=teams, cfg=cfg, state=state,
                    session_id=_sid, csrf_token=_csrf, counts=counts,
                    league_groups=cfg.leagues,
                    leagues_json=request.form.get("leagues_json",""),
                    structure_error=str(e))

        return redirect("/settings")

    # GET — gather all settings data
    current_team = queries.get_my_team_id()
    teams = sorted(cfg.team_names_map.items(), key=lambda x: x[1])
    state = queries.get_state()

    # Connection info
    from league_context import APP_CONFIG_PATH
    app_cfg = _json.loads(APP_CONFIG_PATH.read_text()) if APP_CONFIG_PATH.exists() else {}
    cookie = app_cfg.get("statsplus_cookie", "")
    session_id, csrf_token = "", ""
    for part in cookie.split(";"):
        part = part.strip()
        if part.startswith("sessionid="):
            session_id = part.split("=", 1)[1]
        elif part.startswith("csrftoken="):
            csrf_token = part.split("=", 1)[1]

    # Record counts
    from web_league_context import get_db
    counts = {}
    try:
        conn = get_db()
        for tbl in ["players", "ratings", "batting_stats", "pitching_stats", "contracts", "teams"]:
            counts[tbl] = conn.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
        conn.close()
    except Exception:
        counts = {t: 0 for t in ["players", "ratings", "batting_stats", "pitching_stats", "contracts", "teams"]}

    # All MLB teams for the structure editor
    all_mlb_teams = {int(k): v for k, v in cfg.team_abbr_map.items()}

    return render_template("settings.html",
                           current=current_team, teams=teams,
                           cfg=cfg, state=state,
                           session_id=session_id, csrf_token=csrf_token,
                           counts=counts, league_groups=cfg.leagues,
                           all_mlb_teams=all_mlb_teams,
                           leagues_json=__import__("json").dumps(cfg.leagues, indent=2))


@app.route("/switch-league/<slug>")
def switch_league(slug):
    import json as _json
    from league_context import APP_CONFIG_PATH, get_league_dir
    league_dir = get_league_dir(slug)
    if not (league_dir / "config" / "league_settings.json").exists():
        return "League not found", 404
    app_cfg = _json.loads(APP_CONFIG_PATH.read_text()) if APP_CONFIG_PATH.exists() else {}
    app_cfg["active_league"] = slug
    APP_CONFIG_PATH.write_text(_json.dumps(app_cfg, indent=2) + "\n")
    return redirect("/")


# ── Onboarding Wizard ──

@app.route("/onboard")
def onboard():
    from league_context import get_statsplus_cookie
    # Pre-fill from existing cookie if available
    existing = get_statsplus_cookie()
    session_id, csrf_token = "", ""
    for part in existing.split(";"):
        part = part.strip()
        if part.startswith("sessionid="):
            session_id = part.split("=", 1)[1]
        elif part.startswith("csrftoken="):
            csrf_token = part.split("=", 1)[1]
    return render_template("onboard.html", step=1, slug="",
                           session_id=session_id, csrf_token=csrf_token)


@app.route("/onboard/step1", methods=["POST"])
def onboard_step1():
    import json as _json
    slug = request.form.get("slug", "").strip().lower()
    session_id = request.form.get("session_id", "").strip()
    csrf_token = request.form.get("csrf_token", "").strip()
    if not slug:
        return render_template("onboard.html", step=1, slug=slug,
                               session_id=session_id, csrf_token=csrf_token,
                               error="Slug is required")
    if not session_id:
        return render_template("onboard.html", step=1, slug=slug,
                               session_id=session_id, csrf_token=csrf_token,
                               error="Session ID is required")
    # Assemble cookie string
    cookie = f"sessionid={session_id}"
    if csrf_token:
        cookie += f";csrftoken={csrf_token}"
    # Save cookie globally
    from league_context import APP_CONFIG_PATH
    APP_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    app_cfg = _json.loads(APP_CONFIG_PATH.read_text()) if APP_CONFIG_PATH.exists() else {}
    app_cfg["statsplus_cookie"] = cookie
    APP_CONFIG_PATH.write_text(_json.dumps(app_cfg, indent=2) + "\n")
    # Verify connection — only /ratings/ requires auth, so test that specifically.
    # This also kicks off the ratings export — capture the poll URL to reuse in step 2.
    try:
        from statsplus import client
        import re as _re
        client.configure(slug, cookie)
        resp = client._get("/ratings/")
        match = _re.search(r'https?://\S+', resp)
        ratings_poll_url = match.group(0).rstrip(".)") if match else ""
    except Exception as e:
        return render_template("onboard.html", step=1, slug=slug,
                               session_id=session_id, csrf_token=csrf_token,
                               error=f"Connection failed: {e}")
    return render_template("onboard.html", step=2, slug=slug,
                           ratings_poll_url=ratings_poll_url)



# ── Onboard refresh (async) ──

_onboard_refresh = {"running": False, "stage": "", "error": "", "done": False, "slug": ""}


def _run_onboard_refresh(slug, ratings_poll_url=""):
    """Run refresh.py for onboarding, capturing stage progress."""
    from log_config import get_logger as _gl
    _log = _gl("onboard")
    _log.info("=== onboard refresh started (slug=%s) ===", slug)
    try:
        from league_context import get_statsplus_cookie
        cookie = get_statsplus_cookie()
        script = Path(__file__).parent.parent / "scripts" / "refresh.py"
        cmd = [sys.executable, "-u", str(script), "--no-fv", "2033"]
        env = {**os.environ, "STATSPLUS_LEAGUE_URL": slug, "STATSPLUS_COOKIE": cookie}
        if ratings_poll_url:
            env["RATINGS_POLL_URL"] = ratings_poll_url
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1, env=env,
        )
        for line in proc.stdout:
            line = line.strip()
            if line:
                _log.debug(line)
            if line.startswith("──"):
                _onboard_refresh["stage"] = line
        proc.wait(timeout=600)
        if proc.returncode != 0:
            _log.error("refresh exited with code %d", proc.returncode)
            _onboard_refresh["error"] = _onboard_refresh["stage"] or "Refresh failed"
            if "CookieExpiredError" in (_onboard_refresh["stage"] or ""):
                _onboard_refresh["error"] = "Session expired — go back and update your credentials."
        else:
            _log.info("=== onboard refresh complete ===")
            _onboard_refresh["done"] = True
    except Exception as e:
        _log.exception("onboard refresh failed")
        _onboard_refresh["error"] = str(e)[:200]
    finally:
        _onboard_refresh["running"] = False


@app.route("/onboard/start-refresh", methods=["POST"])
def onboard_start_refresh():
    import json as _json
    from league_context import APP_CONFIG_PATH
    data = request.get_json(silent=True) or {}
    slug = data.get("slug", "")
    if _onboard_refresh["running"]:
        return jsonify({"status": "already_running"})
    # Ensure league directory and settings exist before refresh
    league_dir = APP_CONFIG_PATH.parent / slug
    config_dir = league_dir / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    for sub in ("history", "reports", "tmp"):
        (league_dir / sub).mkdir(exist_ok=True)
    settings_path = config_dir / "league_settings.json"
    if not settings_path.exists():
        settings_path.write_text(_json.dumps({
            "league": slug.upper(),
            "statsplus_slug": slug,
            "year": 2033,
            "default_team_id": 1,
            "divisions": {},
            "team_abbr": {},
            "team_names": {},
            "pos_map": {"1":"P","2":"C","3":"1B","4":"2B","5":"3B","6":"SS","7":"LF","8":"CF","9":"RF","10":"DH"},
            "level_map": {"1":"MLB","2":"AAA","3":"AA","4":"A","5":"A-Short","6":"Rookie","7":"Indy","8":"Intl"},
            "role_map": {"0":"position_player","11":"starter","12":"reliever","13":"closer"},
            "minimum_salary": DEFAULT_MINIMUM_SALARY,
            "pyth_exp": 1.83,
            "wild_cards_per_league": 3,
        }, indent=2) + "\n")
    state_path = config_dir / "state.json"
    if not state_path.exists():
        state_path.write_text(_json.dumps({"game_date": "", "year": 2033, "my_team_id": 1}, indent=2) + "\n")
    # Set as active league so refresh.py writes to the right directory
    app_cfg = _json.loads(APP_CONFIG_PATH.read_text()) if APP_CONFIG_PATH.exists() else {}
    app_cfg["active_league"] = slug
    APP_CONFIG_PATH.write_text(_json.dumps(app_cfg, indent=2) + "\n")

    _onboard_refresh.update(running=True, stage="Starting...", error="", done=False, slug=slug)
    ratings_poll_url = data.get("ratings_poll_url", "")
    threading.Thread(target=_run_onboard_refresh, args=(slug, ratings_poll_url), daemon=True).start()
    return jsonify({"status": "started"})


@app.route("/onboard/refresh-status")
def onboard_refresh_status():
    return jsonify({
        "running": _onboard_refresh["running"],
        "stage": _onboard_refresh["stage"],
        "error": _onboard_refresh["error"],
        "done": _onboard_refresh["done"],
    })


@app.route("/onboard/step3", methods=["GET", "POST"])
def onboard_step3():
    import json as _json
    from league_context import APP_CONFIG_PATH
    slug = request.args.get("slug", "") or request.form.get("slug", "")
    slug = slug.strip()
    league_dir = APP_CONFIG_PATH.parent / slug

    if request.method == "GET":
        # Arriving from JS after refresh completed — load teams from DB
        import db as _db
        conn = _db.get_conn(league_dir)
        # Get full team names from API (DB only stores city name)
        from statsplus import client
        api_teams = {t["ID"]: f"{t['Name']} {t['Nickname']}" for t in client.get_teams()
                     if t.get("Nickname")}
        mlb_ids = conn.execute('''
            SELECT DISTINCT p.team_id
            FROM players p WHERE p.level = '1'
        ''').fetchall()
        teams = sorted(
            [(r[0], api_teams.get(r[0], f"Team {r[0]}")) for r in mlb_ids if r[0] in api_teams],
            key=lambda x: x[1])
        # Auto-detect ratings scale: >80 = 1-100, ≤20 = 1-20, else 20-80
        max_rating = conn.execute(
            "SELECT MAX(MAX(ovr, pot)) FROM latest_ratings"
        ).fetchone()
        max_val = max_rating[0] if max_rating and max_rating[0] else 0
        if max_val > 80:
            detected_scale = "1-100"
        elif max_val <= 20:
            detected_scale = "1-20"
        else:
            detected_scale = "20-80"
        conn.close()
        # Load auto-detected structure for the editor
        settings_path = league_dir / "config" / "league_settings.json"
        s = _json.loads(settings_path.read_text()) if settings_path.exists() else {}
        leagues = s.get("leagues", [])
        all_mlb_teams = {int(k): v for k, v in s.get("team_abbr", {}).items()}
        team_names_map = {int(k): v for k, v in s.get("team_names", {}).items()}
        return render_template("onboard.html", step=3, slug=slug, teams=teams,
                               league_name=slug.upper(), leagues=leagues,
                               all_mlb_teams=all_mlb_teams,
                               team_names_map=team_names_map,
                               min_salary=s.get("minimum_salary"),
                               detected_scale=detected_scale)

    # POST — save configuration
    league_name = request.form.get("league_name", slug.upper())
    team_id = int(request.form.get("team_id", 1))
    settings_path = league_dir / "config" / "league_settings.json"
    s = _json.loads(settings_path.read_text())
    s["league"] = league_name
    sp_slug = request.form.get("statsplus_slug", "").strip()
    if sp_slug:
        s["statsplus_slug"] = sp_slug
    wc = request.form.get("wild_cards_per_league", "")
    if wc.isdigit():
        s["wild_cards_per_league"] = int(wc)
    dh = request.form.get("dh_rule", "")
    if dh in ("No DH", "Universal DH", "AL Only DH"):
        s["dh_rule"] = dh
    rs = request.form.get("ratings_scale", "")
    if rs in ("1-100", "20-80"):
        s["ratings_scale"] = rs
    ms = request.form.get("minimum_salary", "")
    if ms.isdigit():
        s["minimum_salary"] = int(ms)
    settings_path.write_text(_json.dumps(s, indent=2) + "\n")
    state_path = league_dir / "config" / "state.json"
    st = _json.loads(state_path.read_text())
    st["my_team_id"] = team_id
    state_path.write_text(_json.dumps(st, indent=2) + "\n")
    # Run fv_calc now that settings are finalized
    try:
        script = Path(__file__).parent.parent / "scripts" / "fv_calc.py"
        subprocess.run([sys.executable, str(script)],
                       capture_output=True, text=True, timeout=120)
    except Exception:
        pass  # Non-fatal — user can re-run via refresh later
    return render_template("onboard.html", step=4, league_name=league_name)


@app.route("/refresh", methods=["POST"])
def refresh():
    if not _refresh_lock.acquire(blocking=False):
        return jsonify({"status": "already_running"}), 409
    _refresh_status["running"] = True
    _refresh_status["result"] = None
    _refresh_status["message"] = ""
    threading.Thread(target=_run_refresh, daemon=True).start()
    return jsonify({"status": "started"})


@app.route("/refresh/status")
def refresh_status():
    return jsonify({
        "running": _refresh_status["running"],
        "result": _refresh_status["result"],
        "message": _refresh_status["message"],
    })


@app.route("/api/game-date")
def api_game_date():
    """Return local and remote game dates for staleness check."""
    local_date = queries.get_state().get("game_date", "")
    try:
        from statsplus import client
        from league_context import get_statsplus_cookie
        cfg = _get_cfg()
        slug = cfg.settings.get("statsplus_slug", "")
        cookie = get_statsplus_cookie()
        if slug and cookie:
            client.configure(slug, cookie)
        remote_date = client.get_date().strip()
    except Exception:
        remote_date = None
    return jsonify({"local": local_date, "remote": remote_date})


@app.route("/api/test-connection", methods=["POST"])
def api_test_connection():
    """Test StatsPlus API connection with the provided or saved cookie."""
    cfg = _get_cfg()
    slug = cfg.settings.get("statsplus_slug", "")
    data = request.get_json(silent=True) or {}
    cookie = data.get("cookie", "").strip()
    if not cookie:
        from league_context import get_statsplus_cookie
        cookie = get_statsplus_cookie()
    if not cookie:
        return jsonify({"ok": False, "error": "No cookie configured"})
    try:
        from statsplus import client
        client.configure(slug, cookie)
        date = client.get_date()
        # Only /ratings/ requires auth — test it to validate the cookie
        client._get("/ratings/")
        return jsonify({"ok": True, "game_date": date})
    except client.CookieExpiredError:
        return jsonify({"ok": False, "error": "Cookie expired or invalid — see instructions below to get a fresh one."})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)[:200]})


if __name__ == "__main__":
    app.run(debug=True, port=5001)
