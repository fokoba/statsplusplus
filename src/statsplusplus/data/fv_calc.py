"""League-wide FV and surplus calculation pipeline.

Prospects (non-MLB, age ≤ 24): FV → prospect_fv table
MLB players: surplus value → player_surplus table

This module orchestrates the batch evaluation of all players in a league.
It reads ratings from the DB, calls the evaluation functions from the package,
and writes results back.

Usage:
    python3 -m statsplusplus.data.fv_calc
    python3 scripts/fv_calc.py
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

# Level mappings
LEVEL_INT_KEY = {0: "draft", 2: "aaa", 3: "aa", 4: "a", 5: "a-short", 6: "usl", 8: "intl", 10: "draft", 11: "draft"}
LEVEL_INT_LABEL = {0: "Draft", 1: "MLB", 2: "AAA", 3: "AA", 4: "A", 5: "A-Short", 6: "Rookie", 8: "International", 10: "College", 11: "HS"}

RATINGS_SQL = """
    SELECT r.player_id AS ID,
           p.name AS Name, p.age AS Age, p.team_id, p.parent_team_id, p.level, p.pos, p.role,
           r.ovr AS Ovr, r.pot AS Pot,
           r.composite_score, r.ceiling_score, r.secondary_composite,
           r.cntct AS Cntct, r.gap AS Gap, r.pow AS Pow, r.eye AS Eye, r.ks AS Ks,
           r.speed AS Speed, r.steal AS Steal,
           r.stf AS Stf, r.mov AS Mov, r.ctrl AS Ctrl, r.ctrl_r AS Ctrl_R, r.ctrl_l AS Ctrl_L,
           r.fst AS Fst, r.snk AS Snk, r.crv AS Crv, r.sld AS Sld, r.chg AS Chg,
           r.splt AS Splt, r.cutt AS Cutt, r.cir_chg AS CirChg, r.scr AS Scr,
           r.frk AS Frk, r.kncrv AS Kncrv, r.knbl AS Knbl, r.stm AS Stm, r.vel AS Vel,
           r.pot_stf AS PotStf, r.pot_mov AS PotMov, r.pot_ctrl AS PotCtrl,
           r.pot_fst AS PotFst, r.pot_snk AS PotSnk, r.pot_crv AS PotCrv,
           r.pot_sld AS PotSld, r.pot_chg AS PotChg, r.pot_splt AS PotSplt,
           r.pot_cutt AS PotCutt, r.pot_cir_chg AS PotCirChg, r.pot_scr AS PotScr,
           r.pot_frk AS PotFrk, r.pot_kncrv AS PotKncrv, r.pot_knbl AS PotKnbl,
           r.pot_cntct AS PotCntct, r.pot_gap AS PotGap, r.pot_pow AS PotPow,
           r.pot_eye AS PotEye, r.pot_ks AS PotKs,
           r.c AS C, r.ss AS SS, r.second_b AS "2B", r.third_b AS "3B",
           r.first_b AS "1B", r.lf AS LF, r.cf AS CF, r.rf AS RF,
           r.pot_c AS PotC, r.pot_ss AS PotSS, r.pot_second_b AS Pot2B,
           r.pot_third_b AS Pot3B, r.pot_first_b AS Pot1B,
           r.pot_lf AS PotLF, r.pot_cf AS PotCF, r.pot_rf AS PotRF,
           r.ofa AS OFA, r.ifa AS IFA, r.c_arm AS CArm, r.c_blk AS CBlk, r.c_frm AS CFrm,
           r.ifr AS IFR, r.ofr AS OFR, r.ife AS IFE, r.ofe AS OFE, r.tdp AS TDP,
           r.height AS Height,
           r.cntct_l AS Cntct_L, r.cntct_r AS Cntct_R,
           r.stf_l AS Stf_L, r.stf_r AS Stf_R,
           r.int_ AS Int, r.wrk_ethic AS WrkEthic, r.greed AS Greed,
           r.loy AS Loy, r.lead AS Lead, r.acc AS Acc,
           r.league_id AS LeagueId,
           r.offensive_grade, r.baserunning_value, r.defensive_value,
           r.durability_score, r.offensive_ceiling, r.true_ceiling
    FROM ratings r
    JOIN players p ON r.player_id = p.player_id
    WHERE r.snapshot_date = (
        SELECT MAX(r2.snapshot_date) FROM ratings r2 WHERE r2.player_id = r.player_id
    )
"""


def run(league_dir: Path | None = None) -> None:
    """Run the full FV/surplus calculation pipeline.

    Args:
        league_dir: Path to the league data directory. If None, resolves
            from the active league in app_config.json.
    """
    # Lazy imports to allow both package and legacy invocation
    import os
    _base = Path(__file__).resolve().parent.parent.parent.parent
    if str(_base / "scripts") not in sys.path:
        sys.path.insert(0, str(_base / "scripts"))

    from statsplusplus.data.db import get_connection as _get_conn, init_schema as _init_schema
    from statsplusplus.config.league_config import LeagueConfig
    from statsplusplus.utils.positions import assign_bucket, LEVEL_NORM_AGE
    from statsplusplus.evaluation.fv import calc_fv_from_dict as calc_fv
    from statsplusplus.config.league_config import dollars_per_war as _dpw_fn, league_minimum as _lm_fn
    from statsplusplus.evaluation.war import peak_war_from_score as peak_war_from_ovr, aging_mult
    from statsplusplus.evaluation.war import load_stat_history as _lsh_fn
    def load_stat_history(conn, game_date):
        return _lsh_fn(conn, game_date, dh_rule=cfg.settings.get("dh_rule", "Universal DH"))
    dollars_per_war = lambda: _dpw_fn(league_dir)
    league_minimum = lambda: _lm_fn(league_dir)
    from statsplusplus.evaluation.composite import compute_combined_value
    from statsplusplus.evaluation.fv import (
        compute_performance_adjusted_ceiling,
        compute_stat_risk_modifier,
    )
    from statsplusplus.data.milb import load_milb_averages, load_milb_stat_seasons

    if league_dir is None:
        from statsplusplus.config.league_context import get_league_dir
        league_dir = get_league_dir()

    conn = _get_conn(league_dir)
    _init_schema(league_dir)

    cfg = LeagueConfig(base_dir=league_dir)

    state_path = league_dir / "config" / "state.json"
    with open(state_path) as f:
        game_date = json.load(f)["game_date"]
    role_map = {str(k): v for k, v in cfg.role_map.items()}

    # Check use_custom_scores flag
    settings_path = league_dir / "config" / "league_settings.json"
    use_custom_scores = True
    if settings_path.exists():
        try:
            with open(settings_path) as f:
                settings = json.load(f)
            use_custom_scores = settings.get("use_custom_scores", True)
        except (json.JSONDecodeError, OSError):
            pass

    # Pre-load stat history for unified evaluation
    bat_hist, pit_hist, two_way = load_stat_history(conn, game_date)

    # Career service for rookie eligibility (130 AB / 50 IP)
    _career_ab = dict(conn.execute(
        "SELECT player_id, SUM(ab) FROM mlb_batting_stats WHERE split_id=1 GROUP BY player_id"
    ).fetchall())
    _career_ip = dict(conn.execute(
        "SELECT player_id, SUM(ip) FROM mlb_pitching_stats WHERE split_id=1 GROUP BY player_id"
    ).fetchall())

    # Load MiLB stat context
    _milb_averages = load_milb_averages(league_dir)
    _milb_discounts: dict = {}
    _milb_norm_ages: dict = {}
    _mw_path = league_dir / "config" / "model_weights.json"
    if _mw_path.exists():
        try:
            _mw_data = json.loads(_mw_path.read_text())
            _milb_discounts = _mw_data.get("MILB_LEVEL_DISCOUNTS", {})
            _milb_norm_ages = _mw_data.get("MILB_NORM_AGES", {})
        except (json.JSONDecodeError, OSError):
            pass

    rows = conn.execute(RATINGS_SQL).fetchall()

    # Filter to our league's organizations
    _our_tids = cfg.mlb_team_ids
    if _our_tids:
        rows = [r for r in rows if r["team_id"] in _our_tids
                or r["parent_team_id"] in _our_tids
                or r["team_id"] == 0
                or (r["parent_team_id"] == 0 and str(r["level"] or "") != "1")]

    # Load COMPOSITE_TO_WAR tables
    _comp_war_tables: dict = {}
    if _mw_path.exists():
        with open(_mw_path) as _f:
            _mw = json.load(_f)
        _comp_war_tables = _mw.get("COMPOSITE_TO_WAR", _mw.get("OVR_TO_WAR", {}))

    prospect_rows: list[tuple] = []
    _adjusted_ceilings: dict[int, int] = {}  # pid → PAC-adjusted ceiling for unified eval

    for rat in rows:
        p = dict(rat)
        pid = p["ID"]
        age = p["Age"]
        level = p["level"]

        if use_custom_scores:
            if p.get("secondary_composite") is not None:
                primary = p.get("composite_score") or p.get("Ovr") or 0
                secondary = p.get("secondary_composite") or 0
                combined = compute_combined_value(primary, secondary)
                p["Ovr"] = combined
            else:
                p["Ovr"] = p.get("composite_score") or p.get("Ovr") or 0
            p["Pot"] = p.get("true_ceiling") or p.get("ceiling_score") or p.get("Pot") or 0
            if p.get("defensive_value") is not None:
                p["_defensive_value"] = p["defensive_value"]
            if p.get("offensive_grade") is not None:
                p["_offensive_grade"] = p["offensive_grade"]
            if p.get("offensive_ceiling") is not None:
                p["_offensive_ceiling"] = p["offensive_ceiling"]
        else:
            p["Ovr"] = p.get("Ovr") or 0
            p["Pot"] = p.get("Pot") or 0

        # Skip malformed ratings
        ovr_raw = p.get("Ovr", 0)
        if not isinstance(ovr_raw, (int, float)):
            try:
                p["Ovr"] = int(ovr_raw)
            except (ValueError, TypeError):
                continue

        role_str = role_map.get(str(p.get("role") or 0), "position_player")
        p["_role"] = role_str
        p["Pos"] = str(p.get("pos") or "")
        p["_is_pitcher"] = (p["Pos"] == "P" or role_str in ("starter", "reliever", "closer"))
        bucket = assign_bucket(p)
        p["_bucket"] = bucket
        p["_mlb_median"] = 50

        # Skip foreign/independent league players
        if str(level) in ("7", "8"):
            continue

        if int(level) == 1:
            # Rookie-eligible: compute FV grade for the unified pipeline
            # Rookie-eligible: compute FV if low service time
            if _career_ab.get(pid, 0) < 130 and _career_ip.get(pid, 0) < 50:
                p["_norm_age"] = LEVEL_NORM_AGE["aaa"]
                p["_level"] = "aaa"
                _apply_milb_context(p, conn, pid, _milb_averages, _milb_discounts, _milb_norm_ages, load_milb_stat_seasons)
                _adjusted_ceilings[pid] = int(p.get("Pot") or 0)
                fv_base, fv_risk = calc_fv(p)
                fv_str = str(fv_base)
                if bucket == "RP":
                    p["_bucket"] = "SP"
                    raw_fv, _ = calc_fv(p)
                    p["_bucket"] = bucket
                else:
                    raw_fv = fv_base
                fv_continuous = p.get("_fv_continuous", raw_fv)
                prospect_rows.append((
                    pid, game_date, fv_base, fv_str,
                    "MLB", bucket, 0, fv_risk, fv_continuous
                ))
        else:
            # Minor leaguer — compute FV if below rookie threshold
            if _career_ab.get(pid, 0) >= 130 or _career_ip.get(pid, 0) >= 50:
                continue
            level_key = LEVEL_INT_KEY.get(int(level))
            if not level_key:
                continue
            p["_norm_age"] = LEVEL_NORM_AGE[level_key]
            p["_level"] = level_key
            _apply_milb_context(p, conn, pid, _milb_averages, _milb_discounts, _milb_norm_ages, load_milb_stat_seasons)
            _adjusted_ceilings[pid] = int(p.get("Pot") or 0)
            fv_base, fv_risk = calc_fv(p)
            fv_str = str(fv_base)
            level_label = LEVEL_INT_LABEL.get(int(level), str(level))
            if bucket == "RP":
                p["_bucket"] = "SP"
                raw_fv, _ = calc_fv(p)
                p["_bucket"] = bucket
            else:
                raw_fv = fv_base
            fv_continuous = p.get("_fv_continuous", raw_fv)
            prospect_rows.append((
                pid, game_date, fv_base, fv_str,
                level_label, bucket, 0, fv_risk, fv_continuous
            ))

    # Write FV grades to prospect_fv (surplus placeholder = 0, overwritten below)
    conn.execute("DELETE FROM prospect_fv")
    _pf_cols = {r[1] for r in conn.execute("PRAGMA table_info(prospect_fv)").fetchall()}
    if "risk" not in _pf_cols:
        conn.execute("ALTER TABLE prospect_fv ADD COLUMN risk TEXT")
    if "fv_continuous" not in _pf_cols:
        conn.execute("ALTER TABLE prospect_fv ADD COLUMN fv_continuous REAL")
    conn.executemany("INSERT INTO prospect_fv VALUES (?,?,?,?,?,?,?,?,?)", prospect_rows)

    # Ensure player_surplus table exists for unified pipeline to populate
    conn.execute("DROP TABLE IF EXISTS player_surplus")
    conn.execute("""CREATE TABLE player_surplus (
        player_id INTEGER, eval_date TEXT, name TEXT, bucket TEXT,
        age INTEGER, ovr INTEGER, fv INTEGER, fv_str TEXT,
        surplus INTEGER, surplus_yr1 INTEGER, level TEXT,
        team_id INTEGER, parent_team_id INTEGER,
        PRIMARY KEY (player_id, eval_date))""")

    # --- Unified evaluation (single source of truth for all surplus) ---
    _write_unified_evaluations(
        conn, rows, game_date, role_map, use_custom_scores,
        _career_ab, _career_ip, bat_hist, pit_hist, two_way,
        cfg, league_dir, _adjusted_ceilings,
    )

    # --- Populate legacy tables from unified model ---
    _overwrite_legacy_from_unified(conn, game_date)

    conn.commit()
    conn.close()

    print(f"fv_calc: {len(prospect_rows)} prospects evaluated — eval_date {game_date}")


def _apply_milb_context(
    p: dict,
    conn,
    pid: int,
    milb_averages: dict,
    milb_discounts: dict,
    milb_norm_ages: dict,
    load_fn,
) -> None:
    """Apply MiLB stat context (PAC and risk modifier) to a player dict in-place."""
    from statsplusplus.evaluation.fv import (
        compute_performance_adjusted_ceiling,
        compute_stat_risk_modifier,
    )

    if not milb_averages:
        return
    milb_s = load_fn(conn, pid, p["_is_pitcher"], milb_averages)
    if not milb_s:
        return

    disc_key = "pitcher" if p["_is_pitcher"] else "hitter"
    weighted_sum = 0.0
    total_w = 0.0
    for ms in milb_s[:3]:
        lv = str(ms.get("level", 0))
        disc = float(milb_discounts.get(disc_key, {}).get(lv, 0.0))
        if disc <= 0:
            continue
        pa = ms.get("pa", 0) if not p["_is_pitcher"] else ms.get("ip", 0) * 4.3
        w = pa * disc
        weighted_sum += ms["stat_2080"] * w
        total_w += w

    if total_w > 0:
        stat_2080 = weighted_sum / total_w
        eff_pa = total_w
        level_str = str(int(p.get("level", 2)))
        norm_age_lv = int(milb_norm_ages.get(level_str, p["_norm_age"]))
        tool_only = p.get("composite_score") or p.get("Ovr") or 0

        p["Pot"] = compute_performance_adjusted_ceiling(
            p["Pot"], stat_2080, p["Age"], norm_age_lv, eff_pa, tool_only
        )
        p["_stat_risk_modifier"] = compute_stat_risk_modifier(
            stat_2080, p["Age"], norm_age_lv, eff_pa, tool_only
        )


def _check_fv_tier_discrepancy(p: dict, fv_base: int, fv_risk: str) -> None:
    """Log a warning when the component-based defensive bonus produces an FV
    grade differing from the old defensive_score() path by more than one FV
    tier (5 FV points). Only runs when ``_defensive_value`` was used."""
    if p.get("_defensive_value") is None:
        return
    from statsplusplus.evaluation.fv import calc_fv_from_dict as calc_fv
    p_old = dict(p)
    del p_old["_defensive_value"]
    fv_old, _ = calc_fv(p_old)

    if abs(fv_base - fv_old) > 5:
        logger.warning(
            "FV tier discrepancy for player %s: component-based=%d, "
            "raw-tool-based=%d (defensive_value=%s)",
            p.get("ID", "?"), fv_base, fv_old, p["_defensive_value"],
        )


def _overwrite_legacy_from_unified(conn, game_date: str) -> None:
    """Overwrite prospect_fv and player_surplus with unified model values.

    Maps player_evaluation data back into the legacy table schemas so all
    existing consumers get unified model values without code changes.
    """
    try:
        count = conn.execute(
            "SELECT COUNT(*) FROM player_evaluation WHERE eval_date=?", (game_date,)
        ).fetchone()[0]
        if count == 0:
            return  # Unified eval didn't run — keep legacy values
    except Exception:
        return

    # Overwrite prospect_fv: players with risk label (prospects) or low stat_confidence
    conn.execute("DELETE FROM prospect_fv")
    conn.execute("""
        INSERT INTO prospect_fv (player_id, eval_date, fv, fv_str, level, bucket,
                                  prospect_surplus, risk, fv_continuous)
        SELECT player_id, eval_date, fv, fv_str, level, bucket,
               surplus, risk, fv_continuous
        FROM player_evaluation
        WHERE eval_date = ?
        AND (risk IS NOT NULL OR stat_confidence < 0.5)
        AND level != 'MLB'
    """, (game_date,))
    # Also include rookie-eligible MLB players (same as before)
    conn.execute("""
        INSERT OR IGNORE INTO prospect_fv (player_id, eval_date, fv, fv_str, level, bucket,
                                            prospect_surplus, risk, fv_continuous)
        SELECT player_id, eval_date, fv, fv_str, level, bucket,
               surplus, risk, fv_continuous
        FROM player_evaluation
        WHERE eval_date = ?
        AND risk IS NOT NULL
        AND level = 'MLB'
    """, (game_date,))

    # Overwrite player_surplus: all MLB-level players
    conn.execute("DELETE FROM player_surplus")
    conn.execute("""
        INSERT INTO player_surplus (player_id, eval_date, name, bucket, age, ovr,
                                     fv, fv_str, surplus, surplus_yr1, level,
                                     team_id, parent_team_id)
        SELECT player_id, eval_date, name, bucket, age, composite,
               fv, fv_str, surplus, surplus_yr1, level,
               team_id, parent_team_id
        FROM player_evaluation
        WHERE eval_date = ?
        AND level = 'MLB'
    """, (game_date,))

    pf_count = conn.execute("SELECT COUNT(*) FROM prospect_fv").fetchone()[0]
    ps_count = conn.execute("SELECT COUNT(*) FROM player_surplus").fetchone()[0]
    logger.info(f"legacy overwrite: {pf_count} prospect_fv, {ps_count} player_surplus (from unified)")


def _write_unified_evaluations(
    conn,
    rows: list,
    game_date: str,
    role_map: dict,
    use_custom_scores: bool,
    career_ab: dict,
    career_ip: dict,
    bat_hist: dict,
    pit_hist: dict,
    two_way: set,
    cfg,
    league_dir,
    adjusted_ceilings: dict[int, int] | None = None,
) -> None:
    """Compute and write unified evaluations for all rated players.

    Phase 2 dual-write: populates player_evaluation table alongside the
    existing prospect_fv and player_surplus tables. Non-fatal on failure.
    """
    from statsplusplus.evaluation.unified import unified_surplus
    from statsplusplus.evaluation.war import stat_peak_war
    from statsplusplus.evaluation.constants import load_model_weights
    from statsplusplus.config.league_config import dollars_per_war, league_minimum
    from statsplusplus.utils.positions import assign_bucket
    from pathlib import Path

    try:
        weights = load_model_weights(league_dir)
        dpw = dollars_per_war(league_dir)
        min_sal = league_minimum(league_dir)
        perpetual_arb = cfg.perpetual_arb
    except Exception as e:
        logger.warning(f"unified eval: failed to load config: {e}")
        return

    # Load career PA (AB + BB + HBP + SF)
    career_pa: dict[int, int] = {}
    try:
        for r in conn.execute(
            "SELECT player_id, SUM(ab + COALESCE(bb,0) + COALESCE(hbp,0) + COALESCE(sf,0)) "
            "FROM mlb_batting_stats WHERE split_id=1 GROUP BY player_id"
        ).fetchall():
            career_pa[r[0]] = int(r[1] or 0)
    except Exception:
        pass

    # Career IP (already available from outer scope but recompute for IP specifically)
    career_ip_totals: dict[int, float] = {}
    try:
        for r in conn.execute(
            "SELECT player_id, SUM(ip) FROM mlb_pitching_stats WHERE split_id=1 GROUP BY player_id"
        ).fetchall():
            career_ip_totals[r[0]] = float(r[1] or 0)
    except Exception:
        pass

    # Load prospect FV data (just written above) for FV continuous values
    prospect_fv_data: dict[int, tuple] = {}
    try:
        for r in conn.execute(
            "SELECT player_id, fv, fv_str, fv_continuous, level, bucket, risk "
            "FROM prospect_fv WHERE eval_date = ?", (game_date,)
        ).fetchall():
            prospect_fv_data[r[0]] = r
    except Exception:
        pass

    # Ensure table exists
    conn.execute("""CREATE TABLE IF NOT EXISTS player_evaluation (
        player_id INTEGER, eval_date TEXT, name TEXT, bucket TEXT,
        age INTEGER, level TEXT, team_id INTEGER, parent_team_id INTEGER,
        composite INTEGER, ceiling INTEGER,
        fv INTEGER, fv_str TEXT, fv_continuous REAL, risk TEXT,
        tool_war REAL, stat_war REAL, stat_confidence REAL, peak_war REAL,
        surplus INTEGER, surplus_yr1 INTEGER, years_control INTEGER, ctrl_type TEXT,
        PRIMARY KEY (player_id, eval_date))""")
    conn.execute("DELETE FROM player_evaluation")

    eval_rows: list[tuple] = []
    errors = 0

    for rat in rows:
        p = dict(rat)
        pid = p["ID"]
        age = p["Age"]
        level = p["level"]

        if level is None or str(level) in ("7", "8"):
            continue

        # Resolve composite/ceiling
        if use_custom_scores:
            composite = p.get("composite_score") or p.get("Ovr") or 0
            ceiling = p.get("true_ceiling") or p.get("ceiling_score") or p.get("Pot") or 0
        else:
            composite = p.get("Ovr") or 0
            ceiling = p.get("Pot") or 0

        # Use PAC-adjusted ceiling when available (from the prospect pipeline)
        if adjusted_ceilings and pid in adjusted_ceilings:
            ceiling = adjusted_ceilings[pid]

        if not composite or not ceiling:
            continue

        # Determine bucket
        role_str = role_map.get(str(p.get("role") or 0), "position_player")
        p["_role"] = role_str
        p["Pos"] = str(p.get("pos") or "")
        p["_is_pitcher"] = (p["Pos"] == "P" or role_str in ("starter", "reliever", "closer"))
        bucket = assign_bucket(p)

        # Level string
        level_int = int(level)
        level_str = LEVEL_INT_LABEL.get(level_int, "MLB") if level_int != 1 else "MLB"

        # FV data
        pf = prospect_fv_data.get(pid)
        if pf:
            fv = pf[1]
            fv_str = pf[2]
            fv_continuous = pf[3] or float(pf[1])
            risk = pf[6]
            # Use prospect_fv's bucket (which may differ from raw assignment)
            bucket = pf[5] or bucket
            # RP FV cap: fv_continuous for RPs may be the uncapped SP-based value
            # (fv_calc computes SP-based FV for surplus interpolation). Cap it here
            # to prevent RPs from appearing with inflated FV in rankings.
            # Real baseball caps RP prospect FV at 50 (elite closer ceiling).
            if bucket == "RP":
                fv_continuous = min(fv_continuous, 50.0)
                fv = min(fv, 50)
                fv_str = str(fv)
            # Infer effective ceiling: PAC may have lowered the ceiling during
            # the prospect pipeline. We don't have the adjusted value stored,
            # but we can infer it: calc_fv caps FV at ceiling-3, so the effective
            # ceiling is approximately fv_continuous + 5 (with some margin).
            # Clamp between composite+3 and raw ceiling.
            inferred_ceiling = min(ceiling, max(composite + 3, int(fv_continuous) + 5))
            ceiling = inferred_ceiling
        else:
            # MLB player without prospect FV — estimate from composite/ceiling
            fv_continuous = float(min(ceiling - 3, composite + (ceiling - composite) * 0.4))
            fv = round(fv_continuous / 5) * 5
            fv_str = str(fv)
            # Estimate risk from composite/ceiling gap and age
            gap = max(0, ceiling - composite)
            if gap <= 3:
                risk = "Low"
            elif gap <= 8 or age >= 26:
                risk = "Medium"
            elif gap <= 15:
                risk = "High"
            else:
                risk = "Extreme"
            # RP FV cap applies here too
            if bucket == "RP":
                fv_continuous = min(fv_continuous, 50.0)
                fv = min(fv, 50)
                fv_str = str(fv)

        # Career stats
        pa = career_pa.get(pid, 0)
        ip = career_ip_totals.get(pid, 0.0)

        # Stat peak WAR
        sw = stat_peak_war(pid, bucket, bat_hist, pit_hist, two_way=two_way)

        # Estimate control period
        ab = career_ab.get(pid, 0) or 0
        ip_career = career_ip.get(pid, 0) or 0
        if ab < 130 and ip_career < 50:
            years_ctrl = 6
            ctrl_type = "pre-arb"
        else:
            # Check contract
            c_row = conn.execute(
                "SELECT years, current_year, salary_0 FROM contracts WHERE player_id=?", (pid,)
            ).fetchone()
            if c_row and c_row[0]:
                contract_years_left = max(1, c_row[0] - (c_row[1] or 0))
                contract_salary = c_row[2] or 0
                # If it's a 1-year deal at min salary, player is likely pre-arb
                # Use service time to estimate true control
                if contract_years_left == 1 and contract_salary <= min_sal * 1.1:
                    svc = conn.execute(
                        "SELECT mlb_service_years FROM players WHERE player_id=?", (pid,)
                    ).fetchone()
                    svc_years = (svc[0] or 0) if svc else 0
                    if svc_years < 3:
                        years_ctrl = max(1, 6 - svc_years)
                        ctrl_type = "pre-arb"
                    elif svc_years < 6:
                        years_ctrl = max(1, 6 - svc_years)
                        ctrl_type = "arb"
                    else:
                        years_ctrl = 1
                        ctrl_type = "contract"
                else:
                    years_ctrl = contract_years_left
                    ctrl_type = "contract"
            else:
                years_ctrl = 3  # Default arb-eligible
                ctrl_type = "arb"

        # Get salary schedule from contract if available
        salaries = None
        if ctrl_type == "contract":
            c_full = conn.execute("SELECT * FROM contracts WHERE player_id=?", (pid,)).fetchone()
            if c_full:
                sals = []
                start = c_full["current_year"] or 0
                for i in range(start, c_full["years"]):
                    key = f"salary_{i}"
                    if key in c_full.keys():
                        sals.append(c_full[key] or min_sal)
                if len(sals) >= years_ctrl:
                    salaries = sals[:years_ctrl]

        try:
            result = unified_surplus(
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
                salaries=salaries,
                dpw=dpw,
                min_sal=min_sal,
                perpetual_arb=perpetual_arb,
                weights=weights,
            )
        except Exception:
            errors += 1
            continue

        eval_rows.append((
            pid, game_date, p["Name"], bucket, age, level_str,
            p.get("team_id"), p.get("parent_team_id"),
            composite, ceiling,
            fv, fv_str, fv_continuous, risk,
            result["tool_war"], result.get("stat_war"),
            result["stat_confidence"], result["peak_war"],
            result["surplus"], result["surplus_yr1"],
            years_ctrl, ctrl_type,
        ))

    if eval_rows:
        conn.executemany(
            "INSERT OR REPLACE INTO player_evaluation VALUES "
            "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            eval_rows,
        )

    logger.info(f"unified eval: {len(eval_rows)} players written, {errors} errors")


def main() -> None:
    """CLI entry point."""
    run()


if __name__ == "__main__":
    main()
