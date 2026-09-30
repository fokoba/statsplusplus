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
           p.name AS Name, p.age AS Age, p.team_id, p.parent_team_id, p.organization_id, p.level, p.pos, p.role,
           p.free_agent AS FreeAgent, p.draft_eligible AS DraftEligible,
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
           r.gap_l AS Gap_L, r.gap_r AS Gap_R,
           r.pow_l AS Pow_L, r.pow_r AS Pow_R,
           r.eye_l AS Eye_L, r.eye_r AS Eye_R,
           r.stf_l AS Stf_L, r.stf_r AS Stf_R,
           r.mov_l AS Mov_L, r.mov_r AS Mov_R,
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

    from statsplusplus.data import db as _db
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
    from prospect_value import prospect_surplus_with_option as _prospect_surplus_opt
    from contract_value import contract_value as _contract_value
    from statsplusplus.evaluation.composite import compute_combined_value
    from statsplusplus.evaluation.fv import (
        compute_performance_adjusted_ceiling,
        compute_stat_risk_modifier,
    )
    from statsplusplus.data.milb import load_milb_averages, load_milb_stat_seasons

    if league_dir is None:
        from statsplusplus.config.league_context import get_league_dir
        league_dir = get_league_dir()

    conn = _db.get_conn(league_dir)
    _db.init_schema(league_dir)

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

    # Pre-load stat history for batch contract_value calls
    bat_hist, pit_hist, two_way = load_stat_history(conn, game_date)
    _cv_hist = (bat_hist, pit_hist, two_way)

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
                or r["organization_id"] in _our_tids
                or r["team_id"] == 0
                or (r["parent_team_id"] == 0 and str(r["level"] or "") != "1")]

    # Load COMPOSITE_TO_WAR tables
    _comp_war_tables: dict = {}
    if _mw_path.exists():
        with open(_mw_path) as _f:
            _mw = json.load(_f)
        _comp_war_tables = _mw.get("COMPOSITE_TO_WAR", _mw.get("OVR_TO_WAR", {}))

    prospect_rows: list[tuple] = []
    surplus_rows: list[tuple] = []
    _dev_bucket: dict[int, tuple] = {}   # pid -> (bucket, age) for the dev-speed pass

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
        _dev_bucket[pid] = (bucket, age)

        # Defensive potential for scarcity
        _DEF_KEY = {'CF': 'PotCF', 'SS': 'PotSS', 'C': 'PotC', '2B': 'Pot2B', '3B': 'Pot3B'}
        def_rating = p.get(_DEF_KEY.get(bucket)) or 0

        # Skip independent-league and foreign-league players — but level 8
        # is overloaded: it also means "our own International Complex" for
        # a player directly on our own team_id (parent_team_id=0), who
        # should be graded exactly like any other domestic prospect, not
        # treated as an unowned foreign scouting target. Only skip level 8
        # rows that AREN'T actually ours.
        if str(level) == "7":
            continue
        if str(level) == "8" and p["team_id"] not in _our_tids:
            continue

        if int(level) == 1:
            ovr = int(p.get("Ovr") or 0)
            surplus = 0
            surplus_yr1 = 0
            cv = _contract_value(pid, _conn=conn, _hist=_cv_hist)
            if cv:
                surplus = cv["total_surplus"].get("base", 0)
                bd = cv.get("breakdown")
                if bd:
                    surplus_yr1 = round(bd[0].get("surplus", 0))
            surplus_rows.append((
                pid, game_date, p["Name"], bucket, age,
                ovr, ovr, str(ovr), surplus, surplus_yr1,
                "MLB", p["team_id"], p["parent_team_id"]
            ))
            # Rookie-eligible
            if age <= 24 and _career_ab.get(pid, 0) < 130 and _career_ip.get(pid, 0) < 50:
                p["_norm_age"] = LEVEL_NORM_AGE["aaa"]
                p["_level"] = "aaa"
                _apply_milb_context(p, conn, pid, _milb_averages, _milb_discounts, _milb_norm_ages, load_milb_stat_seasons)
                fv_base, fv_risk = calc_fv(p)
                fv_str = str(fv_base)
                if bucket == "RP":
                    p["_bucket"] = "SP"
                    raw_fv, _ = calc_fv(p)
                    p["_bucket"] = bucket
                else:
                    raw_fv = fv_base
                fv_continuous = p.get("_fv_continuous", raw_fv)
                p_surplus = _prospect_surplus_opt(
                    fv_continuous, age, "MLB", bucket,
                    ovr=p.get("Ovr"), pot=p.get("Pot"), def_rating=def_rating,
                    offensive_grade=p.get("offensive_grade"),
                    offensive_ceiling=p.get("offensive_ceiling"),
                    defensive_value=p.get("defensive_value"),
                    durability_score=p.get("durability_score"),
                )
                prospect_rows.append((
                    pid, game_date, fv_base, fv_str,
                    "MLB", bucket, p_surplus, fv_risk, fv_continuous
                ))
        else:
            # No age cap here — the career_ab/career_ip check right below is
            # the real "hasn't proven himself at the MLB level yet" signal.
            # An age<=24 gate on top of it left minor leaguers over 24 with
            # no row in either prospect_fv or player_surplus at all (not
            # just a smaller number — nothing computed), even when they're
            # legitimately unproven org depth still worth valuing (e.g. a
            # 25-27yo who's simply never gotten a real MLB look).
            if _career_ab.get(pid, 0) >= 130 or _career_ip.get(pid, 0) >= 50:
                # Proven at the MLB level but not currently on the active
                # roster (optioned/secondary) — still has a real contract,
                # so value him the same way a level==1 player would be
                # rather than dropping him from both tables entirely.
                ovr = int(p.get("Ovr") or 0)
                surplus = 0
                surplus_yr1 = 0
                cv = _contract_value(pid, _conn=conn, _hist=_cv_hist)
                if cv:
                    surplus = cv["total_surplus"].get("base", 0)
                    bd = cv.get("breakdown")
                    if bd:
                        surplus_yr1 = round(bd[0].get("surplus", 0))
                surplus_rows.append((
                    pid, game_date, p["Name"], bucket, age,
                    ovr, ovr, str(ovr), surplus, surplus_yr1,
                    "MLB", p["team_id"], p["parent_team_id"]
                ))
                continue
            level_key = LEVEL_INT_KEY.get(int(level))
            if not level_key:
                continue
            p["_norm_age"] = LEVEL_NORM_AGE[level_key]
            p["_level"] = level_key
            _apply_milb_context(p, conn, pid, _milb_averages, _milb_discounts, _milb_norm_ages, load_milb_stat_seasons)
            fv_base, fv_risk = calc_fv(p)
            fv_str = str(fv_base)
            # level=0 free agents are already-proven, immediately signable
            # professionals — not amateur draft prospects, even though they
            # share the same level code (every level=0 row in this data is
            # free_agent=1, including genuine draft-pool amateurs, so
            # free_agent alone doesn't separate them). Route only the real,
            # signable pool through prospect_value's "FA" treatment
            # (immediate debut, no development discount) instead of "Draft"
            # (years away, ovr-based effective level) — using the exact same
            # free_agent=1 AND draft_eligible!=1 test get_free_agent_candidates()
            # uses to decide who appears in the Free Agent Adds box at all,
            # so the stored total Surplus and the live Peak Yr Surplus for
            # the same free agent agree on how many years away he is.
            if int(level) == 0 and p.get("FreeAgent") and not p.get("DraftEligible"):
                level_label = "FA"
            else:
                level_label = LEVEL_INT_LABEL.get(int(level), str(level))
            if bucket == "RP":
                p["_bucket"] = "SP"
                raw_fv, _ = calc_fv(p)
                p["_bucket"] = bucket
            else:
                raw_fv = fv_base
            fv_continuous = p.get("_fv_continuous", raw_fv)
            surplus = _prospect_surplus_opt(
                fv_continuous, age, level_label, bucket,
                ovr=p.get("Ovr"), pot=p.get("Pot"), def_rating=def_rating,
                offensive_grade=p.get("offensive_grade"),
                offensive_ceiling=p.get("offensive_ceiling"),
                defensive_value=p.get("defensive_value"),
                durability_score=p.get("durability_score"),
            )
            prospect_rows.append((
                pid, game_date, fv_base, fv_str,
                level_label, bucket, surplus, fv_risk, fv_continuous
            ))

    # Write results
    conn.execute("DELETE FROM prospect_fv")
    _pf_cols = {r[1] for r in conn.execute("PRAGMA table_info(prospect_fv)").fetchall()}
    if "risk" not in _pf_cols:
        conn.execute("ALTER TABLE prospect_fv ADD COLUMN risk TEXT")
    if "fv_continuous" not in _pf_cols:
        conn.execute("ALTER TABLE prospect_fv ADD COLUMN fv_continuous REAL")
    conn.execute("DROP TABLE IF EXISTS player_surplus")
    conn.execute("""CREATE TABLE player_surplus (
        player_id INTEGER, eval_date TEXT, name TEXT, bucket TEXT,
        age INTEGER, ovr INTEGER, fv INTEGER, fv_str TEXT,
        surplus INTEGER, surplus_yr1 INTEGER, level TEXT,
        team_id INTEGER, parent_team_id INTEGER,
        PRIMARY KEY (player_id, eval_date))""")
    conn.executemany("INSERT INTO prospect_fv VALUES (?,?,?,?,?,?,?,?,?)", prospect_rows)
    conn.executemany("INSERT INTO player_surplus VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", surplus_rows)

    try:
        _compute_dev_speed_pass(conn, game_date, _dev_bucket, league_dir)
    except Exception as e:
        logger.warning(f"dev-speed pass failed (non-fatal): {e}")

    conn.commit()
    conn.close()

    print(f"fv_calc: {len(prospect_rows)} prospects, {len(surplus_rows)} MLB players — eval_date {game_date}")


def _compute_dev_speed_pass(conn, game_date: str, bucket_map: dict, league_dir: Path | None = None) -> None:
    """Compute + store the development-speed metric for all players.

    Ported from upstream tfalsone/statsplusplus (commit 886ee50) and adapted to
    this fork's schema (ratings_history/players/batting_stats/pitching_stats,
    which already carry the same column names upstream's version expects).

    Loads longitudinal ratings_history, builds a per (bucket, age-band) baseline
    from the full population, computes each player's trailing-window dev-speed,
    and writes the `dev_speed` table. Buckets come from bucket_map (canonical
    assign_bucket, computed in the main loop above).
    """
    from collections import defaultdict
    from statsplusplus.evaluation import dev_speed as ds
    from statsplusplus.evaluation.constants import load_model_weights, PEAK_AGE_HITTER, PEAK_AGE_PITCHER

    # Per-league peak age (2026-09-30) — MODEL_PARAMS override in
    # model_weights.json, empirically derived from real career-WAR
    # trajectories, falling back to the shared unvalidated defaults.
    if league_dir is not None:
        _w = load_model_weights(league_dir)
        peak_age_hitter = _w.get_param("PEAK_AGE_HITTER", PEAK_AGE_HITTER)
        peak_age_pitcher = _w.get_param("PEAK_AGE_PITCHER", PEAK_AGE_PITCHER)
    else:
        peak_age_hitter, peak_age_pitcher = PEAK_AGE_HITTER, PEAK_AGE_PITCHER

    # trailing-window cutoff (string compare on YYYY-MM-DD)
    latest = conn.execute(
        "SELECT MAX(snapshot_date) FROM ratings_history WHERE composite_score IS NOT NULL"
    ).fetchone()[0]
    if not latest:
        logger.info("dev-speed: no ratings_history — skipped")
        return
    ly, lm, _ = map(int, latest.split("-"))
    sy, sm = ly - (ds.WINDOW_MONTHS // 12), lm - (ds.WINDOW_MONTHS % 12)
    if sm <= 0:
        sm += 12; sy -= 1
    cutoff = f"{sy:04d}-{sm:02d}-01"

    rows = conn.execute("""
        SELECT h.player_id, h.snapshot_date, h.composite_score, h.ceiling_score,
               h.true_ceiling, h.ovr, h.pot, h.offensive_grade, h.defensive_value,
               h.cntct, h.gap, h.pow, h.eye, h.stf, h.mov, h.ctrl, p.age
        FROM ratings_history h JOIN players p ON p.player_id = h.player_id
        WHERE h.composite_score IS NOT NULL AND h.composite_score > 0
        ORDER BY h.player_id, h.snapshot_date
    """).fetchall()
    by_player: dict[int, list] = defaultdict(list)
    age_of: dict[int, int] = {}
    for r in rows:
        # NOTE: "gap" here is the Gap-Power hit tool (ratings_history.gap,
        # dev_speed.HITTER_TOOLS) — unrelated to compute_dev_speed()'s own
        # "gap" (ceiling minus composite), which lives in a different dict.
        d = {"snapshot_date": r["snapshot_date"], "composite_score": r["composite_score"],
             "ceiling_score": r["ceiling_score"], "true_ceiling": r["true_ceiling"],
             "ovr": r["ovr"], "pot": r["pot"],
             "offensive_grade": r["offensive_grade"], "defensive_value": r["defensive_value"],
             "cntct": r["cntct"], "gap": r["gap"], "pow": r["pow"], "eye": r["eye"],
             "stf": r["stf"], "mov": r["mov"], "ctrl": r["ctrl"]}
        by_player[r["player_id"]].append(d)
        age_of[r["player_id"]] = r["age"]

    # acc + recent playing time (last 2 game-years)
    acc_of = dict(conn.execute("SELECT player_id, acc FROM latest_ratings").fetchall())
    yr = conn.execute("SELECT MAX(year) FROM batting_stats").fetchone()[0] or 0
    pa_of = dict(conn.execute(
        "SELECT player_id, SUM(pa) FROM batting_stats WHERE split_id=1 AND year>=? GROUP BY player_id",
        (yr - 1,)).fetchall())
    ip_of = dict(conn.execute(
        "SELECT player_id, SUM(outs)/3.0 FROM pitching_stats WHERE split_id=1 AND year>=? GROUP BY player_id",
        (yr - 1,)).fetchall())

    # Build the window per player + records for the baseline.
    windows: dict[int, list] = {}
    records = []
    for pid, snaps in by_player.items():
        win = [s for s in snaps if s["snapshot_date"] >= cutoff]
        if len(win) < ds.MIN_SNAPSHOTS:
            win = snaps
        if len(win) < ds.MIN_SNAPSHOTS:
            continue
        bucket = bucket_map.get(pid, (None,))[0]
        if bucket is None:
            continue
        age = age_of[pid]
        windows[pid] = win
        first, last = win[0], win[-1]
        yrs = ds._years_between(first["snapshot_date"], last["snapshot_date"])
        if yrs * 365.25 < ds.MIN_WINDOW_DAYS:
            continue
        d_comp = (last["composite_score"] - first["composite_score"]) / yrs
        d_off, _, _ = ds.component_delta(win, "offensive_grade")
        records.append({"age": age, "bucket": bucket, "band": ds.age_band(age),
                        "annual_comp": d_comp, "annual_off": d_off})

    baseline = ds.build_baseline(records)

    out_rows = []
    for pid, win in windows.items():
        bucket, age = bucket_map[pid]
        is_pit = bucket in ("SP", "RP")
        pt = (ip_of.get(pid, 0) if is_pit else pa_of.get(pid, 0)) or 0
        res = ds.compute_dev_speed(
            bucket=bucket, age=age, window=win, baseline=baseline,
            acc=acc_of.get(pid), playing_time=pt,
            peak_age_hitter=peak_age_hitter, peak_age_pitcher=peak_age_pitcher)
        if res is None:
            continue
        out_rows.append((
            pid, game_date, 1 if res["available"] else 0, res["z"], res["signal"],
            res["label"], res["css_class"], res["note"], res["gap"], res["d_ovr"],
            res["d_pot"], res["confidence"], res["annual_move"], res["peer_mean"],
            res["peer_sd"], res["peer_n"], res["comp_first"], res["comp_last"],
            res["off_first"], res["off_last"], res["def_first"], res["def_last"],
            res["window_years"], res["n_snaps"],
            res["schedule_status"], res["schedule_label"], res["schedule_note"],
            ",".join(res["stagnant_tools"]) if res["stagnant_tools"] else None,
            res["gap_closed_pct_yr"], res["years_to_peak"],
            res["gap_smoothed"], res["gap_trend"],
        ))

    conn.execute("DELETE FROM dev_speed")
    if out_rows:
        conn.executemany(
            "INSERT OR REPLACE INTO dev_speed VALUES "
            "(" + ",".join("?" * 32) + ")", out_rows)
    n_avail = sum(1 for r in out_rows if r[2] == 1)
    logger.info(f"dev-speed: {len(out_rows)} computed, {n_avail} reportable, "
                f"{sum(len(v) for v in baseline.values())} baseline cells")


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
    import sys
    _scripts = str(Path(__file__).resolve().parent.parent.parent.parent / "scripts")
    if _scripts not in sys.path:
        sys.path.insert(0, _scripts)
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


def main() -> None:
    """CLI entry point."""
    run()


if __name__ == "__main__":
    main()
