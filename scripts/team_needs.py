#!/usr/bin/env python3
"""
team_needs.py — Positional production vs league average. Identifies upgrade priorities.

Usage:
  python3 scripts/team_needs.py                  # My team
  python3 scripts/team_needs.py --team MIN       # Any team
  python3 scripts/team_needs.py --aaa-roster     # Full AAA roster (all players, not just top prospects)

Output:
  - Hitting: each position's OPS vs league average, WAR, PA, flagged by severity
  - Pitching: rotation and bullpen ERA/WAR vs league average
  - Summary: ranked upgrade priorities
  - AAA roster (with --aaa-roster): all AAA players sorted by Ovr, including vets below FV threshold
"""

import argparse, json, os, sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "scripts"))

from statsplusplus.config.league_context import get_league_dir, get_active_league_slug
from statsplusplus.config.league_config import LeagueConfig
from statsplusplus.data.db import get_connection

league_dir = get_league_dir(get_active_league_slug())
_cfg = LeagueConfig(base_dir=league_dir)

def _get_conn():
    return get_connection(league_dir)

POS_MAP  = {2:"C", 3:"1B", 4:"2B", 5:"3B", 6:"SS", 7:"LF", 8:"CF", 9:"RF", 10:"DH"}
POS_ORDER = ["C","1B","2B","3B","SS","LF","CF","RF","DH"]

# Gap thresholds vs league average OPS
SEVERE   = -0.060   # clear upgrade needed
WEAK     = -0.030   # below average, worth monitoring
STRONG   =  0.040   # above average


def _resolve_team(arg):
    if arg is None:
        return _cfg.my_team_id
    a = arg.upper()
    for tid, abbr in _cfg.team_abbr_map.items():
        if abbr.upper() == a:
            return tid
    for tid, name in _cfg.team_names_map.items():
        if a in name.upper():
            return tid
    raise ValueError(f"Team not found: {arg}")


def analyze(team_id=None, year=None):
    team_id = team_id or _cfg.my_team_id
    year    = year or _cfg.year
    conn    = _get_conn()

    la_path = os.path.join(str(_cfg.league_dir), "config", "league_averages.json")
    la      = json.load(open(la_path))
    lg_ops  = la["batting"]["ops"]
    lg_era  = la["pitching"]["era"]
    lg_fip  = la["pitching"].get("fip", lg_era)

    # ── Hitting: primary position = most PA ──────────────────────────────────
    rows = conn.execute("""
        SELECT p.player_id, p.name, p.pos,
               COALESCE(r.ovr, r.composite_score) AS ovr,
               COALESCE(r.pot, r.ceiling_score) AS pot,
               b.avg, b.obp, b.slg, (b.obp + b.slg) as ops,
               b.war, b.pa, b.hr, b.bb, b.k,
               r.cntct_l, r.cntct_r, r.pow_l, r.pow_r, r.eye_l, r.eye_r, r.bats
        FROM players p
        JOIN latest_ratings r ON p.player_id = r.player_id
        JOIN mlb_batting_stats b ON p.player_id = b.player_id
            AND b.year = ? AND b.split_id = 1
        WHERE p.team_id = ? AND p.level = '1' AND p.role = 0
        ORDER BY b.pa DESC
    """, (year, team_id)).fetchall()

    # Assign each player to their primary position (first appearance = most PA)
    pos_slots = {}   # pos_label -> best row
    seen_pids = set()
    for r in rows:
        if r["player_id"] in seen_pids:
            continue
        seen_pids.add(r["player_id"])
        pos = POS_MAP.get(r["pos"])
        if pos and pos not in pos_slots:
            pos_slots[pos] = r

    def _platoon_note(r):
        """Flag platoon concern only when: (1) gap is large (20+ combined), AND
        (2) the weaker side is genuinely weak (below 45 on contact or power).
        A player who hits better vs one hand but is still solid vs the other is not a platoon concern.
        """
        cnt = (r["cntct_l"] or 0) - (r["cntct_r"] or 0)
        pow_ = (r["pow_l"] or 0) - (r["pow_r"] or 0)
        eye = (r["eye_l"] or 0) - (r["eye_r"] or 0)
        score = cnt + pow_ + eye  # positive = better vs LHP, negative = better vs RHP
        bats = r["bats"] or "R"

        def _weak_vs_rhp():
            return (r["cntct_r"] or 0) < 45 or (r["pow_r"] or 0) < 45

        def _weak_vs_lhp():
            return (r["cntct_l"] or 0) < 45 or (r["pow_l"] or 0) < 45

        if bats == "R" and score <= -20 and _weak_vs_rhp():
            return "R-platoon"
        if bats == "L" and score >= 20 and _weak_vs_lhp():
            return "L-platoon"
        if bats == "S":
            if score >= 20 and _weak_vs_lhp(): return "L-lean"
            if score <= -20 and _weak_vs_rhp(): return "R-lean"
        return ""

    hitting = []
    for pos in POS_ORDER:
        r = pos_slots.get(pos)
        if r is None:
            hitting.append({"pos": pos, "name": "—", "ovr": 0, "ops": None,
                            "delta": None, "war": 0, "pa": 0, "hr": 0, "flag": "EMPTY",
                            "platoon": ""})
            continue
        ops   = r["ops"] or 0
        delta = ops - lg_ops
        flag  = "SEVERE" if delta < SEVERE else ("WEAK" if delta < WEAK else
                ("STRONG" if delta > STRONG else "OK"))
        hitting.append({
            "pos": pos, "name": r["name"], "ovr": r["ovr"], "pot": r["pot"],
            "ops": ops, "delta": delta, "war": r["war"] or 0,
            "pa": r["pa"] or 0, "hr": r["hr"] or 0, "flag": flag,
            "platoon": _platoon_note(r),
        })

    # ── Pitching: rotation (role=11) and bullpen (role=12/13) ────────────────
    pit_rows = conn.execute("""
        SELECT p.player_id, p.name, p.role,
               COALESCE(r.ovr, r.composite_score) AS ovr,
               pi.era, pi.ip, pi.war, pi.k, pi.bb, pi.outs, pi.er,
               pi.gs, pi.g
        FROM players p
        JOIN latest_ratings r ON p.player_id = r.player_id
        JOIN mlb_pitching_stats pi ON p.player_id = pi.player_id
            AND pi.year = ? AND pi.split_id = 1
        WHERE p.team_id = ? AND p.level = '1' AND p.role IN (11, 12, 13)
        ORDER BY pi.ip DESC
    """, (year, team_id)).fetchall()

    seen_pids = set()
    sp_rows, rp_rows = [], []
    for r in pit_rows:
        if r["player_id"] in seen_pids:
            continue
        seen_pids.add(r["player_id"])
        if r["role"] == 11:
            sp_rows.append(r)
        else:
            rp_rows.append(r)

    def _era(rows):
        er = sum(r["er"] or 0 for r in rows)
        outs = sum(r["outs"] or 0 for r in rows)
        return (er * 27 / outs) if outs > 0 else None

    def _war(rows):
        return sum(r["war"] or 0 for r in rows)

    sp_era  = _era(sp_rows)
    rp_era  = _era(rp_rows)
    sp_war  = _war(sp_rows)
    rp_war  = _war(rp_rows)

    sp_flag = "SEVERE" if sp_era and sp_era > lg_era + 0.60 else \
              ("WEAK"   if sp_era and sp_era > lg_era + 0.25 else \
              ("STRONG" if sp_era and sp_era < lg_era - 0.40 else "OK"))
    rp_flag = "SEVERE" if rp_era and rp_era > lg_era + 0.80 else \
              ("WEAK"   if rp_era and rp_era > lg_era + 0.35 else \
              ("STRONG" if rp_era and rp_era < lg_era - 0.50 else "OK"))

    pitching = {
        "sp": {"era": sp_era, "war": sp_war, "n": len(sp_rows), "flag": sp_flag,
               "starters": [{"name": r["name"], "ovr": r["ovr"], "era": r["era"],
                              "ip": r["ip"], "war": r["war"]} for r in sp_rows[:5]]},
        "rp": {"era": rp_era, "war": rp_war, "n": len(rp_rows), "flag": rp_flag,
               "relievers": [{"name": r["name"], "ovr": r["ovr"], "era": r["era"],
                               "ip": r["ip"], "war": r["war"]} for r in rp_rows[:6]]},
        "lg_era": lg_era,
    }

    conn.close()
    return {
        "team_id": team_id,
        "team_name": _cfg.team_names_map.get(team_id, str(team_id)),
        "year": year,
        "lg_ops": lg_ops,
        "hitting": hitting,
        "pitching": pitching,
    }


def print_report(data):
    team  = data["team_name"]
    lg_ops = data["lg_ops"]
    lg_era = data["pitching"]["lg_era"]

    print(f"\n{'='*65}")
    print(f"  {team} — Positional Needs Report ({data['year']})")
    print(f"  League avg OPS: {lg_ops:.3f}  |  League avg ERA: {lg_era:.2f}")
    print(f"{'='*65}")

    # ── Hitting ──────────────────────────────────────────────────────────────
    FLAG_ICON = {"SEVERE": "🔴", "WEAK": "🟡", "OK": "✅", "STRONG": "💚", "EMPTY": "⬜"}
    print(f"\n{'Pos':<5} {'Name':<22} {'Ovr':>3}  {'OPS':>5}  {'vs Lg':>6}  {'WAR':>5}  {'PA':>4}  {'HR':>3}  Status")
    print("-" * 75)
    for h in data["hitting"]:
        icon = FLAG_ICON.get(h["flag"], "")
        ops_str  = f"{h['ops']:.3f}" if h["ops"] is not None else "  ---"
        delta_str = f"{h['delta']:+.3f}" if h["delta"] is not None else "     "
        war_str  = f"{h['war']:.1f}" if h["war"] else "  -"
        platoon  = f" [{h['platoon']}]" if h.get("platoon") else ""
        ovr_str = f"{h['ovr']:>3}" if h.get("ovr") is not None else "  -"
        print(f"{h['pos']:<5} {h['name']:<22} {ovr_str}  {ops_str}  {delta_str}  {war_str:>5}  {h['pa']:>4}  {h['hr']:>3}  {icon} {h['flag']}{platoon}")

    # ── Pitching ─────────────────────────────────────────────────────────────
    sp = data["pitching"]["sp"]
    rp = data["pitching"]["rp"]
    print(f"\n── Rotation ({sp['n']} SPs) ──")
    sp_era_str = f"{sp['era']:.2f}" if sp["era"] else "-.--"
    rp_era_str = f"{rp['era']:.2f}" if rp["era"] else "-.--"
    print(f"  Team ERA: {sp_era_str}  vs league {lg_era:.2f} ({sp['era']-lg_era:+.2f})  WAR: {sp['war']:.1f}  {FLAG_ICON.get(sp['flag'],'')} {sp['flag']}")
    for s in sp["starters"]:
        era = f"{s['era']:.2f}" if s["era"] else "-.--"
        ip  = f"{s['ip']:.0f}" if s["ip"] else "-"
        war = f"{s['war']:.1f}" if s["war"] else "-"
        print(f"    {s['name']:<22} Ovr:{s['ovr']:>2}  ERA:{era}  {ip}IP  WAR:{war}")

    print(f"\n── Bullpen ({rp['n']} RPs) ──")
    print(f"  Team ERA: {rp_era_str}  vs league {lg_era:.2f} ({rp['era']-lg_era:+.2f})  WAR: {rp['war']:.1f}  {FLAG_ICON.get(rp['flag'],'')} {rp['flag']}")
    for r in rp["relievers"]:
        era = f"{r['era']:.2f}" if r["era"] else "-.--"
        ip  = f"{r['ip']:.0f}" if r["ip"] else "-"
        war = f"{r['war']:.1f}" if r["war"] else "-"
        print(f"    {r['name']:<22} Ovr:{r['ovr']:>2}  ERA:{era}  {ip}IP  WAR:{war}")

    # ── Upgrade priorities ───────────────────────────────────────────────────
    print(f"\n── Upgrade Priorities ──")
    priorities = []
    for h in data["hitting"]:
        platoon_note = f" (platoon: {h['platoon']})" if h.get("platoon") else ""
        if h["flag"] == "SEVERE":
            priorities.append(f"🔴 {h['pos']} — {h['name']} ({h['ops']:.3f} OPS, {h['delta']:+.3f} vs lg){platoon_note}")
        elif h["flag"] == "WEAK":
            priorities.append(f"🟡 {h['pos']} — {h['name']} ({h['ops']:.3f} OPS, {h['delta']:+.3f} vs lg){platoon_note}")
        elif h["flag"] == "EMPTY":
            priorities.append(f"⬜ {h['pos']} — no starter")
    if sp["flag"] in ("SEVERE", "WEAK"):
        priorities.append(f"{'🔴' if sp['flag']=='SEVERE' else '🟡'} SP — rotation ERA {sp_era_str} ({sp['era']-lg_era:+.2f} vs lg)")
    if rp["flag"] in ("SEVERE", "WEAK"):
        priorities.append(f"{'🔴' if rp['flag']=='SEVERE' else '🟡'} RP — bullpen ERA {rp_era_str} ({rp['era']-lg_era:+.2f} vs lg)")

    if priorities:
        for p in priorities:
            print(f"  {p}")
    else:
        print("  No significant weaknesses detected.")
    print()


def aaa_roster(team_id=None):
    team_id = team_id or _cfg.my_team_id
    # Find AAA level key from level_map (value == 'AAA')
    aaa_level = next((k for k, v in _cfg.level_map.items() if v == "AAA"), "2")
    conn = _get_conn()
    pos_map = {2:"C", 3:"1B", 4:"2B", 5:"3B", 6:"SS", 7:"LF", 8:"CF", 9:"RF", 10:"DH", 1:"P"}
    rows = conn.execute("""
        SELECT p.player_id, p.name, p.age, p.pos,
               COALESCE(r.ovr, r.composite_score) AS ovr,
               COALESCE(r.pot, r.ceiling_score) AS pot,
               r.cf, t.name as team_name
        FROM players p
        JOIN latest_ratings r ON p.player_id = r.player_id
        JOIN teams t ON p.team_id = t.team_id
        WHERE p.parent_team_id = ? AND p.level = ?
        ORDER BY COALESCE(r.ovr, r.composite_score) DESC
    """, (team_id, aaa_level)).fetchall()
    conn.close()
    team_name = _cfg.team_names_map.get(team_id, str(team_id))
    print(f"\n{team_name} — Full AAA Roster\n")
    print(f"{'Name':<25} {'Age':>3}  {'Pos':<4} {'Ovr':>3}  {'Pot':>3}  {'CF':>4}")
    print("-" * 50)
    for r in rows:
        pos = pos_map.get(r["pos"], "?")
        cf  = f"{r['cf']:>4}" if r["cf"] else "   -"
        print(f"{r['name']:<25} {r['age']:>3}  {pos:<4} {r['ovr']:>3}  {r['pot']:>3}  {cf}")
    print()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Team positional needs vs league average")
    parser.add_argument("--team", default=None, help="Team abbreviation (default: my team)")
    parser.add_argument("--year", type=int, default=None)
    parser.add_argument("--aaa-roster", action="store_true", help="Print full AAA roster sorted by Ovr")
    args = parser.parse_args()

    try:
        team_id = _resolve_team(args.team)
    except ValueError as e:
        print(e); sys.exit(1)

    if args.aaa_roster:
        data = analyze(team_id=team_id, year=args.year)
        print_report(data)
        aaa_roster(team_id=team_id)
    else:
        data = analyze(team_id=team_id, year=args.year)
        print_report(data)
