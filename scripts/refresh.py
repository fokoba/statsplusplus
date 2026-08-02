#!/usr/bin/env python3
"""
Usage:
  python3 scripts/refresh.py [year]                    # Full league refresh
  python3 scripts/refresh.py state <game_date> [year]  # Update state only

Game date is fetched automatically from the API. State is updated and fv_calc
is run at the end — single command does everything.

Targets the active league from data/app_config.json. Year defaults to the
league's configured year.
"""

import json, os, sys
from datetime import datetime, timezone
from pathlib import Path

BASE = Path(__file__).parent.parent
sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(BASE))
from statsplus import client
import db as _db
from league_config import config as _cfg
from league_context import get_league_dir
from log_config import get_logger

log = get_logger("refresh")


def _upsert_teams(conn, teams):
    conn.executemany(
        "INSERT OR REPLACE INTO teams VALUES (?,?,?,?,?)",
        [(t["ID"], t.get("Name",""), t.get("Level",""), t.get("Parent Team ID"), t.get("League",""))
         for t in teams]
    )

def _upsert_players(conn, players):
    _PLAYER_COLS = (
        "player_id", "name", "age", "team_id", "parent_team_id", "level", "pos", "role",
        # Injury
        "injury_is_injured", "injury_dl_left", "injury_left",
        "is_on_dl", "is_on_dl60", "dl_days_this_year",
        # Service time
        "mlb_service_years", "mlb_service_days", "mlb_service_days_this_year",
        "pro_service_years", "pro_service_days",
        # Roster status
        "is_active", "is_on_secondary", "is_on_waivers",
        "designated_for_assignment", "free_agent", "was_traded",
        "days_on_waivers", "days_on_waivers_left", "has_received_arbitration",
        # Draft info
        "draft_year", "draft_round", "draft_pick", "draft_overall_pick", "draft_team_id",
        # Demographics
        "date_of_birth", "weight", "nation_id", "uniform_number",
        # Our own free-agent/retirement tracking (not part of the StatsPlus
        # API expansion above)
        "retired", "draft_eligible",
    )
    col_list = ",".join(_PLAYER_COLS)
    placeholders = ",".join(["?"] * len(_PLAYER_COLS))

    def row(p):
        return (
            p["ID"],
            f"{p.get('First Name', '')} {p.get('Last Name', '')}".strip(),
            p.get("Age"), p.get("Team ID"), p.get("Parent Team ID"),
            p.get("Level"), p.get("Pos"), p.get("Role"),
            # Injury
            p.get("injury_is_injured"), p.get("injury_dl_left"), p.get("injury_left"),
            p.get("is_on_dl"), p.get("is_on_dl60"), p.get("dl_days_this_year"),
            # Service time
            p.get("mlb_service_years"), p.get("mlb_service_days"), p.get("mlb_service_days_this_year"),
            p.get("pro_service_years"), p.get("pro_service_days"),
            # Roster status
            p.get("is_active"), p.get("is_on_secondary"), p.get("is_on_waivers"),
            p.get("designated_for_assignment"), p.get("free_agent"), p.get("was_traded"),
            p.get("days_on_waivers"), p.get("days_on_waivers_left"), p.get("has_received_arbitration"),
            # Draft info
            p.get("draft_year"), p.get("draft_round"), p.get("draft_pick"),
            p.get("draft_overall_pick"), p.get("draft_team_id"),
            # Demographics
            p.get("date_of_birth"), p.get("weight"), p.get("nation_id"), p.get("uniform_number"),
            p.get("Retired") or 0, p.get("draft_eligible") or 0,
        )

    conn.executemany(
        f"INSERT OR REPLACE INTO players ({col_list}) VALUES ({placeholders})",
        [row(p) for p in players]
    )

def _upsert_ratings(conn, ratings, snapshot_date, keep_history=True):
    # Column names matching the DB schema. The INSERT uses explicit column names
    # so it works regardless of column order (existing DBs with ALTER TABLE ADD
    # have new columns appended at the end, not in schema order).
    _RATING_COLS = (
        "player_id", "snapshot_date", "ovr", "pot",
        "cntct", "gap", "pow", "eye", "ks",
        "babip",
        "speed", "steal",
        "stf", "mov", "ctrl", "ctrl_r", "ctrl_l",
        "hra", "pbabip",
        "fst", "snk", "crv", "sld", "chg",
        "splt", "cutt", "cir_chg", "scr",
        "frk", "kncrv", "knbl", "stm", "vel",
        "pot_stf", "pot_mov", "pot_ctrl",
        "pot_hra", "pot_pbabip",
        "pot_fst", "pot_snk", "pot_crv", "pot_sld",
        "pot_chg", "pot_splt", "pot_cutt",
        "pot_cir_chg", "pot_scr", "pot_frk",
        "pot_kncrv", "pot_knbl",
        "pot_cntct", "pot_gap", "pot_pow", "pot_eye", "pot_ks",
        "pot_babip",
        "c", "ss", "second_b", "third_b",
        "first_b", "lf", "cf", "rf",
        "pot_c", "pot_ss", "pot_second_b", "pot_third_b",
        "pot_first_b", "pot_lf", "pot_cf", "pot_rf",
        "p", "pot_p",
        "ofa", "ifa", "c_arm", "c_blk", "c_frm",
        "ifr", "ofr",
        "ife", "ofe", "tdp", "gb",
        "cntct_l", "cntct_r", "gap_l", "gap_r",
        "pow_l", "pow_r", "eye_l", "eye_r",
        "ks_l", "ks_r",
        "babip_l", "babip_r",
        "stf_l", "stf_r", "mov_l", "mov_r",
        "hra_l", "hra_r", "pbabip_l", "pbabip_r",
        "int_", "wrk_ethic", "greed", "loy", "lead",
        "prone", "acc",
        "league_id",
        "height", "bats", "throws",
        "stl_rt", "run", "sac_bunt", "bunt_hit", "hold",
    )

    def row(r):
        # Order MUST match _RATING_COLS above.
        return (
            r["ID"], snapshot_date, r.get("Ovr"), r.get("Pot"),
            r.get("Cntct"), r.get("Gap"), r.get("Pow"), r.get("Eye"), r.get("Ks"),
            r.get("BABIP"),
            r.get("Speed"), r.get("Steal"),
            r.get("Stf"), r.get("Mov"), r.get("Ctrl"), r.get("Ctrl_R"), r.get("Ctrl_L"),
            r.get("HRA"), r.get("PBABIP"),
            r.get("Fst"), r.get("Snk"), r.get("Crv"), r.get("Sld"), r.get("Chg"),
            r.get("Splt"), r.get("Cutt"), r.get("CirChg"), r.get("Scr"),
            r.get("Frk"), r.get("Kncrv"), r.get("Knbl"), r.get("Stm"), r.get("Vel"),
            r.get("PotStf"), r.get("PotMov"), r.get("PotCtrl"),
            r.get("PotHRA"), r.get("PotPBABIP"),
            r.get("PotFst"), r.get("PotSnk"), r.get("PotCrv"), r.get("PotSld"),
            r.get("PotChg"), r.get("PotSplt"), r.get("PotCutt"),
            r.get("PotCirChg"), r.get("PotScr"), r.get("PotFrk"),
            r.get("PotKncrv"), r.get("PotKnbl"),
            r.get("PotCntct"), r.get("PotGap"), r.get("PotPow"), r.get("PotEye"), r.get("PotKs"),
            r.get("PotBABIP"),
            r.get("C"), r.get("SS"), r.get("2B"), r.get("3B"),
            r.get("1B"), r.get("LF"), r.get("CF"), r.get("RF"),
            r.get("PotC"), r.get("PotSS"), r.get("Pot2B"), r.get("Pot3B"),
            r.get("Pot1B"), r.get("PotLF"), r.get("PotCF"), r.get("PotRF"),
            r.get("P"), r.get("PotP"),
            r.get("OFA"), r.get("IFA"), r.get("CArm"), r.get("CBlk"), r.get("CFrm"),
            r.get("IFR"), r.get("OFR"),
            r.get("IFE"), r.get("OFE"), r.get("TDP"), r.get("GB"),
            r.get("Cntct_L"), r.get("Cntct_R"), r.get("Gap_L"), r.get("Gap_R"),
            r.get("Pow_L"), r.get("Pow_R"), r.get("Eye_L"), r.get("Eye_R"),
            r.get("Ks_L"), r.get("Ks_R"),
            r.get("BABIP_L"), r.get("BABIP_R"),
            r.get("Stf_L"), r.get("Stf_R"), r.get("Mov_L"), r.get("Mov_R"),
            r.get("HRA_L"), r.get("HRA_R"), r.get("PBABIP_L"), r.get("PBABIP_R"),
            r.get("Int"), r.get("WrkEthic"), r.get("Greed"), r.get("Loy"), r.get("Lead"),
            r.get("Prone"), r.get("Acc"),
            r.get("League"),
            r.get("Height"), r.get("Bats"), r.get("Throws"),
            r.get("StlRt"), r.get("Run"), r.get("SacBunt"), r.get("BuntHit"), r.get("Hold"),
        )
    if not ratings:
        log.info("  ratings: empty response, skipping upsert")
        return
    col_list = ",".join(_RATING_COLS)
    placeholders = ",".join(["?"] * len(_RATING_COLS))
    verb = "INSERT OR IGNORE" if keep_history else "INSERT OR REPLACE"
    conn.executemany(
        f"{verb} INTO ratings ({col_list}) VALUES ({placeholders})",
        [row(r) for r in ratings]
    )
    # Backfill demographics (height/bats/throws) on existing rows that have NULLs
    conn.executemany(
        "UPDATE ratings SET height=?, bats=?, throws=? WHERE player_id=? AND height IS NULL",
        [(r.get("Height"), r.get("Bats"), r.get("Throws"), r["ID"])
         for r in ratings if r.get("Height") is not None]
    )
    # Backfill extended ratings (babip/hra/pbabip/prone) on existing rows that have NULLs
    if any(r.get("BABIP") is not None for r in ratings[:10]):
        _EXT_COLS = [
            ("babip", "BABIP"), ("babip_l", "BABIP_L"), ("babip_r", "BABIP_R"), ("pot_babip", "PotBABIP"),
            ("hra", "HRA"), ("hra_l", "HRA_L"), ("hra_r", "HRA_R"), ("pot_hra", "PotHRA"),
            ("pbabip", "PBABIP"), ("pbabip_l", "PBABIP_L"), ("pbabip_r", "PBABIP_R"), ("pot_pbabip", "PotPBABIP"),
            ("prone", "Prone"),
        ]
        set_clause = ", ".join(f"{db}=?" for db, _ in _EXT_COLS)
        conn.executemany(
            f"UPDATE ratings SET {set_clause} WHERE player_id=? AND babip IS NULL",
            [tuple(r.get(api) for _, api in _EXT_COLS) + (r["ID"],) for r in ratings]
        )

def _snapshot_ratings_history(conn, ratings, snapshot_date):
    """Append a monthly snapshot to ratings_history (one per in-game month)."""
    if not ratings:
        return
    # Check if this month already has a snapshot
    month = snapshot_date[:7]  # "YYYY-MM"
    existing = conn.execute(
        "SELECT 1 FROM ratings_history WHERE snapshot_date LIKE ? LIMIT 1",
        (month + "%",)
    ).fetchone()
    if existing:
        return

    _HIST_COLS = (
        "player_id", "snapshot_date", "ovr", "pot",
        "cntct", "gap", "pow", "eye", "ks", "speed", "stm",
        "stf", "mov", "ctrl",
        "fst", "snk", "crv", "sld", "chg", "splt", "cutt", "cir_chg", "scr", "frk", "kncrv", "knbl",
        "pot_cntct", "pot_gap", "pot_pow", "pot_eye", "pot_ks",
        "pot_stf", "pot_mov", "pot_ctrl",
        "pot_fst", "pot_snk", "pot_crv", "pot_sld", "pot_chg", "pot_splt", "pot_cutt",
        "pot_cir_chg", "pot_scr", "pot_frk", "pot_kncrv", "pot_knbl",
        "babip", "hra", "pbabip", "pot_babip", "pot_hra", "pot_pbabip", "prone",
    )
    _API_KEYS = (
        "ID", None, "Ovr", "Pot",
        "Cntct", "Gap", "Pow", "Eye", "Ks", "Speed", "Stm",
        "Stf", "Mov", "Ctrl",
        "Fst", "Snk", "Crv", "Sld", "Chg", "Splt", "Cutt", "CirChg", "Scr", "Frk", "Kncrv", "Knbl",
        "PotCntct", "PotGap", "PotPow", "PotEye", "PotKs",
        "PotStf", "PotMov", "PotCtrl",
        "PotFst", "PotSnk", "PotCrv", "PotSld", "PotChg", "PotSplt", "PotCutt",
        "PotCirChg", "PotScr", "PotFrk", "PotKncrv", "PotKnbl",
        "BABIP", "HRA", "PBABIP", "PotBABIP", "PotHRA", "PotPBABIP", "Prone",
    )

    def row(r):
        vals = []
        for key in _API_KEYS:
            if key is None:
                vals.append(snapshot_date)
            else:
                vals.append(r.get(key))
        return tuple(vals)

    col_list = ",".join(_HIST_COLS)
    placeholders = ",".join(["?"] * len(_HIST_COLS))
    conn.executemany(
        f"INSERT OR IGNORE INTO ratings_history ({col_list}) VALUES ({placeholders})",
        [row(r) for r in ratings]
    )
    log.info(f"  ratings_history: saved {len(ratings)} rows for {snapshot_date}")


def _upsert_contracts(conn, contracts):
    _COLS = (
        "player_id", "team_id", "contract_team_id", "is_major",
        "season_year", "years", "current_year",
        "salary_0", "salary_1", "salary_2", "salary_3",
        "salary_4", "salary_5", "salary_6", "salary_7",
        "salary_8", "salary_9", "salary_10", "salary_11",
        "salary_12", "salary_13", "salary_14",
        "no_trade", "last_year_team_option", "last_year_player_option",
        "last_year_vesting_option", "last_year_option_buyout",
        "next_last_year_team_option", "next_last_year_player_option",
        "next_last_year_vesting_option", "next_last_year_option_buyout",
        "minimum_pa", "minimum_pa_bonus", "minimum_ip", "minimum_ip_bonus",
        "mvp_bonus", "cyyoung_bonus", "allstar_bonus",
    )
    col_list = ",".join(_COLS)
    placeholders = ",".join(["?"] * len(_COLS))

    def row(c):
        return (
            c["player_id"], c.get("team_id"), c.get("contract_team_id"), c.get("is_major"),
            c.get("season_year"), c.get("years"), c.get("current_year"),
            *[c.get(f"salary{i}") for i in range(15)],
            c.get("no_trade"), c.get("last_year_team_option"), c.get("last_year_player_option"),
            c.get("last_year_vesting_option"), c.get("last_year_option_buyout"),
            c.get("next_last_year_team_option"), c.get("next_last_year_player_option"),
            c.get("next_last_year_vesting_option"), c.get("next_last_year_option_buyout"),
            c.get("minimum_pa"), c.get("minimum_pa_bonus"),
            c.get("minimum_ip"), c.get("minimum_ip_bonus"),
            c.get("mvp_bonus"), c.get("cyyoung_bonus"), c.get("allstar_bonus"),
        )
    conn.executemany(
        f"INSERT OR REPLACE INTO contracts ({col_list}) VALUES ({placeholders})",
        [row(c) for c in contracts]
    )

def _upsert_extensions(conn, extensions):
    conn.execute("DELETE FROM contract_extensions")
    rows = []
    for e in extensions:
        if e.get("years", 0) <= 0:
            continue
        rows.append((
            e["player_id"], e.get("team_id"), e.get("years"), e.get("current_year"),
            *[e.get(f"salary{i}", 0) for i in range(15)],
            e.get("no_trade", 0), e.get("last_year_team_option", 0), e.get("last_year_player_option", 0),
        ))
    if rows:
        conn.executemany(
            f"INSERT OR REPLACE INTO contract_extensions VALUES ({','.join(['?']*22)})",
            rows
        )

def _upsert_batting(conn, rows, league_id=None):
    def _avg(r): return r.get("h", 0) / r["ab"] if r.get("ab") else None
    def _obp(r):
        h, bb, hp, ab, sf = r.get("h",0), r.get("bb",0), r.get("hp",0), r.get("ab",0), r.get("sf",0)
        denom = ab + bb + hp + sf
        return (h + bb + hp) / denom if denom else None
    def _slg(r):
        ab = r.get("ab", 0)
        if not ab: return None
        return (r.get("h",0) + r.get("d",0) + 2*r.get("t",0) + 3*r.get("hr",0)) / ab

    n_cols = 33  # 32 original + league_id
    conn.executemany(
        "INSERT OR REPLACE INTO batting_stats VALUES (" + ",".join(["?"]*n_cols) + ")",
        [(r["player_id"], r.get("year"), r.get("team_id"), r.get("split_id"),
          r.get("ab"), r.get("h"), r.get("d"), r.get("t"), r.get("hr"),
          r.get("r"), r.get("rbi"), r.get("sb"), r.get("bb"), r.get("k"),
          _avg(r), _obp(r), _slg(r), r.get("war"),
          r.get("pa"), r.get("stint"), r.get("hp"), r.get("sf"),
          r.get("g"), r.get("gs"), r.get("cs"), r.get("gdp"), r.get("ibb"),
          r.get("sh"), r.get("ci"), r.get("pitches_seen"), r.get("ubr"), r.get("wpa"),
          league_id)
         for r in rows]
    )

def _upsert_pitching(conn, rows, league_id=None):
    def _ip(outs):
        return outs / 3 if outs else 0.0
    def _era(r):
        outs = r.get("outs") or 0
        er = r.get("er") or 0
        return round(er * 27 / outs, 2) if outs > 0 else 0.0

    n_cols = 53  # 52 original + league_id
    conn.executemany(
        "INSERT OR REPLACE INTO pitching_stats VALUES (" + ",".join(["?"]*n_cols) + ")",
        [(r["player_id"], r.get("year"), r.get("team_id"), r.get("split_id"),
          _ip(r.get("outs")), r.get("g"), r.get("gs"), r.get("w"), r.get("l"), r.get("s"),
          _era(r), r.get("k"), r.get("bb"), r.get("ha"), r.get("war"),
          r.get("outs"), r.get("stint"), r.get("ra9war"), r.get("hra"), r.get("bf"), r.get("hp"),
          r.get("ab"), r.get("er"), r.get("r"), r.get("cg"), r.get("sho"), r.get("gf"),
          r.get("hld"), r.get("bs"), r.get("svo"), r.get("qs"),
          r.get("gb"), r.get("fb"), r.get("pi"), r.get("wp"), r.get("bk"),
          r.get("iw"), r.get("ir"), r.get("irs"), r.get("rs"), r.get("dp"),
          r.get("sb"), r.get("cs"), r.get("sf"), r.get("sh"), r.get("ci"),
          r.get("tb"), r.get("li"), r.get("wpa"), r.get("ra"), r.get("md"), r.get("sd"),
          league_id)
         for r in rows]
    )



def _upsert_fielding(conn, rows, league_id=None):
    n_cols = 19  # 18 original + league_id
    conn.executemany(
        "INSERT OR REPLACE INTO fielding_stats VALUES (" + ",".join(["?"]*n_cols) + ")",
        [(r["player_id"], r.get("year"), r.get("team_id"), r.get("position"),
          r.get("g"), r.get("gs"), (r.get("ip") or 0) / 3,
          r.get("tc"), r.get("a"), r.get("po"),
          r.get("e"), r.get("dp"), r.get("pb"), r.get("sba"), r.get("rto"),
          r.get("zr"), r.get("framing"), r.get("arm"),
          league_id)
         for r in rows]
    )


def _upsert_games(conn, rows):
    conn.executemany("""
        INSERT OR REPLACE INTO games (game_id, home_team, away_team, date, runs0, runs1, game_type, played, winning_pitcher, losing_pitcher, save_pitcher)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)
    """, [(r.get("game_id"), r.get("home_team"), r.get("away_team"), r.get("date"),
           r.get("runs0"), r.get("runs1"), r.get("game_type"), r.get("played"),
           r.get("winning_pitcher"), r.get("losing_pitcher"), r.get("save_pitcher")) for r in rows])

def _upsert_team_stats(conn, year):
    tb = client.get_team_batting_stats(year=year, split=1)
    tp = client.get_team_pitching_stats(year=year, split=1)
    conn.executemany(
        "INSERT OR REPLACE INTO team_batting_stats VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [(r["tid"], year, r.get("split_id", 1), r.get("name", ""),
          r.get("pa"), r.get("ab"), r.get("h"), r.get("k"), r.get("hr"),
          r.get("r"), r.get("rbi"), r.get("bb"), r.get("sb"),
          r.get("avg"), r.get("obp"), r.get("slg"), r.get("ops"), r.get("iso"),
          r.get("k_pct"), r.get("bb_pct"), r.get("babip"), r.get("woba"))
         for r in tb]
    )
    conn.executemany(
        "INSERT OR REPLACE INTO team_pitching_stats VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [(r["tid"], year, r.get("split_id", 1), r.get("name", ""),
          r.get("outs") / 3 if r.get("outs") else r.get("ip"),
          r.get("era"), r.get("k"), r.get("bb"), r.get("ha"),
          r.get("r"), r.get("er"), r.get("hra"), r.get("g", 0),
          r.get("k_pct"), r.get("bb_pct"), r.get("fip"), r.get("babip"),
          r.get("avg"), r.get("obp"))
         for r in tp]
    )
    log.info(f"  team batting: {len(tb)}, team pitching: {len(tp)}")


def _detect_league_structure(conn, year):
    """Auto-detect divisions and leagues from game history and team stats API.

    Returns (divisions, leagues, team_abbr, team_names) or None if insufficient data.
    Uses game frequency between teams to cluster into divisions, then inter-division
    frequency to group divisions into leagues.
    """
    from collections import Counter, defaultdict

    # Get team ordering + abbreviations from API (teams come grouped by division)
    bat_stats = client.get_team_batting_stats(year=year, split=1)
    if not bat_stats:
        bat_stats = client.get_team_batting_stats(year=year - 1, split=1)
    if not bat_stats:
        return None
    seen = set()
    api_order = []  # [(tid, abbr, name), ...] in API order
    for s in bat_stats:
        if s["tid"] not in seen:
            seen.add(s["tid"])
            api_order.append((s["tid"], s.get("abbr", "?"), s.get("name", "?")))

    mlb_tids = {t[0] for t in api_order}
    if len(mlb_tids) < 6:
        return None

    # Build pairwise game counts from all history
    games = conn.execute(
        "SELECT home_team, away_team FROM games WHERE played=1 AND game_type=0"
    ).fetchall()
    pair_counts = Counter()
    for h, a in games:
        if h in mlb_tids and a in mlb_tids:
            pair_counts[tuple(sorted([h, a]))] += 1

    team_vs = defaultdict(Counter)
    for (t1, t2), cnt in pair_counts.items():
        team_vs[t1][t2] = cnt
        team_vs[t2][t1] = cnt

    # Cluster into divisions using game frequency.
    # For each team with enough games, its division mates are the teams it plays
    # most frequently — identified by the largest gap in opponent frequency.
    parent = {t: t for t in mlb_tids}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        a, b = find(a), find(b)
        if a != b:
            parent[b] = a

    clustered = set()
    for tid in mlb_tids:
        opps = team_vs[tid].most_common()
        if len(opps) < 6:
            continue  # expansion team with too few games — handle later
        freqs = [c for _, c in opps]
        gaps = [(freqs[i] - freqs[i + 1], i + 1) for i in range(min(7, len(freqs) - 1))]
        best_gap, best_pos = max(gaps, key=lambda x: x[0])
        if best_gap >= 20:
            for opp, _ in opps[:best_pos]:
                union(tid, opp)
            clustered.add(tid)

    # Group into divisions
    div_map = defaultdict(set)
    for tid in mlb_tids:
        div_map[find(tid)].add(tid)

    # --- Fallback: detect isolated leagues (no inter-league play) ---
    # When clustering fails (no frequency gap) but teams form disconnected
    # groups (some pairs have 0 games), use connectivity to identify leagues.
    # This handles leagues with no divisions and no interleague play.
    if len(div_map) == len(mlb_tids):
        # Every team is its own "division" — clustering found nothing.
        # Check if teams form natural connected components (play each other).
        all_pairs = set(pair_counts.keys())
        # Build adjacency from game data
        adj = defaultdict(set)
        for (t1, t2) in all_pairs:
            adj[t1].add(t2)
            adj[t2].add(t1)
        # BFS to find connected components
        visited = set()
        components = []
        for tid in mlb_tids:
            if tid in visited:
                continue
            component = set()
            queue = [tid]
            while queue:
                curr = queue.pop()
                if curr in visited:
                    continue
                visited.add(curr)
                component.add(curr)
                for nbr in adj.get(curr, set()):
                    if nbr not in visited and nbr in mlb_tids:
                        queue.append(nbr)
            components.append(component)

        if 2 <= len(components) <= 4 and all(len(c) >= 4 for c in components):
            # Found isolated leagues — treat each component as a single division
            log.info(f"  Detected {len(components)} isolated leagues (no inter-league play)")
            div_map = {min(c): c for c in components}
            # Union-find within each component
            for component in components:
                rep = min(component)
                for tid in component:
                    parent[tid] = rep

    # Assign unclustered teams (expansion with few games) using API ordering.
    # They sit between two division groups in the API — assign to the preceding one.
    unclustered = mlb_tids - clustered
    if unclustered:
        api_tids = [t[0] for t in api_order]
        for tid in unclustered:
            idx = api_tids.index(tid)
            # Walk backward to find the nearest clustered team
            for i in range(idx - 1, -1, -1):
                if api_tids[i] in clustered:
                    union(tid, api_tids[i])
                    break
        # Rebuild div_map
        div_map = defaultdict(set)
        for tid in mlb_tids:
            div_map[find(tid)].add(tid)

    divisions_list = list(div_map.values())

    # --- Standard 30-team MLB fallback ---
    # When we detect exactly 30 teams in 6 divisions of 5, use the canonical
    # MLB division/league assignments. The inter-division game frequency
    # heuristic can't reliably distinguish AL from NL in interleague-heavy
    # schedules, but the team-to-division mapping is unambiguous.
    _STANDARD_MLB = {
        "AL": {
            "East":    {"BAL", "BOS", "NYY", "TB", "TOR"},
            "Central": {"CWS", "CLE", "DET", "KC", "MIN"},
            "West":    {"HOU", "LAA", "OAK", "SEA", "TEX"},
        },
        "NL": {
            "East":    {"ATL", "MIA", "NYM", "PHI", "WSH"},
            "Central": {"CHC", "CIN", "MIL", "PIT", "STL"},
            "West":    {"ARI", "COL", "LAD", "SD", "SF"},
        },
    }

    if (len(mlb_tids) == 30
            and len(divisions_list) == 6
            and all(len(d) == 5 for d in divisions_list)):
        # Map abbreviations to team IDs
        abbr_to_tid = {t[1]: t[0] for t in api_order}
        # Check if every team's abbreviation matches the standard set
        all_abbrs = {t[1] for t in api_order}
        std_abbrs = set()
        for lg in _STANDARD_MLB.values():
            for div in lg.values():
                std_abbrs.update(div)
        if std_abbrs <= all_abbrs:
            log.info("  30/6/5 detected — using standard MLB divisions")
            abbr_map = {t[0]: t[1] for t in api_order}
            name_rows = conn.execute(
                "SELECT t.team_id, t.name FROM teams t WHERE t.team_id IN ({})".format(
                    ",".join(str(t) for t in mlb_tids))
            ).fetchall()
            full_names = {r[0]: r[1] for r in name_rows}
            api_teams = client.get_teams()
            nick_map = {t["ID"]: t.get("Nickname", "") for t in api_teams}
            team_names_out = {str(tid): f"{full_names.get(tid, '?')} {nick_map.get(tid, '')}".strip()
                              for tid in mlb_tids}
            team_abbr_out = {str(tid): abbr_map.get(tid, "?") for tid in mlb_tids}
            leagues_out = []
            divisions_out = {}
            colors = ["#508cff", "#ff6b6b"]
            for lg_idx, (lg_name, divs) in enumerate(_STANDARD_MLB.items()):
                lg_obj = {"name": lg_name, "short": lg_name,
                          "color": colors[lg_idx], "divisions": {}}
                for div_name, abbrs in divs.items():
                    tids = sorted(abbr_to_tid[a] for a in abbrs if a in abbr_to_tid)
                    lg_obj["divisions"][div_name] = tids
                    divisions_out[f"{lg_name} {div_name}"] = tids
                leagues_out.append(lg_obj)
            return divisions_out, leagues_out, team_abbr_out, team_names_out

    # --- General case: group divisions into leagues by game frequency ---
    n_divs = len(divisions_list)
    inter_avg = {}
    for i in range(n_divs):
        for j in range(i + 1, n_divs):
            total = sum(pair_counts.get(tuple(sorted([t1, t2])), 0)
                        for t1 in divisions_list[i] for t2 in divisions_list[j])
            pairs = len(divisions_list[i]) * len(divisions_list[j])
            inter_avg[(i, j)] = total / pairs if pairs else 0

    # Cluster divisions into leagues: union-find on divisions
    div_parent = list(range(n_divs))

    def dfind(x):
        while div_parent[x] != x:
            div_parent[x] = div_parent[div_parent[x]]
            x = div_parent[x]
        return x

    def dunion(a, b):
        a, b = dfind(a), dfind(b)
        if a != b:
            div_parent[b] = a

    # Threshold: same-league divisions play ~40+ games/pair on average
    threshold = 35
    for (i, j), avg in inter_avg.items():
        if avg >= threshold:
            dunion(i, j)

    league_groups = defaultdict(list)
    for i in range(n_divs):
        league_groups[dfind(i)].append(i)

    # Name divisions and leagues using API order.
    # API returns teams grouped by division within each league.
    # Use the API ordering to assign division names (East/Central/West).
    api_tids = [t[0] for t in api_order]

    def div_api_position(div_set):
        """Average position of division's teams in API order."""
        return sum(api_tids.index(t) for t in div_set if t in api_tids) / len(div_set)

    # Build team_abbr and team_names from API data
    abbr_map = {t[0]: t[1] for t in api_order}
    # Get full names from teams table
    name_rows = conn.execute(
        "SELECT t.team_id, t.name FROM teams t WHERE t.team_id IN ({})".format(
            ",".join(str(t) for t in mlb_tids))
    ).fetchall()
    full_names = {r[0]: r[1] for r in name_rows}
    # Get nicknames from API
    api_teams = client.get_teams()
    nick_map = {t["ID"]: t.get("Nickname", "") for t in api_teams}
    team_names_out = {str(tid): f"{full_names.get(tid, '?')} {nick_map.get(tid, '')}".strip()
                      for tid in mlb_tids}
    team_abbr_out = {str(tid): abbr_map.get(tid, "?") for tid in mlb_tids}

    # Standard division name patterns by count
    _DIV_NAMES = {
        2: ["East", "West"],
        3: ["East", "Central", "West"],
        4: ["East", "North", "South", "West"],
    }

    # Traditional AL team IDs — used to assign AL/NL labels when there are 2 leagues
    _TRAD_AL = {33, 34, 35, 38, 40, 42, 43, 44, 47, 48, 50, 54, 57, 58, 59}

    # Build leagues array and flat divisions dict
    leagues_out = []
    divisions_out = {}
    colors = ["#508cff", "#ff6b6b", "#50c878", "#ffa500"]

    sorted_groups = sorted(league_groups.items())
    n_leagues = len(sorted_groups)

    # For 2-league setups, determine which is AL by traditional team membership
    if n_leagues == 2:
        group_al_counts = []
        for _, div_indices in sorted_groups:
            tids_in_group = set()
            for di in div_indices:
                tids_in_group.update(divisions_list[di])
            group_al_counts.append(len(tids_in_group & _TRAD_AL))
        # Put the group with more traditional AL teams first
        if group_al_counts[0] < group_al_counts[1]:
            sorted_groups = [sorted_groups[1], sorted_groups[0]]

    for lg_idx, (lg_root, div_indices) in enumerate(sorted_groups):
        # Sort divisions within league by API position
        div_indices_sorted = sorted(div_indices, key=lambda i: div_api_position(divisions_list[i]))
        div_names = _DIV_NAMES.get(len(div_indices_sorted),
                                    [f"Division {j+1}" for j in range(len(div_indices_sorted))])

        if n_leagues == 2:
            lg_name = "AL" if lg_idx == 0 else "NL"
        else:
            lg_name = f"League {lg_idx + 1}"

        lg_obj = {
            "name": lg_name, "short": lg_name,
            "color": colors[lg_idx % len(colors)],
            "divisions": {},
        }

        for j, di in enumerate(div_indices_sorted):
            div_name = div_names[j]
            full_div_name = f"{lg_name} {div_name}"
            tids = sorted(divisions_list[di])
            lg_obj["divisions"][div_name] = tids
            divisions_out[full_div_name] = tids

        leagues_out.append(lg_obj)

    return divisions_out, leagues_out, team_abbr_out, team_names_out


def refresh_league(year, game_date=None):
    """Pull all teams into DB for the active league."""
    import time as _time
    log.info("=== refresh_league started (year=%s) ===", year)
    league_dir = get_league_dir()
    _db.init_schema(league_dir)
    conn = _db.get_conn(league_dir)

    # Kick off ratings export first — it takes 30s+ to generate on the server.
    # If RATINGS_POLL_URL is set (from onboarding auth check), reuse it.
    ratings_poll_url = os.environ.get("RATINGS_POLL_URL", "")
    if ratings_poll_url:
        log.info("── ratings (reusing poll URL from auth check)")
    else:
        log.info("── ratings (kicked off — will collect later)")
        ratings_poll_url = client.start_ratings_export()
    ratings_start = _time.monotonic()

    log.info("── teams")
    _upsert_teams(conn, client.get_teams())

    log.info("── players (all orgs)")
    players = client.get_players()
    _upsert_players(conn, players)
    log.info(f"  {len(players)} players loaded")

    log.info("── contracts (all orgs)")
    contracts = client.get_contracts()
    _upsert_contracts(conn, contracts)
    log.info(f"  {len(contracts)} contracts loaded")

    log.info("── contract extensions")
    _upsert_extensions(conn, client.get_contract_extensions())

    log.info("── batting (all orgs)")
    bat_rows = client.get_player_batting_stats(year=year, split=1)
    _upsert_batting(conn, bat_rows)
    log.info(f"  {len(bat_rows)} batting rows (year={year})")

    log.info("── pitching (all orgs)")
    pit_rows = client.get_player_pitching_stats(year=year, split=1)
    _upsert_pitching(conn, pit_rows)
    log.info(f"  {len(pit_rows)} pitching rows (year={year})")

    # L/R splits for current year (platoon analysis, team roster toggle)
    log.info("── splits (vs L / vs R)")
    for split in (2, 3):
        bat_s = client.get_player_batting_stats(year=year, split=split)
        _upsert_batting(conn, bat_s)
        pit_s = client.get_player_pitching_stats(year=year, split=split)
        _upsert_pitching(conn, pit_s)
    log.info(f"  vs L: {len(bat_s)} bat, {len(pit_s)} pit")

    # Always re-fetch prior year to ensure complete end-of-season stats.
    # Mid-season refreshes may have stored partial data for the prior year;
    # once the season ends, we need the final totals.
    prior_year = year - 1
    log.info(f"── prior year stats (year={prior_year})")
    bat_prior = client.get_player_batting_stats(year=prior_year, split=1)
    _upsert_batting(conn, bat_prior)
    pit_prior = client.get_player_pitching_stats(year=prior_year, split=1)
    _upsert_pitching(conn, pit_prior)
    for split in (2, 3):
        _upsert_batting(conn, client.get_player_batting_stats(year=prior_year, split=split))
        _upsert_pitching(conn, client.get_player_pitching_stats(year=prior_year, split=split))
    log.info(f"  {len(bat_prior)} bat, {len(pit_prior)} pit (year={prior_year}, +splits)")

    # Historical stats — up to 15 prior years for $/WAR, player history, etc.
    # Only pull years not already in the DB (excludes current and prior year
    # which are always fetched above).
    existing_years = {r[0] for r in conn.execute(
        "SELECT DISTINCT year FROM mlb_batting_stats").fetchall()}
    hist_start = year - 15
    hist_years = [y for y in range(hist_start, prior_year) if y not in existing_years]
    if hist_years:
        log.info(f"── historical stats ({hist_years[0]}–{hist_years[-1]}, {len(hist_years)} years)")
        for y in hist_years:
            bat = client.get_player_batting_stats(year=y, split=1)
            _upsert_batting(conn, bat)
            pit = client.get_player_pitching_stats(year=y, split=1)
            _upsert_pitching(conn, pit)
            for split in (2, 3):
                _upsert_batting(conn, client.get_player_batting_stats(year=y, split=split))
                _upsert_pitching(conn, client.get_player_pitching_stats(year=y, split=split))
            fld = client.get_player_fielding_stats(year=y)
            _upsert_fielding(conn, fld)
            _upsert_team_stats(conn, y)
            log.info(f"  {y}: {len(bat)} bat, {len(pit)} pit, {len(fld)} fld (+splits)")
        conn.commit()
    else:
        log.info(f"── historical stats: skipped (all years {hist_start}-{year-1} already in DB)")

    # Backfill splits for years that have overall stats but are missing L/R splits
    existing_split_years = {r[0] for r in conn.execute(
        "SELECT DISTINCT year FROM mlb_batting_stats WHERE split_id=2").fetchall()}
    split_gap_years = [y for y in range(hist_start, prior_year)
                       if y in existing_years and y not in existing_split_years]
    if split_gap_years:
        log.info(f"── splits backfill ({split_gap_years[0]}–{split_gap_years[-1]}, {len(split_gap_years)} years)")
        for y in split_gap_years:
            for split in (2, 3):
                _upsert_batting(conn, client.get_player_batting_stats(year=y, split=split))
                _upsert_pitching(conn, client.get_player_pitching_stats(year=y, split=split))
            log.info(f"  {y}: splits fetched")
        conn.commit()

    # Backfill fielding for years that have batting/pitching but no fielding
    existing_fld_years = {r[0] for r in conn.execute(
        "SELECT DISTINCT year FROM mlb_fielding_stats").fetchall()}
    fld_gap_years = [y for y in range(hist_start, prior_year)
                     if y in existing_years and y not in existing_fld_years]
    if fld_gap_years:
        log.info(f"── fielding backfill ({fld_gap_years[0]}–{fld_gap_years[-1]}, {len(fld_gap_years)} years)")
        for y in fld_gap_years:
            fld = client.get_player_fielding_stats(year=y)
            _upsert_fielding(conn, fld)
            log.info(f"  {y}: {len(fld)} fld")
        conn.commit()

    log.info("── fielding (all orgs)")
    fielding = client.get_player_fielding_stats(year=year)
    _upsert_fielding(conn, fielding)
    # Always re-fetch prior year fielding (same rationale as batting/pitching above)
    fielding_prior = client.get_player_fielding_stats(year=prior_year)
    _upsert_fielding(conn, fielding_prior)
    log.info(f"  {len(fielding)} rows (year={year}), {len(fielding_prior)} rows (year={prior_year})")

    log.info("── team stats")
    _upsert_team_stats(conn, year)

    log.info("── game history")
    games = client.get_game_history(year=year)
    _upsert_games(conn, games)
    log.info(f"  {len(games)} games")

    # Minor league stats — fetch from all discovered MiLB leagues (current year only)
    log.info("── minor league stats")
    try:
        lgdata = client.get_lgdata()
        # Find non-MLB leagues (level > 1) that are active in our league structure
        primary_lid = None
        milb_lids = []
        for lg in lgdata.get("leagues", []):
            if lg.get("primary_league"):
                primary_lid = lg["league_id"]
            elif lg.get("level", 0) in (2, 3, 4, 5, 6) and lg.get("parent_league_id") == primary_lid:
                milb_lids.append(lg["league_id"])

        # Store league hierarchy in settings for downstream use
        settings_path = league_dir / "config" / "league_settings.json"
        if settings_path.exists():
            s = json.loads(settings_path.read_text())
            s["minor_leagues"] = [{"lid": lg["league_id"], "name": lg["name"], "level": lg["level"]}
                                  for lg in lgdata.get("leagues", [])
                                  if lg["league_id"] in milb_lids]
            s["primary_league_id"] = primary_lid
            settings_path.write_text(json.dumps(s, indent=2) + "\n")

        if milb_lids:
            total_bat = total_pit = 0
            for lid in milb_lids:
                bat = client.get_player_batting_stats(year=year, split=1, lid=lid)
                _upsert_batting(conn, bat, league_id=lid)
                total_bat += len(bat)
                pit = client.get_player_pitching_stats(year=year, split=1, lid=lid)
                _upsert_pitching(conn, pit, league_id=lid)
                total_pit += len(pit)
            log.info(f"  {len(milb_lids)} leagues, {total_bat} batting rows, {total_pit} pitching rows")

            # Backfill prior-year MiLB stats (up to 5 years back)
            # Only fetches years not already stored. On first run after this
            # feature ships, fetches all available history; subsequently only
            # adds the previous year when a new season starts.
            existing_milb_years = {r[0] for r in conn.execute(
                "SELECT DISTINCT year FROM batting_stats WHERE league_id IS NOT NULL"
            ).fetchall()}
            milb_hist_start = year - 5
            milb_hist_years = [y for y in range(milb_hist_start, year)
                               if y not in existing_milb_years]
            if milb_hist_years:
                log.info(f"  MiLB history backfill: {milb_hist_years}")
                for y in milb_hist_years:
                    yr_bat = yr_pit = 0
                    for lid in milb_lids:
                        try:
                            bat = client.get_player_batting_stats(year=y, split=1, lid=lid)
                            _upsert_batting(conn, bat, league_id=lid)
                            yr_bat += len(bat)
                            pit = client.get_player_pitching_stats(year=y, split=1, lid=lid)
                            _upsert_pitching(conn, pit, league_id=lid)
                            yr_pit += len(pit)
                        except Exception:
                            pass  # individual league/year failures non-fatal
                    if yr_bat or yr_pit:
                        log.info(f"    {y}: {yr_bat} bat, {yr_pit} pit")
                conn.commit()

            # Compute per-league MiLB averages for stat normalization
            conn.commit()  # ensure upserted data is visible
            _compute_milb_averages(conn, milb_lids, year, league_dir)
        else:
            log.info("  no minor leagues found (primary_lid=%s)", primary_lid)
    except Exception as e:
        log.warning(f"  MiLB stats failed (non-fatal): {e}")

    conn.commit()

    # Trade block — players explicitly made available
    log.info("── trade block")
    try:
        tb = client.get_tradeblock()
        player_ids = tb.get("player_ids", [])
        conn.execute("DELETE FROM trade_block")
        if player_ids:
            conn.executemany(
                "INSERT OR REPLACE INTO trade_block (player_id, fetched_date) VALUES (?, ?)",
                [(pid, game_date) for pid in player_ids]
            )
        log.info(f"  {len(player_ids)} players on trade block")
    except Exception as e:
        log.warning(f"  trade block failed (non-fatal): {e}")

    # Standings from /lgdata — real W-L-GB
    log.info("── standings")
    try:
        lgdata = client.get_lgdata()
        standings_rows = lgdata.get("standings", [])
        if standings_rows:
            conn.execute("DELETE FROM standings")
            conn.executemany(
                "INSERT OR REPLACE INTO standings (team_id, w, l, t, pct, gb, pos, streak, magic_number, fetched_date) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [(s["team_id"], s.get("w", 0), s.get("l", 0), s.get("t", 0),
                  s.get("pct", 0), s.get("gb", 0), s.get("pos", 0),
                  s.get("streak", 0), s.get("magic_number", 0), game_date)
                 for s in standings_rows]
            )
            log.info(f"  {len(standings_rows)} team standings stored")
        else:
            log.info("  no standings data in /lgdata")
    except Exception as e:
        log.warning(f"  standings failed (non-fatal): {e}")

    conn.commit()

    # Now collect ratings — wait for the minimum 30s if other pulls were fast
    elapsed = _time.monotonic() - ratings_start
    remaining = 30 - elapsed
    if remaining > 0:
        log.info("── ratings (waiting for export...)")
        _time.sleep(remaining)
    else:
        log.info("── ratings (collecting...)")

    state = json.loads((league_dir / "config" / "state.json").read_text())
    snapshot_date = game_date or state.get("game_date", "unknown")
    org_id = _cfg.my_team_id
    all_ratings = client.get_ratings(poll_url=ratings_poll_url, skip_initial_wait=elapsed >= 30)
    log.info(f"  {len(all_ratings)} ratings received")
    my_ids = {p["ID"] for p in players if p.get("Team ID") == org_id or p.get("Parent Team ID") == org_id}
    my_ratings    = [r for r in all_ratings if r["ID"] in my_ids]
    other_ratings = [r for r in all_ratings if r["ID"] not in my_ids]
    log.info(f"  storing: {len(my_ratings)} org, {len(other_ratings)} other (snapshot={snapshot_date})")
    _upsert_ratings(conn, my_ratings,    snapshot_date, keep_history=False)
    _upsert_ratings(conn, other_ratings, snapshot_date, keep_history=False)
    _snapshot_ratings_history(conn, all_ratings, snapshot_date)
    # Prune old ratings snapshots — history table handles archival
    conn.execute("DELETE FROM ratings WHERE snapshot_date != ?", (snapshot_date,))

    # Fix intl complex players: API reports level=1 but league_id is negative
    intl_ids = [r["ID"] for r in all_ratings if (r.get("League") or 0) < 0]
    if intl_ids:
        conn.executemany(
            "UPDATE players SET level = 8 WHERE player_id = ? AND level = '1'",
            [(pid,) for pid in intl_ids]
        )
        log.info(f"  reclassified {len(intl_ids)} intl complex players to level=8")

    conn.commit()

    log.info("── league structure")
    structure = _detect_league_structure(conn, year)
    conn.close()
    if structure:
        divisions, leagues, team_abbr, team_names = structure
        settings_path = league_dir / "config" / "league_settings.json"
        s = json.loads(settings_path.read_text()) if settings_path.exists() else {}
        # Only overwrite divisions/leagues if not manually configured
        if not s.get("manual_structure"):
            s["divisions"] = divisions
            s["leagues"] = leagues
        # Always update team names/abbreviations (these come from the API)
        s["team_abbr"] = team_abbr
        s["team_names"] = team_names
        settings_path.write_text(json.dumps(s, indent=2) + "\n")
        log.info(f"  {len(leagues)} leagues, {len(divisions)} divisions, {len(team_abbr)} teams")
    else:
        log.info("  insufficient data — skipped")

    log.info("── league averages")
    _refresh_league_averages(year)
    _refresh_stat_percentiles(year)
    _refresh_dollar_per_war(year)
    _detect_minimum_salary()
    log.info("=== refresh_league complete (year=%s, date=%s) ===", year, game_date)


def _refresh_dollar_per_war(year):
    """Compute market $/WAR from free agent contracts.

    Uses multi-year contracts to players aged 28+ (past arbitration) as the
    true market rate. This is what a team would pay on the open market to
    acquire WAR — the relevant figure for surplus calculations.
    """
    league_dir = get_league_dir()
    conn = _db.get_conn(league_dir)

    from player_utils import league_minimum
    from constants import DEFAULT_MINIMUM_SALARY
    min_sal = league_minimum()
    sal_threshold = round(5_000_000 * min_sal / DEFAULT_MINIMUM_SALARY) if min_sal else 5_000_000
    sal_threshold = max(sal_threshold, min_sal * 3) if min_sal else sal_threshold

    # Free agent contracts: multi-year deals to players past arb (age 28+)
    # These represent the true open-market price for WAR.
    signed_rows = conn.execute("""
        SELECT p.player_id, c.salary_0
        FROM contracts c JOIN players p ON c.player_id = p.player_id
        WHERE p.level = 1 AND c.is_major = 1
          AND c.salary_0 >= ? AND c.years >= 2 AND p.age >= 28
    """, (sal_threshold,)).fetchall()

    # Fallback: if too few FA contracts, use all contracts above threshold
    if len(signed_rows) < 30:
        signed_rows = conn.execute("""
            SELECT p.player_id, c.salary_0
            FROM contracts c JOIN players p ON c.player_id = p.player_id
            WHERE p.level = 1 AND c.is_major = 1
              AND c.salary_0 >= ?
        """, (sal_threshold,)).fetchall()

    if not signed_rows:
        conn.close()
        return

    pids = [r["player_id"] for r in signed_rows]
    total_salary = sum(r["salary_0"] for r in signed_rows)

    placeholders = ",".join("?" * len(pids))
    bat_war = conn.execute(
        f"SELECT SUM(war) FROM (SELECT player_id, SUM(war) as war FROM mlb_batting_stats "
        f"WHERE year=? AND split_id=1 AND player_id IN ({placeholders}) GROUP BY player_id)",
        [year - 1] + pids  # use prior season WAR — current season is incomplete
    ).fetchone()[0] or 0
    pit_war = conn.execute(
        f"SELECT SUM(war) FROM (SELECT player_id, SUM(war) as war FROM mlb_pitching_stats "
        f"WHERE year=? AND split_id=1 AND player_id IN ({placeholders}) GROUP BY player_id)",
        [year - 1] + pids
    ).fetchone()[0] or 0
    total_war = bat_war + pit_war
    conn.close()

    if total_war <= 0:
        return

    dpw = round(total_salary / total_war)
    avg_path = league_dir / "config" / "league_averages.json"
    averages = json.loads(avg_path.read_text())
    averages["dollar_per_war"] = dpw
    averages["dollar_per_war_note"] = (
        f"Market rate: {len(pids)} MLB contracts (salary >= ${sal_threshold:,}), "
        f"total salary ${total_salary:,} / prior-season WAR {total_war:.1f}. "
        f"Recalculated each league refresh."
    )
    avg_path.write_text(json.dumps(averages, indent=2))
    log.info(f"  $/WAR calibrated: ${dpw:,} ({len(pids)} contracts, "
          f"payroll ${total_salary:,} / WAR {total_war:.1f})")


def _avg(rows, field):
    vals = [r[field] for r in rows if isinstance(r.get(field), float)]
    return round(sum(vals) / len(vals), 4) if vals else None


def _write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2))
    log.info(f"  → {path.relative_to(BASE)}")


def _detect_minimum_salary():
    """Detect league minimum salary from the mode of lowest MLB contract salaries."""
    league_dir = get_league_dir()
    conn = _db.get_conn(league_dir)
    rows = conn.execute("""
        SELECT c.salary_0 FROM contracts c
        JOIN players p ON c.player_id = p.player_id
        WHERE p.level = '1' AND c.is_major = 1 AND c.salary_0 > 0
        ORDER BY c.salary_0 LIMIT 200
    """).fetchall()
    conn.close()
    if not rows:
        return
    # Mode of the bottom 200 salaries — the minimum salary cluster dominates
    from collections import Counter
    salary, count = Counter(r[0] for r in rows).most_common(1)[0]
    settings_path = league_dir / "config" / "league_settings.json"
    s = json.loads(settings_path.read_text()) if settings_path.exists() else {}
    s["minimum_salary"] = salary
    settings_path.write_text(json.dumps(s, indent=2) + "\n")
    log.info(f"  minimum salary: ${salary:,} (mode of bottom 200 MLB contracts)")


def _compute_milb_averages(conn, milb_lids, year, league_dir):
    """Compute per-league MiLB batting and pitching averages for stat normalization.

    Stores averages in league_settings.json under each minor_league entry:
        minor_leagues[i].batting_avg = {obp, slg, pa_pool}
        minor_leagues[i].pitching_avg = {era, ip_pool}

    These are used by the evaluation engine to compute level-relative OPS+/ERA-
    for the MiLB stat integration feature.
    """
    settings_path = league_dir / "config" / "league_settings.json"
    if not settings_path.exists():
        return

    s = json.loads(settings_path.read_text())
    minor_leagues = s.get("minor_leagues", [])

    for ml in minor_leagues:
        lid = ml["lid"]

        # Batting: compute average OBP and SLG from players with >= 50 PA
        bat_row = conn.execute("""
            SELECT AVG(sub.obp) as avg_obp, AVG(sub.slg) as avg_slg, COUNT(*) as n
            FROM (
                SELECT (SUM(h)+SUM(bb)+SUM(hbp))*1.0/NULLIF(SUM(pa),0) as obp,
                       (SUM(h)+SUM(d)+2*SUM(t)+3*SUM(hr))*1.0/NULLIF(SUM(ab),0) as slg
                FROM batting_stats
                WHERE league_id=? AND split_id=1 AND year=?
                GROUP BY player_id
                HAVING SUM(pa) >= 50
            ) sub
        """, (lid, year)).fetchone()

        if bat_row and bat_row["avg_obp"]:
            ml["batting_avg"] = {
                "obp": round(bat_row["avg_obp"], 4),
                "slg": round(bat_row["avg_slg"], 4),
                "n": bat_row["n"],
                "year": year,
            }

        # Pitching: compute average ERA from pitchers with >= 30 IP
        pit_row = conn.execute("""
            SELECT AVG(sub.era) as avg_era, COUNT(*) as n
            FROM (
                SELECT SUM(er)*27.0/NULLIF(SUM(outs),0) as era
                FROM pitching_stats
                WHERE league_id=? AND split_id=1 AND year=?
                GROUP BY player_id
                HAVING SUM(ip) >= 30
            ) sub
            WHERE sub.era IS NOT NULL
        """, (lid, year)).fetchone()

        if pit_row and pit_row["avg_era"]:
            ml["pitching_avg"] = {
                "era": round(pit_row["avg_era"], 2),
                "n": pit_row["n"],
                "year": year,
            }

    s["minor_leagues"] = minor_leagues
    settings_path.write_text(json.dumps(s, indent=2) + "\n")
    log.info("  MiLB averages computed for %d leagues", len(minor_leagues))


def _refresh_league_averages(year):
    bat = client.get_team_batting_stats(year=year, split=1)
    pit = client.get_team_pitching_stats(year=year, split=1)
    # Fall back to prior year if current season hasn't started (spring training)
    if not bat or not pit:
        log.info(f"  no team stats for {year}, trying {year - 1}")
        bat = client.get_team_batting_stats(year=year - 1, split=1)
        pit = client.get_team_pitching_stats(year=year - 1, split=1)
    league_dir = get_league_dir()
    avg_path = league_dir / "config" / "league_averages.json"
    existing = json.loads(avg_path.read_text()) if avg_path.exists() else {}
    averages = {
        "year": year,
        "teams_in_sample": len(bat),
        "last_updated": datetime.now(timezone.utc).isoformat(),
        "batting": {f: _avg(bat, f) for f in
            ["avg", "obp", "slg", "ops", "woba", "babip", "iso", "k_pct", "bb_pct"]},
        "pitching": {f: _avg(pit, f) for f in
            ["era", "fip", "x_fip", "k_pct", "bb_pct", "k_bb_pct", "babip", "avg", "obp"]},
    }
    # Preserve dollar_per_war if already calculated — only league refresh recalculates it
    for key in ("dollar_per_war", "dollar_per_war_note"):
        if key in existing:
            averages[key] = existing[key]
    _write_json(avg_path, averages)


def _refresh_stat_percentiles(year):
    """Compute P95 OPS+ and P5 ERA- from qualifying player seasons.

    These calibrate the stat-to-2080 conversion slopes so that the top
    producers in this league map to the correct grade (70-75) rather than
    being compressed by a formula designed for a wider stat range.

    ERA- is used for pitchers rather than FIP- because OOTP WAR is RA9-based
    and the best pitchers suppress runs via contact management, not strikeouts.
    FIP is uncorrelated with WAR in this league.

    Written into league_averages.json as:
        batting.ops_plus_p95 — P95 OPS+ among PA>=300 hitters
        pitching.era_minus_p5 — P5 ERA- among qualifying pitchers (lower=better)
    """
    league_dir = get_league_dir()
    avg_path = league_dir / "config" / "league_averages.json"
    averages = json.loads(avg_path.read_text()) if avg_path.exists() else {}

    lg_obp = averages.get("batting", {}).get("obp") or 0
    lg_slg = averages.get("batting", {}).get("slg") or 0
    lg_era = averages.get("pitching", {}).get("era") or 0

    if lg_obp <= 0 or lg_slg <= 0 or lg_era <= 0:
        return

    conn = _db.get_conn(league_dir)

    # P95 OPS+ from qualifying hitters (PA >= 300)
    # Use prior year if available for full-season samples; fall back to current year
    for yr in (year - 1, year):
        bat_rows = conn.execute("""
            SELECT obp, slg FROM mlb_batting_stats
            WHERE year = ? AND split_id = 1 AND pa >= 300
        """, (yr,)).fetchall()
        if len(bat_rows) >= 20:
            break

    if bat_rows:
        ops_plus_vals = sorted(
            100.0 * (r["obp"] / lg_obp + r["slg"] / lg_slg - 1.0)
            for r in bat_rows
        )
        p95_idx = int(0.95 * len(ops_plus_vals))
        averages.setdefault("batting", {})["ops_plus_p95"] = round(ops_plus_vals[p95_idx], 1)

    # P5 ERA- from qualifying pitchers (outs >= 300)
    for yr in (year - 1, year):
        pit_rows = conn.execute("""
            SELECT era FROM mlb_pitching_stats
            WHERE year = ? AND split_id = 1 AND outs >= 300 AND era IS NOT NULL
        """, (yr,)).fetchall()
        if len(pit_rows) >= 10:
            break

    if pit_rows:
        era_minus_vals = sorted(r["era"] / lg_era * 100.0 for r in pit_rows)
        p5_idx = int(0.05 * len(era_minus_vals))
        averages.setdefault("pitching", {})["era_minus_p5"] = round(era_minus_vals[p5_idx], 1)

    # GB% distribution for contextualizing pitcher groundball tendencies
    for yr in (year - 1, year):
        gb_rows = conn.execute("""
            SELECT 100.0 * gb / (gb + fb) as gb_pct
            FROM mlb_pitching_stats
            WHERE year = ? AND split_id = 1 AND (gb + fb) > 100
        """, (yr,)).fetchall()
        if len(gb_rows) >= 20:
            break
    if gb_rows:
        import statistics
        gb_vals = [r[0] for r in gb_rows]
        averages.setdefault("pitching", {})["gb_pct_mean"] = round(statistics.mean(gb_vals), 1)
        averages.setdefault("pitching", {})["gb_pct_stdev"] = round(statistics.stdev(gb_vals), 1)

    # GB% regression: actual_gb = intercept + slope * gb_rating
    # Used by percentile engine to compute expected GB% from rating
    for yr in (year - 1, year):
        gb_reg_rows = conn.execute("""
            SELECT r.gb as rating, 100.0 * ps.gb / (ps.gb + ps.fb) as actual
            FROM mlb_pitching_stats ps
            JOIN latest_ratings r ON ps.player_id = r.player_id
            WHERE ps.year = ? AND ps.split_id = 1 AND (ps.gb + ps.fb) > 100
              AND r.gb IS NOT NULL AND r.gb > 0
        """, (yr,)).fetchall()
        if len(gb_reg_rows) >= 30:
            break
    if gb_reg_rows and len(gb_reg_rows) >= 30:
        ratings = [r[0] for r in gb_reg_rows]
        actuals = [r[1] for r in gb_reg_rows]
        mean_x = statistics.mean(ratings)
        mean_y = statistics.mean(actuals)
        var_x = statistics.variance(ratings)
        if var_x > 0:
            cov = sum((r[0] - mean_x) * (r[1] - mean_y) for r in gb_reg_rows) / (len(gb_reg_rows) - 1)
            slope = cov / var_x
            intercept = mean_y - slope * mean_x
            averages.setdefault("pitching", {})["gb_regression"] = {
                "intercept": round(intercept, 2),
                "slope": round(slope, 4),
                "n": len(gb_reg_rows),
            }

    conn.close()
    avg_path.write_text(json.dumps(averages, indent=2))
    bat_p95 = averages.get("batting", {}).get("ops_plus_p95")
    pit_p5 = averages.get("pitching", {}).get("era_minus_p5")
    log.info(f"  stat percentiles: OPS+ P95={bat_p95}, ERA- P5={pit_p5}")


def update_state(game_date, year):
    league_dir = get_league_dir()
    state_path = league_dir / "config" / "state.json"
    existing = json.loads(state_path.read_text()) if state_path.exists() else {}
    state = {
        "game_date": game_date,
        "last_updated": datetime.now(timezone.utc).isoformat(),
        "year": int(year),
        "my_team_id": existing.get("my_team_id", _cfg.my_team_id),
    }
    _write_json(state_path, state)


def _run_calibrate():
    """Run calibrate.py to derive league-specific model weights."""
    import subprocess
    log.info("── calibrate")
    result = subprocess.run(
        [sys.executable, str(BASE / "scripts" / "calibrate.py")],
        capture_output=True, text=True
    )
    if result.stdout:
        for line in result.stdout.strip().splitlines():
            log.info(f"  cal: {line}")
    if result.returncode != 0:
        msg = result.stderr.strip().splitlines()
        last = msg[-1] if msg else "unknown error"
        log.warning("calibrate failed (using defaults): %s", last)
    else:
        log.info("  calibrate complete")


def _run_evaluation_engine():
    """Run evaluation_engine.py to compute composite scores for all players."""
    import time as _time
    log.info("── evaluation engine")
    try:
        league_dir = get_league_dir()
        _db.init_schema(league_dir)  # ensure new columns exist before engine runs
        t0 = _time.monotonic()
        from evaluation_engine import run as eval_run
        eval_run(league_dir=league_dir)
        elapsed = _time.monotonic() - t0
        log.info(f"  evaluation engine complete ({elapsed:.1f}s)")
    except Exception as e:
        log.warning("evaluation engine failed (scores will be NULL): %s", e)


def _run_fv_calc():
    """Run fv_calc.py as a subprocess to compute league-wide FV + surplus."""
    import subprocess
    log.info("── fv_calc")
    result = subprocess.run(
        [sys.executable, str(BASE / "scripts" / "fv_calc.py")],
        capture_output=True, text=True
    )
    if result.stdout:
        for line in result.stdout.strip().splitlines():
            log.info(f"  fv: {line}")
    if result.returncode != 0:
        msg = result.stderr.strip().splitlines()
        last = msg[-1] if msg else "unknown error"
        log.error("fv_calc failed: %s", last)
        sys.exit(1)
    else:
        log.info("  fv_calc complete")


if __name__ == "__main__":
    args = sys.argv[1:]
    default_year = _cfg.year
    if args and args[0] == "state":
        game_date = args[1] if len(args) > 1 else "unknown"
        year      = int(args[2]) if len(args) > 2 else default_year
        update_state(game_date, year)
    else:
        if args and args[0] == "--league":
            args = args[1:]
        skip_fv = False
        if args and args[0] == "--no-fv":
            skip_fv = True
            args = args[1:]
        year = int(args[0]) if args else default_year
        game_date = client.get_date()
        # Derive year from API game date — state.json may be stale or uninitialized
        if game_date and len(game_date) >= 4:
            year = int(game_date[:4])
        log.info("=== Full pipeline: year=%s, game_date=%s ===", year, game_date)
        refresh_league(year, game_date=game_date)
        update_state(game_date, year)
        if not skip_fv:
            _run_evaluation_engine()
            _run_calibrate()
            _run_fv_calc()
            log.info("=== Pipeline complete ===")
        else:
            log.info("=== Pipeline complete (--no-fv: skipped eval/calibrate/fv) ===")
