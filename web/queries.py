"""DB queries for the web dashboard — state helpers and league-level queries.

Team queries: web/team_queries.py
Player queries: web/player_queries.py
Percentiles: web/percentiles.py

Note: query functions use sqlite3.Row access. Integer indexing (r[0]) still works
of sqlite3.Row. This is intentional — these functions use positional indexing (r[0],
r[1], etc.) for performance. Do not change without updating all index references.
"""

import os, sys, json, math

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "scripts"))
from statsplusplus.utils.positions import display_pos as _display_pos
from statsplusplus.config.ratings import norm as _norm_raw, norm_floor as _norm_floor_raw
from web_league_context import (get_db, get_cfg, team_abbr_map, team_names_map, pos_order, year, mlb_team_ids, level_map,
                                 money_divisor as _money_divisor, dev_cell as _dev_cell)
from statsplusplus.utils.positions import ROLE_MAP

# Wrap norm functions to use the request-scoped scale
def _norm(val):
    return _norm_raw(val, get_cfg().ratings_scale)

def _norm_floor(val, floor=20):
    return _norm_floor_raw(val, get_cfg().ratings_scale, floor)

# Legacy module-level aliases — used by app.py and re-export consumers.
# These are properties that re-evaluate each access via get_cfg().
class _DynMap:
    """Lazy proxy so `queries.TEAM_NAMES[tid]` still works in request context."""
    def __init__(self, fn): self._fn = fn
    def get(self, k, d=None): return self._fn().get(k, d)
    def __getitem__(self, k): return self._fn()[k]
    def __contains__(self, k): return k in self._fn()
    def keys(self): return self._fn().keys()
    def values(self): return self._fn().values()
    def items(self): return self._fn().items()

TEAM_ABBR = _DynMap(team_abbr_map)
TEAM_NAMES = _DynMap(team_names_map)


# ── state helpers ────────────────────────────────────────────────────────

def get_state(force=False):
    cfg = get_cfg()
    if force:
        cfg.reload()
    with open(cfg.state_path) as f:
        return json.load(f)


def get_my_team_id():
    return get_cfg().my_team_id


def get_my_team_abbr():
    cfg = get_cfg()
    return cfg.team_abbr(cfg.my_team_id)


def set_my_team(team_id):
    cfg = get_cfg()
    state = get_state()
    state["my_team_id"] = team_id
    from statsplusplus.config.league_context import atomic_write_text
    atomic_write_text(cfg.state_path, json.dumps(state, indent=2) + "\n")
    cfg.reload()


# ── league-level queries ────────────────────────────────────────────────

def _get_prospect_eval_date():
    """Get the most recent prospect_fv eval_date, cached per request."""
    from flask import g as _g, has_request_context as _hrc
    if _hrc() and hasattr(_g, "_pf_eval_date_cache"):
        return _g._pf_eval_date_cache
    conn = get_db()
    ed = conn.execute("SELECT MAX(eval_date) FROM prospect_fv").fetchone()[0]
    if _hrc():
        _g._pf_eval_date_cache = ed
    return ed


_ETA_BASE = {"MLB": 0, "AAA": 1, "AA": 2, "A": 3,
             "A-Short": 4, "Rookie": 4, "USL": 5, "DSL": 5, "Intl": 5}

def _calc_eta(level, ovr, pot):
    """ETA in calendar year. Pull forward if Ovr already MLB-viable (≥45)."""
    base = _ETA_BASE.get(level, 3)
    if ovr and ovr >= 45 and base > 0:
        base = max(base - 1, 0)
    return year() + base

def get_top_prospects(n=100):
    conn = get_db()
    ed = _get_prospect_eval_date()

    _abbr = team_abbr_map()
    _names = team_names_map()
    _po = pos_order()
    mlb_tids = mlb_team_ids()
    rows = conn.execute("""
        SELECT p.name, p.age, COALESCE(NULLIF(p.organization_id,0), NULLIF(p.parent_team_id,0), p.team_id) as org_id,
               pf.fv, pf.fv_str, pf.bucket,
               pf.level, pf.prospect_surplus, p.pos, p.player_id,
               r.height, r.bats, r.throws, r.ovr, r.pot,
               r.composite_score, r.ceiling_score, pf.risk,
               ds.available, ds.css_class, ds.label, ds.confidence, ds.z,
               ds.schedule_status, ds.schedule_label, ds.schedule_note
        FROM prospect_fv pf
        JOIN players p ON pf.player_id=p.player_id
        LEFT JOIN latest_ratings r ON pf.player_id=r.player_id
        LEFT JOIN dev_speed ds ON pf.player_id=ds.player_id AND ds.eval_date=pf.eval_date
        WHERE pf.eval_date=? AND p.age <= 25
    """, (ed,)).fetchall()

    rows = [r for r in rows if r[2] in mlb_tids]

    def sort_key(r):
        fv_val = r[3] + (0.1 if r[4].endswith("+") else 0)
        return (-fv_val, -(r[7] or 0))

    rows = sorted(rows, key=sort_key)[:n]

    def fmt_ht(cm):
        if not cm: return ""
        ft = int(cm / 30.48)
        inch = round((cm % 30.48) / 2.54)
        return f"{ft}'{inch}\""

    return [{"rank": i + 1, "name": r[0], "age": r[1],
             "team": _abbr.get(r[2], "FA"), "tid": r[2],
             "team_name": _names.get(r[2], ""),
             "fv": r[3], "fv_str": r[4],
             "bucket": _display_pos(r[5], r[8]), "level": r[6],
             "pos_order": _po.get(_display_pos(r[5], r[8]), 99),
             "surplus": round(r[7] / _money_divisor(), 1) if r[7] else 0,
             "pid": r[9],
             "eta": _calc_eta(r[6], r[13], r[14]),
             "height": fmt_ht(r[10]),
             "bats": r[11] or "", "throws": r[12] or "",
             "composite_score": r[15], "ceiling_score": r[16], "risk": r[17],
             "dev": _dev_cell(r, 18)}
            for i, r in enumerate(rows)]


def _build_league_team_sets():
    """Build team sets per league dynamically from config.leagues."""
    cfg = get_cfg()
    result = {}
    for lg in cfg.leagues:
        tids = set()
        for div_tids in lg["divisions"].values():
            tids.update(div_tids)
        result[lg["short"]] = tids
    return result


def _build_batting_leaders(rows, pa_qual, n=5):
    """Build stat leader panels from a list of batting rows."""
    _abbr = team_abbr_map()
    def top(rows, key, fmt, n=n, low=False, qual=False):
        pool = [(r, key(r)) for r in rows if key(r) is not None and (not qual or (r[12] or 0) >= pa_qual)]
        pool.sort(key=lambda x: x[1], reverse=not low)
        return [{"pid": r[0], "name": r[1], "team": _abbr.get(r[2], "?"),
                 "tid": r[2], "val": fmt(v)} for r, v in pool[:n]]
    return {
        "AVG": top(rows, lambda r: r[4]/r[3] if r[3] else None, lambda v: f"{v:.3f}", qual=True),
        "HR":  top(rows, lambda r: r[7], str),
        "RBI": top(rows, lambda r: r[8], str),
        "SB":  top(rows, lambda r: r[11], str),
        "OPS": top(rows, lambda r: ((r[4]+r[9]+(r[15] or 0))/r[12] + (r[4]+r[5]+2*r[6]+3*r[7])/r[3]) if r[3] and r[12] and (r[12] or 0) >= pa_qual else None,
                    lambda v: f"{v:.3f}", qual=True),
        "WAR": top(rows, lambda r: r[13], lambda v: f"{v:.1f}"),
    }


def get_batting_leaders(yr=None):
    """Top 5 per stat, keyed by 'All' + each league short name.

    Rate-stat panels (AVG, OPS) are gated by a games-scaled qualifier
    (``pa_qual`` = 3.1 PA/team-game) so they stay meaningful all season.
    Counting-stat panels (HR, RBI, SB, WAR) are ungated — a home-run
    leaderboard should not require a full-season plate-appearance minimum.
    No playing-time floor is applied to the row set; the qualifier is the
    only gate, and the top-N selector ignores NULL values.
    """
    yr = yr or year()
    conn = get_db()
    tip = conn.execute("SELECT AVG(ip) FROM team_pitching_stats WHERE year=? AND split_id=1",
                       (yr,)).fetchone()
    team_g = round(tip[0] / 9) if tip and tip[0] else 0
    pa_qual = round(3.1 * team_g)
    rows = conn.execute("""
        SELECT p.player_id, p.name, p.team_id,
               b.ab, b.h, b.d, b.t, b.hr, b.rbi, b.bb, b.k, b.sb, b.pa, b.war, b.r, b.hbp
        FROM mlb_batting_stats b JOIN players p ON b.player_id=p.player_id
        WHERE b.year=? AND b.split_id=1
        ORDER BY b.war DESC
    """, (yr,)).fetchall()
    league_sets = _build_league_team_sets()
    result = {"All": _build_batting_leaders(rows, pa_qual)}
    for lg_short, tids in league_sets.items():
        result[lg_short] = _build_batting_leaders([r for r in rows if r[2] in tids], pa_qual)
    return result


def _build_pitching_leaders(rows, ip_qual, n=5):
    """Build stat leader panels from a list of pitching rows."""
    _abbr = team_abbr_map()
    ip_ok = lambda r: (r[3] or 0) >= ip_qual
    def top(rows, key, fmt, n=n, low=False, qual=False):
        pool = [(r, key(r)) for r in rows if key(r) is not None and (not qual or ip_ok(r))]
        pool.sort(key=lambda x: x[1], reverse=not low)
        return [{"pid": r[0], "name": r[1], "team": _abbr.get(r[2], "?"),
                 "tid": r[2], "val": fmt(v)} for r, v in pool[:n]]
    return {
        "ERA":  top(rows, lambda r: r[4] if ip_ok(r) else None, lambda v: f"{v:.2f}", low=True, qual=True),
        "W":    top(rows, lambda r: r[7], str),
        "K":    top(rows, lambda r: r[5], str),
        "SV":   top(rows, lambda r: r[9] if r[9] else None, str),
        "WHIP": top(rows, lambda r: (r[6]+r[11])/r[3] if ip_ok(r) and r[3] else None,
                     lambda v: f"{v:.2f}", low=True, qual=True),
        "WAR":  top(rows, lambda r: r[10], lambda v: f"{v:.1f}"),
    }


def get_pitching_leaders(yr=None):
    """Top 5 per stat, keyed by 'All' + each league short name.

    Rate-stat panels (ERA, WHIP) are gated by a games-scaled qualifier
    (``ip_qual`` = 1.0 IP/team-game). Counting-stat panels (W, K, SV, WAR) are
    ungated — a saves leaderboard should not require a full-season innings
    minimum. No playing-time floor is applied to the row set; the qualifier is
    the only gate, and the top-N selector ignores NULL values.
    """
    yr = yr or year()
    conn = get_db()
    tip = conn.execute("SELECT AVG(ip) FROM team_pitching_stats WHERE year=? AND split_id=1",
                       (yr,)).fetchone()
    team_g = round(tip[0] / 9) if tip and tip[0] else 0
    ip_qual = round(1.0 * team_g)
    rows = conn.execute("""
        SELECT p.player_id, p.name, p.team_id,
               ps.ip, ps.era, ps.k, ps.bb, ps.w, ps.l, ps.sv, ps.war, ps.ha, ps.hld
        FROM mlb_pitching_stats ps JOIN players p ON ps.player_id=p.player_id
        WHERE ps.year=? AND ps.split_id=1
        ORDER BY ps.war DESC
    """, (yr,)).fetchall()
    league_sets = _build_league_team_sets()
    result = {"All": _build_pitching_leaders(rows, ip_qual)}
    for lg_short, tids in league_sets.items():
        result[lg_short] = _build_pitching_leaders([r for r in rows if r[2] in tids], ip_qual)
    return result


def search_players(query):
    """League-wide player search. Returns up to 15 matches (MLB first, then prospects)."""
    if not query or len(query) < 2:
        return []
    conn = get_db()
    _abbr = team_abbr_map()
    _lm = level_map()
    _pm = {str(k): v for k, v in get_cfg().pos_map.items()}
    like = f"%{query}%"
    rows = conn.execute("""
        SELECT p.player_id, p.name, p.age, p.level,
               COALESCE(NULLIF(p.organization_id,0), NULLIF(p.parent_team_id,0), p.team_id) AS org_id,
               r.ovr, pf.fv, COALESCE(pf.bucket, ps.bucket) AS bucket, p.pos,
               r.composite_score
        FROM players p
        LEFT JOIN latest_ratings r ON p.player_id = r.player_id
        LEFT JOIN prospect_fv pf ON p.player_id = pf.player_id
        LEFT JOIN player_surplus ps ON p.player_id = ps.player_id
        WHERE p.name LIKE ?
        ORDER BY (CASE WHEN p.level = '1' THEN 0 ELSE 1 END),
                 COALESCE(r.composite_score, r.ovr, 0) DESC
        LIMIT 15
    """, (like,)).fetchall()
    return [{"pid": r[0], "name": r[1], "age": r[2],
             "level": _lm.get(str(r[3]), str(r[3])),
             "team": _abbr.get(r[4], "FA"),
             "ovr": r[9] if r[9] is not None else r[5],
             "fv": r[6],
             "pos": _display_pos(r[7], r[8]) if r[7] else _pm.get(str(r[8]), "?")}
            for r in rows]


def get_all_prospects():
    """All FV≥40 prospects for by-team/by-position views."""
    conn = get_db()
    ed = _get_prospect_eval_date()

    _abbr = team_abbr_map()
    _names = team_names_map()
    _po = pos_order()
    mlb_tids = mlb_team_ids()
    rows = conn.execute("""
        SELECT p.name, p.age, COALESCE(NULLIF(p.organization_id,0), NULLIF(p.parent_team_id,0), p.team_id) as org_id,
               pf.fv, pf.fv_str, pf.bucket,
               pf.level, pf.prospect_surplus, p.pos, p.player_id,
               r.height, r.bats, r.throws, r.ovr, r.pot,
               r.composite_score, r.ceiling_score, pf.risk,
               ds.available, ds.css_class, ds.label, ds.confidence, ds.z,
               ds.schedule_status, ds.schedule_label, ds.schedule_note
        FROM prospect_fv pf
        JOIN players p ON pf.player_id=p.player_id
        LEFT JOIN latest_ratings r ON pf.player_id=r.player_id
        LEFT JOIN dev_speed ds ON pf.player_id=ds.player_id AND ds.eval_date=pf.eval_date
        WHERE pf.eval_date=? AND pf.fv >= 40 AND p.age <= 25
    """, (ed,)).fetchall()

    rows = [r for r in rows if r[2] in mlb_tids]

    def sort_key(r):
        fv_val = r[3] + (0.1 if r[4].endswith("+") else 0)
        return (-fv_val, -(r[7] or 0))

    rows = sorted(rows, key=sort_key)

    def fmt_ht(cm):
        if not cm: return ""
        ft = int(cm / 30.48)
        inch = round((cm % 30.48) / 2.54)
        return f"{ft}'{inch}\""

    return [{"name": r[0], "age": r[1],
             "team": _abbr.get(r[2], "FA"), "tid": r[2],
             "team_name": _names.get(r[2], ""),
             "fv": r[3], "fv_str": r[4],
             "bucket": _display_pos(r[5], r[8]), "level": r[6],
             "pos_order": _po.get(_display_pos(r[5], r[8]), 99),
             "surplus": round(r[7] / _money_divisor(), 1) if r[7] else 0,
             "pid": r[9],
             "eta": _calc_eta(r[6], r[13], r[14]),
             "height": fmt_ht(r[10]),
             "bats": r[11] or "", "throws": r[12] or "",
             "composite_score": r[15], "ceiling_score": r[16], "risk": r[17],
             "dev": _dev_cell(r, 18)}
            for r in rows]


def get_prospect_summary(pid):
    """Prospect side-panel data: ratings, FV, surplus, scouting summary."""
    conn = get_db()
    ed = _get_prospect_eval_date()

    pf = conn.execute("""
        SELECT pf.fv, pf.fv_str, pf.bucket, pf.level, pf.prospect_surplus,
               p.name, p.age,
               COALESCE(NULLIF(p.organization_id,0), NULLIF(p.parent_team_id,0), p.team_id),
               p.role, pf.risk
        FROM prospect_fv pf JOIN players p ON pf.player_id=p.player_id
        WHERE pf.eval_date=? AND pf.player_id=?
    """, (ed, pid)).fetchone()
    if not pf:
        return None

    fv, fv_str, bucket, level, surplus, name, age, tid, role, risk = pf

    r = conn.execute("SELECT * FROM latest_ratings WHERE player_id=?", (pid,)).fetchone()
    if not r:
        return None
    cols = [d[0] for d in conn.execute("SELECT * FROM latest_ratings LIMIT 0").description]
    rd = dict(zip(cols, r))

    is_pitcher = bucket in ('SP', 'RP')

    ovr_val = rd.get("ovr")
    pot_val = rd.get("pot")

    out = {
        "pid": pid, "name": name, "age": age, "bucket": bucket, "level": level,
        "team": TEAM_ABBR.get(tid, "FA"), "team_name": TEAM_NAMES.get(tid, ""),
        "fv": fv, "fv_str": fv_str,
        # NOTE: stays in raw millions (not money_divisor-scaled) — this feeds
        # the JS renderPanel()/fmtSurplus(), which already does its own
        # per-value adaptive M/K formatting assuming millions-scale input.
        "surplus": round(surplus / 1e6, 1) if surplus else 0,
        "eta": _calc_eta(level, ovr_val, pot_val),
        "ovr": ovr_val, "pot": pot_val,
        "composite_score": rd.get("composite_score"),
        "ceiling_score": rd.get("ceiling_score"),
        "height": _fmt_ht(rd.get("height")),
        "bats": rd.get("bats", ""), "throws": rd.get("throws", ""),
    }

    _build_tools(rd, is_pitcher, out)

    # Scouting summary
    import json as _json
    try:
        league_dir = get_cfg().league_dir
        with open(os.path.join(str(league_dir), "history", "prospects.json")) as f:
            pros = _json.load(f)
        entry = pros.get(str(pid))
        if entry and entry.get("summary"):
            out["summary"] = entry["summary"]
    except (FileNotFoundError, _json.JSONDecodeError):
        pass

    return out


def _build_tools(rd, is_pitcher, out):
    """Populate out with tools, pitches, defense from a ratings dict."""
    n80 = _norm
    if is_pitcher:
        ctrl_r, ctrl_l = rd.get("ctrl_r", 0) or 0, rd.get("ctrl_l", 0) or 0
        ctrl = rd.get("ctrl") or (round((ctrl_r + ctrl_l) / 2) if ctrl_r and ctrl_l else ctrl_r or ctrl_l)
        tools = [
            {"name": "Stuff", "cur": n80(rd.get("stf")), "fut": n80(rd.get("pot_stf"))},
            {"name": "Movement", "cur": n80(rd.get("mov")), "fut": n80(rd.get("pot_mov"))},
        ]
        if rd.get("hra") is not None:
            tools.append({"name": "HR Allow", "cur": n80(rd["hra"]), "fut": n80(rd.get("pot_hra")), "sub": True})
        if rd.get("pbabip") is not None:
            tools.append({"name": "BABIP Allow", "cur": n80(rd["pbabip"]), "fut": n80(rd.get("pot_pbabip")), "sub": True})
        tools.append({"name": "Control", "cur": n80(ctrl), "fut": n80(rd.get("pot_ctrl"))})
        if rd.get("stm"):
            tools.append({"name": "Stamina", "cur": n80(rd["stm"]), "fut": None})
        out["tools"] = tools
        if rd.get("vel"):
            out["velocity"] = rd["vel"]
        pitch_map = [
            ("fst", "Fastball"), ("snk", "Sinker"), ("crv", "Curveball"),
            ("sld", "Slider"), ("chg", "Changeup"), ("splt", "Splitter"),
            ("cutt", "Cutter"), ("cir_chg", "Circle Change"), ("scr", "Screwball"),
            ("frk", "Forkball"), ("kncrv", "Knuckle Curve"), ("knbl", "Knuckleball"),
        ]
        pitches = []
        for col, label in pitch_map:
            cur, fut = rd.get(col), rd.get(f"pot_{col}")
            if cur or fut:
                pitches.append({"name": label, "cur": n80(cur), "pot": n80(fut)})
        pitches.sort(key=lambda x: -(x["cur"] or 0))
        out["pitches"] = pitches[:5]
    else:
        tools = [
            {"name": "Hit", "cur": n80(rd.get("cntct")), "fut": n80(rd.get("pot_cntct"))},
        ]
        if rd.get("babip") is not None:
            tools.append({"name": "BABIP", "cur": n80(rd["babip"]), "fut": n80(rd.get("pot_babip")), "sub": True})
        tools.append({"name": "Avoid K's", "cur": n80(rd.get("ks")), "fut": n80(rd.get("pot_ks")), "sub": True})
        tools += [
            {"name": "Gap", "cur": n80(rd.get("gap")), "fut": n80(rd.get("pot_gap"))},
            {"name": "Power", "cur": n80(rd.get("pow")), "fut": n80(rd.get("pot_pow"))},
            {"name": "Eye", "cur": n80(rd.get("eye")), "fut": n80(rd.get("pot_eye"))},
        ]
        tools.append({"name": "Speed", "cur": n80(rd.get("speed")), "fut": n80(rd.get("speed"))})
        out["tools"] = tools
        def_map = {"C": "c", "1B": "first_b", "2B": "second_b", "3B": "third_b",
                    "SS": "ss", "LF": "lf", "CF": "cf", "RF": "rf"}
        _def_order = ["C", "1B", "2B", "3B", "SS", "LF", "CF", "RF"]
        defense = []
        for pos_lbl, col in def_map.items():
            cur = n80(rd.get(col))
            fut = n80(rd.get(f"pot_{col}"))
            if (cur and cur > 20) or (fut and fut > 20):
                defense.append({"pos": pos_lbl, "cur": cur or 20, "fut": fut or 20})
        defense.sort(key=lambda x: _def_order.index(x["pos"]))
        out["defense"] = defense
        # Defensive tools — only show tools relevant to positions the player can play
        has_if = any(d["pos"] in ("2B", "3B", "SS", "1B") for d in defense)
        has_of = any(d["pos"] in ("LF", "CF", "RF") for d in defense)
        has_c = any(d["pos"] == "C" for d in defense)
        def_tools = []
        if has_if:
            for label, col in [("IF Range", "ifr"), ("IF Error", "ife"), ("IF Arm", "ifa"), ("TDP", "tdp")]:
                v = n80(rd.get(col))
                if v and v > 20:
                    def_tools.append({"name": label, "val": v})
        if has_of:
            for label, col in [("OF Range", "ofr"), ("OF Error", "ofe"), ("OF Arm", "ofa")]:
                v = n80(rd.get(col))
                if v and v > 20:
                    def_tools.append({"name": label, "val": v})
        if has_c:
            for label, col in [("C Arm", "c_arm"), ("C Block", "c_blk"), ("C Frame", "c_frm")]:
                v = n80(rd.get(col))
                if v and v > 20:
                    def_tools.append({"name": label, "val": v})
        out["def_tools"] = def_tools


def get_player_card(pid):
    """Side-panel-style card data for any player (MLB or prospect)."""
    conn = get_db()
    _abbr = team_abbr_map()

    p = conn.execute("""
        SELECT p.name, p.age, p.role, p.team_id, p.parent_team_id, p.level, p.organization_id
        FROM players p WHERE p.player_id = ?
    """, (pid,)).fetchone()
    if not p:
        return None
    name, age, role, tid, ptid, level, org = p
    org_id = org or ptid or tid
    is_pitcher = role in (11, 12, 13)
    bucket_row = conn.execute(
        "SELECT bucket FROM player_surplus WHERE player_id=? "
        "UNION ALL SELECT bucket FROM prospect_fv WHERE player_id=? LIMIT 1",
        (pid, pid)).fetchone()
    if bucket_row:
        bucket = bucket_row[0]
    elif is_pitcher:
        bucket = {11: "SP", 12: "RP", 13: "RP"}.get(role, "SP")
    else:
        # Derive bucket from ratings for amateur/untracked players
        r_tmp = conn.execute("SELECT * FROM latest_ratings WHERE player_id=?", (pid,)).fetchone()
        if r_tmp:
            cols_tmp = [d[0] for d in conn.execute("SELECT * FROM latest_ratings LIMIT 0").description]
            rd_tmp = dict(zip(cols_tmp, r_tmp))
            n = _norm
            p_dict = {"Age": age, "Pos": "P" if is_pitcher else str(role or ""),
                       "_role": {11:"starter",12:"reliever",13:"closer"}.get(role, "position_player"),
                       "_is_pitcher": is_pitcher, "Pot": rd_tmp.get("pot", 0)}
            for f, c in [("PotC","pot_c"),("PotSS","pot_ss"),("Pot2B","pot_second_b"),
                         ("Pot3B","pot_third_b"),("Pot1B","pot_first_b"),
                         ("PotCF","pot_cf"),("PotLF","pot_lf"),("PotRF","pot_rf")]:
                p_dict[f] = rd_tmp.get(c, 0)
            bucket = assign_bucket(p_dict)
        else:
            bucket = "?"

    r = conn.execute("SELECT * FROM latest_ratings WHERE player_id=?", (pid,)).fetchone()
    if not r:
        return None
    cols = [d[0] for d in conn.execute("SELECT * FROM latest_ratings LIMIT 0").description]
    rd = dict(zip(cols, r))

    _lm = level_map()
    _pos_str = {11:"SP",12:"RP",13:"CL"}.get(role) if is_pitcher else bucket
    _composite = rd.get("composite_score")
    _tool_only = rd.get("tool_only_score")
    _ceiling = rd.get("ceiling_score")
    out = {
        "pid": pid, "name": name, "age": age, "bucket": bucket,
        "pos": _pos_str,
        "level": _lm.get(str(level), str(level)),
        "team": _abbr.get(org_id, "FA"),
        "ovr": rd.get("ovr"), "pot": rd.get("pot"),
        "composite_score": _composite,
        "ceiling_score": _ceiling,
        "height": _fmt_ht(rd.get("height")),
        "bats": rd.get("bats", ""), "throws": rd.get("throws", ""),
    }
    # Divergence detection
    if _tool_only is not None and rd.get("ovr") is not None:
        try:
            from statsplusplus.data.evaluation_engine import detect_divergence
            out["divergence"] = detect_divergence(_tool_only, rd.get("ovr"))
        except Exception:
            pass
    _build_tools(rd, is_pitcher, out)

    # Current season stats
    year = get_cfg().year
    if is_pitcher:
        st = conn.execute("""
            SELECT SUM(ip), SUM(er)*27.0/NULLIF(SUM(outs),0), SUM(k), SUM(bb),
                   SUM(war), SUM(sv), SUM(hld)
            FROM mlb_pitching_stats WHERE player_id=? AND year=? AND split_id=1
        """, (pid, year)).fetchone()
        if st and st[0]:
            out["stats"] = {"ip": round(st[0], 1), "era": round(st[1], 2) if st[1] else None,
                            "k": st[2], "bb": st[3], "war": round(st[4], 1)}
    else:
        st = conn.execute("""
            SELECT SUM(ab), SUM(h)*1.0/NULLIF(SUM(ab),0),
                   (SUM(h)+SUM(bb)+SUM(hbp))*1.0/NULLIF(SUM(ab)+SUM(bb)+SUM(hbp)+SUM(sf),0),
                   (SUM(h)-SUM(d)-SUM(t)-SUM(hr)+2*SUM(d)+3*SUM(t)+4*SUM(hr))*1.0/NULLIF(SUM(ab),0),
                   SUM(hr), SUM(sb), SUM(war)
            FROM mlb_batting_stats WHERE player_id=? AND year=? AND split_id=1
        """, (pid, year)).fetchone()
        if st and st[0]:
            out["stats"] = {"avg": round(st[1], 3) if st[1] else None,
                            "obp": round(st[2], 3) if st[2] else None,
                            "slg": round(st[3], 3) if st[3] else None,
                            "hr": st[4], "sb": st[5], "war": round(st[6], 1)}

    return out


def _fmt_ht(cm):
    if not cm: return ""
    ft = int(cm / 30.48)
    inch = round((cm % 30.48) / 2.54)
    return f"{ft}'{inch}\""


def get_prospect_comps(pid):
    """Find MLB player comps for a prospect at 3 outcome tiers (upside/likely/floor).

    Matches prospect's scaled potential ratings against MLB players' current ratings
    in the same bucket, weighted 70% tool shape + 30% WAR proximity.
    Returns list of {tier, pct, comp_pid, comp_name, comp_team, comp_ovr, comp_war, comp_age}.
    """
    from math import sqrt
    from statsplusplus.evaluation.war import peak_war_from_score as peak_war_from_ovr

    conn = get_db()
    _abbr = team_abbr_map()

    _DEF_COL = {"C": "r.c", "1B": "r.first_b", "2B": "r.second_b", "3B": "r.third_b",
                "SS": "r.ss", "LF": "r.lf", "CF": "r.cf", "RF": "r.rf", "COF": "r.cf"}

    # Get prospect info
    ed = _get_prospect_eval_date()
    bucket_row = conn.execute(
        "SELECT bucket FROM prospect_fv WHERE eval_date=? AND player_id=?", (ed, pid)).fetchone()
    if not bucket_row:
        return None
    def_col = _DEF_COL.get(bucket_row[0], "NULL")
    pf = conn.execute(f"""
        SELECT pf.fv, pf.bucket, pf.level, p.age,
               r.cntct, r.gap, r.pow, r.eye, r.ks, r.speed,
               r.pot_cntct, r.pot_gap, r.pot_pow, r.pot_eye, r.pot_ks,
               r.stf, r.mov, r.ctrl, r.pot_stf, r.pot_mov, r.pot_ctrl,
               r.ovr, r.pot, p.role, {def_col}
        FROM prospect_fv pf
        JOIN players p ON pf.player_id = p.player_id
        JOIN latest_ratings r ON pf.player_id = r.player_id
        WHERE pf.eval_date = ? AND pf.player_id = ?
    """, (ed, pid)).fetchone()
    if not pf:
        return None

    fv, bucket, level, age = pf[0], pf[1], pf[2], pf[3]
    is_pit = bucket in ("SP", "RP")

    if is_pit:
        cur_tools = [pf[15] or 20, pf[16] or 20, pf[17] or 20]  # stf, mov, ctrl
        pot_tools = [pf[18] or 20, pf[19] or 20, pf[20] or 20]
    else:
        def_val = pf[24] or 20
        cur_tools = [pf[4] or 20, pf[5] or 20, pf[6] or 20, pf[7] or 20, pf[8] or 20, pf[9] or 20, def_val]
        pot_tools = [pf[10] or 20, pf[11] or 20, pf[12] or 20, pf[13] or 20, pf[14] or 20, pf[9] or 20, def_val]

    # Build target tool vectors for 3 tiers (blend cur→pot)
    # Upside: 100% pot, Likely: 70% pot, Floor: 40% pot
    tiers = []
    for label, blend in [("Upside", 1.1), ("Likely", 0.7), ("Floor", 0.4)]:
        target = [c + blend * (p - c) for c, p in zip(cur_tools, pot_tools)]
        tiers.append((label, target))

    # Get outcome probabilities for the tier WAR targets
    import prospect_value as _pv
    outcome = _pv.career_outcome_probs(fv, age, level, bucket,
                                        ovr=pf[21], pot=pf[22])
    # Extract p75/p50/p25 WAR from the tier distribution
    total_area = sum(t["prob"] for t in outcome["tiers"])
    cum = 0
    p25_war, p50_war, p75_war = 0.125, 0.125, 0.125
    p25_pct, p50_pct, p75_pct = 0, 0, 0
    for t in outcome["tiers"]:
        cum += t["prob"]
        if cum < total_area * 0.25:
            p25_war = t["war"]
            p25_pct = t["prob"]
        if cum < total_area * 0.50:
            p50_war = t["war"]
            p50_pct = t["prob"]
        if cum < total_area * 0.75:
            p75_war = t["war"]
            p75_pct = t["prob"]
    tier_wars = [p75_war, p50_war, p25_war]

    # Get all MLB players in matching bucket(s)
    of_buckets = ("CF", "COF", "LF", "RF")
    if bucket in of_buckets:
        bucket_clause = f"ps.bucket IN ({','.join('?' * len(of_buckets))})"
        bucket_params = list(of_buckets)
    else:
        bucket_clause = "ps.bucket = ?"
        bucket_params = [bucket]

    if is_pit:
        tool_sql = "r.stf, r.mov, r.ctrl"
    else:
        mlb_def_col = _DEF_COL.get(bucket, "NULL")
        tool_sql = f"r.cntct, r.gap, r.pow, r.eye, r.ks, r.speed, {mlb_def_col}"

    mlb_rows = conn.execute(f"""
        SELECT ps.player_id, p.name, ps.ovr, p.age,
               COALESCE(NULLIF(p.organization_id,0), NULLIF(p.parent_team_id,0), p.team_id) AS org_id,
               {tool_sql}
        FROM player_surplus ps
        JOIN players p ON ps.player_id = p.player_id
        JOIN latest_ratings r ON ps.player_id = r.player_id
        WHERE {bucket_clause}
          AND (p.age >= 26 OR (r.pot - r.ovr) <= 5)
          AND ps.player_id != ?
          AND ps.player_id NOT IN (SELECT player_id FROM prospect_fv)
    """, bucket_params + [pid]).fetchall()

    if not mlb_rows:
        return None

    # Build MLB player list with tool vectors and WAR
    n_tools = 3 if is_pit else 7
    mlb_players = []
    for row in mlb_rows:
        tools = [row[5 + i] or 20 for i in range(n_tools)]
        war = peak_war_from_ovr(row[2], bucket)
        mlb_players.append({
            "pid": row[0], "name": row[1], "ovr": row[2], "age": row[3],
            "team": _abbr.get(row[4], "FA"), "tools": tools, "war": round(war, 2),
        })

    # For each tier, score all MLB players and pick best
    def tool_dist(a, b):
        return sqrt(sum((x - y) ** 2 for x, y in zip(a, b)))

    # Normalize distances for scoring
    max_tool_dist = sqrt(n_tools * 60 ** 2)  # max possible (all 20 vs all 80)

    comps = []
    used_pids = set()
    for (label, target), target_war in zip(tiers, tier_wars):
        best, best_score = None, float("inf")
        for mp in mlb_players:
            if mp["pid"] in used_pids:
                continue
            td = tool_dist(target, mp["tools"]) / max_tool_dist
            wd = abs(mp["war"] - target_war) / max(target_war, 0.5)
            score = 0.7 * td + 0.3 * min(1.0, wd)
            if score < best_score:
                best_score = score
                best = mp
        if best:
            used_pids.add(best["pid"])
            comps.append({
                "tier": label,
                "pid": best["pid"], "name": best["name"],
                "team": best["team"], "ovr": best["ovr"],
                "age": best["age"], "war": best["war"],
            })

    # Attach outcome probability: P(WAR >= comp's WAR) from the tier distribution
    for comp, target_war in zip(comps, tier_wars):
        # Find the tier closest to the comp's projected WAR
        prob = 0
        for t in outcome["tiers"]:
            if t["war"] <= comp["war"] + 0.063:  # half a tier step
                prob = t["prob"]
            else:
                break
        comp["pct"] = round(prob * 100)

    # Attach current-year stats to each comp
    yr = get_cfg().year
    for comp in comps:
        cpid = comp["pid"]
        if is_pit:
            st = conn.execute("""
                SELECT SUM(ip), SUM(er)*27.0/NULLIF(SUM(outs),0), SUM(k), SUM(war)
                FROM mlb_pitching_stats WHERE player_id=? AND year=? AND split_id=1
            """, (cpid, yr)).fetchone()
            if st and st[0]:
                comp["line"] = f"{round(st[1],2) if st[1] else 0:.2f} ERA · {round(st[0],1)} IP · {round(st[3],1)} WAR"
        else:
            st = conn.execute("""
                SELECT SUM(h)*1.0/NULLIF(SUM(ab),0),
                       (SUM(h)+SUM(bb)+SUM(hbp))*1.0/NULLIF(SUM(ab)+SUM(bb)+SUM(hbp)+SUM(sf),0),
                       (SUM(h)-SUM(d)-SUM(t)-SUM(hr)+2*SUM(d)+3*SUM(t)+4*SUM(hr))*1.0/NULLIF(SUM(ab),0),
                       SUM(hr), SUM(war)
                FROM mlb_batting_stats WHERE player_id=? AND year=? AND split_id=1
            """, (cpid, yr)).fetchone()
            if st and st[0]:
                avg = f"{st[0]:.3f}"[1:]
                obp = f"{st[1]:.3f}"[1:]
                slg = f"{st[2]:.3f}"[1:]
                comp["line"] = f"{avg}/{obp}/{slg} · {st[3]} HR · {round(st[4],1)} WAR"

    return comps


def get_prospect_comp_stats(pid):
    """Get aggregate WAR statistics for MLB players matching a prospect's profile.

    Uses tool-profile matching (no WAR bias) to find all similar MLB seasons,
    then returns summary statistics. Complements get_prospect_comps() which
    picks 3 named comps.

    Returns dict with {n, mean, median, p25, p75, min, max, implied_fv} or None.
    """
    from comp_validate import find_comps, summarize
    from statsplusplus.config.ratings import norm_continuous as _norm

    conn = get_db()
    ed = _get_prospect_eval_date()
    row = conn.execute("""
        SELECT r.pot_cntct, r.pot_pow, r.pot_eye, r.pot_gap,
               r.pot_stf, r.pot_mov, r.pot_ctrl,
               pf.bucket
        FROM prospect_fv pf
        JOIN latest_ratings r ON pf.player_id = r.player_id
        WHERE pf.eval_date = ? AND pf.player_id = ?
    """, (ed, pid)).fetchone()

    if not row:
        return None

    bucket = row["bucket"]
    is_pitcher = bucket in ("SP", "RP")

    if is_pitcher:
        tools = {
            "stuff": _norm(row["pot_stf"]),
            "movement": _norm(row["pot_mov"]),
            "control": _norm(row["pot_ctrl"]),
        }
    else:
        tools = {
            "contact": _norm(row["pot_cntct"]),
            "power": _norm(row["pot_pow"]),
            "eye": _norm(row["pot_eye"]),
            "gap": _norm(row["pot_gap"]),
        }
    tools = {k: v for k, v in tools.items() if v is not None}
    if not tools:
        return None

    min_pa = 50 if is_pitcher else 200
    comps = find_comps(conn, tools, bucket, tolerance=10, min_pa=min_pa)
    stats = summarize(comps)
    if not stats:
        return None

    # Implied FV from median
    med = stats["median"]
    if med >= 4.0:
        stats["implied_fv"] = "60+"
    elif med >= 3.0:
        stats["implied_fv"] = "55-60"
    elif med >= 2.0:
        stats["implied_fv"] = "50-55"
    elif med >= 1.0:
        stats["implied_fv"] = "45-50"
    else:
        stats["implied_fv"] = "40-45"

    return stats


# ── re-exports from extracted modules ───────────────────────────────────

from team_queries import (get_summary, get_standings, get_division_standings,
                          get_roster, get_farm, get_intl_complex, get_team_stats, get_contracts,
                          get_roster_summary, get_upcoming_fa, get_surplus_leaders,
                          get_age_distribution, get_farm_depth, get_stat_leaders,
                          get_power_rankings, get_recent_games, get_payroll_summary,
                          get_record_breakdown, get_depth_chart,
                          get_depth_chart_roles, set_depth_chart_role,
                          get_pitcher_depth_chart_roles,
                          list_retained_salaries, set_retained_salary,
                          get_roster_hitters, get_roster_pitchers,
                          get_org_overview, get_draft_org_depth,
                          get_minor_league_team, get_minor_league_roster,
                          get_minor_league_notables, get_affiliates,
                          get_org_minor_league_roster,
                          get_head_to_head_matrix, get_cut_candidates,
                          get_waiver_candidates, get_free_agent_candidates,
                          get_defense_page, get_farm_system_rankings, _gr_tier,
                          _personality_type_info, _draft_bonus_verdict)
from player_queries import get_player
from percentiles import get_hitter_percentiles, get_pitcher_percentiles


# ── Draft Pool ──────────────────────────────────────────────────────────

def _detect_amateur_levels(conn):
    """Detect which DB levels represent the amateur draft pool."""
    levels = []
    for lvl in ('10', '11', '0'):
        cnt = conn.execute(
            "SELECT COUNT(*) FROM players WHERE level=? AND age BETWEEN 17 AND 23", (lvl,)
        ).fetchone()[0]
        if cnt >= 50:
            levels.append(lvl)
    return levels


# An uploaded pool is considered "stale" (a prior draft's) when fewer than this
# fraction of its players are still on the amateur (draft-eligible) levels — the
# rest have been drafted and moved into org systems.
_POOL_FRESH_MIN_AMATEUR_FRAC = 0.5


def _pool_is_stale(conn, pool_ids, amateur_levels):
    """True if the uploaded pool looks like a *prior* draft's pool — i.e. most of
    its players are no longer draft-eligible (they've been drafted off the
    amateur levels). Uses the league's detected amateur levels; falls back to the
    standard set when detection is empty. Empty pool → not stale (nothing to
    judge). Conservative: only flags stale when clearly so.

    Sample the first 300 IDs (draft_pool.json is rank-ordered — see
    _pool_is_stale in scripts/draft_board.py for the reasoning on why sampling
    the front of the list beats scanning the whole pool).
    Ported from upstream tfalsone/statsplusplus 59c5c5c.
    """
    if not pool_ids:
        return False
    levels = set(amateur_levels) or {'0', '10', '11'}
    # Sample to bound the query for very large pools.
    sample = pool_ids if len(pool_ids) <= 300 else pool_ids[:300]
    ph = ",".join("?" * len(sample))
    rows = conn.execute(
        f"SELECT level FROM players WHERE player_id IN ({ph})", sample).fetchall()
    if not rows:
        # None of the pool IDs resolve in this DB — treat as stale/foreign.
        return True
    amateur = sum(1 for r in rows if str(r[0]) in levels)
    return (amateur / len(rows)) < _POOL_FRESH_MIN_AMATEUR_FRAC


def _annotate_adp(results):
    """Add expected draft position (ADP) data to each prospect entry.

    Ranks by POT descending (how other GMs draft), compares to our FV rank,
    and labels value gaps.
    """
    if not results:
        return
    try:
        num_teams = len(get_cfg().mlb_team_ids)
    except Exception:
        num_teams = 30

    # POT rank (other GMs' likely order): POT desc, age asc for ties
    # When POT is unavailable (league doesn't surface it), fall back to ceiling.
    has_pot = any(results[i].get("pot") for i in range(min(20, len(results))))
    if has_pot:
        pot_sorted = sorted(range(len(results)),
                            key=lambda i: (-(results[i].get("pot") or 0), results[i].get("age") or 99))
    else:
        pot_sorted = sorted(range(len(results)),
                            key=lambda i: (-(results[i].get("true_ceiling") or results[i].get("ceiling_score") or results[i].get("pot") or 0), results[i].get("age") or 99))
    pot_rank = {}
    for rank, idx in enumerate(pot_sorted, 1):
        pot_rank[idx] = rank

    for i, entry in enumerate(results):
        fv_rank = i + 1  # results are already sorted by our value
        pr = pot_rank[i]
        exp_round = (pr - 1) // num_teams + 1
        gap = pr - fv_rank  # positive = others undervalue (will fall)

        if gap >= num_teams:
            label = "Sleeper"
        elif gap >= num_teams // 2:
            label = "Value"
        elif gap <= -num_teams:
            label = "Reach"
        elif gap <= -(num_teams // 2):
            label = "Goes Early"
        else:
            label = ""

        entry["adp"] = {
            "pot_rank": pr,
            "exp_round": exp_round,
            "gap": gap,
            "label": label,
        }


# Signing-bonus slot values ($) by overall pick number — real anchor points
# pulled from PPL's own draft-order screen (full round 1 + round 2, plus
# Philadelphia's pick-9 slot value at rounds 5/10/15/20/25). A "Slot" bonus
# ask means the player signs for whatever their actual pick's assigned
# bonus is — NOT $0 — so this estimates that real cost for the Bonus Value
# column instead of treating Slot as a free pass. See _apply_slot_value_estimates.
_SLOT_VALUE_ANCHORS = [
    # Round 1, overall picks 1-16
    (1, 450000), (2, 380860), (3, 352770), (4, 329010), (5, 309560),
    (6, 294440), (7, 283640), (8, 277160), (9, 275000), (10, 250000),
    (11, 225000), (12, 200000), (13, 175000), (14, 150000), (15, 125000), (16, 100000),
    # Round 2, overall picks 17-32
    (17, 75000), (18, 67590), (19, 64580), (20, 62030), (21, 59950),
    (22, 58330), (23, 57170), (24, 56480), (25, 56250), (26, 53570),
    (27, 50890), (28, 48210), (29, 45530), (30, 42850), (31, 40170), (32, 37500),
    # Philadelphia's own pick (slot 9) at later-round milestones
    (73, 16875),   # round 5, pick 9
    (153, 7915),   # round 10, pick 9
    (233, 5175),   # round 15, pick 9
    (313, 3845),   # round 20, pick 9
    (393, 3060),   # round 25, pick 9
]


def _estimate_slot_value(overall_pick):
    """Log-log interpolates a signing-bonus slot value for an overall pick
    number between the known anchors above. Clamped at both ends of the
    known range rather than extrapolated, since we have no data past
    round 25 or before pick 1."""
    if not overall_pick or overall_pick < 1:
        return None
    anchors = _SLOT_VALUE_ANCHORS
    if overall_pick <= anchors[0][0]:
        return float(anchors[0][1])
    if overall_pick >= anchors[-1][0]:
        return float(anchors[-1][1])
    for (p_lo, v_lo), (p_hi, v_hi) in zip(anchors, anchors[1:]):
        if p_lo <= overall_pick <= p_hi:
            if p_hi == p_lo:
                return float(v_lo)
            t = (math.log(overall_pick) - math.log(p_lo)) / (math.log(p_hi) - math.log(p_lo))
            return math.exp(math.log(v_lo) + t * (math.log(v_hi) - math.log(v_lo)))
    return float(anchors[-1][1])


def _apply_slot_value_estimates(results, pid_to_overall=None):
    """Replaces the fake $0 "Slot" bonus ask with an estimated real dollar
    cost, recomputing the Bonus Value verdict against it. Uses the player's
    actual overall pick if they've already been drafted (pid_to_overall),
    otherwise their pre-draft ADP (adp.pot_rank, set by _annotate_adp —
    must run before this) as an expected-slot proxy. Leaves non-"Slot"
    asks (numeric demands, "Impos.", no ask on file) untouched.
    """
    pid_to_overall = pid_to_overall or {}
    for entry in results:
        if (entry.get("bonus_ask_raw") or "").strip().lower() != "slot":
            continue
        overall = pid_to_overall.get(entry["pid"])
        if overall is None:
            overall = (entry.get("adp") or {}).get("pot_rank")
        est_dollars = _estimate_slot_value(overall)
        if est_dollars is None:
            continue
        entry["bonus_ask"] = round(est_dollars / 1e6, 4)
        entry["bonus_ask_slot_estimated"] = True
        verdict = _draft_bonus_verdict(entry.get("surplus"), est_dollars, None)
        entry["bonus_score"] = verdict["score"]
        entry["bonus_verdict"] = verdict["label"]
        entry["bonus_verdict_class"] = verdict["class"]


_DRAFT_TREE_ROUND_BUCKETS = [
    (1, 1, "Rd 1"), (2, 2, "Rd 2"), (3, 3, "Rd 3"), (4, 4, "Rd 4"), (5, 5, "Rd 5"),
    (6, 10, "Rd 6-10"), (11, 15, "Rd 11-15"), (16, 20, "Rd 16-20"),
    (21, 30, "Rd 21-30"), (31, 999, "Rd 31+"),
]

_SITE_DRAFT_DATA_CACHE: dict = {}


def _load_site_draft_json(name):
    """Loads a manually-refreshed snapshot from StatsPlus's own website
    (see scripts/fetch_site_draft_value.py) — not something this app can
    fetch live, since it needs the site's session cookie and isn't exposed
    by the sanctioned CSV/API client. Returns [] if never fetched."""
    if name in _SITE_DRAFT_DATA_CACHE:
        return _SITE_DRAFT_DATA_CACHE[name]
    try:
        path = get_cfg().league_dir / "config" / name
        data = json.loads(path.read_text()) if path.exists() else []
    except Exception:
        data = []
    _SITE_DRAFT_DATA_CACHE[name] = data
    return data


def get_site_draft_skill():
    """Per-team historical draft performance vs. StatsPlus's own Expected
    WAR curve (site_draft_skill.json — see fetch_site_draft_value.py).
    Empty list if the snapshot has never been pulled."""
    return _load_site_draft_json("site_draft_skill.json")


def _site_expected_war_for_round_bucket(lo_round, hi_round, num_teams=16):
    """Averages the site's own per-pick Expected WAR (its exponential-curve
    fit across all 799 picks in the same 1949-1954 window this app's own
    Draft Tree uses) over the overall picks that fall in a round range,
    assuming num_teams picks/round. NOT a more "mature" baseline — it's the
    same immature sample, just curve-smoothed at pick-level granularity
    instead of this app's raw per-round average, so small per-bucket sample
    noise (a few lucky/unlucky picks skewing a whole round) washes out.
    Treat a big gap between avg_war and this as "this bucket's raw average
    is noisy," not "this bucket is underperforming its true talent level."
    """
    site_rows = _load_site_draft_json("site_draft_value.json")
    if not site_rows:
        return None
    lo_pick = (lo_round - 1) * num_teams + 1
    hi_pick = hi_round * num_teams if hi_round < 999 else 10**6
    picks_in_range = [r for r in site_rows if lo_pick <= r["pick"] <= hi_pick and r.get("expected_war") is not None]
    if not picks_in_range:
        return None
    return sum(r["expected_war"] for r in picks_in_range) / len(picks_in_range)


def get_draft_tree(min_year=None, max_year=None):
    """This league's own historical draft outcomes: career MLB WAR accrued
    so far by draft round, pooled across completed PPL drafts.

    Real front offices call this a "draft tree" (e.g. the public Fangraphs/
    Baseball-Reference round-by-round WAR charts) — here it's built from this
    league's actual draft_year/draft_round + realized batting_stats/
    pitching_stats.war, not an external league's history, so it directly
    answers "what actually happens to a pick in this league" rather than
    leaning on real-MLB draft lore that may not transfer to PPL's own talent
    pool / 125% Talent Change Randomness settings.

    Excludes draft_year 0 (undated/legacy imports) and lets the caller bound
    the year range — very recent classes haven't had time to accrue career
    value yet and will understate their eventual hit rate.
    """
    conn = get_db()
    where = "p.draft_year IS NOT NULL AND p.draft_year > 0 AND p.draft_round IS NOT NULL"
    params = []
    if min_year is not None:
        where += " AND p.draft_year >= ?"
        params.append(min_year)
    if max_year is not None:
        where += " AND p.draft_year <= ?"
        params.append(max_year)

    rows = conn.execute(f"""
        SELECT p.draft_year, p.draft_round, p.player_id,
               COALESCE(b.war, 0) + COALESCE(pi.war, 0) AS career_war
        FROM players p
        LEFT JOIN (SELECT player_id, SUM(war) AS war FROM batting_stats
                   WHERE split_id = 1 GROUP BY player_id) b ON b.player_id = p.player_id
        LEFT JOIN (SELECT player_id, SUM(war) AS war FROM pitching_stats
                   WHERE split_id = 1 GROUP BY player_id) pi ON pi.player_id = p.player_id
        WHERE {where}
    """, params).fetchall()

    def bucket_for(rd):
        for lo, hi, label in _DRAFT_TREE_ROUND_BUCKETS:
            if lo <= rd <= hi:
                return label
        return None

    buckets = {}
    years_seen = set()
    for r in rows:
        yr, rd, war = r["draft_year"], r["draft_round"], r["career_war"] or 0.0
        years_seen.add(yr)
        label = bucket_for(rd)
        if label is None:
            continue
        b = buckets.setdefault(label, {"n": 0, "total_war": 0.0, "contributor": 0, "regular": 0, "star": 0})
        b["n"] += 1
        b["total_war"] += war
        if war >= 2.0:
            b["contributor"] += 1
        if war >= 8.0:
            b["regular"] += 1
        if war >= 20.0:
            b["star"] += 1

    result = []
    for lo, hi, label in _DRAFT_TREE_ROUND_BUCKETS:
        b = buckets.get(label)
        if not b or not b["n"]:
            continue
        curve_expected = _site_expected_war_for_round_bucket(lo, hi)
        result.append({
            "round": label,
            "picks": b["n"],
            "avg_war": round(b["total_war"] / b["n"], 2),
            "pct_contributor": round(100 * b["contributor"] / b["n"]),
            "pct_regular": round(100 * b["regular"] / b["n"]),
            "pct_star": round(100 * b["star"] / b["n"]),
            "site_curve_expected_war": round(curve_expected, 2) if curve_expected is not None else None,
        })
    return {"rounds": result, "years": sorted(years_seen),
            "has_site_baseline": bool(_load_site_draft_json("site_draft_value.json"))}


def get_draft_pool():
    """Return draft board: either from API picks (if draft is active/complete)
    or top 800 amateurs by Pot (pre-draft scouting approximation).

    Returns dict with 'state', 'players', and optionally 'picks'.
    """
    conn = get_db()
    amateur_levels = _detect_amateur_levels(conn)

    # Draft year — used by the web UI to namespace per-draft localStorage
    # pick storage so a newly uploaded pool never inherits a prior draft's
    # picks (ported from upstream tfalsone/statsplusplus f42bc12, "Draft
    # board: fix phantom picks carried across drafts", 2026-09-18).
    draft_year = None
    try:
        import json as _json_dy
        _state_path = os.path.join(str(get_cfg().league_dir), "config", "state.json")
        if os.path.exists(_state_path):
            with open(_state_path) as _f_dy:
                draft_year = _json_dy.load(_f_dy).get("year")
    except Exception:
        draft_year = None

    from statsplusplus.data.fv_calc import RATINGS_SQL
    from statsplusplus.utils.positions import assign_bucket, LEVEL_NORM_AGE; from statsplusplus.evaluation.fv import calc_fv_from_dict as calc_fv; from statsplusplus.config.ratings import norm
    from statsplusplus.evaluation.composite import compute_batting_composite
    from statsplusplus.data.evaluation_engine import load_tool_weights, DEFAULT_TOOL_WEIGHTS

    # Extend RATINGS_SQL with bats/throws which aren't in the base query
    _DRAFT_SQL = RATINGS_SQL.replace("r.league_id AS LeagueId",
        "r.league_id AS LeagueId, r.bats AS Bats, r.throws AS Throws")

    n = _norm
    _hitter_weights_by_bucket = load_tool_weights(get_cfg().league_dir).get(
        "hitter", DEFAULT_TOOL_WEIGHTS["hitter"])
    role_map = {str(k): v for k, v in get_cfg().role_map.items()}
    _LVL_KEY = {'11': 'intl', '10': 'a', '0': 'dsl'}
    _POS_LABEL = {1:'P',2:'C',3:'1B',4:'2B',5:'3B',6:'SS',7:'LF',8:'CF',9:'RF',10:'DH'}

    # --- Custom-Upload-parity helpers: tool weights / park factors, loaded
    # once per call (not per player) — mirrors evaluate_row()'s per-league
    # loading in scripts/custom_upload.py.
    from statsplusplus.evaluation.composite import (
        compute_composite_hitter, compute_composite_pitcher,
        compute_specialist_score, specialist_label,
    )
    from statsplusplus.evaluation.constants import DEFENSIVE_WEIGHTS
    from statsplusplus.evaluation.park_fit import (
        load_park_factors, compute_batter_park_fit, compute_pitcher_park_fit_from_tools,
    )
    from statsplusplus.data.evaluation_engine import DEFAULT_TOOL_WEIGHTS, load_tool_weights
    from statsplusplus.utils.positions import PITCH_FIELDS

    # NOTE: must use get_cfg().league_dir (request/session-scoped), NOT
    # statsplusplus.config.league_context.get_league_dir() — that falls back
    # to the *global* app_config.json "active_league" (or env var) whenever
    # no slug is passed, which any other browser tab/session can overwrite
    # via /switch-league. Using it here caused prospect surplus/composite/
    # park-fit calcs to silently use another session's league (wrong $/WAR,
    # wrong ratings scale, wrong calibrated tables) whenever two leagues were
    # active in different tabs at once.
    try:
        _league_dir = get_cfg().league_dir
    except Exception:
        _league_dir = None
    _tool_weights = DEFAULT_TOOL_WEIGHTS
    _park = None
    if _league_dir is not None:
        try:
            _tool_weights = load_tool_weights(_league_dir)
        except Exception:
            pass
        try:
            _park = load_park_factors(_league_dir)
        except Exception:
            pass
    _game_year = None
    try:
        _game_year = year()
    except Exception:
        pass

    # Mirrors custom_upload.py's _TRAIT_NOTE_MAP / _trait_notes() — adaptability
    # deliberately excluded (out of scope for the draft page per spec).
    _TRAIT_NOTE_MAP = {
        "we":    {"H": ("buff", "Hard Worker"), "L": ("concern", "Low Work Ethic")},
        "int":   {"H": ("buff", "High IQ"), "L": ("concern", "Low IQ")},
        "lead":  {"H": ("buff", "Leader"), "L": ("concern", "Low Leadership")},
        "loy":   {"H": ("buff", "Loyal"), "L": ("concern", "Low Loyalty")},
        "greed": {"H": ("concern", "Greedy"), "L": ("buff", "Not Greedy")},
    }

    def _trait_notes(we, intel, lead, loy, greed):
        buffs, concerns = [], []
        for field, value in (("we", we), ("int", intel), ("lead", lead),
                             ("loy", loy), ("greed", greed)):
            note = _TRAIT_NOTE_MAP.get(field, {}).get(value)
            if not note:
                continue
            kind, label = note
            (buffs if kind == "buff" else concerns).append(label)
        return buffs, concerns

    # Personality Type column: real OOTP Type + Adaptability aren't in
    # RATINGS_SQL (they only exist via a manual Custom Upload, stored in
    # personality_overrides — see db.py) so fetch the whole table once here
    # rather than per player. Reuses team_queries._personality_type_info(),
    # the same trait-combo-inference logic (with real precision numbers)
    # already driving this everywhere else in the app.
    _personality_overrides_map = {}
    try:
        for _r in conn.execute("SELECT player_id, personality_type, adaptability FROM personality_overrides"):
            _personality_overrides_map[_r["player_id"]] = (_r["personality_type"], _r["adaptability"])
    except Exception:
        pass

    # Bonus Ask / Bonus Value columns: signing-bonus demand for each
    # draft-eligible amateur, from the draft pool export's DEM column (see
    # custom_upload.import_draft_bonus_asks). Fetched once here, same
    # pattern as personality_overrides above.
    _bonus_ask_map = {}
    try:
        for _r in conn.execute("SELECT player_id, ask_raw, ask_dollars FROM draft_bonus_asks"):
            _bonus_ask_map[_r["player_id"]] = (_r["ask_raw"], _r["ask_dollars"])
    except Exception:
        pass

    # OSA Draft Pipeline Rank: a manually-entered top-200 scouting list (no
    # CSV export of this exists — see osa_top200.json's own comment),
    # matched to this year's draft pool by exact player name. Not every
    # name on that list is draft-eligible this year, so this only ever
    # ADDS a rank to players already in the pool — it never pulls in
    # players who aren't otherwise part of it.
    _osa_rank_by_name = {}
    try:
        import json as _json_osa
        _osa_path = os.path.join(str(_league_dir), "config", "osa_top200.json") if _league_dir else None
        if _osa_path and os.path.exists(_osa_path):
            with open(_osa_path) as _f_osa:
                _osa_names = _json_osa.load(_f_osa).get("ranks", [])
            _osa_rank_by_name = {name: i + 1 for i, name in enumerate(_osa_names)}
    except Exception:
        pass

    # Mirrors custom_upload.py's _BEST_POSITION_PRIORITY / _best_position(),
    # applied to the draft entry's already-normalized (20-80) potential
    # defensive ratings instead of raw CSV columns.
    _BEST_POSITION_PRIORITY = [
        ("SS", ("SS",), 60),
        ("CF", ("CF",), 60),
        ("C", ("C",), 55),
        ("2B", ("2B",), 55),
        ("3B", ("3B",), 55),
        ("LF/RF", ("LF", "RF"), 55),
        ("1B", ("1B",), 55),
    ]

    def _best_position(defense):
        for label, keys, threshold in _BEST_POSITION_PRIORITY:
            grade = max((defense.get(k) or 0) for k in keys)
            if grade >= threshold:
                return label, grade
        return None, None

    # Mirrors custom_upload.py's _defense_for_bucket() — current (not
    # potential) defensive component ratings + weights for compute_composite_
    # hitter()'s defense arg, built from RATINGS_SQL's raw column aliases.
    def _current_defense_for_bucket(p, bucket, ng):
        if bucket == "C":
            defense = {"CFrm": ng(p.get("CFrm") or 0), "CBlk": ng(p.get("CBlk") or 0),
                       "CArm": ng(p.get("CArm") or 0)}
            return defense, DEFENSIVE_WEIGHTS.get("C", {})
        if bucket in ("SS", "2B", "3B"):
            defense = {"IFR": ng(p.get("IFR") or 0), "IFE": ng(p.get("IFE") or 0),
                       "IFA": ng(p.get("IFA") or 0), "TDP": ng(p.get("TDP") or 0)}
            return defense, DEFENSIVE_WEIGHTS.get(bucket, {})
        if bucket in ("CF", "COF"):
            defense = {"OFR": ng(p.get("OFR") or 0), "OFE": ng(p.get("OFE") or 0),
                       "OFA": ng(p.get("OFA") or 0)}
            if bucket == "CF":
                return defense, DEFENSIVE_WEIGHTS.get("CF", {})
            lf_w, rf_w = DEFENSIVE_WEIGHTS.get("COF_LF", {}), DEFENSIVE_WEIGHTS.get("COF_RF", {})
            def _score(weights):
                return sum((defense.get(k) or 0) * w for k, w in weights.items())
            return defense, (lf_w if _score(lf_w) >= _score(rf_w) else rf_w)
        return {}, {}

    # Mirrors custom_upload.py's _platoon_split() — same tool pairs/threshold,
    # applied to the draft entry's already-normalized split ratings.
    _PLATOON_GAP_THRESHOLD = 10

    def _platoon_strong_side(p, ng, is_pitcher):
        if is_pitcher:
            pairs = [("Stf_L", "Stf_R")]
            strong_labels = ("vs LHB", "vs RHB")
        else:
            pairs = [("Cntct_L", "Cntct_R"), ("Pow_L", "Pow_R"),
                     ("Gap_L", "Gap_R"), ("Eye_L", "Eye_R")]
            strong_labels = ("vs LHP", "vs RHP")
        max_gap, strong_side = 0, None
        for l_col, r_col in pairs:
            lv, rv = ng(p.get(l_col)), ng(p.get(r_col))
            if not lv or not rv:
                continue
            gap = abs(lv - rv)
            if gap > max_gap:
                max_gap = gap
                strong_side = strong_labels[0] if lv > rv else strong_labels[1]
        if max_gap < _PLATOON_GAP_THRESHOLD:
            return None
        return strong_side

    def _build_prospect(rat):
        p = dict(rat)
        ng = _norm_floor
        role_str = role_map.get(str(p.get("role") or 0), "position_player")
        p["_role"] = role_str
        p["Pos"] = str(p.get("pos") or "")
        p["_is_pitcher"] = (p["Pos"] == "P" or role_str in ("starter", "reliever", "closer"))
        bucket = assign_bucket(p)
        p["_bucket"] = bucket
        level = str(p["level"])
        level_key = _LVL_KEY.get(level, 'dsl')
        # Draft prospects: weight Pot heavily — Ovr reflects pre-pro development,
        # not talent ceiling. HS gets slightly more Pot weight than college.
        p["_norm_age"] = p["Age"] + 4  # force diff >= 3 → base 0.65
        p["_level"] = "a-short"  # +0.10 low_level bonus, no cap → dw = 0.75
        fv_base, fv_plus = calc_fv(p)
        # Use prospect_fv table values when available (canonical grades)
        pf_row = conn.execute(
            "SELECT fv, fv_str, risk, prospect_surplus FROM prospect_fv WHERE player_id=?", (p["ID"],)
        ).fetchone()
        if pf_row:
            fv_base = pf_row[0]
            fv_str_display = pf_row[1]
            pf_risk = pf_row[2]
            pf_surplus = pf_row[3] or 0
        else:
            fv_str_display = f"{fv_base}+" if fv_plus else str(fv_base)
            pf_risk = None
            pf_surplus = 0
        pos_str = ROLE_MAP.get(p.get("role"), _POS_LABEL.get(p.get("pos"), "?"))
        college_hs = "College" if level == '10' else "HS" if level == '11' else ("HS" if (p.get("Age") or 0) <= 18 else "College")
        entry = {
            "pid": p["ID"], "name": p["Name"], "age": p["Age"],
            "pos": pos_str, "bucket": bucket, "type": college_hs,
            "ovr": n(p["Ovr"]) or p.get("composite_score") or 0,
            "pot": n(p["Pot"]) or p.get("true_ceiling") or p.get("ceiling_score") or 0,
            "fv": fv_base, "fv_str": fv_str_display,
            "risk": pf_risk,
            "bats": p.get("Bats", ""), "throws": p.get("Throws", ""),
            "acc": p.get("Acc", ""), "we": p.get("WrkEthic", ""),
            "lead": p.get("Lead", ""), "int": p.get("Int", ""),
            "loy": p.get("Loy", ""), "greed": p.get("Greed", ""),
        }
        _buffs, _concerns = _trait_notes(p.get("WrkEthic"), p.get("Int"), p.get("Lead"),
                                          p.get("Loy"), p.get("Greed"))
        entry["buffs"], entry["concerns"] = _buffs, _concerns
        _po_type, _po_adapt = _personality_overrides_map.get(p["ID"], (None, None))
        _type_info = _personality_type_info(_po_type, p.get("WrkEthic"), p.get("Lead"),
                                             p.get("Loy"), p.get("Greed"), p.get("Int"), _po_adapt)
        entry["personality_type"] = _type_info["label"]
        entry["personality_type_class"] = _type_info["class"]
        if p["_is_pitcher"]:
            # Count viable pitches (pot >= 45)
            from statsplusplus.utils.positions import PITCH_FIELDS
            _pitch_names = {'Fst':'FB','Snk':'SI','Crv':'CB','Sld':'SL','Chg':'CH',
                            'Splt':'SPL','Cutt':'CUT','CirChg':'CC','Scr':'SCR',
                            'Frk':'FRK','Kncrv':'KC','Knbl':'KN'}
            pitch_data = {}
            num_p = 0
            best_p = 20
            for pf in PITCH_FIELDS:
                v = ng(p.get("Pot" + pf) or 0)
                if v and v >= 30:
                    pitch_data[_pitch_names.get(pf, pf)] = v
                    num_p += 1
                    if v > best_p: best_p = v
            entry["tools"] = {
                "stf": [ng(p.get("Stf") or 0), ng(p.get("PotStf") or 0)],
                "mov": [ng(p.get("Mov") or 0), ng(p.get("PotMov") or 0)],
                "ctrl": [ng(p.get("Ctrl") or 0), ng(p.get("PotCtrl") or 0)],
                "stm": ng(p.get("Stm") or 0),
                "vel": p.get("Vel"),
                "num_pitches": num_p,
                "best_pitch": best_p,
                "pitches": pitch_data,
            }
            entry["best_position"], entry["best_position_grade"] = None, None

            # Specialist/Generalist balance score — current (not potential)
            # stuff/movement/control, matching custom_upload.py's evaluate_row().
            _cur_role = "RP" if role_str in ("reliever", "closer") else "SP"
            _pit_tools_cur = {
                "stuff": ng(p.get("Stf") or 0), "movement": ng(p.get("Mov") or 0),
                "control": ng(p.get("Ctrl") or 0),
            }
            entry["specialist_score"] = compute_specialist_score(_pit_tools_cur, True)
            entry["specialist_label"] = specialist_label(entry["specialist_score"])

            # Comp vL/vR + platoon strong side — swap in the vL/vR split
            # ratings for stuff/movement/control, same as
            # custom_upload.py's _pitcher_side_tools().
            try:
                _pit_weights = _tool_weights.get("pitcher", DEFAULT_TOOL_WEIGHTS["pitcher"])[_cur_role]
                _pit_transforms = (_tool_weights.get("tool_transforms", {}) or {}).get(_cur_role)
                _arsenal_cur = {pf: ng(p.get(pf) or 0) for pf in PITCH_FIELDS if p.get(pf)}
                _stamina_cur = ng(p.get("Stm") or 0) or 50
                _tools_l = dict(_pit_tools_cur)
                _v = ng(p.get("Stf_L")); _tools_l["stuff"] = _v if _v is not None else _tools_l["stuff"]
                _v = ng(p.get("Mov_L")); _tools_l["movement"] = _v if _v is not None else _tools_l["movement"]
                _v = ng(p.get("Ctrl_L")); _tools_l["control"] = _v if _v is not None else _tools_l["control"]
                _tools_r = dict(_pit_tools_cur)
                _v = ng(p.get("Stf_R")); _tools_r["stuff"] = _v if _v is not None else _tools_r["stuff"]
                _v = ng(p.get("Mov_R")); _tools_r["movement"] = _v if _v is not None else _tools_r["movement"]
                _v = ng(p.get("Ctrl_R")); _tools_r["control"] = _v if _v is not None else _tools_r["control"]
                entry["composite_vs_l"] = compute_composite_pitcher(
                    _tools_l, _pit_weights, _arsenal_cur, _stamina_cur, _cur_role, _pit_transforms)
                entry["composite_vs_r"] = compute_composite_pitcher(
                    _tools_r, _pit_weights, _arsenal_cur, _stamina_cur, _cur_role, _pit_transforms)
            except Exception:
                entry["composite_vs_l"] = entry["composite_vs_r"] = None
            entry["platoon_strong_side"] = _platoon_strong_side(p, ng, True)

            # Park fit vs the querying team's own park (unsigned prospects
            # have no home park yet — "if we draft him" is the only fit that
            # makes sense), scouting-tool fallback (no game logs pre-draft),
            # matching custom_upload.py's always-tools-based pitcher path.
            entry["park_fit"] = (compute_pitcher_park_fit_from_tools(_pit_tools_cur, _park)
                                  if _park else None)
        else:
            entry["tools"] = {
                "con": [ng(p.get("Cntct") or 0), ng(p.get("PotCntct") or 0)],
                "gap": [ng(p.get("Gap") or 0), ng(p.get("PotGap") or 0)],
                "pow": [ng(p.get("Pow") or 0), ng(p.get("PotPow") or 0)],
                "eye": [ng(p.get("Eye") or 0), ng(p.get("PotEye") or 0)],
                "spd": ng(p.get("Speed") or 0),
            }
            # Simple pure Contact/Gap/Power/Eye weighted average — no
            # defense/speed/transforms/recombination. Separate & simpler
            # than the full compute_composite_hitter pipeline used elsewhere.
            _bw = _hitter_weights_by_bucket.get(bucket, _hitter_weights_by_bucket.get("COF", {}))
            entry["bat_ovr"] = compute_batting_composite(
                ng(p.get("Cntct")), ng(p.get("Gap")), ng(p.get("Pow")), ng(p.get("Eye")), _bw)
            entry["bat_pot"] = compute_batting_composite(
                ng(p.get("PotCntct")), ng(p.get("PotGap")), ng(p.get("PotPow")), ng(p.get("PotEye")), _bw)
            entry["bat_vr"] = compute_batting_composite(
                ng(p.get("Cntct_R")), ng(p.get("Gap_R")), ng(p.get("Pow_R")), ng(p.get("Eye_R")), _bw)
            entry["bat_vl"] = compute_batting_composite(
                ng(p.get("Cntct_L")), ng(p.get("Gap_L")), ng(p.get("Pow_L")), ng(p.get("Eye_L")), _bw)
            _def_fields = [("C","PotC"),("1B","Pot1B"),("2B","Pot2B"),("3B","Pot3B"),
                           ("SS","PotSS"),("LF","PotLF"),("CF","PotCF"),("RF","PotRF")]
            defs = {}
            best_def = 20
            for pos_label, field in _def_fields:
                v = ng(p.get(field) or 0)
                if v and v > 20:
                    defs[pos_label] = v
                    if v > best_def:
                        best_def = v
            entry["tools"]["def"] = best_def
            entry["defense"] = defs
            entry["field"] = {
                "ifr": ng(p.get("IFR") or 0), "ifa": ng(p.get("IFA") or 0),
                "ofr": ng(p.get("OFR") or 0), "ofa": ng(p.get("OFA") or 0),
                "cblk": ng(p.get("CBlk") or 0), "cfrm": ng(p.get("CFrm") or 0),
            }
            # Position mismatch detection: show note when our evaluation bucket
            # differs meaningfully from the player's listed position.
            listed_pos = entry["pos"]
            bucket_display = "LF/RF" if bucket == "COF" else bucket
            # Determine if there's a real mismatch worth showing
            same = (bucket_display == listed_pos or
                    (bucket == "COF" and listed_pos in ("LF", "RF")) or
                    listed_pos in ("P", "DH", "?"))
            if not same:
                entry["pos_note"] = bucket_display

            entry["best_position"], entry["best_position_grade"] = _best_position(defs)

            # Specialist/Generalist balance score — current (not potential)
            # con/gap/pow/eye, matching custom_upload.py's evaluate_row().
            _hit_tools_cur = {
                "contact": ng(p.get("Cntct") or 0), "gap": ng(p.get("Gap") or 0),
                "power": ng(p.get("Pow") or 0), "eye": ng(p.get("Eye") or 0),
                "speed": ng(p.get("Speed") or 0),
            }
            entry["specialist_score"] = compute_specialist_score(_hit_tools_cur, False)
            entry["specialist_label"] = specialist_label(entry["specialist_score"])

            # Comp vL/vR + platoon strong side — swap in the vL/vR split
            # ratings for contact/gap/power/eye, same as
            # custom_upload.py's _hitter_side_tools().
            try:
                _hitter_weights = _tool_weights.get("hitter", DEFAULT_TOOL_WEIGHTS["hitter"])
                _hw = _hitter_weights.get(bucket, _hitter_weights.get("COF", {}))
                _hit_transforms = (_tool_weights.get("tool_transforms", {}) or {}).get("hitter")
                _def_cur, _def_w = _current_defense_for_bucket(p, bucket, ng)
                _tools_l = dict(_hit_tools_cur)
                for key, col in (("contact", "Cntct_L"), ("gap", "Gap_L"),
                                 ("power", "Pow_L"), ("eye", "Eye_L")):
                    _v = ng(p.get(col))
                    if _v is not None:
                        _tools_l[key] = _v
                _tools_r = dict(_hit_tools_cur)
                for key, col in (("contact", "Cntct_R"), ("gap", "Gap_R"),
                                 ("power", "Pow_R"), ("eye", "Eye_R")):
                    _v = ng(p.get(col))
                    if _v is not None:
                        _tools_r[key] = _v
                entry["composite_vs_l"] = compute_composite_hitter(
                    _tools_l, _hw, _def_cur, _def_w, _hit_transforms)
                entry["composite_vs_r"] = compute_composite_hitter(
                    _tools_r, _hw, _def_cur, _def_w, _hit_transforms)
            except Exception:
                entry["composite_vs_l"] = entry["composite_vs_r"] = None
            entry["platoon_strong_side"] = _platoon_strong_side(p, ng, False)

            # Park fit vs the querying team's own park (unsigned prospects
            # have no home park yet — "if we draft him" is the only fit that
            # makes sense), matching custom_upload.py's compute_batter_park_fit call.
            try:
                entry["park_fit"] = (compute_batter_park_fit(
                    _hit_tools_cur, entry["bats"], _hw, _park) if _park else None)
            except Exception:
                entry["park_fit"] = None

        # Career outcome summary for range indicator
        try:
            import prospect_value as _pv
            # Use composite_score as fallback when OVR is unavailable
            _ovr = n(p["Ovr"]) or p.get("composite_score") or 0
            _pot = n(p["Pot"]) or p.get("true_ceiling") or p.get("ceiling_score") or 0
            # Map Ovr to equivalent minor league level for outcome model
            if _ovr >= 45: _oc_level = 'aaa'
            elif _ovr >= 35: _oc_level = 'aa'
            elif _ovr >= 28: _oc_level = 'a'
            else: _oc_level = 'a-short'
            oc = _pv.career_outcome_probs(
                fv_base, p["Age"], _oc_level, bucket,
                ovr=_ovr, pot=_pot)
            if oc:
                entry["outcome"] = {
                    "thresholds": oc.get("thresholds", {}),
                    "likely": oc.get("likely_range", [0, 0]),
                }
            surplus_val = pf_surplus if pf_surplus else _pv.prospect_surplus_with_option(
                fv_base, p["Age"], _oc_level, bucket,
                ovr=_ovr, pot=_pot, league_dir=_league_dir)
            # NOTE: stays in raw millions — feeds the draft board's JS
            # fmtSurplus(), which already does its own per-value adaptive
            # M/K formatting assuming millions-scale input.
            entry["surplus"] = round(surplus_val / 1e6, 3) if surplus_val else 0
            # Raw (ceiling scenario) surplus — what the player is worth if they
            # fully develop to their ceiling with no time/risk discount.
            # Uses ceiling FV to represent the best-case outcome.
            _ceil_fv = _pv._ceiling_fv(_pot) if _pot else fv_base
            _raw_result = _pv.prospect_surplus(_ceil_fv, p["Age"], _oc_level, bucket,
                                               ovr=_ovr, pot=_pot, league_dir=_league_dir)
            if _raw_result and _raw_result.get("breakdown"):
                # dollars_per_war() takes no args — reads the module-global
                # league context that the prospect_surplus() call just above
                # already refreshed via _ensure_league_context(_league_dir).
                _dpw = _pv.dollars_per_war()
                raw_total = sum(b["war"] * _dpw - b["salary"] for b in _raw_result["breakdown"])
                entry["raw_surplus"] = max(entry["surplus"], round(max(0, raw_total) / 1e6, 3))
            else:
                entry["raw_surplus"] = entry["surplus"]

            # Surplus horizons + peak-year surplus — same functions
            # custom_upload.py's evaluate_row() calls, using the same
            # ovr/pot/level/bucket already resolved above for this entry.
            try:
                _peak = _pv.peak_year_surplus(
                    fv_base, p["Age"], _oc_level, bucket, ovr=_ovr, pot=_pot, league_dir=_league_dir)
                entry["peak_surplus"] = round(_peak["surplus"] / 1e6, 3) if _peak["surplus"] else 0
                entry["peak_age"] = _peak["age"]
            except Exception:
                entry["peak_surplus"] = entry["peak_age"] = None
            entry["current_year_surplus"] = entry["next_year_surplus"] = entry["three_year_surplus"] = None
            if _game_year is not None:
                try:
                    _cur, _nxt, _three = _pv.prospect_surplus_horizons(
                        fv_base, p["Age"], _oc_level, bucket, _game_year,
                        ovr=_ovr, pot=_pot, league_dir=_league_dir)
                    entry["current_year_surplus"] = round(_cur / 1e6, 3) if _cur else (0 if _cur == 0 else None)
                    entry["next_year_surplus"] = round(_nxt / 1e6, 3) if _nxt else (0 if _nxt == 0 else None)
                    entry["three_year_surplus"] = round(_three / 1e6, 3) if _three else (0 if _three == 0 else None)
                except Exception:
                    pass
        except Exception:
            entry["surplus"] = 0
            entry.setdefault("peak_surplus", None); entry.setdefault("peak_age", None)
            entry.setdefault("current_year_surplus", None)
            entry.setdefault("next_year_surplus", None)
            entry.setdefault("three_year_surplus", None)

        entry.setdefault("best_position", None)
        entry.setdefault("best_position_grade", None)
        entry.setdefault("specialist_score", None)
        entry.setdefault("specialist_label", None)
        entry.setdefault("composite_vs_l", None)
        entry.setdefault("composite_vs_r", None)
        entry.setdefault("platoon_strong_side", None)
        entry.setdefault("park_fit", None)
        entry.setdefault("buffs", [])
        entry.setdefault("concerns", [])

        _ask_raw, _ask_dollars = _bonus_ask_map.get(p["ID"], (None, None))
        _ask_special = "unsignable" if (_ask_raw or "").strip().lower().startswith("impos") else None
        entry["bonus_ask_raw"] = _ask_raw
        entry["bonus_ask"] = round(_ask_dollars / 1e6, 4) if _ask_dollars else (0 if _ask_dollars == 0 else None)
        _verdict = _draft_bonus_verdict(entry.get("surplus"), _ask_dollars, _ask_special)
        entry["bonus_score"] = _verdict["score"]
        entry["bonus_verdict"] = _verdict["label"]
        entry["bonus_verdict_class"] = _verdict["class"]

        entry["osa_rank"] = _osa_rank_by_name.get(p["Name"])

        return entry

    # Try to load uploaded draft pool first
    uploaded_pids = None
    try:
        from statsplusplus.config.league_context import get_league_dir
        pool_path = get_league_dir() / "config" / "draft_pool.json"
        if pool_path.exists():
            import json as _json
            uploaded_pids = _json.loads(pool_path.read_text()).get("player_ids", [])
    except Exception:
        pass

    if uploaded_pids:
        # Staleness guard: an uploaded draft_pool.json persists on disk, but the
        # players in a *prior* draft's pool get drafted and moved off the amateur
        # levels (level 0/10/11 → org levels). If most of the uploaded pool is no
        # longer draft-eligible, it's a stale pool from a past draft — discard it
        # and fall through to the live/DB-derived pool (per draft-page spec
        # State 3: "new season → previous draft's IDs don't match current pool").
        if _pool_is_stale(conn, uploaded_pids, amateur_levels):
            uploaded_pids = None

    # Try to get draft picks from API to determine state
    picks = []
    try:
        from statsplus import client as _dc
        from statsplusplus.config.league_context import get_statsplus_cookie, get_statsplus_token
        cfg = get_cfg()
        slug = cfg.settings.get("statsplus_slug", "")
        cookie = get_statsplus_cookie()
        token = get_statsplus_token()
        if slug and (cookie or token):
            _dc.configure(slug, cookie, token)
        raw = _dc.get_draft()
        picks = [{"pid": d["ID"], "name": d["Player Name"], "team": d["Team"],
                  "tid": d["Team ID"], "pos": d["Position"], "age": d["Age"],
                  "round": d["Round"], "pick": d["Pick In Round"],
                  "overall": d["Overall"], "college": d["College"],
                  "supp": bool(d.get("Supp")), "auto": bool(d.get("Auto Pick")),
                  "time_utc": d.get("Time (UTC)")}
                 for d in raw if d.get("ID")]
    except Exception:
        pass

    # Determine state and build pool
    state = "no_data"
    if uploaded_pids:
        state = "uploaded"
    elif picks:
        sample_pids = [p["pid"] for p in picks[:10]]
        in_amateur = 0
        in_org = 0
        for pid in sample_pids:
            row = conn.execute("SELECT level, parent_team_id FROM players WHERE player_id=?", (pid,)).fetchone()
            if row:
                if str(row[0]) in ('10', '11', '0'):
                    in_amateur += 1
                elif row[1] and row[1] > 0:
                    in_org += 1
        if in_amateur > in_org:
            state = "active"
        else:
            state = "pre_draft"
    elif amateur_levels:
        state = "pre_draft"

    if state == "uploaded":
        # Use exact uploaded pool. Discard API picks — they're from the prior draft.
        placeholders = ",".join("?" * len(uploaded_pids))
        sql = _DRAFT_SQL + f" AND r.player_id IN ({placeholders})"
        rows = conn.execute(sql, uploaded_pids).fetchall()
        results = [_build_prospect(r) for r in rows]
        results.sort(key=lambda x: (x['surplus'], x['fv'] + (0.5 if '+' in x['fv_str'] else 0)), reverse=True)
        _annotate_adp(results)
        for i, r in enumerate(results):
            r['rank'] = i + 1
        _apply_slot_value_estimates(results)
        return {"state": state, "players": results, "picks": [], "year": draft_year}

    elif state == "active":
        # Use draft API player IDs as the definitive pool
        pick_pids = [p["pid"] for p in picks]
        if not pick_pids:
            return {"state": state, "players": [], "picks": picks, "year": draft_year}
        placeholders = ",".join("?" * len(pick_pids))
        sql = _DRAFT_SQL + f" AND r.player_id IN ({placeholders})"
        rows = conn.execute(sql, pick_pids).fetchall()
        by_pid = {dict(r)["ID"]: r for r in rows}
        results = []
        for r in rows:
            results.append(_build_prospect(r))
        results.sort(key=lambda x: (x['surplus'], x['fv'] + (0.5 if '+' in x['fv_str'] else 0)), reverse=True)
        _annotate_adp(results)
        for i, r in enumerate(results):
            r['rank'] = i + 1
        _apply_slot_value_estimates(results, {p["pid"]: p["overall"] for p in picks})
        return {"state": state, "players": results, "picks": picks, "year": draft_year}

    elif state == "pre_draft" and amateur_levels:
        # Scouting approximation: top 800 amateurs by Pot. Discard API picks — stale from prior draft.
        clauses = []
        for lvl in amateur_levels:
            min_age = 19 if lvl == '10' else 18
            clauses.append(f"(p.level = '{lvl}' AND p.age >= {min_age})")
        where = " OR ".join(clauses)
        sql = _DRAFT_SQL + f" AND ({where}) ORDER BY r.pot DESC LIMIT 800"
        rows = conn.execute(sql).fetchall()
        results = [_build_prospect(r) for r in rows]
        results.sort(key=lambda x: (x['surplus'], x['fv'] + (0.5 if '+' in x['fv_str'] else 0)), reverse=True)
        _annotate_adp(results)
        for i, r in enumerate(results):
            r['rank'] = i + 1
        _apply_slot_value_estimates(results)
        return {"state": state, "players": results, "picks": [], "year": draft_year}

    return {"state": "no_data", "players": [], "picks": [], "year": draft_year}



# ---------------------------------------------------------------------------
# Positional Rankings
# ---------------------------------------------------------------------------

_POS_GROUPS = [
    ("C", {"positions": [2], "roles": [], "label": "C"}),
    ("1B", {"positions": [3], "roles": [], "label": "1B"}),
    ("2B", {"positions": [4], "roles": [], "label": "2B"}),
    ("3B", {"positions": [5], "roles": [], "label": "3B"}),
    ("SS", {"positions": [6], "roles": [], "label": "SS"}),
    ("CF", {"positions": [8], "roles": [], "label": "CF"}),
    ("COF", {"positions": [7, 9], "roles": [], "label": "COF"}),
    ("SP", {"positions": [], "roles": [11], "label": "SP"}),
    ("RP", {"positions": [], "roles": [12, 13], "label": "RP"}),
]

_BUCKET_TO_GROUP = {
    "C": "C", "1B": "1B", "2B": "2B", "3B": "3B", "SS": "SS",
    "CF": "CF", "COF": "COF", "SP": "SP", "RP": "RP",
}


def get_positional_rankings():
    """Return positional rankings: MLB players by composite, prospects by FV.

    Returns list of (key, {label, mlb: [...], prospects: [...]}).
    """
    conn = get_db()
    teams = team_abbr_map()
    cfg = get_cfg()
    stats_year = cfg.year
    # Captured before the `for key, cfg in _POS_GROUPS` loop below shadows
    # this `cfg` name with each position group's own config dict.
    ratings_scale = cfg.ratings_scale

    # Get MLB org IDs for filtering. Use the request-scoped mlb_team_ids()
    # (web_league_context), NOT LeagueConfig().mlb_team_ids — that fresh
    # singleton lazily caches whichever league it first computed for the life
    # of the process and never invalidates on /switch-league, so it can return
    # another league's team IDs and reject every player here (empty rankings).
    try:
        mlb_org_ids = set(mlb_team_ids())
    except Exception:
        mlb_org_ids = set(teams.keys())

    # MLB players with composite scores, PLUS unsigned free agents (team_id=0,
    # free_agent=1) that have played in this league — so users can see where an
    # available FA stacks up against rostered players at each position. FAs are
    # tagged is_fa=1 for the template to badge; foreign-league players (no stats
    # in this league) are excluded via the EXISTS check.
    mlb_rows = conn.execute("""
        SELECT p.player_id, p.name, p.age, p.pos, p.role, p.team_id,
               r.composite_score, r.true_ceiling, r.tool_only_score,
               r.offensive_grade, r.defensive_value, r.ctrl,
               r.c, r.first_b, r.second_b, r.third_b, r.ss, r.lf, r.cf, r.rf,
               0 AS is_fa
        FROM players p
        JOIN latest_ratings r ON r.player_id = p.player_id
        WHERE p.level = '1' AND r.composite_score IS NOT NULL
        UNION ALL
        SELECT p.player_id, p.name, p.age, p.pos, p.role, p.team_id,
               r.composite_score, r.true_ceiling, r.tool_only_score,
               r.offensive_grade, r.defensive_value, r.ctrl,
               r.c, r.first_b, r.second_b, r.third_b, r.ss, r.lf, r.cf, r.rf,
               1 AS is_fa
        FROM players p
        JOIN latest_ratings r ON r.player_id = p.player_id
        WHERE p.free_agent = 1 AND p.team_id = 0 AND r.composite_score IS NOT NULL
          AND (EXISTS (SELECT 1 FROM mlb_batting_stats b WHERE b.player_id = p.player_id)
            OR EXISTS (SELECT 1 FROM mlb_pitching_stats pt WHERE pt.player_id = p.player_id))
        ORDER BY composite_score DESC
    """).fetchall()

    # For pitchers, determine SP/RP from actual usage (GS ratio) rather than
    # role codes, which are unreliable across leagues (e.g. PPL uses role=12
    # for some starters).
    pitcher_is_sp = {}
    pit_stats = conn.execute("""
        SELECT player_id, gs, g
        FROM mlb_pitching_stats
        WHERE split_id = 1 AND year = ?
    """, (stats_year,)).fetchall()
    # Fall back to prior year if current year has no data (spring training)
    if not pit_stats:
        pit_stats = conn.execute("""
            SELECT player_id, gs, g
            FROM mlb_pitching_stats
            WHERE split_id = 1 AND year = ?
        """, (stats_year - 1,)).fetchall()
    for r in pit_stats:
        g = r["g"] or 0
        gs = r["gs"] or 0
        if g > 0:
            # A starter starts the majority of their appearances. The gs/g
            # ratio is the real discriminator; do NOT add an absolute GS floor
            # (e.g. gs > 3) — early in a season an ace has only 1-2 starts, and
            # such a floor would misclassify every starter as a reliever until
            # ~a month in. A reliever's occasional spot start keeps gs/g well
            # below 0.5, so the ratio alone handles that case.
            pitcher_is_sp[r["player_id"]] = (gs > 0 and gs / g > 0.5)

    # Prospects with FV grades — only from MLB orgs
    prospect_rows = conn.execute("""
        SELECT p.player_id, p.name, p.age, pf.bucket, p.team_id, p.parent_team_id, p.organization_id,
               pf.fv, pf.fv_str, pf.risk, r.true_ceiling, pf.prospect_surplus
        FROM prospect_fv pf
        JOIN players p ON pf.player_id = p.player_id
        JOIN latest_ratings r ON r.player_id = p.player_id
        WHERE pf.fv >= 40 AND p.age <= 25
        ORDER BY pf.fv DESC, pf.prospect_surplus DESC
    """).fetchall()

    result = []
    for key, cfg in _POS_GROUPS:
        group = {"label": cfg["label"], "mlb": [], "prospects": []}

        # Collect all MLB players for this position (for median calculation)
        all_composites = []

        # Assign MLB players
        for r in mlb_rows:
            pos, role = r["pos"], r["role"]
            pid = r["player_id"]

            # For pitchers (pos=1), use stats-based SP/RP classification
            # rather than role codes which are unreliable across leagues
            if pos == 1 and key in ("SP", "RP"):
                is_sp = pitcher_is_sp.get(pid)
                if is_sp is None:
                    # No stats — fall back to role code
                    is_sp = (role == 11)
                if key == "SP" and not is_sp:
                    continue
                if key == "RP" and is_sp:
                    continue
                matched = True
            else:
                matched = (role in cfg["roles"] or pos in cfg["positions"])

            if not matched:
                continue
            is_fa = bool(r["is_fa"])
            if not is_fa and r["team_id"] not in mlb_org_ids:
                continue

            all_composites.append(r["composite_score"])
            if len(group["mlb"]) < 20:
                from statsplusplus.config.ratings import norm as _norm_r
                # Get the defensive rating for this specific position group
                _pos_def_map = {
                    "C": r["c"], "1B": r["first_b"], "2B": r["second_b"],
                    "3B": r["third_b"], "SS": r["ss"],
                    "CF": r["cf"],
                    "COF": max(r["lf"] or 0, r["rf"] or 0) or None,
                }
                _pos_def_raw = _pos_def_map.get(key)
                # norm() defaults to scale="1-100" — silently wrong for a
                # "20-80" league (PPL), where a raw value already IS the
                # 20-80 grade and re-normalizing it as if on 1-100
                # understates it (e.g. norm(70, "1-100") -> 60, confirmed
                # against a real player: Terry Jessup's raw SS rating is
                # 70, displayed here as 60 before this fix).
                # Base = pure tool-only grade (no in-season stat blend);
                # Perf = how many points the stat blend added/subtracted to
                # arrive at the full Comp. Same tool_only_score already
                # shown parenthetically on the player page.
                _base = r["tool_only_score"] if r["tool_only_score"] is not None else r["composite_score"]
                _perf = (r["composite_score"] - _base) if (r["composite_score"] is not None and _base is not None) else None
                group["mlb"].append({
                    "pid": r["player_id"], "name": r["name"], "age": r["age"],
                    "team": "FA" if is_fa else teams.get(r["team_id"], "?"),
                    "is_fa": is_fa,
                    "base": _base, "perf": _perf,
                    "composite": r["composite_score"], "ceiling": r["true_ceiling"],
                    "off": r["offensive_grade"],
                    "def": _norm_r(_pos_def_raw, ratings_scale) if _pos_def_raw else None,
                    "rank": len(group["mlb"]) + 1,
                })

        # Compute positional median and add vs_avg
        pos_median = 0
        if all_composites:
            sorted_comps = sorted(all_composites)
            pos_median = sorted_comps[len(sorted_comps) // 2]
        group["median"] = pos_median
        _n_mlb = len(group["mlb"])
        for p in group["mlb"]:
            p["vs_avg"] = p["composite"] - pos_median if p["composite"] and pos_median else 0
            p["tier"] = _gr_tier(p["rank"], _n_mlb, "pill")

        # Assign prospects
        for r in prospect_rows:
            if len(group["prospects"]) >= 20:
                break
            if _BUCKET_TO_GROUP.get(r["bucket"]) == key:
                org_id = r["organization_id"] or r["parent_team_id"] or r["team_id"]
                if org_id not in mlb_org_ids:
                    continue
                group["prospects"].append({
                    "pid": r["player_id"], "name": r["name"], "age": r["age"],
                    "team": teams.get(org_id, "?"),
                    "fv": r["fv"], "fv_str": r["fv_str"], "risk": r["risk"],
                    "ceiling": r["true_ceiling"],
                    "surplus": round(r["prospect_surplus"] / _money_divisor(), 1) if r["prospect_surplus"] else 0,
                    "rank": len(group["prospects"]) + 1,
                })

        result.append((key, group))

    return result


# ── Waiver Wire ──────────────────────────────────────────────────────────

def get_waiver_wire():
    """Return players currently on waivers with evaluation context.

    Returns list of dicts sorted by composite score descending, with:
    - Player identity (name, age, pos, bats/throws)
    - Current ability (composite, ceiling, OVR)
    - Recent stats (current or prior year)
    - Contract/control context (salary, service time, years remaining)
    - Waiver context (days remaining, was DFA'd)
    - FV grade if prospect-eligible
    """
    conn = get_db()

    rows = conn.execute("""
        SELECT p.player_id, p.name, p.age, p.pos, p.role, p.level,
               p.days_on_waivers, p.days_on_waivers_left,
               p.designated_for_assignment, p.parent_team_id, p.team_id,
               p.mlb_service_years, p.mlb_service_days,
               p.injury_is_injured, p.injury_left,
               r.composite_score, r.true_ceiling, r.ceiling_score, r.ovr, r.pot,
               r.bats, r.throws,
               pf.fv, pf.bucket, pf.risk, pf.prospect_surplus,
               c.years AS contract_years, c.current_year AS contract_current_year,
               c.salary_0, p.organization_id
        FROM players p
        LEFT JOIN latest_ratings r ON p.player_id = r.player_id
        LEFT JOIN prospect_fv pf ON p.player_id = pf.player_id
        LEFT JOIN contracts c ON p.player_id = c.player_id
        WHERE p.is_on_waivers = 1
        ORDER BY COALESCE(r.composite_score, r.ovr, 0) DESC
    """).fetchall()

    from web_league_context import team_abbr_map, get_cfg
    _abbr = team_abbr_map()
    _year = get_cfg().year
    _pos_labels = {1: 'P', 2: 'C', 3: '1B', 4: '2B', 5: '3B', 6: 'SS', 7: 'LF', 8: 'CF', 9: 'RF', 10: 'DH'}

    results = []
    for row in rows:
        pid = row[0]
        pos_num = row[3]
        role = row[4]
        if role in (11, 12):
            pos_str = "SP"
        elif role == 13:
            pos_str = "RP"
        else:
            pos_str = _pos_labels.get(pos_num, "?")

        composite = row[15] or row[18] or 0
        ceiling = row[16] or row[17] or row[19] or 0
        org_id = row[29] or row[9] or row[10]

        # Get most recent stats
        stat_row = conn.execute("""
            SELECT year, pa, war FROM mlb_batting_stats
            WHERE player_id=? AND split_id=1 ORDER BY year DESC LIMIT 1
        """, (pid,)).fetchone() if role not in (11, 12, 13) else None

        pit_row = conn.execute("""
            SELECT year, ip, era, war FROM mlb_pitching_stats
            WHERE player_id=? AND split_id=1 ORDER BY year DESC LIMIT 1
        """, (pid,)).fetchone() if role in (11, 12, 13) else None

        # Service time and control
        from statsplusplus.evaluation.arb import service_time as _svc
        _st = _svc(conn, pid)
        contract_years = row[26]
        contract_cur = row[27]
        salary = row[28] or 0
        years_remaining = (contract_years - contract_cur) if contract_years and contract_cur is not None else None

        results.append({
            "player_id": pid,
            "name": row[1],
            "age": row[2],
            "pos": pos_str,
            "level": row[5],
            "team_abbr": _abbr.get(org_id, "?"),
            "bats": {1: "R", 2: "L", 3: "S"}.get(row[20], "?"),
            "throws": {1: "R", 2: "L"}.get(row[21], "?"),
            "composite": round(composite),
            "ceiling": round(ceiling),
            "days_left": row[7],
            "was_dfa": bool(row[8]),
            "injured": bool(row[13]) and (row[14] or 0) > 0,
            "fv": row[22],
            "bucket": row[23],
            "risk": row[24],
            "surplus": row[25],  # prospect_surplus from pf table
            "service": _st.display() if _st.total_days > 0 else None,
            "salary": salary,
            "years_control": years_remaining,
            "bat_stats": {"year": stat_row[0], "pa": stat_row[1], "war": round(stat_row[2], 1)} if stat_row else None,
            "pit_stats": {"year": pit_row[0], "ip": round(pit_row[1], 1), "era": round(pit_row[2], 2), "war": round(pit_row[3], 1)} if pit_row else None,
        })

    return results
