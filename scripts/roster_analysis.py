#!/usr/bin/env python3
"""
roster_analysis.py — MLB roster scaffold for the active league's team.
Produces tmp/roster_scaffold_<game_date>.md per docs/roster_analysis_guide.md.
Usage: python3 scripts/roster_analysis.py
"""

import json, os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from statsplusplus.config.league_context import get_league_dir, get_active_league_slug
from statsplusplus.config.league_config import LeagueConfig
from statsplusplus.config.ratings import norm as _norm_pkg, norm_floor as _norm_floor_pkg
from statsplusplus.data.db import get_connection
from statsplusplus.utils.formatting import height_str, fmt_table
from statsplusplus.utils.positions import PITCH_FIELDS, PITCH_NAMES

league_dir = get_league_dir(get_active_league_slug())
_cfg = LeagueConfig(base_dir=league_dir)
_scale = _cfg.ratings_scale

def norm(val):
    return _norm_pkg(val, _scale)

def norm_floor(val, floor=20):
    return _norm_floor_pkg(val, _scale, floor)

def _get_conn():
    return get_connection(league_dir)

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def delta_str(val, avg, higher_is_better=True):
    """Return a +/- delta string vs league average."""
    d = val - avg
    if abs(d) < 0.001:
        return "≈avg"
    sign = "+" if d > 0 else ""
    better = (d > 0) == higher_is_better
    marker = "▲" if better else "▼"
    return f"{sign}{d:.3f} {marker}"

def contract_info(c):
    """Return (cur_sal, aav, yrs_rem, total_rem, options_str) from a contract record."""
    years = c["years"]
    cur   = c["current_year"]
    sals  = [c.get(f"salary{i}", 0) for i in range(years)]
    cur_sal     = sals[cur] if cur < len(sals) else 0
    total_val   = sum(sals)
    aav         = total_val / years if years else 0
    yrs_rem     = years - cur
    total_rem   = sum(sals[cur:])
    nt          = c.get("no_trade", 0)

    # Options
    opt_parts = []
    game_year = c.get("season_year", 0) + cur
    if c.get("last_year_team_option"):
        yr = c.get("season_year", 0) + years - 1
        bo = c.get("last_year_option_buyout", 0)
        sal = sals[years - 1] if years - 1 < len(sals) else 0
        opt_parts.append(f"TEAM {yr} ${sal/1e6:.1f}M (bo ${bo/1e6:.1f}M)")
    if c.get("last_year_player_option"):
        yr = c.get("season_year", 0) + years - 1
        sal = sals[years - 1] if years - 1 < len(sals) else 0
        opt_parts.append(f"PLAYER {yr} ${sal/1e6:.1f}M")
    if c.get("last_year_vesting_option"):
        yr = c.get("season_year", 0) + years - 1
        sal = sals[years - 1] if years - 1 < len(sals) else 0
        opt_parts.append(f"VESTING {yr} ${sal/1e6:.1f}M")
    options_str = "; ".join(opt_parts) if opt_parts else "—"

    return cur_sal, aav, yrs_rem, total_rem, nt, options_str

def contract_flag(player, aav, yrs_rem, total_rem):
    ovr = player["Ovr"]
    age = player["Age"]
    if yrs_rem <= 1 and ovr >= 50:
        return "EXTENSION"
    if (age >= 35 and yrs_rem >= 2) or (age >= 33 and yrs_rem >= 3):
        if ovr < 55:
            return "CONCERN"
        return "WATCH"
    if total_rem >= 100_000_000 and player["Pot"] <= ovr + 2:
        if ovr < 60:
            return "WATCH"
    return "OK"

# ---------------------------------------------------------------------------
# Grade tables
# ---------------------------------------------------------------------------

def batter_grade_table(p, bucket):
    hit  = norm(p.get("Cntct", 50))
    pow_ = norm(p.get("Pow", 50))
    eye  = norm(p.get("Eye", 50))
    ks   = norm(p.get("Ks", 50))
    gap  = norm(p.get("Gap", 50))
    spd  = norm(p.get("Speed", 50))

    pos_field = {"C":"C","SS":"SS","2B":"2B","CF":"CF","COF":"LF","3B":"3B","1B":"1B","DH":"1B"}.get(bucket, "1B")
    fld = norm(p.get(pos_field, 50))
    arm_field = "CArm" if bucket == "C" else ("OFA" if bucket in ("CF","COF") else "IFA")
    arm = norm(p.get(arm_field, 50))

    headers = ["Hit","Power","Eye","K-Rate","Gap","Speed","Fielding","Arm"]
    values  = [str(hit), str(pow_), str(eye), str(ks), str(gap), str(spd), str(fld), str(arm)]

    if bucket == "C":
        cblk = norm(p.get("CBlk", 1))
        cfrm = norm(p.get("CFrm", 1))
        headers += ["Blocking","Framing"]
        values  += [str(cblk), str(cfrm)]

    # Grade callout
    all_grades = dict(zip(headers, [int(v) for v in values]))
    highs = [f"{k} ({v})" for k, v in all_grades.items() if v >= 60]
    lows  = [f"{k} ({v})" for k, v in all_grades.items() if v <= 40]
    callout = []
    if highs: callout.append(f"PLUS+ tools: {', '.join(highs)}")
    if lows:  callout.append(f"BELOW-AVG tools: {', '.join(lows)}")

    # Defensive detail — Range/Error for notable values
    is_if = bucket in ("C", "SS", "2B", "3B", "1B")
    rng = norm_floor(p.get("IFR" if is_if else "OFR", 0))
    err = norm_floor(p.get("IFE" if is_if else "OFE", 0))
    def_parts = []
    if rng >= 60 or rng <= 35: def_parts.append(f"Range {rng}")
    if err >= 60 or err <= 35: def_parts.append(f"Error {err}")
    if bucket in ("SS", "2B"):
        tdp = norm_floor(p.get("TDP", 0))
        if tdp >= 60 or tdp <= 35: def_parts.append(f"TDP {tdp}")
    if def_parts:
        callout.append(f"Defense detail: {', '.join(def_parts)}")

    # L/R split flag
    split_notes = []
    for tool, lf, rf in [("Contact", "Cntct_L", "Cntct_R"), ("Power", "Pow_L", "Pow_R")]:
        lv, rv = p.get(lf, 0) or 0, p.get(rf, 0) or 0
        if lv and rv and abs(lv - rv) >= 20:
            weak = "vs LHP" if lv < rv else "vs RHP"
            split_notes.append(f"{tool} {norm(lv)}/{norm(rv)} (L/R) — weaker {weak}")
    if split_notes:
        callout.append(f"Split flag: {'; '.join(split_notes)}")

    return fmt_table(headers, values), callout

def pitcher_grade_table(p):
    viable = [(f, p.get(f, 0)) for f in PITCH_FIELDS if p.get(f, 0) >= 25]
    viable.sort(key=lambda x: x[1], reverse=True)
    top4 = viable[:4]

    ctrl = p.get("Ctrl") or ((p.get("Ctrl_R") or 1) + (p.get("Ctrl_L") or 1)) / 2
    stf  = norm(p.get("Stf", 1))
    mov  = norm(p.get("Mov", 1))
    ctl  = norm(ctrl)
    vel  = p.get("Vel", "?")

    pitch_cols = [(PITCH_NAMES[f], str(norm(v))) for f, v in top4]
    headers = [n for n, _ in pitch_cols] + ["Velocity","Stuff","Movement","Control"]
    values  = [g for _, g in pitch_cols] + [vel, str(stf), str(mov), str(ctl)]

    all_grades = {n: int(g) for n, g in pitch_cols}
    all_grades.update({"Stuff": stf, "Movement": mov, "Control": ctl})
    highs = [f"{k} ({v})" for k, v in all_grades.items() if v >= 60]
    lows  = [f"{k} ({v})" for k, v in all_grades.items() if v <= 40]
    callout = []
    if highs: callout.append(f"PLUS+ grades: {', '.join(highs)}")
    if lows:  callout.append(f"BELOW-AVG grades: {', '.join(lows)}")

    stm = p.get("Stm", 0)
    if stm >= 50:
        stm_note = "Stm: FULL STARTER"
    elif stm >= 40:
        stm_note = "Stm: SHORT STARTER (5-inning profile — do not project as reliever)"
    else:
        stm_note = "Stm: RELIEVER (lacks stamina to start)"
    callout.append(stm_note)

    # GB% context
    gb = p.get("GB")
    if gb and gb > 0:
        gb_label = "extreme groundball" if gb >= 65 else "groundball" if gb >= 55 else \
                   "neutral" if gb >= 45 else "flyball" if gb >= 35 else "extreme flyball"
        callout.append(f"GB%: {gb}% ({gb_label})")

    # L/R split flag
    split_notes = []
    for tool, lf, rf in [("Stuff", "Stf_L", "Stf_R"), ("Movement", "Mov_L", "Mov_R")]:
        lv, rv = p.get(lf, 0) or 0, p.get(rf, 0) or 0
        if lv and rv and abs(lv - rv) >= 20:
            weak = "vs LHB" if lv < rv else "vs RHB"
            split_notes.append(f"{tool} {norm(lv)}/{norm(rv)} (L/R) — weaker {weak}")
    if split_notes:
        callout.append(f"Split flag: {'; '.join(split_notes)}")

    return fmt_table(headers, values), callout

# ---------------------------------------------------------------------------
# Stat lines
# ---------------------------------------------------------------------------

def batter_stat_line(pid, bat_stats, lg):
    s = next((r for r in bat_stats if r["player_id"] == pid), None)
    if not s or s.get("ab", 0) < 1:
        return "2033 Stats: no data"
    ab  = s["ab"]; h = s["h"]; hr = s["hr"]; rbi = s["rbi"]
    bb  = s["bb"]; k = s["k"]; pa = s.get("pa", ab + bb)
    avg = h / ab if ab else 0
    obp = (h + bb) / pa if pa else 0
    slg = (h + s.get("d",0) + 2*s.get("t",0) + 3*hr) / ab if ab else 0
    war = s.get("war", 0)
    g   = s.get("g", 0)

    d_avg = delta_str(avg, lg["batting"]["avg"])
    d_obp = delta_str(obp, lg["batting"]["obp"])
    d_slg = delta_str(slg, lg["batting"]["slg"])

    return (f"2033 Stats ({g}G / {ab}AB): "
            f".{int(avg*1000):03d} [{d_avg}] / "
            f".{int(obp*1000):03d} OBP [{d_obp}] / "
            f".{int(slg*1000):03d} SLG [{d_slg}] | "
            f"{hr} HR / {rbi} RBI / {bb} BB / {k} K | WAR {war:.2f}")

def pitcher_stat_line(pid, pit_stats, lg):
    s = next((r for r in pit_stats if r["player_id"] == pid), None)
    if not s or s.get("ip", 0) < 1:
        return "2033 Stats: no data"
    ip  = s["ip"]; era = s.get("era") or 0.0; k = s["k"]; bb = s["bb"]
    ha  = s.get("ha", 0); gs = s.get("gs", 0); g = s.get("g", 0)
    war = s.get("war", 0)
    whip = (ha + bb) / ip if ip else 0
    bf  = s.get("bf", ip * 4)
    k_pct  = (k / bf * 100) if bf else 0
    bb_pct = (bb / bf * 100) if bf else 0

    d_era  = delta_str(era,   lg["pitching"]["era"],   higher_is_better=False)
    d_k    = delta_str(k_pct, lg["pitching"]["k_pct"], higher_is_better=True)
    d_bb   = delta_str(bb_pct,lg["pitching"]["bb_pct"],higher_is_better=False)

    return (f"2033 Stats ({g}G/{gs}GS / {ip}IP): "
            f"ERA {era:.2f} [{d_era}] | "
            f"K% {k_pct:.1f} [{d_k}] | BB% {bb_pct:.1f} [{d_bb}] | "
            f"WHIP {whip:.2f} | WAR {war:.2f}")

# ---------------------------------------------------------------------------
# Positional bucket (MLB context — no age-gating, use current grades)
# ---------------------------------------------------------------------------

def mlb_bucket(p, role_str):
    if role_str in ("starter", "reliever", "closer") or p.get("Pos") == "P":
        return "P"
    # DH: pos field = 10 in roster, but ratings Pos is a string
    if p.get("Pos") == "DH":
        return "DH"
    # Use current positional grades
    if p.get("C", 0) >= 45:   return "C"
    if p.get("SS", 0) >= 50:  return "SS"
    if p.get("2B", 0) >= 50 or p.get("SS", 0) >= 50: return "2B"
    if p.get("CF", 0) >= 55:  return "CF"
    if p.get("LF", 0) >= 45 or p.get("RF", 0) >= 45: return "COF"
    if p.get("3B", 0) >= 45:  return "3B"
    if p.get("1B", 0) >= 45:  return "1B"
    return "DH"

# ---------------------------------------------------------------------------
# Notes
# ---------------------------------------------------------------------------

NOTES_PATH = os.path.join(str(league_dir), "history", "roster_notes.json")

def load_notes():
    if not os.path.exists(NOTES_PATH):
        return {}
    with open(NOTES_PATH) as f:
        data = json.load(f)
    # Keyed by string player_id (same pattern as prospects.json)
    return {int(k): v for k, v in data.items()}

# ---------------------------------------------------------------------------
# Player card
# ---------------------------------------------------------------------------

def player_card(p, role_str, bucket, bat_stats, pit_stats, contracts_by_pid, notes, lg, game_year=None):
    pid  = p["ID"]
    name = p["Name"]
    age  = p["Age"]
    ovr  = p["Ovr"]
    pot  = p["Pot"]
    ht   = height_str(p.get("Height", 180))
    bats = p.get("Bats", "?")
    throws = p.get("Throws", "?")

    c = contracts_by_pid.get(pid)
    if c:
        cur_sal, aav, yrs_rem, total_rem, nt, options_str = contract_info(c)
        nt_str = " [NT]" if nt else ""
        contract_line = (f"{yrs_rem}yr ${aav/1e6:.1f}M AAV | "
                         f"${cur_sal/1e6:.1f}M this yr | "
                         f"${total_rem/1e6:.1f}M remaining{nt_str}")
    else:
        aav = yrs_rem = total_rem = 0
        contract_line = "no contract data"

    lines = [
        f"### {name} | {bucket} | Age {age} | Ovr {ovr} | Pot {pot}",
        f"{ht} | {bats}/{throws} | player_id: {pid}",
        f"Contract: {contract_line}",
        "",
    ]

    is_pitcher = role_str in ("starter", "reliever", "closer")
    if is_pitcher:
        table, callout = pitcher_grade_table(p)
        lines.append(table)
        lines.append(pitcher_stat_line(pid, pit_stats, lg))
    else:
        table, callout = batter_grade_table(p, bucket)
        lines.append(table)
        lines.append(batter_stat_line(pid, bat_stats, lg))

    if callout:
        lines.append("Grade flags: " + " | ".join(callout))

    note = notes.get(pid)
    if note and note.get("summary"):
        reasons = []
        ovr_delta = ovr - note.get("last_ovr", ovr)
        if abs(ovr_delta) >= 5:
            reasons.append(f"Ovr moved {'+' if ovr_delta>0 else ''}{ovr_delta}")
        if game_year and note.get("last_year") and game_year != note["last_year"]:
            reasons.append(f"new season ({note['last_year']} → {game_year})")
        if reasons:
            signal = f"[EXISTING SUMMARY — {', '.join(reasons)}, consider rewrite]"
        else:
            signal = "[EXISTING SUMMARY]"
        lines.append(f"\n{signal}\n{note['summary']}")
    else:
        lines.append("\n[NEW PLAYER — write assessment per roster_analysis_guide.md]")

    return "\n".join(lines)

# ---------------------------------------------------------------------------
# Contract table
# ---------------------------------------------------------------------------

def contract_table(players_with_contracts):
    rows = []
    for p, role_str, c in players_with_contracts:
        cur_sal, aav, yrs_rem, total_rem, nt, options_str = contract_info(c)
        if yrs_rem < 1:
            continue
        flag = contract_flag(p, aav, yrs_rem, total_rem)
        nt_str = "Y" if nt else "N"
        rows.append((p["Name"], mlb_bucket(p, role_str), p["Age"], p["Ovr"], p["Pot"],
                     aav, yrs_rem, total_rem, flag, nt_str, options_str))

    rows.sort(key=lambda r: -r[7])  # sort by total remaining desc

    headers = ["Player","Pos","Age","Ovr","Pot","AAV","Yrs Rem","Total Rem","Flag","NT","Options"]
    lines = ["| " + " | ".join(headers) + " |",
             "| " + " | ".join(["---"] * len(headers)) + " |"]
    for r in rows:
        name, pos, age, ovr, pot, aav, yrs, total, flag, nt, opts = r
        lines.append(f"| {name} | {pos} | {age} | {ovr} | {pot} | "
                     f"${aav/1e6:.1f}M | {yrs} | ${total/1e6:.1f}M | {flag} | {nt} | {opts} |")
    return "\n".join(lines)

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    ld = str(league_dir)
    game_date = _cfg.game_date
    year      = _cfg.year
    my_tid    = _cfg.my_team_id

    lg        = json.load(open(os.path.join(ld, "config", "league_averages.json")))
    role_map  = {str(k): v for k, v in _cfg.role_map.items()}

    conn = _get_conn()

    # MLB roster: level=1, my org, exclude intl complex (league_id < 0)
    roster_rows = conn.execute("""
        SELECT p.player_id AS ID, p.role, p.name, p.age, r.*
        FROM players p
        JOIN ratings r ON r.player_id = p.player_id
        WHERE (p.team_id = ? OR p.parent_team_id = ?)
          AND p.level = 1
          AND (r.league_id IS NULL OR r.league_id >= 0)
          AND r.snapshot_date = (
              SELECT MAX(r2.snapshot_date) FROM ratings r2 WHERE r2.player_id = p.player_id
          )
    """, (my_tid, my_tid)).fetchall()

    mlb_ids = {r["ID"] for r in roster_rows}

    # Batting stats
    bat_rows = conn.execute("""
        SELECT * FROM mlb_batting_stats
        WHERE player_id IN ({}) AND year=? AND split_id=1
    """.format(",".join("?" * len(mlb_ids))),
        list(mlb_ids) + [year]
    ).fetchall() if mlb_ids else []

    # Pitching stats
    pit_rows = conn.execute("""
        SELECT * FROM mlb_pitching_stats
        WHERE player_id IN ({}) AND year=? AND split_id=1
    """.format(",".join("?" * len(mlb_ids))),
        list(mlb_ids) + [year]
    ).fetchall() if mlb_ids else []

    # Contracts
    contract_rows = conn.execute("""
        SELECT * FROM contracts
        WHERE player_id IN ({}) AND is_major=1
    """.format(",".join("?" * len(mlb_ids))),
        list(mlb_ids)
    ).fetchall() if mlb_ids else []

    conn.close()

    # Convert to dicts matching the format the rest of the script expects
    def ratings_dict(r):
        d = dict(r)
        # Map DB column names to ratings field names used throughout the script
        d["ID"]     = d.pop("player_id", d.get("ID"))
        d["Name"]   = d.get("name", "")
        d["Age"]    = d.get("age", 0)
        # Leagues that don't surface OVR/POT (e.g. PPL) store NULL. Fall back to
        # the app's own composite/ceiling scores so downstream logic (contract
        # flags, sorting, ceiling gaps) has numbers to work with. Note: dict.get
        # returns None (not the default) when the key exists with a NULL value.
        d["Ovr"]    = d.get("ovr") or d.get("composite_score") or 0
        d["Pot"]    = d.get("pot") or d.get("ceiling_score") or d["Ovr"]
        d["Cntct"]  = d.get("cntct", 0);  d["Gap"]   = d.get("gap", 0)
        d["Pow"]    = d.get("pow", 0);    d["Eye"]   = d.get("eye", 0)
        d["Ks"]     = d.get("ks", 0);     d["Speed"] = d.get("speed", 0)
        d["Steal"]  = d.get("steal", 0)
        d["Stf"]    = d.get("stf", 0);    d["Mov"]   = d.get("mov", 0)
        d["Ctrl_R"] = d.get("ctrl_r", 0); d["Ctrl_L"]= d.get("ctrl_l", 0)
        d["Stm"]    = d.get("stm", 0);    d["Vel"]   = d.get("vel", "")
        d["OFA"]    = d.get("ofa", 0);    d["IFA"]   = d.get("ifa", 0)
        d["CArm"]   = d.get("c_arm", 0);  d["CBlk"]  = d.get("c_blk", 0)
        d["CFrm"]   = d.get("c_frm", 0)
        d["IFR"]    = d.get("ifr", 0);    d["OFR"]   = d.get("ofr", 0)
        d["IFE"]    = d.get("ife", 0);    d["OFE"]   = d.get("ofe", 0)
        d["TDP"]    = d.get("tdp", 0);    d["GB"]    = d.get("gb", 0)
        d["Cntct_L"]= d.get("cntct_l", 0); d["Cntct_R"]= d.get("cntct_r", 0)
        d["Pow_L"]  = d.get("pow_l", 0);  d["Pow_R"] = d.get("pow_r", 0)
        d["Stf_L"]  = d.get("stf_l", 0);  d["Stf_R"] = d.get("stf_r", 0)
        d["Mov_L"]  = d.get("mov_l", 0);  d["Mov_R"] = d.get("mov_r", 0)
        d["C"]      = d.get("c", 0);      d["SS"]    = d.get("ss", 0)
        d["2B"]     = d.get("second_b", 0); d["3B"]  = d.get("third_b", 0)
        d["1B"]     = d.get("first_b", 0);  d["LF"]  = d.get("lf", 0)
        d["CF"]     = d.get("cf", 0);     d["RF"]    = d.get("rf", 0)
        d["WrkEthic"] = d.get("wrk_ethic", "N")
        d["Height"]   = d.get("height", 180)
        d["Bats"]     = d.get("bats", "?")
        d["Throws"]   = d.get("throws", "?")
        for f in PITCH_FIELDS:
            db_key = f.lower().replace("circhg", "cir_chg").replace("kncrv", "kncrv").replace("knbl", "knbl")
            d[f] = d.get(db_key, 0) or d.get(f, 0)
        return d

    def contract_dict(r):
        d = dict(r)
        # Map salary_0..14 → salary0..14 for contract_info()
        for i in range(15):
            d[f"salary{i}"] = d.get(f"salary_{i}", 0)
        d["current_year"] = d.get("current_year", 0)
        d["years"]        = d.get("years", 0)
        return d

    bat_stats      = [dict(r) for r in bat_rows]
    pit_stats      = [dict(r) for r in pit_rows]
    ratings_by_id  = {r["ID"]: ratings_dict(r) for r in roster_rows}
    contracts_by_pid = {contract_dict(c)["player_id"]: contract_dict(c)
                        for c in contract_rows}
    notes = load_notes()

    # Build player list with role and bucket
    players = []
    for ros in roster_rows:
        ros = dict(ros)
        pid = ros["ID"]
        p   = ratings_by_id.get(pid)
        if not p:
            continue
        role_str = role_map.get(str(ros.get("role") or 0), "position_player")
        bucket   = mlb_bucket(p, role_str)
        players.append((p, role_str, bucket))

    # Separate and sort
    pos_order = ["C","1B","2B","SS","3B","CF","COF","DH"]
    hitters   = [(p, r, b) for p, r, b in players if b not in ("P",)]
    pitchers  = [(p, r, b) for p, r, b in players if b == "P"]
    starters  = [(p, r, b) for p, r, b in pitchers if r == "starter"]
    relievers = [(p, r, b) for p, r, b in pitchers if r in ("reliever", "closer")]

    hitters.sort(key=lambda x: (pos_order.index(x[2]) if x[2] in pos_order else 99, -(x[0].get("Ovr") or 0)))
    starters.sort(key=lambda x: -(x[0].get("Ovr") or 0))
    relievers.sort(key=lambda x: (0 if x[1] == "closer" else 1, -(x[0].get("Ovr") or 0)))

    out = [
        f"# {_cfg.team_name(_cfg.my_team_id)} — Roster Scaffold",
        f"## Game Date: {game_date}",
        "## Agent: complete prose per docs/roster_analysis_guide.md. Do NOT print this scaffold to terminal.",
        "",
        "---",
        "",
        "# Section 1 — Position Players",
        "",
    ]

    for p, role_str, bucket in hitters:
        out.append(player_card(p, role_str, bucket, bat_stats, pit_stats,
                               contracts_by_pid, notes, lg, year))
        out.append("\n---\n")

    out.append("# Section 2 — Starting Rotation\n")
    for p, role_str, bucket in starters:
        out.append(player_card(p, role_str, bucket, bat_stats, pit_stats,
                               contracts_by_pid, notes, lg, year))
        out.append("\n---\n")

    out.append("# Section 3 — Bullpen\n")
    for p, role_str, bucket in relievers:
        out.append(player_card(p, role_str, bucket, bat_stats, pit_stats,
                               contracts_by_pid, notes, lg, year))
        out.append("\n---\n")

    out.append("# Section 4 — Contract Health\n")
    all_with_contracts = [(p, r, contracts_by_pid[p["ID"]])
                          for p, r, b in players if p["ID"] in contracts_by_pid]
    out.append(contract_table(all_with_contracts))

    out.append("\n\n# Section 5 — Roster Construction Assessment\n")
    out.append("[Agent writes this section based on the data above.]\n")

    scaffold = "\n".join(out)
    out_path = os.path.join(ld, "tmp", f"roster_scaffold_{game_date}.md")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w") as f:
        f.write(scaffold)

    # Clean up scaffold files older than 30 days
    import time
    tmp_dir = os.path.join(ld, "tmp")
    cutoff = time.time() - 30 * 86400
    for fname in os.listdir(tmp_dir):
        fpath = os.path.join(tmp_dir, fname)
        if os.path.isfile(fpath) and os.path.getmtime(fpath) < cutoff:
            os.remove(fpath)

    print(f"Scaffold written to {out_path}")

if __name__ == "__main__":
    main()
