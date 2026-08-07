#!/usr/bin/env python3
"""
free_agents.py — Upcoming free agent class analysis.
Usage:
  python3 scripts/free_agents.py                    # All upcoming FAs (contract expires after this season)
  python3 scripts/free_agents.py --bucket SP        # Filter by position bucket
  python3 scripts/free_agents.py --min-war 2.0      # Minimum projected WAR
  python3 scripts/free_agents.py --years 2          # FAs in 2 years (default: 1 = this offseason)
  python3 scripts/free_agents.py --angels           # Angels expiring contracts only
"""

import argparse, json, os, sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "scripts"))

from statsplusplus.config.league_context import get_league_dir, get_active_league_slug
from statsplusplus.config.league_config import LeagueConfig
from statsplusplus.data.db import get_connection
from statsplusplus.evaluation.constants import DEFAULT_MINIMUM_SALARY

league_dir = get_league_dir(get_active_league_slug())
_cfg = LeagueConfig(base_dir=league_dir)

def _get_conn():
    return get_connection(league_dir)


def upcoming_fas(year, years_out=1, bucket=None, min_war=None, my_team_only=False):
    conn = _get_conn()

    # Get latest eval_date
    eval_date = conn.execute(
        "SELECT MAX(eval_date) FROM player_surplus"
    ).fetchone()[0]

    # Contracts expiring: years - current_year <= years_out
    # current_year is 0-indexed, so a 1yr contract has years=1, current_year=0
    # "expires after this season" = years - current_year = 1
    query = """
        SELECT c.player_id, p.name, p.age, c.years, c.current_year,
               c.salary_0, c.contract_team_id,
               c.last_year_team_option, c.last_year_player_option,
               c.last_year_vesting_option, c.last_year_option_buyout,
               p.mlb_service_years, p.mlb_service_days,
               s.bucket, s.ovr, s.surplus,
               t.name as team_name
        FROM contracts c
        JOIN players p ON p.player_id = c.player_id
        LEFT JOIN player_surplus s ON s.player_id = c.player_id AND s.eval_date = ?
        LEFT JOIN teams t ON t.team_id = c.contract_team_id
        WHERE c.years - c.current_year <= ?
          AND c.years > 0
          AND p.level IN ('1', 1)
          AND NOT (c.years = 1 AND c.salary_0 <= ?)
    """
    params = [eval_date, years_out, _cfg.minimum_salary]

    if my_team_only:
        query += f" AND c.contract_team_id = {_cfg.my_team_id}"

    rows = conn.execute(query, params).fetchall()
    conn.close()

    results = []
    for r in rows:
        bkt = r["bucket"] or "?"
        ovr = r["ovr"] or 0
        surplus = r["surplus"] or 0
        age = r["age"] or 0

        if bucket and bkt != bucket:
            continue

        # Estimate projected WAR from OVR (rough, using surplus as proxy)
        # For min_war filter, use surplus / $/WAR as approximation
        if min_war is not None:
            settings = json.load(open(os.path.join(str(league_dir), "config", "league_averages.json")))
            dpw = settings.get("dollar_per_war", 8621429)
            est_war = surplus / dpw if dpw else 0
            if est_war < min_war:
                continue

        yrs_left = r["years"] - r["current_year"]
        cur_sal = r["salary_0"] or 0

        # Detect arb-eligible using exact service time from DB
        is_arb = False
        if yrs_left <= 1 and cur_sal > _cfg.minimum_salary:
            svc_yrs = r["mlb_service_years"]
            svc_days = r["mlb_service_days"] or 0
            if svc_yrs is not None:
                # mlb_service_days is total cumulative days (172 days = 1 year)
                svc = svc_days / 172.0
            else:
                # Fallback to estimation if service time not available
                from statsplusplus.evaluation.arb import estimate_service_time as _est_svc
                conn2 = _get_conn()
                svc = _est_svc(conn2, r["player_id"])
                conn2.close()
            if svc is not None and svc < 6.0:
                is_arb = True

        results.append({
            "pid": r["player_id"],
            "name": r["name"],
            "age": age,
            "bucket": bkt,
            "ovr": ovr,
            "team": r["team_name"] or "?",
            "yrs_left": yrs_left,
            "salary": cur_sal,
            "surplus": surplus,
            "to": r["last_year_team_option"],
            "po": r["last_year_player_option"],
            "vo": r["last_year_vesting_option"],
            "buyout": r["last_year_option_buyout"],
            "is_arb": is_arb,
        })

    results.sort(key=lambda x: (x.get("is_arb", False), -x["ovr"]))
    return results


def print_fas(results, title="Upcoming Free Agents"):
    print(f"\n{title} ({len(results)} players)\n")
    hdr = f"{'Name':<25} {'Age':>3} {'Pos':<4} {'Ovr':>3} {'Team':<20} {'Salary':>10} {'Surplus':>10} {'Status':>8}"
    print(hdr)
    print("-" * len(hdr))
    for r in results:
        sal_str = f"${r['salary']:,.0f}" if r["salary"] else "min"
        sur_str = f"${r['surplus']/1e6:+.1f}M" if r["surplus"] else "n/a"
        if r.get("is_arb"):
            status = "ARB"
        elif r.get("vo"):
            status = "VO"
        elif r.get("to"):
            buyout = r.get("buyout")
            status = f"TO(${buyout/1e6:.1f}M)" if buyout else "TO"
        elif r.get("po"):
            status = "PO"
        else:
            status = "FA"
        print(f"{r['name']:<25} {r['age']:>3} {r['bucket']:<4} {r['ovr']:>3} {r['team']:<20} {sal_str:>10} {sur_str:>10} {status:>8}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--bucket", type=str, default=None)
    ap.add_argument("--min-war", type=float, default=None)
    ap.add_argument("--years", type=int, default=1, help="Years until FA (1=this offseason)")
    ap.add_argument("--my-team", action="store_true", help="Show only my team's expiring contracts")
    ap.add_argument("--year", type=int, default=None)
    args = ap.parse_args()

    if args.year is None:
        args.year = _cfg.year

    results = upcoming_fas(args.year, args.years, args.bucket, args.min_war, args.my_team)
    team_name = _cfg.team_name(_cfg.my_team_id)
    title = f"{team_name} Expiring Contracts" if args.my_team else f"Upcoming Free Agents (in {args.years}yr)"
    print_fas(results, title)
    print()
