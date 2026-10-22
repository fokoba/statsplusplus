"""Stretch run and World Series planning (shown on the Season Pulse page).

Rotation planner
    Walks the remaining dated schedule and picks a starter for every game: the
    depth-chart rotation in order, but (a) the best rested starter takes games
    against strong clubs, (b) once a starter's projected innings pass his cap
    (his career high + 5% by default) or the pennant is very likely settled, a
    rested spot starter (Yeager-type #4 or a long reliever) takes the game
    against weaker clubs, and (c) the ace is lined up rested for World Series
    Game 1. Starters need 3 days of rest (four-man rotation).

World Series prep
    The likely opponent is the other league's pennant favourite from the same
    race simulation as the Trade Center. Their lineup's results against
    right- and left-handed pitching, their rotation and bullpen, your split
    lineups, and series odds for a four-man and a three-man rotation (each
    starter's FIP shifts the per-game win probability).
"""

from __future__ import annotations

import math
from datetime import date, timedelta

from web_league_context import get_db, get_cfg
import trade_center_queries as tc

REST_DAYS = 3                # days between starts in a four-man rotation
CAP_OVER_HIGH = 1.05         # innings cap = career high x this
WIN_PER_FIP_RUN = 0.06       # per-game win prob change per run of starter FIP edge
TOUGH = 0.54                 # opponent true strength that earns a top starter
WS_GAP_DAYS = 2              # assumed days from the last regular game to WS Game 1


def _fip_const(conn, year):
    lg = conn.execute("SELECT SUM(er)*9.0/SUM(outs/3.0), SUM(hra), SUM(bb+hp), SUM(k), SUM(outs)/3.0 "
                      "FROM mlb_pitching_stats WHERE year=? AND split_id=1", (year,)).fetchone()
    if not lg or not lg[4]:
        return 3.1, 3.8
    return lg[0] - (13 * lg[1] + 3 * lg[2] - 2 * lg[3]) / lg[4], lg[0]


def _pitchers(conn, team_id, year, fip_c):
    out = {}
    for r in conn.execute("""
        SELECT p.player_id, p.name, p.age, p.role, r.throws, r.stm,
               SUM(s.outs)/3.0 AS ip, SUM(s.gs) AS gs, SUM(s.g) AS g, SUM(s.er) AS er,
               SUM(s.hra) AS hr, SUM(s.bb+s.hp) AS bb, SUM(s.k) AS k, SUM(s.war) AS war
        FROM players p JOIN latest_ratings r ON r.player_id=p.player_id
        LEFT JOIN mlb_pitching_stats s ON s.player_id=p.player_id AND s.year=? AND s.split_id=1
        WHERE p.team_id=? AND p.level='1' AND p.role IN (11,12,13)
        GROUP BY p.player_id
    """, (year, team_id)).fetchall():
        ip = r["ip"] or 0
        fip = ((13 * (r["hr"] or 0) + 3 * (r["bb"] or 0) - 2 * (r["k"] or 0)) / ip + fip_c) if ip >= 10 else None
        hist = conn.execute("SELECT MAX(t) FROM (SELECT SUM(outs)/3.0 AS t FROM mlb_pitching_stats "
                            "WHERE player_id=? AND split_id=1 AND year<? GROUP BY year)",
                            (r["player_id"], year)).fetchone()[0]
        out[r["player_id"]] = {"pid": r["player_id"], "name": r["name"], "age": r["age"], "role": r["role"],
                               "throws": r["throws"] or "R", "stm": r["stm"], "ip": ip, "gs": r["gs"] or 0,
                               "g": r["g"] or 0, "era": (r["er"] or 0) * 9 / ip if ip else None,
                               "fip": fip, "war": r["war"], "career_high": hist,
                               "ip_per_gs": ip / r["gs"] if r["gs"] else None}
    return out


def _roles(conn, team_id):
    rot = [r[0] for r in conn.execute(
        "SELECT player_id FROM depth_chart_roles WHERE team_id=? AND position='SP' AND role='starter' "
        "ORDER BY COALESCE(slot, 99)", (team_id,)).fetchall()]
    spot = [r[0] for r in conn.execute(
        "SELECT player_id FROM depth_chart_roles WHERE team_id=? AND "
        "((position='SP' AND role='spot_starter') OR (position='RP' AND role='long_relief')) "
        "ORDER BY COALESCE(slot, 99)", (team_id,)).fetchall()]
    return rot, spot


# ── rotation planner ─────────────────────────────────────────────────────────

def rotation_plan(conn, team_id, year, ros):
    fip_c, lg_era = _fip_const(conn, year)
    P = _pitchers(conn, team_id, year, fip_c)
    rot, spot = _roles(conn, team_id)
    if not rot:
        rot = [pid for pid, p in sorted(P.items(), key=lambda kv: -kv[1]["gs"]) if p["role"] == 11][:4]
    rot = [pid for pid in rot if pid in P]
    spot = [pid for pid in spot if pid in P and pid not in rot]
    if len(rot) >= 4 and rot[3] not in spot:
        spot = [rot[3]] + spot           # the #4 is the first choice to absorb spot starts
    games = [g for g in ros["games"] if g["date"]]
    if not games or not rot:
        return None
    clinch_date = (ros.get("clinch") or {}).get("p50", {}) or {}
    clinch_date = clinch_date.get("date")
    last_game = games[-1]["date"]
    ws_g1 = last_game + timedelta(days=WS_GAP_DAYS)
    ace = rot[0]

    proj_ip = {pid: P[pid]["ip"] for pid in P}
    # Innings per start: his own average once he has a real sample of starts,
    # otherwise a long reliever's ~5 innings. Capped at 9 (relief innings with a
    # single start would otherwise read as 25 IP per start).
    per_start = {pid: (min(9.0, P[pid]["ip_per_gs"]) if P[pid]["gs"] >= 5 and P[pid]["ip_per_gs"] else 5.0)
                 for pid in P}
    cap = {pid: (P[pid]["career_high"] or proj_ip[pid] + 60) * CAP_OVER_HIGH for pid in P}
    last_start = {pid: None for pid in P}
    order_i = 0
    plan = []
    for g in games:
        d = g["date"]

        def rested(pid):
            ls = last_start[pid]
            return ls is None or (d - ls).days > REST_DAYS

        # keep the ace fresh for World Series Game 1
        def protects_ws(pid):
            return pid == ace and (ws_g1 - d).days <= REST_DAYS

        tough = g["opp_pct"] >= TOUGH
        settled = clinch_date is not None and d > clinch_date
        nxt = rot[order_i % len(rot)]
        pick, reason = None, ""
        if tough:
            cands = [p for p in rot[:2] if rested(p) and not protects_ws(p)]
            if cands:
                pick, reason = cands[0], "top starter vs a strong club"
        # one tune-up start for the ace 4-6 days before World Series Game 1
        if pick is None and 4 <= (ws_g1 - d).days <= 6 and rested(ace) and ace not in [
                x["starter_pid"] for x in plan if 4 <= (ws_g1 - x["date"]).days <= 6]:
            pick, reason = ace, "tune-up start, rested for World Series Game 1"
        if pick is None:
            cand = nxt
            over = proj_ip[cand] + per_start[cand] > cap[cand]
            if (not rested(cand) or protects_ws(cand) or (over and cand in rot[:3] and not tough)):
                alt = [p for p in spot if rested(p)]
                if alt:
                    pick = alt[0]
                    reason = ("lines up the ace for World Series Game 1" if protects_ws(cand) else
                              f"spares {P[cand]['name']} (past his innings cap{', pennant settled' if settled else ''})" if over else
                              f"{P[cand]['name']} not rested")
                    order_i += 1
            if pick is None:
                cands = [p for p in rot if rested(p) and not protects_ws(p)] or [cand]
                pick = cand if cand in cands else cands[0]
                reason = reason or "regular turn"
                order_i += 1
        last_start[pick] = d
        proj_ip[pick] += per_start[pick]
        plan.append({**g, "starter": P[pick]["name"], "starter_pid": pick, "reason": reason,
                     "spot": pick not in rot[:3]})

    summary = []
    for pid in list(dict.fromkeys(rot + spot)):
        p = P[pid]
        starts = sum(1 for x in plan if x["starter_pid"] == pid)
        pace_naive = p["ip"] + (len(games) / len(rot)) * (per_start[pid]) if pid in rot else p["ip"]
        summary.append({"name": p["name"], "throws": p["throws"], "ip": p["ip"], "starts": starts,
                        "proj": proj_ip[pid], "naive": pace_naive, "cap": cap[pid],
                        "high": p["career_high"], "over": proj_ip[pid] > cap[pid]})
    ace_last = last_start[ace]
    return {"plan": plan, "summary": summary, "ws_g1": ws_g1,
            "ace": P[ace]["name"], "ace_rest": (ws_g1 - ace_last).days if ace_last else None,
            "spot_starts": sum(1 for x in plan if x["spot"]), "clinch_date": clinch_date}


# ── World Series prep ───────────────────────────────────────────────────────

def _team_splits(conn, team_id, year):
    out = {}
    for split, key in ((2, "vl"), (3, "vr")):
        r = conn.execute("""SELECT SUM(pa), SUM(ab), SUM(h), SUM(d), SUM(t), SUM(hr), SUM(bb), SUM(hbp), SUM(sf)
                            FROM mlb_batting_stats WHERE year=? AND split_id=? AND team_id=?""",
                         (year, split, team_id)).fetchone()
        pa, ab, h, d2, t3, hr, bb, hbp, sf = [x or 0 for x in r]
        obp = (h + bb + hbp) / (ab + bb + hbp + sf) if ab else None
        slg = (h + d2 + 2 * t3 + 3 * hr) / ab if ab else None
        out[key] = {"pa": pa, "obp": obp, "slg": slg, "ops": (obp or 0) + (slg or 0) if obp else None}
    return out


def _league_splits(conn, year):
    out = {}
    for split, key in ((2, "vl"), (3, "vr")):
        r = conn.execute("""SELECT SUM(ab), SUM(h), SUM(d), SUM(t), SUM(hr), SUM(bb), SUM(hbp), SUM(sf)
                            FROM mlb_batting_stats WHERE year=? AND split_id=?""", (year, split)).fetchone()
        ab, h, d2, t3, hr, bb, hbp, sf = [x or 0 for x in r]
        obp = (h + bb + hbp) / (ab + bb + hbp + sf) if ab else 0
        slg = (h + d2 + 2 * t3 + 3 * hr) / ab if ab else 0
        out[key] = obp + slg
    return out


def series_prep(conn, team_id, year, gps):
    sim = tc.simulate(conn, year, gps, team_id, war_steps=[0])
    res = sim["results"][0]
    other = next((lg for lg in sim["leagues"] if team_id not in lg), [])
    if not other:
        return None
    opp = max(other, key=lambda t: res["pennant"][t])
    names = get_cfg().team_names_map or {}
    fip_c, lg_era = _fip_const(conn, year)
    mine = _pitchers(conn, team_id, year, fip_c)
    theirs = _pitchers(conn, opp, year, fip_c)
    rot, _spot = _roles(conn, team_id)
    rot = [p for p in rot if p in mine] or [pid for pid, p in sorted(mine.items(), key=lambda kv: -kv[1]["gs"])][:4]
    their_rot = [pid for pid, p in sorted(theirs.items(), key=lambda kv: -kv[1]["gs"]) if p["role"] == 11][:4]
    their_pen = sorted([p for p in theirs.values() if p["role"] in (12, 13) and p["ip"] >= 15],
                       key=lambda p: (p["fip"] or 9))[:5]
    splits = _team_splits(conn, opp, year)
    my_splits = _team_splits(conn, team_id, year)
    lg = _league_splits(conn, year)
    lg_fip = lg_era

    p_base = tc._log5(sim["strength"][team_id], sim["strength"][opp])

    def game_p(my_sp, their_sp):
        mf = (mine.get(my_sp) or {}).get("fip") or lg_fip
        tf = (theirs.get(their_sp) or {}).get("fip") or lg_fip
        # our starter's edge on them minus theirs on us, partial-game weight
        return min(0.9, max(0.1, p_base + WIN_PER_FIP_RUN * (tf - mf)))

    def series(order_mine, order_theirs):
        ps = [game_p(order_mine[i], order_theirs[i]) for i in range(7)]
        # P(win best-of-7) with game-specific probabilities (DP over games)
        dist = {(0, 0): 1.0}
        win = 0.0
        for p in ps:
            nd = {}
            for (w, l), pr in dist.items():
                if w == 4 or l == 4:
                    continue
                for (dw, dl, q) in ((1, 0, p), (0, 1, 1 - p)):
                    k = (w + dw, l + dl)
                    nd[k] = nd.get(k, 0) + pr * q
            for (w, l), pr in nd.items():
                if w == 4:
                    win += pr
            dist = {k: v for k, v in nd.items() if k[0] < 4 and k[1] < 4}
        return win, ps

    tr = (their_rot + their_rot + their_rot)[:7] if their_rot else [None] * 7
    options = []
    if len(rot) >= 4:
        four = [rot[i % 4] for i in range(7)]
        w4, p4 = series(four, tr)
        options.append({"label": "Four-man (1-2-3-4-1-2-3)", "order": [mine[p]["name"] for p in four], "p": w4, "games": p4})
    if len(rot) >= 3:
        three = [rot[i % 3] for i in range(7)]
        w3, p3 = series(three, tr)
        options.append({"label": "Three-man (1-2-3-1-2-3-1)", "order": [mine[p]["name"] for p in three], "p": w3, "games": p3})
    options.sort(key=lambda o: -o["p"])

    # Our split lineups (best 8 hitters by vR / vL composite)
    from team_queries import _org_vr_vl_composites
    comps = _org_vr_vl_composites(conn, team_id)
    hitters = [dict(r) for r in conn.execute(
        "SELECT player_id, name, pos FROM players WHERE team_id=? AND level='1' AND COALESCE(role,0) NOT IN (11,12,13)",
        (team_id,)).fetchall()]
    for h in hitters:
        c = comps.get(h["player_id"]) or {}
        h["vr"], h["vl"] = c.get("vr") or 0, c.get("vl") or 0
        h["pos_name"] = {2: "C", 3: "1B", 4: "2B", 5: "3B", 6: "SS", 7: "LF", 8: "CF", 9: "RF"}.get(h["pos"], "?")
    best_vr = sorted(hitters, key=lambda h: -h["vr"])[:9]
    best_vl = sorted(hitters, key=lambda h: -h["vl"])[:9]
    their_lefty_starts = sum(1 for p in their_rot if theirs[p]["throws"] == "L")
    return {
        "opp": opp, "opp_name": names.get(opp, str(opp)), "p_opp": res["pennant"][opp],
        "p_title": res["title"].get(team_id, 0), "opp_strength": sim["strength"][opp],
        "my_strength": sim["strength"][team_id], "p_game": p_base,
        "splits": splits, "my_splits": my_splits, "lg": lg,
        "their_rot": [theirs[p] for p in their_rot], "their_pen": their_pen,
        "my_rot": [mine[p] for p in rot], "options": options,
        "best_vr": best_vr, "best_vl": best_vl, "their_lefty_starts": their_lefty_starts,
        "my_lefties": [mine[p]["name"] for p in rot if mine[p]["throws"] == "L"],
        "my_lefty_rp": [p["name"] for p in mine.values() if p["throws"] == "L" and p["pid"] not in rot],
    }


# ── World Series roster builder ──────────────────────────────────────────────

ROSTER_SIZE = 25
N_PITCHERS = 10
_POS_COLS = {"C": "c", "1B": "first_b", "2B": "second_b", "3B": "third_b", "SS": "ss",
             "LF": "lf", "CF": "cf", "RF": "rf"}
_FILL_ORDER = ["C", "SS", "CF", "2B", "3B", "RF", "LF", "1B"]   # scarce positions first


def ws_roster(conn, team_id, year, series):
    """Best 25 for a short series: the series rotation, the best bullpen (at least
    one lefty), a lineup for the opponent's dominant starter hand, and a bench
    built by job (backup C, platoon bat for the other hand, pinch-runner,
    defensive sub, utility infielder)."""
    if not series:
        return None
    from team_queries import _org_vr_vl_composites
    fip_c, _ = _fip_const(conn, year)
    P = _pitchers(conn, team_id, year, fip_c)
    comps = _org_vr_vl_composites(conn, team_id)
    vs_right = series["their_lefty_starts"] * 2 <= len(series["their_rot"] or [1])
    key, other = ("vr", "vl") if vs_right else ("vl", "vr")

    # Pitchers: series rotation first, then the best relievers by FIP, a lefty guaranteed.
    rot_names = series["options"][0]["order"][:3] if series["options"] else []
    rot = [p for p in P.values() if p["name"] in rot_names]
    rest = sorted([p for p in P.values() if p["name"] not in rot_names],
                  key=lambda p: (p["fip"] if p["fip"] is not None else 9.0))
    staff = [dict(p, job="Starter") for p in rot]
    lefty = next((p for p in rest if p["throws"] == "L"), None)
    if lefty:
        staff.append(dict(lefty, job="Lefty specialist"))
    for p in rest:
        if len(staff) >= N_PITCHERS:
            break
        if lefty and p["pid"] == lefty["pid"]:
            continue
        job = "Long man / 4th starter" if p["role"] == 11 and not any(s["job"].startswith("Long") for s in staff) else "Reliever"
        staff.append(dict(p, job=job))
    staff_ids = {p["pid"] for p in staff}

    # Hitters
    hit = [dict(r) for r in conn.execute(f"""
        SELECT p.player_id, p.name, p.age, p.pos, r.speed, {', '.join('r.' + c for c in _POS_COLS.values())},
               p.injury_is_injured, p.injury_left
        FROM players p JOIN latest_ratings r ON r.player_id=p.player_id
        WHERE p.team_id=? AND p.level='1' AND COALESCE(p.role,0) NOT IN (11,12,13)
    """, (team_id,)).fetchall()]
    for h in hit:
        c = comps.get(h["player_id"]) or {}
        h["vr"], h["vl"] = c.get("vr") or 0, c.get("vl") or 0
        h["out"] = bool(h["injury_is_injured"] and (h["injury_left"] or 0) > 10)
    healthy = [h for h in hit if not h["out"]]
    # Lineup: your own depth-chart choice at each position for this hand (everyday
    # starter, else the platoon side), skipping anyone hurt; otherwise the best
    # healthy bat whose listed position (or a 50+ rating) fits.
    platoon_role = "platoon_vr" if vs_right else "platoon_vl"
    roles = {}
    for pos, role, pid, share in conn.execute(
            "SELECT position, role, player_id, share FROM depth_chart_roles WHERE team_id=? "
            "AND position NOT IN ('SP','RP')", (team_id,)).fetchall():
        roles.setdefault(pos, []).append((role, pid, share))
    by_id = {h["player_id"]: h for h in healthy}
    _POSNUM = {"C": 2, "1B": 3, "2B": 4, "3B": 5, "SS": 6, "LF": 7, "CF": 8, "RF": 9}
    lineup, used = [], set()
    for pos in _FILL_ORDER:
        pick, why = None, ""
        rs = roles.get(pos, [])
        for want in ("starter", platoon_role):
            opts = sorted([(sh if sh is not None else 1.0, pid) for r, pid, sh in rs
                           if r == want and pid in by_id and pid not in used], reverse=True)
            if opts:
                pick, why = by_id[opts[0][1]], "your depth chart"
                break
        if pick is None:
            col = _POS_COLS[pos]
            cands = [h for h in healthy if h["player_id"] not in used
                     and (h["pos"] == _POSNUM[pos] or (h.get(col) or 0) >= 50)]
            if cands:
                pick, why = max(cands, key=lambda h: h[key]), "best healthy fit (depth-chart choice unavailable)"
        if pick:
            lineup.append(dict(pick, slot=pos, job=f"Starter vs {'RHP' if vs_right else 'LHP'} — {why}"))
            used.add(pick["player_id"])
    bench = []

    def add(h, job):
        if h and h["player_id"] not in used and len(lineup) + len(bench) < ROSTER_SIZE - len(staff):
            bench.append(dict(h, slot="", job=job))
            used.add(h["player_id"])

    pool = lambda: [h for h in healthy if h["player_id"] not in used]
    add(max((h for h in pool() if (h.get("c") or 0) >= 45), key=lambda h: h["c"], default=None), "Backup catcher")
    add(max(pool(), key=lambda h: h[other], default=None), f"Platoon bat vs {'LHP' if vs_right else 'RHP'}")
    add(max(pool(), key=lambda h: h.get("speed") or 0, default=None), "Pinch-runner")
    add(max(pool(), key=lambda h: max((h.get(c) or 0) for c in ("ss", "cf", "second_b")), default=None), "Defensive sub")
    util = max(pool(), key=lambda h: min((h.get(c) or 0) for c in ("second_b", "third_b", "ss")), default=None)
    if util and min((util.get(c) or 0) for c in ("second_b", "third_b", "ss")) >= 40:
        add(util, "Utility infielder")
    for h in sorted(pool(), key=lambda h: -h[key]):
        add(h, "Pinch-hitter")
    left_off = [h for h in hit if h["player_id"] not in used]
    left_off_p = [p for p in P.values() if p["pid"] not in staff_ids]
    return {"vs": "RHP" if vs_right else "LHP", "staff": staff, "lineup": lineup, "bench": bench,
            "left_off": sorted(left_off, key=lambda h: -h[key]), "left_off_p": left_off_p,
            "size": len(staff) + len(lineup) + len(bench)}
