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
from team_queries import _get_state

_HITTER_ORDER = list(_HITTER_POS_CODES.values())  # C,1B,2B,3B,SS,LF,CF,RF

# Minimum PA vs a given handedness before "observed performance" shows a
# real adjustment instead of 0 — below this a hot/cold stretch is mostly
# noise, not signal.
_MIN_SPLIT_PA = 40
# Standard (unweighted-by-season) wOBA linear weights — good enough for a
# relative actual-vs-expected comparison, since both sides of that
# comparison use the same weights and any era/league-wide bias cancels out.
_WOBA_W = {"bb": 0.69, "hbp": 0.72, "1b": 0.89, "2b": 1.27, "3b": 1.62, "hr": 2.10}
# Observed-performance and park-fit deltas are both hints on top of the
# base rating, not a replacement for it — capped so neither can swing the
# adjusted composite further than a real talent gap between two players.
_OBS_DELTA_CAP = 15
_PARK_DELTA_CAP = 5
# Def rating (20-80, same scale as Viable Positions on the minors page)
# required at a position before a player is even considered for that slot.
_DEF_FLOOR = 65


def _linreg(xs, ys):
    """Simple OLS regression: returns (slope, intercept), or None if there
    isn't enough spread in the sample to fit one. Mirrors calibrate.py's
    _linreg() — kept as a small local copy rather than importing that
    module, since it's an offline batch-calibration script, not something
    meant to load into the live web server.
    """
    n = len(xs)
    if n < 5:
        return None
    mx = sum(xs) / n
    my = sum(ys) / n
    ss_xx = sum((x - mx) ** 2 for x in xs)
    ss_xy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    if ss_xx == 0:
        return None
    slope = ss_xy / ss_xx
    intercept = my - slope * mx
    return slope, intercept


def _woba(ab, h, d, t, hr, bb, hbp, sf):
    singles = h - d - t - hr
    num = (_WOBA_W["bb"] * bb + _WOBA_W["hbp"] * hbp + _WOBA_W["1b"] * singles
           + _WOBA_W["2b"] * d + _WOBA_W["3b"] * t + _WOBA_W["hr"] * hr)
    denom = ab + bb + sf + hbp
    return num / denom if denom > 0 else None


def _split_regression(conn, year, split_id):
    """(slope, intercept) fit of actual wOBA vs composite_score, across
    every qualified MLB hitter league-wide for this one split — the
    "expected wOBA for this composite" baseline real observed performance
    gets compared against. Self-calibrating: no hardcoded assumption about
    how many runs a composite point is worth, just "what did hitters who
    graded out this way actually produce, this split, this league, right
    now."
    """
    rows = conn.execute("""
        SELECT r.composite_score, b.ab, b.h, b.d, b.t, b.hr, b.bb, b.hbp, b.sf
        FROM mlb_batting_stats b
        JOIN players p ON p.player_id = b.player_id
        JOIN latest_ratings r ON r.player_id = b.player_id
        WHERE b.year=? AND b.split_id=? AND p.level='1' AND b.pa >= ?
              AND r.composite_score IS NOT NULL
    """, (year, split_id, _MIN_SPLIT_PA)).fetchall()
    xs, ys = [], []
    for comp, ab, h, d, t, hr, bb, hbp, sf in rows:
        w = _woba(ab or 0, h or 0, d or 0, t or 0, hr or 0, bb or 0, hbp or 0, sf or 0)
        if w is not None:
            xs.append(comp)
            ys.append(w)
    return _linreg(xs, ys)


def _split_stats_by_pid(conn, team_id, year):
    """{pid: {2: {...vs-L raw stat dict...}, 3: {...vs-R...}}} for this
    team's active roster — the raw ingredients _obs_delta() needs per
    player, fetched once rather than per-player.
    """
    rows = conn.execute("""
        SELECT player_id, split_id, ab, h, d, t, hr, bb, hbp, sf, pa
        FROM mlb_batting_stats WHERE year=? AND split_id IN (2,3) AND team_id=?
    """, (year, team_id)).fetchall()
    by_pid = {}
    for pid, split_id, ab, h, d, t, hr, bb, hbp, sf, pa in rows:
        by_pid.setdefault(pid, {})[split_id] = {
            "ab": ab or 0, "h": h or 0, "d": d or 0, "t": t or 0, "hr": hr or 0,
            "bb": bb or 0, "hbp": hbp or 0, "sf": sf or 0, "pa": pa or 0,
        }
    return by_pid


def _obs_delta(reg, split_stats, composite):
    """Composite points above/below `composite` this player's real
    observed performance in this split currently runs, per the league-wide
    wOBA~composite fit — None (not 0) below the PA floor or with no
    regression fit, so the template can show "-" instead of a misleadingly
    confident "+0".
    """
    if not reg or not split_stats or split_stats["pa"] < _MIN_SPLIT_PA or composite is None:
        return None
    actual = _woba(split_stats["ab"], split_stats["h"], split_stats["d"], split_stats["t"],
                    split_stats["hr"], split_stats["bb"], split_stats["hbp"], split_stats["sf"])
    if actual is None:
        return None
    slope, intercept = reg
    if slope == 0:
        return None
    expected = intercept + slope * composite
    points = (actual - expected) / slope
    return max(-_OBS_DELTA_CAP, min(_OBS_DELTA_CAP, round(points)))


def _park_delta(park_fit):
    if park_fit is None:
        return 0
    return max(-_PARK_DELTA_CAP, min(_PARK_DELTA_CAP, round(park_fit / 20)))


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

    # Base rating (vR/vL composite) is the primary signal — this just adds
    # two more numbers alongside it: how far real observed performance vs
    # that handedness is currently running from what the rating alone would
    # predict, and how many points this park is worth for this hitter's
    # tool profile. Both computed once per hitter here, up front, so every
    # pool the selection logic below considers already carries them.
    stats_year = _get_state()["stats_year"]
    reg_r = _split_regression(conn, stats_year, 3)  # vs RHP -> feeds vR
    reg_l = _split_regression(conn, stats_year, 2)  # vs LHP -> feeds vL
    split_stats = _split_stats_by_pid(conn, team_id, stats_year)
    for p in hitters:
        ss = split_stats.get(p["pid"], {})
        p["vr_obs_delta"] = _obs_delta(reg_r, ss.get(3), p["vr_score"])
        p["vl_obs_delta"] = _obs_delta(reg_l, ss.get(2), p["vl_score"])
        p["park_delta"] = _park_delta(p["park_fit"])
        p["vr_adjusted"] = (round(p["vr_score"] + (p["vr_obs_delta"] or 0) + p["park_delta"])
                             if p["vr_score"] is not None else None)
        p["vl_adjusted"] = (round(p["vl_score"] + (p["vl_obs_delta"] or 0) + p["park_delta"])
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

    def _def_qualified(pool):
        """Only players grading _DEF_FLOOR+ at this position — falls back
        to the full pool if nobody clears it, so a thin position never
        goes empty just because no one's a plus defender there yet."""
        qualified = [p for p in pool if p["def_rating"] is not None and p["def_rating"] >= _DEF_FLOOR]
        return qualified if qualified else pool

    lineup = []
    used_pids = set()
    for group in _HITTER_ORDER:
        pool = _def_qualified(by_group.get(group, []))
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
        "def_floor": _DEF_FLOOR, "min_split_pa": _MIN_SPLIT_PA,
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
