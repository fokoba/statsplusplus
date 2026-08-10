#!/usr/bin/env python3
"""
model_regression.py — Validate model predictions against actual WAR production.

Measures how well the composite score, imbalance penalties, stat blending,
and surplus model predict actual in-game outcomes. Outputs diagnostic tables
and summary statistics that inform parameter tuning.

Usage:
    python3 scripts/model_regression.py [--league SLUG]
    python3 scripts/model_regression.py --test imbalance
    python3 scripts/model_regression.py --test composite
    python3 scripts/model_regression.py --test aging
    python3 scripts/model_regression.py --test all

Tests available:
    composite   — Composite score vs actual WAR (R², residuals by position)
    imbalance   — Do penalized players underperform non-penalized peers?
    aging       — Actual WAR decline vs projected aging curve
    stat_blend  — Does stat blending improve prediction over tools alone?
    all         — Run all tests
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from statsplusplus.config.league_context import get_league_dir
from statsplusplus.config.league_config import LeagueConfig
from statsplusplus.config.ratings import norm_continuous as _norm
from statsplusplus.data.db import get_connection
from statsplusplus.evaluation.composite import (
    compute_composite_hitter,
    compute_composite_pitcher,
    PITCHER_CORE_KEYS,
)
from statsplusplus.evaluation.constants import (
    HITTER_IMBALANCE_SPREAD_THRESHOLD,
    PITCHER_IMBALANCE_SPREAD_THRESHOLD,
    HITTER_IMBALANCE_WEAKNESS_THRESHOLD,
    PITCHER_IMBALANCE_WEAKNESS_THRESHOLD,
)
from statsplusplus.data.evaluation_engine import (
    load_tool_weights,
    DEFAULT_TOOL_WEIGHTS,
)
from statsplusplus.evaluation.constants import DEFENSIVE_WEIGHTS
from statsplusplus.utils.positions import assign_bucket


# ---------------------------------------------------------------------------
# Statistics helpers
# ---------------------------------------------------------------------------

def pearson_r(xs: list[float], ys: list[float]) -> float:
    """Pearson correlation coefficient."""
    n = len(xs)
    if n < 3:
        return 0.0
    mx = sum(xs) / n
    my = sum(ys) / n
    sx = math.sqrt(sum((x - mx) ** 2 for x in xs) / n)
    sy = math.sqrt(sum((y - my) ** 2 for y in ys) / n)
    if sx == 0 or sy == 0:
        return 0.0
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / n
    return cov / (sx * sy)


def r_squared(xs: list[float], ys: list[float]) -> float:
    """R² (coefficient of determination)."""
    r = pearson_r(xs, ys)
    return r ** 2


def mean(vals: list[float]) -> float:
    return sum(vals) / len(vals) if vals else 0.0


def median(vals: list[float]) -> float:
    if not vals:
        return 0.0
    s = sorted(vals)
    n = len(s)
    if n % 2 == 0:
        return (s[n // 2 - 1] + s[n // 2]) / 2
    return s[n // 2]


def rmse(residuals: list[float]) -> float:
    if not residuals:
        return 0.0
    return math.sqrt(sum(r ** 2 for r in residuals) / len(residuals))


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def load_mlb_player_seasons(conn: sqlite3.Connection, min_pa: int = 200, min_ip: float = 50.0) -> list[dict]:
    """Load MLB player seasons with ratings + actual WAR.

    Returns one row per player-year with both tool ratings and production.
    Only includes qualifying seasons (min PA for hitters, min IP for pitchers).
    """
    conn.row_factory = sqlite3.Row

    # Hitters: join ratings with batting stats
    hitter_sql = """
        SELECT r.player_id, p.name, p.age, p.pos, p.role,
               b.year, b.pa, b.war,
               r.cntct, r.gap, r.pow, r.eye,
               r.speed, r.steal,
               r.ifr, r.ife, r.ifa, r.tdp,
               r.ofr, r.ofe, r.ofa,
               r.c_frm, r.c_blk, r.c_arm,
               r.composite_score, r.ceiling_score, r.tool_only_score
        FROM ratings r
        JOIN players p ON r.player_id = p.player_id
        JOIN mlb_batting_stats b ON r.player_id = b.player_id AND b.year = (
            SELECT MAX(year) FROM mlb_batting_stats WHERE player_id = r.player_id
        )
        WHERE b.pa >= ? AND p.role NOT IN (11, 12, 13)
          AND r.snapshot_date = (SELECT MAX(snapshot_date) FROM ratings WHERE player_id = r.player_id)
    """

    # Pitchers: join ratings with pitching stats
    pitcher_sql = """
        SELECT r.player_id, p.name, p.age, p.pos, p.role,
               ps.year, ps.ip, ps.war,
               r.stf, r.mov, r.ctrl,
               r.stm,
               r.hra, r.pbabip,
               r.composite_score, r.ceiling_score, r.tool_only_score
        FROM ratings r
        JOIN players p ON r.player_id = p.player_id
        JOIN mlb_pitching_stats ps ON r.player_id = ps.player_id AND ps.year = (
            SELECT MAX(year) FROM mlb_pitching_stats WHERE player_id = r.player_id
        )
        WHERE ps.ip >= ? AND p.role IN (11, 12, 13)
          AND r.snapshot_date = (SELECT MAX(snapshot_date) FROM ratings WHERE player_id = r.player_id)
    """

    rows = []
    for row in conn.execute(hitter_sql, (min_pa,)):
        rows.append({"type": "hitter", **dict(row)})
    for row in conn.execute(pitcher_sql, (min_ip,)):
        rows.append({"type": "pitcher", **dict(row)})

    conn.row_factory = None
    return rows


def load_multi_year_seasons(conn: sqlite3.Connection, min_pa: int = 200, min_ip: float = 50.0) -> list[dict]:
    """Load all qualifying player-seasons (multi-year) for aging analysis.

    Returns one row per player-year pair across all available years.
    """
    conn.row_factory = sqlite3.Row

    hitter_sql = """
        SELECT b.player_id, p.name, p.age, p.pos, p.role,
               b.year, b.pa, b.war
        FROM mlb_batting_stats b
        JOIN players p ON b.player_id = p.player_id
        WHERE b.pa >= ? AND p.role NOT IN (11, 12, 13)
    """

    pitcher_sql = """
        SELECT ps.player_id, p.name, p.age, p.pos, p.role,
               ps.year, ps.ip, ps.war
        FROM mlb_pitching_stats ps
        JOIN players p ON ps.player_id = p.player_id
        WHERE ps.ip >= ? AND p.role IN (11, 12, 13)
    """

    rows = []
    for row in conn.execute(hitter_sql, (min_pa,)):
        rows.append({"type": "hitter", **dict(row)})
    for row in conn.execute(pitcher_sql, (min_ip,)):
        rows.append({"type": "pitcher", **dict(row)})

    conn.row_factory = None
    return rows


# ---------------------------------------------------------------------------
# Test: Composite vs actual WAR
# ---------------------------------------------------------------------------

def test_composite_vs_war(conn: sqlite3.Connection, scale: str, league_dir: Path) -> dict:
    """Measure how well composite score predicts actual WAR.

    Reports:
        - Overall R² and RMSE
        - R² by position bucket
        - Residual patterns (over/underperformers)
    """
    rows = load_mlb_player_seasons(conn)
    if not rows:
        return {"error": "No qualifying player-seasons found"}

    weights = load_tool_weights(league_dir)
    hitter_weights = weights.get("hitter", DEFAULT_TOOL_WEIGHTS["hitter"])
    pitcher_weights = weights.get("pitcher", DEFAULT_TOOL_WEIGHTS["pitcher"])

    results_by_bucket: dict[str, list[tuple[float, float]]] = defaultdict(list)
    all_composites: list[float] = []
    all_wars: list[float] = []

    for row in rows:
        # Use stored composite if available, otherwise skip
        composite = row.get("composite_score") or row.get("tool_only_score")
        war = row.get("war")
        if composite is None or war is None:
            continue

        bucket = assign_bucket({"Pos": row.get("pos", 0), "Role": row.get("role", 0)})
        results_by_bucket[bucket].append((float(composite), float(war)))
        all_composites.append(float(composite))
        all_wars.append(float(war))

    if len(all_composites) < 10:
        return {"error": f"Only {len(all_composites)} qualifying seasons"}

    # Overall metrics
    overall_r2 = r_squared(all_composites, all_wars)
    residuals = [w - c * 0.12 for c, w in zip(all_composites, all_wars)]  # rough linear scale
    overall_rmse = rmse(residuals)

    # Per-bucket
    bucket_results = {}
    for bucket, pairs in sorted(results_by_bucket.items()):
        if len(pairs) < 5:
            continue
        comps, wars = zip(*pairs)
        bucket_results[bucket] = {
            "n": len(pairs),
            "r2": round(r_squared(list(comps), list(wars)), 3),
            "mean_composite": round(mean(list(comps)), 1),
            "mean_war": round(mean(list(wars)), 2),
        }

    return {
        "n": len(all_composites),
        "overall_r2": round(overall_r2, 3),
        "overall_rmse": round(overall_rmse, 2),
        "by_bucket": bucket_results,
    }


# ---------------------------------------------------------------------------
# Test: Imbalance penalty validation
# ---------------------------------------------------------------------------

def test_imbalance_penalty(conn: sqlite3.Connection, scale: str, league_dir: Path) -> dict:
    """Test whether imbalance-penalized players underperform non-penalized peers.

    Compares WAR production of players whose tool spread triggers the penalty
    vs players at similar composite levels without the penalty.

    A well-calibrated penalty should make both groups produce similar WAR
    relative to their (penalized) composite. If penalized players still
    underperform their composite, the penalty is too small. If they
    outperform, it's too large.
    """
    rows = load_mlb_player_seasons(conn, min_pa=250, min_ip=60.0)
    if not rows:
        return {"error": "No qualifying player-seasons found"}

    weights = load_tool_weights(league_dir)
    hitter_weights = weights.get("hitter", DEFAULT_TOOL_WEIGHTS["hitter"])
    pitcher_weights = weights.get("pitcher", DEFAULT_TOOL_WEIGHTS["pitcher"])

    penalized_hitters: list[dict] = []
    clean_hitters: list[dict] = []
    penalized_pitchers: list[dict] = []
    clean_pitchers: list[dict] = []

    for row in rows:
        war = row.get("war")
        composite = row.get("composite_score") or row.get("tool_only_score")
        if war is None or composite is None:
            continue

        if row["type"] == "hitter":
            tools = [row.get(k) for k in ("cntct", "gap", "pow", "eye")]
            tools = [_norm(t, scale) for t in tools if t is not None]
            if len(tools) < 3:
                continue
            spread = max(tools) - min(tools)
            has_weak = any(t < HITTER_IMBALANCE_WEAKNESS_THRESHOLD for t in tools)
            is_penalized = spread > HITTER_IMBALANCE_SPREAD_THRESHOLD or has_weak

            entry = {"name": row["name"], "composite": composite, "war": war,
                     "spread": spread, "min_tool": min(tools), "age": row["age"]}
            if is_penalized:
                penalized_hitters.append(entry)
            else:
                clean_hitters.append(entry)

        elif row["type"] == "pitcher":
            tools = [row.get(k) for k in ("stf", "mov", "ctrl")]
            tools = [_norm(t, scale) for t in tools if t is not None]
            if len(tools) < 2:
                continue
            spread = max(tools) - min(tools)
            has_weak = any(t < PITCHER_IMBALANCE_WEAKNESS_THRESHOLD for t in tools)
            is_penalized = spread > PITCHER_IMBALANCE_SPREAD_THRESHOLD or has_weak

            entry = {"name": row["name"], "composite": composite, "war": war,
                     "spread": spread, "min_tool": min(tools), "age": row["age"]}
            if is_penalized:
                penalized_pitchers.append(entry)
            else:
                clean_pitchers.append(entry)

    def compare_group(penalized: list[dict], clean: list[dict], label: str) -> dict:
        """Compare WAR/composite ratio between penalized and clean groups."""
        if len(penalized) < 5 or len(clean) < 5:
            return {"error": f"Insufficient data for {label}",
                    "penalized_n": len(penalized), "clean_n": len(clean)}

        # Group by composite tier (5-point bands) and compare WAR
        pen_wars = [p["war"] for p in penalized]
        clean_wars = [p["war"] for p in clean]
        pen_comps = [p["composite"] for p in penalized]
        clean_comps = [p["composite"] for p in clean]

        # WAR per composite point (slope proxy)
        pen_ratio = mean(pen_wars) / max(1, mean(pen_comps))
        clean_ratio = mean(clean_wars) / max(1, mean(clean_comps))

        # Residual: actual WAR - expected WAR (based on composite)
        # If penalty is correct, residuals should be ~0 for both groups
        pen_residuals = [p["war"] - (p["composite"] - 45) * 0.15 for p in penalized]
        clean_residuals = [p["war"] - (p["composite"] - 45) * 0.15 for p in clean]

        return {
            "penalized_n": len(penalized),
            "clean_n": len(clean),
            "penalized_mean_war": round(mean(pen_wars), 2),
            "clean_mean_war": round(mean(clean_wars), 2),
            "penalized_mean_composite": round(mean(pen_comps), 1),
            "clean_mean_composite": round(mean(clean_comps), 1),
            "penalized_war_per_comp_pt": round(pen_ratio, 4),
            "clean_war_per_comp_pt": round(clean_ratio, 4),
            "penalized_mean_residual": round(mean(pen_residuals), 2),
            "clean_mean_residual": round(mean(clean_residuals), 2),
            "penalty_accuracy": "GOOD" if abs(mean(pen_residuals) - mean(clean_residuals)) < 0.3
                               else "UNDER-PENALIZED" if mean(pen_residuals) < mean(clean_residuals) - 0.3
                               else "OVER-PENALIZED",
        }

    return {
        "hitters": compare_group(penalized_hitters, clean_hitters, "hitters"),
        "pitchers": compare_group(penalized_pitchers, clean_pitchers, "pitchers"),
    }


# ---------------------------------------------------------------------------
# Test: Aging curve validation
# ---------------------------------------------------------------------------

def test_aging_curve(conn: sqlite3.Connection, scale: str, league_dir: Path) -> dict:
    """Validate the aging curve against actual WAR by age.

    Groups player-seasons by age and computes mean WAR relative to peak.
    Compares to our projected aging multipliers.
    """
    from statsplusplus.evaluation.war import aging_mult

    rows = load_multi_year_seasons(conn, min_pa=300, min_ip=80.0)
    if not rows:
        return {"error": "No multi-year data found"}

    # Group WAR by age and type
    hitter_by_age: dict[int, list[float]] = defaultdict(list)
    pitcher_by_age: dict[int, list[float]] = defaultdict(list)

    for row in rows:
        age = row.get("age")
        war = row.get("war")
        if age is None or war is None:
            continue
        if row["type"] == "hitter":
            hitter_by_age[int(age)].append(float(war))
        else:
            pitcher_by_age[int(age)].append(float(war))

    def analyze_curve(by_age: dict[int, list[float]], bucket: str) -> dict:
        if not by_age:
            return {"error": "No data"}

        # Find peak age (highest mean WAR with n >= 10)
        age_means = {age: mean(wars) for age, wars in by_age.items() if len(wars) >= 10}
        if not age_means:
            return {"error": "Not enough data per age"}

        peak_age = max(age_means, key=age_means.get)
        peak_war = age_means[peak_age]

        # Compute actual vs projected multipliers
        comparison = []
        for age in sorted(age_means.keys()):
            if age < 22 or age > 38:
                continue
            actual_mult = age_means[age] / peak_war if peak_war > 0 else 0
            projected_mult = aging_mult(age, bucket)
            comparison.append({
                "age": age,
                "n": len(by_age[age]),
                "actual_mean_war": round(age_means[age], 2),
                "actual_mult": round(actual_mult, 3),
                "projected_mult": round(projected_mult, 3),
                "delta": round(actual_mult - projected_mult, 3),
            })

        # Overall fit
        actuals = [c["actual_mult"] for c in comparison]
        projected = [c["projected_mult"] for c in comparison]
        fit_r2 = r_squared(actuals, projected)
        fit_rmse_val = rmse([a - p for a, p in zip(actuals, projected)])

        return {
            "peak_age": peak_age,
            "peak_war": round(peak_war, 2),
            "n_ages": len(comparison),
            "r2": round(fit_r2, 3),
            "rmse": round(fit_rmse_val, 3),
            "by_age": comparison,
        }

    return {
        "hitters": analyze_curve(hitter_by_age, "SS"),  # Generic hitter bucket
        "pitchers": analyze_curve(pitcher_by_age, "SP"),
    }


# ---------------------------------------------------------------------------
# Test: Stat blending improvement
# ---------------------------------------------------------------------------

def test_stat_blend(conn: sqlite3.Connection, scale: str, league_dir: Path) -> dict:
    """Test whether stat-blended composite predicts WAR better than tools alone.

    Compares prediction accuracy of:
    1. Tool-only score (pre-blend)
    2. Stat-blended composite (post-blend)

    For players with MLB track record, the blended score should be more
    accurate. A large improvement validates the stat blending weight.
    A small improvement suggests the blend weight is too conservative.
    """
    rows = load_mlb_player_seasons(conn, min_pa=300, min_ip=80.0)
    if not rows:
        return {"error": "No qualifying player-seasons"}

    tool_only_scores: list[float] = []
    blended_scores: list[float] = []
    wars: list[float] = []

    for row in rows:
        tool_only = row.get("tool_only_score")
        blended = row.get("composite_score")
        war = row.get("war")
        if tool_only is None or blended is None or war is None:
            continue
        if tool_only == blended:
            # No stat blending occurred (no qualifying stat seasons)
            continue

        tool_only_scores.append(float(tool_only))
        blended_scores.append(float(blended))
        wars.append(float(war))

    if len(wars) < 10:
        return {"error": f"Only {len(wars)} players with stat-blended scores"}

    tool_r2 = r_squared(tool_only_scores, wars)
    blend_r2 = r_squared(blended_scores, wars)

    tool_residuals = [w - t * 0.12 for t, w in zip(tool_only_scores, wars)]
    blend_residuals = [w - b * 0.12 for b, w in zip(blended_scores, wars)]

    return {
        "n": len(wars),
        "tool_only_r2": round(tool_r2, 3),
        "stat_blended_r2": round(blend_r2, 3),
        "improvement": round(blend_r2 - tool_r2, 3),
        "tool_rmse": round(rmse(tool_residuals), 2),
        "blend_rmse": round(rmse(blend_residuals), 2),
        "verdict": "BLEND HELPS" if blend_r2 > tool_r2 + 0.01
                  else "BLEND NEUTRAL" if abs(blend_r2 - tool_r2) <= 0.01
                  else "BLEND HURTS",
    }



# ---------------------------------------------------------------------------
# Calibration: derive league-specific parameters from data
# ---------------------------------------------------------------------------

def calibrate_aging_curves(conn: sqlite3.Connection, min_pa: int = 300, min_ip: float = 80.0) -> dict:
    """Derive league-specific aging curves from longitudinal WAR data.

    Tracks the SAME players across ages to avoid survivorship bias.
    For each player who had qualifying seasons in the peak window (27-28 for
    hitters, 26-28 for pitchers), measures their WAR at other ages relative
    to their peak-window production.

    Returns dict with AGING_CURVE_HITTER and AGING_CURVE_PITCHER.
    """
    conn.row_factory = sqlite3.Row

    # Determine current year from state
    try:
        state_path = Path(str(conn.execute("SELECT * FROM sqlite_master LIMIT 0").description)).parent
    except Exception:
        pass
    # Get max year in data as proxy for state year
    max_year = conn.execute("SELECT MAX(year) FROM mlb_batting_stats").fetchone()[0] or 2033

    def derive_curve(is_pitcher: bool, peak_window: tuple[int, int], min_n: int = 15) -> dict[int, float]:
        if is_pitcher:
            sql = """
                SELECT ps.player_id, p.age, ps.year, ps.war
                FROM mlb_pitching_stats ps
                JOIN players p ON ps.player_id = p.player_id
                WHERE ps.ip >= ? AND ps.split_id = 1 AND p.role IN (11, 12, 13)
            """
            param = min_ip
        else:
            sql = """
                SELECT b.player_id, p.age, b.year, b.war
                FROM mlb_batting_stats b
                JOIN players p ON b.player_id = p.player_id
                WHERE b.pa >= ? AND b.split_id = 1 AND p.role NOT IN (11, 12, 13)
            """
            param = min_pa

        # Build player_id -> {age: war} mapping
        player_seasons: dict[int, dict[int, float]] = defaultdict(dict)
        for row in conn.execute(sql, (param,)):
            pid = row["player_id"]
            current_age = row["age"]
            year = row["year"]
            war = float(row["war"])
            age_at_time = current_age - (max_year - year)
            player_seasons[pid][age_at_time] = war

        # For players with a peak-window season, compute ratios
        ratios_by_age: dict[int, list[float]] = defaultdict(list)
        for pid, ages in player_seasons.items():
            peak_wars = [ages[a] for a in range(peak_window[0], peak_window[1] + 1) if a in ages]
            if not peak_wars:
                continue
            peak_war = max(peak_wars)
            if peak_war <= 0.5:
                continue
            for age, war in ages.items():
                ratios_by_age[age].append(war / peak_war)

        # Build curve from median ratios (robust to outliers)
        raw_curve: dict[int, float] = {}
        for age in range(22, 42):
            vals = ratios_by_age.get(age, [])
            if len(vals) >= min_n:
                sorted_v = sorted(vals)
                raw_curve[age] = sorted_v[len(sorted_v) // 2]

        if not raw_curve:
            return {}

        # Normalize: peak window = 1.0
        peak_val = max(raw_curve.get(a, 0) for a in range(peak_window[0], peak_window[1] + 1))
        if peak_val <= 0:
            return {}

        # Build final curve: monotonic non-increasing from peak forward
        result: dict[int, float] = {}
        prev = 1.0
        for age in range(peak_window[0], 42):
            if age in raw_curve:
                val = min(prev, raw_curve[age] / peak_val)
            else:
                val = max(0.10, prev - 0.03)  # gentle extrapolation
            val = max(0.10, min(1.0, val))
            result[age] = round(val, 3)
            prev = val

        return result

    hitter_curve = derive_curve(is_pitcher=False, peak_window=(27, 28))
    pitcher_curve = derive_curve(is_pitcher=True, peak_window=(26, 28))

    conn.row_factory = None
    return {
        "AGING_CURVE_HITTER": hitter_curve,
        "AGING_CURVE_PITCHER": pitcher_curve,
    }


def calibrate_imbalance_thresholds(conn: sqlite3.Connection, scale: str, league_dir: Path) -> dict:
    """Test whether imbalance penalties improve composite-to-WAR prediction.

    Compares R² of composite vs WAR with and without the penalty applied.
    If removing the penalty improves R², recommends disabling (threshold=999).
    """
    rows = load_mlb_player_seasons(conn, min_pa=250, min_ip=60.0)
    if not rows:
        return {}

    from statsplusplus.evaluation.constants import (
        PITCHER_IMBALANCE_SPREAD_THRESHOLD,
        HITTER_IMBALANCE_SPREAD_THRESHOLD,
    )

    def _r_sq(xs: list[float], ys: list[float]) -> float:
        n = len(xs)
        if n < 3:
            return 0.0
        mx, my = sum(xs) / n, sum(ys) / n
        sx = (sum((x - mx) ** 2 for x in xs) / n) ** 0.5
        sy = (sum((y - my) ** 2 for y in ys) / n) ** 0.5
        if sx == 0 or sy == 0:
            return 0.0
        cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / n
        return (cov / (sx * sy)) ** 2

    def test_penalty(player_rows, tool_keys, threshold, weakness_thresh, spread_rate, weak_rate):
        with_scores, without_scores, wars = [], [], []
        n_affected = 0
        for row in player_rows:
            war = row.get("war")
            comp = row.get("composite_score") or row.get("tool_only_score")
            if war is None or comp is None:
                continue
            tools_raw = [row.get(k) for k in tool_keys]
            tools_n = [_norm(t, scale) for t in tools_raw if t is not None]
            if len(tools_n) < len(tool_keys) - 1:
                continue
            spread = max(tools_n) - min(tools_n)
            penalty = 0.0
            if spread > threshold and len(tools_n) >= 2:
                penalty += (spread - threshold) * spread_rate
                penalty += sum(max(0, weakness_thresh - v) * weak_rate for v in tools_n if v < weakness_thresh)
            if penalty > 0:
                n_affected += 1
            with_scores.append(float(comp))
            without_scores.append(min(80.0, float(comp) + penalty))
            wars.append(float(war))

        if len(wars) < 10:
            return {"error": "insufficient data", "n": len(wars)}

        r2_with = _r_sq(with_scores, wars)
        r2_without = _r_sq(without_scores, wars)
        return {
            "n": len(wars),
            "affected": n_affected,
            "r2_with_penalty": round(r2_with, 4),
            "r2_without_penalty": round(r2_without, 4),
            "delta_r2": round(r2_without - r2_with, 4),
            "verdict": "KEEP" if r2_with > r2_without else "REMOVE",
            "recommended_threshold": threshold if r2_with > r2_without else 999,
        }

    pitcher_rows = [r for r in rows if r["type"] == "pitcher"]
    hitter_rows = [r for r in rows if r["type"] == "hitter"]

    return {
        "pitcher": test_penalty(pitcher_rows, ("stf", "mov", "ctrl"),
                               PITCHER_IMBALANCE_SPREAD_THRESHOLD, 50, 0.20, 0.15),
        "hitter": test_penalty(hitter_rows, ("cntct", "gap", "pow", "eye"),
                              HITTER_IMBALANCE_SPREAD_THRESHOLD, 45, 0.15, 0.12),
    }


def run_calibration(conn: sqlite3.Connection, scale: str, league_dir: Path, dry_run: bool = False) -> dict:
    """Run full parameter calibration and optionally write to model_weights.json."""
    print("Calibrating aging curves...")
    aging = calibrate_aging_curves(conn)

    print("Calibrating imbalance thresholds...")
    imbalance = calibrate_imbalance_thresholds(conn, scale, league_dir)

    # Build MODEL_PARAMS dict
    model_params: dict[str, Any] = {}

    if imbalance.get("pitcher", {}).get("recommended_threshold"):
        model_params["PITCHER_IMBALANCE_SPREAD_THRESHOLD"] = imbalance["pitcher"]["recommended_threshold"]
    if imbalance.get("hitter", {}).get("recommended_threshold"):
        model_params["HITTER_IMBALANCE_SPREAD_THRESHOLD"] = imbalance["hitter"]["recommended_threshold"]

    result = {
        "aging_curves": aging,
        "imbalance": imbalance,
        "model_params": model_params,
    }

    if not dry_run:
        # Write to model_weights.json
        mw_path = league_dir / "config" / "model_weights.json"
        if mw_path.exists():
            with open(mw_path) as f:
                existing = json.load(f)
        else:
            existing = {}

        if aging.get("AGING_CURVE_HITTER"):
            existing["AGING_CURVE_HITTER"] = aging["AGING_CURVE_HITTER"]
        if aging.get("AGING_CURVE_PITCHER"):
            existing["AGING_CURVE_PITCHER"] = aging["AGING_CURVE_PITCHER"]
        if model_params:
            existing.setdefault("MODEL_PARAMS", {}).update(model_params)

        with open(mw_path, "w") as f:
            json.dump(existing, f, indent=2)
        print(f"\nWritten to {mw_path}")

    return result


# ---------------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------------

def print_section(title: str, data: dict, indent: int = 0) -> None:
    """Pretty-print a section of results."""
    prefix = "  " * indent
    print(f"\n{prefix}{'=' * 60}")
    print(f"{prefix}{title}")
    print(f"{prefix}{'=' * 60}")

    for key, value in data.items():
        if isinstance(value, dict):
            print(f"{prefix}  {key}:")
            for k2, v2 in value.items():
                if isinstance(v2, list):
                    print(f"{prefix}    {k2}:")
                    for item in v2:
                        print(f"{prefix}      {item}")
                else:
                    print(f"{prefix}    {k2}: {v2}")
        elif isinstance(value, list):
            print(f"{prefix}  {key}:")
            for item in value[:20]:  # Limit output
                print(f"{prefix}    {item}")
            if len(value) > 20:
                print(f"{prefix}    ... ({len(value) - 20} more)")
        else:
            print(f"{prefix}  {key}: {value}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Model regression testing")
    parser.add_argument("--league", help="League slug (default: active league)")
    parser.add_argument("--test", default="all",
                       choices=["composite", "imbalance", "aging", "stat_blend", "all"],
                       help="Which test to run")
    parser.add_argument("--calibrate", action="store_true",
                       help="Derive league-specific parameters and write to model_weights.json")
    parser.add_argument("--dry-run", action="store_true",
                       help="With --calibrate: show results without writing")
    args = parser.parse_args()

    if args.league:
        os.environ["STATSPP_LEAGUE"] = args.league

    league_dir = get_league_dir()
    lc = LeagueConfig(base_dir=league_dir)
    scale = lc.ratings_scale
    conn = get_connection(league_dir)

    print(f"Model Regression Testing — {league_dir.name}")
    print(f"Ratings scale: {scale}")
    print(f"{'=' * 60}")

    if args.calibrate:
        result = run_calibration(conn, scale, league_dir, dry_run=args.dry_run)
        print_section("Calibration Results", result)
        conn.close()
        return

    tests = {
        "composite": ("Composite Score vs Actual WAR", test_composite_vs_war),
        "imbalance": ("Imbalance Penalty Validation", test_imbalance_penalty),
        "aging": ("Aging Curve Validation", test_aging_curve),
        "stat_blend": ("Stat Blending Improvement", test_stat_blend),
    }

    if args.test == "all":
        run_tests = list(tests.keys())
    else:
        run_tests = [args.test]

    results = {}
    for test_name in run_tests:
        title, func = tests[test_name]
        try:
            result = func(conn, scale, league_dir)
            results[test_name] = result
            print_section(title, result)
        except Exception as e:
            print(f"\n  ERROR in {test_name}: {e}")
            import traceback
            traceback.print_exc()

    # Summary
    print(f"\n{'=' * 60}")
    print("SUMMARY")
    print(f"{'=' * 60}")
    for test_name in run_tests:
        result = results.get(test_name, {})
        if "error" in result:
            print(f"  {test_name}: ERROR — {result['error']}")
        elif test_name == "composite":
            print(f"  {test_name}: R²={result.get('overall_r2', '?')}, n={result.get('n', '?')}")
        elif test_name == "imbalance":
            h = result.get("hitters", {})
            p = result.get("pitchers", {})
            print(f"  {test_name}: hitters={h.get('penalty_accuracy', '?')}, pitchers={p.get('penalty_accuracy', '?')}")
        elif test_name == "aging":
            h = result.get("hitters", {})
            p = result.get("pitchers", {})
            print(f"  {test_name}: hitter_fit_R²={h.get('r2', '?')}, pitcher_fit_R²={p.get('r2', '?')}")
        elif test_name == "stat_blend":
            print(f"  {test_name}: {result.get('verdict', '?')} (Δ R²={result.get('improvement', '?')})")

    conn.close()


if __name__ == "__main__":
    main()
