"""Validate unified evaluation model against existing dual models.

Runs all three models (prospect_surplus, contract_value, unified_surplus)
on every player and reports discrepancies. Used during Phase 1 to ensure
the unified model doesn't degrade existing evaluations.

Usage:
    python3 scripts/validate_unified.py [--league <slug>] [--verbose]
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

_base = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_base / "scripts"))
sys.path.insert(0, str(_base / "src"))

from statsplusplus.config.league_context import get_league_dir
from statsplusplus.config.league_config import LeagueConfig, dollars_per_war, league_minimum
from statsplusplus.data.db import get_connection
from statsplusplus.evaluation.constants import load_model_weights
from statsplusplus.evaluation.player_value import compute_player_value, stat_confidence
from statsplusplus.evaluation.war import stat_peak_war
from statsplusplus.evaluation.surplus import peak_war_from_fv


def _load_career_stats(conn) -> tuple[dict[int, int], dict[int, float]]:
    """Load career PA and IP for all players."""
    career_pa: dict[int, int] = {}
    career_ip: dict[int, float] = {}

    for row in conn.execute(
        "SELECT player_id, SUM(ab + COALESCE(bb, 0) + COALESCE(hbp, 0) + COALESCE(sf, 0)) as pa "
        "FROM mlb_batting_stats WHERE split_id = 1 GROUP BY player_id"
    ).fetchall():
        career_pa[row[0]] = int(row[1] or 0)

    for row in conn.execute(
        "SELECT player_id, SUM(ip) as ip "
        "FROM mlb_pitching_stats WHERE split_id = 1 GROUP BY player_id"
    ).fetchall():
        career_ip[row[0]] = float(row[1] or 0)

    return career_pa, career_ip


def _estimate_salaries(
    conn, pid: int, age: int, bucket: str, years: int,
    current_salary: int, min_sal: int, perpetual_arb: bool,
) -> list[int]:
    """Estimate salary schedule for a player's remaining control."""
    # Simplified: use min_sal for pre-arb, ramp for arb years
    # Full implementation would call estimate_control + arb_salary
    # For validation purposes, this approximation is sufficient
    salaries = []
    for yr in range(years):
        if yr < 3:
            salaries.append(min_sal)
        else:
            # Simple arb ramp
            arb_yr = yr - 2
            arb_mult = {1: 0.25, 2: 0.40, 3: 0.60}.get(arb_yr, 0.60)
            from statsplusplus.evaluation.war import peak_war_from_score
            pw = peak_war_from_score(50, bucket)  # Rough
            salaries.append(max(min_sal, int(pw * 7_000_000 * arb_mult)))
    return salaries


def run(league_slug: str | None = None, verbose: bool = False):
    """Run validation comparison."""
    league_dir = get_league_dir(league_slug)
    conn = get_connection(league_dir)
    conn.row_factory = sqlite3.Row

    cfg = LeagueConfig(league_dir)
    weights = load_model_weights(league_dir)
    state = json.loads((league_dir / "config" / "state.json").read_text())
    game_date = state["game_date"]

    dpw = dollars_per_war(league_dir)
    min_sal = league_minimum(league_dir)
    perpetual_arb = cfg.perpetual_arb

    # Load existing evaluations
    ed = conn.execute("SELECT MAX(eval_date) FROM player_surplus").fetchone()[0]
    if not ed:
        print("No evaluations found. Run fv_calc first.")
        return

    existing_mlb = {}
    for row in conn.execute(
        "SELECT player_id, name, bucket, age, ovr, surplus, surplus_yr1, level "
        "FROM player_surplus WHERE eval_date = ?", (ed,)
    ).fetchall():
        existing_mlb[row["player_id"]] = dict(row)

    existing_prospect = {}
    for row in conn.execute(
        "SELECT player_id, fv, fv_str, prospect_surplus, level, bucket, risk, fv_continuous "
        "FROM prospect_fv WHERE eval_date = ?", (ed,)
    ).fetchall():
        existing_prospect[row["player_id"]] = dict(row)

    # Load career stats
    career_pa, career_ip = _load_career_stats(conn)

    # Load stat history for stat_peak_war
    from contract_value import load_stat_history
    bat_hist, pit_hist, two_way = load_stat_history(conn, game_date)

    # Load ratings for all evaluated players
    all_pids = set(existing_mlb.keys()) | set(existing_prospect.keys())
    ratings = {}
    for row in conn.execute("""
        SELECT r.player_id, r.composite_score, r.true_ceiling, r.ceiling_score,
               p.age, p.level
        FROM ratings r JOIN players p ON r.player_id = p.player_id
        WHERE r.player_id IN ({}) AND r.snapshot_date = (
            SELECT MAX(r2.snapshot_date) FROM ratings r2 WHERE r2.player_id = r.player_id
        )
    """.format(",".join(str(p) for p in all_pids))).fetchall():
        ratings[row["player_id"]] = dict(row)

    # Run unified model on all players
    results = []
    for pid in sorted(all_pids):
        rat = ratings.get(pid)
        if not rat:
            continue

        mlb_row = existing_mlb.get(pid)
        prospect_row = existing_prospect.get(pid)

        composite = rat.get("composite_score") or 0
        ceiling = rat.get("true_ceiling") or rat.get("ceiling_score") or 0
        age = rat.get("age") or 25
        level = rat.get("level", 1)

        if not composite or not ceiling:
            continue

        # Determine FV
        fv_continuous = prospect_row["fv_continuous"] if prospect_row and prospect_row.get("fv_continuous") else None
        if fv_continuous is None and prospect_row:
            fv_continuous = float(prospect_row["fv"])
        if fv_continuous is None:
            # MLB-only player: estimate FV from composite/ceiling
            # This is a rough estimate for comparison purposes
            fv_continuous = float(min(ceiling - 3, composite + (ceiling - composite) * 0.5))

        bucket = (prospect_row or mlb_row)["bucket"]
        level_str = "MLB" if level == 1 else (prospect_row["level"] if prospect_row else "AAA")

        # Get stat history
        pa = career_pa.get(pid, 0)
        ip = career_ip.get(pid, 0.0)
        sw = stat_peak_war(pid, bucket, bat_hist, pit_hist, two_way=two_way)

        # Estimate control and salaries
        years_ctrl = 6 if pa < 130 and ip < 50 else 3  # Simplified
        if mlb_row and prospect_row:
            years_ctrl = 6  # Rookie-eligible

        try:
            unified = compute_player_value(
                fv_continuous=fv_continuous,
                bucket=bucket,
                age=age,
                level=level_str,
                composite=composite,
                ceiling=ceiling,
                career_pa=pa,
                career_ip=ip,
                stat_war=sw,
                years_control=years_ctrl,
                dpw=dpw,
                min_sal=min_sal,
                perpetual_arb=perpetual_arb,
                weights=weights,
            )
        except Exception as e:
            if verbose:
                name = (mlb_row or prospect_row).get("name", str(pid))
                print(f"  ERROR {name}: {e}")
            continue

        # Compare
        existing_surplus = None
        existing_type = None
        name = ""
        if prospect_row and mlb_row:
            # Dual-listed: use prospect surplus (what web UI shows)
            existing_surplus = prospect_row["prospect_surplus"]
            existing_type = "both"
            name = mlb_row.get("name", str(pid))
        elif prospect_row:
            existing_surplus = prospect_row["prospect_surplus"]
            existing_type = "prospect"
            name = str(pid)
        elif mlb_row:
            existing_surplus = mlb_row["surplus"]
            existing_type = "mlb"
            name = mlb_row.get("name", str(pid))

        if existing_surplus is None:
            continue

        results.append({
            "pid": pid,
            "name": name,
            "age": age,
            "bucket": bucket,
            "level": level_str,
            "career_pa": pa,
            "career_ip": ip,
            "stat_conf": unified["stat_confidence"],
            "existing_type": existing_type,
            "existing_surplus": existing_surplus,
            "unified_surplus": unified["surplus"],
            "tool_war": unified["tool_war"],
            "stat_war": unified["stat_war"],
            "peak_war": unified["peak_war"],
        })

    conn.close()

    # Report
    print(f"\n{'='*90}")
    print(f"Unified Model Validation — {league_slug or 'active league'} (eval_date: {ed})")
    print(f"{'='*90}")
    print(f"Total players compared: {len(results)}")

    # Categorize
    prospects = [r for r in results if r["existing_type"] == "prospect"]
    mlb_only = [r for r in results if r["existing_type"] == "mlb"]
    crossover = [r for r in results if r["existing_type"] == "both"]

    for label, group in [("Pure Prospects", prospects), ("MLB Only", mlb_only), ("Crossover (Both)", crossover)]:
        if not group:
            continue
        deltas = [(r["unified_surplus"] - r["existing_surplus"]) / max(1, abs(r["existing_surplus"])) for r in group]
        abs_deltas = [abs(d) for d in deltas]
        within_10 = sum(1 for d in abs_deltas if d <= 0.10)
        within_25 = sum(1 for d in abs_deltas if d <= 0.25)
        avg_delta_pct = sum(deltas) / len(deltas) * 100
        print(f"\n{label} ({len(group)} players):")
        print(f"  Within ±10%: {within_10}/{len(group)} ({100*within_10/len(group):.0f}%)")
        print(f"  Within ±25%: {within_25}/{len(group)} ({100*within_25/len(group):.0f}%)")
        print(f"  Avg delta: {avg_delta_pct:+.1f}%")

        # Show biggest outliers
        group_sorted = sorted(group, key=lambda r: abs(r["unified_surplus"] - r["existing_surplus"]), reverse=True)
        print(f"\n  {'Name':25s} Age Bkt  Lvl    PA  SC   Existing     Unified      Delta")
        print(f"  {'-'*85}")
        for r in group_sorted[:10]:
            delta = r["unified_surplus"] - r["existing_surplus"]
            ex_m = r["existing_surplus"] / 1e6
            un_m = r["unified_surplus"] / 1e6
            d_m = delta / 1e6
            print(f"  {r['name']:25s} {r['age']:3d} {r['bucket']:4s} {r['level']:5s} "
                  f"{r['career_pa']:4d} {r['stat_conf']:.2f}  "
                  f"{ex_m:8.1f}M  {un_m:8.1f}M  {d_m:+7.1f}M")

    if verbose:
        print(f"\n\nFull crossover details:")
        for r in sorted(crossover, key=lambda x: -abs(x["unified_surplus"] - x["existing_surplus"])):
            delta = r["unified_surplus"] - r["existing_surplus"]
            print(f"  {r['name']:25s} age={r['age']} {r['bucket']:4s} "
                  f"PA={r['career_pa']:4d} SC={r['stat_conf']:.2f} "
                  f"tool_war={r['tool_war']:.1f} stat_war={r['stat_war'] or 0:.1f} "
                  f"peak_war={r['peak_war']:.1f} "
                  f"existing={r['existing_surplus']/1e6:.1f}M unified={r['unified_surplus']/1e6:.1f}M "
                  f"delta={delta/1e6:+.1f}M")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Validate unified evaluation model")
    parser.add_argument("--league", type=str, default=None)
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()
    run(league_slug=args.league, verbose=args.verbose)
