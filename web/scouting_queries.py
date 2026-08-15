"""League-wide scouting-target queries.

A scout can only produce so many reports each sim (40-70 players), so the
question this page answers is: of everyone still worth investigating,
who's the best use of that allocation? "Worth investigating" specifically
means good players whose accuracy is still Average/Low/Very Low (or
altogether unscouted) — a High/Very High report is already reliable, so
spending a scouting slot there is wasted allocation. Best Available flips
the same lists to the opposite confidence band: players already reliable
enough to act on today.

Not team-scoped: which players are worth scouting is a league-wide
question, independent of any one roster's needs. The one exception is the
optional team-fit highlight, which needs to know which positions a
specific org is weak at.
"""

import os, sys
import datetime

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "scripts"))
from statsplusplus.utils.positions import GAME_POS_MAP, ROLE_MAP
from statsplusplus.config.ratings import norm_continuous as _normc
from statsplusplus.evaluation.park_fit import (
    load_park_factors, compute_batter_park_fit,
    compute_pitcher_park_fit_from_stats, compute_pitcher_park_fit_from_tools,
)
from statsplusplus.evaluation.composite import compute_composite_hitter, compute_composite_pitcher
from statsplusplus.evaluation.constants import DEFENSIVE_WEIGHTS
from statsplusplus.data.evaluation_engine import load_tool_weights
from statsplusplus.data.db import get_conn
from web_league_context import get_db, get_cfg, money_divisor as _money_divisor

from team_queries import _NIPPON_TEAM_IDS, _INTL_FA_AGE_MAX, _weak_positions_for_org

# Only these accuracy grades are "worth scouting" — High/Very High reports
# are already reliable. NULL (never scouted at all) counts as needing a
# report too, arguably more than "Average" does.
_NEEDS_SCOUT_ACC = ("A", "L", "VL")

# The complement: reports already reliable enough to trust immediately —
# powers the "Best Available" page (same lists, opposite confidence band),
# for players you can act on right now instead of ones worth a scout's time.
_HIGH_CONF_ACC = ("H", "VH")

# Hitter field positions this page tracks individually (raw pos code 1=P,
# 10=DH excluded — DH has no defensive category to bucket into, and
# pitchers are grouped by role instead, not by pos).
_HITTER_POS_CODES = {2: "C", 3: "1B", 4: "2B", 5: "3B", 6: "SS", 7: "LF", 8: "CF", 9: "RF"}

_DEF_COL = {"C": "c", "1B": "first_b", "2B": "second_b", "3B": "third_b",
            "SS": "ss", "LF": "lf", "CF": "cf", "RF": "rf"}

# compute_composite_hitter's defensive-component bucket keys — distinct
# from DEFENSIVE_WEIGHTS' own naming (COF splits into COF_LF/COF_RF; 1B has
# no meaningful defensive skill differentiation in this model, so it's
# left out entirely — matches how real 1B defense barely moves value).
_COMPOSITE_DEF_BUCKET = {"C": "C", "SS": "SS", "2B": "2B", "3B": "3B",
                          "CF": "CF", "LF": "COF_LF", "RF": "COF_RF"}

# Pitch-arsenal field names, matching compute_composite_pitcher's expected
# arsenal dict keys exactly (see scripts/custom_upload.py's _PITCH_COL_MAP
# for the same mapping against a different data source).
_ARSENAL_COLS = {"Fst": "fst", "Snk": "snk", "Crv": "crv", "Sld": "sld", "Chg": "chg",
                  "Splt": "splt", "Cutt": "cutt", "CirChg": "cir_chg", "Scr": "scr",
                  "Frk": "frk", "Kncrv": "kncrv", "Knbl": "knbl"}

# Quality gate: only consider players in the top 25% by Ovr AT THAT
# SPECIFIC POSITION, so a thin position (e.g. catcher) is judged against
# its own realistic pool, not squeezed out by a league-wide cutoff.
_QUALITY_TOP_PCT = 0.25
_TOP_N = 10

# A player counts as a genuine park-fit + surplus "both good" pick when
# both clear these bars — not a precise science, just enough to flag the
# highest-confidence adds without hiding everyone else.
_GOOD_PARK_FIT = 20
_GOOD_SURPLUS = 0


def _signable_where():
    nq = ",".join("?" * len(_NIPPON_TEAM_IDS))
    where = f"""
        p.free_agent = 1 AND p.retired = 0 AND p.team_id = 0
        AND (p.nation_id IS NULL OR p.nation_id != 98)
        AND (p.draft_team_id IS NULL OR p.draft_team_id NOT IN ({nq}))
        AND COALESCE(p.draft_eligible, 0) != 1
        AND (p.age IS NULL OR p.age > ?)
    """
    return where, (*_NIPPON_TEAM_IDS, _INTL_FA_AGE_MAX)


def _fetch_pool(conn, eval_date, ed_surplus):
    """All signable, non-international free agents with everything every
    section below needs — one query, reused by all lists."""
    where, params = _signable_where()
    rows = conn.execute(f"""
        SELECT p.player_id, p.name, p.age, p.pos, p.role, r.bats, r.acc,
               r.composite_score, r.ceiling_score, r.true_ceiling,
               r.cntct, r.gap, r.pow, r.eye, r.stf, r.mov, r.ctrl,
               r.cntct_r, r.gap_r, r.pow_r, r.eye_r, r.stf_r, r.mov_r, r.ctrl_r,
               r.cntct_l, r.gap_l, r.pow_l, r.eye_l, r.stf_l, r.mov_l, r.ctrl_l,
               r.c, r.first_b, r.second_b, r.third_b, r.ss, r.lf, r.cf, r.rf,
               r.c_frm, r.c_blk, r.c_arm, r.ifr, r.ife, r.ifa, r.tdp, r.ofr, r.ofe, r.ofa,
               r.fst, r.snk, r.crv, r.sld, r.chg, r.splt, r.cutt, r.cir_chg, r.scr, r.frk, r.kncrv, r.knbl,
               r.stm, ps.surplus, pf.prospect_surplus
        FROM players p
        LEFT JOIN latest_ratings r ON p.player_id = r.player_id
        LEFT JOIN player_surplus ps ON ps.player_id = p.player_id AND ps.eval_date = ?
        LEFT JOIN prospect_fv pf ON pf.player_id = p.player_id AND pf.eval_date = ?
        WHERE {where} AND r.composite_score IS NOT NULL
    """, (ed_surplus, eval_date, *params)).fetchall()
    return rows


def _pos_group(pos, role):
    """('C'..'RF', is_pitcher=False) for hitters, ('SP'/'RP', True) for
    pitchers, or (None, ...) if this player doesn't fit a tracked group
    (DH-only hitters, or a pitcher role we don't recognize)."""
    if role in ROLE_MAP:
        return ("RP" if ROLE_MAP[role] in ("RP", "CL") else "SP"), True
    return _HITTER_POS_CODES.get(pos), False


def _newly_confirmed_pids(conn):
    """Player IDs whose accuracy is High/Very High as of the latest ratings
    snapshot but was NOT in the prior snapshot — a fresh scouting-report
    confirmation, worth surfacing separately from the rest of Best
    Available since it's new information, not a standing recommendation.
    Empty set if there's no prior snapshot to compare against.
    """
    dates = conn.execute(
        "SELECT DISTINCT snapshot_date FROM ratings ORDER BY snapshot_date DESC LIMIT 2"
    ).fetchall()
    if len(dates) < 2:
        return set()
    latest, prior = dates[0][0], dates[1][0]
    latest_high = {r[0] for r in conn.execute(
        "SELECT player_id FROM ratings WHERE snapshot_date=? AND acc IN ('H','VH')", (latest,))}
    prior_high = {r[0] for r in conn.execute(
        "SELECT player_id FROM ratings WHERE snapshot_date=? AND acc IN ('H','VH')", (prior,))}
    return latest_high - prior_high


def get_scouting_targets(high_confidence=False, team_id=None):
    """Same lists either way — high_confidence=False (default, powers
    /scouting) restricts to Average/Low/Very Low/unscouted, the players
    worth spending a scouting report on. high_confidence=True (powers
    /best-available) restricts to High/Very High instead: players you
    already know enough about to act on immediately.

    team_id, if given, attaches a per-position "weak" flag so the page can
    highlight (or filter to) positions that specific org is thin at.
    """
    conn = get_db()
    park = load_park_factors(get_cfg().league_dir)
    ratings_scale = get_cfg().ratings_scale
    all_weights = load_tool_weights(get_cfg().league_dir)
    hitter_weights = all_weights.get("hitter", {})
    pitcher_weights = all_weights.get("pitcher", {})

    ed = conn.execute("SELECT MAX(eval_date) FROM prospect_fv").fetchone()[0]
    ed_surplus = conn.execute("SELECT MAX(eval_date) FROM player_surplus").fetchone()[0]
    rows = _fetch_pool(conn, ed, ed_surplus)
    newly_confirmed = _newly_confirmed_pids(conn) if high_confidence else set()

    players = []
    for r in rows:
        (pid, name, age, pos, role, bats, acc, comp, ceil_score, true_ceil,
         cntct, gap, pow_, eye, stf, mov, ctrl,
         cntct_r, gap_r, pow_r, eye_r, stf_r, mov_r, ctrl_r,
         cntct_l, gap_l, pow_l, eye_l, stf_l, mov_l, ctrl_l,
         c_def, first_b, second_b, third_b, ss_def, lf, cf, rf,
         c_frm, c_blk, c_arm, ifr, ife, ifa, tdp, ofr, ofe, ofa,
         fst, snk, crv, sld, chg, splt, cutt, cir_chg, scr, frk, kncrv, knbl,
         stm, surplus_raw, prospect_surplus_raw) = r
        group, is_pitcher = _pos_group(pos, role)
        if group is None:
            continue
        potential = true_ceil if true_ceil is not None else ceil_score

        def n(v):
            return _normc(v, ratings_scale)

        if is_pitcher:
            role_key = group  # "SP" or "RP"
            weights = pitcher_weights.get(role_key, {})
            _tools = {"stuff": n(stf), "movement": n(mov), "control": n(ctrl)}
            arsenal = {}
            for pname, raw in (("Fst", fst), ("Snk", snk), ("Crv", crv), ("Sld", sld), ("Chg", chg),
                                ("Splt", splt), ("Cutt", cutt), ("CirChg", cir_chg), ("Scr", scr),
                                ("Frk", frk), ("Kncrv", kncrv), ("Knbl", knbl)):
                v = n(raw)
                if v is not None:
                    arsenal[pname] = v
            stamina = n(stm)
            park_fit = compute_pitcher_park_fit_from_tools(_tools, park) if park else None
            def_rating = None

            vr_tools = dict(_tools)
            if stf_r is not None:
                vr_tools["stuff"] = n(stf_r)
            if mov_r is not None:
                vr_tools["movement"] = n(mov_r)
            if ctrl_r is not None:
                vr_tools["control"] = n(ctrl_r)
            vl_tools = dict(_tools)
            if stf_l is not None:
                vl_tools["stuff"] = n(stf_l)
            if mov_l is not None:
                vl_tools["movement"] = n(mov_l)
            if ctrl_l is not None:
                vl_tools["control"] = n(ctrl_l)
            try:
                vr_score = compute_composite_pitcher(vr_tools, weights, arsenal, stamina, role_key)
                vl_score = compute_composite_pitcher(vl_tools, weights, arsenal, stamina, role_key)
            except Exception:
                vr_score = vl_score = None
        else:
            weights = hitter_weights.get(group, hitter_weights.get("COF", {}))
            _tools = {"contact": n(cntct), "gap": n(gap), "power": n(pow_), "eye": n(eye)}
            park_fit = compute_batter_park_fit(_tools, bats, weights, park) if park else None
            def_raw = {"C": c_def, "1B": first_b, "2B": second_b, "3B": third_b,
                       "SS": ss_def, "LF": lf, "CF": cf, "RF": rf}[group]
            def_rating = n(def_raw)

            def_bucket = _COMPOSITE_DEF_BUCKET.get(group)
            if def_bucket:
                def_weights = DEFENSIVE_WEIGHTS.get(def_bucket, {})
                defense = {"CFrm": n(c_frm), "CBlk": n(c_blk), "CArm": n(c_arm),
                           "IFR": n(ifr), "IFE": n(ife), "IFA": n(ifa), "TDP": n(tdp),
                           "OFR": n(ofr), "OFE": n(ofe), "OFA": n(ofa)}
            else:
                def_weights, defense = {}, {}

            vr_tools = dict(_tools)
            if cntct_r is not None:
                vr_tools["contact"] = n(cntct_r)
            if gap_r is not None:
                vr_tools["gap"] = n(gap_r)
            if pow_r is not None:
                vr_tools["power"] = n(pow_r)
            if eye_r is not None:
                vr_tools["eye"] = n(eye_r)
            vl_tools = dict(_tools)
            if cntct_l is not None:
                vl_tools["contact"] = n(cntct_l)
            if gap_l is not None:
                vl_tools["gap"] = n(gap_l)
            if pow_l is not None:
                vl_tools["power"] = n(pow_l)
            if eye_l is not None:
                vl_tools["eye"] = n(eye_l)
            try:
                vr_score = compute_composite_hitter(vr_tools, weights, defense, def_weights)
                vl_score = compute_composite_hitter(vl_tools, weights, defense, def_weights)
            except Exception:
                vr_score = vl_score = None

        surplus_basis = surplus_raw if surplus_raw is not None else prospect_surplus_raw
        surplus = round(surplus_basis / _money_divisor(), 1) if surplus_basis is not None else None

        players.append({
            "pid": pid, "name": name, "age": age, "group": group, "is_pitcher": is_pitcher,
            "composite_score": comp, "potential": potential, "acc": acc,
            "park_fit": park_fit, "def_rating": def_rating,
            "vr_score": vr_score, "vl_score": vl_score,
            "surplus": surplus,
            "good_pick": bool(park_fit is not None and park_fit >= _GOOD_PARK_FIT
                               and surplus is not None and surplus > _GOOD_SURPLUS),
            "newly_confirmed": pid in newly_confirmed,
        })

    by_group = {}
    for p in players:
        by_group.setdefault(p["group"], []).append(p)

    def _needs_scout(p):
        if high_confidence:
            return p["acc"] in _HIGH_CONF_ACC
        return p["acc"] is None or p["acc"] in _NEEDS_SCOUT_ACC

    def _quality_cutoff(group_players):
        """Ovr value at the 25th-percentile-from-the-top, computed against
        the FULL position pool (any accuracy) — a thin position's bar
        should reflect thin position reality, not a league-wide one."""
        ovrs = sorted((p["composite_score"] for p in group_players if p["composite_score"] is not None), reverse=True)
        if not ovrs:
            return None
        n_ = max(1, round(len(ovrs) * _QUALITY_TOP_PCT))
        return ovrs[n_ - 1]

    ORDER = list(_HITTER_POS_CODES.values()) + ["SP", "RP"]

    def _gated_pool(group):
        group_players = by_group.get(group, [])
        cutoff = _quality_cutoff(group_players)
        if cutoff is None:
            return []
        return [p for p in group_players if _needs_scout(p) and (p["composite_score"] or -999) >= cutoff]

    # ── Best Free Agents Available: top 10 per position by Ovr (no quality gate — it IS the quality ranking) ──
    best_fa = {}
    for group in ORDER:
        pool = [p for p in by_group.get(group, []) if _needs_scout(p) and p["composite_score"] is not None]
        pool.sort(key=lambda p: -p["composite_score"])
        best_fa[group] = pool[:_TOP_N]

    # ── Best Park Fits: top 10 per position by Park Fit, gated to top-25%-Ovr-at-position ──
    best_park = {}
    if park:
        for group in ORDER:
            pool = [p for p in _gated_pool(group) if p["park_fit"] is not None]
            pool.sort(key=lambda p: -p["park_fit"])
            best_park[group] = pool[:_TOP_N]

    # ── Best Defenders: hitters only, top 10 per position by def rating, same quality gate ──
    best_def = {}
    for group in _HITTER_POS_CODES.values():
        pool = [p for p in _gated_pool(group) if p["def_rating"] is not None]
        pool.sort(key=lambda p: -p["def_rating"])
        best_def[group] = pool[:_TOP_N]

    # ── Best Youth: age <= 24, top 10 per position by Potential ──
    best_youth = {}
    for group in ORDER:
        pool = [p for p in by_group.get(group, [])
                if _needs_scout(p) and p["age"] is not None and p["age"] <= 24 and p["potential"] is not None]
        pool.sort(key=lambda p: -p["potential"])
        best_youth[group] = pool[:_TOP_N]

    # ── Best vR / Best vL: top 10 per position by the split-tool composite, same quality gate ──
    best_vr, best_vl = {}, {}
    for group in ORDER:
        gated = _gated_pool(group)
        vr_pool = [p for p in gated if p["vr_score"] is not None]
        vr_pool.sort(key=lambda p: -p["vr_score"])
        best_vr[group] = vr_pool[:_TOP_N]
        vl_pool = [p for p in gated if p["vl_score"] is not None]
        vl_pool.sort(key=lambda p: -p["vl_score"])
        best_vl[group] = vl_pool[:_TOP_N]

    # ── Best Rule 5 Eligible: separate pool entirely (other orgs' minor
    # leaguers, not free agents) — see rule5_eligible table. Empty until an
    # export has been uploaded.
    best_rule5 = _rule5_targets(conn, high_confidence)

    weak_positions = _weak_positions_for_org(team_id) if team_id else set()

    return {
        "best_fa": best_fa, "best_park": best_park, "best_def": best_def, "best_youth": best_youth,
        "best_vr": best_vr, "best_vl": best_vl, "best_rule5": best_rule5,
        "order": ORDER, "hitter_order": list(_HITTER_POS_CODES.values()),
        "park_configured": bool(park), "weak_positions": weak_positions,
        "rule5_uploaded": _rule5_has_data(conn),
    }


def _rule5_has_data(conn):
    try:
        return conn.execute("SELECT COUNT(*) FROM rule5_eligible").fetchone()[0] > 0
    except Exception:
        return False


def _rule5_targets(conn, high_confidence):
    """Rule 5-eligible players (any org's farm system, not just free
    agents) worth a look — same quality gate and accuracy split as
    everything else, but scoped to this much smaller, separate pool.
    """
    if not _rule5_has_data(conn):
        return {}
    try:
        rows = conn.execute("""
            SELECT p.player_id, p.name, p.age, p.pos, p.role, r.acc,
                   r.composite_score, r.ceiling_score, r.true_ceiling
            FROM rule5_eligible re
            JOIN players p ON p.player_id = re.player_id
            LEFT JOIN latest_ratings r ON p.player_id = r.player_id
            WHERE r.composite_score IS NOT NULL
        """).fetchall()
    except Exception:
        return {}

    players = []
    for pid, name, age, pos, role, acc, comp, ceil_score, true_ceil in rows:
        group, is_pitcher = _pos_group(pos, role)
        if group is None:
            continue
        potential = true_ceil if true_ceil is not None else ceil_score
        players.append({"pid": pid, "name": name, "age": age, "group": group,
                         "composite_score": comp, "potential": potential, "acc": acc,
                         "surplus": None, "good_pick": False, "newly_confirmed": False})

    by_group = {}
    for p in players:
        by_group.setdefault(p["group"], []).append(p)

    def _needs_scout(p):
        if high_confidence:
            return p["acc"] in _HIGH_CONF_ACC
        return p["acc"] is None or p["acc"] in _NEEDS_SCOUT_ACC

    ORDER = list(_HITTER_POS_CODES.values()) + ["SP", "RP"]
    out = {}
    for group in ORDER:
        group_players = by_group.get(group, [])
        ovrs = sorted((p["composite_score"] for p in group_players if p["composite_score"] is not None), reverse=True)
        if not ovrs:
            out[group] = []
            continue
        n_ = max(1, round(len(ovrs) * _QUALITY_TOP_PCT))
        cutoff = ovrs[n_ - 1]
        pool = [p for p in group_players if _needs_scout(p) and (p["composite_score"] or -999) >= cutoff]
        pool.sort(key=lambda p: -(p["composite_score"] or 0))
        out[group] = pool[:_TOP_N]
    return out


def import_rule5_eligible(file_bytes, league_dir=None):
    """Import Rule 5 eligibility from a "Player List (All Columns)" export
    that was filtered in-game to "Is Rule 5 Eligible" before exporting —
    every row in a correctly-filtered export IS eligible, so this takes
    the file at its word rather than trying to re-derive eligibility from
    age/draft-year/40-man fields (which, unfiltered, don't reliably encode
    it — real Rule 5 status depends on internal roster-protection history
    the live sync doesn't carry either).

    Full replace on each upload, not a merge: eligibility changes as
    players get added to a 40-man or pass their protection deadline, so a
    stale entry from a prior upload would be actively wrong to keep.
    """
    import datetime
    from custom_upload import parse_rows
    rows = parse_rows(file_bytes)
    pids = []
    for d in rows:
        pid = (d.get("ID") or "").strip()
        if pid:
            pids.append(int(pid))
    if not pids:
        return 0

    conn = get_conn(league_dir)
    now = datetime.datetime.now().isoformat()
    conn.execute("DELETE FROM rule5_eligible")
    conn.executemany(
        "INSERT INTO rule5_eligible (player_id, uploaded_at) VALUES (?, ?)",
        [(pid, now) for pid in pids],
    )
    conn.commit()
    conn.close()
    return len(pids)
