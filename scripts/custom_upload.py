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

from statsplusplus.evaluation.composite import (
    compute_composite_hitter, compute_composite_pitcher,
    compute_specialist_score, specialist_label,
)
from statsplusplus.evaluation.ceiling import compute_ceiling, compute_true_ceiling
from statsplusplus.data.evaluation_engine import DEFAULT_TOOL_WEIGHTS, load_tool_weights
from statsplusplus.evaluation.fv import calc_fv_from_dict as calc_fv
from statsplusplus.evaluation.constants import DEFENSIVE_WEIGHTS
from statsplusplus.utils.positions import LEVEL_NORM_AGE, PITCH_FIELDS, display_pos
from statsplusplus.data.db import get_conn
from statsplusplus.evaluation.park_fit import (
    load_park_factors, compute_batter_park_fit, compute_pitcher_park_fit_from_tools,
)
from prospect_value import prospect_surplus_with_option, peak_year_surplus

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

# OOTP's "Type" column — personality archetype (distinct from the
# WE/INT/Lead/Loy/Greed trait ratings). Verified against a real export
# (1,164 rows): Normal, Sparkplug, Unmotivated, Humble, Selfish,
# Outspoken, Captain, Prankster, Disruptive, Fan Fav. Positive/negative
# split is this app's own judgment call (not an OOTP-provided flag) —
# flag if any of these read wrong.
_PERSONALITY_TYPE_POSITIVE = {"Fan Fav", "Sparkplug", "Captain", "Humble", "Prankster"}
_PERSONALITY_TYPE_NEGATIVE = {"Unmotivated", "Selfish", "Outspoken", "Disruptive"}


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


def _dup(d, base):
    """Look up the *second* occurrence of a column name that's duplicated in
    OOTP's export.

    Different OOTP export presets disambiguate duplicate column names
    differently: some exports (e.g. "Organization > Minor Leagues") repeat
    the header verbatim, so _dedupe_header() suffixes the second occurrence
    "__1" (double underscore); others (e.g. "All Free Agents") already come
    with OOTP's own "_1" (single underscore) suffix baked in, so
    _dedupe_header() never touches it — the raw header already has both
    "CON" and "CON_1" as distinct strings. Code that only checked "__1"
    silently read None for every "_1"-style export (confirmed against a
    real "All Free Agents" export: pitcher control, org, and level all came
    back empty, which is exactly what happened for Devin Freshwater — his
    weak control tool got silently dropped from the composite instead of
    dragging it down, inflating his score).
    """
    return d.get(f"{base}_1") if d.get(f"{base}_1") is not None else d.get(f"{base}__1")


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


def _num_scaled(v, scale):
    """Parse a raw tool rating and normalize it to the 20-80 canonical scale
    compute_composite_hitter/pitcher and compute_ceiling/compute_true_ceiling
    expect.

    _num() alone just parses the string — it never converts scale. Every
    tool-extraction function below used to feed raw values straight through,
    silently treating a 1-100-scale league's ratings as if they were already
    20-80 (e.g. this league's Stuff/Movement/Control/Stamina columns go to
    100, confirmed against a real export, while Ovr/Pot cap at 80 — OOTP
    exports these on genuinely different scales even within the same
    league). Matches the normalization evaluation_engine.py already applies
    to the same ratings via statsplusplus.config.ratings.norm_continuous().
    """
    raw = _num(v)
    if raw is None:
        return None
    from statsplusplus.config.ratings import norm_continuous
    return norm_continuous(raw, scale)


def parse_rows(file_bytes: bytes) -> list[dict]:
    """Parse the raw CSV bytes into deduped-header dict rows."""
    text = file_bytes.decode("utf-8-sig")
    reader = csv.reader(io.StringIO(text))
    header = _dedupe_header(next(reader))
    return [dict(zip(header, row)) for row in reader if any(row)]


# ---------------------------------------------------------------------------
# Per-row extraction
# ---------------------------------------------------------------------------

def _hitter_tools(d, scale):
    return {
        "contact": _num_scaled(d.get("CON"), scale), "gap": _num_scaled(d.get("GAP"), scale),
        "power": _num_scaled(d.get("POW"), scale), "eye": _num_scaled(d.get("EYE"), scale),
        "speed": _num_scaled(d.get("SPE"), scale), "steal": _num_scaled(d.get("STE"), scale),
        "stl_rt": _num_scaled(d.get("SR"), scale),
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


def _hitter_potential_tools(d, scale):
    return {
        "contact": _num_scaled(d.get("CON P"), scale), "gap": _num_scaled(d.get("GAP P"), scale),
        "power": _num_scaled(d.get("POW P"), scale), "eye": _num_scaled(d.get("EYE P"), scale),
        # No separate potential column for these — same as current, matching
        # _extract_potential_hitter_tools() in evaluation_engine.py.
        "speed": _num_scaled(d.get("SPE"), scale), "steal": _num_scaled(d.get("STE"), scale),
        "stl_rt": _num_scaled(d.get("SR"), scale),
    }


def _defense_for_bucket(d, bucket, scale):
    """Raw defensive component ratings + weights for this bucket.

    COF (corner outfield) takes the max of LF/RF weighted values, matching
    defensive_score()'s existing COF handling in player_utils.py.
    """
    if bucket == "C":
        defense = {"CFrm": _num_scaled(d.get("C FRM"), scale), "CBlk": _num_scaled(d.get("C ABI"), scale),
                   "CArm": _num_scaled(d.get("C ARM"), scale)}
        return defense, DEFENSIVE_WEIGHTS["C"]
    if bucket in ("SS", "2B", "3B"):
        defense = {"IFR": _num_scaled(d.get("IF RNG"), scale), "IFE": _num_scaled(d.get("IF ERR"), scale),
                   "IFA": _num_scaled(d.get("IF ARM"), scale), "TDP": _num_scaled(d.get("TDP"), scale)}
        return defense, DEFENSIVE_WEIGHTS[bucket]
    if bucket == "CF":
        defense = {"OFR": _num_scaled(d.get("OF RNG"), scale), "OFE": _num_scaled(d.get("OF ERR"), scale),
                   "OFA": _num_scaled(d.get("OF ARM"), scale)}
        return defense, DEFENSIVE_WEIGHTS["CF"]
    if bucket == "COF":
        defense = {"OFR": _num_scaled(d.get("OF RNG"), scale), "OFE": _num_scaled(d.get("OF ERR"), scale),
                   "OFA": _num_scaled(d.get("OF ARM"), scale)}
        # Pick whichever corner's weight profile grades higher — same
        # max(lf, rf) approach as defensive_score() for COF.
        def _score(weights):
            return sum((defense.get(k) or 0) * w for k, w in weights.items())
        lf_w, rf_w = DEFENSIVE_WEIGHTS["COF_LF"], DEFENSIVE_WEIGHTS["COF_RF"]
        return defense, (lf_w if _score(lf_w) >= _score(rf_w) else rf_w)
    return {}, {}


def _extended_pitcher_tool(v, scale):
    """hra/pbabip are real calibrated-weight tool grades in some leagues
    (e.g. ppl: 0.07-0.15 weight, not the near-zero default) but blank/0 in
    others — normalizing a blank input would land exactly on the 20-80
    floor and get treated as a genuine (terrible) rating instead of "no
    data". Matches evaluation_engine.py's own `if val and val > 20` guard.
    """
    val = _num_scaled(v, scale)
    return val if val and val > 20 else None


def _pitcher_tools(d, scale):
    return {
        "stuff": _num_scaled(d.get("STU"), scale), "movement": _num_scaled(d.get("MOV"), scale),
        "control": _num_scaled(_dup(d, "CON"), scale),
        "hra": _extended_pitcher_tool(d.get("HRR"), scale),
        "pbabip": _extended_pitcher_tool(d.get("PBABIP"), scale),
        "stuff_l": _num_scaled(d.get("STU vL"), scale), "stuff_r": _num_scaled(d.get("STU vR"), scale),
    }


def _pitcher_potential_tools(d, scale):
    return {
        "stuff": _num_scaled(d.get("STU P"), scale), "movement": _num_scaled(d.get("MOV P"), scale),
        "control": _num_scaled(_dup(d, "CON P"), scale),
        "hra": _extended_pitcher_tool(d.get("HRR P"), scale),
        "pbabip": _extended_pitcher_tool(d.get("PBABIP P"), scale),
    }


def _arsenal(d, scale):
    out = {}
    for col, field in _PITCH_COL_MAP.items():
        v = _num_scaled(d.get(col), scale)
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
    from statsplusplus.utils.positions import assign_bucket
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


def _current_contract(d):
    """Current MLB contract display string + raw annual salary for sorting.

    Returns ("MiLC", None) when SLR is blank/"-" (amateurs, minor leaguers,
    anyone without a signed MLB deal). Otherwise formats using SLR (annual
    salary) and YL (years remaining): a plain integer YL > 1 means a real
    negotiated multi-year deal ("6yr $9.5M/yr"); YL of "1 (auto.)" or
    "1 (arbitr.)" is standard one-year team control, not a multi-year deal,
    so it's shown as a bare per-year figure instead.
    """
    slr_raw = (d.get("SLR") or "").strip()
    if not slr_raw or slr_raw == "-":
        return "MiLC", None
    salary = _num(slr_raw.replace("$", "").replace(" ", ""))
    if not salary:
        return "MiLC", None
    sal_display = f"${salary/1e6:.1f}M" if salary >= 1e6 else f"${salary/1e3:.0f}K"
    yl_raw = (d.get("YL") or "").strip()
    yl_num = _num(yl_raw.split()[0]) if yl_raw else None
    if yl_num and yl_num > 1:
        return f"{yl_num}yr {sal_display}/yr", salary
    return f"{sal_display}/yr", salary


def _hitter_side_tools(d, scale, tools, side):
    """Swap in the vL/vR split rating for whichever tools have one (contact,
    power, gap, eye — speed/steal don't vary by pitcher handedness), falling
    back to the unsplit value when this export doesn't carry that column."""
    suffix = " vL" if side == "L" else " vR"
    out = dict(tools)
    for key, col in (("contact", "CON"), ("power", "POW"), ("gap", "GAP"), ("eye", "EYE")):
        v = _num_scaled(d.get(f"{col}{suffix}"), scale)
        if v is not None:
            out[key] = v
    return out


def _pitcher_side_tools(d, scale, tools, side):
    """Swap in the vL/vR split rating for stuff/movement/control, falling
    back to the unsplit value when this export doesn't carry that column.
    Control's split columns follow the same dual-convention duplicate-name
    issue as the base control column (see _dup())."""
    suffix = " vL" if side == "L" else " vR"
    out = dict(tools)
    v_stu = _num_scaled(d.get(f"STU{suffix}"), scale)
    if v_stu is not None:
        out["stuff"] = v_stu
    v_mov = _num_scaled(d.get(f"MOV{suffix}"), scale)
    if v_mov is not None:
        out["movement"] = v_mov
    v_con = _num_scaled(_dup(d, f"CON{suffix}"), scale)
    if v_con is not None:
        out["control"] = v_con
    return out


def evaluate_row(d: dict, league_dir=None) -> dict | None:
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
    org_abbr = (_dup(d, "ORG") or "").strip()
    # First "B" occurrence is the full word ("Right"/"Left"/"Switch") in
    # every export checked so far — sliced to a letter rather than reading
    # the abbreviated duplicate, so this doesn't depend on which
    # duplicate-column convention (see _dup()) this particular export uses.
    bats = (d.get("B") or "").strip()[:1].upper()
    age = _num(d.get("Age"))
    role_str = (d.get("RL") or "").strip().upper()
    is_pitcher = role_str in ("SP", "RP", "CL")
    # Resolve this league's actual ratings display scale — Stuff/Movement/
    # Control/Stamina and every other tool rating below need converting to
    # the 20-80 canonical scale compute_composite_*/compute_ceiling expect;
    # some leagues (e.g. emlb) export these on 1-100 even though Ovr/Pot
    # stay on 20-80, so this can't be hardcoded.
    scale = "1-100"
    tool_weights = DEFAULT_TOOL_WEIGHTS
    park = None
    if league_dir is not None:
        try:
            from statsplusplus.config.league_config import LeagueConfig
            scale = LeagueConfig(base_dir=league_dir).ratings_scale
        except Exception:
            pass
        try:
            tool_weights = load_tool_weights(league_dir)
        except Exception:
            pass
        try:
            park = load_park_factors(league_dir)
        except Exception:
            pass
    stamina = _num_scaled(d.get("STM"), scale) or 50
    acc = _ACC_MAP.get((d.get("SctAcc") or "").strip(), "A")
    wrk_ethic = (d.get("WE") or "N").strip()
    intel = (d.get("INT") or "N").strip()
    personality_type = (d.get("Type") or "").strip()
    if personality_type in _PERSONALITY_TYPE_POSITIVE:
        personality_class = "pos"
    elif personality_type in _PERSONALITY_TYPE_NEGATIVE:
        personality_class = "neg"
    else:
        personality_class = "neutral"
    level_abbr = (_dup(d, "Lev") or "MLB").strip().upper()
    level_key = _LEVEL_KEY_BY_ABBR.get(level_abbr, "mlb")

    bucket = _bucket_for_assign(d, is_pitcher, role_str, stamina)

    if is_pitcher:
        role = "RP" if role_str in ("RP", "CL") else "SP"
        weights = tool_weights.get("pitcher", DEFAULT_TOOL_WEIGHTS["pitcher"])[role]
        tools = _pitcher_tools(d, scale)
        pot_tools = _pitcher_potential_tools(d, scale)
        arsenal = _arsenal(d, scale)
        composite = compute_composite_pitcher(tools, weights, arsenal, stamina, role)
        ceiling = compute_ceiling(
            pot_tools, weights, composite, accuracy=acc, work_ethic=wrk_ethic,
            is_pitcher=True, arsenal=arsenal, stamina=stamina, role=role, age=age or 25,
            ratings_scale=scale,
        )
        true_ceiling = compute_true_ceiling(
            pot_tools, weights, composite, accuracy=acc, work_ethic=wrk_ethic,
            is_pitcher=True, arsenal=arsenal, stamina=stamina, role=role,
        )
        offensive_ceiling = None
        stf_l, stf_r = tools.get("stuff_l"), tools.get("stuff_r")
        composite_vs_l = compute_composite_pitcher(
            _pitcher_side_tools(d, scale, tools, "L"), weights, arsenal, stamina, role)
        composite_vs_r = compute_composite_pitcher(
            _pitcher_side_tools(d, scale, tools, "R"), weights, arsenal, stamina, role)
        # No game logs in an uploaded roster CSV — always the scouting-tool
        # proxy (Movement/Control) here, never real observed GB%/K%/BB%.
        park_fit = compute_pitcher_park_fit_from_tools(tools, park) if park else None
    else:
        hitter_weights = tool_weights.get("hitter", DEFAULT_TOOL_WEIGHTS["hitter"])
        weights = hitter_weights.get(bucket, hitter_weights.get("COF", {}))
        tools = _hitter_tools(d, scale)
        pot_tools = _hitter_potential_tools(d, scale)
        defense, def_weights = _defense_for_bucket(d, bucket, scale)
        pot_defense = defense  # no granular defensive *potential* fields exist in this export either
        composite = compute_composite_hitter(tools, weights, defense, def_weights)
        ceiling = compute_ceiling(
            pot_tools, weights, composite, accuracy=acc, work_ethic=wrk_ethic,
            defense=pot_defense, def_weights=def_weights, age=age or 25,
            ratings_scale=scale,
        )
        true_ceiling = compute_true_ceiling(
            pot_tools, weights, composite, accuracy=acc, work_ethic=wrk_ethic,
            defense=pot_defense, def_weights=def_weights,
        )
        offensive_ceiling = ceiling
        stf_l = stf_r = None
        composite_vs_l = compute_composite_hitter(
            _hitter_side_tools(d, scale, tools, "L"), weights, defense, def_weights)
        composite_vs_r = compute_composite_hitter(
            _hitter_side_tools(d, scale, tools, "R"), weights, defense, def_weights)
        park_fit = compute_batter_park_fit(tools, bats, weights, park) if park else None

    spec_score = compute_specialist_score(tools, is_pitcher)
    spec_label = specialist_label(spec_score)

    role = "RP" if role_str in ("RP", "CL") else ("SP" if is_pitcher else bucket)
    norm_age = LEVEL_NORM_AGE.get(level_key, 25)
    fv_input = {
        "Ovr": composite, "Pot": true_ceiling, "Age": age or norm_age,
        "_is_pitcher": is_pitcher, "_bucket": role, "_norm_age": norm_age,
        "Acc": acc, "WrkEthic": wrk_ethic, "Int": intel,
        "Stf_L": stf_l, "Stf_R": stf_r,
        "_offensive_ceiling": offensive_ceiling,
    }
    fv_grade, risk = calc_fv(fv_input, scale="20-80")

    try:
        surplus = prospect_surplus_with_option(
            fv_grade, age or norm_age, level_abbr, role,
            ovr=composite, pot=true_ceiling, offensive_ceiling=offensive_ceiling,
            league_dir=league_dir,
        )
    except Exception:
        surplus = None
    try:
        peak = peak_year_surplus(
            fv_grade, age or norm_age, level_abbr, role,
            ovr=composite, pot=true_ceiling, league_dir=league_dir,
        )
        peak_surplus, peak_age = peak["surplus"], peak["age"]
    except Exception:
        peak_surplus, peak_age = None, None

    rule5_eligible = (d.get("R5") or "").strip().lower() == "yes"
    # Annual salary demand ("$9.0m" etc.) — verified against a real free
    # agent export: DEM is set for ~22% of free agents (the rest show "-",
    # seemingly players who haven't been actively shopped/negotiated with
    # yet, no clean correlation with scouting accuracy). No MLB/MiLB
    # contract-preference field exists anywhere in this export.
    _dem = (d.get("DEM") or "").strip()
    ask = _dem if _dem and _dem != "-" else None
    # Current contract — verified against a real draft-pool export (1,164
    # rows): SLR is the current annual salary ("$9 500 000"), "-" for the
    # ~99% of players (amateurs/minor leaguers) with no MLB deal. YL is
    # years remaining — a plain integer for a real multi-year contract, or
    # "1 (auto.)"/"1 (arbitr.)" for standard one-year team-control years
    # (not a negotiated multi-year deal, so not called out as one below).
    contract, contract_salary = _current_contract(d)
    if_rng = _num(d.get("IF RNG"))
    of_rng = _num(d.get("OF RNG"))
    best_position, best_position_grade = (None, None) if is_pitcher else _best_position(d)

    # Verified against a real "All Free Agents" export (1,926 rows, zero
    # exceptions): a free agent's "TM" field holds their *former* team name
    # (e.g. "Cleveland Indians"), not literal "Free Agent" text — ORG="-"
    # combined with RET="No" is the actual signal (ORG="-" alone also
    # matches retired legacy players, hence the RET check).
    # "on waivers" is still an unverified guess (no real waiver-wire export
    # seen yet): inferred from the Claim/WAIV/DFA columns being populated
    # (non "-") rather than blank.
    is_free_agent = org_name == "-" and (d.get("RET") or "").strip().lower() == "no"
    on_waivers = any((d.get(c) or "-").strip() != "-" for c in ("Claim", "WAIV", "DFA"))
    platoon_gap, platoon_strong_side = _platoon_split(d, is_pitcher)

    return {
        "pid": pid, "name": name, "age": age,
        "level": level_abbr, "bucket": role if is_pitcher else bucket,
        "is_pitcher": is_pitcher,
        "composite_score": composite, "ceiling_score": ceiling,
        "true_ceiling": true_ceiling, "fv": fv_grade, "risk": risk,
        "composite_vs_l": composite_vs_l, "composite_vs_r": composite_vs_r,
        "specialist_score": spec_score, "specialist_label": spec_label,
        "park_fit": park_fit,
        "acc": acc, "org": org_name, "org_abbr": org_abbr,
        "rule5_eligible": rule5_eligible, "ask": ask,
        "contract": contract, "contract_salary": contract_salary,
        "surplus": surplus, "peak_surplus": peak_surplus, "peak_age": peak_age,
        "if_rng": if_rng, "of_rng": of_rng,
        "best_position": best_position, "best_position_grade": best_position_grade,
        "is_free_agent": is_free_agent, "on_waivers": on_waivers,
        "platoon_gap": platoon_gap, "platoon_strong_side": platoon_strong_side,
        "personality_type": personality_type, "personality_class": personality_class,
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


def import_fa_asking_prices(file_bytes: bytes, league_dir=None) -> int:
    """Import salary demands from an uploaded OOTP "All Free Agents" export
    (the DEM column, e.g. "$9.0m") into fa_asking_prices, keyed by player_id.

    The live statsplus.net sync has no asking-price field at all, so this
    manual-upload table is the only way the app can show a free agent's
    current ask. Returns the number of rows with a real (non "-") demand.
    """
    import datetime
    rows = parse_rows(file_bytes)
    conn = get_conn(league_dir)
    now = datetime.datetime.now().isoformat()
    count = 0
    for d in rows:
        pid = (d.get("ID") or "").strip()
        if not pid:
            continue
        dem = (d.get("DEM") or "").strip()
        if not dem or dem == "-":
            continue
        conn.execute(
            "INSERT INTO fa_asking_prices (player_id, ask_raw, uploaded_at) VALUES (?, ?, ?) "
            "ON CONFLICT(player_id) DO UPDATE SET ask_raw=excluded.ask_raw, uploaded_at=excluded.uploaded_at",
            (int(pid), dem, now),
        )
        count += 1
    conn.commit()
    conn.close()
    return count


def evaluate_csv(file_bytes: bytes, league_dir=None) -> list[dict]:
    rows = parse_rows(file_bytes)
    parsed = []
    for d in rows:
        try:
            r = evaluate_row(d, league_dir=league_dir)
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
