"""Season Pulse: luck vs true talent (team and players) and the rest-of-season
schedule outlook.

Luck / true talent
    Actual record vs Pythagorean record, one-run and blowout records,
    home/road splits, record by opponent, a rolling run-differential trend, and
    the league's luckiest/unluckiest clubs. Player side: hitters whose BABIP is
    far from their career norm, pitchers whose ERA is far from FIP or whose HR/FB
    is extreme, and anyone running well above or below what his ratings imply.

Rest of season
    The real remaining schedule is read from the game's own HTML schedule pages
    (team_<id>_schedule_<month>.html in the save's news/html/teams folder, which
    the game writes when you open the schedule). Each game gets an expected win
    probability (log5 of the two clubs' run-differential strengths); weeks are
    rated by opponent strength; and the clinch date is simulated game by game.
"""

from __future__ import annotations

import math
import re
from datetime import date, timedelta
from pathlib import Path

import numpy as np

from web_league_context import get_db, get_cfg
import trade_center_queries as tc

PYTH_EXP = tc.PYTH_EXP


# ── schedule pages from the OOTP save ────────────────────────────────────────

def _teams_html_dir(league_slug: str):
    home = Path.home()
    saves = {"ppl": ("OOTP Baseball 26", "PPL.lg"), "emlb": ("OOTP Baseball 27", "eMLB.lg")}
    if league_slug not in saves:
        return None
    game, lg = saves[league_slug]
    for base in (home / "Application Support", home / "Library" / "Application Support"):
        p = base / "Out of the Park Developments" / game / "saved_games" / lg / "news" / "html" / "teams"
        if p.is_dir():
            return p
    return None


_MONTHS = {m: i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July", "August",
     "September", "October", "November", "December"], 1)}
_CELL_SPLIT = re.compile(r'<td valign="top" class="sdl_')
_DAY = re.compile(r'font-size:16px; font-weight:bold; color:#999999;">(\d+)</td>')
_GAME = re.compile(r'href="\.\./teams/team_(\d+)\.html" style="font-size:13px; font-weight:bold;">(vs|@) ([A-Z]+)(?: \((\d)\))?</a>')
_RESULT = re.compile(r'class="sdl_(won|lost)"')


def parse_schedule_html(text: str):
    """[(date, opp_team_id, home: bool, played: bool)] from one month page."""
    m = re.search(r"Schedule (\w+) (\d{4})", text)
    if not m or m.group(1) not in _MONTHS:
        return []
    month, year = _MONTHS[m.group(1)], int(m.group(2))
    out = []
    for chunk in _CELL_SPLIT.split(text)[1:]:
        d = _DAY.search(chunk)
        games = _GAME.findall(chunk)
        if not d or not games:
            continue
        played = len(_RESULT.findall(chunk))
        day = date(year, month, int(d.group(1)))
        n_listed = 0
        for opp, side, _abbr, dh in games:
            for _ in range(int(dh) if dh else 1):
                out.append((day, int(opp), side == "vs", n_listed < played))
                n_listed += 1
    return out


def remaining_schedule(team_id: int, league_slug: str, game_date: str, gps: int, games_left: int):
    """The club's unplayed games in date order, from the newest month pages that
    belong to the current season; None if the pages aren't there or don't cover
    the rest of the season."""
    d = _teams_html_dir(league_slug)
    if not d:
        return None
    gd = date.fromisoformat(game_date)
    games = []
    for f in d.glob(f"team_{team_id}_schedule_*.html"):
        try:
            rows = parse_schedule_html(f.read_text(errors="ignore"))
        except OSError:
            continue
        if rows and rows[0][0].year == gd.year:
            games.extend(rows)
    future = sorted({(g[0], g[1], g[2], i) for i, g in enumerate(games)
                     if not g[3] and g[0] > gd}, key=lambda x: (x[0], x[3]))
    future = [(dt, opp, home) for dt, opp, home, _ in future]
    if not future or abs(len(future) - games_left) > 2:
        return None        # stale or missing month page: don't pretend
    return future


# ── team luck ────────────────────────────────────────────────────────────────

def _pyth(rs, ra):
    if rs + ra <= 0:
        return 0.5
    return rs ** PYTH_EXP / (rs ** PYTH_EXP + ra ** PYTH_EXP)


def _team_games(conn, team_id, year):
    rows = conn.execute(
        "SELECT date, home_team, away_team, runs0, runs1 FROM games "
        "WHERE played=1 AND game_type=0 AND date>=? AND date<? AND (home_team=? OR away_team=?) "
        "ORDER BY date, game_id",
        (f"{year}-01-01", f"{year + 1}-01-01", team_id, team_id)).fetchall()
    out = []
    for dt, h, a, r_away, r_home in rows:
        home = h == team_id
        rs, ra = (r_home, r_away) if home else (r_away, r_home)
        out.append({"date": dt, "opp": a if home else h, "home": home, "rs": rs or 0, "ra": ra or 0})
    return out


def _wl(games):
    w = sum(1 for g in games if g["rs"] > g["ra"])
    l = sum(1 for g in games if g["rs"] < g["ra"])
    rs = sum(g["rs"] for g in games)
    ra = sum(g["ra"] for g in games)
    return {"w": w, "l": l, "rs": rs, "ra": ra, "diff": rs - ra, "n": len(games)}


def team_luck(conn, team_id, year):
    games = _team_games(conn, team_id, year)
    tot = _wl(games)
    p = _pyth(tot["rs"], tot["ra"])
    exp_w = p * tot["n"]
    one = _wl([g for g in games if abs(g["rs"] - g["ra"]) == 1])
    blow = _wl([g for g in games if abs(g["rs"] - g["ra"]) >= 5])
    home = _wl([g for g in games if g["home"]])
    road = _wl([g for g in games if not g["home"]])
    by_opp = {}
    for g in games:
        by_opp.setdefault(g["opp"], []).append(g)
    names = get_cfg().team_names_map or {}
    opp_rows = sorted(({"tid": o, "name": names.get(o, str(o)), **_wl(gs)} for o, gs in by_opp.items()),
                      key=lambda r: -(r["w"] - r["l"]))
    # Rolling 15-game window
    trend, win = [], 15
    for i in range(win, len(games) + 1):
        seg = games[i - win:i]
        s = _wl(seg)
        trend.append({"date": seg[-1]["date"], "diff": s["diff"], "wpct": s["w"] / win})
    # By month
    months = {}
    for g in games:
        months.setdefault(g["date"][:7], []).append(g)
    month_rows = [{"month": m, **_wl(gs)} for m, gs in sorted(months.items())]
    for r in month_rows:
        r["pyth_w"] = _pyth(r["rs"], r["ra"]) * r["n"]
    return {"total": tot, "pyth_pct": p, "pyth_w": exp_w, "luck": tot["w"] - exp_w,
            "one_run": one, "blowout": blow, "home": home, "road": road,
            "by_opp": opp_rows, "trend": trend, "months": month_rows}


def league_luck(conn, year):
    names = get_cfg().team_names_map or {}
    mlb = sorted(get_cfg().mlb_team_ids or [])
    rows = []
    for t in mlb:
        s = _wl(_team_games(conn, t, year))
        if not s["n"]:
            continue
        pw = _pyth(s["rs"], s["ra"]) * s["n"]
        rows.append({"tid": t, "name": names.get(t, str(t)), **s, "pyth_w": pw, "luck": s["w"] - pw})
    rows.sort(key=lambda r: -r["luck"])
    return rows


# ── player luck ──────────────────────────────────────────────────────────────

def player_luck(conn, team_id, year, weights):
    from statsplusplus.evaluation.war import peak_war_from_score
    out_h, out_p = [], []
    # Hitters: season BABIP vs career BABIP; WAR pace vs ratings-implied WAR.
    for r in conn.execute("""
        SELECT p.player_id, p.name, p.pos, r.composite_score AS comp,
               SUM(CASE WHEN b.year=? THEN b.h-b.hr END) AS h_bip, SUM(CASE WHEN b.year=? THEN b.ab-b.k-b.hr+b.sf END) AS bip,
               SUM(b.h-b.hr) AS c_hbip, SUM(b.ab-b.k-b.hr+b.sf) AS c_bip,
               SUM(CASE WHEN b.year=? THEN b.war END) AS war, SUM(CASE WHEN b.year=? THEN b.g END) AS g,
               SUM(CASE WHEN b.year=? THEN b.pa END) AS pa
        FROM players p JOIN mlb_batting_stats b ON b.player_id=p.player_id AND b.split_id=1
        JOIN latest_ratings r ON r.player_id=p.player_id
        WHERE p.team_id=? AND p.level='1' AND COALESCE(p.role,0) NOT IN (11,12,13)
        GROUP BY p.player_id HAVING pa >= 80
    """, (year, year, year, year, year, team_id)).fetchall():
        babip = r["h_bip"] / r["bip"] if r["bip"] else None
        car = r["c_hbip"] / r["c_bip"] if r["c_bip"] else None
        slot = tc.POS_TO_SLOT.get(r["pos"])
        exp = peak_war_from_score(r["comp"], tc.SLOT_BUCKET.get(slot, "COF"), weights) if slot else None
        pace = (r["war"] or 0) / r["g"] * 154 if r["g"] else None
        out_h.append({"pid": r["player_id"], "name": r["name"], "pa": r["pa"], "babip": babip,
                      "career_babip": car, "babip_diff": (babip - car) if babip is not None and car else None,
                      "war": r["war"], "pace": pace, "exp_war": exp,
                      "war_gap": (pace - exp) if pace is not None and exp is not None else None})
    # Pitchers: ERA vs FIP, HR/FB, BABIP against.
    lg = conn.execute("SELECT SUM(er)*9.0/SUM(outs/3.0), SUM(hra)*13.0, SUM(bb+hp)*3.0, SUM(k)*2.0, SUM(outs)/3.0, "
                      "SUM(hra)*1.0/NULLIF(SUM(fb),0) FROM mlb_pitching_stats WHERE year=? AND split_id=1",
                      (year,)).fetchone()
    lg_era, lg_ip, lg_hrfb = lg[0], lg[4], lg[5]
    fip_c = (lg_era - (lg[1] + lg[2] - lg[3]) / lg_ip) if lg_ip else 3.1
    for r in conn.execute("""
        SELECT p.player_id, p.name, p.role, r.composite_score AS comp,
               SUM(s.outs)/3.0 AS ip, SUM(s.er) AS er, SUM(s.hra) AS hr, SUM(s.bb+s.hp) AS bb,
               SUM(s.k) AS k, SUM(s.fb) AS fb, SUM(s.ha) AS ha, SUM(s.bf) AS bf, SUM(s.war) AS war
        FROM players p JOIN mlb_pitching_stats s ON s.player_id=p.player_id AND s.split_id=1 AND s.year=?
        JOIN latest_ratings r ON r.player_id=p.player_id
        WHERE p.team_id=? AND p.level='1' AND p.role IN (11,12,13)
        GROUP BY p.player_id HAVING ip >= 20
    """, (year, team_id)).fetchall():
        ip = r["ip"]
        era = r["er"] * 9 / ip
        fip = (13 * r["hr"] + 3 * r["bb"] - 2 * r["k"]) / ip + fip_c
        hrfb = r["hr"] / r["fb"] if r["fb"] else None
        bip = (r["bf"] or 0) - (r["k"] or 0) - (r["bb"] or 0) - (r["hr"] or 0)
        babip = ((r["ha"] or 0) - (r["hr"] or 0)) / bip if bip > 0 else None
        out_p.append({"pid": r["player_id"], "name": r["name"], "ip": ip, "era": era, "fip": fip,
                      "gap": era - fip, "hrfb": hrfb, "lg_hrfb": lg_hrfb, "babip": babip, "war": r["war"]})
    out_h.sort(key=lambda x: -(abs(x["babip_diff"] or 0)))
    out_p.sort(key=lambda x: -abs(x["gap"]))
    return out_h, out_p


# ── rest of season ───────────────────────────────────────────────────────────

def rest_of_season(conn, team_id, year, gps, league_slug, game_date, n_sims=20000, seed=11):
    stand, strength, leagues, remaining, rs, ra = tc._race_inputs(conn, year, gps)
    names = get_cfg().team_names_map or {}
    abbr = get_cfg().team_abbr_map or {}
    games_left = sum(n for a, b, n in remaining if team_id in (a, b))
    sched = remaining_schedule(team_id, league_slug, game_date, gps, games_left)
    my = strength.get(team_id, 0.5)
    source = "game schedule pages"
    if sched is None:
        # Fall back to the reconstructed head-to-head counts without dates.
        source = "reconstructed head-to-head counts (open the club's schedule in the game to get dates)"
        sched = []
        for a, b, n in remaining:
            if team_id in (a, b):
                opp = b if a == team_id else a
                sched += [(None, opp, None)] * n
    rows = []
    for dt, opp, home in sched:
        p = tc._log5(my, strength.get(opp, 0.5))
        rows.append({"date": dt, "opp": opp, "opp_name": names.get(opp, str(opp)),
                     "abbr": abbr.get(opp, ""), "home": home, "opp_pct": strength.get(opp, 0.5), "p_win": p})

    # Weeks (Mon-Sun) with expected wins and difficulty.
    weeks = []
    if rows and rows[0]["date"]:
        by = {}
        for r in rows:
            wk = r["date"] - timedelta(days=r["date"].weekday())
            by.setdefault(wk, []).append(r)
        for wk, rs_ in sorted(by.items()):
            opp = sorted({x["abbr"] + ("" if x["home"] else "@") for x in rs_})
            weeks.append({"start": wk, "games": len(rs_), "exp_w": sum(x["p_win"] for x in rs_),
                          "opp_pct": sum(x["opp_pct"] for x in rs_) / len(rs_),
                          "home": sum(1 for x in rs_ if x["home"]), "opps": rs_})
    # By-opponent remaining summary
    by_opp = {}
    for r in rows:
        by_opp.setdefault(r["opp"], []).append(r)
    opp_rows = sorted(({"name": v[0]["opp_name"], "games": len(v), "opp_pct": v[0]["opp_pct"],
                        "exp_w": sum(x["p_win"] for x in v), "home": sum(1 for x in v if x["home"])}
                       for v in by_opp.values()), key=lambda x: -x["opp_pct"])

    # Clinch timing: my games in order; the closest rival's games spread over the same span.
    lg = next((l for l in leagues if team_id in l), [])
    rivals = sorted([t for t in lg if t != team_id], key=lambda t: -(stand[t]["w"] - stand[t]["l"]))
    clinch = None
    if rivals and rows:
        rival = rivals[0]
        magic = gps + 1 - stand[team_id]["w"] - stand[rival]["l"]
        rng = np.random.default_rng(seed)
        my_p = np.array([r["p_win"] for r in rows])
        my_win = rng.random((n_sims, len(rows))) < my_p
        rival_left = sum(n for a, b, n in remaining if rival in (a, b))
        rival_opps = [b if a == rival else a for a, b, n in remaining if rival in (a, b) for _ in range(n)]
        rival_p = np.array([tc._log5(strength[rival], strength.get(o, 0.5)) for o in rival_opps]) if rival_opps else np.zeros(0)
        rival_loss = rng.random((n_sims, rival_left)) >= rival_p if rival_left else np.zeros((n_sims, 0), bool)
        # rival game k lands in the slot of my game floor(k * len(rows) / rival_left)
        pos = (np.arange(rival_left) * len(rows) // max(1, rival_left)) if rival_left else np.zeros(0, int)
        rl_by_slot = np.zeros((n_sims, len(rows)))
        if rival_left:
            np.add.at(rl_by_slot.T, pos, rival_loss.T)
        progress = np.cumsum(my_win, axis=1) + np.cumsum(rl_by_slot, axis=1)
        done = progress >= magic
        first = np.where(done.any(axis=1), done.argmax(axis=1), len(rows))
        p_clinch = float(np.mean(first < len(rows)))
        if magic <= 0:
            clinch = {"magic": 0, "rival": names.get(rival, ""), "clinched": True}
        else:
            qs = np.percentile(first[first < len(rows)], [10, 50, 90]) if (first < len(rows)).any() else None
            def _game(i):
                i = int(min(len(rows) - 1, i))
                return {"n": i + 1, "date": rows[i]["date"], "opp": rows[i]["abbr"]}
            by_week = []
            for w in weeks:
                last_idx = max(i for i, r in enumerate(rows) if r in w["opps"])
                by_week.append({"start": w["start"], "p": float(np.mean(first <= last_idx))})
            clinch = {"magic": magic, "rival": names.get(rival, ""), "p_clinch": p_clinch,
                      "p10": _game(qs[0]) if qs is not None else None,
                      "p50": _game(qs[1]) if qs is not None else None,
                      "p90": _game(qs[2]) if qs is not None else None,
                      "by_week": by_week}

    tough = [r for r in rows if r["opp_pct"] >= 0.54]
    exp_final_w = stand[team_id]["w"] + sum(r["p_win"] for r in rows)
    return {"games": rows, "weeks": weeks, "by_opp": opp_rows, "source": source, "clinch": clinch,
            "tough": tough, "exp_final_w": exp_final_w, "my_strength": my,
            "dated": bool(rows and rows[0]["date"])}


def build(team_id, league_dir):
    from statsplusplus.config.league_config import games_per_season
    from statsplusplus.evaluation.constants import load_model_weights
    import json
    conn = get_db()
    gps = games_per_season(league_dir)
    year = conn.execute("SELECT MAX(year) FROM mlb_batting_stats").fetchone()[0]
    game_date = json.loads((Path(league_dir) / "config" / "state.json").read_text())["game_date"]
    weights = load_model_weights(Path(league_dir))
    hit, pit = player_luck(conn, team_id, year, weights)
    rotation = [r[0] for r in conn.execute(
        "SELECT p.name FROM depth_chart_roles d JOIN players p ON p.player_id=d.player_id "
        "WHERE d.team_id=? AND d.position='SP' AND d.role='starter' ORDER BY COALESCE(d.slot, 99)",
        (team_id,)).fetchall()]
    import stretch_queries as sq
    ros = rest_of_season(conn, team_id, year, gps, Path(league_dir).name, game_date)
    try:
        plan = sq.rotation_plan(conn, team_id, year, ros)
    except Exception:
        plan = None
    try:
        series = sq.series_prep(conn, team_id, year, gps)
    except Exception:
        series = None
    try:
        ws_roster = sq.ws_roster(conn, team_id, year, series)
    except Exception:
        ws_roster = None
    return {"year": year, "game_date": game_date, "rotation": rotation,
            "team": team_luck(conn, team_id, year), "league": league_luck(conn, year),
            "hitters": hit, "pitchers": pit,
            "ros": ros, "plan": plan, "series": series, "ws_roster": ws_roster}
