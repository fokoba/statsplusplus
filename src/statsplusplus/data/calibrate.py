#!/usr/bin/env python3
"""
calibrate.py — Derive league-specific valuation tables from actual data.

Produces config/model_weights.json with:
  - OVR_TO_WAR: position-specific Ovr→WAR regression (slope + intercept)
  - FV_TO_PEAK_WAR: derived from OVR_TO_WAR by mapping FV→expected peak Ovr
  - ARB_PCT: arb salary as fraction of market value by arb year
  - SCARCITY_MULT: FA availability by Pot grade (mid-season only)

Falls back to constants.py defaults when insufficient data.

Usage: python3 scripts/calibrate.py [--dry-run]
"""

import json, os, sys, math
from collections import defaultdict
from pathlib import Path

# Ensure project root is on path for statsplus.client (used by refresh)
_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent.parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from statsplusplus.data.db import get_connection as _get_connection, init_schema as _init_schema
from statsplusplus.config.league_context import get_league_dir
from statsplusplus.config.league_config import LeagueConfig
from statsplusplus.utils.positions import assign_bucket
from statsplusplus.config.ratings import norm as _norm_pkg
from statsplusplus.evaluation.composite import defensive_score as _def_score_pkg
from statsplusplus.evaluation.constants import DEFENSIVE_WEIGHTS, OVR_TO_WAR_DEFAULT as OVR_TO_WAR, \
    FV_TO_PEAK_WAR_DEFAULT as FV_TO_PEAK_WAR, FV_TO_PEAK_WAR_RP_DEFAULT as FV_TO_PEAK_WAR_RP, \
    ARB_PCT_DEFAULT as ARB_PCT, SCARCITY_MULT_DEFAULT as SCARCITY_MULT, \
    MIN_REGRESSION_N, CALIBRATION_YEARS, DEFAULT_DOLLARS_PER_WAR, DEFAULT_MINIMUM_SALARY

_cfg = LeagueConfig()
_scale = _cfg.ratings_scale

def norm(val):
    return _norm_pkg(val, _scale)

def defensive_score(p, bucket):
    return _def_score_pkg(p, bucket, _scale)

from statsplusplus.data.evaluation_engine import (
    derive_tool_weights, normalize_coefficients, recombine_component_weights,
    DEFAULT_TOOL_WEIGHTS, validate_tool_weights,
)
HITTER_BUCKETS = ("C", "SS", "2B", "3B", "CF", "COF", "1B")
PITCHER_BUCKETS = ("SP", "RP")

# Key mapping from DB column names to player_utils expected names
_KEY_MAP = {
    "pot_c": "PotC", "pot_ss": "PotSS", "pot_second_b": "Pot2B",
    "pot_third_b": "Pot3B", "pot_first_b": "Pot1B", "pot_lf": "PotLF",
    "pot_cf": "PotCF", "pot_rf": "PotRF",
    "c": "C", "ss": "SS", "second_b": "2B", "third_b": "3B",
    "first_b": "1B", "lf": "LF", "cf": "CF", "rf": "RF",
    "stm": "Stm", "ovr": "Ovr", "pot": "Pot",
    "pot_fst": "PotFst", "pot_snk": "PotSnk", "pot_crv": "PotCrv",
    "pot_sld": "PotSld", "pot_chg": "PotChg", "pot_splt": "PotSplt",
    "pot_cutt": "PotCutt", "pot_cir_chg": "PotCirChg", "pot_scr": "PotScr",
    "pot_frk": "PotFrk", "pot_kncrv": "PotKncrv", "pot_knbl": "PotKnbl",
}


def _linreg(xs, ys):
    """Simple OLS regression. Returns (slope, intercept, r_squared, n)."""
    n = len(xs)
    if n < 5:
        return None
    mx = sum(xs) / n
    my = sum(ys) / n
    ss_xx = sum((x - mx) ** 2 for x in xs)
    ss_xy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    ss_yy = sum((y - my) ** 2 for y in ys)
    if ss_xx == 0 or ss_yy == 0:
        return None
    slope = ss_xy / ss_xx
    intercept = my - slope * mx
    r_sq = (ss_xy ** 2) / (ss_xx * ss_yy)
    return slope, intercept, r_sq, n


def _war_at(slope, intercept, ovr):
    return max(0.0, round(slope * ovr + intercept, 2))


def _bucket_player(row, role_map):
    """Assign bucket to a player row from the calibration query."""
    p = dict(row)
    p["Pos"] = str(p.get("pos") or "")
    p["_role"] = role_map.get(str(p.get("role") or 0), "position_player")
    p["_is_pitcher"] = (p["Pos"] == "P" or p["_role"] in ("starter", "reliever", "closer"))
    p["Age"] = p["age"]
    for db_key, api_key in _KEY_MAP.items():
        if db_key in p:
            v = p[db_key]
            p[api_key] = v if isinstance(v, (int, float)) else (int(v) if str(v).lstrip('-').isdigit() else 0)
    return assign_bucket(p, use_pot=False)


# ---------------------------------------------------------------------------
# Step 0: Component-level tool weight regression
# ---------------------------------------------------------------------------

def _calibrate_tool_weights(conn, game_year, role_map):
    """Derive tool weights from current-year WAR regressions.

    Principle: use peak-age players (27-32) whose ratings are stable, with
    current or most-recent-year stats only. No floors or default-blending —
    let the data speak. Falls back to 2 years if single-year sample is
    insufficient.

    Hitter approach:
    - Pool all positions for offensive tool regression (contact/gap/power/eye → WAR)
    - Derive defense weight separately per position (defense rating → WAR residual)
    - Combine offensive + defense + baserunning using shares from the data

    Pitcher approach:
    - Regress stuff/movement/control/arsenal → WAR directly
    - Per-role (SP/RP) when sample sufficient, else pooled
    """
    league_dir = get_league_dir()

    # Determine years to use: prefer current year, fall back to 2 if insufficient
    year_hi = game_year - 1  # most recent complete season
    min_sample_hitter = 80
    min_sample_pitcher = 60

    # Peak-age players (27-32) have stable ratings year-over-year, so we can
    # safely use multiple years of data without stale-rating contamination.
    # Default to 2 years for robust sample size. Widen age range for small leagues.
    age_lo, age_hi = 27, 32
    year_lo = year_hi - 1  # 2 years by default

    n_hitters = conn.execute(
        "SELECT COUNT(*) FROM mlb_batting_stats b JOIN players p ON b.player_id=p.player_id "
        "WHERE b.pa>=300 AND b.split_id=1 AND b.year>=? AND b.year<=? AND p.role NOT IN (11,12,13) AND p.age BETWEEN ? AND ?",
        (year_lo, year_hi, age_lo, age_hi)).fetchone()[0]
    n_pitchers = conn.execute(
        "SELECT COUNT(*) FROM mlb_pitching_stats ps JOIN players p ON ps.player_id=p.player_id "
        "WHERE ps.ip>=80 AND ps.split_id=1 AND ps.year>=? AND ps.year<=? AND p.role IN (11,12,13) AND p.age BETWEEN ? AND ?",
        (year_lo, year_hi, age_lo, age_hi)).fetchone()[0]

    # If still insufficient, widen age range to 25-34
    if n_hitters < min_sample_hitter or n_pitchers < min_sample_pitcher:
        age_lo, age_hi = 25, 34
        n_hitters = conn.execute(
            "SELECT COUNT(*) FROM mlb_batting_stats b JOIN players p ON b.player_id=p.player_id "
            "WHERE b.pa>=300 AND b.split_id=1 AND b.year>=? AND b.year<=? AND p.role NOT IN (11,12,13) AND p.age BETWEEN ? AND ?",
            (year_lo, year_hi, age_lo, age_hi)).fetchone()[0]
        n_pitchers = conn.execute(
            "SELECT COUNT(*) FROM mlb_pitching_stats ps JOIN players p ON ps.player_id=p.player_id "
            "WHERE ps.ip>=80 AND ps.split_id=1 AND ps.year>=? AND ps.year<=? AND p.role IN (11,12,13) AND p.age BETWEEN ? AND ?",
            (year_lo, year_hi, age_lo, age_hi)).fetchone()[0]

    print(f"Tool weight calibration: years {year_lo}-{year_hi}, "
          f"hitters={n_hitters}, pitchers={n_pitchers}, ages {age_lo}-{age_hi}")

    # -------------------------------------------------------------------
    # Hitter offensive tool weights (pooled across positions, age 27-32)
    # -------------------------------------------------------------------
    hitter_rows = conn.execute("""
        SELECT r.cntct, r.gap, r.pow, r.eye, r.speed, r.steal,
               r.babip, r.ks,
               r.ifr, r.ife, r.ifa, r.tdp, r.ofr, r.ofe, r.ofa,
               r.c_frm, r.c_blk, r.c_arm,
               p.pos, p.role, p.age, b.war
        FROM latest_ratings r
        JOIN players p ON r.player_id = p.player_id
        JOIN mlb_batting_stats b ON b.player_id = p.player_id AND b.split_id = 1
        WHERE b.pa >= 300 AND p.role NOT IN (11, 12, 13)
          AND p.age BETWEEN ? AND ?
          AND b.year >= ? AND b.year <= ?
    """, (age_lo, age_hi, year_lo, year_hi)).fetchall()

    # Build offensive tool vectors using the composite contact rating.
    # Testing showed that decomposing contact into babip+avoidk produces
    # noisier individual composites despite babip being a better aggregate
    # predictor. OOTP's contact rating is already an optimally-weighted
    # combination of its components for in-game performance prediction.
    off_tool_ratings = []
    off_targets = []
    for r in hitter_rows:
        contact = norm(r["cntct"])
        gap = norm(r["gap"])
        power = norm(r["pow"])
        eye = norm(r["eye"])
        if any(v is None for v in (contact, gap, power, eye)):
            continue
        off_tool_ratings.append({"contact": contact, "gap": gap, "power": power, "eye": eye})
        off_targets.append(float(r["war"]))

    # Baserunning
    br_tool_ratings = []
    br_targets = []
    for r in hitter_rows:
        spd = norm(r["speed"])
        stl = norm(r["steal"])
        if spd is None:
            continue
        br_tool_ratings.append({"speed": spd, "steal": stl or spd, "stl_rt": stl or spd})
        br_targets.append(float(r["war"]))

    # Run offensive regression
    off_raw = derive_tool_weights(off_tool_ratings, off_targets, min_n=40)
    br_raw = derive_tool_weights(br_tool_ratings, br_targets, min_n=40)

    if off_raw is None:
        print("Hitter offensive regression failed (N=%d) — using defaults", len(off_tool_ratings))
        off_norm = {"contact": 0.35, "gap": 0.20, "power": 0.25, "eye": 0.20}
    else:
        # No floor — let the data speak. Clamp negatives to zero only.
        off_norm = normalize_coefficients(off_raw, min_weight=0.0)

    if br_raw is None:
        br_norm = {"speed": 0.50, "steal": 0.30, "stl_rt": 0.20}
    else:
        br_norm = normalize_coefficients(br_raw, min_weight=0.0)

    # -------------------------------------------------------------------
    # Defense weight per position (from IFR/OFR correlation with WAR residual)
    # -------------------------------------------------------------------
    # After removing offensive prediction, how much does defense explain?
    # Use a fixed defense share derived from the residual analysis:
    # Positions where defense tools correlate with WAR residual get higher weight.
    DEFENSE_SHARES = {
        "C": 0.15, "SS": 0.15, "2B": 0.15, "3B": 0.10,
        "CF": 0.15, "COF": 0.00, "1B": 0.00,
    }
    BASERUNNING_SHARE = 0.06  # speed/steal get ~6% across positions

    # -------------------------------------------------------------------
    # Build per-position hitter weights
    # -------------------------------------------------------------------
    result_hitter = {}
    for bucket in ("C", "SS", "2B", "3B", "CF", "COF", "1B"):
        def_share = DEFENSE_SHARES[bucket]
        br_share = BASERUNNING_SHARE
        off_share = 1.0 - def_share - br_share

        # Scale offensive tools by their share
        unified = {}
        off_total = sum(off_norm.values())
        if off_total > 0:
            for k, v in off_norm.items():
                unified[k] = (v / off_total) * off_share

        # Scale baserunning by its share
        br_total = sum(br_norm.values())
        if br_total > 0:
            for k, v in br_norm.items():
                unified[k] = (v / br_total) * br_share

        unified["defense"] = def_share

        # Ensure sum = 1.0
        total = sum(unified.values())
        if total > 0:
            unified = {k: round(v / total, 4) for k, v in unified.items()}

        result_hitter[bucket] = unified

    # -------------------------------------------------------------------
    # Pitcher tool weights (age 27-32, WAR target)
    # -------------------------------------------------------------------
    pitcher_rows = conn.execute("""
        SELECT r.stf, r.mov, r.ctrl, r.hra AS rating_hra, r.pbabip AS rating_pbabip,
               r.fst, r.snk, r.crv, r.sld, r.chg, r.splt, r.cutt,
               r.cir_chg, r.scr, r.frk, r.kncrv, r.knbl,
               r.stm, p.role, p.age, p.pos, ps.war, ps.ip, ps.gs
        FROM latest_ratings r
        JOIN players p ON r.player_id = p.player_id
        JOIN mlb_pitching_stats ps ON ps.player_id = p.player_id AND ps.split_id = 1
        WHERE p.level = 1 AND p.age BETWEEN ? AND ?
          AND ps.year >= ? AND ps.year <= ?
          AND ((p.role IN (12,13) AND ps.ip >= 20 AND ps.gs <= 3)
               OR (COALESCE(p.role,0) NOT IN (12,13) AND ps.ip >= 50))
    """, (age_lo, age_hi, year_lo, year_hi)).fetchall()

    PITCHER_BUCKETS = ("SP", "RP")
    pitching_data = {role: ([], []) for role in PITCHER_BUCKETS}

    pitch_cols = ["fst", "snk", "crv", "sld", "chg", "splt", "cutt",
                  "cir_chg", "scr", "frk", "kncrv", "knbl"]

    # Detect extended pitcher ratings
    # Note: we do NOT include hra/pbabip as separate features in the regression.
    # Movement is a composite of hra + ground ball tendency. Including both
    # movement and hra creates multicollinearity (r=0.927). The composite
    # function always receives 'movement' as input, so we calibrate against it.
    # Extended ratings only matter if we later decompose the composite function.

    for r in pitcher_rows:
        bucket = _bucket_player(r, role_map)
        if bucket not in PITCHER_BUCKETS:
            continue

        stuff = norm(r["stf"])
        control = norm(r["ctrl"])
        # Use HRA as movement proxy when available (cleaner signal, r=0.927 with mov)
        hra_val = norm(r["rating_hra"])
        if hra_val and hra_val > 20:
            movement = hra_val
        else:
            movement = norm(r["mov"])
        if any(v is None for v in (stuff, movement, control)):
            continue

        # Arsenal quality
        pitch_ratings = [norm(r[col]) for col in pitch_cols if r[col] and r[col] > 0]
        arsenal_quality = sum(1 for pr in pitch_ratings if pr is not None and pr >= 45)

        tool_dict = {"stuff": stuff, "movement": movement, "control": control, "arsenal": arsenal_quality}

        pitching_data[bucket][0].append(tool_dict)
        pitching_data[bucket][1].append(float(r["war"]))

    result_pitcher = {}
    for role in PITCHER_BUCKETS:
        tool_ratings, targets = pitching_data[role]
        pitching_raw = derive_tool_weights(tool_ratings, targets, min_n=30)

        if pitching_raw is not None:
            # No artificial floor — let the regression decide
            pitching_norm = normalize_coefficients(pitching_raw, min_weight=0.0)
            # Ensure arsenal gets at least 5% (it's always relevant for SP depth)
            if "arsenal" in pitching_norm and pitching_norm["arsenal"] < 0.05:
                deficit = 0.05 - pitching_norm["arsenal"]
                pitching_norm["arsenal"] = 0.05
                # Remove from largest
                largest = max(pitching_norm, key=pitching_norm.get)
                pitching_norm[largest] -= deficit
            # Re-normalize
            pt = sum(pitching_norm.values())
            if pt > 0:
                pitching_norm = {k: round(v / pt, 4) for k, v in pitching_norm.items()}
            result_pitcher[role] = pitching_norm
        else:
            print("Pitcher %s regression failed (N=%d) — using defaults", role, len(tool_ratings))
            result_pitcher[role] = dict(DEFAULT_TOOL_WEIGHTS["pitcher"].get(role, {}))

    # -------------------------------------------------------------------
    # Recombination weights (for component display)
    # -------------------------------------------------------------------
    recombination = {}
    for bucket in result_hitter:
        def_share = DEFENSE_SHARES.get(bucket, 0.0)
        br_share = BASERUNNING_SHARE
        off_share = 1.0 - def_share - br_share
        recombination[bucket] = {
            "offense": round(off_share, 2),
            "defense": round(def_share, 2),
            "baserunning": round(br_share, 2),
        }

    # Build output
    tool_weights = {
        "version": 1,
        "source": "calibrated",
        "calibration_date": f"{game_year}-01-01",
        "calibration_n": {
            "hitter_offensive": len(off_tool_ratings),
            "pitcher_SP": len(pitching_data.get("SP", ([], []))[0]),
            "pitcher_RP": len(pitching_data.get("RP", ([], []))[0]),
        },
        "hitter": result_hitter,
        "pitcher": result_pitcher,
        "recombination": recombination,
    }

    # Validate
    if not validate_tool_weights(tool_weights):
        print("Calibrated tool_weights failed validation — using defaults")
        return dict(DEFAULT_TOOL_WEIGHTS)

    return tool_weights


# Step 1: OVR_TO_WAR regression
# ---------------------------------------------------------------------------

def _calibrate_ovr_to_war(conn, game_year, role_map):
    """Run Ovr→WAR regression per position bucket using recent complete seasons."""
    year_lo = game_year - CALIBRATION_YEARS - 1  # exclusive lower bound
    year_hi = game_year - 1  # inclusive upper bound (exclude current partial season)

    # Guard: skip entirely if league doesn't expose OVR
    ovr_count = conn.execute(
        "SELECT COUNT(*) FROM latest_ratings WHERE ovr IS NOT NULL"
    ).fetchone()[0]
    if ovr_count < MIN_REGRESSION_N:
        return {}, {}

    # Hitters
    hitter_rows = conn.execute("""
        SELECT r.player_id, r.ovr, r.pot, p.age, p.pos, p.role,
               r.pot_c, r.pot_ss, r.pot_second_b, r.pot_third_b, r.pot_first_b,
               r.pot_lf, r.pot_cf, r.pot_rf,
               r.c, r.ss, r.second_b, r.third_b, r.first_b, r.lf, r.cf, r.rf,
               r.stm,
               r.pot_fst, r.pot_snk, r.pot_crv, r.pot_sld, r.pot_chg,
               r.pot_splt, r.pot_cutt, r.pot_cir_chg, r.pot_scr, r.pot_frk,
               r.pot_kncrv, r.pot_knbl,
               bs.war
        FROM latest_ratings r
        JOIN players p ON r.player_id = p.player_id
        JOIN mlb_batting_stats bs ON bs.player_id = p.player_id
        WHERE p.level = 1 AND bs.split_id = 1
          AND bs.year > ? AND bs.year <= ? AND bs.ab >= 300
          AND r.ovr IS NOT NULL
    """, (year_lo, year_hi)).fetchall()

    # Pitchers
    pitcher_rows = conn.execute("""
        SELECT r.player_id, r.ovr, r.pot, p.age, p.pos, p.role,
               r.stm,
               r.pot_fst, r.pot_snk, r.pot_crv, r.pot_sld, r.pot_chg,
               r.pot_splt, r.pot_cutt, r.pot_cir_chg, r.pot_scr, r.pot_frk,
               r.pot_kncrv, r.pot_knbl,
               (ps.war + COALESCE(ps.ra9war, ps.war)) / 2.0 as war,
               ps.gs, ps.ip
        FROM latest_ratings r
        JOIN players p ON r.player_id = p.player_id
        JOIN mlb_pitching_stats ps ON ps.player_id = p.player_id
        WHERE p.level = 1 AND ps.split_id = 1
          AND ps.year > ? AND ps.year <= ?
          AND r.ovr IS NOT NULL
          AND ((p.role IN (12,13) AND ps.ip >= 20 AND ps.gs <= 3)
               OR (COALESCE(p.role,0) NOT IN (12,13) AND ps.ip >= 40))
    """, (year_lo, year_hi)).fetchall()

    bucket_data = defaultdict(list)
    for r in hitter_rows:
        bucket = _bucket_player(r, role_map)
        bucket_data[bucket].append((r["ovr"], r["war"]))
    for r in pitcher_rows:
        bucket = _bucket_player(r, role_map)
        bucket_data[bucket].append((r["ovr"], r["war"]))

    # Run regression per bucket; fall back to grouped for small samples
    all_hitter_data = []
    for b in HITTER_BUCKETS:
        all_hitter_data.extend(bucket_data.get(b, []))

    regressions = {}
    for bucket in list(HITTER_BUCKETS) + list(PITCHER_BUCKETS):
        data = bucket_data.get(bucket, [])
        if len(data) >= MIN_REGRESSION_N:
            result = _linreg([d[0] for d in data], [d[1] for d in data])
            if result:
                regressions[bucket] = result
                continue
        # Fall back to grouped hitter regression
        if bucket in HITTER_BUCKETS and len(all_hitter_data) >= MIN_REGRESSION_N:
            result = _linreg([d[0] for d in all_hitter_data],
                             [d[1] for d in all_hitter_data])
            if result:
                regressions[bucket] = (*result[:3], f"grouped({len(all_hitter_data)})")

    return regressions, bucket_data


def _build_ovr_to_war_table(regressions):
    """Convert regression results into OVR_TO_WAR format: list of (Ovr, hitter, SP, RP) tuples.
    
    For hitters, uses position-specific regressions. The table stores per-position values
    rather than a single hitter column.
    """
    ovr_points = [80, 75, 70, 65, 60, 55, 50, 45, 40]
    table = {}  # bucket -> {ovr: war}

    for bucket in list(HITTER_BUCKETS) + list(PITCHER_BUCKETS):
        reg = regressions.get(bucket)
        if reg:
            slope, intercept = reg[0], reg[1]
            table[bucket] = {ovr: _war_at(slope, intercept, ovr) for ovr in ovr_points}
        else:
            # Use defaults from constants.py
            if bucket in PITCHER_BUCKETS:
                col = 2 if bucket == "SP" else 3
                table[bucket] = {row[0]: row[col] for row in OVR_TO_WAR}
            else:
                table[bucket] = {row[0]: row[1] for row in OVR_TO_WAR}

    return table


# ---------------------------------------------------------------------------
# Development curve calibration (gap closure, age runway, expected gaps)
# ---------------------------------------------------------------------------

def _calibrate_development_curves(conn):
    """Derive per-league development curves from cross-sectional OVR/POT data.

    Queries all players aged 17-26 with valid OVR/POT, computes mean
    realization (OVR/POT) and mean gap (POT-OVR) by age for hitters and
    pitchers separately.

    Returns dict with keys:
        gap_closure_hitter, gap_closure_pitcher: {age: rate}
        age_runway_hitter, age_runway_pitcher: {age: mult}
        expected_gap_hitter, expected_gap_pitcher: {age: gap}
    or None if insufficient data.
    """
    rows = conn.execute("""
        SELECT p.age,
               CASE WHEN p.pos = 1 THEN 1 ELSE 0 END as is_pitcher,
               AVG(CAST(r.ovr AS FLOAT) / NULLIF(r.pot, 0)) as mean_real,
               AVG(r.pot - r.ovr) as mean_gap,
               COUNT(*) as n
        FROM latest_ratings r
        JOIN players p ON r.player_id = p.player_id
        WHERE p.age BETWEEN 17 AND 26
          AND r.pot > 20 AND r.ovr > 0
        GROUP BY p.age, is_pitcher
        ORDER BY is_pitcher, p.age
    """).fetchall()

    if not rows:
        return None

    # Organize by type
    hitter_data = {}  # age -> (mean_real, mean_gap, n)
    pitcher_data = {}
    for r in rows:
        d = pitcher_data if r["is_pitcher"] else hitter_data
        d[r["age"]] = (r["mean_real"], r["mean_gap"], r["n"])

    MIN_N = 30  # minimum sample per age bucket

    result = {}
    for label, data in [("hitter", hitter_data), ("pitcher", pitcher_data)]:
        # Filter to ages with enough data
        ages = sorted(a for a, (_, _, n) in data.items() if n >= MIN_N)
        if len(ages) < 5:
            return None  # not enough age coverage

        # Terminal realization (age 26, or highest available)
        terminal_age = max(ages)
        terminal_real = data[terminal_age][0]

        # Gap closure: forward-looking. At age X, what fraction of the
        # remaining gap (1.0 - realization_X) will close by terminal?
        # closure_X = (terminal_real - real_X) / (1.0 - real_X)
        closure = {}
        for age in ages:
            real_x = data[age][0]
            remaining = 1.0 - real_x
            if remaining > 0.01:
                closure[age] = round((terminal_real - real_x) / remaining, 2)
            else:
                closure[age] = 0.0

        # Age runway: normalized to age 21 = 1.0.
        # Runway = remaining gap fraction relative to age 21's remaining gap.
        real_21 = data.get(21, (0.73, 0, 0))[0]
        remaining_21 = 1.0 - real_21
        runway = {}
        for age in ages:
            real_x = data[age][0]
            remaining_x = 1.0 - real_x
            if remaining_21 > 0.01:
                runway[age] = round(remaining_x / remaining_21, 2)
            else:
                runway[age] = 0.0

        # Expected gap: mean POT-OVR at each age (rounded to int)
        expected_gap = {}
        for age in ages:
            expected_gap[age] = round(data[age][1])

        result[f"gap_closure_{label}"] = closure
        result[f"age_runway_{label}"] = runway
        result[f"expected_gap_{label}"] = expected_gap
        result[f"calibration_n_{label}"] = {a: data[a][2] for a in ages}

    return result


def _calibrate_years_to_mlb(conn):
    """Derive years-to-MLB by level from mean age at each level vs young MLB debut age.

    Returns dict mapping level label → years, or None if insufficient data.
    """
    young_mlb = conn.execute(
        "SELECT AVG(age) FROM players WHERE level = 1 AND age <= 26"
    ).fetchone()[0]
    if not young_mlb:
        return None

    rows = conn.execute("""
        SELECT CAST(p.level AS INTEGER) as lvl, AVG(p.age) as mean_age, COUNT(*) as n
        FROM players p
        JOIN latest_ratings r ON p.player_id = r.player_id
        WHERE CAST(p.level AS INTEGER) >= 2 AND r.pot >= 40 AND r.ovr > 0
          AND p.age BETWEEN 17 AND 25
        GROUP BY CAST(p.level AS INTEGER)
    """).fetchall()

    if not rows:
        return None

    level_names = {2: "AAA", 3: "AA", 4: "A", 5: "A-Short",
                   6: "Rookie", 8: "Intl", 10: "A-Short", 11: "DSL"}
    aliases = {"Rookie": ["USL"]}

    result = {"MLB": 0}
    for r in rows:
        name = level_names.get(int(r["lvl"]))
        if not name or r["n"] < 20:
            continue
        yrs = round(max(0.5, young_mlb - r["mean_age"]), 1)
        result[name] = yrs
        for alias in aliases.get(name, []):
            result[alias] = yrs

    return result if len(result) > 2 else None


# ---------------------------------------------------------------------------
# COMPOSITE_TO_WAR regression (runs in calibrate pass 2)
# ---------------------------------------------------------------------------

def _calibrate_composite_to_war(conn, game_year, role_map):
    """Run Composite_Score→WAR regression per position bucket.

    Same methodology as _calibrate_ovr_to_war() but reads composite_score
    column instead of ovr. Falls back gracefully when composite_score data
    is insufficient (first run before evaluation engine has populated scores).

    Returns (regressions, bucket_data) or (None, None) when insufficient data.
    """
    year_lo = game_year - CALIBRATION_YEARS - 1
    year_hi = game_year - 1

    # Check if composite_score data exists at all
    check = conn.execute(
        "SELECT COUNT(*) FROM latest_ratings WHERE composite_score IS NOT NULL"
    ).fetchone()[0]
    if check < MIN_REGRESSION_N:
        return None, None

    # Hitters
    hitter_rows = conn.execute("""
        SELECT r.player_id, r.composite_score, r.pot, p.age, p.pos, p.role,
               r.pot_c, r.pot_ss, r.pot_second_b, r.pot_third_b, r.pot_first_b,
               r.pot_lf, r.pot_cf, r.pot_rf,
               r.c, r.ss, r.second_b, r.third_b, r.first_b, r.lf, r.cf, r.rf,
               r.stm, r.ovr,
               r.pot_fst, r.pot_snk, r.pot_crv, r.pot_sld, r.pot_chg,
               r.pot_splt, r.pot_cutt, r.pot_cir_chg, r.pot_scr, r.pot_frk,
               r.pot_kncrv, r.pot_knbl,
               bs.war
        FROM latest_ratings r
        JOIN players p ON r.player_id = p.player_id
        JOIN mlb_batting_stats bs ON bs.player_id = p.player_id
        WHERE p.level = 1 AND bs.split_id = 1
          AND bs.year > ? AND bs.year <= ? AND bs.ab >= 300
          AND r.composite_score IS NOT NULL
    """, (year_lo, year_hi)).fetchall()

    # Pitchers
    pitcher_rows = conn.execute("""
        SELECT r.player_id, r.composite_score, r.pot, p.age, p.pos, p.role,
               r.stm,
               r.pot_fst, r.pot_snk, r.pot_crv, r.pot_sld, r.pot_chg,
               r.pot_splt, r.pot_cutt, r.pot_cir_chg, r.pot_scr, r.pot_frk,
               r.pot_kncrv, r.pot_knbl,
               (ps.war + COALESCE(ps.ra9war, ps.war)) / 2.0 as war,
               ps.gs, ps.ip
        FROM latest_ratings r
        JOIN players p ON r.player_id = p.player_id
        JOIN mlb_pitching_stats ps ON ps.player_id = p.player_id
        WHERE p.level = 1 AND ps.split_id = 1
          AND ps.year > ? AND ps.year <= ?
          AND r.composite_score IS NOT NULL
          AND ((p.role IN (12,13) AND ps.ip >= 20 AND ps.gs <= 3)
               OR (COALESCE(p.role,0) NOT IN (12,13) AND ps.ip >= 40))
    """, (year_lo, year_hi)).fetchall()

    bucket_data = defaultdict(list)
    for r in hitter_rows:
        bucket = _bucket_player(r, role_map)
        bucket_data[bucket].append((r["composite_score"], r["war"]))
    for r in pitcher_rows:
        bucket = _bucket_player(r, role_map)
        bucket_data[bucket].append((r["composite_score"], r["war"]))

    # Check if we have enough total data
    total_data = sum(len(v) for v in bucket_data.values())
    if total_data < MIN_REGRESSION_N:
        return None, None

    # Run regression per bucket; fall back to grouped for small samples
    all_hitter_data = []
    for b in HITTER_BUCKETS:
        all_hitter_data.extend(bucket_data.get(b, []))

    regressions = {}
    for bucket in list(HITTER_BUCKETS) + list(PITCHER_BUCKETS):
        data = bucket_data.get(bucket, [])
        if len(data) >= MIN_REGRESSION_N:
            result = _linreg([d[0] for d in data], [d[1] for d in data])
            if result:
                regressions[bucket] = result
                continue
        # Fall back to grouped hitter regression
        if bucket in HITTER_BUCKETS and len(all_hitter_data) >= MIN_REGRESSION_N:
            result = _linreg([d[0] for d in all_hitter_data],
                             [d[1] for d in all_hitter_data])
            if result:
                regressions[bucket] = (*result[:3], f"grouped({len(all_hitter_data)})")

    return regressions, bucket_data


# ---------------------------------------------------------------------------
# Step 2: FV_TO_PEAK_WAR — derived from OVR_TO_WAR
# ---------------------------------------------------------------------------

def _derive_fv_to_peak_war(ovr_table):
    """Map FV grades to peak WAR using the calibrated OVR_TO_WAR.
    
    FV represents expected peak Ovr. A prospect with FV 55 is expected to
    peak around Ovr 55-60. We use FV+5 as the expected peak Ovr (prospects
    who reach their FV typically settle slightly above it at peak).
    
    Produces per-bucket tables for all positions so the surplus model can
    use position-specific WAR expectations (a FV 50 COF produces less WAR
    than a FV 50 SS).
    """
    fv_points = [80, 70, 65, 60, 55, 50, 45, 40]

    def _interp_table(tbl, ovr):
        """Interpolate from a {ovr: war} dict."""
        pts = sorted(tbl.keys())
        if ovr >= pts[-1]:
            return tbl[pts[-1]]
        if ovr <= pts[0]:
            return tbl[pts[0]]
        for i in range(len(pts) - 1):
            if pts[i] <= ovr <= pts[i + 1]:
                t = (ovr - pts[i]) / (pts[i + 1] - pts[i])
                return tbl[pts[i]] + t * (tbl[pts[i + 1]] - tbl[pts[i]])
        return tbl[pts[0]]

    # Per-bucket hitter FV→WAR tables
    hitter_fv_tables = {}
    for bucket in HITTER_BUCKETS:
        if bucket in ovr_table:
            hitter_fv_tables[bucket] = {}
            for fv in fv_points:
                peak_ovr = min(fv + 5, 80)
                hitter_fv_tables[bucket][fv] = round(
                    _interp_table(ovr_table[bucket], peak_ovr), 1)

    # Generic hitter average (fallback for unknown buckets)
    hitter_fv_avg = {}
    for fv in fv_points:
        peak_ovr = min(fv + 5, 80)
        wars = [_interp_table(ovr_table[b], peak_ovr)
                for b in HITTER_BUCKETS if b in ovr_table]
        hitter_fv_avg[fv] = round(sum(wars) / len(wars), 1) if wars else FV_TO_PEAK_WAR.get(fv, 2.0)

    # SP FV→WAR
    sp_fv = {}
    if "SP" in ovr_table:
        for fv in fv_points:
            peak_ovr = min(fv + 5, 80)
            sp_fv[fv] = round(_interp_table(ovr_table["SP"], peak_ovr), 1)
    else:
        sp_fv = dict(FV_TO_PEAK_WAR)

    # RP FV→WAR
    rp_fv = {}
    if "RP" in ovr_table:
        for fv in fv_points:
            peak_ovr = min(fv + 5, 80)
            rp_fv[fv] = round(_interp_table(ovr_table["RP"], peak_ovr), 1)
    else:
        rp_fv = dict(FV_TO_PEAK_WAR_RP)

    return hitter_fv_avg, hitter_fv_tables, sp_fv, rp_fv


# ---------------------------------------------------------------------------
# Step 3: ARB_PCT calibration
# ---------------------------------------------------------------------------

def _calibrate_arb_pct(conn, game_year, dpw):
    """Compute arb salary as fraction of market value by estimated arb year.

    Filters: WAR >= 1.0 (avoids noise from bad-year players whose salary
    looks like a huge % of near-zero market value) and pct < 1.5 (outlier cap).
    Requires N >= 10 per arb year; falls back to defaults otherwise.
    """
    year_lo = game_year - CALIBRATION_YEARS
    year_hi = game_year - 1

    rows = conn.execute("""
        SELECT c.player_id, p.age, c.salary_0, r.ovr
        FROM contracts c
        JOIN players p ON c.player_id = p.player_id
        JOIN latest_ratings r ON r.player_id = p.player_id
        WHERE c.years = 1 AND c.salary_0 > ? AND c.salary_0 < 20000000
          AND p.age < 30 AND p.level = 1
    """, (_cfg.minimum_salary,)).fetchall()

    arb_data = defaultdict(list)
    for r in rows:
        pid = r["player_id"]
        svc = conn.execute("""
            SELECT COUNT(DISTINCT year) FROM (
                SELECT year FROM mlb_batting_stats WHERE player_id=? AND split_id=1 AND ab >= 100
                UNION
                SELECT year FROM mlb_pitching_stats WHERE player_id=? AND split_id=1 AND ip >= 20
            )
        """, (pid, pid)).fetchone()[0]

        # Get prior year WAR
        bat = conn.execute(
            "SELECT SUM(war) as war FROM mlb_batting_stats WHERE player_id=? AND split_id=1 AND year=? AND ab >= 100",
            (pid, game_year - 1)).fetchone()
        pit = conn.execute(
            "SELECT SUM((war + COALESCE(ra9war, war))/2.0) as war FROM mlb_pitching_stats WHERE player_id=? AND split_id=1 AND year=? AND ip >= 20",
            (pid, game_year - 1)).fetchone()

        war = (pit["war"] if pit and pit["war"] is not None else
               bat["war"] if bat and bat["war"] is not None else None)
        if war is None or war < 1.0:
            continue

        mkt = war * dpw
        pct = r["salary_0"] / mkt
        arb_yr = max(1, svc - 2)
        if 1 <= arb_yr <= 3 and pct < 1.5:
            arb_data[arb_yr].append(pct)

    # Use median (robust to outliers), require N >= 10
    import statistics
    result = {}
    for yr in (1, 2, 3):
        pcts = arb_data.get(yr, [])
        if len(pcts) >= 10:
            result[yr] = round(statistics.median(pcts), 2)
        else:
            result[yr] = ARB_PCT[yr]

    # Enforce monotonic: arb 1 <= arb 2 <= arb 3
    if result[1] > result[2]:
        result[1] = result[2]
    if result[2] > result[3]:
        result[2] = result[3]

    return result


# ---------------------------------------------------------------------------
# Step 3b: Perpetual arb salary model calibration
# ---------------------------------------------------------------------------

def _calibrate_arb_salary_model(conn, game_year, dpw):
    """Calibrate the growth+ceiling arb salary model for perpetual arb leagues.

    Fits parameters from actual 1-year contract data:
      salary = min(growth, ceiling), floor at min_sal
      growth  = min_sal + k × max(0, career_WAR - discount)^exp
      ceiling = ceiling_pct × current_WAR × $/WAR

    Only uses players on 1-year contracts (true arb, not long-term deals).
    Requires WAR >= 0.5 and at least 30 qualifying players.

    Returns dict with keys: k, exp, discount, ceiling_pct, calibration_n.
    Returns None if insufficient data.
    """
    import math
    import statistics

    min_sal = _cfg.minimum_salary

    # Gather 1-year contract players with career WAR data
    rows = conn.execute("""
        SELECT p.player_id, p.age, c.salary_0
        FROM players p
        JOIN contracts c ON p.player_id = c.player_id
        WHERE p.level = '1' AND c.years = 1
    """).fetchall()

    data = []
    for r in rows:
        pid = r["player_id"]
        sal = r["salary_0"] or 0
        if sal < min_sal:
            continue

        # Career WAR (sum of all seasons)
        career_bat = conn.execute(
            "SELECT COALESCE(SUM(war), 0) FROM mlb_batting_stats WHERE player_id=? AND split_id=1",
            (pid,)).fetchone()[0]
        career_pit = conn.execute(
            "SELECT COALESCE(SUM((war + COALESCE(ra9war, war))/2.0), 0) FROM mlb_pitching_stats WHERE player_id=? AND split_id=1",
            (pid,)).fetchone()[0]
        career_war = (career_bat or 0) + (career_pit or 0)

        # Prior year WAR (for ceiling fitting)
        prior_bat = conn.execute(
            "SELECT COALESCE(SUM(war), 0) FROM mlb_batting_stats WHERE player_id=? AND split_id=1 AND year=?",
            (pid, game_year - 1)).fetchone()[0]
        prior_pit = conn.execute(
            "SELECT COALESCE(SUM((war + COALESCE(ra9war, war))/2.0), 0) FROM mlb_pitching_stats WHERE player_id=? AND split_id=1 AND year=?",
            (pid, game_year - 1)).fetchone()[0]
        prior_war = max((prior_bat or 0), (prior_pit or 0))

        if career_war < 0.5:
            continue

        data.append({
            "salary": sal, "career_war": career_war,
            "prior_war": prior_war, "age": r["age"],
        })

    if len(data) < 30:
        return None

    # --- Fit growth parameters via log-log regression ---
    # Model: salary - min_sal = k × max(0, career_war - discount)^exp
    # Try discount values and pick the one with best fit

    best_r2 = -1
    best_params = None

    for discount_try in [3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0]:
        log_data = []
        for d in data:
            effective = d["career_war"] - discount_try
            excess_sal = d["salary"] - min_sal
            if effective > 0.5 and excess_sal > 0:
                log_data.append((math.log(effective), math.log(excess_sal)))

        if len(log_data) < 20:
            continue

        # Linear regression on log-log: log(sal - min) = log(k) + exp × log(eff_war)
        n = len(log_data)
        sx = sum(x for x, y in log_data)
        sy = sum(y for x, y in log_data)
        sxy = sum(x * y for x, y in log_data)
        sxx = sum(x * x for x, y in log_data)

        denom = n * sxx - sx * sx
        if denom == 0:
            continue

        exp_fit = (n * sxy - sx * sy) / denom
        log_k_fit = (sy - exp_fit * sx) / n
        k_fit = math.exp(log_k_fit)

        # R² calculation
        y_mean = sy / n
        ss_tot = sum((y - y_mean) ** 2 for x, y in log_data)
        ss_res = sum((y - (log_k_fit + exp_fit * x)) ** 2 for x, y in log_data)
        r2 = 1 - ss_res / ss_tot if ss_tot > 0 else 0

        if r2 > best_r2:
            best_r2 = r2
            best_params = (k_fit, exp_fit, discount_try)

    if best_params is None:
        return None

    k, exp, discount = best_params

    # Sanity clamp: exp should be 0.5-1.5, k > 0
    exp = max(0.5, min(1.5, exp))
    k = max(100, k)

    # --- Fit ceiling_pct ---
    # For established players (career_war > 15, prior_war > 2), salary should be
    # hitting the ceiling. ceiling_pct = median(salary / (prior_war × dpw))
    ceiling_data = [d["salary"] / (d["prior_war"] * dpw)
                    for d in data
                    if d["career_war"] > 15 and d["prior_war"] > 2.0 and d["prior_war"] * dpw > 0]

    if len(ceiling_data) >= 10:
        ceiling_pct = statistics.median(ceiling_data)
        ceiling_pct = max(0.20, min(0.80, ceiling_pct))  # sanity clamp
    else:
        ceiling_pct = 0.35  # fallback

    return {
        "k": round(k, 1),
        "exp": round(exp, 3),
        "discount": round(discount, 1),
        "ceiling_pct": round(ceiling_pct, 3),
        "calibration_n": len(data),
        "calibration_r2": round(best_r2, 4),
    }


# ---------------------------------------------------------------------------
# Step 4: Scarcity curve
# ---------------------------------------------------------------------------

def _calibrate_scarcity(conn, game_date):
    """Compute scarcity multiplier by Pot band. Mid-season only.
    
    Measures how concentrated talent is at each Pot level among rostered
    players (team_id > 0). Uses the fraction of rostered players at each
    Pot who are NOT on MLB rosters as a proxy for availability — low-Pot
    talent is abundant in the minors, high-Pot talent gets absorbed into MLB.
    
    Compares each band's non-MLB rate to the baseline (Pot 38-42) and maps
    through a sigmoid. Adapts to leagues with different roster structures.
    Returns None during offseason.
    """
    game_month = int(game_date[5:7])
    if game_month < 4 or game_month > 10:
        print("  Scarcity: skipped (offseason — FA pool is flooded)")
        return None

    rows = conn.execute("""
        SELECT r.pot,
               SUM(CASE WHEN p.level != 1 THEN 1 ELSE 0 END) as non_mlb,
               COUNT(*) as total
        FROM latest_ratings r
        JOIN players p ON r.player_id = p.player_id
        WHERE r.pot >= 38 AND p.team_id > 0 AND p.age BETWEEN 18 AND 32
        GROUP BY r.pot ORDER BY r.pot
    """).fetchall()

    if not rows:
        return None

    # Group into 2-point bands for smoothing
    bands = defaultdict(lambda: [0, 0])
    for r in rows:
        center = (r["pot"] // 2) * 2
        bands[center][0] += r["non_mlb"]
        bands[center][1] += r["total"]

    avail_rates = {}
    for center in sorted(bands.keys()):
        non_mlb, total = bands[center]
        if total >= 15:
            avail_rates[center] = non_mlb / total

    if not avail_rates:
        return None

    # Baseline: average non-MLB rate at Pot 38-42 (abundant talent)
    baseline_pts = [v for k, v in avail_rates.items() if k <= 42]
    baseline = sum(baseline_pts) / len(baseline_pts) if baseline_pts else 0.95

    # Map ratio-to-baseline through a sigmoid:
    # ratio ~1.0 (as available as low-Pot talent) → scarcity 0.0
    # ratio ~0.65 → scarcity ~0.5
    # ratio ~0.3 → scarcity ~0.95
    import math
    def _ratio_to_scarcity(ratio):
        if ratio <= 0:
            return 1.0
        return max(0.0, min(1.0, 1.0 / (1.0 + math.exp(10 * (ratio - 0.65)))))

    raw = {}
    for pot in sorted(avail_rates.keys()):
        ratio = avail_rates[pot] / baseline if baseline > 0 else 0
        raw[pot] = round(_ratio_to_scarcity(ratio), 2)

    # Enforce monotonic non-decreasing
    pts = sorted(raw.keys())
    scarcity = {}
    prev = 0.0
    for pot in pts:
        val = max(prev, raw[pot])
        scarcity[pot] = val
        prev = val

    # Find where scarcity first hits 1.0 and cap
    result = {}
    hit_one = False
    for pot in pts:
        if hit_one:
            continue
        result[pot] = scarcity[pot]
        if scarcity[pot] >= 1.0:
            hit_one = True
    result[80] = 1.0

    return result


# ---------------------------------------------------------------------------
# Step 5b: Carrying tool calibration
# ---------------------------------------------------------------------------

# Offensive tools eligible for carrying tool analysis (speed excluded per Req 8.5)
_OFFENSIVE_TOOLS = ("contact", "gap", "power", "eye")
# DB column names corresponding to each offensive tool
_TOOL_DB_COLS = {"contact": "cntct", "gap": "gap", "power": "pow", "eye": "eye"}
# Minimum qualifying players with 65+ grade for a position/tool combo (Req 8.4)
_MIN_CARRYING_TOOL_N = 10


def _calibrate_carrying_tools(conn, game_year, role_map):
    """Derive carrying tool parameters from WAR regression data.

    For each position/tool combination:
    1. Compute P85 threshold for that tool at that position (top ~15%).
    2. Compute mean WAR for players at or above the threshold.
    3. Compute mean WAR for all players at that position.
    4. WAR premium = difference.
    5. Compute scarcity percentage (% of players above threshold).

    Using a percentile-based threshold adapts to league-specific tool
    distributions (e.g. VMLB has tighter distributions than EMLB).

    Excludes speed at all positions. Excludes combinations with fewer
    than 10 qualifying players.

    Args:
        conn: SQLite connection.
        game_year: Current game year.
        role_map: Role mapping dict.

    Returns:
        Carrying tool config dict, or None if insufficient data.
    """
    year_lo = game_year - CALIBRATION_YEARS - 1  # exclusive lower bound
    year_hi = game_year - 1  # inclusive upper bound

    rows = conn.execute("""
        SELECT r.player_id, r.cntct, r.gap, r.pow, r.eye,
               r.pot_c, r.pot_ss, r.pot_second_b, r.pot_third_b, r.pot_first_b,
               r.pot_lf, r.pot_cf, r.pot_rf,
               r.c, r.ss, r.second_b, r.third_b, r.first_b, r.lf, r.cf, r.rf,
               r.stm, r.ovr, r.pot,
               r.pot_fst, r.pot_snk, r.pot_crv, r.pot_sld, r.pot_chg,
               r.pot_splt, r.pot_cutt, r.pot_cir_chg, r.pot_scr, r.pot_frk,
               r.pot_kncrv, r.pot_knbl,
               p.age, p.pos, p.role,
               bs.war
        FROM latest_ratings r
        JOIN players p ON r.player_id = p.player_id
        JOIN mlb_batting_stats bs ON bs.player_id = p.player_id
        WHERE p.level = 1 AND bs.split_id = 1
          AND bs.year > ? AND bs.year <= ? AND bs.ab >= 300
    """, (year_lo, year_hi)).fetchall()

    # Group players by position bucket, collecting tool grades and WAR
    # bucket -> list of {"contact": int, "gap": int, ..., "war": float}
    bucket_players = defaultdict(list)
    for r in rows:
        bucket = _bucket_player(r, role_map)
        if bucket not in HITTER_BUCKETS:
            continue

        contact = norm(r["cntct"])
        gap = norm(r["gap"])
        power = norm(r["pow"])
        eye = norm(r["eye"])
        war = r["war"]

        if war is None:
            continue

        bucket_players[bucket].append({
            "contact": contact,
            "gap": gap,
            "power": power,
            "eye": eye,
            "war": float(war),
        })

    # For each position/tool combo, compute WAR premium and scarcity
    positions_config = {}
    total_combos = 0

    for bucket in HITTER_BUCKETS:
        players = bucket_players.get(bucket, [])
        if not players:
            continue

        # Position mean WAR (all players at this position)
        all_wars = [p["war"] for p in players]
        pos_mean_war = sum(all_wars) / len(all_wars)
        total_at_pos = len(players)

        carrying_tools = {}
        for tool in _OFFENSIVE_TOOLS:
            # Dynamic threshold: P85 of tool distribution at this position
            # (top ~15% = "elite" for this league's scale)
            tool_vals = sorted(
                [p[tool] for p in players if p[tool] is not None]
            )
            if len(tool_vals) < _MIN_CARRYING_TOOL_N:
                continue
            threshold = tool_vals[int(len(tool_vals) * 0.85)]

            qualified = [p for p in players
                         if p[tool] is not None and p[tool] >= threshold]
            n_qualified = len(qualified)

            if n_qualified < _MIN_CARRYING_TOOL_N:
                continue

            # Mean WAR for players above threshold
            tool_mean_war = sum(p["war"] for p in qualified) / n_qualified

            # WAR premium = difference from position mean
            war_premium = tool_mean_war - pos_mean_war

            # Skip if premium is zero or negative (tool doesn't help)
            if war_premium <= 0:
                continue

            # Scarcity: % of players at position above threshold
            scarcity_pct = n_qualified / total_at_pos

            # Convert raw WAR premium to war_premium_factor
            # Factor = raw_war_premium / 5.0 (scaling to 20-80 scouting scale)
            war_premium_factor = round(war_premium / 5.0, 2)

            carrying_tools[tool] = {
                "war_premium_factor": war_premium_factor,
                "_calibration": {
                    "threshold": int(threshold),
                    "n_qualified": n_qualified,
                    "n_total": total_at_pos,
                    "war_premium_raw": round(war_premium, 3),
                    "scarcity_pct": round(scarcity_pct, 3),
                    "tool_mean_war": round(tool_mean_war, 3),
                    "pos_mean_war": round(pos_mean_war, 3),
                },
            }
            total_combos += 1

        if carrying_tools:
            positions_config[bucket] = {"carrying_tools": carrying_tools}

    if total_combos == 0:
        return None

    config = {
        "version": 1,
        "source": "calibrated",
        "positions": positions_config,
        "scarcity_schedule": [
            {"threshold": 65, "multiplier": 1.0},
            {"threshold": 70, "multiplier": 1.5},
            {"threshold": 75, "multiplier": 2.0},
            {"threshold": 80, "multiplier": 3.0},
        ],
    }

    return config


# ---------------------------------------------------------------------------
# Positional rating estimation from defensive tools
# ---------------------------------------------------------------------------

# For each position, which defensive tools predict the positional grade.
_POS_MODEL_FEATURES = {
    "pot_ss":       ["ifr", "ifa", "ife", "tdp"],
    "pot_second_b": ["ifr", "ifa", "ife", "tdp"],
    "pot_third_b":  ["ifr", "ifa", "ife", "tdp"],
    "pot_cf":       ["ofr", "ofa", "ofe"],
    "pot_lf":       ["ofr", "ofa", "ofe"],
    "pot_rf":       ["ofr", "ofa", "ofe"],
    "pot_first_b":  ["ifr", "ifa", "ife", "tdp", "height"],
    "pot_c":        ["c_arm", "c_blk", "c_frm"],
}


def _multivariate_ols(X, y):
    """Solve multivariate OLS via normal equations with Gaussian elimination.
    X should already include the intercept column (column of 1s).
    Returns coefficient vector beta."""
    n = len(y)
    k = len(X[0])
    # Build augmented matrix [X^T X | X^T y]
    XtX = [[sum(X[i][a] * X[i][b] for i in range(n)) for b in range(k)] for a in range(k)]
    Xty = [sum(X[i][a] * y[i] for i in range(n)) for a in range(k)]
    A = [XtX[i][:] + [Xty[i]] for i in range(k)]
    # Gaussian elimination with partial pivoting
    for col in range(k):
        max_row = max(range(col, k), key=lambda r: abs(A[r][col]))
        A[col], A[max_row] = A[max_row], A[col]
        if abs(A[col][col]) < 1e-10:
            continue
        for row in range(col + 1, k):
            f = A[row][col] / A[col][col]
            for j in range(col, k + 1):
                A[row][j] -= f * A[col][j]
    # Back-substitution
    beta = [0.0] * k
    for i in range(k - 1, -1, -1):
        beta[i] = A[i][k]
        for j in range(i + 1, k):
            beta[i] -= A[i][j] * beta[j]
        if abs(A[i][i]) > 1e-10:
            beta[i] /= A[i][i]
    return beta


def _calibrate_positional_models(conn):
    """Fit OLS models predicting positional ratings from defensive tools.

    Returns dict: {position: {"features": [...], "coefficients": [...], "r2": float, "n": int}}
    """
    models = {}
    for pos_col, features in _POS_MODEL_FEATURES.items():
        feat_sql = ", ".join(features)
        # Only train on players who have a rating at this position AND have defensive tools
        rows = conn.execute(
            f"SELECT {pos_col}, {feat_sql} FROM latest_ratings "
            f"WHERE {pos_col} > 0 AND {features[0]} > 0"
        ).fetchall()
        if len(rows) < 30:
            continue
        y = [float(r[0]) for r in rows]
        X = [[1.0] + [float(r[i + 1]) for i in range(len(features))] for r in rows]
        beta = _multivariate_ols(X, y)
        # Compute R² and MAE
        pred = [sum(beta[j] * X[i][j] for j in range(len(beta))) for i in range(len(X))]
        mean_y = sum(y) / len(y)
        ss_res = sum((y[i] - pred[i]) ** 2 for i in range(len(y)))
        ss_tot = sum((y[i] - mean_y) ** 2 for i in range(len(y)))
        r2 = 1 - ss_res / ss_tot if ss_tot > 0 else 0
        mae = sum(abs(y[i] - pred[i]) for i in range(len(y))) / len(y)
        models[pos_col] = {
            "features": features,
            "coefficients": beta,  # [intercept, coeff1, coeff2, ...]
            "r2": round(r2, 4),
            "mae": round(mae, 2),
            "n": len(rows),
        }
    return models


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def calibrate(dry_run=False):
    league_dir = get_league_dir()
    conn = _get_connection(league_dir)

    with open(league_dir / "config" / "state.json") as f:
        state = json.load(f)
    game_date = state["game_date"]
    game_year = int(game_date[:4])

    with open(league_dir / "config" / "league_averages.json") as f:
        avgs = json.load(f)
    dpw = avgs.get("dollar_per_war", DEFAULT_DOLLARS_PER_WAR)

    role_map = {str(k): v for k, v in _cfg.role_map.items()}

    print(f"Calibrating model weights for game date {game_date}")
    print(f"  Using {CALIBRATION_YEARS} years of data ({game_year - CALIBRATION_YEARS}-{game_year - 1})")
    print(f"  $/WAR: ${dpw:,.0f}")
    print()

    # Step 0: Tool weight regression (NEW)
    print("=== Tool Weight Regression (Step 0) ===")
    tool_weights = _calibrate_tool_weights(conn, game_year, role_map)
    if tool_weights:
        for bucket in list(HITTER_BUCKETS):
            n = tool_weights.get("calibration_n", {}).get(bucket, 0)
            r2_info = tool_weights.get("calibration_r2", {}).get(bucket, {})
            r2_str = ", ".join(f"{k}={v:.3f}" for k, v in r2_info.items()) if r2_info else "defaults"
            print(f"  {bucket:<4} N={n:<5} R²: {r2_str}")
        for role in PITCHER_BUCKETS:
            n = tool_weights.get("calibration_n", {}).get(role, 0)
            r2_info = tool_weights.get("calibration_r2", {}).get(role, {})
            r2_str = ", ".join(f"{k}={v:.3f}" for k, v in r2_info.items()) if r2_info else "defaults"
            print(f"  {role:<4} N={n:<5} R²: {r2_str}")

        if not dry_run:
            tw_path = league_dir / "config" / "tool_weights.json"
            with open(tw_path, "w") as f:
                json.dump(tool_weights, f, indent=2)
            print(f"  Wrote {tw_path}")
    else:
        print("  No tool weight data available — using defaults")
    print()

    # Development curves (gap closure, age runway, expected gaps)
    print("=== Development Curves ===")
    dev_curves = _calibrate_development_curves(conn)
    if dev_curves:
        for label in ("hitter", "pitcher"):
            n_data = dev_curves.get(f"calibration_n_{label}", {})
            total_n = sum(n_data.values())
            closure = dev_curves[f"gap_closure_{label}"]
            eg = dev_curves[f"expected_gap_{label}"]
            ages = sorted(closure.keys())
            cl_str = " ".join(f"{a}:{closure[a]:.2f}" for a in ages if a in (17, 20, 22, 24))
            eg_str = " ".join(f"{a}:{eg[a]}" for a in ages if a in (17, 20, 22, 24))
            print(f"  {label:<7} N={total_n:<6} closure=[{cl_str}]  gaps=[{eg_str}]")
    else:
        print("  Insufficient data — using hardcoded defaults")
    print()

    # Years-to-MLB by level
    print("=== Years to MLB ===")
    years_to_mlb = _calibrate_years_to_mlb(conn)
    if years_to_mlb:
        for lvl in ["AAA", "AA", "A", "Rookie", "Intl", "DSL"]:
            yrs = years_to_mlb.get(lvl)
            if yrs is not None:
                print(f"  {lvl:<8} {yrs:.1f} yrs")
    else:
        print("  Insufficient data — using hardcoded defaults")
    print()

    # Step 1: OVR_TO_WAR
    print("=== OVR_TO_WAR Regression ===")
    regressions, bucket_data = _calibrate_ovr_to_war(conn, game_year, role_map)
    ovr_table = _build_ovr_to_war_table(regressions)

    for bucket in list(HITTER_BUCKETS) + list(PITCHER_BUCKETS):
        reg = regressions.get(bucket)
        if reg:
            n_str = reg[3] if isinstance(reg[3], str) else reg[3]
            print(f"  {bucket:<4} N={n_str:<5} slope={reg[0]:.4f} R²={reg[2]:.3f}  "
                  f"WAR@50={ovr_table[bucket][50]:.2f}  @60={ovr_table[bucket][60]:.2f}  "
                  f"@70={ovr_table[bucket][70]:.2f}")
        else:
            print(f"  {bucket:<4} (using defaults — insufficient data)")

    # Step 2: FV_TO_PEAK_WAR
    print("\n=== FV_TO_PEAK_WAR (derived) ===")
    hitter_fv, hitter_fv_tables, sp_fv, rp_fv = _derive_fv_to_peak_war(ovr_table)
    print(f"  {'FV':<4} {'HitAvg':>7} {'COF':>5} {'SS':>5} {'C':>5} {'CF':>5} {'SP':>7} {'RP':>7}")
    for fv in sorted(hitter_fv.keys(), reverse=True):
        cof = hitter_fv_tables.get("COF", {}).get(fv, "?")
        ss  = hitter_fv_tables.get("SS", {}).get(fv, "?")
        c   = hitter_fv_tables.get("C", {}).get(fv, "?")
        cf  = hitter_fv_tables.get("CF", {}).get(fv, "?")
        cof_s = f"{cof:>5.1f}" if isinstance(cof, float) else f"{cof:>5}"
        ss_s  = f"{ss:>5.1f}" if isinstance(ss, float) else f"{ss:>5}"
        c_s   = f"{c:>5.1f}" if isinstance(c, float) else f"{c:>5}"
        cf_s  = f"{cf:>5.1f}" if isinstance(cf, float) else f"{cf:>5}"
        print(f"  {fv:<4} {hitter_fv[fv]:>7.1f} {cof_s} {ss_s} {c_s} {cf_s} {sp_fv[fv]:>7.1f} {rp_fv[fv]:>7.1f}")

    # Step 3: ARB_PCT
    print("\n=== ARB_PCT ===")
    arb_pct = _calibrate_arb_pct(conn, game_year, dpw)
    for yr in (1, 2, 3):
        old = ARB_PCT[yr]
        print(f"  Arb {yr}: {arb_pct[yr]:.0%} (was {old:.0%})")

    # Step 3b: Perpetual arb salary model (only for perpetual arb leagues)
    arb_salary_model = None
    if _cfg.perpetual_arb:
        print("\n=== ARB_SALARY_MODEL (perpetual arb) ===")
        arb_salary_model = _calibrate_arb_salary_model(conn, game_year, dpw)
        if arb_salary_model:
            print(f"  k={arb_salary_model['k']:.0f}, exp={arb_salary_model['exp']:.3f}, "
                  f"discount={arb_salary_model['discount']:.1f}, "
                  f"ceiling={arb_salary_model['ceiling_pct']:.0%}")
            print(f"  N={arb_salary_model['calibration_n']}, R²={arb_salary_model['calibration_r2']:.4f}")
        else:
            print("  Insufficient data — using defaults")

    # Step 4: Scarcity
    print("\n=== SCARCITY_MULT ===")
    scarcity = _calibrate_scarcity(conn, game_date)
    if scarcity:
        for pot in sorted(scarcity.keys()):
            old = SCARCITY_MULT.get(pot, "—")
            print(f"  Pot {pot}: {scarcity[pot]:.2f} (was {old})")
    else:
        print("  Using existing curve (no update)")
        scarcity = {str(k): v for k, v in SCARCITY_MULT.items()}

    # Step: Positional rating estimation models (before conn closes)
    print("\n=== Positional Rating Models ===")
    pos_models = _calibrate_positional_models(conn)
    if pos_models:
        for pos_col, model in pos_models.items():
            print(f"  {pos_col:<14} R²={model['r2']:.3f}  MAE={model['mae']:.1f}  N={model['n']}")
    else:
        print("  Insufficient data — skipped")

    conn.close()

    # Step 5: PAP scale (2× stdev of surplus_yr1)
    pap_scale = 25_000_000  # fallback
    conn2 = _get_connection(league_dir)
    yr1_rows = conn2.execute(
        "SELECT surplus_yr1 FROM player_surplus WHERE surplus_yr1 IS NOT NULL AND eval_date=?",
        (game_date,)).fetchall()
    conn2.close()
    if len(yr1_rows) >= 30:
        import statistics
        pap_scale = round(2 * statistics.stdev(r[0] for r in yr1_rows))
    print(f"\n=== PAP_SCALE ===")
    print(f"  N={len(yr1_rows)}  scale=${pap_scale/1e6:.1f}M")

    # Step: COMPOSITE_TO_WAR regression (skipped on first run)
    print("\n=== COMPOSITE_TO_WAR Regression ===")
    conn3 = _get_connection(league_dir)
    comp_regressions, comp_bucket_data = _calibrate_composite_to_war(conn3, game_year, role_map)
    conn3.close()

    composite_ovr_table = None
    comp_hitter_fv = None
    comp_hitter_fv_tables = None
    comp_sp_fv = None
    comp_rp_fv = None

    if comp_regressions is not None:
        composite_ovr_table = _build_ovr_to_war_table(comp_regressions)
        for bucket in list(HITTER_BUCKETS) + list(PITCHER_BUCKETS):
            reg = comp_regressions.get(bucket)
            if reg:
                n_str = reg[3] if isinstance(reg[3], str) else reg[3]
                print(f"  {bucket:<4} N={n_str:<5} slope={reg[0]:.4f} R²={reg[2]:.3f}  "
                      f"WAR@50={composite_ovr_table[bucket][50]:.2f}  @60={composite_ovr_table[bucket][60]:.2f}  "
                      f"@70={composite_ovr_table[bucket][70]:.2f}")
            else:
                print(f"  {bucket:<4} (using OVR_TO_WAR fallback)")

        # Derive FV_TO_PEAK_WAR_COMPOSITE tables
        comp_hitter_fv, comp_hitter_fv_tables, comp_sp_fv, comp_rp_fv = _derive_fv_to_peak_war(composite_ovr_table)
    else:
        print("  Skipped — composite_score data insufficient (first run)")

    # Step 6: Carrying tool calibration
    print("\n=== CARRYING_TOOL_CONFIG ===")
    conn4 = _get_connection(league_dir)
    ct_config = _calibrate_carrying_tools(conn4, game_year, role_map)
    conn4.close()

    if ct_config is not None:
        positions = ct_config["positions"]
        for bucket in HITTER_BUCKETS:
            pos_data = positions.get(bucket)
            if pos_data:
                tools_info = []
                for tool, tool_data in pos_data["carrying_tools"].items():
                    cal = tool_data.get("_calibration", {})
                    n_q = cal.get("n_qualified", "?")
                    factor = tool_data["war_premium_factor"]
                    scarcity_pct = cal.get("scarcity_pct", 0)
                    tools_info.append(f"{tool}(f={factor:.2f}, N={n_q}, sc={scarcity_pct:.1%})")
                print(f"  {bucket:<4} {', '.join(tools_info)}")
            else:
                print(f"  {bucket:<4} (no qualifying tools)")

        if not dry_run:
            ct_path = league_dir / "config" / "carrying_tool_config.json"
            with open(ct_path, "w") as f:
                json.dump(ct_config, f, indent=2)
            print(f"  Wrote {ct_path}")
    else:
        print("  No carrying tool data available — using defaults")

    # Build output
    # OVR_TO_WAR stored as position-specific dicts for flexibility
    weights = {
        "calibration_date": game_date,
        "calibration_years": f"{game_year - CALIBRATION_YEARS}-{game_year - 1}",
        "OVR_TO_WAR": {bucket: {str(k): v for k, v in tbl.items()}
                       for bucket, tbl in ovr_table.items()},
        "FV_TO_PEAK_WAR": {str(k): v for k, v in hitter_fv.items()},
        "FV_TO_PEAK_WAR_BY_POS": {bucket: {str(k): v for k, v in tbl.items()}
                                   for bucket, tbl in hitter_fv_tables.items()},
        "FV_TO_PEAK_WAR_SP": {str(k): v for k, v in sp_fv.items()},
        "FV_TO_PEAK_WAR_RP": {str(k): v for k, v in rp_fv.items()},
        "ARB_PCT": {str(k): v for k, v in arb_pct.items()},
        "SCARCITY_MULT": {str(k): v for k, v in
                          (scarcity if scarcity else SCARCITY_MULT).items()},
        "PAP_SCALE": pap_scale,
    }

    # Add perpetual arb salary model when calibrated
    if arb_salary_model:
        weights["ARB_SALARY_MODEL"] = arb_salary_model

    # Add development curves when available
    if dev_curves:
        for key in ("gap_closure_hitter", "gap_closure_pitcher",
                     "age_runway_hitter", "age_runway_pitcher",
                     "expected_gap_hitter", "expected_gap_pitcher"):
            weights[key] = {str(k): v for k, v in dev_curves[key].items()}

    # Add years-to-MLB when available
    if years_to_mlb:
        weights["YEARS_TO_MLB"] = years_to_mlb

    # Add COMPOSITE_TO_WAR tables when available
    if composite_ovr_table is not None:
        weights["COMPOSITE_TO_WAR"] = {
            bucket: {str(k): v for k, v in tbl.items()}
            for bucket, tbl in composite_ovr_table.items()
        }
    if comp_hitter_fv is not None:
        weights["FV_TO_PEAK_WAR_COMPOSITE"] = {str(k): v for k, v in comp_hitter_fv.items()}
    if comp_hitter_fv_tables is not None:
        weights["FV_TO_PEAK_WAR_COMPOSITE_BY_POS"] = {
            bucket: {str(k): v for k, v in tbl.items()}
            for bucket, tbl in comp_hitter_fv_tables.items()
        }
    if comp_sp_fv is not None:
        weights["FV_TO_PEAK_WAR_COMPOSITE_SP"] = {str(k): v for k, v in comp_sp_fv.items()}
    if comp_rp_fv is not None:
        weights["FV_TO_PEAK_WAR_COMPOSITE_RP"] = {str(k): v for k, v in comp_rp_fv.items()}

    # Store positional models (calibrated above before conn.close())
    if pos_models:
        weights["POSITIONAL_MODELS"] = pos_models

    if dry_run:
        print("\n=== DRY RUN — would write: ===")
        print(json.dumps(weights, indent=2))
    else:
        out_path = league_dir / "config" / "model_weights.json"
        with open(out_path, "w") as f:
            json.dump(weights, f, indent=2)
        print(f"\nWrote {out_path}")

    return weights


def main():
    from statsplusplus.utils.logging import setup_logging
    setup_logging(Path(_PROJECT_ROOT) / "data" / "logs")
    dry_run = "--dry-run" in sys.argv
    calibrate(dry_run=dry_run)


if __name__ == "__main__":
    main()
