#!/usr/bin/env python3
"""
fv_calc.py — League-wide FV and surplus calculation.

Prospects (non-MLB, age ≤ 24): FV → prospect_fv
MLB players: surplus value → player_surplus

Angels org prospects are included — farm_analysis.py will overwrite them
with authoritative values when it runs.

Usage: python3 scripts/fv_calc.py
"""

import json, logging, os, sys

logger = logging.getLogger(__name__)

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "scripts"))

import db as _db
from league_config import config as _cfg
from player_utils import (assign_bucket, calc_fv, LEVEL_NORM_AGE,
                           dollars_per_war, league_minimum,
                           peak_war_from_ovr, aging_mult)
from prospect_value import prospect_surplus_with_option as _prospect_surplus_opt
from contract_value import contract_value as _contract_value

LEVEL_INT_KEY   = {0:"draft", 2:"aaa", 3:"aa", 4:"a", 5:"a-short", 6:"usl", 8:"intl", 10:"draft", 11:"draft"}
LEVEL_INT_LABEL = {0:"Draft", 1:"MLB", 2:"AAA", 3:"AA", 4:"A", 5:"A-Short", 6:"Rookie", 8:"International", 10:"College", 11:"HS"}

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



def _check_fv_tier_discrepancy(p, fv_base, fv_risk):
    """Log a warning when the component-based defensive bonus produces an FV
    grade differing from the old defensive_score() path by more than one FV
    tier (5 FV points).  Only runs when ``_defensive_value`` was used."""
    if p.get("_defensive_value") is None:
        return
    # Compute FV using the old path (without _defensive_value)
    p_old = dict(p)
    del p_old["_defensive_value"]
    fv_old, _ = calc_fv(p_old)

    if abs(fv_base - fv_old) > 5:
        logger.warning(
            "FV tier discrepancy for player %s: component-based=%d, "
            "raw-tool-based=%d (defensive_value=%s)",
            p.get("ID", "?"), fv_base, fv_old, p["_defensive_value"],
        )


def run():
    from league_context import get_league_dir
    league_dir = get_league_dir()
    conn = _db.get_conn(league_dir)
    _db.init_schema(league_dir)

    state_path = league_dir / "config" / "state.json"
    with open(state_path) as f:
        game_date = json.load(f)["game_date"]
    role_map = {str(k): v for k, v in _cfg.role_map.items()}

    # Check use_custom_scores flag from league_settings.json (default: True)
    settings_path = league_dir / "config" / "league_settings.json"
    use_custom_scores = True
    if settings_path.exists():
        try:
            with open(settings_path) as f:
                settings = json.load(f)
            use_custom_scores = settings.get("use_custom_scores", True)
        except (json.JSONDecodeError, OSError):
            pass

    # Pre-load stat history once for batch contract_value calls
    from player_utils import load_stat_history
    bat_hist, pit_hist, two_way = load_stat_history(conn, game_date)
    _cv_hist = (bat_hist, pit_hist, two_way)

    # Career service for rookie eligibility (130 AB / 50 IP)
    _career_ab = dict(conn.execute(
        "SELECT player_id, SUM(ab) FROM mlb_batting_stats WHERE split_id=1 GROUP BY player_id"
    ).fetchall())
    _career_ip = dict(conn.execute(
        "SELECT player_id, SUM(ip) FROM mlb_pitching_stats WHERE split_id=1 GROUP BY player_id"
    ).fetchall())

    rows = conn.execute(RATINGS_SQL).fetchall()

    # Filter to players belonging to our league's organizations.
    # Excludes foreign league players (e.g., Japanese leagues) that have
    # level=1 ratings but aren't part of our league structure.
    # Keeps: our orgs, free agents, and amateur/draft-eligible players.
    _our_tids = _cfg.mlb_team_ids
    if _our_tids:
        rows = [r for r in rows if r["team_id"] in _our_tids
                or r["parent_team_id"] in _our_tids
                or r["team_id"] == 0  # free agents / unsigned
                or (r["parent_team_id"] == 0 and str(r["level"] or "") != "1")  # amateurs (not foreign MLB)
                ]

    # Load COMPOSITE_TO_WAR tables for WAR-based FV
    import json as _json
    _mw_path = league_dir / "config" / "model_weights.json"
    _comp_war_tables = {}
    if _mw_path.exists():
        with open(_mw_path) as _f:
            _mw = _json.load(_f)
        _comp_war_tables = _mw.get("COMPOSITE_TO_WAR", _mw.get("OVR_TO_WAR", {}))

    def _interpolate_war(comp, bucket):
        table = _comp_war_tables.get(bucket, {})
        if not table:
            return 0.0
        keys = sorted(int(k) for k in table.keys())
        if not keys:
            return 0.0
        if comp <= keys[0]:
            return table[str(keys[0])]
        if comp >= keys[-1]:
            return table[str(keys[-1])]
        for i in range(len(keys) - 1):
            if keys[i] <= comp <= keys[i + 1]:
                lo, hi = keys[i], keys[i + 1]
                frac = (comp - lo) / (hi - lo)
                return table[str(lo)] * (1 - frac) + table[str(hi)] * frac
        return 0.0

    # FV thresholds: fixed WAR-based, grounded in actual MLB production
    # FV 50 = median starter (~2.0 WAR), FV 55 = above-avg (~3.0), etc.
    _fv_thresholds = [
        (6.0, 70), (5.0, 65), (4.0, 60), (3.0, 55), (2.0, 50), (1.0, 45), (0.3, 40),
    ]

    prospect_rows = []
    surplus_rows  = []

    for rat in rows:
        p = dict(rat)
        pid   = p["ID"]
        age   = p["Age"]
        level = p["level"]

        # When use_custom_scores is enabled, prefer composite_score/ceiling_score
        # over OVR/POT. Fall back to OVR/POT when composite scores are NULL.
        if use_custom_scores:
            # For two-way players, use combined_value as the effective Composite_Score
            if p.get("secondary_composite") is not None:
                from evaluation_engine import compute_combined_value
                primary = p.get("composite_score") or p.get("Ovr") or 0
                secondary = p.get("secondary_composite") or 0
                combined = compute_combined_value(primary, secondary)
                p["Ovr"] = combined
            else:
                p["Ovr"] = p.get("composite_score") or p.get("Ovr") or 0
            p["Pot"] = p.get("true_ceiling") or p.get("ceiling_score") or p.get("Pot") or 0
            # Pass pre-computed defensive component through to calc_fv
            # so it can use it directly instead of re-deriving from raw tools
            if p.get("defensive_value") is not None:
                p["_defensive_value"] = p["defensive_value"]
            # Pass offensive grade through for positional access premium
            if p.get("offensive_grade") is not None:
                p["_offensive_grade"] = p["offensive_grade"]
            if p.get("offensive_ceiling") is not None:
                p["_offensive_ceiling"] = p["offensive_ceiling"]
        else:
            p["Ovr"] = p.get("Ovr") or 0
            p["Pot"] = p.get("Pot") or 0

        # Skip rows with malformed ratings data (empty strings from API)
        ovr_raw = p.get("Ovr", 0)
        if not isinstance(ovr_raw, (int, float)):
            try:
                p["Ovr"] = int(ovr_raw)
            except (ValueError, TypeError):
                continue  # skip this player entirely

        role_str = role_map.get(str(p.get("role") or 0), "position_player")
        p["_role"] = role_str
        p["Pos"]   = str(p.get("pos") or "")
        p["_is_pitcher"] = (p["Pos"] == "P" or role_str in ("starter", "reliever", "closer"))
        bucket = assign_bucket(p)
        p["_bucket"] = bucket
        p["_mlb_median"] = 50  # legacy, not used by ceiling-based FV

        # Defensive potential for position-adjusted scarcity
        _DEF_KEY = {'CF':'PotCF','SS':'PotSS','C':'PotC','2B':'Pot2B','3B':'Pot3B'}
        def_rating = p.get(_DEF_KEY.get(bucket)) or 0

        # Skip foreign/independent league players (not in MLB pipeline)
        if str(level) in ("7", "8"):
            continue

        if int(level) == 1:
            ovr      = int(p.get("Ovr") or 0)
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
            # Rookie-eligible: <130 career AB AND <50 career IP, age ≤ 24
            if age <= 24 and _career_ab.get(pid, 0) < 130 and _career_ip.get(pid, 0) < 50:
                p["_norm_age"] = LEVEL_NORM_AGE["aaa"]
                p["_level"] = "aaa"
                fv_base, fv_risk = calc_fv(p)
                fv_str = str(fv_base)
                if bucket == "RP":
                    raw_pot = p["Pot"]
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
        elif age <= 24:
            level_key = LEVEL_INT_KEY.get(int(level))
            if not level_key:
                continue
            p["_norm_age"] = LEVEL_NORM_AGE[level_key]
            p["_level"] = level_key
            fv_base, fv_risk = calc_fv(p)
            fv_str = str(fv_base)
            level_label = LEVEL_INT_LABEL.get(int(level), str(level))

            # For surplus, use raw FV (before RP Pot discount) so the RP WAR
            # table is the sole positional adjustment — avoids double-discounting.
            if bucket == "RP":
                raw_pot = p["Pot"]
                p["_bucket"] = "SP"          # temporarily remove RP treatment
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

    conn.execute("DELETE FROM prospect_fv")
    # Ensure risk column exists (migration for existing DBs)
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
    conn.commit()
    conn.close()

    print(f"fv_calc: {len(prospect_rows)} prospects, {len(surplus_rows)} MLB players — eval_date {game_date}")


if __name__ == "__main__":
    run()
