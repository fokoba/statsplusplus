"""League-wide scouting-target queries.

A scout can only produce so many reports each sim (40-70 players), so the
question this page answers is: of everyone still worth investigating,
who's the best use of that allocation? "Worth investigating" specifically
means good players whose accuracy is still Average/Low/Very Low (or
altogether unscouted) — a High/Very High report is already reliable, so
spending a scouting slot there is wasted allocation.

Not team-scoped: which players are worth scouting is a league-wide
question, independent of any one roster's needs.
"""

import os, sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "scripts"))
from statsplusplus.utils.positions import GAME_POS_MAP, ROLE_MAP
from statsplusplus.config.ratings import norm_continuous as _normc
from statsplusplus.evaluation.park_fit import (
    load_park_factors, compute_batter_park_fit,
    compute_pitcher_park_fit_from_stats, compute_pitcher_park_fit_from_tools,
)
from statsplusplus.data.evaluation_engine import load_tool_weights
from web_league_context import get_db, get_cfg

from team_queries import _NIPPON_TEAM_IDS, _INTL_FA_AGE_MAX

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

# Quality gate for Best Park Fits / Best Defenders: only consider players
# in the top 25% by Ovr AT THAT SPECIFIC POSITION, so a thin position (e.g.
# catcher) is judged against its own realistic pool, not squeezed out by
# bat-first outfielders dominating a league-wide cutoff.
_QUALITY_TOP_PCT = 0.25
_TOP_N = 10


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


def _fetch_pool(conn):
    """All signable, non-international free agents with the ratings needed
    for every section below. One query, reused by all four lists."""
    where, params = _signable_where()
    rows = conn.execute(f"""
        SELECT p.player_id, p.name, p.age, p.pos, p.role, r.bats, r.acc,
               r.composite_score, r.ceiling_score, r.true_ceiling,
               r.cntct, r.gap, r.pow, r.eye, r.stf, r.mov, r.ctrl,
               r.c, r.first_b, r.second_b, r.third_b, r.ss, r.lf, r.cf, r.rf
        FROM players p
        LEFT JOIN latest_ratings r ON p.player_id = r.player_id
        WHERE {where} AND r.composite_score IS NOT NULL
    """, params).fetchall()
    return rows


def _pos_group(pos, role):
    """('C'..'RF', is_pitcher=False) for hitters, ('SP'/'RP', True) for
    pitchers, or (None, ...) if this player doesn't fit a tracked group
    (DH-only hitters, or a pitcher role we don't recognize)."""
    if role in ROLE_MAP:
        return ("RP" if ROLE_MAP[role] in ("RP", "CL") else "SP"), True
    return _HITTER_POS_CODES.get(pos), False


def get_scouting_targets(high_confidence=False):
    """Same four lists either way — high_confidence=False (default, powers
    /scouting) restricts to Average/Low/Very Low/unscouted, the players
    worth spending a scouting report on. high_confidence=True (powers
    /best-available) restricts to High/Very High instead: players you
    already know enough about to act on immediately.
    """
    conn = get_db()
    park = load_park_factors(get_cfg().league_dir)
    ratings_scale = get_cfg().ratings_scale
    hitter_weights = load_tool_weights(get_cfg().league_dir).get("hitter", {}) if park else {}

    rows = _fetch_pool(conn)

    # One entry per player, with everything every section needs.
    players = []
    for r in rows:
        (pid, name, age, pos, role, bats, acc, comp, ceil_score, true_ceil,
         cntct, gap, pow_, eye, stf, mov, ctrl,
         c_def, first_b, second_b, third_b, ss_def, lf, cf, rf) = r
        group, is_pitcher = _pos_group(pos, role)
        if group is None:
            continue
        potential = true_ceil if true_ceil is not None else ceil_score

        if is_pitcher:
            _tools = {"stuff": _normc(stf, ratings_scale), "movement": _normc(mov, ratings_scale),
                      "control": _normc(ctrl, ratings_scale)}
            park_fit = compute_pitcher_park_fit_from_tools(_tools, park) if park else None
            def_rating = None
        else:
            _tools = {"contact": _normc(cntct, ratings_scale), "gap": _normc(gap, ratings_scale),
                      "power": _normc(pow_, ratings_scale), "eye": _normc(eye, ratings_scale)}
            _hw = hitter_weights.get(group, hitter_weights.get("COF", {}))
            park_fit = compute_batter_park_fit(_tools, bats, _hw, park) if park else None
            def_raw = {"C": c_def, "1B": first_b, "2B": second_b, "3B": third_b,
                       "SS": ss_def, "LF": lf, "CF": cf, "RF": rf}[group]
            def_rating = _normc(def_raw, ratings_scale)

        players.append({
            "pid": pid, "name": name, "age": age, "group": group, "is_pitcher": is_pitcher,
            "composite_score": comp, "potential": potential, "acc": acc,
            "park_fit": park_fit, "def_rating": def_rating,
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
        n = max(1, round(len(ovrs) * _QUALITY_TOP_PCT))
        return ovrs[n - 1]

    ORDER = list(_HITTER_POS_CODES.values()) + ["SP", "RP"]

    # ── Best Free Agents Available: top 10 per position by Ovr ──
    best_fa = {}
    for group in ORDER:
        pool = [p for p in by_group.get(group, []) if _needs_scout(p) and p["composite_score"] is not None]
        pool.sort(key=lambda p: -p["composite_score"])
        best_fa[group] = pool[:_TOP_N]

    # ── Best Park Fits: top 10 per position by Park Fit, gated to top-25%-Ovr-at-position ──
    best_park = {}
    if park:
        for group in ORDER:
            group_players = by_group.get(group, [])
            cutoff = _quality_cutoff(group_players)
            pool = [p for p in group_players
                    if _needs_scout(p) and p["park_fit"] is not None
                    and cutoff is not None and (p["composite_score"] or -999) >= cutoff]
            pool.sort(key=lambda p: -p["park_fit"])
            best_park[group] = pool[:_TOP_N]

    # ── Best Defenders: hitters only, top 10 per position by def rating, same quality gate ──
    best_def = {}
    for group in _HITTER_POS_CODES.values():
        group_players = by_group.get(group, [])
        cutoff = _quality_cutoff(group_players)
        pool = [p for p in group_players
                if _needs_scout(p) and p["def_rating"] is not None
                and cutoff is not None and (p["composite_score"] or -999) >= cutoff]
        pool.sort(key=lambda p: -p["def_rating"])
        best_def[group] = pool[:_TOP_N]

    # ── Best Youth: age <= 24, top 10 per position by Potential ──
    best_youth = {}
    for group in ORDER:
        pool = [p for p in by_group.get(group, [])
                if _needs_scout(p) and p["age"] is not None and p["age"] <= 24 and p["potential"] is not None]
        pool.sort(key=lambda p: -p["potential"])
        best_youth[group] = pool[:_TOP_N]

    return {
        "best_fa": best_fa, "best_park": best_park, "best_def": best_def, "best_youth": best_youth,
        "order": ORDER, "hitter_order": list(_HITTER_POS_CODES.values()),
        "park_configured": bool(park),
    }
