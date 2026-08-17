"""Lineup Optimizer — best-available hitter per position for a specific
upcoming matchup (opponent + home/away), split by vR/vL, plus how the
selected ballpark helps or hurts each pick. Also surfaces the full
rotation's park fit so starts can be ordered around it.

Scoped strictly to the current active MLB roster (level='1', your own
team_id) — this isn't a promotion-consideration tool, just "who do I
start out of who's already up," matching how the feature was asked for.
"""

from statsplusplus.evaluation.park_fit import load_park_factors
from web_league_context import get_db, get_cfg, my_team_id, team_names_map, team_abbr_map

from scouting_queries import _fetch_rows, _org_where, _build_entries, _HITTER_POS_CODES
from statsplusplus.data.evaluation_engine import load_tool_weights

_HITTER_ORDER = list(_HITTER_POS_CODES.values())  # C,1B,2B,3B,SS,LF,CF,RF

# Park fit is a hint on top of the base rating, not a replacement for it —
# capped small since OOTP is a ratings-driven game (stats are downstream of
# ratings, not independent evidence to weigh against them the way a real-
# world "observed vs. expected" adjustment would) and park fit itself
# empirically nudges by about a point either way for most players, rarely
# more. Capped so it can never come close to overriding a real talent gap.
_PARK_DELTA_CAP = 5
# Def rating (20-80, same scale as Viable Positions on the minors page)
# required at a position before a player is even considered for that slot.
_DEF_FLOOR = 65
# Catcher defense grades run lower league-wide than other positions (it's
# a specialized skill, not directly comparable to a corner infielder's
# range/arm grade) — the flat 65 floor was excluding legitimately-better-
# hitting backstops who'd never clear that bar at any level of the league.
_DEF_FLOOR_BY_GROUP = {"C": 50}


def _park_delta(park_fit):
    if park_fit is None:
        return 0
    return max(-_PARK_DELTA_CAP, min(_PARK_DELTA_CAP, round(park_fit / 20)))


# ---------------------------------------------------------------------------
# Batting order — reproduces the same composite/weighting/ordering logic
# used in the Philadelphia Athletics Assistant GM conversations, now with
# park factored into the composite (that methodology predates park-factor
# data existing in this app at all).
# ---------------------------------------------------------------------------

# Deliberately NOT the same weighting as compute_composite_hitter() — this
# is its own simpler, unrounded weighted average of the split-specific
# tools, matching the exact formula validated in those conversations.
_GM_TOOL_W = {"contact": 1.0, "gap": 1.08, "power": 1.2, "eye": 1.1}
_GM_DIVISOR = 4.3
# Speed/steal blend used for leadoff/9-hole/PR ranking — reverse-engineered
# from a known-good example order (Endicott > Cano > Jarmon) in those same
# conversations. Not a hard constant, just what reproduced that order.
_SPEED_W = (0.7, 0.3)


def _gm_composite(tools, park_delta=0):
    """(CON + GAP*1.08 + POW*1.2 + EYE*1.1) / 4.3, plus the same small
    park-fit nudge already used for the Optimal Lineup Adjusted column —
    None if any of the four core tools is missing rather than silently
    zero-filling a hole in the average.
    """
    if not tools:
        return None
    con, gap, pw, eye = tools.get("contact"), tools.get("gap"), tools.get("power"), tools.get("eye")
    if None in (con, gap, pw, eye):
        return None
    base = (con * _GM_TOOL_W["contact"] + gap * _GM_TOOL_W["gap"]
            + pw * _GM_TOOL_W["power"] + eye * _GM_TOOL_W["eye"]) / _GM_DIVISOR
    return round(base + (park_delta or 0), 1)


def _speed_score(speed, steal):
    if speed is None or steal is None:
        return None
    return round(speed * _SPEED_W[0] + steal * _SPEED_W[1], 1)


_WEAK_BAT_THRESHOLD = 50


def _build_batting_order(starters, tools_key, has_dh):
    """9-slot batting order (1-indexed slots as a 0-indexed list).

    Leadoff is simply the fastest hitter, regardless of bat — a speed/OBP
    leadoff type is the normal case, not something that needs a weak-bat
    qualifier. The bottom-of-order "second leadoff" (turning the last real
    slot before the pitcher's spot into a table-setter) is a genuinely
    different move: it only makes sense as a way to hide a real weakness,
    so it's only used when the fastest REMAINING player is also a below-
    average bat (GM composite under _WEAK_BAT_THRESHOLD). A fast player
    who can also hit belongs wherever his composite already ranks him —
    bumping him to the bottom of the order for being quick would be
    burying a good bat for no reason. When no such weak-bat speedster
    exists, the last real slot just fills by composite like every other
    slot, and (No-DH only) the pitcher stays in his normal 9-hole.

    2-hole is the best composite+speed blend from what's left, 3 through
    the last real slot fill by composite, descending.

    DH leagues have 9 real hitters, so "the last real slot" is 9. No-DH
    leagues have 8 real position players — the last real slot is 8, and
    slot 9 (the actual pitcher) isn't modeled here at all.
    """
    pool = []
    for p in starters:
        gm = _gm_composite(p.get(tools_key), p.get("park_delta", 0))
        if gm is None:
            continue
        spd = _speed_score(p.get("speed"), p.get("steal"))
        pool.append({"p": p, "gm": gm, "speed": spd if spd is not None else -1})

    order = [None] * 9
    last_real_slot = 8 if has_dh else 7  # 0-indexed: batting "9" or "8"

    def _place(slot, e):
        order[slot] = dict(e["p"], gm_composite=e["gm"],
                            speed_score=e["speed"] if e["speed"] != -1 else None)

    by_speed = sorted(pool, key=lambda e: (-e["speed"], -e["gm"]))
    leadoff = by_speed[0] if by_speed else None
    if leadoff:
        pool.remove(leadoff)
        _place(0, leadoff)

    # Look for the fastest player who's ALSO a weak bat, not just whether
    # the single fastest remaining player happens to qualify — a burner
    # further down the speed list can still be the right "second leadoff"
    # even if someone faster but better-hitting (like Lacefield) is ahead
    # of him in raw speed.
    weak_speedsters = [e for e in pool if e["gm"] < _WEAK_BAT_THRESHOLD]
    bottom = max(weak_speedsters, key=lambda e: e["speed"]) if weak_speedsters else None
    if bottom:
        pool.remove(bottom)
        _place(last_real_slot, bottom)

    two_hole = None
    if pool:
        two_hole = max(pool, key=lambda e: e["gm"] * 0.7 + e["speed"] * 0.3)
        pool.remove(two_hole)
        _place(1, two_hole)

    pool.sort(key=lambda e: -e["gm"])
    fill_slots = [s for s in range(2, last_real_slot + 1) if order[s] is None]
    for slot, e in zip(fill_slots, pool):
        _place(slot, e)

    return order


def _bench_pool(hitters, starter_pids):
    return [p for p in hitters if p["pid"] not in starter_pids]


def _ph_ranking(bench, tools_key):
    """Bench hitters ranked by GM composite (handedness-specific, park-
    adjusted), descending — every bench bat, not an arbitrary top-N cut."""
    ranked = []
    for p in bench:
        gm = _gm_composite(p.get(tools_key), p.get("park_delta", 0))
        if gm is not None:
            ranked.append({"p": p, "gm": gm})
    ranked.sort(key=lambda e: -e["gm"])
    return ranked


def _pr_ranking(bench):
    ranked = []
    for p in bench:
        spd = _speed_score(p.get("speed"), p.get("steal"))
        if spd is not None:
            ranked.append({"p": p, "speed": spd})
    ranked.sort(key=lambda e: -e["speed"])
    return ranked


def _load_park(team_id, opponent_id, is_home):
    """My own park (config/park_factors.json) for a home game; the
    opponent's park from the league-wide upload for an away game. None if
    the away park hasn't been uploaded yet.
    """
    if is_home or not opponent_id:
        return load_park_factors(get_cfg().league_dir), True
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT park, avg, avg_l, avg_r, hr, hr_l, hr_r, doubles, triples, overall "
            "FROM league_park_factors WHERE team_id=?", (opponent_id,)
        ).fetchone()
    except Exception:
        row = None
    if not row:
        return None, False
    return dict(row), False


def get_lineup_optimizer(opponent_id=None, is_home=True):
    conn = get_db()
    team_id = my_team_id()
    cfg = get_cfg()
    ratings_scale = cfg.ratings_scale
    all_weights = load_tool_weights(cfg.league_dir)
    hitter_weights = all_weights.get("hitter", {})
    pitcher_weights = all_weights.get("pitcher", {})

    park, park_is_mine = _load_park(team_id, opponent_id, is_home)

    ed = conn.execute("SELECT MAX(eval_date) FROM prospect_fv").fetchone()[0]
    ed_surplus = conn.execute("SELECT MAX(eval_date) FROM player_surplus").fetchone()[0]

    where, params = _org_where(team_id, "mlb")
    rows = _fetch_rows(conn, where, params, ed, ed_surplus)
    entries = _build_entries(rows, ratings_scale, park, hitter_weights, pitcher_weights,
                              set(), is_mine=True)

    hitters = [p for p in entries if not p["is_pitcher"]]
    pitchers = [p for p in entries if p["is_pitcher"]]

    # Base rating (vR/vL composite) is the primary signal and the only
    # foundation for the pick itself — Park is just a small nudge shown
    # alongside it, computed once per hitter here so every pool the
    # selection logic below considers already carries it.
    for p in hitters:
        p["park_delta"] = _park_delta(p["park_fit"])
        p["vr_adjusted"] = (round(p["vr_score"] + p["park_delta"])
                             if p["vr_score"] is not None else None)
        p["vl_adjusted"] = (round(p["vl_score"] + p["park_delta"])
                             if p["vl_score"] is not None else None)

    by_group = {}
    for p in hitters:
        by_group.setdefault(p["group"], []).append(p)

    def _best(pool, score_key):
        """Highest score_key, Park Fit as the tiebreaker (a natural
        secondary sort key — ties in score_key, common on a 20-80 integer
        scale, fall through to Park Fit automatically)."""
        candidates = [p for p in pool if p[score_key] is not None]
        if not candidates:
            return None
        candidates.sort(key=lambda p: (
            -(p[score_key]),
            -(p["park_fit"] if p["park_fit"] is not None else -999),
        ))
        return candidates[0]

    def _def_qualified(pool, group):
        """Only players grading at or above this position's defense floor
        — falls back to the full pool if nobody clears it, so a thin
        position never goes empty just because no one's a plus defender
        there yet."""
        floor = _DEF_FLOOR_BY_GROUP.get(group, _DEF_FLOOR)
        qualified = [p for p in pool if p["def_rating"] is not None and p["def_rating"] >= floor]
        return qualified if qualified else pool

    lineup = []
    used_pids = set()
    for group in _HITTER_ORDER:
        pool = _def_qualified(by_group.get(group, []), group)
        vr_pick = _best(pool, "vr_score")
        vl_pick = _best(pool, "vl_score")
        lineup.append({"group": group, "vr": vr_pick, "vl": vl_pick})
        if vr_pick:
            used_pids.add(vr_pick["pid"])
        if vl_pick:
            used_pids.add(vl_pick["pid"])

    has_dh = cfg.settings.get("dh_rule", "No DH") != "No DH"
    if has_dh:
        dh_pool = [p for p in hitters if p["pid"] not in used_pids]
        dh_vr = _best(dh_pool, "vr_score")
        dh_vl = _best([p for p in dh_pool if p["pid"] != (dh_vr["pid"] if dh_vr else None)], "vl_score")
        lineup.append({"group": "DH", "vr": dh_vr, "vl": dh_vl})

    vr_starters = [slot["vr"] for slot in lineup if slot["vr"]]
    vl_starters = [slot["vl"] for slot in lineup if slot["vl"]]
    batting_order_vr = _build_batting_order(vr_starters, "vr_tools", has_dh)
    batting_order_vl = _build_batting_order(vl_starters, "vl_tools", has_dh)

    vr_starter_pids = {p["pid"] for p in vr_starters}
    vl_starter_pids = {p["pid"] for p in vl_starters}
    bench_vr = _bench_pool(hitters, vr_starter_pids)
    bench_vl = _bench_pool(hitters, vl_starter_pids)
    ph_vr = _ph_ranking(bench_vr, "vr_tools")
    ph_vl = _ph_ranking(bench_vl, "vl_tools")
    pr_vr = _pr_ranking(bench_vr)
    pr_vl = _pr_ranking(bench_vl)

    rotation = [p for p in pitchers if p["group"] == "SP"]
    rotation.sort(key=lambda p: -(p["composite_score"] or 0))
    bullpen = [p for p in pitchers if p["group"] == "RP"]
    bullpen.sort(key=lambda p: -(p["composite_score"] or 0))

    abbrs = team_abbr_map()
    park_team_abbr = abbrs.get(team_id) if park_is_mine else abbrs.get(opponent_id)

    return {
        "lineup": lineup, "rotation": rotation, "bullpen": bullpen,
        "park": park, "park_is_mine": park_is_mine, "park_team_abbr": park_team_abbr,
        "opponent_id": opponent_id, "is_home": is_home,
        "other_teams": sorted(
            ((tid, name) for tid, name in team_names_map().items() if tid != team_id),
            key=lambda x: x[1],
        ),
        "park_uploaded": _league_park_has_data(conn),
        "def_floor": _DEF_FLOOR,
        "batting_order_vr": batting_order_vr, "batting_order_vl": batting_order_vl,
        "has_dh": has_dh,
        "ph_vr": ph_vr, "ph_vl": ph_vl, "pr_vr": pr_vr, "pr_vl": pr_vl,
    }


def _league_park_has_data(conn):
    try:
        return conn.execute("SELECT COUNT(*) FROM league_park_factors").fetchone()[0] > 0
    except Exception:
        return False


def import_league_park_factors(file_bytes, league_dir=None):
    """Import league-wide park factors from an OOTP "Team Info"/"Park
    Info" export. Two real formats seen (one per league so far): one with
    a literal team ID column (matches team_id directly), one without
    (team name only — matched against this league's own team_names_map,
    the same lookup the rest of the app already uses for display names).

    Static data (only relocation changes it, which isn't modeled yet —
    a fresh upload at that point just replaces these rows), so a full
    replace on each upload is correct, not a merge.
    """
    import datetime
    from custom_upload import parse_rows
    from statsplusplus.config.league_config import LeagueConfig
    from statsplusplus.data.db import get_conn as _get_conn

    rows = parse_rows(file_bytes)
    if not rows:
        return 0

    lc = LeagueConfig(base_dir=league_dir)
    name_to_tid = {name: tid for tid, name in lc.team_names_map.items()}

    def _num(v):
        if v is None:
            return None
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    entries = []
    for d in rows:
        team_id = None
        raw_id = (d.get("ID") or "").strip()
        if raw_id:
            try:
                team_id = int(raw_id)
            except ValueError:
                team_id = None
        if team_id is None:
            team_id = name_to_tid.get((d.get("Team Name") or "").strip())
        if team_id is None:
            continue

        avg = _num(d.get("PF AVG"))
        if avg is None:
            continue
        park_name = (d.get("Park") or d.get("Team Name") or "").strip()
        entries.append((
            team_id, park_name,
            avg, _num(d.get("AVG L")), _num(d.get("AVG R")),
            _num(d.get("PF HR")), _num(d.get("HR L")), _num(d.get("HR R")),
            _num(d.get("PF D")), _num(d.get("PF T")), _num(d.get("PF")),
        ))

    if not entries:
        return 0

    conn = _get_conn(league_dir)
    now = datetime.datetime.now().isoformat()
    conn.execute("DELETE FROM league_park_factors")
    conn.executemany(
        "INSERT INTO league_park_factors "
        "(team_id, park, avg, avg_l, avg_r, hr, hr_l, hr_r, doubles, triples, overall, uploaded_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        [tuple(e) + (now,) for e in entries],
    )
    conn.commit()
    conn.close()
    return len(entries)
