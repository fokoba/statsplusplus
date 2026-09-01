#!/usr/bin/env python3
"""
benchmark.py — Evaluation engine performance benchmark.

Measures composite and ceiling accuracy against ground truth (WAR, OVR, POT)
across all position buckets. Outputs a summary table suitable for before/after
comparison when tuning model parameters.

Usage:
    python3 scripts/benchmark.py              # active league
    STATSPP_LEAGUE=emlb python3 scripts/benchmark.py
    python3 scripts/benchmark.py --all        # all leagues
    python3 scripts/benchmark.py --json       # machine-readable output
"""

import json, math, os, sys
from collections import defaultdict
from pathlib import Path

BASE = Path(__file__).parent.parent
sys.path.insert(0, str(BASE / "scripts"))
sys.path.insert(0, str(BASE / "web"))

from statsplusplus.config.league_context import get_league_dir, get_active_league_slug
from statsplusplus.config.league_config import LeagueConfig
from statsplusplus.config.ratings import norm as _norm_pkg
from statsplusplus.data.db import get_connection
from statsplusplus.utils.positions import assign_bucket


# ---------------------------------------------------------------------------
# Stats helpers
# ---------------------------------------------------------------------------

def _pearson(xs, ys):
    n = len(xs)
    if n < 10:
        return None
    mx = sum(xs) / n
    my = sum(ys) / n
    ss_xy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    ss_xx = sum((x - mx) ** 2 for x in xs)
    ss_yy = sum((y - my) ** 2 for y in ys)
    denom = (ss_xx * ss_yy) ** 0.5
    return ss_xy / denom if denom else None


def _mae(xs, ys):
    """Mean absolute error."""
    if not xs:
        return None
    return sum(abs(x - y) for x, y in zip(xs, ys)) / len(xs)


def _mean(xs):
    return sum(xs) / len(xs) if xs else None


def _stdev(xs):
    if len(xs) < 2:
        return None
    m = _mean(xs)
    return (sum((x - m) ** 2 for x in xs) / (len(xs) - 1)) ** 0.5


def _cosine_sim(a, b):
    """Cosine similarity between two dicts with matching keys."""
    keys = set(a) & set(b)
    if not keys:
        return None
    dot = sum(a[k] * b[k] for k in keys)
    mag_a = sum(a[k] ** 2 for k in keys) ** 0.5
    mag_b = sum(b[k] ** 2 for k in keys) ** 0.5
    return dot / (mag_a * mag_b) if mag_a * mag_b else None


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

HITTER_BUCKETS = ("C", "SS", "2B", "3B", "CF", "COF", "1B")
PITCHER_BUCKETS = ("SP", "RP")


def _load_mlb_data(conn, cfg, year):
    """Load MLB players with composite scores and WAR from the most recent full season."""
    role_map = {str(k): v for k, v in cfg.role_map.items()}

    # Try current year first, fall back to prior year if insufficient data
    for yr in (year, year - 1):
        rows = conn.execute("""
            SELECT r.ovr, r.pot, r.composite_score, r.ceiling_score,
                   r.tool_only_score,
                   bs.war AS bat_war, bs.ab AS bat_ab,
                   ps.war AS pit_war, ps.ra9war, ps.ip AS pit_ip, ps.gs,
                   p.age, p.pos, p.role, p.name, p.player_id
            FROM latest_ratings r
            JOIN players p ON r.player_id = p.player_id
            LEFT JOIN mlb_batting_stats bs ON p.player_id = bs.player_id
                AND bs.year = ? AND bs.split_id = 1
            LEFT JOIN mlb_pitching_stats ps ON p.player_id = ps.player_id
                AND ps.year = ? AND ps.split_id = 1
            WHERE p.level = 1 AND r.composite_score IS NOT NULL
        """, (yr, yr)).fetchall()

        # Check if we have enough qualifying players
        qualifying = sum(
            1 for r in rows
            if (r["bat_ab"] or 0) >= 200 or (r["pit_ip"] or 0) >= 20
        )
        if qualifying >= 100:
            break
    else:
        return [], year

    data = []
    for r in rows:
        p = dict(r)
        role_str = role_map.get(str(p.get("role") or 0), "position_player")
        p["_role"] = role_str
        p["Pos"] = str(p.get("pos") or "")
        p["_is_pitcher"] = p["Pos"] == "P" or role_str in (
            "starter", "reliever", "closer",
        )
        bucket = assign_bucket(p)

        bat_war = r["bat_war"] or 0
        pit_war = r["pit_war"] or 0
        ra9war = r["ra9war"]
        bat_ab = r["bat_ab"] or 0
        pit_ip = r["pit_ip"] or 0
        gs = r["gs"] or 0

        if p["_is_pitcher"]:
            war = ((pit_war + ra9war) / 2) if ra9war is not None else pit_war
            qualifying = (
                (bucket == "SP" and pit_ip >= 50)
                or (bucket == "RP" and pit_ip >= 20)
            )
        else:
            war = bat_war
            qualifying = bat_ab >= 200

        if not qualifying:
            continue

        data.append({
            "pid": r["player_id"],
            "name": r["name"],
            "age": r["age"],
            "bucket": bucket,
            "ovr": r["ovr"],
            "pot": r["pot"],
            "comp": r["composite_score"],
            "ceil": r["ceiling_score"],
            "tool_only": r["tool_only_score"],
            "war": war,
        })

    return data, yr


def _load_prospect_data(conn, cfg):
    """Load all non-MLB prospects with composite scores."""
    role_map = {str(k): v for k, v in cfg.role_map.items()}

    rows = conn.execute("""
        SELECT r.ovr, r.pot, r.composite_score, r.ceiling_score,
               p.age, p.level, p.name, p.player_id, p.pos, p.role
        FROM latest_ratings r
        JOIN players p ON r.player_id = p.player_id
        WHERE p.level != 1 AND p.age <= 24
        AND r.composite_score IS NOT NULL
        AND CAST(p.level AS INTEGER) NOT IN (7, 8)
    """).fetchall()

    data = []
    for r in rows:
        p = dict(r)
        role_str = role_map.get(str(p.get("role") or 0), "position_player")
        p["_role"] = role_str
        p["Pos"] = str(p.get("pos") or "")
        p["_is_pitcher"] = p["Pos"] == "P" or role_str in (
            "starter", "reliever", "closer",
        )
        bucket = assign_bucket(p)

        try:
            lvl = int(r["level"])
        except (ValueError, TypeError):
            continue
        if lvl in (7, 8):
            continue

        data.append({
            "pid": r["player_id"],
            "name": r["name"],
            "age": r["age"],
            "level": lvl,
            "bucket": bucket,
            "ovr": r["ovr"],
            "pot": r["pot"],
            "comp": r["composite_score"],
            "ceil": r["ceiling_score"],
        })

    return data


# ---------------------------------------------------------------------------
# Benchmark computation
# ---------------------------------------------------------------------------

def compute_benchmark(league_slug):
    """Compute all benchmark metrics for a league. Returns a results dict."""
    league_dir = get_league_dir(league_slug)
    conn = get_connection(league_dir)
    cfg = LeagueConfig(league_dir)

    with open(league_dir / "config" / "state.json") as f:
        state = json.load(f)
    year = state["year"]

    results = {
        "league": league_slug,
        "year": year,
        "game_date": state["game_date"],
    }

    # --- MLB composite vs WAR ---
    mlb_data, stat_year = _load_mlb_data(conn, cfg, year)
    results["stat_year"] = stat_year
    results["mlb_n"] = len(mlb_data)

    prospect_data = _load_prospect_data(conn, cfg)

    # This tool benchmarks the app's composite against the game's OVR. Leagues
    # that don't surface OVR (e.g. PPL) store NULL — the comparison is
    # meaningless there, so report it rather than crashing on None arithmetic.
    # Check both datasets: either may be empty, so require that any present rows
    # all lack OVR before declaring it unavailable.
    _rows_with_ovr = [d for d in (mlb_data + prospect_data) if d.get("ovr") is not None]
    if (mlb_data or prospect_data) and not _rows_with_ovr:
        results["ovr_unavailable"] = True
        return results

    if mlb_data:
        wars = [d["war"] for d in mlb_data]
        comps = [d["comp"] for d in mlb_data]
        ovrs = [d["ovr"] for d in mlb_data]

        results["mlb_overall"] = {
            "comp_r": _pearson(comps, wars),
            "ovr_r": _pearson(ovrs, wars),
            "comp_mae": _mae(comps, [50 + (w - 2.5) * 4 for w in wars]),
            "mean_comp_minus_ovr": _mean([c - o for c, o in zip(comps, ovrs)]),
        }

        # By bucket
        by_bucket = defaultdict(list)
        for d in mlb_data:
            by_bucket[d["bucket"]].append(d)

        bucket_results = {}
        for bkt in list(HITTER_BUCKETS) + list(PITCHER_BUCKETS):
            group = by_bucket[bkt]
            if len(group) < 10:
                continue
            g_wars = [d["war"] for d in group]
            g_comps = [d["comp"] for d in group]
            g_ovrs = [d["ovr"] for d in group]
            comp_r = _pearson(g_comps, g_wars)
            ovr_r = _pearson(g_ovrs, g_wars)
            bucket_results[bkt] = {
                "n": len(group),
                "comp_r": comp_r,
                "ovr_r": ovr_r,
                "gap": (comp_r - ovr_r) if comp_r and ovr_r else None,
                "mean_comp_minus_ovr": _mean(
                    [d["comp"] - d["ovr"] for d in group]
                ),
            }
        results["mlb_by_bucket"] = bucket_results

        # Composite wins count
        wins = sum(
            1 for b in bucket_results.values()
            if b["gap"] is not None and b["gap"] > 0
        )
        total = sum(
            1 for b in bucket_results.values() if b["gap"] is not None
        )
        results["mlb_bucket_wins"] = f"{wins}/{total}"

    # --- Prospect composite vs OVR ---
    results["prospect_n"] = len(prospect_data)

    if prospect_data:
        all_diffs = [d["comp"] - d["ovr"] for d in prospect_data]
        all_ceil_diffs = [d["ceil"] - d["pot"] for d in prospect_data]

        results["prospect_overall"] = {
            "mean_comp_minus_ovr": _mean(all_diffs),
            "stdev_comp_minus_ovr": _stdev(all_diffs),
            "mean_ceil_minus_pot": _mean(all_ceil_diffs),
            "stdev_ceil_minus_pot": _stdev(all_ceil_diffs),
        }

        # By level
        by_level = defaultdict(list)
        for d in prospect_data:
            by_level[d["level"]].append(d)
        level_names = {2: "AAA", 3: "AA", 4: "A", 5: "A-Short", 6: "Rookie"}
        level_results = {}
        for lvl in (2, 3, 4, 5, 6):
            group = by_level[lvl]
            if len(group) < 10:
                continue
            diffs = [d["comp"] - d["ovr"] for d in group]
            cdiffs = [d["ceil"] - d["pot"] for d in group]
            level_results[level_names.get(lvl, str(lvl))] = {
                "n": len(group),
                "mean_comp_minus_ovr": _mean(diffs),
                "mean_ceil_minus_pot": _mean(cdiffs),
            }
        results["prospect_by_level"] = level_results

        # By age band
        age_bands = [
            ("15-17", lambda a: 15 <= a <= 17),
            ("18-19", lambda a: 18 <= a <= 19),
            ("20-21", lambda a: 20 <= a <= 21),
            ("22-24", lambda a: 22 <= a <= 24),
        ]
        age_results = {}
        for label, pred in age_bands:
            group = [d for d in prospect_data if pred(d["age"])]
            if len(group) < 10:
                continue
            diffs = [d["comp"] - d["ovr"] for d in group]
            cdiffs = [d["ceil"] - d["pot"] for d in group]
            age_results[label] = {
                "n": len(group),
                "mean_comp_minus_ovr": _mean(diffs),
                "mean_ceil_minus_pot": _mean(cdiffs),
            }
        results["prospect_by_age"] = age_results

        # High-POT young prospects (ceiling collapse metric)
        high_pot = [
            d for d in prospect_data if d["age"] <= 20 and d["pot"] >= 60
        ]
        if high_pot:
            cdiffs = [d["ceil"] - d["pot"] for d in high_pot]
            crushed = sum(1 for d in cdiffs if d < -10)
            results["ceiling_collapse"] = {
                "n": len(high_pot),
                "mean_ceil_minus_pot": _mean(cdiffs),
                "stdev": _stdev(cdiffs),
                "pct_crushed_gt10": crushed / len(high_pot),
            }

    # --- FV shift distribution ---
    fv_rows = conn.execute("""
        SELECT pf.player_id, pf.fv, pf.bucket,
               r.ovr, r.pot, r.composite_score, r.ceiling_score
        FROM prospect_fv pf
        JOIN latest_ratings r ON pf.player_id = r.player_id
        WHERE pf.eval_date = ? AND pf.fv >= 40
    """, (state["game_date"],)).fetchall()

    if fv_rows:
        fv40_diffs = [r["composite_score"] - r["ovr"] for r in fv_rows]
        fv40_cdiffs = [r["ceiling_score"] - r["pot"] for r in fv_rows]
        results["fv40_n"] = len(fv_rows)
        results["fv40_mean_comp_minus_ovr"] = _mean(fv40_diffs)
        results["fv40_mean_ceil_minus_pot"] = _mean(fv40_cdiffs)

        # By bucket
        by_bkt = defaultdict(list)
        for r in fv_rows:
            by_bkt[r["bucket"]].append(r)
        fv40_bucket = {}
        for bkt in sorted(by_bkt):
            group = by_bkt[bkt]
            diffs = [r["composite_score"] - r["ovr"] for r in group]
            fv40_bucket[bkt] = {
                "n": len(group),
                "mean_comp_minus_ovr": _mean(diffs),
            }
        results["fv40_by_bucket"] = fv40_bucket

    # --- Weight stability (load if available) ---
    tw_path = league_dir / "config" / "tool_weights.json"
    if tw_path.exists():
        with open(tw_path) as f:
            tw = json.load(f)
        results["tool_weights"] = {
            "hitter": tw.get("hitter", {}),
            "pitcher": tw.get("pitcher", {}),
            "calibration_r2": tw.get("calibration_r2", {}),
            "calibration_n": tw.get("calibration_n", {}),
        }

    conn.close()
    return results


# ---------------------------------------------------------------------------
# Display
# ---------------------------------------------------------------------------

def _fmt(val, fmt=".3f"):
    if val is None:
        return "  —  "
    return f"{val:{fmt}}"


def print_benchmark(results):
    """Print a human-readable benchmark summary."""
    league = results["league"].upper()
    print(f"\n{'=' * 70}")
    print(f"  EVALUATION ENGINE BENCHMARK — {league}")
    print(f"  Game date: {results['game_date']}  |  Stat year: {results.get('stat_year', '?')}")
    print(f"{'=' * 70}\n")

    if results.get("ovr_unavailable"):
        print("  This league does not surface OVR/POT ratings — there is no")
        print("  game rating to benchmark the composite against. (N="
              f"{results.get('mlb_n', 0)} MLB players evaluated.)\n")
        return

    # --- MLB Composite vs WAR ---
    overall = results.get("mlb_overall", {})
    print(f"MLB COMPOSITE vs WAR  (N={results.get('mlb_n', 0)})")
    print(f"  Overall:  Comp r={_fmt(overall.get('comp_r'))}  "
          f"OVR r={_fmt(overall.get('ovr_r'))}  "
          f"Mean Comp-OVR={_fmt(overall.get('mean_comp_minus_ovr'), '+.1f')}")
    print(f"  Bucket wins: {results.get('mlb_bucket_wins', '?')}")
    print()

    buckets = results.get("mlb_by_bucket", {})
    if buckets:
        print(f"  {'Bucket':6s} {'N':>4s} {'Comp r':>7s} {'OVR r':>7s} "
              f"{'Gap':>7s} {'C-O':>6s}")
        print(f"  {'-' * 38}")
        for bkt in list(HITTER_BUCKETS) + list(PITCHER_BUCKETS):
            b = buckets.get(bkt)
            if not b:
                continue
            gap_str = _fmt(b["gap"], "+.3f")
            marker = " ✓" if b["gap"] and b["gap"] > 0 else ""
            print(f"  {bkt:6s} {b['n']:4d} {_fmt(b['comp_r']):>7s} "
                  f"{_fmt(b['ovr_r']):>7s} {gap_str:>7s} "
                  f"{_fmt(b['mean_comp_minus_ovr'], '+.1f'):>6s}{marker}")
        print()

    # --- Prospect Inflation ---
    po = results.get("prospect_overall", {})
    print(f"PROSPECT INFLATION  (N={results.get('prospect_n', 0)})")
    print(f"  All prospects:  Comp-OVR={_fmt(po.get('mean_comp_minus_ovr'), '+.1f')} "
          f"(sd={_fmt(po.get('stdev_comp_minus_ovr'), '.1f')})  "
          f"Ceil-POT={_fmt(po.get('mean_ceil_minus_pot'), '+.1f')} "
          f"(sd={_fmt(po.get('stdev_ceil_minus_pot'), '.1f')})")

    fv40 = results.get("fv40_mean_comp_minus_ovr")
    if fv40 is not None:
        print(f"  FV 40+ (N={results.get('fv40_n', 0)}):  "
              f"Comp-OVR={_fmt(fv40, '+.1f')}  "
              f"Ceil-POT={_fmt(results.get('fv40_mean_ceil_minus_pot'), '+.1f')}")
    print()

    # By level
    by_level = results.get("prospect_by_level", {})
    if by_level:
        print(f"  {'Level':8s} {'N':>5s} {'Comp-OVR':>9s} {'Ceil-POT':>9s}")
        for lvl in ("AAA", "AA", "A", "A-Short", "Rookie"):
            l = by_level.get(lvl)
            if not l:
                continue
            print(f"  {lvl:8s} {l['n']:5d} {_fmt(l['mean_comp_minus_ovr'], '+.1f'):>9s} "
                  f"{_fmt(l['mean_ceil_minus_pot'], '+.1f'):>9s}")
        print()

    # By age
    by_age = results.get("prospect_by_age", {})
    if by_age:
        print(f"  {'Age':8s} {'N':>5s} {'Comp-OVR':>9s} {'Ceil-POT':>9s}")
        for band in ("15-17", "18-19", "20-21", "22-24"):
            a = by_age.get(band)
            if not a:
                continue
            print(f"  {band:8s} {a['n']:5d} {_fmt(a['mean_comp_minus_ovr'], '+.1f'):>9s} "
                  f"{_fmt(a['mean_ceil_minus_pot'], '+.1f'):>9s}")
        print()

    # --- Ceiling Collapse ---
    cc = results.get("ceiling_collapse")
    if cc:
        print(f"CEILING COLLAPSE  (age≤20, POT≥60, N={cc['n']})")
        print(f"  Mean Ceil-POT: {_fmt(cc['mean_ceil_minus_pot'], '+.1f')}  "
              f"(sd={_fmt(cc['stdev'], '.1f')})")
        print(f"  Crushed >10pts: {cc['pct_crushed_gt10']:.0%}")
        print()

    # --- FV 40+ by bucket ---
    fv40_bkt = results.get("fv40_by_bucket", {})
    if fv40_bkt:
        print(f"FV 40+ INFLATION BY BUCKET")
        print(f"  {'Bucket':6s} {'N':>4s} {'Comp-OVR':>9s}")
        for bkt in list(HITTER_BUCKETS) + list(PITCHER_BUCKETS):
            b = fv40_bkt.get(bkt)
            if not b:
                continue
            print(f"  {bkt:6s} {b['n']:4d} {_fmt(b['mean_comp_minus_ovr'], '+.1f'):>9s}")
        print()

    # --- Calibration quality ---
    tw = results.get("tool_weights")
    if tw:
        r2 = tw.get("calibration_r2", {})
        ns = tw.get("calibration_n", {})
        if r2:
            print(f"CALIBRATION QUALITY")
            print(f"  {'Bucket':6s} {'N':>4s} {'R²':>8s} {'Max wt':>7s}")
            for bkt in list(HITTER_BUCKETS) + list(PITCHER_BUCKETS):
                n = ns.get(bkt, "?")
                r2_info = r2.get(bkt, {})
                r2_val = r2_info.get("hitting") or r2_info.get("pitching") or 0
                # Find max offensive/pitching weight
                if bkt in HITTER_BUCKETS:
                    weights = tw.get("hitter", {}).get(bkt, {})
                    offensive = {
                        k: weights.get(k, 0)
                        for k in ("contact", "gap", "power", "eye")
                    }
                else:
                    weights = tw.get("pitcher", {}).get(bkt, {})
                    offensive = {
                        k: weights.get(k, 0)
                        for k in ("stuff", "movement", "control")
                    }
                max_tool = max(offensive, key=offensive.get) if offensive else "?"
                max_wt = max(offensive.values()) if offensive else 0
                print(f"  {bkt:6s} {str(n):>4s} {r2_val:8.3f} "
                      f"{max_wt:5.0%} ({max_tool})")
            print()


def print_comparison(all_results):
    """Print a side-by-side comparison table for multiple leagues."""
    if len(all_results) < 2:
        return

    print(f"\n{'=' * 70}")
    print(f"  CROSS-LEAGUE COMPARISON")
    print(f"{'=' * 70}\n")

    leagues = [r["league"].upper() for r in all_results]
    header = f"{'Metric':40s}" + "".join(f"{lg:>12s}" for lg in leagues)
    print(header)
    print("-" * len(header))

    def _row(label, key_fn):
        vals = []
        for r in all_results:
            try:
                v = key_fn(r)
                vals.append(f"{v:>12s}" if isinstance(v, str) else f"{v:>12.3f}")
            except (KeyError, TypeError):
                vals.append(f"{'—':>12s}")
        print(f"{label:40s}" + "".join(vals))

    _row("MLB N", lambda r: str(r.get("mlb_n", 0)))
    _row("MLB Comp vs WAR r",
         lambda r: r["mlb_overall"]["comp_r"])
    _row("MLB OVR vs WAR r",
         lambda r: r["mlb_overall"]["ovr_r"])
    _row("MLB Bucket wins",
         lambda r: r.get("mlb_bucket_wins", "?"))
    _row("Prospect N", lambda r: str(r.get("prospect_n", 0)))
    _row("Prospect Comp-OVR",
         lambda r: r["prospect_overall"]["mean_comp_minus_ovr"])
    _row("Prospect Ceil-POT",
         lambda r: r["prospect_overall"]["mean_ceil_minus_pot"])
    _row("FV40 Comp-OVR",
         lambda r: r.get("fv40_mean_comp_minus_ovr", 0))
    _row("Ceiling collapse (Ceil-POT)",
         lambda r: r["ceiling_collapse"]["mean_ceil_minus_pot"])
    _row("Ceiling crushed >10pts",
         lambda r: r["ceiling_collapse"]["pct_crushed_gt10"])
    print()

    # Weight stability
    tw_all = [r.get("tool_weights") for r in all_results]
    if all(tw_all) and len(tw_all) >= 2:
        print("WEIGHT STABILITY (cosine similarity between leagues)")
        for bkt in list(HITTER_BUCKETS) + list(PITCHER_BUCKETS):
            if bkt in HITTER_BUCKETS:
                keys = ("contact", "gap", "power", "eye")
                w0 = {k: tw_all[0].get("hitter", {}).get(bkt, {}).get(k, 0) for k in keys}
                w1 = {k: tw_all[1].get("hitter", {}).get(bkt, {}).get(k, 0) for k in keys}
            else:
                keys = ("stuff", "movement", "control")
                w0 = {k: tw_all[0].get("pitcher", {}).get(bkt, {}).get(k, 0) for k in keys}
                w1 = {k: tw_all[1].get("pitcher", {}).get(bkt, {}).get(k, 0) for k in keys}
            sim = _cosine_sim(w0, w1)
            sim_str = f"{sim:.3f}" if sim is not None else "—"
            print(f"  {bkt:6s} cos_sim={sim_str}")
        print()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    json_mode = "--json" in sys.argv
    run_all = "--all" in sys.argv

    if run_all:
        data_dir = BASE / "data"
        slugs = [
            d.name for d in data_dir.iterdir()
            if d.is_dir() and (d / "league.db").exists()
        ]
    else:
        slugs = [get_active_league_slug()]

    all_results = []
    for slug in sorted(slugs):
        results = compute_benchmark(slug)
        all_results.append(results)

    if json_mode:
        print(json.dumps(all_results, indent=2, default=str))
    else:
        for results in all_results:
            print_benchmark(results)
        if len(all_results) >= 2:
            print_comparison(all_results)


if __name__ == "__main__":
    main()
