"""API routes — AJAX endpoints, CSV export, refresh trigger.

Blueprint: api_bp
Prefix: none (routes are /api/*, /refresh, /refresh/status)
"""

import os
import subprocess
import sys
import threading
from pathlib import Path

from flask import Blueprint, g, jsonify, request, session

from statsplusplus.config.league_context import (
    APP_CONFIG_PATH,
    get_league_dir,
    get_statsplus_cookie,
    set_statsplus_cookie,
    get_statsplus_token,
    set_statsplus_token,
)
from statsplusplus.utils.logging import get_logger

api_bp = Blueprint("api", __name__)
log = get_logger("web.api")

_refresh_lock = threading.Lock()
_refresh_status = {"running": False, "result": None, "message": ""}


def _get_cfg():
    """Get config from request context or fallback."""
    if hasattr(g, "league_config"):
        return g.league_config
    from statsplusplus.config.league_config import LeagueConfig; config = LeagueConfig()
    return config


# ── Simple data endpoints ──


@api_bp.route("/api/toggle-offseason", methods=["POST"])
def toggle_offseason():
    """Flip the manual Offseason-mode flag for the active league.

    Stored in the league's state.json (writable, league-scoped). Phase A uses a
    manual toggle; auto-detection is future work.
    """
    import json
    cfg = _get_cfg()
    state_path = Path(cfg.league_dir) / "config" / "state.json"
    try:
        state = json.loads(state_path.read_text()) if state_path.exists() else {}
        new_val = not bool(state.get("offseason_mode", False))
        state["offseason_mode"] = new_val
        state_path.write_text(json.dumps(state, indent=2) + "\n")
        return jsonify({"ok": True, "offseason_mode": new_val})
    except Exception as e:
        log.error("toggle-offseason failed: %s", e)
        return jsonify({"ok": False, "error": str(e)}), 500


@api_bp.route("/api/prospect/<int:pid>")
def api_prospect(pid):
    import queries
    data = queries.get_prospect_summary(pid)
    if not data:
        return jsonify({"error": "not found"}), 404
    return jsonify(data)


@api_bp.route("/api/player-popup/<int:pid>")
def api_player_popup(pid):
    from player_queries import get_player_popup
    data = get_player_popup(pid)
    if not data:
        return jsonify({"error": "not found"}), 404
    return jsonify(data)


@api_bp.route("/api/player-search")
def api_player_search():
    import queries
    q = request.args.get("q", "").strip()
    if len(q) < 2:
        return jsonify([])
    return jsonify(queries.search_players(q))


@api_bp.route("/api/player-card/<int:pid>")
def api_player_card(pid):
    import queries
    data = queries.get_player_card(pid)
    if not data:
        return jsonify({"error": "not found"}), 404
    return jsonify(data)


@api_bp.route("/api/waiver-wire")
def api_waiver_wire():
    from queries import get_waiver_wire
    return jsonify({"players": get_waiver_wire()})


@api_bp.route("/api/org-players/<int:team_id>")
def api_org_players(team_id):
    import trade_queries
    return jsonify(trade_queries.get_org_players(team_id))


@api_bp.route("/api/trade-value", methods=["POST"])
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


@api_bp.route("/api/save-structure", methods=["POST"])
def api_save_structure():
    import json
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
    settings_path.write_text(json.dumps(s, indent=2) + "\n")
    cfg.reload()
    return jsonify({"ok": True})


@api_bp.route("/api/wipe-league", methods=["POST"])
def api_wipe_league():
    import json
    import shutil

    data = request.get_json(silent=True) or {}
    slug = data.get("slug")
    data_dir = APP_CONFIG_PATH.parent

    existing = [d.name for d in sorted(data_dir.iterdir())
                if d.is_dir() and (d / "config" / "league_settings.json").exists()]

    targets = [slug] if slug else list(existing)
    if slug and slug not in existing:
        return jsonify({"ok": False, "error": "League not found"}), 404

    for s in targets:
        shutil.rmtree(data_dir / s, ignore_errors=True)

    remaining = [d.name for d in sorted(data_dir.iterdir())
                 if d.is_dir() and (d / "config" / "league_settings.json").exists()]

    app_cfg = json.loads(APP_CONFIG_PATH.read_text()) if APP_CONFIG_PATH.exists() else {}
    if remaining:
        app_cfg["active_league"] = remaining[0]
        redirect_to = "/"
    else:
        app_cfg.pop("active_league", None)
        redirect_to = "/onboard"
    APP_CONFIG_PATH.write_text(json.dumps(app_cfg, indent=2) + "\n")

    # Clear session if it pointed to a deleted league
    if session.get("active_league") in targets:
        session.pop("active_league", None)

    return jsonify({"ok": True, "redirect": redirect_to})


@api_bp.route("/api/open-file-location", methods=["POST"])
def api_open_file_location():
    import platform
    import shutil

    data = request.get_json(silent=True) or {}
    path = data.get("path", "")
    if not path:
        return jsonify({"ok": False, "error": "No path"}), 400
    folder = str(Path(path).parent)
    try:
        system = platform.system()
        if system == "Linux":
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


# ── Draft detail ──


@api_bp.route("/api/draft-detail/<int:pid>")
def api_draft_detail(pid):
    """Compact grid data for draft prospect detail panel."""
    from web_league_context import get_db
    from statsplusplus.config.ratings import norm as _pnorm_raw
    _scale = _get_cfg().ratings_scale
    n = lambda v: _pnorm_raw(v, _scale) if v else None

    conn = get_db()
    p = conn.execute("SELECT name, age, pos, role, level FROM players WHERE player_id=?", (pid,)).fetchone()
    if not p:
        return jsonify({"error": "not found"}), 404

    r = conn.execute("SELECT * FROM latest_ratings WHERE player_id=?", (pid,)).fetchone()
    if not r:
        return jsonify({"error": "no ratings"}), 404
    r = dict(zip([d[0] for d in conn.execute("SELECT * FROM latest_ratings LIMIT 0").description], r))

    is_pitcher = p["role"] in (11, 12, 13)
    pos_str = {11: "SP", 12: "RP", 13: "CL"}.get(p["role"]) or {
        1: 'P', 2: 'C', 3: '1B', 4: '2B', 5: '3B', 6: 'SS', 7: 'LF', 8: 'CF', 9: 'RF'
    }.get(p["pos"], "?")

    from statsplusplus.utils.positions import assign_bucket
    from statsplusplus.config.league_config import LeagueConfig; _cfg = LeagueConfig()
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
            pos_str = {2: 'C', 3: '1B', 4: '2B', 5: '3B', 6: 'SS', 7: 'LF', 8: 'CF', 9: 'RF'}.get(p["pos"], bucket)

    from statsplusplus.utils.formatting import height_str as _ht
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
        t_l, t_p, t_c = ["Stuff", "Mov"], [n(r.get("pot_stf")), n(r.get("pot_mov"))], [n(r.get("stf")), n(r.get("mov"))]
        if r.get("hra") or r.get("pot_hra"):
            t_l.append("HRA"); t_p.append(n(r.get("pot_hra"))); t_c.append(n(r.get("hra")))
        if r.get("pbabip") or r.get("pot_pbabip"):
            t_l.append("BA"); t_p.append(n(r.get("pot_pbabip"))); t_c.append(n(r.get("pbabip")))
        t_l.append("Ctrl"); t_p.append(n(r.get("pot_ctrl"))); t_c.append(n(r.get("ctrl")))
        out["tools"] = {"labels": t_l, "pot": t_p, "cur": t_c}
        pitches = []
        for fld, nm in [("fst", "FB"), ("snk", "SI"), ("crv", "CB"), ("sld", "SL"), ("chg", "CH"),
                        ("splt", "SPL"), ("cutt", "CUT"), ("cir_chg", "CC"), ("scr", "SCR"),
                        ("frk", "FRK"), ("kncrv", "KC"), ("knbl", "KN")]:
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
        t_l += ["Gap", "Pow", "Eye"]
        t_p += [n(r.get("pot_gap")), n(r.get("pot_pow")), n(r.get("pot_eye"))]
        t_c += [n(r.get("gap")), n(r.get("pow")), n(r.get("eye"))]
        out["tools"] = {"labels": t_l, "pot": t_p, "cur": t_c}
        out["running"] = {
            "labels": ["Spd", "Stl", "Run", "Sac", "Bunt"],
            "vals": [n(r.get("speed")), n(r.get("steal")), n(r.get("run")), n(r.get("sac_bunt")), n(r.get("bunt_hit"))],
        }
        out["fielding"] = {
            "cols": ["C", "IF", "OF"],
            "range": [None, n(r.get("ifr")), n(r.get("ofr"))],
            "error": [None, n(r.get("ife")), n(r.get("ofe"))],
            "arm": [n(r.get("c_arm")), n(r.get("ifa")), n(r.get("ofa"))],
            "tdp": n(r.get("tdp")),
            "c_blk": n(r.get("c_blk")),
            "c_frm": n(r.get("c_frm")),
        }
        pos_grades = {}
        for col, label in [("c", "C"), ("first_b", "1B"), ("second_b", "2B"), ("third_b", "3B"),
                           ("ss", "SS"), ("lf", "LF"), ("cf", "CF"), ("rf", "RF")]:
            cur = n(r.get(col) or 0)
            pot = n(r.get("pot_" + col) or 0)
            if (cur and cur > 20) or (pot and pot > 20) or label == pos_str:
                pos_grades[label] = [cur or 20, pot or 20]
        out["positions"] = pos_grades

    return jsonify(out)


# ── Percentiles ──


@api_bp.route("/api/player-percentiles/<int:pid>")
def api_player_percentiles(pid):
    """Return percentile rankings for a specific year and optionally a specific level."""
    from percentiles import get_hitter_percentiles, get_pitcher_percentiles, get_fielding_percentiles, available_pctile_years
    from web_league_context import get_db
    year = request.args.get("year", type=int)
    split_id = request.args.get("split", 1, type=int)
    stat_type = request.args.get("type", "main")
    level = request.args.get("level", type=int)
    if year is None:
        return jsonify({"error": "year required"}), 400
    if year == 0:
        _is_pit = False
        conn = get_db()
        _role_check = conn.execute("SELECT role FROM players WHERE player_id=?", (pid,)).fetchone()
        if _role_check:
            _is_pit = _role_check[0] in (11, 12, 13)
        _yrs = available_pctile_years(pid, is_pitcher=_is_pit, level=level)
        year = _yrs[0] if _yrs else None
        if not year:
            return jsonify({"error": "no data for level"}), 404
    conn = get_db()
    role = conn.execute("SELECT role FROM players WHERE player_id=?", (pid,)).fetchone()
    if not role:
        return jsonify({"error": "not found"}), 404

    if stat_type == "fielding":
        data = get_fielding_percentiles(pid, year=year)
        if not data:
            return jsonify({"error": "no data for year"}), 404
        return jsonify({"year": year, "positions": data})

    is_pitcher = role[0] in (11, 12, 13)
    if is_pitcher:
        data = get_pitcher_percentiles(pid, split_id=split_id, year=year, level=level)
    else:
        data = get_hitter_percentiles(pid, split_id=split_id, year=year, level=level)
    if not data:
        return jsonify({"error": "no data for year"}), 404
    years = available_pctile_years(pid, is_pitcher=is_pitcher, level=level)
    return jsonify({"year": year, "stats": data, "level": level, "available_years": years})


@api_bp.route("/api/player-percentile-history/<int:pid>")
def api_player_percentile_history(pid):
    """Return full percentile history for a split."""
    from percentiles import get_percentile_history
    from web_league_context import get_db
    split_id = request.args.get("split", 1, type=int)
    conn = get_db()
    role = conn.execute("SELECT role FROM players WHERE player_id=?", (pid,)).fetchone()
    if not role:
        return jsonify({"error": "not found"}), 404
    is_pitcher = role[0] in (11, 12, 13)
    data = get_percentile_history(pid, is_pitcher=is_pitcher, split_id=split_id)
    if not data:
        return jsonify({"error": "no data"}), 404
    return jsonify(data)


# ── Draft picks ──


@api_bp.route("/api/draft-picks")
def api_draft_picks():
    """Fetch current draft picks from StatsPlus API."""
    try:
        from statsplus import client
        cfg = _get_cfg()
        slug = cfg.settings.get("statsplus_slug", "")
        cookie = get_statsplus_cookie(cfg.league_dir)
        token = get_statsplus_token(cfg.league_dir)
        if slug and (cookie or token):
            client.configure(slug, cookie, token)
        raw = client.get_draft()
        picks = [{"pid": d["ID"], "name": d["Player Name"], "team": d["Team"],
                  "tid": d["Team ID"], "pos": d["Position"], "age": d["Age"],
                  "round": d["Round"], "pick": d["Pick In Round"],
                  "overall": d["Overall"], "college": d["College"]}
                 for d in raw if d.get("ID")]
        return jsonify({"picks": picks})
    except Exception as e:
        return jsonify({"picks": [], "error": str(e)})


# ── Draft pool and simulation ──


@api_bp.route("/api/draft-pool-upload", methods=["POST"])
def api_draft_pool_upload():
    """Upload a CSV of draft-eligible player IDs exported from OOTP."""
    import csv
    import io
    import json

    f = request.files.get("file")
    if not f:
        return jsonify({"ok": False, "error": "No file uploaded"}), 400
    try:
        text = f.read().decode("utf-8-sig")
        reader = csv.DictReader(io.StringIO(text))
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
        pool_path = get_league_dir() / "config" / "draft_pool.json"
        pool_path.write_text(json.dumps({"player_ids": pids}, indent=2))
        return jsonify({"ok": True, "total": len(pids)})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400


@api_bp.route("/api/draft-sim", methods=["POST"])
def api_draft_sim():
    """Run a draft simulation."""
    data = request.get_json(silent=True) or {}
    try:
        from draft_board import load_board, simulate_draft
        from draft_settings import load_settings

        league_dir = get_league_dir()
        rows, adp, needs, num_teams, conn = load_board()
        pick_pos = data.get("pick", 30)
        num_rounds = data.get("rounds", 7)
        seed = data.get("seed")

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


@api_bp.route("/api/draft-upload-list", methods=["POST"])
def api_draft_upload_list():
    """Generate the auto-draft list using saved settings and return it."""
    data = request.get_json(silent=True) or {}
    limit = data.get("top", 500)
    exclude_pids = set(data.get("exclude", []))

    try:
        from draft_board import load_board, build_pick_list
        from draft_settings import load_settings

        league_dir = get_league_dir()
        rows, adp, needs, num_teams, conn = load_board()

        if exclude_pids:
            rows = [r for r in rows if r["player_id"] not in exclude_pids]

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


@api_bp.route("/api/draft-settings", methods=["GET"])
def api_draft_settings_get():
    """Return current draft board settings for the active league."""
    try:
        from draft_settings import load_settings, PRESETS
        settings = load_settings(get_league_dir())
        return jsonify({"ok": True, "settings": settings, "presets": PRESETS})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@api_bp.route("/api/draft-settings", methods=["POST"])
def api_draft_settings_post():
    """Save draft board settings for the active league."""
    data = request.get_json(silent=True) or {}
    settings = data.get("settings")
    if not settings:
        return jsonify({"ok": False, "error": "Missing 'settings' in request body"}), 400
    try:
        from draft_settings import save_settings
        save_settings(get_league_dir(), settings)
        return jsonify({"ok": True})
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@api_bp.route("/api/draft-settings/copy", methods=["POST"])
def api_draft_settings_copy():
    """Copy draft settings from another league to the active league."""
    import json
    data = request.get_json(silent=True) or {}
    from_league = data.get("from_league")
    if not from_league:
        return jsonify({"ok": False, "error": "Missing 'from_league'"}), 400
    try:
        from draft_settings import copy_settings

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


# ── Refresh ──


def _run_refresh(slug, league_dir, statsplus_slug, cookie, token=""):
    """Run refresh.py in background thread."""
    _log = get_logger("web")
    _log.info("=== dashboard refresh started (league=%s) ===", slug)
    try:
        from statsplusplus.data import db as _db_mod
        from statsplusplus.config.league_config import LeagueConfig
        bg_cfg = LeagueConfig(base_dir=league_dir)
        script = Path(__file__).parent.parent / "src" / "statsplusplus" / "data" / "refresh.py"
        env = {**os.environ, "STATSPP_LEAGUE": slug, "STATSPLUS_COOKIE": cookie}
        if token:
            env["STATSPLUS_TOKEN"] = token
        if statsplus_slug:
            env["STATSPLUS_LEAGUE_URL"] = statsplus_slug
        result = subprocess.run(
            [sys.executable, str(script), str(bg_cfg.year)],
            capture_output=True, text=True, timeout=600, env=env,
        )
        if result.stdout:
            for line in result.stdout.strip().splitlines():
                _log.debug(line)
        if result.returncode == 0:
            import json
            bg_cfg.reload()
            state = json.loads(bg_cfg.state_path.read_text())
            warnings = []
            try:
                conn = _db_mod.get_connection(league_dir)
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
            err = result.stderr.strip() or result.stdout.strip()
            _log.error("refresh failed (rc=%d):\n%s", result.returncode, err)
            lines = err.splitlines()
            last = lines[-1] if lines else "Unknown error"
            if "CookieExpiredError" in err or "requires user to be logged in" in err:
                last = "StatsPlus session expired — update your cookie in Settings."
            elif "TokenExpiredError" in err or "API token has expired" in err or "Invalid or unknown API token" in err:
                last = "StatsPlus API token expired or invalid — log in on StatsPlus to refresh it, then update it in Settings."
            elif "RateLimitedError" in err or "once per 5 minutes" in err or "Request too soon" in err:
                # Surface the wait time if present.
                import re as _re
                _m = _re.search(r"in about (\d+) seconds", err) or _re.search(r"wait (\d+) seconds", err)
                _secs = _m.group(1) if _m else None
                last = ("StatsPlus limits ratings pulls to once per 5 minutes. "
                        + (f"Try again in about {_secs} seconds." if _secs else "Try again shortly."))
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


@api_bp.route("/refresh", methods=["POST"])
def refresh():
    if not _refresh_lock.acquire(blocking=False):
        return jsonify({"status": "already_running"}), 409
    cfg = _get_cfg()
    slug = g.league_slug
    league_dir = g.league_dir
    statsplus_slug = cfg.settings.get("statsplus_slug", "")
    cookie = get_statsplus_cookie(league_dir)
    token = get_statsplus_token(league_dir)
    _refresh_status["running"] = True
    _refresh_status["result"] = None
    _refresh_status["message"] = ""
    threading.Thread(target=_run_refresh, args=(slug, league_dir, statsplus_slug, cookie, token), daemon=True).start()
    return jsonify({"status": "started"})


@api_bp.route("/refresh/status")
def refresh_status():
    return jsonify({
        "running": _refresh_status["running"],
        "result": _refresh_status["result"],
        "message": _refresh_status["message"],
    })


# ── Connection management ──


@api_bp.route("/api/game-date")
def api_game_date():
    """Return local and remote game dates for staleness check."""
    import queries
    local_date = queries.get_state().get("game_date", "")
    try:
        from statsplus import client
        cfg = _get_cfg()
        slug = cfg.settings.get("statsplus_slug", "")
        cookie = get_statsplus_cookie(cfg.league_dir)
        token = get_statsplus_token(cfg.league_dir)
        if slug and (cookie or token):
            client.configure(slug, cookie, token)
        remote_date = client.get_date().strip()
    except Exception:
        remote_date = None
    return jsonify({"local": local_date, "remote": remote_date})


@api_bp.route("/api/session-cookie")
def api_session_cookie():
    """Return current session cookie components + API token for the active league."""
    league_dir = _get_cfg().league_dir
    cookie = get_statsplus_cookie(league_dir) or ""
    sid = ""
    csrf = ""
    for part in cookie.split(";"):
        part = part.strip()
        if part.startswith("sessionid="):
            sid = part[len("sessionid="):]
        elif part.startswith("csrftoken="):
            csrf = part[len("csrftoken="):]
    token = get_statsplus_token(league_dir) or ""
    return jsonify({"session_id": sid, "csrf_token": csrf, "token": token})


@api_bp.route("/api/save-session-cookie", methods=["POST"])
def api_save_session_cookie():
    """Save a new session cookie for the active league."""
    data = request.get_json(silent=True) or {}
    sid = data.get("session_id", "").strip()
    csrf = data.get("csrf_token", "").strip()
    cookie = f"sessionid={sid}" if sid else ""
    if cookie and csrf:
        cookie += f";csrftoken={csrf}"
    try:
        set_statsplus_cookie(cookie, _get_cfg().league_dir)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@api_bp.route("/api/save-token", methods=["POST"])
def api_save_token():
    """Save the StatsPlus API token for the active league."""
    data = request.get_json(silent=True) or {}
    token = (data.get("token") or "").strip()
    try:
        set_statsplus_token(token, _get_cfg().league_dir)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@api_bp.route("/api/test-connection", methods=["POST"])
def api_test_connection():
    """Test the StatsPlus API connection.

    Accepts either a ``token`` or a ``cookie`` in the JSON body (falling back to
    the saved credentials). A token is validated via the documented
    /tokencheck endpoint (returns the team it maps to); a cookie is tested by
    fetching the game date.
    """
    cfg = _get_cfg()
    slug = cfg.settings.get("statsplus_slug", "")
    data = request.get_json(silent=True) or {}
    from statsplus import client

    # Token path (preferred) — validate via /tokencheck.
    token = (data.get("token") or "").strip()
    if not token and "cookie" not in data:
        token = get_statsplus_token(cfg.league_dir)
    if token:
        ok, detail = client.tokencheck(slug, token)
        if not ok:
            return jsonify({"ok": False, "error": f"Token check failed: {detail}"})
        # Token is valid; also report the game date for a friendly confirmation.
        try:
            client.configure(slug, "", token)
            date = client.get_date()
        except Exception:
            date = None
        return jsonify({"ok": True, "method": "token", "team_id": detail, "game_date": date})

    # Cookie path (fallback).
    cookie = (data.get("cookie") or "").strip()
    if not cookie:
        cookie = get_statsplus_cookie(cfg.league_dir)
    if not cookie:
        return jsonify({"ok": False, "error": "No token or cookie configured"})
    try:
        client.configure(slug, cookie, "")
        date = client.get_date()  # cheap, non-rate-limited — do not spend /ratings budget on a test
        return jsonify({"ok": True, "method": "cookie", "game_date": date})
    except client.TokenExpiredError:
        return jsonify({"ok": False, "error": "Token expired or invalid — log in on StatsPlus to refresh it."})
    except client.CookieExpiredError:
        return jsonify({"ok": False, "error": "Cookie expired or invalid — see instructions below to get a fresh one."})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)[:200]})
