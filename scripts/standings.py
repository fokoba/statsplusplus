#!/usr/bin/env python3
"""
standings.py — League-wide standings via pythagorean expectation.
Usage: python3 scripts/standings.py [--year 2033] [--refresh]

Derives W/L from team RS/RA using pythagorean formula (exponent 1.83).
Games estimated from pitching outs / 27.
Reads from DB (team_batting_stats + team_pitching_stats).
Use --refresh to pull fresh data from the API first.
"""

import argparse, json, os, sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "scripts"))
import db as _db
from league_config import config as _cfg

PYTH_EXP = _cfg.pyth_exp


def _standings_from_db(year):
    conn = _db.get_conn()
    conn.row_factory = None

    bat = conn.execute(
        "SELECT team_id, name, r FROM team_batting_stats WHERE year=? AND split_id=1",
        (year,),
    ).fetchall()
    pit = conn.execute(
        "SELECT team_id, r, ip FROM team_pitching_stats WHERE year=? AND split_id=1",
        (year,),
    ).fetchall()
    conn.close()
    if not bat or not pit:
        return None

    rs_map = {r[0]: (r[1], r[2]) for r in bat}   # tid -> (name, RS)
    ra_map = {r[0]: (r[1], r[2]) for r in pit}    # tid -> (RA, IP)
    return _build_rows(rs_map, ra_map)


def _standings_from_api(year):
    sys.path.insert(0, BASE)
    from statsplus import client

    tb = client.get_team_batting_stats(year=year, split=1)
    tp = client.get_team_pitching_stats(year=year, split=1)
    if not tb or not tp:
        return None

    rs_map = {t["tid"]: (t["name"], t["r"]) for t in tb if t.get("split_id") == 1}
    ra_map = {t["tid"]: (t["r"], t["ip"]) for t in tp if t.get("split_id") == 1}
    return _build_rows(rs_map, ra_map)


def _build_rows(rs_map, ra_map):
    rows = []
    for tid, (name, rs) in rs_map.items():
        if tid not in ra_map:
            continue
        ra, ip = ra_map[tid]
        g = round(ip / 9)
        if g == 0 or rs + ra == 0:
            continue
        pyth = rs**PYTH_EXP / (rs**PYTH_EXP + ra**PYTH_EXP)
        w = round(pyth * g, 1)
        l = round(g - w, 1)
        rows.append({
            "tid": tid, "name": name, "g": g,
            "w": w, "l": l, "pct": pyth,
            "rs": rs, "ra": ra, "diff": rs - ra,
        })
    rows.sort(key=lambda x: x["pct"], reverse=True)
    return rows


def actual_record(team_id, year):
    """Return actual W-L from games table for a team in a given season.

    NOTE: In the games table, runs0 = AWAY team runs, runs1 = HOME team runs.
    Home team wins when runs1 > runs0. Away team wins when runs0 > runs1.
    """
    conn = _db.get_conn()
    conn.row_factory = None
    row = conn.execute("""
        SELECT
            SUM(CASE WHEN (home_team=? AND runs1>runs0) OR (away_team=? AND runs0>runs1) THEN 1 ELSE 0 END) as w,
            SUM(CASE WHEN (home_team=? AND runs1<runs0) OR (away_team=? AND runs0<runs1) THEN 1 ELSE 0 END) as l
        FROM games
        WHERE played=1 AND date >= ? AND (home_team=? OR away_team=?)
    """, (team_id, team_id, team_id, team_id, f"{year}-01-01", team_id, team_id)).fetchone()
    conn.close()
    w, l = (row[0] or 0), (row[1] or 0)
    return w, l


def print_standings(rows, my_tid=None):
    if my_tid is None:
        my_tid = _cfg.my_team_id
    leader_pct = rows[0]["pct"] if rows else 0.5

    # Load real standings from DB if available
    real_standings = {}
    try:
        conn = _db.get_conn()
        for r in conn.execute("SELECT team_id, w, l FROM standings").fetchall():
            real_standings[r[0]] = (r[1], r[2])
        conn.close()
    except Exception:
        pass

    has_real = bool(real_standings)
    if has_real:
        hdr = f"{'#':>2}  {'Team':<24} {'pyW':>4} {'pyL':>4} {'Pct':>6} {'aW':>4} {'aL':>4} {'Δ':>3} {'GB':>5} {'RS':>4} {'RA':>4} {'Diff':>5}"
    else:
        hdr = f"{'#':>2}  {'Team':<28} {'W':>5} {'L':>5} {'Pct':>6} {'GB':>5} {'RS':>4} {'RA':>4} {'Diff':>5}"
    print(hdr)
    print("-" * len(hdr))
    leader_w = rows[0]["w"] if rows else 0
    leader_l = rows[0]["l"] if rows else 0
    for i, r in enumerate(rows, 1):
        gb = ((leader_w - leader_l) - (r["w"] - r["l"])) / 2
        gb_str = "-" if gb < 0.25 else f"{gb:.1f}"
        diff_str = f"+{r['diff']}" if r["diff"] > 0 else str(r["diff"])
        marker = " ◄" if r["tid"] == my_tid else ""
        if has_real and r["tid"] in real_standings:
            aw, al = real_standings[r["tid"]]
            delta = aw - int(round(r["w"]))
            delta_str = f"{delta:+d}" if delta != 0 else " 0"
            print(f"{i:>2}  {r['name']:<24} {r['w']:>4} {r['l']:>4} {r['pct']:>6.3f} {aw:>4} {al:>4} {delta_str:>3} {gb_str:>5} {r['rs']:>4} {r['ra']:>4} {diff_str:>5}{marker}")
        elif has_real:
            print(f"{i:>2}  {r['name']:<24} {r['w']:>4} {r['l']:>4} {r['pct']:>6.3f} {'?':>4} {'?':>4} {'':>3} {gb_str:>5} {r['rs']:>4} {r['ra']:>4} {diff_str:>5}{marker}")
        else:
            print(f"{i:>2}  {r['name']:<28} {r['w']:>5} {r['l']:>5} {r['pct']:>6.3f} {gb_str:>5} {r['rs']:>4} {r['ra']:>4} {diff_str:>5}{marker}")


def all_actual_records(year):
    """Return dict of team_id -> (w, l) for all teams from games table."""
    conn = _db.get_conn()
    conn.row_factory = None
    rows = conn.execute("""
        SELECT home_team, away_team, runs0, runs1
        FROM games
        WHERE played=1 AND date >= ? AND game_type=0
    """, (f"{year}-01-01",)).fetchall()
    conn.close()
    records = {}
    for home, away, r0, r1 in rows:
        # runs0 = away runs, runs1 = home runs
        if r1 > r0:
            records[home] = (records.get(home, (0, 0))[0] + 1, records.get(home, (0, 0))[1])
            records[away] = (records.get(away, (0, 0))[0], records.get(away, (0, 0))[1] + 1)
        elif r0 > r1:
            records[away] = (records.get(away, (0, 0))[0] + 1, records.get(away, (0, 0))[1])
            records[home] = (records.get(home, (0, 0))[0], records.get(home, (0, 0))[1] + 1)
    return records


def league_standings_actual(year, league_name=None):
    """
    Return actual-record standings for a league (AL/NL) or all teams.
    Each entry: {tid, abbr, w, l, pct, division}.
    Sorted by win pct descending.
    """
    records = all_actual_records(year)
    settings = _cfg.settings
    leagues = settings.get("leagues", [])

    results = []
    for lg in leagues:
        if league_name and lg["name"] != league_name and lg["short"] != league_name:
            continue
        for div_name, team_ids in lg["divisions"].items():
            for tid in team_ids:
                w, l = records.get(tid, (0, 0))
                if w + l == 0:
                    continue
                abbr = _cfg.team_abbr(tid)
                results.append({
                    "tid": tid, "abbr": abbr, "w": w, "l": l,
                    "pct": w / (w + l), "division": div_name,
                    "league": lg["name"],
                })

    # If no league filter matched, include all teams
    if not results and not league_name:
        for tid, (w, l) in records.items():
            if w + l == 0:
                continue
            abbr = _cfg.team_abbr(tid)
            results.append({
                "tid": tid, "abbr": abbr, "w": w, "l": l,
                "pct": w / (w + l), "division": "?", "league": "?",
            })

    results.sort(key=lambda x: -x["pct"])
    return results


def playoff_picture(year, team_id):
    """
    Show a team's league standings with division leaders and wild card race.
    Returns a formatted string.
    """
    settings = _cfg.settings
    leagues = settings.get("leagues", [])
    wc_per_league = settings.get("wild_cards_per_league", 3)

    # Find which league this team is in
    team_league = None
    for lg in leagues:
        for div_name, team_ids in lg["divisions"].items():
            if team_id in team_ids:
                team_league = lg["name"]
                break
        if team_league:
            break

    if not team_league:
        return "Team not found in league structure."

    standings = league_standings_actual(year, team_league)
    if not standings:
        return "No games played yet."

    # Identify division leaders
    div_leaders = {}
    for entry in standings:
        div = entry["division"]
        if div not in div_leaders:
            div_leaders[div] = entry

    # Wild card: everyone except div leaders, sorted by pct
    div_leader_tids = {e["tid"] for e in div_leaders.values()}
    wc_teams = [e for e in standings if e["tid"] not in div_leader_tids]

    # WC cutoff
    wc_cutoff_w = wc_teams[wc_per_league - 1]["w"] if len(wc_teams) >= wc_per_league else 0
    wc_cutoff_l = wc_teams[wc_per_league - 1]["l"] if len(wc_teams) >= wc_per_league else 0

    lines = []
    lines.append(f"{team_league} Standings — Actual Record ({year})")
    lines.append("")

    # Division leaders
    lines.append("Division Leaders:")
    for div, entry in sorted(div_leaders.items()):
        marker = " ◄" if entry["tid"] == team_id else ""
        lines.append(f"  {div:<10} {entry['abbr']:<5} {entry['w']}-{entry['l']} ({entry['pct']:.3f}){marker}")

    lines.append("")
    lines.append("Wild Card Race:")
    lines.append(f"  {'#':<3} {'Team':<6} {'W-L':<8} {'Pct':<7} {'GB from WC{0}'.format(wc_per_league)}")
    lines.append(f"  {'-'*45}")
    for i, entry in enumerate(wc_teams[:wc_per_league + 5], 1):
        gb = ((wc_cutoff_w - entry["w"]) + (entry["l"] - wc_cutoff_l)) / 2
        gb_str = "-" if abs(gb) < 0.25 else f"{gb:+.1f}"
        marker = " ◄" if entry["tid"] == team_id else ""
        in_out = "IN " if i <= wc_per_league else "OUT"
        lines.append(f"  {i:<3} {entry['abbr']:<6} {entry['w']}-{entry['l']:<5} {entry['pct']:.3f}   {gb_str:>6}  [{in_out}]{marker}")
        if i == wc_per_league:
            lines.append(f"  {'─'*45}")

    return "\n".join(lines)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--year", type=int, default=None)
    ap.add_argument("--team", type=str, default=None,
                    help="Team abbreviation to focus on (shows playoff picture)")
    ap.add_argument("--refresh", action="store_true", help="Pull fresh from API")
    ap.add_argument("--actual", action="store_true",
                    help="Show actual W-L for my team alongside pythagorean")
    args = ap.parse_args()

    if args.year is None:
        args.year = _cfg.year

    # If --team is specified, show that team's actual record + playoff picture
    if args.team:
        tid = _cfg.team_id_from_abbr(args.team) if hasattr(_cfg, 'team_id_from_abbr') else None
        if tid is None:
            # Manual lookup
            abbr_map = _cfg.team_abbr_map
            tid = next((int(k) for k, v in abbr_map.items() if v.upper() == args.team.upper()), None)
        if tid is None:
            print(f"Unknown team: {args.team}")
            sys.exit(1)
        w, l = actual_record(tid, args.year)
        print(f"\n{_cfg.team_name(tid)} — Actual Record: {w}-{l} ({w/(w+l):.3f})\n")
        print(playoff_picture(args.year, tid))
        print()
        # Also show pythagorean comparison
        rows = _standings_from_db(args.year)
        if rows:
            pyth = next((r for r in rows if r["tid"] == tid), None)
            if pyth:
                delta_w = w - pyth["w"]
                print(f"Pythagorean: {pyth['w']:.1f}-{pyth['l']:.1f} (RS:{pyth['rs']} RA:{pyth['ra']} Diff:{pyth['diff']:+d})")
                print(f"Delta: {delta_w:+.1f}W vs pythagorean")
                if delta_w < -2:
                    print("  → Underperforming pythagorean — likely bullpen/luck drag")
                elif delta_w > 2:
                    print("  → Overperforming pythagorean — regression risk")
        print()
        sys.exit(0)

    rows = None
    if not args.refresh:
        rows = _standings_from_db(args.year)
    if rows is None:
        rows = _standings_from_api(args.year)

    if not rows:
        print("No data available.")
        sys.exit(1)

    print(f"\n{args.year} Standings — Pythagorean ({len(rows)} teams)\n")
    print_standings(rows)
    print()

    if args.actual:
        w, l = actual_record(_cfg.my_team_id, args.year)
        pyth = next((r for r in rows if r["tid"] == _cfg.my_team_id), None)
        if pyth:
            delta_w = w - pyth["w"]
            print(f"Actual record: {w}-{l}  |  Pythagorean: {pyth['w']}-{pyth['l']}  |  Delta: {delta_w:+.1f}W")
            if delta_w < -2:
                print("  → Underperforming pythagorean — likely bullpen/luck drag")
            elif delta_w > 2:
                print("  → Overperforming pythagorean — regression risk")
        print()
