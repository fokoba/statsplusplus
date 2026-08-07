#!/usr/bin/env python3
"""
prospect_query.py — League-wide prospect rankings and farm system comparisons.

Usage:
  python3 scripts/prospect_query.py top [--n 100] [--bucket SP] [--age-max 22] [--age-min 18] [--level A] [--fv-min 50] [--surplus-min 10] [--exclude-org Anaheim]
  python3 scripts/prospect_query.py systems [--n 30]
  python3 scripts/prospect_query.py team <team_name>

Filters (top command):
  --bucket       Position bucket (SP, RP, C, SS, 2B, 3B, 1B, CF, COF)
  --age-max      Maximum age
  --age-min      Minimum age
  --fv-min       Minimum FV grade
  --level        Level filter (MLB, AAA, AA, A, Short-A, Rookie, International)
  --surplus-min  Minimum prospect surplus in $M
  --exclude-org  Exclude org by name (partial match)
"""

import argparse, os, sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "scripts"))

from statsplusplus.config.league_context import get_league_dir, get_active_league_slug
from statsplusplus.config.league_config import LeagueConfig
from statsplusplus.data.db import get_connection

league_dir = get_league_dir(get_active_league_slug())
_cfg = LeagueConfig(base_dir=league_dir)

def _get_conn():
    return get_connection(league_dir)

EMLB_FILTER = """
    p.parent_team_id IN (
        SELECT DISTINCT team_id FROM players WHERE level='1'
    )
"""


def get_game_date(conn):
    return _cfg.game_date


def _fmt_surplus(val):
    if val is None:
        return "—"
    m = val / 1_000_000
    return f"+{m:.1f}M" if m >= 0 else f"{m:.1f}M"


def cmd_top(args):
    conn = _get_conn()
    game_date = get_game_date(conn)

    where = ["pf.eval_date=?", "p.level != '1'", EMLB_FILTER]
    params = [game_date]

    if args.bucket:
        where.append("pf.bucket=?")
        params.append(args.bucket.upper())
    if args.age_max:
        where.append("p.age <= ?")
        params.append(args.age_max)
    if args.fv_min:
        where.append("pf.fv >= ?")
        params.append(args.fv_min)
    if args.age_min:
        where.append("p.age >= ?")
        params.append(args.age_min)
    if args.level:
        where.append("LOWER(pf.level)=LOWER(?)")
        params.append(args.level)
    if args.surplus_min:
        where.append("pf.prospect_surplus >= ?")
        params.append(int(args.surplus_min * 1_000_000))
    if args.exclude_org:
        where.append("LOWER(t.name) NOT LIKE LOWER(?)")
        params.append(f"%{args.exclude_org}%")

    order = "pf.prospect_surplus DESC" if args.sort == "surplus" else "fv_sort DESC, pf.prospect_surplus DESC"

    rows = conn.execute(f"""
        SELECT pf.fv_str, pf.fv, pf.level, pf.bucket, p.name, p.age,
               t.name as team, pf.prospect_surplus,
               pf.fv + CASE WHEN pf.fv_str LIKE '%+' THEN 2.5 ELSE 0 END as fv_sort
        FROM prospect_fv pf
        JOIN players p ON pf.player_id = p.player_id
        LEFT JOIN teams t ON p.parent_team_id = t.team_id
        WHERE {' AND '.join(where)}
        ORDER BY {order}
        LIMIT ?
    """, params + [args.n]).fetchall()

    label = "by Surplus" if args.sort == "surplus" else "by FV"
    print(f"\nTop {args.n} Prospects ({label}) — {game_date}\n")
    print(f"{'#':<4} {'FV':<5} {'Surplus':>9}  {'Level':<14} {'Pos':<4} {'Age':<4} {'Name':<25} Team")
    print("-" * 90)
    for i, r in enumerate(rows, 1):
        print(f"{i:<4} {r['fv_str']:<5} {_fmt_surplus(r['prospect_surplus']):>9}  {r['level']:<14} {r['bucket']:<4} {r['age']:<4} {r['name']:<25} {r['team'] or '—'}")
    conn.close()


def cmd_systems(args):
    conn = _get_conn()
    game_date = get_game_date(conn)

    rows = conn.execute(f"""
        SELECT p.parent_team_id as tid, t.name as team,
               SUM(CASE WHEN pf.fv >= 60 THEN 1 ELSE 0 END) as fv60,
               SUM(CASE WHEN pf.fv >= 55 AND pf.fv < 60 THEN 1 ELSE 0 END) as fv55,
               SUM(CASE WHEN pf.fv >= 50 AND pf.fv < 55 THEN 1 ELSE 0 END) as fv50,
               SUM(CASE WHEN pf.fv >= 45 AND pf.fv < 50 THEN 1 ELSE 0 END) as fv45,
               MAX(pf.fv) as top_fv,
               SUM(COALESCE(CASE WHEN pf.fv >= 40 THEN pf.prospect_surplus ELSE 0 END, 0)) as total_surplus
        FROM prospect_fv pf
        JOIN players p ON pf.player_id = p.player_id
        JOIN teams t ON p.parent_team_id = t.team_id
        WHERE pf.eval_date=? AND p.level != '1' AND {EMLB_FILTER}
        GROUP BY p.parent_team_id, t.name
        ORDER BY total_surplus DESC
    """, (game_date,)).fetchall()

    # Find my team's rank
    my_tid = _cfg.my_team_id
    my_rank = None
    my_row = None
    for i, r in enumerate(rows, 1):
        if r['tid'] == my_tid:
            my_rank = i
            my_row = r
            break

    display = rows[:args.n]
    my_in_display = any(r['tid'] == my_tid for r in display)

    print(f"\nFarm System Rankings — {game_date}\n")
    print(f"{'#':<4} {'Surplus':>10}  {'60+':<5} {'55':<5} {'50':<5} {'45':<5} {'Best':>5}  Team")
    print("-" * 65)
    for i, r in enumerate(display, 1):
        surplus = _fmt_surplus(r['total_surplus'])
        print(f"{i:<4} {surplus:>10}  {r['fv60']:<5} {r['fv55']:<5} {r['fv50']:<5} {r['fv45']:<5} {r['top_fv']:>5}  {r['team']}")

    if not my_in_display and my_row:
        print(f"{'':─<65}")
        r = my_row
        surplus = _fmt_surplus(r['total_surplus'])
        print(f"{my_rank:<4} {surplus:>10}  {r['fv60']:<5} {r['fv55']:<5} {r['fv50']:<5} {r['fv45']:<5} {r['top_fv']:>5}  {r['team']}  ← you")

    conn.close()


def cmd_team(args):
    conn = _get_conn()
    game_date = get_game_date(conn)

    where = ["pf.eval_date=?", "p.level != '1'", EMLB_FILTER, "LOWER(t.name) LIKE LOWER(?)"]
    params = [game_date, f"%{args.team_name}%"]

    if args.fv_min:
        where.append("pf.fv >= ?")
        params.append(args.fv_min)

    order = "pf.prospect_surplus DESC" if args.sort == "surplus" else "fv_sort DESC, pf.prospect_surplus DESC"

    rows = conn.execute(f"""
        SELECT pf.fv_str, pf.fv, pf.level, pf.bucket, p.name, p.age,
               pf.prospect_surplus,
               pf.fv + CASE WHEN pf.fv_str LIKE '%+' THEN 2.5 ELSE 0 END as fv_sort
        FROM prospect_fv pf
        JOIN players p ON pf.player_id = p.player_id
        JOIN teams t ON p.parent_team_id = t.team_id
        WHERE {' AND '.join(where)}
        ORDER BY {order}
        LIMIT ?
    """, params + [args.n]).fetchall()

    if not rows:
        print(f"No prospects found for team matching '{args.team_name}'")
        conn.close()
        return

    print(f"\n{args.team_name} Prospects — {game_date}\n")
    print(f"{'#':<4} {'FV':<5} {'Surplus':>9}  {'Level':<14} {'Pos':<4} {'Age':<4} Name")
    print("-" * 65)
    for i, r in enumerate(rows, 1):
        print(f"{i:<4} {r['fv_str']:<5} {_fmt_surplus(r['prospect_surplus']):>9}  {r['level']:<14} {r['bucket']:<4} {r['age']:<4} {r['name']}")
    conn.close()


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd")

    p_top = sub.add_parser("top")
    p_top.add_argument("--n", type=int, default=100)
    p_top.add_argument("--bucket", type=str, default=None)
    p_top.add_argument("--age-max", type=int, default=None)
    p_top.add_argument("--age-min", type=int, default=None)
    p_top.add_argument("--fv-min", type=int, default=None)
    p_top.add_argument("--level", type=str, default=None)
    p_top.add_argument("--surplus-min", type=float, default=None, help="Min surplus in $M")
    p_top.add_argument("--exclude-org", type=str, default=None)
    p_top.add_argument("--sort", choices=["fv", "surplus"], default="fv", help="Sort order")

    p_sys = sub.add_parser("systems")
    p_sys.add_argument("--n", type=int, default=30)

    p_team = sub.add_parser("team")
    p_team.add_argument("team_name")
    p_team.add_argument("--n", type=int, default=30)
    p_team.add_argument("--fv-min", type=int, default=None)
    p_team.add_argument("--sort", choices=["fv", "surplus"], default="fv")

    args = parser.parse_args()
    if args.cmd == "top":       cmd_top(args)
    elif args.cmd == "systems": cmd_systems(args)
    elif args.cmd == "team":    cmd_team(args)
    else:                       parser.print_help()


if __name__ == "__main__":
    main()
