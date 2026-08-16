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

    lineup = []
    used_pids = set()
    for group in _HITTER_ORDER:
        pool = by_group.get(group, [])
        vr_pick = _best(pool, "vr_score")
        vl_pick = _best(pool, "vl_score")
        lineup.append({"group": group, "vr": vr_pick, "vl": vl_pick})
        if vr_pick:
            used_pids.add(vr_pick["pid"])
        if vl_pick:
            used_pids.add(vl_pick["pid"])

    if cfg.settings.get("dh_rule", "No DH") != "No DH":
        dh_pool = [p for p in hitters if p["pid"] not in used_pids]
        dh_vr = _best(dh_pool, "vr_score")
        dh_vl = _best([p for p in dh_pool if p["pid"] != (dh_vr["pid"] if dh_vr else None)], "vl_score")
        lineup.append({"group": "DH", "vr": dh_vr, "vl": dh_vl})

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
    }


def _league_park_has_data(conn):
    try:
        return conn.execute("SELECT COUNT(*) FROM league_park_factors").fetchone()[0] > 0
    except Exception:
        return False


def import_league_park_factors(file_bytes, league_dir=None):
    """Placeholder — needs a real league-wide OOTP park-info export to
    build the actual parser against (format not yet seen). Returns 0
    until then.
    """
    return 0
