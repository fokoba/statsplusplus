"""
data.py — Data access layer. All DB reads go through here.
Analysis scripts must not issue raw SQL or read JSON directly.
"""

from db import get_conn


def get_players(parent_team_id: int) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT player_id, name AS Name, age AS Age,
                   team_id, parent_team_id, level AS Level,
                   pos AS Pos, role AS Role
            FROM players WHERE team_id = ? OR parent_team_id = ?
        """, (parent_team_id, parent_team_id)).fetchall()
    return [dict(r) for r in rows]


def get_ratings(parent_team_id: int, snapshot_date: str = None, level: int = None) -> list[dict]:
    """Returns most recent snapshot per player if snapshot_date is None.
    level: optional integer level filter (1=MLB, 2=AAA, 3=AA, 4=A, 5=Short-A, 6=Rookie, 8=International)
    Field names match the original JSON keys (e.g. 'Ovr', 'Pot', 'Age') for
    drop-in compatibility with existing callers.
    """
    select = """
        SELECT r.player_id AS ID,
               p.name AS Name, p.age AS Age,
               r.ovr AS Ovr, r.pot AS Pot,
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
               r.ifr AS IFR, r.ofr AS OFR, r.ife AS IFE, r.ofe AS OFE, r.tdp AS TDP, r.gb AS GB,
               r.cntct_l AS Cntct_L, r.cntct_r AS Cntct_R,
               r.gap_l AS Gap_L, r.gap_r AS Gap_R,
               r.pow_l AS Pow_L, r.pow_r AS Pow_R,
               r.eye_l AS Eye_L, r.eye_r AS Eye_R,
               r.ks_l AS Ks_L, r.ks_r AS Ks_R,
               r.stf_l AS Stf_L, r.stf_r AS Stf_R,
               r.mov_l AS Mov_L, r.mov_r AS Mov_R,
               r.int_ AS Int, r.wrk_ethic AS WrkEthic, r.greed AS Greed,
               r.loy AS Loy, r.lead AS Lead, r.acc AS Acc,
               p.pos AS Pos, p.role AS Role, r.league_id AS League
        FROM ratings r
        JOIN players p ON r.player_id = p.player_id
        WHERE (p.team_id = ? OR p.parent_team_id = ?)
    """
    level_clause = " AND p.level = ?" if level is not None else ""
    base_params = (parent_team_id, parent_team_id)
    level_params = (str(level),) if level is not None else ()
    with get_conn() as conn:
        if snapshot_date:
            rows = conn.execute(select + level_clause + " AND r.snapshot_date = ?",
                                base_params + level_params + (snapshot_date,)).fetchall()
        else:
            rows = conn.execute(select + level_clause + """
                AND r.snapshot_date = (
                    SELECT MAX(r2.snapshot_date) FROM ratings r2
                    WHERE r2.player_id = r.player_id
                )
            """, base_params + level_params).fetchall()
    return [dict(r) for r in rows]


def get_contracts(parent_team_id: int) -> list[dict]:
    salary_cols = ", ".join(f"salary_{i} AS salary{i}" for i in range(15))
    with get_conn() as conn:
        rows = conn.execute(
            f"SELECT player_id, team_id, contract_team_id, is_major, season_year, "
            f"years, current_year, {salary_cols}, no_trade, "
            f"last_year_team_option, last_year_player_option "
            f"FROM contracts WHERE contract_team_id = ? OR team_id = ?",
            (parent_team_id, parent_team_id)
        ).fetchall()
    return [dict(r) for r in rows]


def get_batting_stats(parent_team_id: int, year: int, split_id: int = 1) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT s.* FROM mlb_batting_stats s
            JOIN players p ON s.player_id = p.player_id
            WHERE (p.team_id = ? OR p.parent_team_id = ?) AND s.year = ? AND s.split_id = ?
        """, (parent_team_id, parent_team_id, year, split_id)).fetchall()
    return [dict(r) for r in rows]


def get_pitching_stats(parent_team_id: int, year: int, split_id: int = 1) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT s.* FROM mlb_pitching_stats s
            JOIN players p ON s.player_id = p.player_id
            WHERE (p.team_id = ? OR p.parent_team_id = ?) AND s.year = ? AND s.split_id = ?
        """, (parent_team_id, parent_team_id, year, split_id)).fetchall()
    return [dict(r) for r in rows]
