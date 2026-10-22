"""Front Office: revenue & payroll efficiency, a three-year org gap map,
pitcher workload monitor, and a platoon (handedness) audit.

Revenue
    From the newest StatsPlus finances export in ~/Downloads (the league
    finances table: payroll, budget, revenue lines, cash for trades). Revenue is
    season-to-date, so the per-win figure is a to-date regression across the
    league's clubs, not a full-season number.

Gap map
    The depth chart's three-season projection (current + two more years: aging,
    contracts, prospects arriving) per position, against what an average regular
    (composite 50) produces at that position.

Workload
    Each pitcher's innings pace to the end of the schedule against his own
    history (last season, career high), with starts and innings per start.

Platoon audit
    Plate appearances against right- and left-handed pitching versus the
    hitter's own vR/vL composite, with the best roster alternative at the same
    position, priced in runs with the run-space composite scale.
"""

from __future__ import annotations

import csv
from pathlib import Path

from web_league_context import get_db, get_cfg

SLOT_BUCKET = {"C": "C", "1B": "1B", "2B": "2B", "3B": "3B", "SS": "SS",
               "LF": "COF", "CF": "CF", "RF": "COF"}
POS_NAME = {2: "C", 3: "1B", 4: "2B", 5: "3B", 6: "SS", 7: "LF", 8: "CF", 9: "RF"}
POS_RATING_COL = {"C": "c", "1B": "first_b", "2B": "second_b", "3B": "third_b",
                  "SS": "ss", "LF": "lf", "CF": "cf", "RF": "rf"}


def _num(v):
    try:
        return float(str(v).replace("$", "").replace(",", "").strip())
    except (TypeError, ValueError):
        return None


# ── 3. revenue & efficiency ──────────────────────────────────────────────────

def _finance_table():
    files = sorted(Path.home().joinpath("Downloads").glob("statsplus*.csv"),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    for f in files[:15]:
        try:
            rows = list(csv.DictReader(f.open(encoding="utf-8-sig")))
        except (OSError, UnicodeDecodeError):
            continue
        if rows and "GateRevenue" in rows[0] and "Payroll" in rows[0]:
            return [r for r in rows if (r.get("Team") or "") not in ("Average", "")], f
    return None, None


def _ols(xs, ys):
    n = len(xs)
    if n < 3:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    if not sxx or not syy:
        return None
    b = sxy / sxx
    return {"slope": b, "intercept": my - b * mx, "r2": sxy * sxy / (sxx * syy)}


def revenue_efficiency(conn, team_id, year):
    from statsplusplus.config.league_config import dollars_per_war
    cfg = get_cfg()
    rows, f = _finance_table()
    if not rows:
        return {"available": False}
    by_abbr = {v.upper(): k for k, v in (cfg.team_abbr_map or {}).items()}
    stand = {r[0]: (r[1], r[2]) for r in conn.execute("SELECT team_id, w, l FROM standings").fetchall()}
    war = {}
    for tid, w in conn.execute("SELECT team_id, SUM(war) FROM mlb_batting_stats WHERE year=? AND split_id=1 GROUP BY team_id", (year,)):
        war[tid] = war.get(tid, 0) + (w or 0)
    for tid, w in conn.execute("SELECT team_id, SUM(war) FROM mlb_pitching_stats WHERE year=? AND split_id=1 GROUP BY team_id", (year,)):
        war[tid] = war.get(tid, 0) + (w or 0)
    home = dict(conn.execute("SELECT home_team, COUNT(*) FROM games WHERE played=1 AND game_type=0 AND date>=? GROUP BY home_team",
                             (f"{year}-01-01",)).fetchall())
    teams = []
    for r in rows:
        tid = by_abbr.get((r.get("Team") or "").upper())
        if tid is None:
            continue
        w, l = stand.get(tid, (None, None))
        payroll, rev = _num(r.get("Payroll")), _num(r.get("TotalRevenue"))
        gate = _num(r.get("GateRevenue"))
        t = {"tid": tid, "abbr": r["Team"], "name": cfg.team_names_map.get(tid, r["Team"]),
             "payroll": payroll, "budget": _num(r.get("Budget")), "revenue": rev, "gate": gate,
             "season_tickets": _num(r.get("SeasonTickets")), "playoff": _num(r.get("PlayoffRevenue")),
             "merch": _num(r.get("MerchRevenue")), "media": _num(r.get("MediaRevenue")),
             "cash": _num(r.get("Cash(for Trades)")), "w": w, "l": l, "war": war.get(tid),
             "home_games": home.get(tid, 0), "mine": tid == team_id}
        t["margin"] = (rev or 0) - (payroll or 0)
        t["pay_per_win"] = payroll / w if payroll and w else None
        t["pay_per_war"] = payroll / t["war"] if payroll and t["war"] and t["war"] > 0 else None
        t["gate_per_game"] = gate / t["home_games"] if gate and t["home_games"] else None
        t["wpct"] = w / (w + l) if w is not None and (w + l) else None
        teams.append(t)
    # Revenue-to-date per win, across the league (wins also to date).
    fit_rev = _ols([t["w"] for t in teams if t["w"] is not None], [t["revenue"] for t in teams if t["w"] is not None])
    fit_gate = _ols([t["wpct"] for t in teams if t["wpct"] is not None and t["gate_per_game"]],
                    [t["gate_per_game"] for t in teams if t["wpct"] is not None and t["gate_per_game"]])
    # The finances file isn't tagged with a league: only trust it when its clubs
    # are this league's clubs (abbreviations overlap across leagues).
    if len(teams) < 0.9 * max(len(rows), len(cfg.mlb_team_ids or [])):
        return {"available": False, "reason": f"{f.name} looks like another league's finances"}
    teams.sort(key=lambda t: -(t["w"] or 0))
    for rank, t in enumerate(sorted(teams, key=lambda t: (t["pay_per_win"] or 9e18)), 1):
        t["eff_rank"] = rank
    dpw = dollars_per_war(cfg.league_dir)
    mine = next((t for t in teams if t["mine"]), None)
    return {"available": True, "file": f.name, "teams": teams, "fit_rev": fit_rev, "fit_gate": fit_gate,
            "dpw": dpw, "mine": mine,
            # one extra win = +1/154 win%; x gate/game slope x a full season of home games
            "gate_per_win": (fit_gate["slope"] / 154 * 77 if fit_gate else None)}


# ── 4. org gap map ───────────────────────────────────────────────────────────

def gap_map(team_id, weights):
    from statsplusplus.evaluation.war import peak_war_from_score
    import queries
    dc = queries.get_depth_chart(team_id)
    years = dc["years"]
    by = dc["by_year"]
    rows = []
    for pos in ["C", "1B", "2B", "3B", "SS", "LF", "CF", "RF", "SP", "RP"]:
        if pos == "SP":
            bench = 4 * peak_war_from_score(50, "SP", weights)
        elif pos == "RP":
            bench = 5 * peak_war_from_score(50, "RP", weights)
        else:
            bench = peak_war_from_score(50, SLOT_BUCKET[pos], weights)
        cells = []
        for y in years:
            yd = by[y] if isinstance(by, dict) else by[years.index(y)]
            war = (yd.get("pos_war") or {}).get(pos)
            if pos in ("SP", "RP"):
                lst = yd.get(pos.lower()) or []
            else:
                lst = (yd.get("positions") or {}).get(pos) or []
            top = sorted(lst, key=lambda p: -(p.get("pt_pct") or p.get("ip") or 0))[:2]
            status = ("hole" if war is not None and war < 0.5 * bench else
                      "thin" if war is not None and war < bench else "ok")
            cells.append({"year": y, "war": war, "status": status,
                          "who": [{"name": p.get("name"), "age": p.get("age"), "level": p.get("level")} for p in top]})
        rows.append({"pos": pos, "bench": round(bench, 1), "cells": cells})
    return {"years": years, "rows": rows}


# ── 7. pitcher workload ──────────────────────────────────────────────────────

def workload(conn, team_id, year, gps):
    played = conn.execute("SELECT w + l + COALESCE(t,0) FROM standings WHERE team_id=?", (team_id,)).fetchone()
    played = played[0] if played and played[0] else None
    out = []
    for r in conn.execute("""
        SELECT p.player_id, p.name, p.age, p.role, r.stm, r.prone,
               p.injury_is_injured, p.injury_left
        FROM players p JOIN latest_ratings r ON r.player_id=p.player_id
        WHERE p.team_id=? AND p.level='1' AND p.role IN (11,12,13)
    """, (team_id,)).fetchall():
        hist = conn.execute(
            "SELECT year, SUM(outs)/3.0, SUM(gs), SUM(g) FROM mlb_pitching_stats WHERE player_id=? AND split_id=1 GROUP BY year ORDER BY year",
            (r["player_id"],)).fetchall()
        cur = next((h for h in hist if h[0] == year), None)
        if not cur or not cur[1]:
            continue
        ip, gs, g = cur[1], cur[2] or 0, cur[3] or 0
        prior = [h for h in hist if h[0] < year and h[1]]
        last = prior[-1][1] if prior and prior[-1][0] == year - 1 else None
        high = max((h[1] for h in prior), default=None)
        pace = ip / played * gps if played else None
        flags = []
        if pace and high and pace > high * 1.10:
            flags.append(f"pace {pace:.0f} IP beats his career high ({high:.0f}) by {pace / high - 1:.0%}")
        if pace and last and pace > last * 1.25 and pace - last > 30:
            flags.append(f"{pace - last:.0f} IP more than last year")
        if r["age"] and r["age"] <= 24 and pace and pace > 220:
            flags.append("young arm on a heavy load")
        if gs >= 5 and ip / gs >= 7.6:
            flags.append(f"{ip / gs:.1f} IP per start")
        if r["role"] in (12, 13) and played and g / played * gps > 70:
            flags.append(f"on pace for {g / played * gps:.0f} appearances")
        if r["injury_is_injured"]:
            flags.append("currently injured")
        risk = "High" if len(flags) >= 2 or (flags and "career high" in flags[0]) else "Watch" if flags else "OK"
        out.append({"pid": r["player_id"], "name": r["name"], "age": r["age"],
                    "role": {11: "SP", 12: "RP", 13: "CL"}.get(r["role"], "P"),
                    "ip": ip, "gs": gs, "g": g, "pace": pace, "last": last, "high": high,
                    "stm": r["stm"], "ip_per_gs": ip / gs if gs else None, "flags": flags, "risk": risk})
    order = {"High": 0, "Watch": 1, "OK": 2}
    out.sort(key=lambda x: (order[x["risk"]], -(x["pace"] or 0)))
    return {"played": played, "rows": out}


# ── 8. platoon audit ─────────────────────────────────────────────────────────

def platoon_audit(conn, team_id, year, league_dir):
    import json
    from team_queries import _org_vr_vl_composites
    comps = _org_vr_vl_composites(conn, team_id)
    try:
        mapping = json.loads((Path(league_dir) / "config" / "tool_weights.json").read_text())["run_space"]["comp_mapping"]
        runs_per_pt_600 = 1.0 / mapping["slope"]
    except (OSError, KeyError, ValueError, ZeroDivisionError):
        runs_per_pt_600 = 2.3
    hitters = [dict(r) for r in conn.execute("""
        SELECT p.player_id, p.name, p.pos, p.bats_hand,
               r.c, r.first_b, r.second_b, r.third_b, r.ss, r.lf, r.cf, r.rf,
               SUM(CASE WHEN b.split_id=2 THEN b.pa END) AS pa_vl,
               SUM(CASE WHEN b.split_id=3 THEN b.pa END) AS pa_vr
        FROM (SELECT player_id, name, pos, NULL AS bats_hand FROM players
              WHERE team_id=? AND level='1' AND COALESCE(role,0) NOT IN (11,12,13)) p
        JOIN latest_ratings r ON r.player_id=p.player_id
        LEFT JOIN mlb_batting_stats b ON b.player_id=p.player_id AND b.year=? AND b.team_id=?
        GROUP BY p.player_id
    """, (team_id, year, team_id)).fetchall()]
    for h in hitters:
        c = comps.get(h["player_id"]) or {}
        h["vr"], h["vl"] = c.get("vr"), c.get("vl")
        h["pa_vl"], h["pa_vr"] = h["pa_vl"] or 0, h["pa_vr"] or 0
        h["pos_name"] = POS_NAME.get(h["pos"], "?")
    tot_vl = sum(h["pa_vl"] for h in hitters) or 1
    tot_vr = sum(h["pa_vr"] for h in hitters) or 1
    # A hitter already playing regularly against a hand isn't a free alternative
    # there (moving him opens a hole elsewhere); only bench-level bats count.
    max_vl = max((h["pa_vl"] for h in hitters), default=0)
    max_vr = max((h["pa_vr"] for h in hitters), default=0)
    rows, total_runs = [], 0.0
    for h in hitters:
        if not (h["pa_vl"] + h["pa_vr"]):
            continue
        pos = h["pos_name"]
        col = POS_RATING_COL.get(pos)
        alts = [a for a in hitters if a is not h and col and (a.get(col) or 0) >= 45]
        notes, runs = [], 0.0
        for side, pa, comp in (("LHP", h["pa_vl"], h["vl"]), ("RHP", h["pa_vr"], h["vr"])):
            if not pa or comp is None:
                continue
            if pa < 30:
                continue
            key = "vl" if side == "LHP" else "vr"
            side_pa, side_max = ("pa_vl", max_vl) if side == "LHP" else ("pa_vr", max_vr)
            free = [a for a in alts if a[side_pa] < 0.4 * side_max]
            best = max(free, key=lambda a: a.get(key) or 0, default=None)
            if best and (best.get(key) or 0) - comp >= 5:
                lost = pa * ((best[key] - comp) * runs_per_pt_600 / 600.0)
                runs += lost
                notes.append({"side": side, "pa": pa, "comp": comp, "alt": best["name"],
                              "alt_comp": best[key], "runs": lost})
        total_runs += runs
        rows.append({"pid": h["player_id"], "name": h["name"], "pos": pos,
                     "vr": h["vr"], "vl": h["vl"], "pa_vr": h["pa_vr"], "pa_vl": h["pa_vl"],
                     "share_vr": h["pa_vr"] / tot_vr, "share_vl": h["pa_vl"] / tot_vl,
                     "split": (h["vr"] or 0) - (h["vl"] or 0), "notes": notes, "runs": runs})
    rows.sort(key=lambda x: -x["runs"])
    return {"rows": rows, "total_runs": total_runs, "runs_per_pt_600": runs_per_pt_600}


def build(team_id, league_dir):
    from statsplusplus.config.league_config import games_per_season
    from statsplusplus.evaluation.constants import load_model_weights
    conn = get_db()
    gps = games_per_season(league_dir)
    year = conn.execute("SELECT MAX(year) FROM mlb_batting_stats").fetchone()[0]
    weights = load_model_weights(Path(league_dir))
    return {"year": year, "gps": gps,
            "revenue": revenue_efficiency(conn, team_id, year),
            "gaps": gap_map(team_id, weights),
            "workload": workload(conn, team_id, year, gps),
            "platoon": platoon_audit(conn, team_id, year, league_dir)}
