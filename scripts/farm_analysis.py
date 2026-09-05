#!/usr/bin/env python3
"""
farm_analysis.py — Farm system analysis for the active league's team.
Produces a ranked prospect report per docs/farm_analysis_guide.md.
Usage: python3 farm_analysis.py [--date YYYY-MM-DD]
"""

import json, math, os, sys
from datetime import date

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from statsplusplus.config.league_context import get_league_dir, get_active_league_slug
from statsplusplus.config.league_config import LeagueConfig
from statsplusplus.config.ratings import norm as _norm_pkg, norm_floor as _norm_floor_pkg
from statsplusplus.utils.formatting import height_str, fmt_table
from statsplusplus.utils.positions import assign_bucket, PITCH_FIELDS, PITCH_NAMES, LEVEL_NORM_AGE
from statsplusplus.evaluation.fv import dev_weight
from statsplusplus.evaluation.fv import calc_fv_from_dict as calc_fv

league_dir = get_league_dir(get_active_league_slug())
_cfg = LeagueConfig(base_dir=league_dir)
_scale = _cfg.ratings_scale

def norm(val):
    return _norm_pkg(val, _scale)

def norm_floor(val, floor=20):
    return _norm_floor_pkg(val, _scale, floor)

ORG_ID = _cfg.my_team_id

def _league_dir():
    return str(league_dir)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

FARM_LEVELS = {
    "aaa":     {"label": "AAA",     "norm_age": LEVEL_NORM_AGE["aaa"]},
    "aa":      {"label": "AA",      "norm_age": LEVEL_NORM_AGE["aa"]},
    "a":       {"label": "A",       "norm_age": LEVEL_NORM_AGE["a"]},
    "a-short": {"label": "A-Short", "norm_age": LEVEL_NORM_AGE["a-short"]},
    "usl":     {"label": "USL",     "norm_age": LEVEL_NORM_AGE["usl"]},
    "dsl":     {"label": "DSL",     "norm_age": LEVEL_NORM_AGE["dsl"]},
    "intl":    {"label": "Intl",    "norm_age": LEVEL_NORM_AGE["intl"]},
}

FARM_LEVEL_INT = {
    "aaa": 2, "aa": 3, "a": 4, "a-short": 5, "usl": 6, "dsl": 6, "intl": 8,
}

def fielding_grade(p, bucket):
    age = p["Age"]
    use_pot = age <= 23
    field_map = {
        "C": ("C", "PotC"), "SS": ("SS", "PotSS"), "2B": ("2B", "Pot2B"),
        "CF": ("CF", "PotCF"), "COF": ("LF", "PotLF"), "3B": ("3B", "Pot3B"),
        "1B": ("1B", "Pot1B"),
    }
    if bucket not in field_map:
        return 50, 50
    cur_f, pot_f = field_map[bucket]
    cur = norm(p.get(cur_f, 50))
    fut = norm(p.get(pot_f, 50)) if use_pot else cur
    return cur, fut

def arm_grade(p, bucket):
    if bucket == "C":   return norm(p.get("CArm", 50))
    if bucket in ("COF", "CF"): return norm(p.get("OFA", 50))
    return norm(p.get("IFA", 50))

def top_pitches(p, n=4):
    # Include pitches viable by present grade (≥25) OR by projection (Pot ≥ 45)
    seen = set()
    viable = []
    for f in PITCH_FIELDS:
        if p.get(f, 0) >= 25 or p.get("Pot" + f, 0) >= 45:
            viable.append((f, p.get(f, 0)))
            seen.add(f)
    viable.sort(key=lambda x: x[1], reverse=True)
    return viable[:n]

# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def load_level(level_key, game_date=None):
    from statsplusplus.data.db import get_connection
    conn = get_connection(league_dir)
    level_int = FARM_LEVEL_INT[level_key]

    # Get ratings for this org's players at this level
    ratings = [dict(r) for r in conn.execute("""
        SELECT r.player_id AS ID, p.name AS Name, p.age AS Age,
               r.ovr AS Ovr, r.pot AS Pot, r.*
        FROM ratings r JOIN players p ON r.player_id = p.player_id
        WHERE (p.team_id = ? OR p.parent_team_id = ?) AND p.level = ?
          AND r.snapshot_date = (SELECT MAX(r2.snapshot_date) FROM ratings r2 WHERE r2.player_id = r.player_id)
    """, (ORG_ID, ORG_ID, level_int)).fetchall()]
    all_players = [dict(r) for r in conn.execute("""
        SELECT player_id, name AS Name, age AS Age, team_id, parent_team_id,
               level AS Level, pos AS Pos, role AS Role
        FROM players WHERE team_id = ? OR parent_team_id = ?
    """, (ORG_ID, ORG_ID)).fetchall()]
    roster = {p["player_id"]: p for p in all_players if str(p.get("Level")) == str(level_int)}

    role_map = {str(k): v for k, v in _cfg.role_map.items()}

    # Load cached FV from prospect_fv for today's game_date if available
    cached_fv = {}
    if game_date:
        rows = conn.execute(
            "SELECT player_id, fv, fv_str, prospect_surplus FROM prospect_fv WHERE eval_date=?", (game_date,)
        ).fetchall()
        cached_fv = {r[0]: (r[1], r[2], r[3]) for r in rows}

    players = []
    for rat in ratings:
        pid = rat["ID"]
        ros = roster.get(pid, {})
        if ros.get("Retired", 0):
            continue
        age = rat.get("Age", ros.get("Age", 99))
        if age >= 26:
            continue
        # Intl-complex filter drops low-ceiling teenagers by POT. OVR/POT-less
        # leagues (e.g. PPL) return NULL POT — fall back to the app's ceiling
        # score so the filter still works.
        _pot = rat.get("Pot")
        if _pot is None:
            _pot = rat.get("ceiling_score")
        if level_key == "intl" and _pot is not None and _pot < 40:
            continue

        role_num = str(ros.get("Role", 0))
        role_str = role_map.get(role_num, "position_player")

        p = dict(rat)
        # OVR/POT-less leagues (e.g. PPL) store NULL. Fall back to the app's own
        # composite/ceiling scores so gap-based logic (Pot - Ovr) works.
        p["Ovr"] = (rat.get("Ovr") if rat.get("Ovr") is not None else rat.get("composite_score")) or 0
        p["Pot"] = (rat.get("Pot") if rat.get("Pot") is not None else rat.get("ceiling_score")) or p["Ovr"]
        p["_level_key"] = level_key
        p["_level_label"] = FARM_LEVELS[level_key]["label"]
        p["_norm_age"] = FARM_LEVELS[level_key]["norm_age"]
        p["_role"] = role_str
        p["_roster"] = ros
        p["Age"] = age

        pos_str = rat.get("Pos", "")
        is_pitcher = (role_str in ("starter", "reliever", "closer")) or (pos_str == "P")
        p["_is_pitcher"] = is_pitcher
        p["_bucket"] = assign_bucket(p)

        if pid in cached_fv:
            fv_base, fv_str, surplus = cached_fv[pid]
            fv_plus = fv_str.endswith("+")
        else:
            fv_base, fv_plus = calc_fv(p)
            fv_str = f"{fv_base}+" if fv_plus else str(fv_base)
            surplus = None

        p["_fv"] = fv_base
        p["_fv_plus"] = fv_plus
        p["_fv_str"] = fv_str
        p["_surplus"] = surplus
        players.append(p)

    return players

def load_all(game_date=None):
    all_players = []
    for lk in FARM_LEVELS:
        all_players.extend(load_level(lk, game_date=game_date))
    # Deduplicate by player_id — keep highest level (lowest level_int)
    seen = {}
    for p in all_players:
        pid = p["ID"]
        if pid not in seen or FARM_LEVEL_INT[p["_level_key"]] < FARM_LEVEL_INT[seen[pid]["_level_key"]]:
            seen[pid] = p
    return list(seen.values())

# ---------------------------------------------------------------------------
# Report formatting
# ---------------------------------------------------------------------------

def fmt_grade(cur, fut):
    return f"{cur}/{fut}"

def pitcher_card(p):
    pitches = top_pitches(p)
    pitch_cols = []
    for f, _ in pitches:
        cur = norm(p.get(f, 0))
        fut = norm(p.get("Pot" + f, 0))
        pitch_cols.append((PITCH_NAMES[f], fmt_grade(cur, fut)))

    ctrl = p.get("Ctrl") or (p.get("Ctrl_R", 1) + p.get("Ctrl_L", 1)) / 2
    ctrl_cur = norm(ctrl)
    ctrl_fut = norm(p.get("PotCtrl", ctrl))
    stf_cur  = norm(p.get("Stf", 1))
    stf_fut  = norm(p.get("PotStf", 1))
    mov_cur  = norm(p.get("Mov", 1))
    mov_fut  = norm(p.get("PotMov", 1))
    vel      = p.get("Vel", "?")

    headers = [n for n, _ in pitch_cols] + ["Velocity", "Stuff", "Movement", "Control"]
    values  = [g for _, g in pitch_cols] + [vel, fmt_grade(stf_cur, stf_fut),
                                              fmt_grade(mov_cur, mov_fut),
                                              fmt_grade(ctrl_cur, ctrl_fut)]
    table = fmt_table(headers, values)

    # GB% context line
    gb = p.get("GB")
    if gb and gb > 0:
        gb_label = "extreme groundball" if gb >= 65 else "groundball" if gb >= 55 else \
                   "neutral" if gb >= 45 else "flyball" if gb >= 35 else "extreme flyball"
        table += f"\nGB%: {gb}% ({gb_label})"

    # L/R split flag — flag extreme splits (>20 raw point gap in stuff or movement)
    split_notes = []
    for tool, lf, rf in [("Stuff", "Stf_L", "Stf_R"), ("Movement", "Mov_L", "Mov_R")]:
        lv, rv = p.get(lf, 0) or 0, p.get(rf, 0) or 0
        if lv and rv and abs(lv - rv) >= 20:
            weak = "vs LHB" if lv < rv else "vs RHB"
            split_notes.append(f"{tool} {norm(lv)}/{norm(rv)} (L/R) — weaker {weak}")
    if split_notes:
        table += "\nSplit flag: " + "; ".join(split_notes)

    return table

def batter_card(p, bucket):
    age = p["Age"]
    use_pot = age <= 23

    hit_cur  = norm(p.get("Cntct", 50))
    hit_fut  = norm(p.get("PotCntct", 50)) if use_pot else hit_cur
    rpow_cur = norm(p.get("Pow", 50))
    rpow_fut = norm(p.get("PotPow", 50)) if use_pot else rpow_cur

    def game_power(pow_f, gap_f, cnt_f):
        rpow = p.get(pow_f, 50)
        base = (p.get(pow_f, 50) + p.get(gap_f, 50)) / 2
        cf   = 0.6 + 0.4 * (p.get(cnt_f, 50) / 100)
        return norm(min(rpow, base * cf))

    gpow_cur = game_power("Pow", "Gap", "Cntct")
    gpow_fut = game_power("PotPow", "PotGap", "PotCntct") if use_pot else gpow_cur
    run      = norm(p.get("Speed", 50))
    eye_cur  = norm(p.get("Eye", 50))
    eye_fut  = norm(p.get("PotEye", 50)) if use_pot else eye_cur
    ks_cur   = norm(p.get("Ks", 50))
    ks_fut   = norm(p.get("PotKs", 50)) if use_pot else ks_cur
    fld_cur, fld_fut = fielding_grade(p, bucket)
    arm      = arm_grade(p, bucket)

    if bucket == "C":
        cblk = norm(p.get("CBlk", 1))
        cfrm = norm(p.get("CFrm", 1))
        headers = ["Hit", "Raw Power", "Game Power", "Run", "Eye", "K-Rate", "Fielding (C)", "Arm", "Blocking", "Framing"]
        values  = [fmt_grade(hit_cur, hit_fut), fmt_grade(rpow_cur, rpow_fut),
                   fmt_grade(gpow_cur, gpow_fut), str(run),
                   fmt_grade(eye_cur, eye_fut), fmt_grade(ks_cur, ks_fut),
                   fmt_grade(fld_cur, fld_fut), str(arm), str(cblk), str(cfrm)]
    else:
        headers = ["Hit", "Raw Power", "Game Power", "Run", "Eye", "K-Rate", "Fielding", "Arm"]
        values  = [fmt_grade(hit_cur, hit_fut), fmt_grade(rpow_cur, rpow_fut),
                   fmt_grade(gpow_cur, gpow_fut), str(run),
                   fmt_grade(eye_cur, eye_fut), fmt_grade(ks_cur, ks_fut),
                   fmt_grade(fld_cur, fld_fut), str(arm)]

    table = fmt_table(headers, values)

    # Defensive detail — Range/Error for notable values
    is_if = bucket in ("SS", "2B", "3B", "1B")
    rng = norm_floor(p.get("IFR" if is_if else "OFR", 0))
    err = norm_floor(p.get("IFE" if is_if else "OFE", 0))
    def_parts = []
    if rng >= 60 or rng <= 35: def_parts.append(f"Range {rng}")
    if err >= 60 or err <= 35: def_parts.append(f"Error {err}")
    if bucket in ("SS", "2B"):
        tdp = norm_floor(p.get("TDP", 0))
        if tdp >= 60 or tdp <= 35: def_parts.append(f"TDP {tdp}")
    if def_parts:
        table += "\nDefense detail: " + ", ".join(def_parts)

    # L/R split flag — flag extreme splits (>20 raw point gap in contact or power)
    split_notes = []
    for tool, lf, rf in [("Contact", "Cntct_L", "Cntct_R"), ("Power", "Pow_L", "Pow_R")]:
        lv, rv = p.get(lf, 0) or 0, p.get(rf, 0) or 0
        if lv and rv and abs(lv - rv) >= 20:
            weak = "vs LHP" if lv < rv else "vs RHP"
            split_notes.append(f"{tool} {norm(lv)}/{norm(rv)} (L/R) — weaker {weak}")
    if split_notes:
        table += "\nSplit flag: " + "; ".join(split_notes)

    return table

def prospect_card(rank, p, history):
    pid = p["ID"]
    name = p["Name"]
    bucket = p["_bucket"]
    level = p["_level_label"]
    age = p["Age"]
    fv_str = p["_fv_str"]

    # FV movement
    prior = history.get(pid)
    fv_note = ""
    if prior:
        delta = p["_fv"] - prior["fv"]
        if delta > 0:   fv_note = f" (↑{delta} since last eval)"
        elif delta < 0: fv_note = f" (↓{abs(delta)} since last eval)"

    ht = height_str(p["Height"]) if p.get("Height") else None
    bats = p.get("Bats")
    throws = p.get("Throws")
    bio = f"{ht} | {bats}/{throws}" if ht and bats and throws else None

    lines = [f"{rank}. {name} | {bucket} | {level} | Age {age} | FV {fv_str}{fv_note}"]
    if bio:
        lines.append(bio)
    lines += ["", "Tool Grades (Present/Future)"]

    if p["_is_pitcher"]:
        lines.append(pitcher_card(p))
    else:
        lines.append(batter_card(p, bucket))

    return "\n".join(lines)

# ---------------------------------------------------------------------------
# Scouting summaries (data-driven, no LLM)
# ---------------------------------------------------------------------------

GRADE_LABELS = {80:"elite",75:"plus-plus",70:"plus-plus",65:"above-average",
                60:"plus",55:"above-average",50:"average",45:"fringe-average",
                40:"below-average",35:"well below-average",30:"well below-average",20:"poor"}

def grade_label(g):
    for threshold in sorted(GRADE_LABELS.keys(), reverse=True):
        if g >= threshold:
            return GRADE_LABELS[threshold]
    return "poor"

def pitcher_summary(p):
    bucket = p["_bucket"]
    age = p["Age"]
    norm_age = p["_norm_age"]
    pitches = top_pitches(p)
    stm = p.get("Stm", 0)

    ctrl = p.get("Ctrl") or (p.get("Ctrl_R", 1) + p.get("Ctrl_L", 1)) / 2
    ctrl_fut = norm(p.get("PotCtrl", ctrl))
    stf_fut  = norm(p.get("PotStf", p.get("Stf", 1)))
    best_pitch_name = PITCH_NAMES[pitches[0][0]] if pitches else "fastball"
    best_pitch_fut  = norm(p.get("Pot" + pitches[0][0], pitches[0][1])) if pitches else 50
    vel = p.get("Vel", "?")

    summary_parts = [
        f"A {grade_label(best_pitch_fut)} {best_pitch_name} ({vel} mph) headlines the arsenal."
    ]
    if len(pitches) >= 2:
        p2_name = PITCH_NAMES[pitches[1][0]]
        p2_fut  = norm(p.get("Pot" + pitches[1][0], pitches[1][1]))
        summary_parts.append(f"The {p2_name} projects as a {grade_label(p2_fut)} secondary offering.")

    ctrl_label = grade_label(ctrl_fut)
    summary_parts.append(f"Control projects to {ctrl_label}.")

    if stm < 50 and bucket == "SP":
        summary_parts.append("Lacks the durability to project as a true starter — better suited to shorter stints.")

    if p.get("Acc") == "L":
        summary_parts.append("Additional scouting is needed for a complete picture.")

    # AAA near-MLB note
    if p["_level_key"] == "aaa" and abs(p["Ovr"] - p["Pot"]) <= 5 and age >= norm_age - 1:
        summary_parts.append("Knocking on the door of the big league club.")

    return " ".join(summary_parts)

def batter_summary(p, bucket):
    age = p["Age"]
    norm_age = p["_norm_age"]
    use_pot = age <= 23

    hit_fut  = norm(p.get("PotCntct", p.get("Cntct", 50)))
    rpow_fut = norm(p.get("PotPow", p.get("Pow", 50)))
    gpow_fut = norm((p.get("PotPow", p.get("Pow", 50)) + p.get("PotGap", p.get("Gap", 50))) / 2)
    eye_fut  = norm(p.get("PotEye", p.get("Eye", 50)))
    ks_fut   = norm(p.get("PotKs", p.get("Ks", 50)))
    fld_cur, fld_fut = fielding_grade(p, bucket)
    run      = norm(p.get("Speed", 50))

    # Best offensive tool
    off_tools = {"hit tool": hit_fut, "raw power": rpow_fut, "game power": gpow_fut,
                 "plate discipline": eye_fut, "contact ability": ks_fut}
    best_tool = max(off_tools, key=off_tools.get)
    best_val  = off_tools[best_tool]

    summary_parts = [f"A {grade_label(best_val)} {best_tool} is the carrying tool."]

    # Defense
    def_label = grade_label(fld_fut)
    summary_parts.append(f"Projects as a {def_label} defender at {bucket}.")

    # Biggest risk — lowest offensive tool
    worst_tool = min(off_tools, key=off_tools.get)
    worst_val  = off_tools[worst_tool]
    if worst_val <= 45:
        summary_parts.append(f"The {worst_tool} ({grade_label(worst_val)}) is the primary concern.")

    # Ceiling/floor
    fv = p["_fv"]
    if fv >= 60:
        summary_parts.append("Ceiling is an All-Star caliber regular.")
    elif fv >= 50:
        summary_parts.append("Ceiling is an everyday player.")
    elif fv >= 45:
        summary_parts.append("Ceiling is a low-end regular or platoon piece.")
    else:
        summary_parts.append("Projects as a bench bat or organizational depth.")

    if p.get("Acc") == "L":
        summary_parts.append("Additional scouting is needed for a complete picture.")

    if p["_level_key"] == "aaa" and abs(p["Ovr"] - p["Pot"]) <= 5 and age >= norm_age - 1:
        summary_parts.append("Knocking on the door of the big league club.")

    return " ".join(summary_parts)

PROSPECTS_PATH = os.path.join(_league_dir(), "history", "prospects.json")

# ---------------------------------------------------------------------------
# Prospects (unified history + notes)
# ---------------------------------------------------------------------------

def load_prospects():
    """Return dict of player_id (int) -> prospect record."""
    if not os.path.exists(PROSPECTS_PATH):
        return {}
    with open(PROSPECTS_PATH) as f:
        data = json.load(f)
    # Keys are stored as strings in JSON; convert to int
    return {int(k): v for k, v in data.items()}

def load_history():
    """Return dict of player_id -> list of history entries sorted oldest-first."""
    prospects = load_prospects()
    return {pid: sorted(p["history"], key=lambda e: e["date"])
            for pid, p in prospects.items() if p.get("history")}

def load_notes():
    """Return dict of player_id -> note record (excludes archived)."""
    prospects = load_prospects()
    return {pid: p for pid, p in prospects.items()
            if not p.get("archived") and (p.get("summary") or p.get("watch_summary"))}

def _parse_fv(fv):
    """Parse fv value which may be int or string like '60+'."""
    if isinstance(fv, int):
        return fv
    return int(str(fv).rstrip("+"))

def dev_signal(pid, current_fv, history, current_ovr=None, game_year=None):
    """Return a scaffold flag string describing development trajectory."""
    from datetime import date, timedelta
    entries = history.get(pid, [])
    if not entries:
        return "[NEW PLAYER]"

    today = date.fromisoformat(entries[-1]["date"])
    window_start = today - timedelta(days=180)

    # Baseline: oldest entry within the trailing 180-day window
    window_entries = [e for e in entries if date.fromisoformat(e["date"]) >= window_start]
    baseline = window_entries[0] if window_entries else entries[-1]

    baseline_fv  = _parse_fv(baseline["fv"])
    baseline_ovr = baseline.get("ovr")
    delta_fv     = current_fv - baseline_fv
    delta_ovr    = (current_ovr - baseline_ovr) if (current_ovr and baseline_ovr) else 0
    span         = (today - date.fromisoformat(baseline["date"])).days

    # FV movement (vs baseline)
    if abs(delta_fv) >= 5:
        direction = f"+{delta_fv}" if delta_fv > 0 else str(delta_fv)
        return f"[FV {direction} — update summary]"

    # Developing: Ovr up ≥5 within the window, even if FV is flat
    if delta_ovr >= 5:
        return f"[DEVELOPING — Ovr +{delta_ovr} over {span} days]"

    # Stagnation: flat FV and flat Ovr across a span of at least 180 days
    if span >= 180 and delta_fv == 0 and delta_ovr < 3:
        return f"[STAGNANT — FV {current_fv} for {span} days]"

    # Acceleration: FV up ≥5 in two consecutive intervals (uses full history)
    if len(entries) >= 2:
        prev_delta = _parse_fv(entries[-1]["fv"]) - _parse_fv(entries[-2]["fv"])
        if prev_delta >= 5 and delta_fv >= 5:
            return f"[ACCELERATING — +{prev_delta}, +{delta_fv}]"

    # New season: if the last history entry is from a different year, flag for refresh
    if game_year and entries:
        last_year = int(entries[-1]["date"][:4])
        if game_year != last_year:
            return f"[NEW SEASON ({last_year} → {game_year}) — review summary]"

    return "[FV STABLE — reuse summary]"

def append_history(top15, watch, game_date):
    """Update prospects.json with current eval snapshot; trim entries >3 game-years old."""
    cutoff_year = int(game_date[:4]) - 3
    cutoff = f"{cutoff_year}{game_date[4:]}"

    prospects = load_prospects()
    active_ids = {p["ID"] for p in top15 + watch}

    for p in top15 + watch:
        pid = p["ID"]
        entry = {
            "date": game_date,
            "fv": p["_fv"],
            "fv_str": p["_fv_str"],
            "level": p["_level_label"],
            "bucket": p["_bucket"],
            "ovr": p.get("Ovr"),
        }
        if pid not in prospects:
            prospects[pid] = {"name": p["Name"], "history": []}

        # Update name in case it changed
        prospects[pid]["name"] = p["Name"]

        # Upsert: replace existing entry for this date or append
        hist = prospects[pid].setdefault("history", [])
        for i, e in enumerate(hist):
            if e["date"] == game_date:
                hist[i] = entry
                break
        else:
            hist.append(entry)

        # Trim old entries
        prospects[pid]["history"] = [e for e in hist if e["date"] >= cutoff]

    # Archive players absent from last 3 distinct eval dates
    all_dates = sorted({e["date"]
                        for p in prospects.values()
                        for e in p.get("history", [])}, reverse=True)
    recent_dates = set(all_dates[:3])
    if len(recent_dates) >= 3:
        for pid, p in prospects.items():
            if pid not in active_ids and not p.get("archived"):
                player_dates = {e["date"] for e in p.get("history", [])}
                if not player_dates & recent_dates:
                    p["archived"] = True

    os.makedirs(os.path.dirname(PROSPECTS_PATH), exist_ok=True)
    with open(PROSPECTS_PATH, "w") as f:
        json.dump({str(k): v for k, v in prospects.items()}, f, indent=2)

# ---------------------------------------------------------------------------
# Scaffold output
# ---------------------------------------------------------------------------

def near_mlb_note(p):
    if (p["_level_key"] == "aaa"
            and abs(p["Ovr"] - p["Pot"]) <= 5
            and p["Age"] >= p["_norm_age"] - 1):
        return " [NEAR MLB READY]"
    return ""

def acc_note(p):
    return " [Acc=L: wider range of outcomes]" if p.get("Acc") == "L" else ""

def prospect_scaffold(rank, p, history, notes, game_year=None):
    pid = p["ID"]
    bucket = p["_bucket"]
    level = p["_level_label"]
    age = p["Age"]
    fv_str = p["_fv_str"]
    signal = dev_signal(pid, p["_fv"], history, current_ovr=p.get("Ovr"), game_year=game_year)
    flags  = near_mlb_note(p) + acc_note(p)

    ht = height_str(p["Height"]) if p.get("Height") else None
    bats = p.get("Bats")
    throws = p.get("Throws")
    bio = f"{ht} | {bats}/{throws}" if ht and bats and throws else None

    lines = [f"{rank}. {p['Name']} | {bucket} | {level} | Age {age} | FV {fv_str}"]
    if bio:
        lines.append(bio)
    lines += ["", "Tool Grades (Present/Future)"]
    lines.append(pitcher_card(p) if p["_is_pitcher"] else batter_card(p, bucket))

    # For pitchers, surface stamina tier so agent applies correct prose rule
    if p["_is_pitcher"]:
        stm = p.get("Stm", 0)
        if stm >= 50:
            stm_note = "Stm tier: FULL STARTER (no stamina comment needed)"
        elif stm >= 40:
            stm_note = f"Stm tier: SHORT STARTER — profiles as 5-inning starter, will not go deep (do not project as reliever)"
        else:
            stm_note = f"Stm tier: RELIEVER — lacks stamina to start"
        lines.append(f"[{stm_note}]")

    surplus = p.get("_surplus")
    if surplus is not None:
        lines.append(f"Surplus value: ${surplus/1e6:.1f}M")

    note = notes.get(pid)
    existing_summary = note.get("summary") if note else None

    if signal == "[NEW PLAYER]":
        lines.append(f"\n{signal}{flags}")
    elif existing_summary:
        lines.append(f"\n{signal}{flags}\n{existing_summary}")
    else:
        lines.append(f"\n{signal}{flags}\n[NO STORED SUMMARY — write one]")

    return "\n".join(lines)

def watch_scaffold(p, history, notes, game_year=None):
    pid = p["ID"]
    bucket = p["_bucket"]
    level = p["_level_label"]
    age = p["Age"]
    fv_str = p["_fv_str"]

    signal = dev_signal(pid, p["_fv"], history, current_ovr=p.get("Ovr"), game_year=game_year)
    flags  = acc_note(p)

    lines = [
        f"**{p['Name']}** | {bucket} | {level} | Age {age} | FV {fv_str}",
        f"player_id: {pid}",
    ]

    note = notes.get(pid)
    watch_text = (note.get("watch_summary") or note.get("summary")) if note else None

    if watch_text:
        lines.append(f"{signal}{flags}\n{watch_text}")
    else:
        lines.append(f"{signal}{flags}\n[NEW PLAYER — write watch blurb per guide standards]")

    return "\n".join(lines)

def org_data(top15, all_players):
    from collections import Counter

    all_buckets    = {"C","SS","2B","CF","COF","3B","1B","SP","RP"}
    fv50_by_bucket = Counter(p["_bucket"] for p in top15 if p["_fv"] >= 50)
    fv45_buckets   = {p["_bucket"] for p in top15 if p["_fv"] >= 45}
    by_level       = Counter(p["_level_label"] for p in top15)
    ages           = [p["Age"] for p in top15]
    thin_buckets   = sorted(all_buckets - fv45_buckets)
    missing_levels = sorted({v["label"] for v in FARM_LEVELS.values()} - set(by_level.keys()))

    old_counts = {}
    for p in all_players:
        if p["Age"] >= p["_norm_age"] + 2:
            lv = p["_level_label"]
            old_counts[lv] = old_counts.get(lv, 0) + 1

    aaa_old       = len([p for p in all_players if p["_level_key"] == "aaa" and p["Age"] >= FARM_LEVELS["aaa"]["norm_age"] + 2])
    young_ceiling = len([p for p in all_players if p["Age"] <= 20 and p["Pot"] >= 55])

    lines = [
        "## Org Data (for agent use in Sections 3–5)\n",
        f"FV 50+ by bucket: {dict(fv50_by_bucket.most_common())}",
        f"Top-15 by level: {dict(by_level.most_common())}",
        f"Age range: {min(ages)}–{max(ages)}, avg {sum(ages)/len(ages):.1f}",
        f"No FV 45+ at: {thin_buckets}",
        f"Levels with no top-15 rep: {missing_levels}",
        f"Old-for-level counts: {old_counts}",
        f"Old-for-level at AAA: {aaa_old}",
        f"High-ceiling prospects age ≤20 (Pot 55+): {young_ceiling}",
    ]
    return "\n".join(lines)

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    ld = _league_dir()
    state     = json.load(open(os.path.join(ld, "config", "state.json")))
    game_date = state["game_date"]
    game_year = state["year"]

    all_players = load_all(game_date=game_date)
    history     = load_history()
    notes       = load_notes()

    # Intl players get a sort penalty so same-FV players at higher levels rank above them
    def sort_key(p):
        fv_val = p["_fv"] + (0.1 if p["_fv_plus"] else 0)
        intl_penalty = 0.05 if p["_level_key"] == "intl" else 0
        return (-fv_val + intl_penalty, p["Age"])

    ranked = sorted(all_players, key=sort_key)
    top15  = ranked[:15]
    watch  = [p for p in ranked[15:] if p["Age"] <= 22 and (p["Pot"] - p["Ovr"]) >= 15 and p["_level_key"] != "intl"][:5]

    lines = [
        f"# {_cfg.team_name(ORG_ID) or 'My Team'} Farm System — Scaffold",
        f"## Game Date: {game_date}",
        "## Generated: agent completes prose per farm_analysis_guide.md",
        "",
        "# Section 1 — Top 15 Prospects",
        "",
    ]
    for i, p in enumerate(top15, 1):
        lines.append(prospect_scaffold(i, p, history, notes, game_year))
        lines.append("\n---\n")

    lines.append("# Section 2 — Players to Watch\n")
    for p in watch:
        lines.append(watch_scaffold(p, history, notes, game_year))
        lines.append("")

    lines.append(org_data(top15, all_players))

    scaffold = "\n".join(lines)

    out_path = os.path.join(ld, "tmp", f"farm_scaffold_{game_date}.md")
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

    append_history(top15, watch, game_date)

    print(scaffold)
    print(f"\n--- Scaffold written to {out_path} ---")

if __name__ == "__main__":
    main()
