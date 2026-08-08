"""Settings, onboarding, and league-switching routes.

Blueprint: settings_bp
Prefix: none (routes are /, /settings, /onboard/*, /switch-league/<slug>)
"""

import json
import os
import subprocess
import sys
import threading
from pathlib import Path

from flask import Blueprint, g, jsonify, redirect, render_template, request, session

from statsplusplus.evaluation.constants import DEFAULT_MINIMUM_SALARY
from statsplusplus.config.league_context import (
    APP_CONFIG_PATH,
    get_active_league_slug,
    get_league_dir,
    get_statsplus_cookie,
    set_statsplus_cookie,
)
from statsplusplus.utils.logging import get_logger

settings_bp = Blueprint("settings", __name__)
log = get_logger("web.settings")


def _get_cfg():
    """Get config from request context or fallback."""
    if hasattr(g, "league_config"):
        return g.league_config
    from statsplusplus.config.league_config import LeagueConfig; config = LeagueConfig()
    return config


# ── Settings ──


@settings_bp.route("/settings", methods=["GET", "POST"])
def settings():
    import queries
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
            settings_path.write_text(json.dumps(s, indent=2) + "\n")
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
            settings_path.write_text(json.dumps(s, indent=2) + "\n")
            cfg.reload()

        elif action == "save_cookie":
            sid = request.form.get("session_id", "").strip()
            csrf = request.form.get("csrf_token", "").strip()
            cookie = f"sessionid={sid}" if sid else ""
            if cookie and csrf:
                cookie += f";csrftoken={csrf}"
            set_statsplus_cookie(cookie, cfg.league_dir)

        elif action == "save_structure":
            try:
                leagues = json.loads(request.form.get("leagues_json", "[]"))
                if not isinstance(leagues, list):
                    raise ValueError("Must be a JSON array")
                s = cfg.settings
                s["leagues"] = leagues
                flat = {}
                for lg in leagues:
                    for div_name, tids in lg.get("divisions", {}).items():
                        flat[f"{lg['short']} {div_name}"] = tids
                s["divisions"] = flat
                settings_path = cfg.league_dir / "config" / "league_settings.json"
                settings_path.write_text(json.dumps(s, indent=2) + "\n")
                cfg.reload()
            except (ValueError, json.JSONDecodeError, KeyError) as e:
                current_team = queries.get_my_team_id()
                teams = sorted(cfg.team_names_map.items(), key=lambda x: x[1])
                state = queries.get_state()
                from web_league_context import get_db
                conn = get_db()
                counts = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                          for t in ["players", "ratings", "batting_stats", "pitching_stats", "contracts", "teams"]}
                _ck = get_statsplus_cookie(cfg.league_dir)
                _sid, _csrf = "", ""
                for _p in _ck.split(";"):
                    _p = _p.strip()
                    if _p.startswith("sessionid="):
                        _sid = _p.split("=", 1)[1]
                    elif _p.startswith("csrftoken="):
                        _csrf = _p.split("=", 1)[1]
                return render_template("settings.html",
                    current=current_team, teams=teams, cfg=cfg, state=state,
                    session_id=_sid, csrf_token=_csrf, counts=counts,
                    league_groups=cfg.leagues,
                    leagues_json=request.form.get("leagues_json", ""),
                    structure_error=str(e))

        return redirect("/settings")

    # GET — gather all settings data
    current_team = queries.get_my_team_id()
    teams = sorted(cfg.team_names_map.items(), key=lambda x: x[1])
    state = queries.get_state()

    cookie = get_statsplus_cookie(cfg.league_dir)
    session_id, csrf_token = "", ""
    for part in cookie.split(";"):
        part = part.strip()
        if part.startswith("sessionid="):
            session_id = part.split("=", 1)[1]
        elif part.startswith("csrftoken="):
            csrf_token = part.split("=", 1)[1]

    from web_league_context import get_db
    counts = {}
    try:
        conn = get_db()
        for tbl in ["players", "ratings", "batting_stats", "pitching_stats", "contracts", "teams"]:
            counts[tbl] = conn.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
    except Exception:
        counts = {tbl: 0 for tbl in ["players", "ratings", "batting_stats", "pitching_stats", "contracts", "teams"]}

    all_mlb_teams = {int(k): v for k, v in cfg.team_abbr_map.items()}

    return render_template("settings.html",
                           current=current_team, teams=teams,
                           cfg=cfg, state=state,
                           session_id=session_id, csrf_token=csrf_token,
                           counts=counts, league_groups=cfg.leagues,
                           all_mlb_teams=all_mlb_teams,
                           leagues_json=json.dumps(cfg.leagues, indent=2))


# ── League Switching ──


@settings_bp.route("/switch-league/<slug>")
def switch_league(slug):
    league_dir = get_league_dir(slug)
    if not (league_dir / "config" / "league_settings.json").exists():
        return "League not found", 404
    session["active_league"] = slug
    return redirect("/")


# ── Onboarding Wizard ──

_onboard_refresh = {"running": False, "stage": "", "error": "", "done": False, "slug": ""}


@settings_bp.route("/onboard")
def onboard():
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


@settings_bp.route("/onboard/step1", methods=["POST"])
def onboard_step1():
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
    cookie = f"sessionid={session_id}"
    if csrf_token:
        cookie += f";csrftoken={csrf_token}"
    APP_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    app_cfg = json.loads(APP_CONFIG_PATH.read_text()) if APP_CONFIG_PATH.exists() else {}
    app_cfg["statsplus_cookie"] = cookie
    APP_CONFIG_PATH.write_text(json.dumps(app_cfg, indent=2) + "\n")
    try:
        from statsplus import client
        import re
        client.configure(slug, cookie)
        resp = client._get("/ratings/")
        match = re.search(r'https?://\S+', resp)
        ratings_poll_url = match.group(0).rstrip(".)") if match else ""
    except Exception as e:
        return render_template("onboard.html", step=1, slug=slug,
                               session_id=session_id, csrf_token=csrf_token,
                               error=f"Connection failed: {e}")
    return render_template("onboard.html", step=2, slug=slug,
                           ratings_poll_url=ratings_poll_url)


def _run_onboard_refresh(slug, ratings_poll_url=""):
    """Run refresh.py for onboarding, capturing stage progress."""
    _log = get_logger("onboard")
    _log.info("=== onboard refresh started (slug=%s) ===", slug)
    try:
        cookie = get_statsplus_cookie()
        script = Path(__file__).parent.parent / "src" / "statsplusplus" / "data" / "refresh.py"
        cmd = [sys.executable, "-u", str(script)]
        env = {**os.environ, "STATSPLUS_LEAGUE_URL": slug, "STATSPLUS_COOKIE": cookie, "STATSPP_LEAGUE": slug}
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


@settings_bp.route("/onboard/start-refresh", methods=["POST"])
def onboard_start_refresh():
    data = request.get_json(silent=True) or {}
    slug = data.get("slug", "")
    if _onboard_refresh["running"]:
        return jsonify({"status": "already_running"})
    league_dir = APP_CONFIG_PATH.parent / slug
    config_dir = league_dir / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    for sub in ("history", "reports", "tmp"):
        (league_dir / sub).mkdir(exist_ok=True)
    settings_path = config_dir / "league_settings.json"
    if not settings_path.exists():
        settings_path.write_text(json.dumps({
            "league": slug.upper(),
            "statsplus_slug": slug,
            "year": 2033,
            "default_team_id": 1,
            "divisions": {},
            "team_abbr": {},
            "team_names": {},
            "pos_map": {"1": "P", "2": "C", "3": "1B", "4": "2B", "5": "3B", "6": "SS", "7": "LF", "8": "CF", "9": "RF", "10": "DH"},
            "level_map": {"1": "MLB", "2": "AAA", "3": "AA", "4": "A", "5": "A-Short", "6": "Rookie", "7": "Indy", "8": "Intl"},
            "role_map": {"0": "position_player", "11": "starter", "12": "reliever", "13": "closer"},
            "minimum_salary": DEFAULT_MINIMUM_SALARY,
            "pyth_exp": 1.83,
            "wild_cards_per_league": 3,
        }, indent=2) + "\n")
    state_path = config_dir / "state.json"
    if not state_path.exists():
        state_path.write_text(json.dumps({"game_date": "", "year": 2033, "my_team_id": 1}, indent=2) + "\n")
    app_cfg = json.loads(APP_CONFIG_PATH.read_text()) if APP_CONFIG_PATH.exists() else {}
    app_cfg["active_league"] = slug
    APP_CONFIG_PATH.write_text(json.dumps(app_cfg, indent=2) + "\n")
    bootstrap_cookie = get_statsplus_cookie(league_dir)
    if bootstrap_cookie:
        set_statsplus_cookie(bootstrap_cookie, league_dir)

    _onboard_refresh.update(running=True, stage="Starting...", error="", done=False, slug=slug)
    ratings_poll_url = data.get("ratings_poll_url", "")
    threading.Thread(target=_run_onboard_refresh, args=(slug, ratings_poll_url), daemon=True).start()
    return jsonify({"status": "started"})


@settings_bp.route("/onboard/refresh-status")
def onboard_refresh_status():
    return jsonify({
        "running": _onboard_refresh["running"],
        "stage": _onboard_refresh["stage"],
        "error": _onboard_refresh["error"],
        "done": _onboard_refresh["done"],
    })


@settings_bp.route("/onboard/step3", methods=["GET", "POST"])
def onboard_step3():
    slug = request.args.get("slug", "") or request.form.get("slug", "")
    slug = slug.strip()
    league_dir = APP_CONFIG_PATH.parent / slug

    if request.method == "GET":
        from statsplusplus.data import db as _db
        conn = _db.get_conn(league_dir)
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
        settings_path = league_dir / "config" / "league_settings.json"
        s = json.loads(settings_path.read_text()) if settings_path.exists() else {}
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
    s = json.loads(settings_path.read_text())
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
    settings_path.write_text(json.dumps(s, indent=2) + "\n")
    state_path = league_dir / "config" / "state.json"
    st = json.loads(state_path.read_text())
    st["my_team_id"] = team_id
    state_path.write_text(json.dumps(st, indent=2) + "\n")
    try:
        script = Path(__file__).parent.parent / "src" / "statsplusplus" / "data" / "fv_calc.py"
        subprocess.run([sys.executable, str(script)],
                       capture_output=True, text=True, timeout=120)
    except Exception:
        pass
    return render_template("onboard.html", step=4, league_name=league_name)
