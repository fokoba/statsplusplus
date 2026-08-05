"""custom_upload.py — Evaluate an uploaded OOTP "All Columns" roster export
in complete isolation from this league's database.

Every scoring function this module calls (compute_composite_hitter/pitcher,
compute_ceiling, compute_true_ceiling, calc_fv) is pure — no DB access, no
league_context, no side effects — so a CSV row can be scored exactly the
same way this app scores its own players, without touching StatsPlus data
at all. There is deliberately no MLB stat-performance blending here: the
uploaded sheet has no game logs to blend from, so every player is scored
100% off scouting tools (Ovr = tool_only composite).

Column mapping was reverse-engineered and validated against a real export
of this league's own Philadelphia Athletics roster, cross-checked against
known values already in this league's database (e.g. Melvin Valdes's
Stuff/Movement/Control matched exactly: STU=50, MOV=55, CON=55).
"""

import csv
import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from evaluation_engine import (
    compute_composite_hitter, compute_composite_pitcher,
    compute_ceiling, compute_true_ceiling,
    DEFAULT_TOOL_WEIGHTS,
)
from fv_model import calc_fv, DEFENSIVE_WEIGHTS, LEVEL_NORM_AGE
from constants import PITCH_FIELDS
from player_utils import display_pos
from db import get_conn

# Same exclusion set as web/team_queries.py's get_free_agent_candidates —
# NPB-drafted players aren't actually signable even when marked a free agent.
_NIPPON_TEAM_IDS = tuple(range(288, 302)) + tuple(range(320, 334))


# ---------------------------------------------------------------------------
# CSV column -> pitch-field name (validated against Melvin Valdes's known
# arsenal: Chg/Crv/Sld/Snk/Splt/Kncrv, matching his exported "GB'er" profile)
# ---------------------------------------------------------------------------
_PITCH_COL_MAP = {
    "FB": "Fst", "CH": "Chg", "CB": "Crv", "SL": "Sld", "SI": "Snk",
    "SP": "Splt", "CT": "Cutt", "FO": "Frk", "CC": "CirChg",
    "SC": "Scr", "KC": "Kncrv", "KN": "Knbl",
}

_LEVEL_KEY_BY_ABBR = {
    "MLB": "mlb", "AAA": "aaa", "AA": "aa", "A": "a",
    "A-Short": "a-short", "ROOK": "a-short", "USL": "usl", "DSL": "dsl",
}


def _dedupe_header(header: list[str]) -> list[str]:
    """OOTP's export repeats several column names (TM, ORG, LG, Lev, B, T,
    INJ, DEM, Name) — suffix duplicates so nothing gets silently overwritten
    when zipped into a dict."""
    seen: dict[str, int] = {}
    out = []
    for h in header:
        if h in seen:
            seen[h] += 1
            out.append(f"{h}__{seen[h]}")
        else:
            seen[h] = 0
            out.append(h)
    return out


def _num(v):
    """'-' and '' mean "not applicable" in this export; everything else
    is an int rating on the league's normalized scale."""
    if v is None:
        return None
    v = v.strip()
    if v in ("", "-"):
        return None
    try:
        return int(v)
    except ValueError:
        try:
            return int(float(v))
        except ValueError:
            return None


def parse_rows(file_bytes: bytes) -> list[dict]:
    """Parse the raw CSV bytes into deduped-header dict rows."""
    text = file_bytes.decode("utf-8-sig")
    reader = csv.reader(io.StringIO(text))
    header = _dedupe_header(next(reader))
    return [dict(zip(header, row)) for row in reader if any(row)]


# ---------------------------------------------------------------------------
# Per-row extraction
# ---------------------------------------------------------------------------

def _hitter_tools(d):
    return {
        "contact": _num(d.get("CON")), "gap": _num(d.get("GAP")),
        "power": _num(d.get("POW")), "eye": _num(d.get("EYE")),
        "speed": _num(d.get("SPE")), "steal": _num(d.get("STE")),
        "stl_rt": _num(d.get("SR")),
    }


# web/player_queries.py's "Platoon candidate" insight uses 15 — that's a
# definitive badge, calibrated conservative on purpose. This is a discovery
# filter (actively hunting for candidates), so a lower bar serves better;
# the actual gap size is still shown so you can judge quality yourself.
_PLATOON_GAP_THRESHOLD = 10


def _platoon_split(d, is_pitcher):
    """(max_gap, strong_side) for whichever tool has the biggest L/R split.

    strong_side is "vs RHB"/"vs LHB" for pitchers (which batter hand they
    fare better against) or "vs RHP"/"vs LHP" for hitters (which pitcher
    hand they fare better against). Returns (gap, None) if no split data or
    the gap doesn't meet the threshold.
    """
    if is_pitcher:
        pairs = [("STU vL", "STU vR")]
        strong_labels = ("vs LHB", "vs RHB")
    else:
        pairs = [("CON vL", "CON vR"), ("POW vL", "POW vR"),
                 ("GAP vL", "GAP vR"), ("EYE vL", "EYE vR")]
        strong_labels = ("vs LHP", "vs RHP")

    max_gap, strong_side = 0, None
    for l_col, r_col in pairs:
        lv, rv = _num(d.get(l_col)), _num(d.get(r_col))
        if lv is None or rv is None:
            continue
        gap = abs(lv - rv)
        if gap > max_gap:
            max_gap = gap
            # Higher "vL" rating -> stronger facing that side (matches
            # player_queries.py's convention, generalized to both labels).
            strong_side = strong_labels[0] if lv > rv else strong_labels[1]
    if max_gap < _PLATOON_GAP_THRESHOLD:
        return max_gap, None
    return max_gap, strong_side


def _hitter_potential_tools(d):
    return {
        "contact": _num(d.get("CON P")), "gap": _num(d.get("GAP P")),
        "power": _num(d.get("POW P")), "eye": _num(d.get("EYE P")),
        # No separate potential column for these — same as current, matching
        # _extract_potential_hitter_tools() in evaluation_engine.py.
        "speed": _num(d.get("SPE")), "steal": _num(d.get("STE")),
        "stl_rt": _num(d.get("SR")),
    }


def _defense_for_bucket(d, bucket):
    """Raw defensive component ratings + weights for this bucket.

    COF (corner outfield) takes the max of LF/RF weighted values, matching
    defensive_score()'s existing COF handling in player_utils.py.
    """
    if bucket == "C":
        defense = {"CFrm": _num(d.get("C FRM")), "CBlk": _num(d.get("C ABI")),
                   "CArm": _num(d.get("C ARM"))}
        return defense, DEFENSIVE_WEIGHTS["C"]
    if bucket in ("SS", "2B", "3B"):
        defense = {"IFR": _num(d.get("IF RNG")), "IFE": _num(d.get("IF ERR")),
                   "IFA": _num(d.get("IF ARM")), "TDP": _num(d.get("TDP"))}
        return defense, DEFENSIVE_WEIGHTS[bucket]
    if bucket == "CF":
        defense = {"OFR": _num(d.get("OF RNG")), "OFE": _num(d.get("OF ERR")),
                   "OFA": _num(d.get("OF ARM"))}
        return defense, DEFENSIVE_WEIGHTS["CF"]
    if bucket == "COF":
        defense = {"OFR": _num(d.get("OF RNG")), "OFE": _num(d.get("OF ERR")),
                   "OFA": _num(d.get("OF ARM"))}
        # Pick whichever corner's weight profile grades higher — same
        # max(lf, rf) approach as defensive_score() for COF.
        def _score(weights):
            return sum((defense.get(k) or 0) * w for k, w in weights.items())
        lf_w, rf_w = DEFENSIVE_WEIGHTS["COF_LF"], DEFENSIVE_WEIGHTS["COF_RF"]
        return defense, (lf_w if _score(lf_w) >= _score(rf_w) else rf_w)
    return {}, {}


def _pitcher_tools(d):
    return {
        "stuff": _num(d.get("STU")), "movement": _num(d.get("MOV")),
        "control": _num(d.get("CON__1")), "hra": _num(d.get("HRR")),
        "pbabip": _num(d.get("PBABIP")),
        "stuff_l": _num(d.get("STU vL")), "stuff_r": _num(d.get("STU vR")),
    }


def _pitcher_potential_tools(d):
    return {
        "stuff": _num(d.get("STU P")), "movement": _num(d.get("MOV P")),
        "control": _num(d.get("CON P__1")), "hra": _num(d.get("HRR P")),
        "pbabip": _num(d.get("PBABIP P")),
    }


def _arsenal(d):
    out = {}
    for col, field in _PITCH_COL_MAP.items():
        v = _num(d.get(col))
        if v is not None:
            out[field] = v
    return out


def _bucket_for_assign(d, is_pitcher, role_str, stamina):
    """Build the dict assign_bucket() expects and call it."""
    p = {
        "Pos": d.get("POS") if is_pitcher else str(_pos_code(d.get("POS"))),
        "_role": role_str,
        "Stm": stamina,
        "PotC": _num(d.get("C Pot")), "PotSS": _num(d.get("SS Pot")),
        "Pot2B": _num(d.get("2B Pot")), "Pot3B": _num(d.get("3B Pot")),
        "PotCF": _num(d.get("CF Pot")), "PotLF": _num(d.get("LF Pot")),
        "PotRF": _num(d.get("RF Pot")), "Pot1B": _num(d.get("1B Pot")),
        "PotKnbl": _num(d.get("KNP")), "PotKncrv": _num(d.get("KCP")),
    }
    for f in PITCH_FIELDS:
        col = {v: k for k, v in _PITCH_COL_MAP.items()}.get(f)
        p[f"Pot{f}"] = _num(d.get(f"{col}P")) if col else None
    from player_utils import assign_bucket
    if is_pitcher:
        p["_role"] = {"SP": "starter", "RP": "reliever", "CL": "closer"}.get(role_str, "reliever")
        p["Pos"] = "P"
    return assign_bucket(p)


_POS_NAME_TO_CODE = {"P": 1, "C": 2, "1B": 3, "2B": 4, "3B": 5, "SS": 6,
                     "LF": 7, "CF": 8, "RF": 9, "DH": 10}


def _pos_code(pos_name):
    return _POS_NAME_TO_CODE.get((pos_name or "").strip(), 0)


_ACC_MAP = {"Very High": "VH", "High": "H", "Average": "A", "Low": "L", "Very Low": "L"}

# Priority order for "best position" display: (label, CSV potential column(s),
# minimum grade to qualify). Checked in order; first match wins, so a
# 62-potential SS displays as SS even if he'd also clear the CF or 1B bar.
_BEST_POSITION_PRIORITY = [
    ("SS", ("SS Pot",), 60),
    ("CF", ("CF Pot",), 60),
    ("C", ("C Pot",), 55),
    ("2B", ("2B Pot",), 55),
    ("3B", ("3B Pot",), 55),
    ("LF/RF", ("LF Pot", "RF Pot"), 55),
    ("1B", ("1B Pot",), 55),
]


def _best_position(d):
    """Highest-priority position a hitter's potential clears the bar for.

    Returns (label, grade) for the first (highest-priority) position whose
    potential rating meets its threshold, or (None, None) if none qualify.
    """
    for label, cols, threshold in _BEST_POSITION_PRIORITY:
        grade = max((_num(d.get(c)) or 0) for c in cols)
        if grade >= threshold:
            return label, grade
    return None, None


def evaluate_row(d: dict) -> dict | None:
    """Evaluate one CSV row. Returns None for rows with no usable ID/name."""
    pid = (d.get("ID") or "").strip()
    if not pid:
        return None
    name = f"{d.get('First Name', '')} {d.get('Last Name', '')}".strip() or d.get("Name", "")
    # ORG is always the parent MLB organization, even for a minor leaguer
    # rostered on a specific affiliate (TM/TM__1/TM__2 give the affiliate's
    # own name/city/abbreviation instead) — use ORG for team grouping so
    # minor leaguers bucket under their real MLB parent, not their affiliate.
    org_name = (d.get("ORG") or "").strip()
    org_abbr = (d.get("ORG__1") or "").strip()
    age = _num(d.get("Age"))
    role_str = (d.get("RL") or "").strip().upper()
    is_pitcher = role_str in ("SP", "RP", "CL")
    stamina = _num(d.get("STM")) or 50
    acc = _ACC_MAP.get((d.get("SctAcc") or "").strip(), "A")
    wrk_ethic = (d.get("WE") or "N").strip()
    intel = (d.get("INT") or "N").strip()
    level_abbr = (d.get("Lev__1") or "MLB").strip().upper()
    level_key = _LEVEL_KEY_BY_ABBR.get(level_abbr, "mlb")

    bucket = _bucket_for_assign(d, is_pitcher, role_str, stamina)

    if is_pitcher:
        role = "RP" if role_str in ("RP", "CL") else "SP"
        weights = DEFAULT_TOOL_WEIGHTS["pitcher"][role]
        tools = _pitcher_tools(d)
        pot_tools = _pitcher_potential_tools(d)
        arsenal = _arsenal(d)
        composite = compute_composite_pitcher(tools, weights, arsenal, stamina, role)
        ceiling = compute_ceiling(
            pot_tools, weights, composite, accuracy=acc, work_ethic=wrk_ethic,
            is_pitcher=True, arsenal=arsenal, stamina=stamina, role=role, age=age or 25,
        )
        true_ceiling = compute_true_ceiling(
            pot_tools, weights, composite, accuracy=acc, work_ethic=wrk_ethic,
            is_pitcher=True, arsenal=arsenal, stamina=stamina, role=role,
        )
        offensive_ceiling = None
        stf_l, stf_r = tools.get("stuff_l"), tools.get("stuff_r")
    else:
        hitter_weights = DEFAULT_TOOL_WEIGHTS["hitter"]
        weights = hitter_weights.get(bucket, hitter_weights.get("COF", {}))
        tools = _hitter_tools(d)
        pot_tools = _hitter_potential_tools(d)
        defense, def_weights = _defense_for_bucket(d, bucket)
        pot_defense = defense  # no granular defensive *potential* fields exist in this export either
        composite = compute_composite_hitter(tools, weights, defense, def_weights)
        ceiling = compute_ceiling(
            pot_tools, weights, composite, accuracy=acc, work_ethic=wrk_ethic,
            defense=pot_defense, def_weights=def_weights, age=age or 25,
        )
        true_ceiling = compute_true_ceiling(
            pot_tools, weights, composite, accuracy=acc, work_ethic=wrk_ethic,
            defense=pot_defense, def_weights=def_weights,
        )
        offensive_ceiling = ceiling
        stf_l = stf_r = None

    role = "RP" if role_str in ("RP", "CL") else ("SP" if is_pitcher else bucket)
    norm_age = LEVEL_NORM_AGE.get(level_key, 25)
    fv_input = {
        "Ovr": composite, "Pot": true_ceiling, "Age": age or norm_age,
        "_is_pitcher": is_pitcher, "_bucket": role, "_norm_age": norm_age,
        "Acc": acc, "WrkEthic": wrk_ethic, "Int": intel,
        "Stf_L": stf_l, "Stf_R": stf_r,
        "_offensive_ceiling": offensive_ceiling,
    }
    fv_grade, risk = calc_fv(fv_input)

    rule5_eligible = (d.get("R5") or "").strip().lower() == "yes"
    # Annual salary demand ("$9.0m" etc.) — verified against a real free
    # agent export: DEM is set for ~22% of free agents (the rest show "-",
    # seemingly players who haven't been actively shopped/negotiated with
    # yet, no clean correlation with scouting accuracy). No MLB/MiLB
    # contract-preference field exists anywhere in this export.
    _dem = (d.get("DEM") or "").strip()
    ask = _dem if _dem and _dem != "-" else None
    if_rng = _num(d.get("IF RNG"))
    of_rng = _num(d.get("OF RNG"))
    best_position, best_position_grade = (None, None) if is_pitcher else _best_position(d)

    # NOTE: unverified against a real example. This sample export was your
    # own signed roster, so Claim/WAIV/DFA/TM never show a free-agent or
    # waivers row here to confirm against. Best guess based on standard OOTP
    # convention: a free agent's "TM" field literally reads "Free Agent";
    # "on waivers" is inferred from the Claim/WAIV/DFA columns being
    # populated (non "-") rather than blank. Verify once you upload an
    # export that actually contains such players.
    tm_field = (d.get("TM") or "").strip()
    is_free_agent = tm_field.lower() == "free agent"
    on_waivers = any((d.get(c) or "-").strip() != "-" for c in ("Claim", "WAIV", "DFA"))
    platoon_gap, platoon_strong_side = _platoon_split(d, is_pitcher)

    return {
        "pid": pid, "name": name, "age": age,
        "level": level_abbr, "bucket": role if is_pitcher else bucket,
        "is_pitcher": is_pitcher,
        "composite_score": composite, "ceiling_score": ceiling,
        "true_ceiling": true_ceiling, "fv": fv_grade, "risk": risk,
        "acc": acc, "org": org_name, "org_abbr": org_abbr,
        "rule5_eligible": rule5_eligible, "ask": ask,
        "if_rng": if_rng, "of_rng": of_rng,
        "best_position": best_position, "best_position_grade": best_position_grade,
        "is_free_agent": is_free_agent, "on_waivers": on_waivers,
        "platoon_gap": platoon_gap, "platoon_strong_side": platoon_strong_side,
    }


def _db_free_agent_status(pids: list[str]) -> dict[str, bool]:
    """Look up each pid in this league's own DB and return whether it's a
    truly signable free agent there (same criteria as get_free_agent_candidates
    in team_queries.py: free_agent, not retired, unattached, not NPB-drafted,
    not in the draft-eligible amateur pool). Pids not found in the DB at all
    are omitted from the result so the caller can fall back to a CSV-only
    guess for players this league hasn't seen yet (e.g. a brand new signee).
    """
    try:
        conn = get_conn()
    except Exception:
        return {}
    nippon_qs = ",".join("?" * len(_NIPPON_TEAM_IDS))
    pid_qs = ",".join("?" * len(pids))
    rows = conn.execute(
        f"""SELECT player_id, free_agent, retired, team_id, nation_id,
                   draft_team_id, draft_eligible
            FROM players WHERE player_id IN ({pid_qs})""",
        pids,
    ).fetchall()
    conn.close()
    status = {}
    for r in rows:
        signable = (
            r["free_agent"] == 1 and r["retired"] == 0 and r["team_id"] == 0
            and (r["nation_id"] is None or r["nation_id"] != 98)
            and (r["draft_team_id"] is None or r["draft_team_id"] not in _NIPPON_TEAM_IDS)
            and (r["draft_eligible"] or 0) != 1
        )
        status[str(r["player_id"])] = signable
    return status


def evaluate_csv(file_bytes: bytes) -> list[dict]:
    rows = parse_rows(file_bytes)
    parsed = []
    for d in rows:
        try:
            r = evaluate_row(d)
        except Exception as e:
            r = {"pid": d.get("ID", "?"), "name": d.get("Name", "?"), "error": str(e)}
        if r:
            parsed.append(r)

    pids = [r["pid"] for r in parsed if "error" not in r]
    db_status = _db_free_agent_status(pids) if pids else {}
    for r in parsed:
        if "error" in r:
            continue
        known = db_status.get(r["pid"])
        if known is not None:
            r["is_free_agent"] = known

    return parsed
