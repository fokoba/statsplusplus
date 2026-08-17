"""League-wide scouting-target queries.

A scout can only produce so many reports each sim (40-70 players), so the
question this page answers is: of everyone still worth investigating,
who's the best use of that allocation? "Worth investigating" specifically
means good players whose accuracy is still Average/Low/Very Low (or
altogether unscouted) — a High/Very High report is already reliable, so
spending a scouting slot there is wasted allocation. Best Available flips
the same lists to the opposite confidence band: players already reliable
enough to act on today.

Not team-scoped by default: which players are worth scouting is a
league-wide question, independent of any one roster's needs. The optional
roster_view ("mlb" or "org") adds that team's own qualifying players
alongside each box for comparison — always shown in full (never subject
to the top-10 cap, the top-25% quality gate, or the accuracy split), since
the point is "how do my own players stack up," not "are they good enough
to be a fresh target."
"""

import os, sys
import datetime

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "scripts"))
from statsplusplus.utils.positions import GAME_POS_MAP, ROLE_MAP
from statsplusplus.config.ratings import norm_continuous as _normc
from statsplusplus.evaluation.park_fit import (
    load_park_factors, compute_batter_park_fit, compute_batter_park_value_pct,
    compute_pitcher_park_fit_from_stats, compute_pitcher_park_fit_from_tools,
    compute_pitcher_park_value_pct_from_stats, compute_pitcher_park_value_pct_from_tools,
)
from statsplusplus.evaluation.composite import compute_composite_hitter, compute_composite_pitcher
from statsplusplus.evaluation.constants import DEFENSIVE_WEIGHTS
from statsplusplus.data.evaluation_engine import load_tool_weights
from statsplusplus.data.db import get_conn
from web_league_context import get_db, get_cfg, money_divisor as _money_divisor, level_map

from team_queries import _NIPPON_TEAM_IDS, _INTL_FA_AGE_MAX, _weak_positions_for_org, _personality_fields

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

# Quality gate: only consider players in the top 25% by Ovr AT THAT
# SPECIFIC POSITION, so a thin position (e.g. catcher) is judged against
# its own realistic pool, not squeezed out by a league-wide cutoff.
_QUALITY_TOP_PCT = 0.25

# Rule 5 Youth specifically uses a looser bar than every other quality-gated
# section: the whole point there is catching a 55-60 Pot player who's only
# a ~40 Ovr today (not yet realized, by definition), so a top-25% Ovr cutoff
# was excluding exactly the profile the section exists to surface.
_RULE5_YOUTH_TOP_PCT = 0.50
_TOP_N = 10

# A player counts as a genuine park-fit + surplus "both good" pick when
# both clear these bars — not a precise science, just enough to flag the
# highest-confidence adds without hiding everyone else.
_GOOD_PARK_FIT = 20
_GOOD_SURPLUS = 0

ORDER = list(_HITTER_POS_CODES.values()) + ["SP", "RP"]

# Shared column list — same shape for both the free-agent pool and the
# roster-comparison pool, so a single row-processing function can build a
# player entry from either source.
_ROW_COLUMNS_SQL = """
    p.player_id, p.name, p.age, p.pos, p.role, p.level, r.bats, r.acc,
    r.composite_score, r.ceiling_score, r.true_ceiling,
    r.cntct, r.gap, r.pow, r.eye, r.stf, r.mov, r.ctrl,
    r.cntct_r, r.gap_r, r.pow_r, r.eye_r, r.stf_r, r.mov_r, r.ctrl_r,
    r.cntct_l, r.gap_l, r.pow_l, r.eye_l, r.stf_l, r.mov_l, r.ctrl_l,
    r.c, r.first_b, r.second_b, r.third_b, r.ss, r.lf, r.cf, r.rf,
    r.c_frm, r.c_blk, r.c_arm, r.ifr, r.ife, r.ifa, r.tdp, r.ofr, r.ofe, r.ofa,
    r.fst, r.snk, r.crv, r.sld, r.chg, r.splt, r.cutt, r.cir_chg, r.scr, r.frk, r.kncrv, r.knbl,
    r.stm, ps.surplus, pf.prospect_surplus,
    r.int_, r.wrk_ethic, r.lead, r.loy, r.greed, fap.ask_raw,
    r.speed, r.steal, r.adaptability, r.personality_type
"""


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


# Roster-comparison scope: "mlb" = this org's active MLB roster only;
# "org" = the whole organization, majors and minors (same parent_team_id
# affiliate structure the Contracts/Cut-Add tables already use).
_MLB_ROSTER_WHERE = "p.team_id = ? AND p.level = '1'"
_WHOLE_ORG_WHERE = "(p.parent_team_id = ? OR (p.parent_team_id = 0 AND p.team_id = ?))"


def _org_where(team_id, scope):
    if scope == "mlb":
        return _MLB_ROSTER_WHERE, (team_id,)
    return _WHOLE_ORG_WHERE, (team_id, team_id)


def _fetch_rows(conn, where, params, eval_date, ed_surplus):
    return conn.execute(f"""
        SELECT {_ROW_COLUMNS_SQL}
        FROM players p
        LEFT JOIN latest_ratings r ON p.player_id = r.player_id
        LEFT JOIN player_surplus ps ON ps.player_id = p.player_id AND ps.eval_date = ?
        LEFT JOIN prospect_fv pf ON pf.player_id = p.player_id AND pf.eval_date = ?
        LEFT JOIN fa_asking_prices fap ON fap.player_id = p.player_id
        WHERE {where} AND r.composite_score IS NOT NULL
    """, (ed_surplus, eval_date, *params)).fetchall()


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


def _build_entries(rows, ratings_scale, park, hitter_weights, pitcher_weights, newly_confirmed, is_mine, is_theirs=False):
    """Turn raw _ROW_COLUMNS_SQL rows into player entry dicts — the exact
    same computation (tools, park fit, defensive rating, vR/vL composite,
    surplus) regardless of whether the source is the free-agent pool or a
    roster-comparison query, so "mine" and "target" entries are always
    directly comparable.
    """
    out = []
    for r in rows:
        (pid, name, age, pos, role, level, bats, acc, comp, ceil_score, true_ceil,
         cntct, gap, pow_, eye, stf, mov, ctrl,
         cntct_r, gap_r, pow_r, eye_r, stf_r, mov_r, ctrl_r,
         cntct_l, gap_l, pow_l, eye_l, stf_l, mov_l, ctrl_l,
         c_def, first_b, second_b, third_b, ss_def, lf, cf, rf,
         c_frm, c_blk, c_arm, ifr, ife, ifa, tdp, ofr, ofe, ofa,
         fst, snk, crv, sld, chg, splt, cutt, cir_chg, scr, frk, kncrv, knbl,
         stm, surplus_raw, prospect_surplus_raw,
         intel, wrk_ethic, lead, loy, greed, ask_raw,
         speed_raw, steal_raw, adaptability, ptype) = r
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
            park_value_pct = compute_pitcher_park_value_pct_from_tools(_tools, park) if park else None
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
            park_value_pct = compute_batter_park_value_pct(_tools, bats, weights, park) if park else None
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
        level_disp = level_map().get(str(level), str(level)) if level is not None else None
        _pers = _personality_fields(intel, wrk_ethic, lead, loy, greed, adaptability, ptype)

        out.append({
            "pid": pid, "name": name, "age": age, "group": group, "is_pitcher": is_pitcher,
            "composite_score": comp, "potential": potential, "acc": acc,
            "park_fit": park_fit, "park_value_pct": park_value_pct, "def_rating": def_rating,
            "vr_score": vr_score, "vl_score": vl_score,
            # Split tool dicts + speed/steal, hitters only — not used by any
            # existing composite/park-fit math above, but the Lineup
            # Optimizer's batting-order builder needs the raw split tools
            # directly (its ordering formula isn't the same weighting as
            # compute_composite_hitter) rather than just the final score.
            "vr_tools": vr_tools if not is_pitcher else None,
            "vl_tools": vl_tools if not is_pitcher else None,
            "speed": n(speed_raw) if not is_pitcher else None,
            "steal": n(steal_raw) if not is_pitcher else None,
            "surplus": surplus, **_pers,
            # No asking price on file means the last uploaded "All Free
            # Agents" export reported a blank/"-" demand for this player
            # (or the export simply hasn't been uploaded yet, which reads
            # the same way) — same "MiLC" convention already used for the
            # Ask column on Add Candidates.
            "is_milc": ask_raw is None,
            "good_pick": bool(park_fit is not None and park_fit >= _GOOD_PARK_FIT
                               and surplus is not None and surplus > _GOOD_SURPLUS),
            "newly_confirmed": pid in newly_confirmed,
            "is_mine": is_mine,
            "is_theirs": is_theirs,
            "roster_tag": level_disp if (is_mine or is_theirs) else None,
        })
    return out


def get_scouting_targets(high_confidence=False, team_id=None, roster_view=None):
    """Same lists either way — high_confidence=False (default, powers
    /scouting) restricts to Average/Low/Very Low/unscouted, the players
    worth spending a scouting report on. high_confidence=True (powers
    /best-available) restricts to High/Very High instead: players you
    already know enough about to act on immediately.

    team_id, if given, attaches a per-position "weak" flag so the page can
    highlight (or filter to) positions that specific org is thin at.

    roster_view, if "mlb" or "org", comingles that team's own qualifying
    players into every ranked list for direct comparison — merged and
    re-sorted by the same metric as the section (so e.g. a 54 Ovr player
    of yours shows up ahead of a 50 Ovr free agent target, right where it
    visually demonstrates whether there's an upgrade available), but never
    subject to the quality gate or accuracy split, and never able to push
    an original top-10 target off the list entirely — only reorders and
    extends it.
    """
    conn = get_db()
    park = load_park_factors(get_cfg().league_dir)
    ratings_scale = get_cfg().ratings_scale
    all_weights = load_tool_weights(get_cfg().league_dir)
    hitter_weights = all_weights.get("hitter", {})
    pitcher_weights = all_weights.get("pitcher", {})

    ed = conn.execute("SELECT MAX(eval_date) FROM prospect_fv").fetchone()[0]
    ed_surplus = conn.execute("SELECT MAX(eval_date) FROM player_surplus").fetchone()[0]

    fa_where, fa_params = _signable_where()
    fa_rows = _fetch_rows(conn, fa_where, fa_params, ed, ed_surplus)
    newly_confirmed = _newly_confirmed_pids(conn) if high_confidence else set()
    players = _build_entries(fa_rows, ratings_scale, park, hitter_weights, pitcher_weights,
                              newly_confirmed, is_mine=False)

    mine_players = []
    if roster_view in ("mlb", "org") and team_id:
        org_where, org_params = _org_where(team_id, roster_view)
        org_rows = _fetch_rows(conn, org_where, org_params, ed, ed_surplus)
        mine_players = _build_entries(org_rows, ratings_scale, park, hitter_weights, pitcher_weights,
                                       set(), is_mine=True)

    by_group = {}
    for p in players:
        by_group.setdefault(p["group"], []).append(p)
    mine_by_group = {}
    for p in mine_players:
        mine_by_group.setdefault(p["group"], []).append(p)

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

    def _gated_pool(group):
        group_players = by_group.get(group, [])
        cutoff = _quality_cutoff(group_players)
        if cutoff is None:
            return []
        return [p for p in group_players if _needs_scout(p) and (p["composite_score"] or -999) >= cutoff]

    def _comingle(target_pool, group, key):
        """Merge this org's qualifying players (no accuracy split, no
        quality gate, no cap) into the ranked target list and re-sort the
        whole thing together by the same metric — every original target
        stays visible, just reordered, with mine interleaved at its true
        rank. No-op (returns target_pool unchanged) when roster_view is
        off, since mine_by_group is empty in that case anyway.
        """
        mine_pool = [p for p in mine_by_group.get(group, []) if p[key] is not None]
        if not mine_pool:
            return target_pool
        merged = target_pool + mine_pool
        merged.sort(key=lambda p: -(p[key] if p[key] is not None else -999))
        return merged

    # ── Best Free Agents Available: top 10 per position by Ovr (no quality gate — it IS the quality ranking) ──
    best_fa = {}
    for group in ORDER:
        pool = [p for p in by_group.get(group, []) if _needs_scout(p) and p["composite_score"] is not None]
        pool.sort(key=lambda p: -p["composite_score"])
        best_fa[group] = _comingle(pool[:_TOP_N], group, "composite_score")

    # ── Best Park Fits: top 10 per position by Park Fit, gated to top-25%-Ovr-at-position ──
    best_park = {}
    if park:
        for group in ORDER:
            pool = [p for p in _gated_pool(group) if p["park_fit"] is not None]
            pool.sort(key=lambda p: -p["park_fit"])
            best_park[group] = _comingle(pool[:_TOP_N], group, "park_fit")

    # ── Best Defenders: hitters only, top 10 per position by def rating, same quality gate ──
    best_def = {}
    for group in _HITTER_POS_CODES.values():
        pool = [p for p in _gated_pool(group) if p["def_rating"] is not None]
        pool.sort(key=lambda p: -p["def_rating"])
        best_def[group] = _comingle(pool[:_TOP_N], group, "def_rating")

    # ── Best Youth: age <= 24, top 10 per position by Potential ──
    best_youth = {}
    for group in ORDER:
        pool = [p for p in by_group.get(group, [])
                if _needs_scout(p) and p["age"] is not None and p["age"] <= 24 and p["potential"] is not None]
        pool.sort(key=lambda p: -p["potential"])
        top = pool[:_TOP_N]
        mine_pool = [p for p in mine_by_group.get(group, [])
                     if p["age"] is not None and p["age"] <= 24 and p["potential"] is not None]
        if mine_pool:
            top = top + mine_pool
            top.sort(key=lambda p: -(p["potential"] if p["potential"] is not None else -999))
        best_youth[group] = top

    # ── Best vR / Best vL: top 10 per position by the split-tool composite, same quality gate ──
    best_vr, best_vl = {}, {}
    for group in ORDER:
        gated = _gated_pool(group)
        vr_pool = [p for p in gated if p["vr_score"] is not None]
        vr_pool.sort(key=lambda p: -p["vr_score"])
        best_vr[group] = _comingle(vr_pool[:_TOP_N], group, "vr_score")
        vl_pool = [p for p in gated if p["vl_score"] is not None]
        vl_pool.sort(key=lambda p: -p["vl_score"])
        best_vl[group] = _comingle(vl_pool[:_TOP_N], group, "vl_score")

    # ── Best Rule 5 Eligible / Rule 5 Youth: separate pool entirely (other
    # orgs' minor leaguers, not free agents) — see rule5_eligible table.
    # Empty until an export has been uploaded. "Mine" here means something
    # different (your OWN exposed players needing protection, not a
    # comparison target) but comingles the same way — sorted in together
    # so you can see at a glance where your exposed players rank against
    # the broader eligible crop. Unlike the FA sections above, this merge
    # is NOT gated behind the "Compare against" toggle — knowing which of
    # your own prospects need 40-man protection isn't optional context,
    # it's the entire point of this section. Always scoped to the whole
    # org (not just the MLB roster), since Rule 5 exposure is inherently
    # about minor leaguers.
    best_rule5 = _rule5_targets(conn, high_confidence)
    best_rule5_youth = _rule5_youth_targets(conn, high_confidence)
    rule5_mine = _rule5_mine(conn, team_id, "org") if team_id else {}
    for group, gp in rule5_mine.items():
        # _rule5_targets()'s general pool doesn't exclude this org, so a
        # player of yours who also clears its quality/accuracy gate would
        # otherwise appear twice — once from the general pool, once from
        # this "mine" merge. rule5_mine is the complete, authoritative
        # list of your own exposed players regardless of that gate, so
        # drop any pid it already covers from the general side first.
        mine_pids = {p["pid"] for p in gp}
        general = [p for p in best_rule5.get(group, []) if p["pid"] not in mine_pids]
        merged = general + gp
        merged.sort(key=lambda p: -(p["composite_score"] if p["composite_score"] is not None else -999))
        best_rule5[group] = merged
    for group, gp in rule5_mine.items():
        young = [p for p in gp if p["age"] is not None and p["age"] <= 24]
        if not young:
            continue
        young_pids = {p["pid"] for p in young}
        general = [p for p in best_rule5_youth.get(group, []) if p["pid"] not in young_pids]
        merged = general + young
        merged.sort(key=lambda p: -(p["potential"] if p["potential"] is not None else -999))
        best_rule5_youth[group] = merged

    weak_positions = _weak_positions_for_org(team_id) if team_id else set()
    asks_uploaded = conn.execute("SELECT COUNT(*) FROM fa_asking_prices").fetchone()[0] > 0

    return {
        "best_fa": best_fa, "best_park": best_park, "best_def": best_def, "best_youth": best_youth,
        "best_vr": best_vr, "best_vl": best_vl,
        "best_rule5": best_rule5, "best_rule5_youth": best_rule5_youth,
        "order": ORDER, "hitter_order": list(_HITTER_POS_CODES.values()),
        "park_configured": bool(park), "weak_positions": weak_positions,
        "rule5_uploaded": _rule5_has_data(conn),
        "roster_view": roster_view,
        "asks_uploaded": asks_uploaded,
    }


def _rule5_has_data(conn):
    try:
        return conn.execute("SELECT COUNT(*) FROM rule5_eligible").fetchone()[0] > 0
    except Exception:
        return False


def _rule5_all_players(conn):
    """All Rule 5-eligible players with ratings, grouped by position — the
    shared base pool for every Rule 5 section (main, youth). Each caller
    computes its own quality cutoff at whatever percentile it needs via
    _position_cutoffs(), since the main and youth sections intentionally
    use different bars.
    """
    if not _rule5_has_data(conn):
        return {}
    try:
        rows = conn.execute("""
            SELECT p.player_id, p.name, p.age, p.pos, p.role, r.acc,
                   r.composite_score, r.ceiling_score, r.true_ceiling,
                   r.int_, r.wrk_ethic, r.lead, r.loy, r.greed, r.adaptability, r.personality_type
            FROM rule5_eligible re
            JOIN players p ON p.player_id = re.player_id
            LEFT JOIN latest_ratings r ON p.player_id = r.player_id
            WHERE r.composite_score IS NOT NULL
        """).fetchall()
    except Exception:
        return {}

    players = []
    for (pid, name, age, pos, role, acc, comp, ceil_score, true_ceil,
         intel, wrk_ethic, lead, loy, greed, adaptability, ptype) in rows:
        group, is_pitcher = _pos_group(pos, role)
        if group is None:
            continue
        potential = true_ceil if true_ceil is not None else ceil_score
        _pers = _personality_fields(intel, wrk_ethic, lead, loy, greed, adaptability, ptype)
        players.append({"pid": pid, "name": name, "age": age, "group": group,
                         "composite_score": comp, "potential": potential, "acc": acc,
                         "surplus": None, "good_pick": False, "newly_confirmed": False,
                         "is_mine": False, "roster_tag": None,
                         **_pers})

    by_group = {}
    for p in players:
        by_group.setdefault(p["group"], []).append(p)
    return by_group


def _position_cutoffs(by_group, top_pct):
    cutoffs = {}
    for group, gp in by_group.items():
        ovrs = sorted((p["composite_score"] for p in gp if p["composite_score"] is not None), reverse=True)
        if ovrs:
            n_ = max(1, round(len(ovrs) * top_pct))
            cutoffs[group] = ovrs[n_ - 1]
    return cutoffs


def _rule5_targets(conn, high_confidence):
    """Rule 5-eligible players worth a look — same quality gate and
    accuracy split as everything else, scoped to this separate pool."""
    by_group = _rule5_all_players(conn)
    if not by_group:
        return {}
    cutoffs = _position_cutoffs(by_group, _QUALITY_TOP_PCT)

    def _needs_scout(p):
        if high_confidence:
            return p["acc"] in _HIGH_CONF_ACC
        return p["acc"] is None or p["acc"] in _NEEDS_SCOUT_ACC

    out = {}
    for group in ORDER:
        cutoff = cutoffs.get(group)
        gp = by_group.get(group, [])
        if cutoff is None:
            out[group] = []
            continue
        pool = [p for p in gp if _needs_scout(p) and (p["composite_score"] or -999) >= cutoff]
        pool.sort(key=lambda p: -(p["composite_score"] or 0))
        out[group] = pool[:_TOP_N]
    return out


def _rule5_youth_targets(conn, high_confidence):
    """The real value in Rule 5 is near-MLB-ready upside: age 24 or under,
    sorted by Potential instead of Ovr, gated to a looser top-50%-Ovr-at-
    position cutoff (vs. the main section's top 25%) — a 55-60 Pot player
    is often still only a ~40 Ovr today by definition (that's the whole
    gap this section exists to surface), so the tighter bar was excluding
    exactly the profile Rule 5 is worth using on.
    """
    by_group = _rule5_all_players(conn)
    if not by_group:
        return {}
    cutoffs = _position_cutoffs(by_group, _RULE5_YOUTH_TOP_PCT)

    def _needs_scout(p):
        if high_confidence:
            return p["acc"] in _HIGH_CONF_ACC
        return p["acc"] is None or p["acc"] in _NEEDS_SCOUT_ACC

    out = {}
    for group in ORDER:
        cutoff = cutoffs.get(group)
        gp = by_group.get(group, [])
        if cutoff is None:
            out[group] = []
            continue
        pool = [p for p in gp if _needs_scout(p) and p["age"] is not None and p["age"] <= 24
                and p["potential"] is not None and (p["composite_score"] or -999) >= cutoff]
        pool.sort(key=lambda p: -(p["potential"] or 0))
        out[group] = pool[:_TOP_N]
    return out


def _rule5_mine(conn, team_id, scope):
    """Which of THIS org's own players are currently Rule 5-exposed —
    needs protecting (a 40-man add) before the draft, not a target to
    compare against. No accuracy/quality/top-10 gating: every exposed
    player of yours matters, however deep the list runs.
    """
    if not _rule5_has_data(conn):
        return {}
    where, params = _org_where(team_id, scope)
    try:
        rows = conn.execute(f"""
            SELECT p.player_id, p.name, p.age, p.pos, p.role, r.acc,
                   r.composite_score, r.ceiling_score, r.true_ceiling, p.level,
                   r.int_, r.wrk_ethic, r.lead, r.loy, r.greed, r.adaptability, r.personality_type
            FROM rule5_eligible re
            JOIN players p ON p.player_id = re.player_id
            LEFT JOIN latest_ratings r ON p.player_id = r.player_id
            WHERE {where} AND r.composite_score IS NOT NULL
        """, params).fetchall()
    except Exception:
        return {}

    out = {}
    for (pid, name, age, pos, role, acc, comp, ceil_score, true_ceil, level,
         intel, wrk_ethic, lead, loy, greed, adaptability, ptype) in rows:
        group, is_pitcher = _pos_group(pos, role)
        if group is None:
            continue
        potential = true_ceil if true_ceil is not None else ceil_score
        _pers = _personality_fields(intel, wrk_ethic, lead, loy, greed, adaptability, ptype)
        entry = {"pid": pid, "name": name, "age": age, "group": group,
                 "composite_score": comp, "potential": potential, "acc": acc,
                 "surplus": None, "good_pick": False, "newly_confirmed": False,
                 "is_mine": True, "roster_tag": level_map().get(str(level), str(level)),
                 **_pers}
        out.setdefault(group, []).append(entry)
    for group in out:
        out[group].sort(key=lambda p: -(p["composite_score"] or 0))
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


def get_team_compare(my_team_id, my_scope, their_team_id, their_scope):
    """Roster-vs-roster comparison: my team/org vs one other team/org,
    comingled into the same ranked lists per position — no accuracy split
    (this isn't about scouting allocation, both sides are fully known
    rosters) and no top-25% quality gate (neither side is a free-agent
    pool that needs filtering down to realistic MLB talent — every
    rostered player from both sides is inherently relevant to the
    comparison). Park Fit is always computed against MY park, regardless
    of which side a player is on, since the question is "how would this
    player perform in my park," not two different home parks at once.
    """
    conn = get_db()
    park = load_park_factors(get_cfg().league_dir)
    ratings_scale = get_cfg().ratings_scale
    all_weights = load_tool_weights(get_cfg().league_dir)
    hitter_weights = all_weights.get("hitter", {})
    pitcher_weights = all_weights.get("pitcher", {})

    ed = conn.execute("SELECT MAX(eval_date) FROM prospect_fv").fetchone()[0]
    ed_surplus = conn.execute("SELECT MAX(eval_date) FROM player_surplus").fetchone()[0]

    my_where, my_params = _org_where(my_team_id, my_scope)
    my_rows = _fetch_rows(conn, my_where, my_params, ed, ed_surplus)
    mine = _build_entries(my_rows, ratings_scale, park, hitter_weights, pitcher_weights,
                           set(), is_mine=True)

    their_where, their_params = _org_where(their_team_id, their_scope)
    their_rows = _fetch_rows(conn, their_where, their_params, ed, ed_surplus)
    theirs = _build_entries(their_rows, ratings_scale, park, hitter_weights, pitcher_weights,
                             set(), is_mine=False, is_theirs=True)

    by_group = {}
    for p in mine + theirs:
        by_group.setdefault(p["group"], []).append(p)

    def _ranked(key):
        out = {}
        for group in ORDER:
            pool = [p for p in by_group.get(group, []) if p[key] is not None]
            pool.sort(key=lambda p: -p[key])
            out[group] = pool
        return out

    def _ranked_def():
        out = {}
        for group in _HITTER_POS_CODES.values():
            pool = [p for p in by_group.get(group, []) if p["def_rating"] is not None]
            pool.sort(key=lambda p: -p["def_rating"])
            out[group] = pool
        return out

    def _ranked_youth():
        out = {}
        for group in ORDER:
            pool = [p for p in by_group.get(group, [])
                    if p["age"] is not None and p["age"] <= 24 and p["potential"] is not None]
            pool.sort(key=lambda p: -p["potential"])
            out[group] = pool
        return out

    return {
        "cmp_ovr": _ranked("composite_score"),
        "cmp_park": _ranked("park_fit") if park else {},
        "cmp_def": _ranked_def(),
        "cmp_vr": _ranked("vr_score"),
        "cmp_vl": _ranked("vl_score"),
        "cmp_youth": _ranked_youth(),
        "order": ORDER, "hitter_order": list(_HITTER_POS_CODES.values()),
        "park_configured": bool(park),
    }
