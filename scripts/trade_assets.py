#!/usr/bin/env python3
"""
trade_assets.py — Show tradeable assets for your team (or any team).

Lists MLB surplus players and farm prospects ranked by trade value, grouped
by position. Helps identify what you can offer in a trade.

Usage:
  python3 scripts/trade_assets.py                    # My team's assets
  python3 scripts/trade_assets.py --team MIN         # Another team's assets
  python3 scripts/trade_assets.py --bucket SP        # Filter by position
  python3 scripts/trade_assets.py --min-surplus 10   # Min surplus value ($M)
  python3 scripts/trade_assets.py --prospects-only
  python3 scripts/trade_assets.py --mlb-only
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


def _resolve_team(team_arg):
    """Resolve team abbreviation or name to team_id."""
    if team_arg is None:
        return _cfg.my_team_id
    ta = team_arg.upper()
    for tid, abbr in _cfg.team_abbr_map.items():
        if abbr.upper() == ta:
            return tid
    for tid, name in _cfg.team_names_map.items():
        if ta in name.upper():
            return tid
    raise ValueError(f"Team not found: {team_arg}")


def get_assets(team_id=None, bucket=None, min_surplus_m=0,
               prospects_only=False, mlb_only=False):
    team_id = team_id or _cfg.my_team_id
    conn = _get_conn()

    ed_s = conn.execute("SELECT MAX(eval_date) FROM player_surplus").fetchone()[0]
    ed_f = conn.execute("SELECT MAX(eval_date) FROM prospect_fv").fetchone()[0]

    mlb = []
    if not prospects_only:
        rows = conn.execute("""
            SELECT s.player_id, p.name, p.age, s.bucket, s.ovr, s.surplus,
                   c.salary_0, c.years, c.current_year,
                   c.last_year_team_option, c.last_year_player_option,
                   ce.salary_0 as ext_salary,
                   b.avg, b.obp, b.slg, b.war as bwar,
                   pi.era, pi.war as pwar
            FROM player_surplus s
            JOIN players p ON s.player_id = p.player_id
            LEFT JOIN contracts c ON s.player_id = c.player_id
            LEFT JOIN contract_extensions ce ON s.player_id = ce.player_id
            LEFT JOIN mlb_batting_stats b ON s.player_id = b.player_id
                AND b.year = ? AND b.split_id = 1
            LEFT JOIN mlb_pitching_stats pi ON s.player_id = pi.player_id
                AND pi.year = ? AND pi.split_id = 1
            WHERE s.eval_date = ?
              AND s.team_id = ?
              AND s.level IN ('1', 'MLB')
            ORDER BY s.surplus DESC
        """, (_cfg.year, _cfg.year, ed_s, team_id)).fetchall()

        seen = set()
        for r in rows:
            if r["player_id"] in seen:
                continue
            seen.add(r["player_id"])
            surplus_m = (r["surplus"] or 0) / 1e6
            if surplus_m < min_surplus_m:
                continue
            if bucket and r["bucket"] != bucket:
                continue
            yrs_left = (r["years"] or 1) - (r["current_year"] or 0)
            has_opt = r["last_year_team_option"] or r["last_year_player_option"]
            ext_m = (r["ext_salary"] or 0) / 1e6
            status = "RENTAL" if yrs_left <= 1 and not has_opt else \
                     "RENTAL+EXT" if yrs_left <= 1 and ext_m > 0 else \
                     "OPTION" if yrs_left <= 1 and has_opt else "CONTROLLED"
            is_pitcher = r["bucket"] in ("SP", "RP")
            mlb.append({
                "pid": r["player_id"], "name": r["name"], "age": r["age"],
                "bucket": r["bucket"], "ovr": r["ovr"],
                "surplus_m": round(surplus_m, 1),
                "salary_m": (r["salary_0"] or 0) / 1e6,
                "ext_m": round(ext_m, 1),
                "status": status, "yrs_left": yrs_left,
                "war": r["pwar"] if is_pitcher else r["bwar"],
                "era": r["era"],
                "avg": r["avg"], "obp": r["obp"], "slg": r["slg"],
                "is_pitcher": is_pitcher,
            })

    prospects = []
    if not mlb_only:
        rows = conn.execute("""
            SELECT pf.player_id, p.name, p.age, pf.bucket, pf.fv, pf.fv_str,
                   pf.level, pf.prospect_surplus,
                   r.ovr, r.pot
            FROM prospect_fv pf
            JOIN players p ON pf.player_id = p.player_id
            JOIN latest_ratings r ON pf.player_id = r.player_id
            WHERE pf.eval_date = ?
              AND p.parent_team_id = ?
            ORDER BY pf.prospect_surplus DESC
        """, (ed_f, team_id)).fetchall()

        for r in rows:
            surplus_m = (r["prospect_surplus"] or 0) / 1e6
            if surplus_m < min_surplus_m:
                continue
            if bucket and r["bucket"] != bucket:
                continue
            prospects.append({
                "pid": r["player_id"], "name": r["name"], "age": r["age"],
                "bucket": r["bucket"], "fv": r["fv"], "fv_str": r["fv_str"],
                "level": r["level"], "surplus_m": round(surplus_m, 1),
                "ovr": r["ovr"], "pot": r["pot"],
            })

    conn.close()
    return mlb, prospects


def print_assets(mlb, prospects, team_id):
    team_name = _cfg.team_names_map.get(team_id, str(team_id))
    print(f"\nTradeable Assets — {team_name}\n")

    if mlb:
        print("── MLB Players ──")
        print(f"{'Name':<22} {'Age':>3} {'Pos':<5} {'Ovr':>3} {'Status':<12} "
              f"{'Sal':>6} {'Surp':>8}  Stats")
        print("-" * 95)
        for r in mlb:
            ext = f"+EXT${r['ext_m']:.1f}M" if r["ext_m"] > 0 else ""
            sal = f"${r['salary_m']:.1f}M"
            surp = f"${r['surplus_m']:+.1f}M"
            if r["is_pitcher"]:
                era = f"ERA:{r['era']:.2f}" if r["era"] else ""
                war = f"WAR:{r['war']:.1f}" if r["war"] else ""
                stats = f"{era} {war}".strip()
            else:
                avg = f".{int((r['avg'] or 0)*1000):03}" if r["avg"] else "---"
                obp = f".{int((r['obp'] or 0)*1000):03}" if r["obp"] else "---"
                slg = f".{int((r['slg'] or 0)*1000):03}" if r["slg"] else "---"
                war = f"WAR:{r['war']:.1f}" if r["war"] else ""
                stats = f"{avg}/{obp}/{slg} {war}".strip()
            status_str = r["status"] + (f" {ext}" if ext else "")
            print(f"{r['name']:<22} {r['age']:>3} {r['bucket']:<5} {r['ovr']:>3} "
                  f"{status_str:<12} {sal:>6} {surp:>8}  {stats}")

    if prospects:
        print(f"\n── Farm Prospects ──")
        print(f"{'Name':<22} {'Age':>3} {'Pos':<5} {'FV':<5} {'Lvl':<8} {'Ovr/Pot':>8} {'Surp':>8}")
        print("-" * 70)
        for r in prospects:
            surp = f"${r['surplus_m']:+.1f}M"
            print(f"{r['name']:<22} {r['age']:>3} {r['bucket']:<5} {r['fv_str']:<5} "
                  f"{r['level']:<8} {r['ovr']:>3}/{r['pot']:<3} {surp:>8}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Show tradeable assets for a team")
    parser.add_argument("--team", default=None, help="Team abbreviation (default: my team)")
    parser.add_argument("--bucket", default=None, help="Filter by position bucket")
    parser.add_argument("--min-surplus", type=float, default=0,
                        help="Minimum surplus value in $M (default: 0)")
    parser.add_argument("--prospects-only", action="store_true")
    parser.add_argument("--mlb-only", action="store_true")
    args = parser.parse_args()

    try:
        team_id = _resolve_team(args.team)
    except ValueError as e:
        print(e); sys.exit(1)

    mlb, prospects = get_assets(
        team_id=team_id,
        bucket=args.bucket,
        min_surplus_m=args.min_surplus,
        prospects_only=args.prospects_only,
        mlb_only=args.mlb_only,
    )
    print_assets(mlb, prospects, team_id)
