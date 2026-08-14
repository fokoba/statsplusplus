"""Team-level DB queries for the web dashboard.

Note: query functions use sqlite3.Row access. Integer indexing (r[0]) is used
for compactness in many functions; named access (r["col"]) works equally well.
"""

import os, sys
from collections import defaultdict

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "scripts"))
from statsplusplus.utils.positions import display_pos as _display_pos
from statsplusplus.evaluation.surplus import calc_pap
from statsplusplus.config.league_config import dollars_per_war as _dpw_pkg, league_minimum as _lm_pkg
from statsplusplus.utils.positions import ROLE_MAP
from statsplusplus.evaluation.constants import DEFAULT_MINIMUM_SALARY
from statsplusplus.data.evaluation_engine import load_tool_weights
from web_league_context import (get_db, get_cfg, team_abbr_map, team_names_map,
                                 level_map, pos_map, pos_order, pyth_exp, my_team_id,
                                 mlb_team_ids, league_averages as _load_la,
                                 money_unit as _money_unit, money_divisor as _money_divisor)

# Local wrappers using request-scoped league_dir
def _dollars_per_war():
    return _dpw_pkg(get_cfg().league_dir)

def league_minimum():
    return _lm_pkg(get_cfg().league_dir)


# SQL fragment + params to filter contracts to players currently in a given org.
# This is the *only* org filter these queries should use — contract_team_id
# is unreliable and must not be ANDed alongside it: Rule 5 picks and traded
# players both retain their contract row's original contract_team_id, so a
# hard `contract_team_id = ?` gate silently excludes them even though this
# players-table check (their actual current team_id/parent_team_id) already
# correctly identifies them as belonging to this org.
_CONTRACT_ORG_SQL = (
    "AND (p.parent_team_id = ? OR (p.parent_team_id = 0 AND p.team_id = ?))"
)
def _contract_org_params(team_id):
    return (team_id, team_id)


def _pap_context(conn, tid, year):
    """Get shared context for PAP calculation: team games, $/WAR, salary map."""
    team_g = conn.execute(
        "SELECT COUNT(*) FROM games WHERE (home_team=? OR away_team=?) AND date>=? AND played=1",
        (tid, tid, f"{year}-01-01")).fetchone()[0]
    dpw = _dollars_per_war()
    sal_rows = conn.execute(
        "SELECT player_id, salary_0 FROM contracts WHERE player_id IN "
        "(SELECT player_id FROM players WHERE team_id=? AND level='1')", (tid,)).fetchall()
    salaries = {r["player_id"]: r["salary_0"] or 0 for r in sal_rows}
    return team_g, dpw, salaries


def _get_state():
    from flask import g as _g, has_request_context as _hrc
    if _hrc() and hasattr(_g, "_tq_state_cache"):
        return _g._tq_state_cache
    import json
    cfg = get_cfg()
    with open(cfg.state_path) as f:
        state = json.load(f)
    # During preseason, current year has no stats. Use most recent year with data
    # so all queries that reference state["year"] for stats get valid results.
    conn = get_db()
    row = conn.execute(
        "SELECT MAX(year) FROM mlb_batting_stats WHERE year <= ?", (state["year"],)
    ).fetchone()
    state["stats_year"] = row[0] if row and row[0] else state["year"]
    if _hrc():
        _g._tq_state_cache = state
    return state


def _get_eval_date():
    """Get the most recent eval_date, cached per request."""
    from flask import g as _g, has_request_context as _hrc
    if _hrc() and hasattr(_g, "_tq_eval_date_cache"):
        return _g._tq_eval_date_cache
    conn = get_db()
    ed = conn.execute("SELECT MAX(eval_date) FROM player_surplus").fetchone()[0]
    if _hrc():
        _g._tq_eval_date_cache = ed
    return ed


def _peak_surplus(fv_continuous, age, level, bucket, ovr=None, pot=None):
    """Best single expected-grade projected year of surplus (money-scaled),
    or None when there isn't enough data (no prospect_fv row for this
    player). Quality signal independent of runway length — see
    peak_year_surplus() in scripts/prospect_value.py for why this is a
    better "how good is this prospect" comparison than total surplus.
    """
    if fv_continuous is None or age is None or not level or not bucket:
        return None
    try:
        from prospect_value import peak_year_surplus as _pys
        result = _pys(fv_continuous, age, level, bucket, ovr=ovr, pot=pot,
                      league_dir=get_cfg().league_dir)
        return round(result["surplus"] / _money_divisor(), 1)
    except Exception:
        return None


def _surplus_horizons_live(fv_continuous, age, level, bucket, ovr=None, pot=None):
    """Current-year, next-year, and 3-year surplus (money-scaled) for a
    prospect/non-contract player. Same live-computed source as
    _peak_surplus() above, just prospect_surplus_horizons() instead of
    peak_year_surplus() — see scripts/prospect_value.py.
    """
    if fv_continuous is None or age is None or not level or not bucket:
        return None, None, None
    try:
        from prospect_value import prospect_surplus_horizons as _psh
        cur_s, next_s, three_s = _psh(fv_continuous, age, level, bucket, get_cfg().year,
                                      ovr=ovr, pot=pot, league_dir=get_cfg().league_dir)
        return (round(cur_s / _money_divisor(), 1) if cur_s is not None else None,
                round(next_s / _money_divisor(), 1) if next_s is not None else None,
                round(three_s / _money_divisor(), 1) if three_s is not None else None)
    except Exception:
        return None, None, None


def get_summary(team_id=None):
    state = _get_state()
    conn = get_db()
    year = state.get("stats_year", state["year"])
    tid = team_id or my_team_id()
    ed = _get_eval_date()
    mlb_surplus = conn.execute(
        "SELECT COALESCE(SUM(surplus),0) FROM player_surplus WHERE eval_date=? AND team_id=?",
        (ed, tid)).fetchone()[0]
    farm_surplus = conn.execute(
        "SELECT COALESCE(SUM(prospect_surplus),0) FROM prospect_fv pf JOIN players p ON pf.player_id=p.player_id WHERE pf.eval_date=? AND (p.parent_team_id=? OR (p.team_id=? AND p.level='1'))",
        (ed, tid, tid)).fetchone()[0]
    fv50 = conn.execute(
        "SELECT COUNT(*) FROM prospect_fv pf JOIN players p ON pf.player_id=p.player_id WHERE pf.eval_date=? AND (p.parent_team_id=? OR (p.team_id=? AND p.level='1')) AND pf.fv>=50 AND p.age<=25",
        (ed, tid, tid)).fetchone()[0]
    # Determine season phase from game date
    gd = state["game_date"]
    month = int(gd[5:7]) if gd and len(gd) >= 7 else 0
    # Check if any games have been played this year
    games_played = conn.execute(
        "SELECT COUNT(*) FROM games WHERE date LIKE ? AND played=1 AND game_type=0",
        (f"{state['year']}%",)).fetchone()[0]
    if games_played == 0 and month <= 4:
        phase = "Spring Training"
    elif month >= 10 or (month == 9 and games_played > 140 * 8):
        phase = "Postseason"
    elif month >= 11 or month <= 1:
        phase = "Offseason"
    else:
        phase = "Regular Season"

    # Roster-wide Current/Next/3-Year surplus — same per-player horizons
    # shown on the Contracts tab, summed across every MLB roster player
    # (not just the ones the Contracts table displays, which drops
    # minimum-salary rookies to declutter that view).
    cur_sum = next_sum = three_sum = 0.0
    have_any = False
    try:
        from contract_value import contract_surplus_horizons as _csh
        _game_year = get_cfg().year
        _league_dir = get_cfg().league_dir
        pids = [r[0] for r in conn.execute(
            "SELECT player_id FROM contracts WHERE is_major=1 AND player_id IN "
            "(SELECT player_id FROM players WHERE team_id=?)", (tid,)).fetchall()]
        for pid in pids:
            try:
                cs, ns, ts = _csh(pid, _game_year, league_dir=_league_dir)
            except Exception:
                cs, ns, ts = None, None, None
            if cs is not None:
                cur_sum += cs; have_any = True
            if ns is not None:
                next_sum += ns
            if ts is not None:
                three_sum += ts
    except Exception:
        have_any = False

    return {
        "game_date": state["game_date"], "year": state["year"], "phase": phase,
        "mlb_surplus": round(mlb_surplus / _money_divisor(), 1),
        "farm_surplus": round(farm_surplus / _money_divisor(), 1),
        "current_year_surplus": round(cur_sum / _money_divisor(), 1) if have_any else None,
        "next_year_surplus": round(next_sum / _money_divisor(), 1) if have_any else None,
        "three_year_surplus": round(three_sum / _money_divisor(), 1) if have_any else None,
        "fv50_count": fv50,
    }


def _team_won(g, tid):
    """Did tid win? API convention: runs0=away, runs1=home."""
    if g[0] == tid:  # home
        return g[2] > g[1]  # runs1(home) > runs0(away)
    return g[1] > g[2]  # runs0(away) > runs1(home)


def get_power_rankings():
    """Composite power rankings: pyth W% (50%), last-10 (25%), run diff/game (25%)."""
    standings = get_standings()
    if not standings:
        return []

    state = _get_state()
    year = state.get("stats_year", state["year"])
    conn = get_db()

    # Surplus for display only
    ed = _get_eval_date()
    surplus_map = dict(conn.execute(
        "SELECT team_id, SUM(surplus) FROM player_surplus WHERE eval_date=? GROUP BY team_id",
        (ed,)).fetchall())
    farm_map = dict(conn.execute("""
        SELECT COALESCE(NULLIF(p.parent_team_id,0), p.team_id), SUM(pf.prospect_surplus)
        FROM prospect_fv pf JOIN players p ON pf.player_id=p.player_id
        WHERE pf.eval_date=?
        GROUP BY COALESCE(NULLIF(p.parent_team_id,0), p.team_id)
    """, (ed,)).fetchall())

    # Last-10 record and streak
    tids = [r["tid"] for r in standings]
    l10_map, streak_map = {}, {}
    has_games = conn.execute(
        "SELECT COUNT(*) FROM games WHERE date LIKE ? AND played=1 AND game_type=0",
        (f"{year}%",)).fetchone()[0] > 0

    if has_games:
        for tid in tids:
            games = conn.execute("""
                SELECT home_team, runs0, runs1 FROM games
                WHERE (home_team=? OR away_team=?) AND played=1 AND game_type=0 AND date LIKE ?
                ORDER BY date DESC, game_id DESC LIMIT 10
            """, (tid, tid, f"{year}%")).fetchall()
            w = sum(1 for g in games if _team_won(g, tid))
            l10_map[tid] = (w, len(games) - w)
            s_count, s_type = 0, None
            for g in games:
                res = "W" if _team_won(g, tid) else "L"
                if s_type is None:
                    s_type = res
                if res == s_type:
                    s_count += 1
                else:
                    break
            streak_map[tid] = f"{s_type}{s_count}" if s_type else "-"


    # Normalize components to 0-1
    pyths = {r["tid"]: r["pct"] for r in standings}
    rdpg = {r["tid"]: r["diff"] / r["g"] if r["g"] else 0 for r in standings}
    l10_pct = {t: l10_map[t][0] / sum(l10_map[t]) if t in l10_map and sum(l10_map[t]) else 0.5 for t in tids}

    def _norm(d):
        vals = list(d.values())
        lo, hi = min(vals), max(vals)
        span = hi - lo if hi != lo else 1
        return {k: (v - lo) / span for k, v in d.items()}

    n_pyth, n_rdpg, n_l10 = _norm(pyths), _norm(rdpg), _norm(l10_pct)
    w_pyth, w_l10, w_rdpg = (0.50, 0.25, 0.25) if has_games else (0.65, 0.00, 0.35)

    rows = []
    for r in standings:
        t = r["tid"]
        score = n_pyth[t]*w_pyth + n_l10[t]*w_l10 + n_rdpg[t]*w_rdpg
        l10w, l10l = l10_map.get(t, (0, 0))
        rows.append({
            "tid": t, "name": r["name"], "abbr": team_abbr_map().get(t, "?"),
            "g": r["g"], "w": r["w"], "l": r["l"],
            "pct": r["w"] / r["g"] if r["g"] else 0,
            "pyth_w": r["pyth_w"], "pyth_l": r["pyth_l"],
            "rs": r["rs"], "ra": r["ra"], "diff": r["diff"],
            "rdpg": rdpg[t],
            "l10": f"{l10w}-{l10l}" if has_games else "-",
            "streak": streak_map.get(t, "-"),
            "mlb_surplus": round(surplus_map.get(t, 0) / _money_divisor(), 1),
            "farm_surplus": round(farm_map.get(t, 0) / _money_divisor(), 1),
            "score": round(score * 100, 1),
            "is_mine": r["is_mine"],
        })
    rows.sort(key=lambda x: -x["score"])
    for i, r in enumerate(rows):
        r["rank"] = i + 1
    return rows


def get_standings():
    state = _get_state()
    conn = get_db()
    year = state.get("stats_year", state["year"])

    bat = {r[0]: (r[1], r[2]) for r in conn.execute(
        "SELECT team_id, name, r FROM team_batting_stats WHERE year=? AND split_id=1", (year,)).fetchall()}
    # Fall back to prior year if current year has no stats (preseason)
    if not bat:
        year = year - 1
        bat = {r[0]: (r[1], r[2]) for r in conn.execute(
            "SELECT team_id, name, r FROM team_batting_stats WHERE year=? AND split_id=1", (year,)).fetchall()}
    pit = {r[0]: (r[1], r[2]) for r in conn.execute(
        "SELECT team_id, r, ip FROM team_pitching_stats WHERE year=? AND split_id=1", (year,)).fetchall()}

    # Actual W/L from games (runs0=away, runs1=home)
    actual_wl = {}
    game_rows = conn.execute(
        "SELECT home_team, away_team, runs0, runs1 FROM games WHERE date LIKE ? AND played=1 AND game_type=0",
        (f"{year}%",)).fetchall()
    if game_rows:
        from collections import Counter
        wins, losses = Counter(), Counter()
        for g in game_rows:
            # g = (home_team, away_team, runs0=away_runs, runs1=home_runs)
            if g[3] > g[2]:  # home wins (runs1 > runs0)
                wins[g[0]] += 1; losses[g[1]] += 1
            else:  # away wins
                wins[g[1]] += 1; losses[g[0]] += 1
        for tid in set(wins) | set(losses):
            actual_wl[tid] = (wins[tid], losses[tid])


    rows = []
    if not bat:
        # Preseason: show all MLB teams with zero records
        names = team_names_map()
        for tid in mlb_team_ids():
            name = names.get(tid, "?")
            rows.append({"tid": tid, "name": name, "g": 0,
                          "w": 0, "l": 0, "pyth_w": 0, "pyth_l": 0,
                          "pct": 0.0, "rs": 0, "ra": 0, "diff": 0,
                          "div": get_cfg().team_div_map.get(tid, ""),
                          "has_actual": False})
    else:
        for tid, (name, rs) in bat.items():
            if tid not in pit:
                continue
            ra, ip = pit[tid]
            g = round(ip / 9)
            if g == 0 or rs + ra == 0:
                continue
            pyth = rs**pyth_exp() / (rs**pyth_exp() + ra**pyth_exp())
            pyth_w = round(pyth * g, 1)
            pyth_l = round(g - pyth_w, 1)
            aw, al = actual_wl.get(tid, (pyth_w, pyth_l))
            ag = aw + al
            pct = aw / ag if ag else pyth
            rows.append({"tid": tid, "name": name, "g": ag,
                          "w": aw, "l": al, "pyth_w": pyth_w, "pyth_l": pyth_l,
                          "pct": pct, "rs": rs, "ra": ra, "diff": rs - ra,
                          "div": get_cfg().team_div_map.get(tid, ""),
                          "has_actual": tid in actual_wl})
    rows.sort(key=lambda x: x["pct"], reverse=True)

    if rows:
        leader_w, leader_l = rows[0]["w"], rows[0]["l"]
        for i, r in enumerate(rows):
            r["rank"] = i + 1
            gb = ((leader_w - leader_l) - (r["w"] - r["l"])) / 2
            r["gb"] = "-" if gb < 0.25 else f"{gb:.1f}"
            r["is_mine"] = r["tid"] == my_team_id()
    return rows


def get_division_standings(team_id=None):
    all_rows = get_standings()
    tid = team_id or my_team_id()
    my_div = get_cfg().team_div_map.get(tid, "")
    div_rows = [r for r in all_rows if r["div"] == my_div]
    # If division has only 1 team (misconfigured), show the full league instead
    if len(div_rows) <= 1:
        # Find the league this team belongs to
        lg = get_cfg().league_for_team(tid)
        if lg:
            lg_tids = set()
            for tids in lg["divisions"].values():
                lg_tids.update(tids)
            div_rows = [r for r in all_rows if r["tid"] in lg_tids]
            my_div = lg["name"]
        else:
            div_rows = all_rows
            my_div = "League"
    if div_rows:
        div_rows.sort(key=lambda x: x["pct"], reverse=True)
        leader_w, leader_l = div_rows[0]["w"], div_rows[0]["l"]
        for i, r in enumerate(div_rows):
            r["rank"] = i + 1
            gb = ((leader_w - leader_l) - (r["w"] - r["l"])) / 2
            r["gb"] = "-" if gb < 0.25 else f"{gb:.1f}"
    return div_rows, my_div


def get_roster(team_id=None):
    state = _get_state()
    conn = get_db()
    year = state.get("stats_year", state["year"])
    tid = team_id or my_team_id()
    ed = _get_eval_date()

    players = conn.execute("""
        SELECT p.player_id, p.name, p.age, p.pos, p.role,
               ps.ovr, ps.surplus, ps.bucket,
               r.composite_score
        FROM players p
        LEFT JOIN player_surplus ps ON p.player_id=ps.player_id AND ps.eval_date=?
        LEFT JOIN latest_ratings r ON p.player_id=r.player_id
        WHERE p.team_id=? AND p.level='1'
    """, (ed, tid)).fetchall()

    bat = {}
    for r in conn.execute(
        "SELECT player_id, ab, h, d, t, hr, bb, pa, war FROM mlb_batting_stats WHERE year=? AND split_id=1 AND team_id=?", (year, tid)
    ).fetchall():
        pid, ab, h, d, t, hr, bb, pa, war = r
        avg = h / ab if ab else None
        obp = (h + bb) / pa if pa else None
        slg = (h + d + 2 * t + 3 * hr) / ab if ab else None
        bat[pid] = (avg, obp, slg, war)

    pit = {}
    for r in conn.execute(
        "SELECT player_id, era, ip, k, war FROM mlb_pitching_stats WHERE year=? AND split_id=1 AND team_id=?", (year, tid)
    ).fetchall():
        pit[r[0]] = (r[1], r[2], r[3], r[4])

    mlb_pids = {row[0] for row in players}

    hitters, pitchers = [], []
    for pid, name, age, pos, role, ovr, surplus, bucket, comp_score in players:
        _display_ovr = comp_score if comp_score is not None else (ovr or 0)
        base = {"pid": pid, "name": name, "age": age, "ovr": _display_ovr,
                "surplus": round(surplus / _money_divisor(), 1) if surplus else 0}
        if role in (11, 12, 13):
            s = pit.get(pid, (None, None, None, None))
            role_str = ROLE_MAP.get(role, "P")
            base.update({"role": role_str, "role_order": pos_order().get(role_str, 99),
                          "era": s[0], "ip": s[1], "k": s[2],
                          "war": round(s[3], 1) if s[3] is not None else 0})
            pitchers.append(base)
        else:
            s = bat.get(pid, (None, None, None, None))
            base.update({"pos": pos_map().get(pos, "?"),
                          "pos_order": pos_order().get(pos_map().get(pos, "?"), 99),
                          "avg": s[0], "obp": s[1], "slg": s[2],
                          "war": round(s[3], 1) if s[3] is not None else 0})
            hitters.append(base)

    hitters.sort(key=lambda x: x["war"], reverse=True)
    pitchers.sort(key=lambda x: x["war"], reverse=True)
    return hitters, pitchers


def get_roster_hitters(team_id=None):
    """Hitters with all 3 splits for the roster Hitters tab.
    Includes two-way players (pitchers with PA >= 30)."""
    state = _get_state()
    conn = get_db()
    year = state.get("stats_year", state["year"])
    tid = team_id or my_team_id()
    ed = _get_eval_date()

    # Position players
    players = conn.execute("""
        SELECT p.player_id, p.name, p.age, p.pos, p.role,
               ps.ovr, ps.surplus, ps.surplus_yr1,
               r.composite_score,
               p.injury_is_injured, p.injury_left, p.is_on_dl60,
               p.designated_for_assignment, p.is_on_waivers, p.is_on_dl
        FROM players p
        LEFT JOIN player_surplus ps ON p.player_id=ps.player_id AND ps.eval_date=?
        LEFT JOIN latest_ratings r ON p.player_id=r.player_id
        WHERE p.team_id=? AND p.level='1' AND COALESCE(p.role,0) NOT IN (11,12,13)
    """, (ed, tid)).fetchall()

    # Two-way pitchers with meaningful batting (PA >= 30)
    twp = conn.execute("""
        SELECT p.player_id, p.name, p.age, p.pos, p.role,
               ps.ovr, ps.surplus, ps.surplus_yr1,
               r.composite_score,
               p.injury_is_injured, p.injury_left, p.is_on_dl60,
               p.designated_for_assignment, p.is_on_waivers, p.is_on_dl
        FROM players p
        LEFT JOIN player_surplus ps ON p.player_id=ps.player_id AND ps.eval_date=?
        LEFT JOIN latest_ratings r ON p.player_id=r.player_id
        JOIN mlb_batting_stats b ON b.player_id=p.player_id AND b.year=? AND b.split_id=1 AND b.pa>=30
        WHERE p.team_id=? AND p.level='1' AND p.role IN (11,12,13)
    """, (ed, year, tid)).fetchall()
    twp_pids = {p["player_id"] for p in twp}
    players = list(players) + list(twp)

    # Load all 3 splits — scoped to this team_id so a mid-season trade doesn't
    # let the other team's stint silently clobber this one (players keep a
    # separate stats row per team_id they played for in a given year).
    bat = {}  # pid -> {split_id -> dict}
    for r in conn.execute("""
        SELECT player_id, split_id, ab, h, d, t, hr, r, rbi, sb, bb, k, pa, war, g, cs, hbp, sf
        FROM mlb_batting_stats WHERE year=? AND split_id IN (1,2,3) AND team_id=?
    """, (year, tid)):
        bat.setdefault(r["player_id"], {})[r["split_id"]] = dict(r)

    # For two-way players: primary non-pitcher fielding position
    conn_fld = {}
    if twp_pids:
        for r in conn.execute(
            "SELECT player_id, position, g FROM mlb_fielding_stats "
            "WHERE year=? AND position != 1 AND player_id IN ({})".format(
                ",".join("?" * len(twp_pids))),
            [year] + list(twp_pids)
        ).fetchall():
            pid = r["player_id"]
            if pid not in conn_fld or r["g"] > conn_fld[pid][1]:
                conn_fld[pid] = (r["position"], r["g"])
        conn_fld = {pid: pos for pid, (pos, _) in conn_fld.items()}


    def _fmt_split(s):
        if not s:
            return None
        ab, pa = s["ab"] or 0, s["pa"] or 0
        h, d, t, hr = s["h"] or 0, s["d"] or 0, s["t"] or 0, s["hr"] or 0
        bb, k, hbp, sf = s["bb"] or 0, s["k"] or 0, s["hbp"] or 0, s["sf"] or 0
        avg = h / ab if ab else None
        obp = (h + bb + hbp) / (ab + bb + hbp + sf) if (ab + bb + hbp + sf) else None
        slg = (h + d + 2*t + 3*hr) / ab if ab else None
        ops = (obp or 0) + (slg or 0) if obp is not None else None
        return {
            "pa": pa, "ab": ab, "avg": _r3(avg), "obp": _r3(obp), "slg": _r3(slg),
            "ops": _r3(ops), "hr": hr, "r": s["r"] or 0, "rbi": s["rbi"] or 0,
            "sb": s["sb"] or 0, "cs": s["cs"] or 0,
            "bb_pct": round(100 * bb / pa, 1) if pa else None,
            "k_pct": round(100 * k / pa, 1) if pa else None,
            "war": round(s["war"], 1) if s["war"] is not None else 0,
            "g": s["g"] or 0,
        }

    result = []
    team_g, dpw, salaries = _pap_context(conn, tid, year)
    for p in players:
        splits = bat.get(p["player_id"])
        pid = p["player_id"]
        if pid in twp_pids:
            fld = conn_fld.get(pid)
            pos = pos_map().get(fld, "DH") if fld else "DH"
        else:
            pos = pos_map().get(p["pos"], "?")
        s1 = splits.get(1) if splits else None
        war = s1["war"] if s1 and s1["war"] is not None else None
        _display_ovr = p["composite_score"] if p["composite_score"] is not None else (p["ovr"] or 0)
        result.append({
            "pid": pid, "name": p["name"], "age": p["age"],
            "ovr": _display_ovr, "pos": pos,
            "pos_order": pos_order().get(pos, 99),
            "surplus": round(p["surplus_yr1"] / _money_divisor(), 1) if p["surplus_yr1"] else 0,
            "pap": calc_pap(war, salaries.get(pid, 0), team_g, dpw),
            "is_two_way": pid in twp_pids,
            "status": "DL" if (p["is_on_dl"] or p["is_on_dl60"]) else
                      ("INJ" if p["injury_is_injured"] else
                       ("DFA" if p["designated_for_assignment"] else
                        ("WVR" if p["is_on_waivers"] else None))),
            "injury_days": p["injury_left"] if p["injury_is_injured"] and p["injury_left"] and p["injury_left"] < 1000 else None,
            "splits": {
                "1": _fmt_split(splits.get(1) if splits else None),
                "2": _fmt_split(splits.get(2) if splits else None),
                "3": _fmt_split(splits.get(3) if splits else None),
            }
        })
    result.sort(key=lambda x: (x["splits"]["1"]["war"] if x["splits"]["1"] else 0), reverse=True)
    return result


def get_roster_pitchers(team_id=None):
    """Pitchers with all 3 splits for the roster Pitchers tab."""
    state = _get_state()
    conn = get_db()
    year = state.get("stats_year", state["year"])
    tid = team_id or my_team_id()
    ed = _get_eval_date()

    players = conn.execute("""
        SELECT p.player_id, p.name, p.age, p.pos, p.role,
               ps.ovr, ps.surplus, ps.surplus_yr1,
               r.composite_score,
               p.injury_is_injured, p.injury_left, p.is_on_dl60,
               p.designated_for_assignment, p.is_on_waivers, p.is_on_dl
        FROM players p
        LEFT JOIN player_surplus ps ON p.player_id=ps.player_id AND ps.eval_date=?
        LEFT JOIN latest_ratings r ON p.player_id=r.player_id
        WHERE p.team_id=? AND p.level='1' AND p.role IN (11,12,13)
    """, (ed, tid)).fetchall()

    # Scoped to this team_id so a mid-season trade doesn't let the other
    # team's stint silently clobber this one (players keep a separate stats
    # row per team_id they played for in a given year).
    pit = {}  # pid -> {split_id -> dict}
    for r in conn.execute("""
        SELECT player_id, split_id, ip, g, gs, w, l, sv, era, k, bb, ha, war,
               hra, bf, hld, bs, qs, er, r AS runs, cg, sho, ir, irs
        FROM mlb_pitching_stats WHERE year=? AND split_id IN (1,2,3) AND team_id=?
    """, (year, tid)):
        pit.setdefault(r["player_id"], {})[r["split_id"]] = dict(r)

    # Detect two-way pitchers
    pitcher_pids = {p["player_id"] for p in players}
    twp_pids = set()
    if pitcher_pids:
        for r in conn.execute(
            "SELECT player_id FROM mlb_batting_stats WHERE year=? AND split_id=1 AND pa>=30 AND player_id IN ({})".format(
                ",".join("?" * len(pitcher_pids))),
            [year] + list(pitcher_pids)
        ).fetchall():
            twp_pids.add(r["player_id"])


    def _fmt_split(s):
        if not s:
            return None
        ip, bf = s["ip"] or 0, s["bf"] or 0
        k, bb, ha, hra = s["k"] or 0, s["bb"] or 0, s["ha"] or 0, s["hra"] or 0
        ir, irs = s["ir"] or 0, s["irs"] or 0
        whip = (bb + ha) / ip if ip else None
        irs_pct = round(100 * irs / ir, 1) if ir else None
        return {
            "ip": ip, "g": s["g"] or 0, "gs": s["gs"] or 0,
            "w": s["w"] or 0, "l": s["l"] or 0, "sv": s["sv"] or 0,
            "era": round(s["era"], 2) if s["era"] is not None else None,
            "whip": round(whip, 2) if whip else None,
            "k": k, "bb": bb, "hra": hra,
            "k_pct": round(100 * k / bf, 1) if bf else None,
            "bb_pct": round(100 * bb / bf, 1) if bf else None,
            "k_bb_pct": round(100 * (k - bb) / bf, 1) if bf else None,
            "war": round(s["war"], 1) if s["war"] is not None else 0,
            "hld": s["hld"] or 0, "bs": s["bs"] or 0,
            "qs": s["qs"] or 0, "irs_pct": irs_pct,
        }

    result = []
    team_g, dpw, salaries = _pap_context(conn, tid, year)
    for p in players:
        splits = pit.get(p["player_id"])
        pid = p["player_id"]
        role_str = ROLE_MAP.get(p["role"], "P")
        s1 = splits.get(1) if splits else None
        war = s1["war"] if s1 and s1["war"] is not None else None
        _display_ovr = p["composite_score"] if p["composite_score"] is not None else (p["ovr"] or 0)
        result.append({
            "pid": pid, "name": p["name"], "age": p["age"],
            "ovr": _display_ovr, "role": role_str,
            "role_order": pos_order().get(role_str, 99),
            "surplus": round(p["surplus_yr1"] / _money_divisor(), 1) if p["surplus_yr1"] else 0,
            "pap": calc_pap(war, salaries.get(pid, 0), team_g, dpw),
            "is_two_way": pid in twp_pids,
            "status": "DL" if (p["is_on_dl"] or p["is_on_dl60"]) else
                      ("INJ" if p["injury_is_injured"] else
                       ("DFA" if p["designated_for_assignment"] else
                        ("WVR" if p["is_on_waivers"] else None))),
            "injury_days": p["injury_left"] if p["injury_is_injured"] and p["injury_left"] and p["injury_left"] < 1000 else None,
            "splits": {
                "1": _fmt_split(splits.get(1) if splits else None),
                "2": _fmt_split(splits.get(2) if splits else None),
                "3": _fmt_split(splits.get(3) if splits else None),
            }
        })
    result.sort(key=lambda x: (x["splits"]["1"]["war"] if x["splits"]["1"] else 0), reverse=True)
    return result


def _r3(v):
    return round(v, 3) if v is not None else None


# Age at/above which a minor leaguer is a cut candidate on age grounds alone.
_CUT_AGE_THRESHOLD = 25
# FV ceiling below which a young (<= 24) player is a cut candidate on ability
# grounds — a fixed, absolute quality bar (FV 40 = replacement-level tier).
_CUT_FV_THRESHOLD = 40
# Potential is judged relative to this org's own system, not an absolute
# number — see _org_potential_percentile below.
_CUT_POTENTIAL_PERCENTILE = 0.10


def _percentile(sorted_vals, pct):
    """Value at the given percentile (0-1) of an already-sorted list."""
    if not sorted_vals:
        return None
    idx = int(len(sorted_vals) * pct)
    idx = min(idx, len(sorted_vals) - 1)
    return sorted_vals[idx]


def get_cut_candidates(team_id=None):
    """Minor-league cut candidates for one organization, split by scouting
    confidence.

    Flags a player if any of:
      - age >= 25 (too old to be a real prospect)
      - Low Work Ethic or Low Intelligence (makeup red flag)
      - age <= 24 with BOTH FV <= 40 AND potential in the bottom 20% of this
        org's own age <= 24 population (a stronger system has a higher bar;
        a weaker system flags more of its own players by comparison)

    Returns {"confirmed": [...], "needs_scouting": [...], "potential_cutoff": n}
    — "confirmed" is scouting accuracy High/Very High (trust the ratings),
    "needs_scouting" is Average/Low (same red flags, but the ratings
    themselves are unreliable so this should be treated as "go get a fresh
    scouting report" rather than an actual cut recommendation).
    """
    conn = get_db()
    conn.row_factory = None
    tid = team_id or my_team_id()
    ed = conn.execute("SELECT MAX(eval_date) FROM prospect_fv").fetchone()[0]
    ed_surplus = _get_eval_date()

    rows = conn.execute("""
        SELECT p.player_id, p.name, p.age, p.level, p.pos, p.role, p.team_id,
               r.int_, r.wrk_ethic, r.acc, r.composite_score, r.ceiling_score,
               r.true_ceiling, pf.fv, pf.fv_str, pf.bucket, t.name, ps.surplus,
               pf.prospect_surplus, pf.fv_continuous
        FROM players p
        LEFT JOIN latest_ratings r ON p.player_id = r.player_id
        LEFT JOIN prospect_fv pf ON pf.player_id = p.player_id AND pf.eval_date = ?
        LEFT JOIN teams t ON t.team_id = p.team_id
        LEFT JOIN player_surplus ps ON ps.player_id = p.player_id AND ps.eval_date = ?
        WHERE p.parent_team_id = ? AND p.level != '1' AND p.level NOT IN ('7', '8')
    """, (ed, ed_surplus, tid)).fetchall()

    # Org-relative potential floor: bottom 20% of this org's own age <= 24
    # players (by potential ceiling), computed before any filtering below.
    _u24_potentials = sorted(
        (true_ceil if true_ceil is not None else ceil_score)
        for (_p, _n, age, _l, _pos, _r, _at, _i, _we, _ac, _c, ceil_score,
             true_ceil, _fv, _fs, _pb, _an, _su, _psu, _fvc) in rows
        if age is not None and age <= 24
        and (true_ceil is not None or ceil_score is not None)
    )
    potential_cutoff = _percentile(_u24_potentials, _CUT_POTENTIAL_PERCENTILE)

    _pos_letter = pos_map()
    confirmed, needs_scouting = [], []
    for r in rows:
        (pid, name, age, level, pos, role, aff_tid, intel, wrk_ethic, acc,
         comp, ceil_score, true_ceil, fv, fv_str, pf_bucket, aff_name, surplus_raw,
         prospect_surplus_raw, fv_continuous) = r

        # Prefer the evaluation engine's own bucket (handles COF/SP/RP/CL
        # correctly); fall back to raw position/role for players with no
        # prospect_fv row (_display_pos ignores its 2nd arg, so this needs
        # to be resolved by hand rather than passed through).
        if pf_bucket:
            bucket_display = _display_pos(pf_bucket)
        elif role in ROLE_MAP:
            bucket_display = ROLE_MAP[role]
        else:
            letter = _pos_letter.get(pos, "?")
            bucket_display = "OF" if letter in ("LF", "RF") else letter

        reasons = []
        if age is not None and age >= _CUT_AGE_THRESHOLD:
            reasons.append(f"Age {age}")
        if wrk_ethic == "L":
            reasons.append("Low Work Ethic")
        if intel == "L":
            reasons.append("Low Intelligence")
        potential = true_ceil if true_ceil is not None else ceil_score
        is_bottom_20 = (age is not None and age <= 24 and potential_cutoff is not None
                        and fv is not None and fv <= _CUT_FV_THRESHOLD
                        and potential is not None and potential <= potential_cutoff)
        if is_bottom_20:
            _pct_label = f"{int(_CUT_POTENTIAL_PERCENTILE * 100)}%"
            reasons.append(f"Low FV ({fv_str or fv})")
            reasons.append(f"Bottom {_pct_label} Potential ({potential} ≤ {potential_cutoff} for this org)")

        if not reasons:
            continue

        _lvl_disp = level_map().get(str(level), str(level))
        _cur_s, _next_s, _three_s = _surplus_horizons_live(fv_continuous, age, _lvl_disp,
                                                            pf_bucket, ovr=comp, pot=potential)
        entry = {
            "pid": pid, "name": name, "age": age,
            "level": _lvl_disp,
            "bucket": bucket_display,
            "team_id": aff_tid,
            "team_name": aff_name or team_names_map().get(aff_tid, str(aff_tid)),
            "composite_score": comp, "potential": potential,
            "fv_str": fv_str, "acc": acc, "reasons": reasons,
            "surplus": round((surplus_raw if surplus_raw is not None else prospect_surplus_raw) / _money_divisor(), 1)
                       if (surplus_raw is not None or prospect_surplus_raw is not None) else None,
            "peak_surplus": _peak_surplus(fv_continuous, age, _lvl_disp, pf_bucket, ovr=comp, pot=potential),
            "current_year_surplus": _cur_s, "next_year_surplus": _next_s, "three_year_surplus": _three_s,
            "_is_bottom_20": is_bottom_20,
            "_is_personality": wrk_ethic == "L" or intel == "L",
        }
        if acc in ("H", "VH"):
            confirmed.append(entry)
        else:
            needs_scouting.append(entry)

    # Priority order: (1) bottom-20%-potential players first, (2) then
    # everyone else with a makeup red flag, (3) then anyone flagged on age
    # alone. Within each tier, more red flags and older age sort first.
    def _tier_key(e):
        tier = 0 if e["_is_bottom_20"] else (1 if e["_is_personality"] else 2)
        return (tier, -len(e["reasons"]), -(e["age"] or 0))

    confirmed.sort(key=_tier_key)
    needs_scouting.sort(key=_tier_key)
    for e in confirmed + needs_scouting:
        del e["_is_bottom_20"], e["_is_personality"]
    return {"confirmed": confirmed, "needs_scouting": needs_scouting,
            "potential_cutoff": potential_cutoff,
            "potential_percentile_pct": int(_CUT_POTENTIAL_PERCENTILE * 100)}


# Personality trait -> {value: (kind, label)}. "kind" is "buff" or "concern".
# Greed is inverted vs. the others: high greed is the concern, low is the buff.
_TRAIT_NOTES = {
    "wrk_ethic": {"H": ("buff", "Hard Worker"), "L": ("concern", "Low Work Ethic")},
    "int_":      {"H": ("buff", "High IQ"), "L": ("concern", "Low IQ")},
    "lead":      {"H": ("buff", "Leader"), "L": ("concern", "Low Leadership")},
    "loy":       {"H": ("buff", "Loyal"), "L": ("concern", "Low Loyalty")},
    "greed":     {"H": ("concern", "Greedy"), "L": ("buff", "Not Greedy")},
}


def _personality_notes(intel, wrk_ethic, lead, loy, greed):
    """Return (buffs, concerns) label lists from the five personality traits."""
    buffs, concerns = [], []
    for field, value in (("wrk_ethic", wrk_ethic), ("int_", intel),
                         ("lead", lead), ("loy", loy), ("greed", greed)):
        note = _TRAIT_NOTES.get(field, {}).get(value)
        if not note:
            continue
        kind, label = note
        (buffs if kind == "buff" else concerns).append(label)
    return buffs, concerns


def _bucket_for_display(pf_bucket, role, pos):
    """Resolve a display bucket the same way get_cut_candidates does."""
    if pf_bucket:
        return _display_pos(pf_bucket)
    if role in ROLE_MAP:
        return ROLE_MAP[role]
    letter = pos_map().get(pos, "?")
    return "OF" if letter in ("LF", "RF") else letter


def _weak_positions_for_org(tid):
    """Positions where this org ranks in the bottom half of the league,
    reusing the same WAR-based ranking that powers the depth chart."""
    year = _get_state().get("stats_year", _get_state()["year"])
    lg_rankings = _league_pos_rankings(get_db(), year)
    num_teams = max(len(v) for v in lg_rankings.values()) if lg_rankings else 0
    weak = set()
    for pos, tw in lg_rankings.items():
        for i, (tid2, _war) in enumerate(tw):
            if tid2 == tid:
                if i + 1 > num_teams / 2:
                    weak.add(pos)
                break
    return weak


def _fit_position(bucket, weak_positions):
    """Where a candidate would fit in the org, or '' if no obvious need.

    _league_pos_rankings ranks LF/RF separately, but a corner-outfield
    prospect's bucket only tells us "OF" (COF collapsed by _display_pos) —
    treat that as a fit if either corner is weak.
    """
    if not bucket:
        return ""
    if bucket == "OF":
        return "LF/RF" if ("LF" in weak_positions or "RF" in weak_positions) else ""
    return bucket if bucket in weak_positions else ""


def get_waiver_candidates(team_id=None):
    """Players currently on waivers, excluding this org's own players.

    Shows Ovr/Pot/FV (if evaluated), scouting accuracy, personality
    buffs/concerns, and where they'd fit in this org (blank if the org
    doesn't have an obvious need at that position).
    """
    conn = get_db()
    conn.row_factory = None
    tid = team_id or my_team_id()
    ed = conn.execute("SELECT MAX(eval_date) FROM prospect_fv").fetchone()[0]
    ed_surplus = _get_eval_date()
    weak_positions = _weak_positions_for_org(tid)

    rows = conn.execute("""
        SELECT p.player_id, p.name, p.age, p.level, p.pos, p.role, p.team_id,
               r.int_, r.wrk_ethic, r.lead, r.loy, r.greed, r.acc,
               r.composite_score, r.ceiling_score, r.true_ceiling,
               pf.fv, pf.fv_str, pf.bucket, t.name, ps.surplus, pf.prospect_surplus,
               pf.fv_continuous
        FROM players p
        LEFT JOIN latest_ratings r ON p.player_id = r.player_id
        LEFT JOIN prospect_fv pf ON pf.player_id = p.player_id AND pf.eval_date = ?
        LEFT JOIN teams t ON t.team_id = p.team_id
        LEFT JOIN player_surplus ps ON ps.player_id = p.player_id AND ps.eval_date = ?
        WHERE p.is_on_waivers = 1 AND p.team_id != ?
    """, (ed, ed_surplus, tid)).fetchall()

    out = []
    for r in rows:
        (pid, name, age, level, pos, role, cur_tid, intel, wrk_ethic, lead, loy,
         greed, acc, comp, ceil_score, true_ceil, fv, fv_str, pf_bucket, cur_name,
         surplus_raw, prospect_surplus_raw, fv_continuous) = r
        bucket = _bucket_for_display(pf_bucket, role, pos)
        potential = true_ceil if true_ceil is not None else ceil_score
        buffs, concerns = _personality_notes(intel, wrk_ethic, lead, loy, greed)
        level_disp = level_map().get(str(level)) or ("FA" if str(level)=="0" else str(level))
        _cur_s, _next_s, _three_s = _surplus_horizons_live(fv_continuous, age, level_disp,
                                                            pf_bucket, ovr=comp, pot=potential)
        out.append({
            "pid": pid, "name": name, "age": age,
            "level": level_disp,
            "bucket": bucket,
            "cur_team_id": cur_tid, "cur_team_name": cur_name or str(cur_tid),
            "composite_score": comp, "potential": potential, "fv_str": fv_str,
            "acc": acc, "buffs": buffs, "concerns": concerns,
            "fit": _fit_position(bucket, weak_positions),
            "surplus": round((surplus_raw if surplus_raw is not None else prospect_surplus_raw) / _money_divisor(), 1)
                       if (surplus_raw is not None or prospect_surplus_raw is not None) else None,
            "peak_surplus": _peak_surplus(fv_continuous, age, level_disp, pf_bucket, ovr=comp, pot=potential),
            "current_year_surplus": _cur_s, "next_year_surplus": _next_s, "three_year_surplus": _three_s,
        })
    out.sort(key=lambda e: -(e["composite_score"] or 0))
    return out


_FA_TOP_PCT = 0.05


# Nippon-affiliated team IDs: 320-333 are the current 12 NPB clubs plus the
# Central/Pacific League placeholders; 288-301 is an older/historical
# numbering of the same 14 entities (confirmed by matching counts). Used to
# exclude players drafted by an NPB team even when their nationality isn't
# Japanese (e.g. an American player historically drafted by Nankai).
_NIPPON_TEAM_IDS = tuple(range(288, 302)) + tuple(range(320, 334))


_FA_PROSPECT_AGE_MAX = 24
# Prospect (age <= 24) free agents are shown if they clear this FV bar,
# rather than a top-N%-of-pool cut — a fresh draft class landing in the
# pool shouldn't get squeezed out by an arbitrary percentage.
_FA_PROSPECT_MIN_FV = 30

# International-market amateur free agents (mostly 16-year-olds signing out
# of Latin America/Asia) go through a separate signing process from
# domestic/college free agents and shouldn't be mixed into the Free Agent
# Adds / Top Free Agent Prospects lists. There's no explicit "international"
# flag in the players table, but free_agent=1 + age<=17 + draft_eligible=0
# reliably identifies this pool: verified against a real exported
# "International Amateur FA" list, where every one of its 127 players
# matched exactly this combination in the DB (and vice versa, modulo a
# handful of players who signed/aged between the export and the DB
# snapshot — not a sign the heuristic is wrong).
_INTL_FA_AGE_MAX = 17


def get_free_agent_candidates(team_id=None):
    """Top 5% of free-agent hitters and top 5% of free-agent pitchers
    league-wide, by current Ovr (composite_score), plus a separate top 5%
    young-prospect cut (age <= 24, ranked by FV/potential instead of Ovr).

    Excludes:
      - nation_id 98 (Nippon/Japan) — historical NPB players seeded into
        the world database, not real signable free agents here
      - anyone drafted by an NPB team (see _NIPPON_TEAM_IDS), regardless
        of nationality
      - draft_eligible players — current/future amateur draft class, not
        actually signable as free agents

    International-market amateur free agents (age <= _INTL_FA_AGE_MAX) are
    segmented out of hitters/pitchers/young_*/clean_* entirely and returned
    separately as international_hitters/international_pitchers — they sign
    via a different process than domestic free agents and shouldn't be
    mixed into the domestic Free Agent Adds / Prospects lists.

    Shows Ovr/Pot/FV (if evaluated), scouting accuracy, personality
    buffs/concerns, and where they'd fit in this org.
    """
    conn = get_db()
    conn.row_factory = None
    tid = team_id or my_team_id()
    ed = conn.execute("SELECT MAX(eval_date) FROM prospect_fv").fetchone()[0]
    ed_surplus = _get_eval_date()
    weak_positions = _weak_positions_for_org(tid)

    _nippon_qs = ",".join("?" * len(_NIPPON_TEAM_IDS))
    _signable_where = f"""
        p.free_agent = 1 AND p.retired = 0 AND p.team_id = 0
        AND (p.nation_id IS NULL OR p.nation_id != 98)
        AND (p.draft_team_id IS NULL OR p.draft_team_id NOT IN ({_nippon_qs}))
        AND COALESCE(p.draft_eligible, 0) != 1
    """

    # Pool transparency counts — how many free agents exist at all vs. how
    # many are actually signable once Nippon/draft-pool players are excluded.
    total_fa = conn.execute(
        "SELECT COUNT(*) FROM players WHERE free_agent=1 AND retired=0 AND team_id=0"
    ).fetchone()[0]
    nippon_excluded = conn.execute(
        "SELECT COUNT(*) FROM players WHERE free_agent=1 AND retired=0 AND team_id=0 "
        f"AND (nation_id=98 OR draft_team_id IN ({_nippon_qs}))", _NIPPON_TEAM_IDS
    ).fetchone()[0]
    draft_pool_excluded = conn.execute(
        "SELECT COUNT(*) FROM players WHERE free_agent=1 AND retired=0 AND team_id=0 "
        "AND COALESCE(draft_eligible,0)=1"
    ).fetchone()[0]
    signable_pool = conn.execute(
        f"SELECT COUNT(*) FROM players p WHERE {_signable_where}", _NIPPON_TEAM_IDS
    ).fetchone()[0]

    rows = conn.execute(f"""
        SELECT p.player_id, p.name, p.age, p.level, p.pos, p.role,
               r.int_, r.wrk_ethic, r.lead, r.loy, r.greed, r.acc,
               r.composite_score, r.ceiling_score, r.true_ceiling,
               pf.fv, pf.fv_str, pf.bucket, ps.surplus, pf.prospect_surplus,
               pf.fv_continuous, fap.ask_raw,
               r.cntct, r.gap, r.pow, r.eye, r.stf, r.mov, r.ctrl, r.bats
        FROM players p
        LEFT JOIN latest_ratings r ON p.player_id = r.player_id
        LEFT JOIN prospect_fv pf ON pf.player_id = p.player_id AND pf.eval_date = ?
        LEFT JOIN player_surplus ps ON ps.player_id = p.player_id AND ps.eval_date = ?
        LEFT JOIN fa_asking_prices fap ON fap.player_id = p.player_id
        WHERE {_signable_where}
              AND r.composite_score IS NOT NULL
    """, (ed, ed_surplus, *_NIPPON_TEAM_IDS)).fetchall()

    from statsplusplus.config.ratings import norm_continuous as _normc
    from statsplusplus.evaluation.composite import compute_specialist_score, specialist_label as _spec_label
    from statsplusplus.evaluation.park_fit import (
        load_park_factors, compute_batter_park_fit,
        compute_pitcher_park_fit_from_stats, compute_pitcher_park_fit_from_tools,
    )
    ratings_scale = get_cfg().ratings_scale
    park = load_park_factors(get_cfg().league_dir)
    hitter_weights_by_bucket = load_tool_weights(get_cfg().league_dir).get("hitter", {}) if park else {}

    # Real observed GB%/K%/BB% (all levels, career-to-date) for every free
    # agent pitcher in this pool — preferred over the scouting-tool proxy
    # whenever there's a meaningful sample (150+ batters faced, ~40 IP).
    # Falls back to compute_pitcher_park_fit_from_tools() below the
    # threshold, same as an uploaded CSV with no game logs at all.
    _PARK_FIT_BF_THRESHOLD = 150
    pitcher_pids = [r[0] for r in rows if r[5] in ROLE_MAP]
    pitcher_stats = {}
    lg_gb_pct = lg_k_pct = lg_bb_pct = None
    if park and pitcher_pids:
        pid_qs = ",".join("?" * len(pitcher_pids))
        for row in conn.execute(
            f"SELECT player_id, SUM(gb), SUM(fb), SUM(k), SUM(bb), SUM(bf) "
            f"FROM pitching_stats WHERE player_id IN ({pid_qs}) GROUP BY player_id",
            pitcher_pids,
        ).fetchall():
            p_pid, s_gb, s_fb, s_k, s_bb, s_bf = row
            if s_bf and s_bf >= _PARK_FIT_BF_THRESHOLD:
                pitcher_stats[p_pid] = {
                    "gb_pct": s_gb / (s_gb + s_fb) if (s_gb or 0) + (s_fb or 0) > 0 else None,
                    "k_pct": s_k / s_bf, "bb_pct": s_bb / s_bf,
                }
        lg = conn.execute(
            "SELECT SUM(gb), SUM(fb), SUM(k), SUM(bb), SUM(bf) FROM mlb_pitching_stats"
        ).fetchone()
        if lg and lg[4]:
            lg_gb, lg_fb, lg_k, lg_bb, lg_bf = lg
            lg_gb_pct = lg_gb / (lg_gb + lg_fb) if (lg_gb or 0) + (lg_fb or 0) > 0 else None
            lg_k_pct, lg_bb_pct = lg_k / lg_bf, lg_bb / lg_bf

    hitters, pitchers = [], []
    intl_hitters, intl_pitchers = [], []
    for r in rows:
        (pid, name, age, level, pos, role, intel, wrk_ethic, lead, loy, greed,
         acc, comp, ceil_score, true_ceil, fv, fv_str, pf_bucket, surplus_raw,
         prospect_surplus_raw, fv_continuous, ask_raw,
         cntct, gap, pow_, eye, stf, mov, ctrl, bats) = r
        bucket = _bucket_for_display(pf_bucket, role, pos)
        potential = true_ceil if true_ceil is not None else ceil_score
        buffs, concerns = _personality_notes(intel, wrk_ethic, lead, loy, greed)
        level_disp = level_map().get(str(level)) or ("FA" if str(level)=="0" else str(level))
        is_pitcher = role in ROLE_MAP
        if is_pitcher:
            _tools = {"stuff": _normc(stf, ratings_scale), "movement": _normc(mov, ratings_scale),
                      "control": _normc(ctrl, ratings_scale)}
        else:
            _tools = {"contact": _normc(cntct, ratings_scale), "gap": _normc(gap, ratings_scale),
                      "power": _normc(pow_, ratings_scale), "eye": _normc(eye, ratings_scale)}
        spec_score = compute_specialist_score(_tools, is_pitcher)

        park_fit = None
        if park:
            if is_pitcher:
                obs = pitcher_stats.get(pid)
                if obs and obs["gb_pct"] is not None and lg_gb_pct is not None:
                    park_fit = compute_pitcher_park_fit_from_stats(
                        obs["gb_pct"], obs["k_pct"], obs["bb_pct"],
                        lg_gb_pct, lg_k_pct, lg_bb_pct, park)
                else:
                    park_fit = compute_pitcher_park_fit_from_tools(_tools, park)
            else:
                _hw = hitter_weights_by_bucket.get(bucket, hitter_weights_by_bucket.get("COF", {}))
                park_fit = compute_batter_park_fit(_tools, bats, _hw, park)

        _cur_s, _next_s, _three_s = _surplus_horizons_live(fv_continuous, age, level_disp,
                                                            pf_bucket, ovr=comp, pot=potential)
        entry = {
            "pid": pid, "name": name, "age": age,
            "level": level_disp,
            "bucket": bucket, "composite_score": comp, "potential": potential,
            "fv": fv, "fv_str": fv_str, "acc": acc, "buffs": buffs, "concerns": concerns,
            "fit": _fit_position(bucket, weak_positions),
            "surplus": round((surplus_raw if surplus_raw is not None else prospect_surplus_raw) / _money_divisor(), 1)
                       if (surplus_raw is not None or prospect_surplus_raw is not None) else None,
            "peak_surplus": _peak_surplus(fv_continuous, age, level_disp, pf_bucket, ovr=comp, pot=potential),
            "current_year_surplus": _cur_s, "next_year_surplus": _next_s, "three_year_surplus": _three_s,
            "ask": ask_raw or "MiLC",
            "specialist_score": spec_score, "specialist_label": _spec_label(spec_score),
            "park_fit": park_fit,
        }
        if age is not None and age <= _INTL_FA_AGE_MAX:
            (intl_pitchers if is_pitcher else intl_hitters).append(entry)
        else:
            (pitchers if is_pitcher else hitters).append(entry)

    def _top_pct(pool, key, min_count=1):
        pool = sorted(pool, key=key)
        n = max(min_count, int(len(pool) * _FA_TOP_PCT)) if pool else 0
        return pool[:n]

    def _ovr_key(e):
        return -(e["composite_score"] or 0)

    def _prospect_key(e):
        # FV is the more authoritative grade when available; fall back to
        # raw potential ceiling for players not in prospect_fv.
        return -(e["fv"] if e["fv"] is not None else (e["potential"] or 0))

    young_hitters = [e for e in hitters if e["age"] is not None and e["age"] <= _FA_PROSPECT_AGE_MAX]
    young_pitchers = [e for e in pitchers if e["age"] is not None and e["age"] <= _FA_PROSPECT_AGE_MAX]
    clean_hitters = [e for e in hitters if not e["concerns"]]
    clean_pitchers = [e for e in pitchers if not e["concerns"]]
    clean_young_hitters = [e for e in young_hitters if not e["concerns"]]
    clean_young_pitchers = [e for e in young_pitchers if not e["concerns"]]

    def _fv_min(pool):
        qualifying = [e for e in pool if e["fv"] is not None and e["fv"] >= _FA_PROSPECT_MIN_FV]
        return sorted(qualifying, key=_prospect_key)

    # No FV floor here — these are 15-17yo amateurs being browsed as a pool,
    # not a curated "worth signing now" cut, so an FV threshold tuned for
    # domestic prospects doesn't apply.
    international_hitters = sorted(intl_hitters, key=_prospect_key)
    international_pitchers = sorted(intl_pitchers, key=_prospect_key)

    return {"hitters": _top_pct(hitters, _ovr_key), "pitchers": _top_pct(pitchers, _ovr_key),
            "young_hitters": _fv_min(young_hitters),
            "young_pitchers": _fv_min(young_pitchers),
            "clean_hitters": _top_pct(clean_hitters, _ovr_key, min_count=5),
            "clean_pitchers": _top_pct(clean_pitchers, _ovr_key, min_count=5),
            "clean_young_hitters": _fv_min(clean_young_hitters),
            "clean_young_pitchers": _fv_min(clean_young_pitchers),
            "international_hitters": international_hitters,
            "international_pitchers": international_pitchers,
            "intl_age_max": _INTL_FA_AGE_MAX,
            "prospect_min_fv": _FA_PROSPECT_MIN_FV,
            "prospect_age_max": _FA_PROSPECT_AGE_MAX,
            "top_pct": int(_FA_TOP_PCT * 100),
            "total_fa": total_fa, "nippon_excluded": nippon_excluded,
            "draft_pool_excluded": draft_pool_excluded, "signable_pool": signable_pool}


def get_farm(team_id=None):
    conn = get_db()
    tid = team_id or my_team_id()
    ed = conn.execute("SELECT MAX(eval_date) FROM prospect_fv").fetchone()[0]

    rows = conn.execute("""
        SELECT p.name, p.age, p.level, pf.fv, pf.fv_str, pf.bucket, pf.prospect_surplus, p.player_id, p.pos,
               r.composite_score, r.ceiling_score, pf.risk
        FROM prospect_fv pf
        JOIN players p ON pf.player_id=p.player_id
        LEFT JOIN latest_ratings r ON pf.player_id=r.player_id
        WHERE pf.eval_date=? AND (p.parent_team_id=? OR (p.team_id=? AND p.level='1'))
              AND p.age <= 25
        ORDER BY pf.fv DESC, p.age ASC
    """, (ed, tid, tid)).fetchall()

    def sort_key(r):
        fv_val = r[3] + (0.1 if r[4].endswith("+") else 0)
        return (-fv_val, -(r[6] or 0))

    rows = sorted(rows, key=sort_key)[:15]
    return [{"rank": i + 1, "name": r[0], "age": r[1],
             "level": level_map().get(str(r[2]), str(r[2])),
             "fv": r[3], "fv_str": r[4],
             "bucket": _display_pos(r[5], r[8]),
             "pos_order": pos_order().get(_display_pos(r[5], r[8]), 99),
             "surplus": round(r[6] / _money_divisor(), 1) if r[6] else 0,
             "pid": r[7],
             "composite_score": r[9], "ceiling_score": r[10],
             "risk": r[11]}
            for i, r in enumerate(rows)]


def get_team_stats(team_id):
    state = _get_state()
    year = state.get("stats_year", state["year"])
    conn = get_db()

    bat_rows = conn.execute(
        "SELECT team_id, avg, obp, slg, ops, hr, r, bb_pct, k_pct, iso FROM team_batting_stats WHERE year=? AND split_id=1", (year,)
    ).fetchall()
    pit_rows = conn.execute(
        "SELECT team_id, era, fip, k_pct, bb_pct, hra, r, ip, bb, k, ha FROM team_pitching_stats WHERE year=? AND split_id=1", (year,)
    ).fetchall()

    n = len(bat_rows)

    def rankings(rows, specs, tid):
        out = {}
        for label, idx, low in specs:
            vals = sorted([r[idx] for r in rows if r[idx] is not None], reverse=not low)
            my = next((r[idx] for r in rows if r[0] == tid), None)
            out[label] = {"val": my, "rank": (vals.index(my) + 1) if my in vals else n, "n": n}
        return out

    bat = rankings(bat_rows, [
        ("AVG",1,False),("OBP",2,False),("SLG",3,False),("OPS",4,False),
        ("HR",5,False),("R",6,False),("BB%",7,False),("K%",8,True),("ISO",9,False),
    ], team_id)

    pit_derived = []
    for r in pit_rows:
        tid, era, fip, kp, bbp, hra, ra, ip, bb, k, ha = r
        whip = (bb + ha) / ip if ip else 99
        k9 = k * 9 / ip if ip else 0
        bb9 = bb * 9 / ip if ip else 99
        hr9 = hra * 9 / ip if ip else 99
        pit_derived.append((tid, era, fip, kp, bbp, hra, ra, whip, k9, bb9, hr9))

    pit = rankings(pit_derived, [
        ("ERA",1,True),("FIP",2,True),("K%",3,False),("BB%",4,True),
        ("RA",6,True),("WHIP",7,True),("K/9",8,False),("BB/9",9,True),("HR/9",10,True),
    ], team_id)

    return {"batting": bat, "pitching": pit}


def get_contracts(team_id):
    conn = get_db()
    ed = _get_eval_date()

    rows = conn.execute("""
        SELECT c.player_id, p.name, c.years, c.current_year,
               c.salary_0, c.salary_1, c.salary_2, c.salary_3, c.salary_4,
               c.salary_5, c.salary_6, c.salary_7, c.salary_8, c.salary_9,
               c.salary_10, c.salary_11, c.salary_12, c.salary_13, c.salary_14,
               c.no_trade, c.last_year_team_option, c.last_year_player_option,
               ps.surplus, c.is_major, ps.fv, ps.age, ps.bucket
        FROM contracts c
        JOIN players p ON c.player_id = p.player_id
        LEFT JOIN player_surplus ps ON c.player_id = ps.player_id AND ps.eval_date = ?
        WHERE 1=1
          {_CONTRACT_ORG_SQL}
        ORDER BY c.salary_0 DESC
    """.format(_CONTRACT_ORG_SQL=_CONTRACT_ORG_SQL), (ed, *_contract_org_params(team_id))).fetchall()

    out = []
    for r in rows:
        pid, name = r[0], r[1]
        years, cur_yr = r[2], r[3]
        salaries = [r[4 + i] for i in range(15)]
        ntc, to, po = r[19], r[20], r[21]
        surplus, is_major = r[22], r[23]
        ps_fv, ps_age, ps_bucket = r[24], r[25], r[26]
        cur_sal = salaries[cur_yr] if cur_yr < len(salaries) else salaries[0]
        yrs_left = max(years - cur_yr, 1)
        total_left = sum(salaries[cur_yr:years]) if cur_yr < years else cur_sal
        out.append({
            "pid": pid, "name": name,
            "salary": cur_sal, "years_left": yrs_left, "total_left": total_left,
            "ntc": ntc, "to": to, "po": po,
            "surplus": round(surplus / _money_divisor(), 1) if surplus else 0,
            "is_major": is_major,
            # For an established MLB player, "FV" and current Ovr are the
            # same number in this table (fv_calc.py stores ovr twice) — an
            # already-proven veteran's true talent IS his FV, there's no
            # separate development ceiling to project toward. That's the
            # right input for a forward-looking peak-year projection.
            "peak_surplus": _peak_surplus(ps_fv, ps_age, "MLB", ps_bucket, ovr=ps_fv),
        })

    display = [c for c in out if c["is_major"] and (c["salary"] > DEFAULT_MINIMUM_SALARY or c["years_left"] > 1)]
    display.sort(key=lambda x: -x["salary"])
    total_payroll = sum(c["salary"] for c in out if c["is_major"])

    from contract_value import contract_surplus_horizons as _csh
    _game_year = get_cfg().year
    _league_dir = get_cfg().league_dir
    for c in display:
        try:
            cur_s, next_s, three_s = _csh(c["pid"], _game_year, league_dir=_league_dir)
        except Exception:
            cur_s, next_s, three_s = None, None, None
        c["current_year_surplus"] = round(cur_s / _money_divisor(), 1) if cur_s is not None else None
        c["next_year_surplus"] = round(next_s / _money_divisor(), 1) if next_s is not None else None
        c["three_year_surplus"] = round(three_s / _money_divisor(), 1) if three_s is not None else None

    return display, total_payroll


def get_payroll_summary(team_id):
    """Committed payroll by year with per-player breakdown, including arb projections."""
    state = _get_state()
    year = state.get("stats_year", state["year"])
    conn = get_db()
    rows = conn.execute("""
        SELECT c.player_id, p.name, c.years, c.current_year,
               c.salary_0, c.salary_1, c.salary_2, c.salary_3, c.salary_4,
               c.salary_5, c.salary_6, c.salary_7, c.salary_8, c.salary_9,
               c.salary_10, c.salary_11, c.salary_12, c.salary_13, c.salary_14,
               c.last_year_team_option, c.last_year_player_option, c.no_trade
        FROM contracts c
        JOIN players p ON c.player_id = p.player_id
        WHERE c.is_major = 1
          {_CONTRACT_ORG_SQL}
    """.format(_CONTRACT_ORG_SQL=_CONTRACT_ORG_SQL), (*_contract_org_params(team_id),)).fetchall()

    # Project salaries for 1yr contract players using arb model (no non-tender gate)
    from contract_value import _resolve
    from statsplusplus.evaluation.arb import estimate_control as _estimate_control_raw
    from statsplusplus.config.league_config import league_minimum as _lm_fn; from statsplusplus.evaluation.war import aging_mult
    _lmin = league_minimum()
    _perp = get_cfg().perpetual_arb
    def _estimate_control(conn, pid, age, sal, bucket=None):
        return _estimate_control_raw(conn, pid, age, sal, min_sal=_lmin, perpetual_arb=_perp, bucket=bucket)
    from statsplusplus.data import db as _scripts_db
    import math
    cv_conn = _scripts_db.get_connection(get_cfg().league_dir)
    lmin = league_minimum()
    projections = {}  # pid -> [(year_offset, salary), ...]
    for r in rows:
        if r[2] != 1:  # multi-year contract, skip
            continue
        pid, sal = r[0], r[4]
        try:
            res = _resolve(cv_conn, str(pid))
            if not res:
                continue
            _, _, age, ovr, pot, bucket = res
            est = _estimate_control(cv_conn, pid, age, sal)
            ctrl, _, pre_arb = est
            if not ctrl or ctrl <= 1:
                continue
            from statsplusplus.evaluation.arb import arb_salary as _arb_salary
            proj = []
            prev_sal = sal
            for i in range(1, ctrl):
                if i < pre_arb:
                    s = lmin
                else:
                    arb_yr = i - pre_arb + 1  # 1-indexed
                    s = _arb_salary(ovr, bucket, arb_yr, prev_sal, lmin)
                proj.append((i, s))
                prev_sal = s
            projections[pid] = proj
        except Exception:
            pass
    cv_conn.close()

    # Real per-year figures from an uploaded "Team Salary" export override the
    # formula projection above wherever they cover a given calendar year.
    uploaded_by_pid = {}
    try:
        for r in conn.execute("SELECT player_id, year, amount FROM salary_estimates"):
            uploaded_by_pid.setdefault(r["player_id"], {})[r["year"]] = r["amount"]
    except Exception:
        pass

    horizon = 6
    future_years = [year + i for i in range(horizon)]
    min_sal = get_cfg().minimum_salary
    players = []
    totals = [0] * horizon
    for r in rows:
        pid, name = r[0], r[1]
        yrs_total, cur_yr = r[2], r[3]
        sals = [r[4 + i] for i in range(15)]
        to, po, ntc = r[19], r[20], r[21]

        proj = projections.get(pid)
        proj_map = {i: s for i, s in proj} if proj else {}
        by_year = []
        pid_uploaded = uploaded_by_pid.get(pid)
        for i in range(horizon):
            contract_yr = cur_yr + i
            abs_year = year + i
            if pid_uploaded and abs_year in pid_uploaded:
                sal = pid_uploaded[abs_year]
                by_year.append({"sal": sal, "option": None, "projected": True})
                totals[i] += sal
            elif i in proj_map:
                by_year.append({"sal": proj_map[i], "option": None, "projected": True})
                totals[i] += proj_map[i]
            elif contract_yr < yrs_total:
                is_option = (contract_yr == yrs_total - 1) and (to or po)
                sal = sals[contract_yr]
                by_year.append({"sal": sal, "option": "TO" if to and is_option else "PO" if po and is_option else None, "projected": False})
                totals[i] += sal
            else:
                by_year.append(None)
        if not any(s for s in by_year if s):
            continue
        players.append({"pid": pid, "name": name, "by_year": by_year, "ntc": ntc})
    players.sort(key=lambda p: -(p["by_year"][0]["sal"] if p["by_year"][0] else 0))
    return {"years": future_years, "players": players, "totals": totals}

def get_roster_summary(team_id):
    state = _get_state()
    year = state.get("stats_year", state["year"])
    conn = get_db()
    rows = conn.execute("""
        SELECT p.role, p.age FROM players p
        WHERE p.team_id=? AND p.level='1'
          AND (p.player_id IN (SELECT player_id FROM mlb_batting_stats WHERE year=? AND split_id=1)
            OR p.player_id IN (SELECT player_id FROM mlb_pitching_stats WHERE year=? AND split_id=1))
    """, (team_id, year, year)).fetchall()

    groups = {"SP": [], "RP": [], "Pos": []}
    for role, age in rows:
        if role == 11:
            groups["SP"].append(age)
        elif role in (12, 13):
            groups["RP"].append(age)
        else:
            groups["Pos"].append(age)

    return {k: {"count": len(v), "avg_age": round(sum(v) / len(v), 1) if v else 0}
            for k, v in groups.items()}


def get_upcoming_fa(team_id):
    conn = get_db()
    ed = _get_eval_date()

    rows = conn.execute("""
        SELECT c.player_id, p.name, p.age, c.years, c.current_year,
               c.salary_0, ps.surplus, ps.ovr, ps.bucket
        FROM contracts c
        JOIN players p ON c.player_id = p.player_id
        LEFT JOIN player_surplus ps ON c.player_id = ps.player_id AND ps.eval_date = ?
        WHERE c.is_major = 1
          {_CONTRACT_ORG_SQL}
    """.format(_CONTRACT_ORG_SQL=_CONTRACT_ORG_SQL), (ed, *_contract_org_params(team_id))).fetchall()

    out = []
    for pid, name, age, years, cur_yr, sal, surplus, ovr, bucket in rows:
        if not ovr:
            continue
        yrs_left = max(years - cur_yr, 1)
        if yrs_left > 2:
            continue
        if years == 1 and age < 30:
            continue
        out.append({
            "pid": pid, "name": name, "age": age,
            "pos": _display_pos(bucket) if bucket else "?",
            "yrs_left": yrs_left, "salary": sal,
            "surplus": round(surplus / _money_divisor(), 1) if surplus else 0,
            "ovr": ovr or 0,
        })
    out.sort(key=lambda x: (-x["ovr"], x["yrs_left"]))
    return out


def get_surplus_leaders(team_id):
    conn = get_db()
    ed = _get_eval_date()

    mlb = conn.execute("""
        SELECT ps.player_id, p.name, ps.bucket, ps.surplus, 'MLB' as src
        FROM player_surplus ps JOIN players p ON ps.player_id = p.player_id
        WHERE ps.eval_date = ? AND ps.team_id = ?
    """, (ed, team_id)).fetchall()

    farm = conn.execute("""
        SELECT pf.player_id, p.name, pf.bucket, pf.prospect_surplus, 'Farm' as src
        FROM prospect_fv pf JOIN players p ON pf.player_id = p.player_id
        WHERE pf.eval_date = ? AND p.parent_team_id = ? AND p.level != '1'
    """, (ed, team_id)).fetchall()

    combined = []
    for pid, name, bucket, surplus, src in list(mlb) + list(farm):
        if not surplus:
            continue
        combined.append({"pid": pid, "name": name,
                         "pos": _display_pos(bucket) if bucket else "?",
                         "surplus": round(surplus / _money_divisor(), 1), "src": src})
    combined.sort(key=lambda x: -x["surplus"])
    return combined[:15]


def get_age_distribution(team_id):
    state = _get_state()
    year = state.get("stats_year", state["year"])
    conn = get_db()

    mlb_breaks = [("≤25", 0, 25), ("26-29", 26, 29), ("30-33", 30, 33), ("34+", 34, 99)]
    farm_breaks = [("≤20", 0, 20), ("21-23", 21, 23), ("24+", 24, 99)]

    def bucket(ages, breaks):
        out = {label: 0 for label, _, _ in breaks}
        for (age,) in ages:
            for label, lo, hi in breaks:
                if lo <= age <= hi:
                    out[label] += 1
                    break
        return out

    def pcts(counts):
        total = sum(counts.values())
        return {k: round(v / total * 100, 1) if total else 0 for k, v in counts.items()}

    mlb_ages = conn.execute("""
        SELECT p.age FROM players p
        WHERE p.team_id=? AND p.level='1'
          AND (p.player_id IN (SELECT player_id FROM mlb_batting_stats WHERE year=? AND split_id=1)
            OR p.player_id IN (SELECT player_id FROM mlb_pitching_stats WHERE year=? AND split_id=1))
    """, (team_id, year, year)).fetchall()

    ed = conn.execute("SELECT MAX(eval_date) FROM prospect_fv").fetchone()[0]
    farm_ages = conn.execute("""
        SELECT p.age FROM prospect_fv pf
        JOIN players p ON pf.player_id = p.player_id
        WHERE pf.eval_date=? AND p.parent_team_id=? AND p.level!='1' AND pf.fv >= 40
    """, (ed, team_id)).fetchall()

    mlb = bucket(mlb_ages, mlb_breaks)
    farm = bucket(farm_ages, farm_breaks)

    mlb_tids = mlb_team_ids()
    all_mlb = conn.execute("""
        SELECT p.team_id, p.age FROM players p
        WHERE p.level='1'
          AND (p.player_id IN (SELECT player_id FROM mlb_batting_stats WHERE year=? AND split_id=1)
            OR p.player_id IN (SELECT player_id FROM mlb_pitching_stats WHERE year=? AND split_id=1))
    """, (year, year)).fetchall()

    all_farm = conn.execute("""
        SELECT COALESCE(NULLIF(p.parent_team_id,0), p.team_id), p.age FROM prospect_fv pf
        JOIN players p ON pf.player_id = p.player_id
        WHERE pf.eval_date=? AND pf.fv >= 40
    """, (ed,)).fetchall()

    def league_avg_pcts(rows, breaks, tid_idx):
        teams = defaultdict(list)
        for row in rows:
            tid = row[tid_idx]
            if tid in mlb_tids:
                teams[tid].append((row[1],))
        if not teams:
            return {label: 0 for label, _, _ in breaks}
        team_pcts = [pcts(bucket(ages, breaks)) for ages in teams.values()]
        return {k: round(sum(tp[k] for tp in team_pcts) / len(team_pcts), 1)
                for k in team_pcts[0]}

    lg_mlb = league_avg_pcts(all_mlb, mlb_breaks, 0)
    lg_farm = league_avg_pcts(all_farm, farm_breaks, 0)

    return {"mlb": mlb, "farm": farm, "lg_mlb": lg_mlb, "lg_farm": lg_farm}



def get_record_breakdown(team_id):
    """Record splits: home/away, vs division, 1-run games, last 10, streak."""
    state = _get_state()
    year = state.get("stats_year", state["year"])
    conn = get_db()
    rows = conn.execute("""
        SELECT home_team, away_team, runs0, runs1
        FROM games
        WHERE (home_team=? OR away_team=?) AND date LIKE ? AND played=1 AND game_type=0
        ORDER BY date, game_id
    """, (team_id, team_id, f"{year}%")).fetchall()
    if not rows:
        return None

    # Find division mates
    div_teams = set()
    for div, teams in get_cfg().divisions.items():
        if team_id in teams:
            div_teams = set(teams) - {team_id}
            break

    splits = {
        "overall": [0, 0], "home": [0, 0], "away": [0, 0],
        "vs_div": [0, 0], "one_run": [0, 0],
    }
    results = []  # ordered W/L booleans
    for home, away, r0, r1 in rows:
        is_home = home == team_id
        opp = away if is_home else home
        won = (r1 > r0) if is_home else (r0 > r1)
        margin = abs(r1 - r0)
        idx = 0 if won else 1
        splits["overall"][idx] += 1
        splits["home" if is_home else "away"][idx] += 1
        if opp in div_teams:
            splits["vs_div"][idx] += 1
        if margin == 1:
            splits["one_run"][idx] += 1
        results.append(won)

    # Last 10
    last10 = results[-10:]
    l10_w = sum(last10)
    l10_l = len(last10) - l10_w

    # Streak
    streak_type = results[-1] if results else True
    streak_len = 0
    for r in reversed(results):
        if r == streak_type:
            streak_len += 1
        else:
            break
    streak = f"{'W' if streak_type else 'L'}{streak_len}"

    return {
        "overall": splits["overall"],
        "home": splits["home"],
        "away": splits["away"],
        "vs_div": splits["vs_div"],
        "one_run": splits["one_run"],
        "l10": [l10_w, l10_l],
        "streak": streak,
    }


def get_recent_games(team_id, n=10):
    """Last n games for a team with W/L, score, opponent."""
    state = _get_state()
    year = state.get("stats_year", state["year"])
    conn = get_db()
    rows = conn.execute("""
        SELECT g.date, g.home_team, g.away_team, g.runs0, g.runs1,
               g.winning_pitcher, g.losing_pitcher, g.save_pitcher,
               th.name as home_name, ta.name as away_name
        FROM games g
        JOIN teams th ON g.home_team = th.team_id
        JOIN teams ta ON g.away_team = ta.team_id
        WHERE (g.home_team=? OR g.away_team=?) AND g.played=1 AND g.game_type=0
          AND g.date LIKE ?
        ORDER BY g.date DESC, g.game_id DESC LIMIT ?
    """, (team_id, team_id, f"{year}%", n)).fetchall()

    # Collect pitcher IDs and names
    pids = set()
    for r in rows:
        for i in (5, 6, 7):
            if r[i]:
                pids.add(r[i])
    pname = {}
    if pids:
        ph = ",".join("?" * len(pids))
        for p in conn.execute(f"SELECT player_id, name FROM players WHERE player_id IN ({ph})", list(pids)).fetchall():
            pname[p[0]] = p[1]

    # Running W/L/SV from game history for these pitchers (only their games)
    if pids:
        ph2 = ",".join("?" * len(pids))
        pid_list = list(pids)
        all_games = conn.execute(f"""
            SELECT date, winning_pitcher, losing_pitcher, save_pitcher
            FROM games WHERE date LIKE ? AND played=1 AND game_type=0
              AND (winning_pitcher IN ({ph2}) OR losing_pitcher IN ({ph2}) OR save_pitcher IN ({ph2}))
            ORDER BY date, game_id
        """, [f"{year}%"] + pid_list * 3).fetchall()
    else:
        all_games = []

    # Build cumulative counts keyed by (pid, date) -> count after that date's games
    from collections import defaultdict
    pw, pl, ps = defaultdict(int), defaultdict(int), defaultdict(int)
    pw_at, pl_at, ps_at = {}, {}, {}  # (pid, date) -> running total
    for g in all_games:
        d = g[0]
        if g[1] in pids:
            pw[g[1]] += 1
        if g[2] in pids:
            pl[g[2]] += 1
        if g[3] and g[3] in pids:
            ps[g[3]] += 1
        # Update running totals for any pitcher who appeared in this game
        for slot in (g[1], g[2], g[3]):
            if slot and slot in pids:
                pw_at[(slot, d)] = pw[slot]
                pl_at[(slot, d)] = pl[slot]
                ps_at[(slot, d)] = ps[slot]

    def _pfmt(pid, date, mode):
        if not pid or pid not in pname:
            return None
        name = pname[pid]
        if mode == "sv":
            stat = f"({ps_at.get((pid, date), 0)})"
        else:
            w = pw_at.get((pid, date), 0)
            l = pl_at.get((pid, date), 0)
            stat = f"({w}-{l})"
        return {"pid": pid, "name": name, "stat": stat}

    out = []
    for r in rows:
        home = r[1] == team_id
        # runs0=away, runs1=home
        team_runs = r[4] if home else r[3]
        opp_runs = r[3] if home else r[4]
        opp_name = r[9] if home else r[8]
        opp_tid = r[2] if home else r[1]
        wl = "W" if team_runs > opp_runs else "L"
        out.append({
            "date": r[0], "home": home,
            "opp": opp_name, "opp_tid": opp_tid,
            "team_runs": team_runs, "opp_runs": opp_runs,
            "wl": wl,
            "wp": _pfmt(r[5], r[0], "wl"),
            "lp": _pfmt(r[6], r[0], "wl"),
            "sv": _pfmt(r[7], r[0], "sv") if r[7] else "",
        })
    return out


def get_stat_leaders(team_id):
    """Top 3 players in key batting/pitching categories for a team."""
    state = _get_state()
    year = state.get("stats_year", state["year"])
    conn = get_db()

    # Team games for MLB qualification thresholds
    tip = conn.execute("SELECT ip FROM team_pitching_stats WHERE team_id=? AND year=? AND split_id=1",
                       (team_id, year)).fetchone()
    team_g = round(tip[0] / 9) if tip and tip[0] else 0
    pa_qual = round(3.1 * team_g)   # MLB batting: 3.1 PA per team game
    ip_qual = round(1.0 * team_g)   # MLB pitching: 1.0 IP per team game

    bat_rows = conn.execute("""
        SELECT b.player_id, p.name, b.ab, b.h, b.hr, b.rbi, b.sb, b.war,
               b.pa, b.bb, b.d, b.t
        FROM mlb_batting_stats b JOIN players p ON b.player_id = p.player_id
        WHERE b.year=? AND b.split_id=1 AND b.team_id=?
    """, (year, team_id)).fetchall()

    pit_rows = conn.execute("""
        SELECT b.player_id, p.name, b.era, b.ip, b.k, b.war, b.w, b.l, b.sv, b.bb, b.ha
        FROM mlb_pitching_stats b JOIN players p ON b.player_id = p.player_id
        WHERE b.year=? AND b.split_id=1 AND b.team_id=?
    """, (year, team_id)).fetchall()

    def top3(rows, key, fmt, low=False):
        pool = [(r, key(r)) for r in rows if key(r) is not None]
        pool.sort(key=lambda x: x[1], reverse=not low)
        return [{"pid": r[0], "name": r[1], "val": fmt(v)} for r, v in pool[:3]]

    pa_ok = lambda r: (r[8] or 0) >= pa_qual
    ip_ok = lambda r: (r[3] or 0) >= ip_qual

    batting = {
        "HR":  top3(bat_rows, lambda r: r[4], str),
        "RBI": top3(bat_rows, lambda r: r[5], str),
        "AVG": top3(bat_rows, lambda r: r[3]/r[2] if pa_ok(r) and r[2] else None, lambda v: f"{v:.3f}"),
        "OPS": top3(bat_rows, lambda r: ((r[3]+r[9])/r[8] + (r[3]+r[10]+2*r[11]+3*r[4])/r[2]) if pa_ok(r) and r[2] and r[8] else None,
                     lambda v: f"{v:.3f}"),
        "SB":  top3(bat_rows, lambda r: r[6], str),
        "WAR": top3(bat_rows, lambda r: r[7], lambda v: f"{v:.1f}"),
    }

    pitching = {
        "ERA":  top3(pit_rows, lambda r: r[2] if ip_ok(r) else None, lambda v: f"{v:.2f}", low=True),
        "W":    top3(pit_rows, lambda r: r[6], str),
        "SV":   top3(pit_rows, lambda r: r[8] if r[8] else None, str),
        "K":    top3(pit_rows, lambda r: r[4], str),
        "WHIP": top3(pit_rows, lambda r: (r[9]+r[10])/r[3] if ip_ok(r) and r[3] else None,
                      lambda v: f"{v:.2f}", low=True),
        "WAR":  top3(pit_rows, lambda r: r[5], lambda v: f"{v:.1f}"),
    }

    return {"batting": batting, "pitching": pitching}

def get_farm_depth(team_id):
    conn = get_db()
    ed = conn.execute("SELECT MAX(eval_date) FROM prospect_fv").fetchone()[0]

    by_bucket = conn.execute("""
        SELECT pf.bucket, COUNT(*), COALESCE(SUM(pf.prospect_surplus), 0)
        FROM prospect_fv pf JOIN players p ON pf.player_id = p.player_id
        WHERE pf.eval_date=? AND (p.parent_team_id=? OR (p.team_id=? AND p.level='1')) AND pf.fv >= 40
              AND p.age <= 25
        GROUP BY pf.bucket
    """, (ed, team_id, team_id)).fetchall()

    by_level = conn.execute("""
        SELECT pf.level, COUNT(*)
        FROM prospect_fv pf JOIN players p ON pf.player_id = p.player_id
        WHERE pf.eval_date=? AND (p.parent_team_id=? OR (p.team_id=? AND p.level='1')) AND pf.fv >= 40
              AND p.age <= 25
        GROUP BY pf.level
    """, (ed, team_id, team_id)).fetchall()

    mlb_tids = mlb_team_ids()
    lg = conn.execute("""
        SELECT COALESCE(NULLIF(p.parent_team_id,0), p.team_id), SUM(pf.prospect_surplus)
        FROM prospect_fv pf JOIN players p ON pf.player_id = p.player_id
        WHERE pf.eval_date=? AND p.age <= 25
        GROUP BY COALESCE(NULLIF(p.parent_team_id,0), p.team_id)
    """, (ed,)).fetchall()

    lg_vals = sorted([s for tid, s in lg if s and tid in mlb_tids], reverse=True)
    team_surplus = sum(s for _, _, s in by_bucket)
    lg_avg = sum(lg_vals) / len(lg_vals) if lg_vals else 0
    lg_rank = next((i + 1 for i, v in enumerate(lg_vals) if v <= team_surplus), len(lg_vals))

    buckets = [{"bucket": _display_pos(b), "count": c, "surplus": round(s / _money_divisor(), 1)}
               for b, c, s in sorted(by_bucket, key=lambda x: -x[2])]

    level_order = {"AAA": 1, "AA": 2, "A": 3, "A-Short": 4, "Rookie": 5, "Intl": 6}
    levels = [{"level": l, "count": c}
              for l, c in sorted(by_level, key=lambda x: level_order.get(x[0], 9))]

    return {
        "buckets": buckets, "levels": levels,
        "total_surplus": round(team_surplus / _money_divisor(), 1),
        "lg_avg": round(lg_avg / _money_divisor(), 1),
        "lg_rank": lg_rank, "lg_n": len(lg_vals),
    }


def _resolve_depth_score(row, is_pitcher=False):
    """Resolve the best available score for WAR projection in depth chart code paths.

    Priority: composite_score > ovr > tool-derived estimate > 0

    This handles leagues without OVR ratings (e.g. PPL) by falling back to
    composite_score (from the evaluation engine) or, as a last resort, estimating
    from individual tool ratings.

    Accepts both sqlite3.Row and dict objects.
    """
    keys = row.keys() if hasattr(row, "keys") else ()

    def _val(col):
        if col in keys:
            return row[col]
        return None

    # 1. composite_score — evaluation engine output, most reliable
    cs = _val("composite_score")
    if cs is not None and cs > 0:
        return cs

    # 2. Game OVR — direct from StatsPlus API
    ovr = _val("ovr")
    if ovr is not None and ovr > 0:
        return ovr

    # 3. Estimate from individual tools (last resort)
    # Use a simple weighted average that roughly approximates OVR on the 20-80 scale
    tool_cols = ("stf", "mov", "ctrl") if is_pitcher else ("cntct", "gap", "pow", "eye")
    tools = []
    for col in tool_cols:
        val = _val(col)
        if val is not None and val > 0:
            tools.append(val)
    if tools:
        from statsplusplus.config.ratings import norm_continuous
        # Average the tools and normalize to 20-80 scale
        avg = sum(tools) / len(tools)
        normed = norm_continuous(int(avg))
        return normed if normed else 0
    return 0


def _league_pos_rankings(conn, year):
    """Rank all 34 MLB teams by WAR at each position. Returns {pos: [(team_id, war), ...]}."""
    from projections import project_war
    from collections import defaultdict

    team_pos = defaultdict(lambda: defaultdict(list))

    # Position players — primary position = most fielding games
    seen = set()
    for r in conn.execute("""
        SELECT f.player_id, f.team_id, f.position, f.g,
               r.ovr, r.pot, r.composite_score,
               r.cntct, r.gap, r.pow, r.eye,
               p.age
        FROM mlb_fielding_stats f
        JOIN players p ON f.player_id = p.player_id
        JOIN latest_ratings r ON f.player_id = r.player_id
        WHERE p.level = 1 AND f.year = ? AND f.position != 1 AND r.league_id > 0
        ORDER BY f.player_id, f.g DESC
    """, (year,)).fetchall():
        if r['player_id'] in seen:
            continue
        seen.add(r['player_id'])
        pos = pos_map().get(r['position'])
        if pos:
            _ovr = _resolve_depth_score(r, is_pitcher=False)
            _pot = r['pot'] or _ovr
            team_pos[r['team_id']][pos].append(
                project_war(_ovr, _pot, r['age'], 'CF', 0))

    # Pitchers
    for r in conn.execute("""
        SELECT p.team_id, p.role,
               r.ovr, r.pot, r.composite_score,
               r.stf, r.mov, r.ctrl,
               p.age
        FROM mlb_pitching_stats ps
        JOIN players p ON ps.player_id = p.player_id
        JOIN latest_ratings r ON ps.player_id = r.player_id
        WHERE p.level = 1 AND ps.year = ? AND ps.split_id = 1 AND r.league_id > 0
        GROUP BY ps.player_id
    """, (year,)).fetchall():
        bucket = 'SP' if r['role'] == 11 else 'RP'
        _ovr = _resolve_depth_score(r, is_pitcher=True)
        _pot = r['pot'] or _ovr
        team_pos[r['team_id']][bucket].append(
            project_war(_ovr, _pot, r['age'], bucket, 0))

    TOP_N = {'C':1,'1B':1,'2B':1,'3B':1,'SS':1,'LF':1,'CF':1,'RF':1,'DH':1,'SP':5,'RP':5}
    rankings = {}
    for pos in ['C','1B','2B','3B','SS','LF','CF','RF','SP','RP']:
        tw = []
        for tid, pdict in team_pos.items():
            wars = sorted(pdict.get(pos, []), reverse=True)[:TOP_N[pos]]
            tw.append((tid, round(sum(wars), 1)))
        tw.sort(key=lambda x: -x[1])
        rankings[pos] = tw
    return rankings


def get_draft_org_depth(team_id):
    """Per-position positive surplus totals (MLB + farm) for the draft needs panel.

    Returns dict keyed by display position: {pos: {mlb: $M, farm: $M, total: $M}}
    Only counts positive surplus to avoid noise from bad contracts/low-ceiling prospects.
    Color thresholds are relative to the league average per position.
    """
    conn = get_db()
    ed_s = conn.execute("SELECT MAX(eval_date) FROM player_surplus").fetchone()[0]
    ed_f = conn.execute("SELECT MAX(eval_date) FROM prospect_fv").fetchone()[0]

    POS_ORDER = ["C", "1B", "2B", "3B", "SS", "LF/RF", "CF", "SP", "RP"]
    result = {p: {"mlb": 0.0, "farm": 0.0} for p in POS_ORDER}

    # MLB: positive surplus by bucket
    for r in conn.execute("""
        SELECT bucket, SUM(surplus) FROM player_surplus
        WHERE eval_date=? AND team_id=? AND surplus > 0
        GROUP BY bucket
    """, (ed_s, team_id)).fetchall():
        bucket = r[0]
        if not bucket:
            continue
        # Collapse COF/LF/RF into LF/RF display key
        key = "LF/RF" if bucket in ("COF", "LF", "RF") else ("CF" if bucket == "CF" else _display_pos(bucket))
        if key in result:
            result[key]["mlb"] += (r[1] or 0) / _money_divisor()

    # Farm: positive prospect_surplus by bucket
    for r in conn.execute("""
        SELECT pf.bucket, SUM(pf.prospect_surplus)
        FROM prospect_fv pf JOIN players p ON pf.player_id = p.player_id
        WHERE pf.eval_date=? AND p.parent_team_id=? AND p.level != '1'
          AND pf.prospect_surplus > 0
        GROUP BY pf.bucket
    """, (ed_f, team_id)).fetchall():
        bucket = r[0]
        if not bucket:
            continue
        key = "LF/RF" if bucket in ("COF", "LF", "RF") else ("CF" if bucket == "CF" else _display_pos(bucket))
        if key in result:
            result[key]["farm"] += (r[1] or 0) / _money_divisor()

    # Compute league average per position for relative thresholds.
    # Use the request-scoped mlb_team_ids() (web_league_context), not the raw
    # league_config singleton — that singleton lazily caches whichever
    # league's data it first computes for the life of the process and never
    # invalidates on /switch-league, so it can silently return another
    # league's (or a stale empty) team count here.
    num_teams = len(mlb_team_ids()) or 16

    league_avg = {p: 0.0 for p in POS_ORDER}
    for r in conn.execute("""
        SELECT bucket, SUM(surplus) FROM player_surplus
        WHERE eval_date=? AND surplus > 0
        GROUP BY bucket
    """, (ed_s,)).fetchall():
        bucket = r[0]
        if not bucket:
            continue
        key = "LF/RF" if bucket in ("COF", "LF", "RF") else ("CF" if bucket == "CF" else _display_pos(bucket))
        if key in league_avg:
            league_avg[key] += (r[1] or 0) / _money_divisor() / num_teams

    for r in conn.execute("""
        SELECT pf.bucket, SUM(pf.prospect_surplus)
        FROM prospect_fv pf JOIN players p ON pf.player_id = p.player_id
        WHERE pf.eval_date=? AND p.level != '1' AND pf.prospect_surplus > 0
        GROUP BY pf.bucket
    """, (ed_f,)).fetchall():
        bucket = r[0]
        if not bucket:
            continue
        key = "LF/RF" if bucket in ("COF", "LF", "RF") else ("CF" if bucket == "CF" else _display_pos(bucket))
        if key in league_avg:
            league_avg[key] += (r[1] or 0) / _money_divisor() / num_teams

    # Round and add total with league-relative indicator
    out = {}
    for pos in POS_ORDER:
        mlb = round(result[pos]["mlb"], 1)
        farm = round(result[pos]["farm"], 1)
        total = round(mlb + farm, 1)
        avg = league_avg.get(pos, 0)
        # Relative: >1.2× avg = ok, 0.6-1.2× = thin, <0.6× = gap
        ratio = total / avg if avg > 0 else (2.0 if total > 0 else 0.0)
        out[pos] = {"mlb": mlb, "farm": farm, "total": total, "ratio": round(ratio, 2)}
    return out


def get_depth_chart(team_id):
    """Build 3-year depth chart for a team.

    Returns dict with 'years' list and 'by_year' dict keyed by year, each containing:
        positions: {pos: [{pid, name, level, pt_pct, pa, war, ops_plus}, ...]},
        sp: [{pid, name, level, pt_pct, ip, war, era, fip}, ...],
        rp: [{pid, name, level, rp_role, pt_pct, ip, war, era, fip}, ...],
        team_pa, team_ip, total_war, departed (list of names gone since prior year)
    """
    import json, math
    from projections import (
        project_war, project_ovr, project_ops_plus, project_ops_plus_splits,
        project_era, project_fip, project_ratings,
        assign_diamond_positions, allocate_playing_time, allocate_pitcher_time,
        roster_availability, LEVEL_DISCOUNT, DEFAULT_TEAM_PA, DEFAULT_TEAM_IP,
    )
    from statsplusplus.evaluation.war import stat_peak_war, load_stat_history
    from contract_value import contract_value as _cv
    from statsplusplus.evaluation.arb import estimate_control as _ec_raw
    _lmin2 = league_minimum()
    _perp2 = get_cfg().perpetual_arb
    def _estimate_control(conn, pid, age, sal, bucket=None):
        return _ec_raw(conn, pid, age, sal, min_sal=_lmin2, perpetual_arb=_perp2, bucket=bucket)
    from prospect_value import prospect_surplus as _pv

    state = _get_state()
    year = state.get("stats_year", state["year"])
    conn = get_db()

    lg = _load_la()
    lg_era = lg["pitching"]["era"]
    lg_fip = lg["pitching"]["fip"]

    bat_hist, pit_hist, two_way = load_stat_history(conn, state["game_date"])

    # ── Query MLB roster ────────────────────────────────────────────────
    mlb_rows = conn.execute('''
        SELECT p.player_id, p.name, p.age, p.role,
               r.ovr, r.pot, r.composite_score,
               r.cntct, r.gap, r.pow, r.eye,
               r.stf, r.mov, r.ctrl,
               r.cntct_l, r.cntct_r, r.gap_l, r.gap_r,
               r.pow_l, r.pow_r, r.eye_l, r.eye_r,
               r.pot_cntct, r.pot_gap, r.pot_pow, r.pot_eye,
               r.c, r.ss, r.second_b, r.third_b, r.first_b, r.lf, r.cf, r.rf,
               r.pot_c, r.pot_ss, r.pot_second_b, r.pot_third_b,
               r.pot_first_b, r.pot_lf, r.pot_cf, r.pot_rf,
               c.years, c.current_year,
               c.salary_0, c.salary_1, c.salary_2, c.salary_3, c.salary_4,
               c.salary_5, c.salary_6, c.salary_7, c.salary_8, c.salary_9,
               c.salary_10, c.salary_11, c.salary_12, c.salary_13, c.salary_14,
               c.last_year_team_option, c.last_year_player_option
        FROM players p
        JOIN latest_ratings r ON p.player_id = r.player_id
        JOIN contracts c ON p.player_id = c.player_id
        WHERE p.team_id = ? AND p.level = 1 AND r.league_id > 0
    ''', (team_id,)).fetchall()

    # Fielding and batting games for year-1 position assignment
    fielding = {}
    for r in conn.execute(
        'SELECT player_id, position, g FROM mlb_fielding_stats '
        'WHERE team_id=? AND year=? AND position!=1', (team_id, year)
    ).fetchall():
        fielding.setdefault(r["player_id"], {})[r["position"]] = r["g"]

    bat_games = {r["player_id"]: r["g"] for r in conn.execute(
        'SELECT player_id, g FROM mlb_batting_stats '
        'WHERE team_id=? AND year=? AND split_id=1', (team_id, year)
    ).fetchall()}

    # ── Build MLB player dicts ──────────────────────────────────────────
    all_players = []
    for row in mlb_rows:
        pid, role = row["player_id"], row["role"]
        bucket = "SP" if role == 11 else ("RP" if role in (12, 13) else "CF")
        sw = stat_peak_war(pid, bucket, bat_hist, pit_hist, two_way=two_way)
        _ovr = _resolve_depth_score(row, is_pitcher=(role != 0))
        _pot = row["pot"] or _ovr
        war = project_war(_ovr, _pot, row["age"], bucket, 0, sw)

        if role == 0:
            ovr_ops = project_ops_plus(row["cntct"], row["gap"], row["pow"], row["eye"])
            split_ops, vl, vr = project_ops_plus_splits(dict(row))
        else:
            ovr_ops, split_ops, vl, vr = 0, 0, 0, 0

        salaries = [row[f"salary_{i}"] or 0 for i in range(15)]
        ctrl = None
        if row["years"] == 1:
            est = _estimate_control(conn, pid, row["age"], salaries[0])
            if est[0]:
                ctrl = {"ctrl_years": est[0], "pre_arb_left": est[2] or 0}

        fg = fielding.get(pid)
        bg = bat_games.get(pid, 0)
        yr1_pos = assign_diamond_positions(
            {"role": role, "war_proj": war,
             **{k: row[k] for k in ("c", "ss", "second_b", "third_b",
                                     "first_b", "lf", "cf", "rf")}},
            fg, bg)
        dh_primary = any(pos == "DH" and w >= 0.5 for pos, w in yr1_pos)
        primary_pos = max(yr1_pos, key=lambda x: x[1])[0] if yr1_pos else None
        yr1_positions = {pos for pos, _ in yr1_pos}
        # Also include positions where current ratings are well above viable
        # (not just barely qualifying). This lets Rockwell (RF=76) play RF
        # without letting Gentry (RF=45, barely viable) leak there.
        from projections import POS_THRESHOLDS
        for pos, (field, thresh) in POS_THRESHOLDS.items():
            val = row[field] if field in row.keys() else 0
            if val and val >= thresh + 15:  # solidly above threshold
                yr1_positions.add(pos)

        all_players.append({
            "player_id": pid, "name": row["name"], "age": row["age"],
            "level": "MLB", "ovr": _ovr, "pot": _pot,
            "bucket": bucket, "war_proj": war, "role": role, "stat_peak": sw,
            "ovr_ops_plus": ovr_ops, "split_ops_plus": split_ops,
            "ops_vs_l": vl, "ops_vs_r": vr,
            # Current + potential positional ratings
            "c": row["c"], "ss": row["ss"], "second_b": row["second_b"],
            "third_b": row["third_b"], "first_b": row["first_b"],
            "lf": row["lf"], "cf": row["cf"], "rf": row["rf"],
            "pot_c": row["pot_c"], "pot_ss": row["pot_ss"],
            "pot_second_b": row["pot_second_b"], "pot_third_b": row["pot_third_b"],
            "pot_first_b": row["pot_first_b"], "pot_lf": row["pot_lf"],
            "pot_cf": row["pot_cf"], "pot_rf": row["pot_rf"],
            # Offensive rating potentials for project_ratings
            "pot_cntct": row["pot_cntct"], "pot_gap": row["pot_gap"],
            "pot_pow": row["pot_pow"], "pot_eye": row["pot_eye"],
            "cntct": row["cntct"], "gap": row["gap"],
            "pow": row["pow"], "eye": row["eye"],
            # Split ratings
            "cntct_l": row["cntct_l"], "cntct_r": row["cntct_r"],
            "gap_l": row["gap_l"], "gap_r": row["gap_r"],
            "pow_l": row["pow_l"], "pow_r": row["pow_r"],
            "eye_l": row["eye_l"], "eye_r": row["eye_r"],
            # Year-1 flags
            "fielding": fg, "bat_games": bg,
            "dh_primary": dh_primary, "primary_pos": primary_pos,
            "yr1_positions": yr1_positions,
            "contract": {"years": row["years"], "current_year": row["current_year"],
                         "salaries": salaries,
                         "team_option": bool(row["last_year_team_option"]),
                         "player_option": bool(row["last_year_player_option"])},
            "control": ctrl,
        })

    # ── Pre-compute WAR curves from surplus model ───────────────────────
    war_curves = {}  # {player_id: {year: war}}
    hist = (bat_hist, pit_hist)
    for p in all_players:
        cv = _cv(p["player_id"], _conn=conn, _hist=hist)
        if cv and cv.get("breakdown"):
            war_curves[p["player_id"]] = {
                b["year"]: round(b["war_base"], 2) for b in cv["breakdown"]
            }

    # ── Query org prospects ─────────────────────────────────────────────
    prospect_rows = conn.execute('''
        SELECT pf.player_id, p.name, p.age, p.role, pf.fv, pf.level, pf.bucket,
               r.ovr, r.pot, r.composite_score,
               r.cntct, r.gap, r.pow, r.eye,
               r.stf, r.mov, r.ctrl,
               r.cntct_l, r.cntct_r, r.gap_l, r.gap_r,
               r.pow_l, r.pow_r, r.eye_l, r.eye_r,
               r.pot_cntct, r.pot_gap, r.pot_pow, r.pot_eye,
               r.c, r.ss, r.second_b, r.third_b, r.first_b, r.lf, r.cf, r.rf,
               r.pot_c, r.pot_ss, r.pot_second_b, r.pot_third_b,
               r.pot_first_b, r.pot_lf, r.pot_cf, r.pot_rf
        FROM prospect_fv pf
        JOIN players p ON pf.player_id = p.player_id
        JOIN latest_ratings r ON pf.player_id = r.player_id
        WHERE (p.parent_team_id = ? OR p.team_id = ?)
          AND pf.level != 'MLB'
          AND (pf.fv >= 50 OR (pf.fv >= 40 AND pf.level IN ('AAA', 'AA')))
          AND r.league_id > 0
          AND pf.eval_date = (SELECT MAX(pf2.eval_date) FROM prospect_fv pf2
                              WHERE pf2.player_id = pf.player_id)
        GROUP BY pf.player_id
    ''', (team_id, team_id)).fetchall()

    # League-wide position rankings (lightweight, ~0.02s)
    lg_rankings = _league_pos_rankings(conn, year)
    num_teams = max(len(v) for v in lg_rankings.values()) if lg_rankings else 34
    pos_rank = {}
    for pos, tw in lg_rankings.items():
        for i, (tid, _war) in enumerate(tw):
            if tid == team_id:
                pos_rank[pos] = i + 1
                break


    for row in prospect_rows:
        bucket = row["bucket"]
        role = 11 if bucket == "SP" else (12 if bucket == "RP" else 0)
        _ovr = _resolve_depth_score(row, is_pitcher=(bucket in ("SP", "RP")))
        _pot = row["pot"] or _ovr
        war = project_war(_ovr, _pot, row["age"],
                          bucket if bucket in ("SP", "RP") else "CF", 0)
        if role == 0:
            ovr_ops = project_ops_plus(row["cntct"], row["gap"], row["pow"], row["eye"])
            split_ops, vl, vr = project_ops_plus_splits(dict(row))
        else:
            ovr_ops, split_ops, vl, vr = 0, 0, 0, 0

        all_players.append({
            "player_id": row["player_id"], "name": row["name"], "age": row["age"],
            "level": row["level"], "ovr": _ovr, "pot": _pot,
            "bucket": bucket, "war_proj": war, "role": role, "fv": row["fv"],
            "ovr_ops_plus": ovr_ops, "split_ops_plus": split_ops,
            "ops_vs_l": vl, "ops_vs_r": vr,
            "c": row["c"], "ss": row["ss"], "second_b": row["second_b"],
            "third_b": row["third_b"], "first_b": row["first_b"],
            "lf": row["lf"], "cf": row["cf"], "rf": row["rf"],
            "pot_c": row["pot_c"], "pot_ss": row["pot_ss"],
            "pot_second_b": row["pot_second_b"], "pot_third_b": row["pot_third_b"],
            "pot_first_b": row["pot_first_b"], "pot_lf": row["pot_lf"],
            "pot_cf": row["pot_cf"], "pot_rf": row["pot_rf"],
            "pot_cntct": row["pot_cntct"], "pot_gap": row["pot_gap"],
            "pot_pow": row["pot_pow"], "pot_eye": row["pot_eye"],
            "cntct": row["cntct"], "gap": row["gap"],
            "pow": row["pow"], "eye": row["eye"],
            "cntct_l": row["cntct_l"], "cntct_r": row["cntct_r"],
            "gap_l": row["gap_l"], "gap_r": row["gap_r"],
            "pow_l": row["pow_l"], "pow_r": row["pow_r"],
            "eye_l": row["eye_l"], "eye_r": row["eye_r"],
            "dh_primary": False, "primary_pos": None,
            "contract": None, "control": None,
        })

    # ── Pre-compute prospect WAR curves ─────────────────────────────────
    for p in all_players:
        if p.get("fv") and p["level"] != "MLB":
            pv = _pv(p["fv"], p["age"], p["level"], p["bucket"],
                     ovr=p["ovr"], pot=p["pot"])
            if pv and pv.get("breakdown"):
                eta = pv["years_to_mlb"]
                curve = {}
                for b in pv["breakdown"]:
                    cal_year = year + eta + (b["control_year"] - 1)
                    # Map to integer year (round down — partial years count)
                    curve[int(cal_year)] = round(b["war"], 2)
                war_curves[p["player_id"]] = curve

    # ── Roster availability across 3 years ──────────────────────────────
    avail = roster_availability(all_players, (0, 1, 2))

    LEVEL_ORDER = ["Intl", "Rookie", "A", "A-Short", "AA", "AAA", "MLB"]

    def _promote(level, offset):
        idx = LEVEL_ORDER.index(level) if level in LEVEL_ORDER else 0
        return LEVEL_ORDER[min(idx + offset, len(LEVEL_ORDER) - 1)]

    # ── Per-year assembly ───────────────────────────────────────────────
    by_year = {}
    prev_names = set()

    for off in (0, 1, 2):
        yr = year + off
        pool = avail[off]
        hitter_entries = []  # (pos, player_dict)
        sp_pool, rp_pool = [], []

        for p in pool:
            level = _promote(p["level"], off) if p["level"] != "MLB" else "MLB"
            discount = LEVEL_DISCOUNT.get(level, 0.1)
            bucket = p.get("bucket", "CF")
            pit_bucket = bucket if bucket in ("SP", "RP") else "CF"

            # Project ratings forward for pre-peak players
            if off > 0:
                proj_r = project_ratings(p, off, p["age"], pit_bucket)
            else:
                proj_r = None

            # Use surplus model WAR curve for MLB players, fall back to project_war
            cv_war = war_curves.get(p["player_id"], {}).get(yr)
            if cv_war is not None:
                war = cv_war
            else:
                war = project_war(p["ovr"], p["pot"], p["age"], pit_bucket, off,
                                  p.get("stat_peak"))

            # Pitchers
            if p["role"] in (11, 12, 13):
                era = project_era(p["ovr"], p["pot"], p["age"], bucket, off, lg_era, p.get("stat_peak"))
                fip = project_fip(p["ovr"], p["pot"], p["age"], bucket, off, lg_fip, p.get("stat_peak"))
                entry = dict(p, war_proj=war, level_discount=discount,
                             _level=level, _era=era, _fip=fip)
                if p["role"] == 11:
                    sp_pool.append(entry)
                else:
                    rp_pool.append(entry)
                continue

            # Hitters — compute OPS+ from projected ratings if future year
            if proj_r:
                ovr_ops = project_ops_plus(proj_r["cntct"], proj_r["gap"],
                                           proj_r["pow"], proj_r["eye"])
                # Re-derive splits from projected overall (rough — splits stay proportional)
                ratio_l = p["ops_vs_l"] / p["ovr_ops_plus"] if p["ovr_ops_plus"] else 1.0
                ratio_r = p["ops_vs_r"] / p["ovr_ops_plus"] if p["ovr_ops_plus"] else 1.0
                vl = ovr_ops * ratio_l
                vr = ovr_ops * ratio_r
                split_ops = vr * 0.60 + vl * 0.40
            else:
                ovr_ops = p["ovr_ops_plus"]
                split_ops = p["split_ops_plus"]
                vl, vr = p["ops_vs_l"], p["ops_vs_r"]

            # Position assignment
            use_pot = off > 0 or level != "MLB"
            if off == 0 and level == "MLB":
                positions = assign_diamond_positions(p, p.get("fielding"), p.get("bat_games", 0))
            else:
                positions = assign_diamond_positions(p, use_pot=use_pot)
                # MLB players: constrain to year-1 positions so they don't
                # suddenly appear at new positions via potential ratings
                yr1p = p.get("yr1_positions")
                if yr1p and p.get("level") == "MLB":
                    positions = [(pos, w) for pos, w in positions if pos in yr1p]
                    if positions:
                        wt = sum(w for _, w in positions)
                        positions = [(pos, w / wt) for pos, w in positions]

            for pos, w in positions:
                entry = dict(p, pos_weight=w, level_discount=discount,
                             war_proj=war, _level=level,
                             ovr_ops_plus=ovr_ops, split_ops_plus=split_ops,
                             ops_vs_l=vl, ops_vs_r=vr)
                hitter_entries.append((pos, entry))

        # Allocate playing time
        players_by_pos = {}
        for pos, e in hitter_entries:
            players_by_pos.setdefault(pos, []).append(e)
        pos_result = allocate_playing_time(players_by_pos)

        # Backfill DH: when the primary DH rests, a field player DHs.
        # Prefer bat-first players (high OPS+) at non-premium positions.
        # Elite defenders at CF/SS/C should almost never DH.
        dh_players = pos_result.get("DH", [])
        dh_used = sum(p["pt_pct"] for p in dh_players)
        dh_gap = 100.0 - dh_used
        if dh_gap > 1.0:
            # Defensive position penalty: DHing an elite CF wastes his glove
            _DEF_PEN = {"C": 15, "SS": 12, "CF": 12, "2B": 6, "3B": 4,
                        "LF": 2, "RF": 2, "1B": 0}
            field_candidates = []
            seen = {p["player_id"] for p in dh_players}
            for fpos in ["1B", "LF", "RF", "3B", "2B", "SS", "CF"]:
                for p in pos_result.get(fpos, []):
                    if p["player_id"] not in seen:
                        seen.add(p["player_id"])
                        ops = p.get("ovr_ops_plus", 0) or 0
                        war = p.get("war_proj", 0)
                        # DH score: bat quality minus defensive opportunity cost
                        score = ops - _DEF_PEN.get(fpos, 0) * max(war, 0.5)
                        field_candidates.append((p, fpos, score))
            field_candidates.sort(key=lambda x: x[2], reverse=True)
            top = field_candidates[:5]
            total_s = sum(max(s, 1) for _, _, s in top) or 1
            pos_pa = DEFAULT_TEAM_PA / 9
            for p, fpos, score in top:
                share = dh_gap * max(score, 1) / total_s
                dh_entry = {k: v for k, v in p.items() if not k.startswith("_")}
                dh_entry["pt_pct"] = round(share, 1)
                dh_entry["pa"] = round(pos_pa * share / 100)
                dh_players.append(dh_entry)
            pos_result["DH"] = dh_players

        # Allocate pitcher time
        # SP prospects who can't crack the rotation move to the bullpen.
        # Sort SP by effective WAR, keep top 5 MLB-caliber starters,
        # overflow SP prospects become RP candidates.
        sp_pool.sort(key=lambda x: x["war_proj"] * x.get("level_discount", 1.0),
                     reverse=True)
        rotation_size = 5
        sp_keep, sp_overflow = sp_pool[:rotation_size], sp_pool[rotation_size:]
        for p in sp_overflow:
            if p.get("level", "MLB") != "MLB":
                # Prospect — re-project as RP
                rp_war = project_war(p["ovr"], p["pot"], p["age"], "RP", off)
                rp_era = project_era(p["ovr"], p["pot"], p["age"], "RP", off, lg_era)
                rp_fip = project_fip(p["ovr"], p["pot"], p["age"], "RP", off, lg_fip)
                rp_entry = dict(p, war_proj=rp_war, _era=rp_era, _fip=rp_fip,
                                bucket="RP", role=12)
                rp_pool.append(rp_entry)
            else:
                # MLB SP who didn't make top 5 stays as 6th starter / swingman
                sp_keep.append(p)
        sp_result, rp_result = allocate_pitcher_time(sp_keep, rp_pool)

        # ── Format output ───────────────────────────────────────────────
        def _fmt_hitter(p):
            w = round(p["war_proj"], 1)
            pt = p["pt_pct"]
            return {
                "pid": p["player_id"], "name": p["name"], "age": p["age"] + off,
                "level": p.get("_level", "MLB"),
                "pt_pct": pt, "pa": p["pa"],
                "war": round(w * pt / 100, 1),
                "full_war": w,
                "ops_plus": round(p.get("ovr_ops_plus", 0)),
            }

        def _fmt_pitcher(p):
            w = round(p["war_proj"], 1)
            ip = p.get("ip", 0)
            is_sp = p.get("role") == 11 or p.get("bucket") == "SP"
            full_ip = 200 if is_sp else 70
            return {
                "pid": p["player_id"], "name": p["name"], "age": p["age"] + off,
                "level": p.get("_level", "MLB"),
                "pt_pct": p.get("pt_pct", 0), "ip": ip,
                "war": round(w * min(ip / full_ip, 1.0), 1),
                "full_war": w,
                "era": round(p.get("_era", 5.0), 2),
                "fip": round(p.get("_fip", 5.0), 2),
                "rp_role": p.get("rp_role", ""),
            }

        positions = {}
        pos_war_map = {}
        for pos in ["C", "1B", "2B", "3B", "SS", "LF", "CF", "RF", "DH"]:
            players = [_fmt_hitter(p) for p in pos_result.get(pos, [])
                       if p["pa"] > 0 and round(p.get("pt_pct", 0)) >= 2]
            positions[pos] = players
            pos_war_map[pos] = round(sum(p["war"] for p in players), 1)

        sp_fmt = [_fmt_pitcher(p) for p in sp_result if round(p.get("pt_pct", 0)) >= 2]
        rp_fmt = [_fmt_pitcher(p) for p in rp_result if round(p.get("pt_pct", 0)) >= 2]
        pos_war_map["SP"] = round(sum(p["war"] for p in sp_fmt), 1)
        pos_war_map["RP"] = round(sum(p["war"] for p in rp_fmt), 1)

        curr_names = {p["name"] for p in pool}
        departed = sorted(prev_names - curr_names) if off > 0 else []
        prev_names = curr_names

        by_year[yr] = {
            "positions": positions,
            "pos_war": pos_war_map,
            "sp": sp_fmt,
            "rp": rp_fmt,
            "team_pa": DEFAULT_TEAM_PA,
            "team_ip": DEFAULT_TEAM_IP,
            "total_war": round(sum(pos_war_map.values()), 1),
            "departed": departed,
        }

    return {"years": [year, year + 1, year + 2], "by_year": by_year,
            "pos_rank": pos_rank, "num_teams": num_teams}


def get_org_overview(team_id):
    """Cross-level org summary: position depth, payroll shape, retention priorities."""
    state = _get_state()
    year = state.get("stats_year", state["year"])
    conn = get_db()
    ed_s = conn.execute("SELECT MAX(eval_date) FROM player_surplus").fetchone()[0]
    ed_f = conn.execute("SELECT MAX(eval_date) FROM prospect_fv").fetchone()[0]

    def _entry(r, war_key="war"):
        w = r[war_key]
        return {"pid": r["player_id"], "name": r["name"], "ovr": r["ovr"] or 0,
                "war": round(w, 1) if w else 0, "age": r["age"],
                "surplus": round(r["surplus"] / _money_divisor(), 1) if r["surplus"] else 0}

    # ── Position depth: MLB starters per position ──
    mlb_by_pos = defaultdict(list)  # pos_label -> [entries] sorted by WAR

    # Position players from fielding_stats (current roster only)
    fld_rows = conn.execute("""
        SELECT f.player_id, p.name, f.position, f.g, ps.ovr, ps.surplus,
               COALESCE(b.war, pt.war, 0) as war, p.age
        FROM mlb_fielding_stats f
        JOIN players p ON f.player_id = p.player_id
        LEFT JOIN player_surplus ps ON f.player_id = ps.player_id AND ps.eval_date = ?
        LEFT JOIN mlb_batting_stats b ON f.player_id = b.player_id AND b.year = ? AND b.split_id = 1
        LEFT JOIN mlb_pitching_stats pt ON f.player_id = pt.player_id AND pt.year = ? AND pt.split_id = 1
        WHERE f.team_id = ? AND f.year = ? AND f.position != 1
          AND (p.team_id = ? OR p.parent_team_id = ?)
        ORDER BY f.player_id, f.g DESC
    """, (ed_s, year, year, team_id, year, team_id, team_id)).fetchall()
    seen_fld = set()
    for r in fld_rows:
        if r["player_id"] in seen_fld:
            continue
        seen_fld.add(r["player_id"])
        pos = pos_map().get(r["position"])
        if pos:
            mlb_by_pos[pos].append(_entry(r))

    # Fallback: if no fielding data, use batting_stats + players.pos
    if not seen_fld:
        bat_rows = conn.execute("""
            SELECT b.player_id, p.name, p.pos as position, ps.ovr, ps.surplus,
                   b.war, p.age
            FROM mlb_batting_stats b
            JOIN players p ON b.player_id = p.player_id
            LEFT JOIN player_surplus ps ON b.player_id = ps.player_id AND ps.eval_date = ?
            WHERE b.team_id = ? AND b.year = ? AND b.split_id = 1
              AND p.pos != 1 AND p.role NOT IN (11, 12, 13)
              AND (p.team_id = ? OR p.parent_team_id = ?)
            ORDER BY b.war DESC
        """, (ed_s, team_id, year, team_id, team_id)).fetchall()
        for r in bat_rows:
            pos = pos_map().get(r["position"])
            if pos:
                mlb_by_pos[pos].append(_entry(r))

    # Pitchers — collect all, sorted by WAR (current roster only)
    pit_rows = conn.execute("""
        SELECT p.player_id, p.name, p.role, ps.ovr, ps.surplus, pt.war, p.age
        FROM mlb_pitching_stats pt
        JOIN players p ON pt.player_id = p.player_id
        LEFT JOIN player_surplus ps ON pt.player_id = ps.player_id AND ps.eval_date = ?
        WHERE pt.team_id = ? AND pt.year = ? AND pt.split_id = 1
          AND (p.team_id = ? OR p.parent_team_id = ?)
        ORDER BY pt.war DESC
    """, (ed_s, team_id, year, team_id, team_id)).fetchall()
    for r in pit_rows:
        bucket = "SP" if r["role"] == 11 else "RP"
        mlb_by_pos[bucket].append(_entry(r))

    for pos in mlb_by_pos:
        mlb_by_pos[pos].sort(key=lambda x: -x["ovr"])

    # Top prospects per bucket (collect all, sorted by FV then surplus)
    prospect_by_pos = defaultdict(list)
    prosp_rows = conn.execute("""
        SELECT pf.player_id, p.name, pf.bucket, pf.fv, pf.fv_str, pf.level,
               p.age, p.pos, pf.prospect_surplus, pf.risk
        FROM prospect_fv pf
        JOIN players p ON pf.player_id = p.player_id
        WHERE pf.eval_date = ? AND p.parent_team_id = ? AND p.level != '1'
        ORDER BY pf.fv DESC, pf.prospect_surplus DESC, p.age ASC
    """, (ed_f, team_id)).fetchall()
    for r in prosp_rows:
        bucket = _display_pos(r["bucket"], r["pos"])
        prospect_by_pos[bucket].append({
            "pid": r["player_id"], "name": r["name"],
            "fv": r["fv"], "fv_str": r["fv_str"],
            "level": r["level"], "age": r["age"], "bucket": bucket,
            "surplus": round(r["prospect_surplus"] / _money_divisor(), 1) if r["prospect_surplus"] else 0,
        })

    # Build position depth rows
    # SP shows top 5, RP top 3, position players show 1 MLB + 1 prospect
    of_buckets = {"LF", "CF", "RF", "OF"}
    pos_slots = {"SP": 5, "RP": 3}
    pos_order_list = ["C", "1B", "2B", "3B", "SS", "LF", "CF", "RF", "SP", "RP"]
    position_depth = []
    used_prospect_pids = set()  # deduplicate prospects across positions

    for pos in pos_order_list:
        n_mlb = pos_slots.get(pos, 1)
        n_prosp = pos_slots.get(pos, 1)
        mlb_list = mlb_by_pos.get(pos, [])[:n_mlb]

        # Build deduped prospect list for this position
        prosp_list = prospect_by_pos.get(pos, [])
        if not prosp_list and pos in of_buckets:
            prosp_list = prospect_by_pos.get("OF", [])
        prosp_deduped = []
        for p in prosp_list:
            if p["pid"] not in used_prospect_pids:
                # Label OF prospects with the specific field position
                entry = dict(p)
                if entry["bucket"] == "OF":
                    entry["bucket"] = pos
                prosp_deduped.append(entry)
                if len(prosp_deduped) >= n_prosp:
                    break
        for p in prosp_deduped:
            used_prospect_pids.add(p["pid"])

        n_rows = max(len(mlb_list), len(prosp_deduped), 1)
        for i in range(n_rows):
            mlb = [mlb_list[i]] if i < len(mlb_list) else []
            prosp = prosp_deduped[i] if i < len(prosp_deduped) else None
            position_depth.append({
                "pos": pos if i == 0 else "",
                "mlb": mlb, "prospect": prosp,
                "is_first": i == 0,
                "parent_pos": pos,
            })

    # ── League-wide position rankings ──
    lg_rankings = _league_pos_rankings(conn, year)
    num_teams = max(len(v) for v in lg_rankings.values()) if lg_rankings else 34
    pos_rank = {}
    for pos, tw in lg_rankings.items():
        for i, (tid, _war) in enumerate(tw):
            if tid == team_id:
                pos_rank[pos] = i + 1
                break

    # ── Payroll shape (next 4 years) ──
    payroll_data = get_payroll_summary(team_id)
    payroll_shape = []
    for i, yr in enumerate(payroll_data["years"][:4]):
        payroll_shape.append({"year": yr, "total": payroll_data["totals"][i]})

    # ── Retention priorities: positive surplus, ≤2 years estimated control ──
    from statsplusplus.evaluation.arb import estimate_control as _ec_raw3
    _lmin3 = league_minimum()
    _perp3 = get_cfg().perpetual_arb
    def _estimate_control(conn, pid, age, sal, bucket=None):
        return _ec_raw3(conn, pid, age, sal, min_sal=_lmin3, perpetual_arb=_perp3, bucket=bucket)
    retention = []
    ctrl_rows = conn.execute("""
        SELECT c.player_id, p.name, p.age, c.years, c.current_year,
               c.salary_0, ps.surplus, ps.ovr, ps.bucket, p.role
        FROM contracts c
        JOIN players p ON c.player_id = p.player_id
        LEFT JOIN player_surplus ps ON c.player_id = ps.player_id AND ps.eval_date = ?
        WHERE c.is_major = 1
          {_CONTRACT_ORG_SQL}
    """.format(_CONTRACT_ORG_SQL=_CONTRACT_ORG_SQL), (ed_s, *_contract_org_params(team_id))).fetchall()
    for r in ctrl_rows:
        surplus = r["surplus"]
        if not surplus or surplus <= 0:
            continue
        contract_yrs_left = max(r["years"] - r["current_year"], 1)
        # Multi-year contracts: control = contract years remaining
        # 1-year contracts: estimate arb/pre-arb control beyond the contract
        if r["years"] > 1:
            total_ctrl = contract_yrs_left
        else:
            est = _estimate_control(conn, r["player_id"], r["age"], r["salary_0"] or 0)
            total_ctrl = est[0] if est[0] else 1
        if total_ctrl > 2:
            continue
        pos = _display_pos(r["bucket"]) if r["bucket"] else ROLE_MAP.get(r["role"], "?")
        retention.append({
            "pid": r["player_id"], "name": r["name"], "age": r["age"],
            "pos": pos, "ovr": r["ovr"] or 0,
            "surplus": round(surplus / _money_divisor(), 1), "yrs_left": total_ctrl,
        })
    retention.sort(key=lambda x: -x["surplus"])

    # ── Surplus leaders (full list, not capped) ──
    mlb_surp = conn.execute("""
        SELECT ps.player_id, p.name, ps.bucket, ps.surplus, p.role, p.level
        FROM player_surplus ps JOIN players p ON ps.player_id = p.player_id
        WHERE ps.eval_date = ? AND ps.team_id = ?
    """, (ed_s, team_id)).fetchall()
    farm_surp = conn.execute("""
        SELECT pf.player_id, p.name, pf.bucket, pf.prospect_surplus, p.role, pf.level
        FROM prospect_fv pf JOIN players p ON pf.player_id = p.player_id
        WHERE pf.eval_date = ? AND p.parent_team_id = ? AND p.level != '1'
    """, (ed_f, team_id)).fetchall()
    all_surplus = []
    for r in mlb_surp:
        if not r["surplus"]:
            continue
        pos = _display_pos(r["bucket"]) if r["bucket"] else ROLE_MAP.get(r["role"], "?")
        all_surplus.append({"pid": r["player_id"], "name": r["name"], "pos": pos,
                            "surplus": round(r["surplus"] / _money_divisor(), 1), "level": "MLB"})
    for r in farm_surp:
        if not r["prospect_surplus"]:
            continue
        pos = _display_pos(r["bucket"]) if r["bucket"] else ROLE_MAP.get(r["role"], "?")
        all_surplus.append({"pid": r["player_id"], "name": r["name"], "pos": pos,
                            "surplus": round(r["prospect_surplus"] / _money_divisor(), 1), "level": r["level"]})
    all_surplus.sort(key=lambda x: -x["surplus"])

    return {
        "position_depth": position_depth,
        "pos_rank": pos_rank,
        "num_teams": num_teams,
        "surplus_leaders": all_surplus,
        "payroll_shape": payroll_shape,
        "retention": retention,
    }


# ── Minor League Team Queries ──────────────────────────────────────────────


def get_affiliates(team_id):
    """Get list of minor league affiliates for an MLB team."""
    conn = get_db()
    rows = conn.execute("""
        SELECT DISTINCT t.team_id, t.name, p.level
        FROM teams t
        JOIN players p ON p.team_id = t.team_id
        WHERE t.parent_team_id = ? AND p.level != '1'
        GROUP BY t.team_id
        ORDER BY p.level
    """, (team_id,)).fetchall()
    lmap = level_map()
    return [{"team_id": r[0], "name": r[1],
             "level": lmap.get(str(r[2]), str(r[2]))}
            for r in rows]


# Configurable thresholds for "notable" players on minor league rosters
NOTABLE_MIN_COMPOSITE = 50
NOTABLE_MIN_CEILING = 55
NOTABLE_MIN_FV = 45
NOTABLE_YOUNG_FOR_LEVEL_YEARS = 2  # years below level age norm


# Age norms by level (approximate OOTP norms)
_LEVEL_AGE_NORMS = {
    "2": 24,   # AAA
    "3": 23,   # AA
    "4": 22,   # A / A+
    "5": 21,   # A-Short
    "6": 20,   # Rookie
    "8": 19,   # Intl / DSL
    "10": 18,  # Draft picks
    "11": 18,  # FA signees
}


def get_minor_league_team(team_id):
    """Get minor league team info: name, level, parent org, affiliates."""
    conn = get_db()

    row = conn.execute(
        "SELECT team_id, name, level, parent_team_id FROM teams WHERE team_id=?",
        (team_id,)
    ).fetchone()
    if not row:
        return None

    tid, name, _team_level, parent_id = row

    # Determine level from players on this team
    lvl_row = conn.execute(
        "SELECT level FROM players WHERE team_id=? LIMIT 1", (tid,)
    ).fetchone()
    player_level = lvl_row[0] if lvl_row else None

    # If this is an MLB team (level 1), not a minor league team
    if player_level == "1":
        return None

    # Get parent org name
    parent_name = None
    if parent_id:
        p = conn.execute("SELECT name FROM teams WHERE team_id=?", (parent_id,)).fetchone()
        parent_name = p[0] if p else None

    # Get all affiliates of the same parent org
    affiliates = []
    if parent_id:
        aff_rows = conn.execute("""
            SELECT DISTINCT t.team_id, t.name, p.level
            FROM teams t
            JOIN players p ON p.team_id = t.team_id
            WHERE t.parent_team_id = ? AND p.level != '1'
            GROUP BY t.team_id
            ORDER BY p.level
        """, (parent_id,)).fetchall()
        lmap = level_map()
        for a in aff_rows:
            affiliates.append({
                "team_id": a[0], "name": a[1],
                "level": lmap.get(str(a[2]), str(a[2])),
                "level_num": a[2],
                "current": a[0] == tid,
            })

    lmap = level_map()
    return {
        "team_id": tid,
        "name": name,
        "level": lmap.get(str(player_level), str(player_level)),
        "level_num": player_level,
        "parent_id": parent_id,
        "parent_name": parent_name,
        "affiliates": affiliates,
    }


def get_minor_league_roster(team_id):
    """Full roster for a minor league team, split into hitters and pitchers with tool ratings."""
    conn = get_db()
    from statsplusplus.config.ratings import norm as _norm_rating

    rows = conn.execute("""
        SELECT p.player_id, p.name, p.age, p.pos, p.role, p.level,
               r.ovr, r.pot, r.composite_score, r.true_ceiling, r.ceiling_score,
               r.cntct, r.gap, r.pow, r.eye, r.speed,
               r.pot_cntct, r.pot_gap, r.pot_pow, r.pot_eye,
               r.stf, r.mov, r.ctrl, r.stm,
               r.pot_stf, r.pot_mov, r.pot_ctrl,
               r.bats, r.throws,
               r.c, r.ss, r.second_b, r.third_b, r.first_b, r.lf, r.cf, r.rf,
               r.fst, r.snk, r.crv, r.sld, r.chg, r.splt, r.cutt,
               r.cir_chg, r.scr, r.frk, r.kncrv, r.knbl,
               pf.fv, pf.fv_str, pf.risk, pf.prospect_surplus, pf.bucket,
               ps.surplus, pf.fv_continuous
        FROM players p
        LEFT JOIN latest_ratings r ON p.player_id = r.player_id
        LEFT JOIN prospect_fv pf ON p.player_id = pf.player_id
        LEFT JOIN player_surplus ps ON p.player_id = ps.player_id
        WHERE p.team_id = ?
        ORDER BY COALESCE(r.composite_score, r.ovr, 0) DESC
    """, (team_id,)).fetchall()

    n = _norm_rating
    _pm = pos_map()
    _role_pos = {11: "SP", 12: "SP", 13: "RP"}
    _pos_order = {"C": 1, "1B": 2, "2B": 3, "3B": 4, "SS": 5, "LF": 6, "CF": 7, "RF": 8, "OF": 9, "DH": 10}
    _role_order = {"SP": 1, "RP": 2}

    hitters = []
    pitchers = []

    for r in rows:
        pid, name, age, pos, role, level = r[0:6]
        ovr, pot, composite, true_ceil, ceil_score = r[6:11]
        cntct, gap, pw, eye, speed = r[11:16]
        pot_cntct, pot_gap, pot_pw, pot_eye = r[16:20]
        stf, mov, ctrl, stm = r[20:24]
        pot_stf, pot_mov, pot_ctrl = r[24:27]
        bats, throws = r[27:29]
        c, ss, second_b, third_b, first_b, lf, cf, rf = r[29:37]
        pitches_raw = r[37:49]  # fst, snk, crv, sld, chg, splt, cutt, cir_chg, scr, frk, kncrv, knbl
        fv, fv_str, risk, prospect_surplus, bucket = r[49:54]
        mlb_surplus = r[54]
        fv_continuous = r[55]

        ceiling = true_ceil or ceil_score
        is_pitcher = role in (11, 12, 13)
        potential = true_ceil if true_ceil is not None else ceil_score

        # Handedness display
        bt = ""
        if bats and throws:
            bt = f"{bats}/{throws}"
        elif bats:
            bt = bats

        # Position display
        if bucket:
            display_p = _display_pos(bucket, pos)
        elif is_pitcher:
            display_p = _role_pos.get(role, "P")
        else:
            display_p = _pm.get(pos, "?")

        base = {
            "pid": pid, "name": name, "age": age,
            "pos": display_p, "bt": bt,
            "composite": composite, "ceiling": ceiling,
            "fv": fv, "fv_str": fv_str, "risk": risk,
            "surplus": round((prospect_surplus if prospect_surplus is not None else mlb_surplus) / _money_divisor(), 1)
                       if (prospect_surplus is not None or mlb_surplus is not None) else None,
            "peak_surplus": _peak_surplus(fv_continuous, age, level_map().get(str(level), str(level)),
                                          bucket, ovr=composite, pot=potential),
        }

        if is_pitcher:
            # Count viable pitches (current rating >= 30 on 20-80 scale)
            num_pitches = sum(1 for p in pitches_raw if p and (n(p) or 0) >= 30)
            base.update({
                "stf": n(stf), "pot_stf": n(pot_stf),
                "mov": n(mov), "pot_mov": n(pot_mov),
                "ctrl": n(ctrl), "pot_ctrl": n(pot_ctrl),
                "stm": n(stm),
                "pitches": num_pitches,
                "_sort": (_role_order.get(display_p, 3), -(composite or 0)),
                "_pos_sort": _role_order.get(display_p, 3),
            })
            pitchers.append(base)
        else:
            # Defensive rating at the player's listed position
            _pos_def_map = {"C": c, "SS": ss, "2B": second_b, "3B": third_b,
                            "1B": first_b, "LF": lf, "CF": cf, "RF": rf}
            pos_def = _pos_def_map.get(display_p)
            base.update({
                "con": n(cntct), "pot_con": n(pot_cntct),
                "gap": n(gap), "pot_gap": n(pot_gap),
                "pow": n(pw), "pot_pow": n(pot_pw),
                "eye": n(eye), "pot_eye": n(pot_eye),
                "spd": n(speed), "def": n(pos_def) if pos_def else None,
                "_sort": (-_pos_order.get(display_p, 0), -(composite or 0)),
                "_pos_sort": _pos_order.get(display_p, 99),
            })
            hitters.append(base)

    hitters.sort(key=lambda x: x["_sort"])
    pitchers.sort(key=lambda x: x["_sort"])


    # Compute promotion readiness and demotion risk for all players
    try:
        from promotion_readiness import compute_promotion_readiness, compute_demotion_risk
        _league_dir = get_cfg().league_dir
        for p in hitters + pitchers:
            p["promo"] = compute_promotion_readiness(p["pid"], conn, _league_dir)
            p["demotion"] = compute_demotion_risk(p["pid"], conn, _league_dir)
    except Exception:
        pass

    return {"hitters": hitters, "pitchers": pitchers}


def get_org_minor_league_roster(parent_team_id):
    """Full minor league roster for an entire org (all levels), split into hitters and pitchers."""
    conn = get_db()
    from statsplusplus.config.ratings import norm as _norm_rating

    # Get all affiliate team_ids for this org
    aff_rows = conn.execute("""
        SELECT DISTINCT t.team_id
        FROM teams t
        JOIN players p ON p.team_id = t.team_id
        WHERE t.parent_team_id = ? AND p.level != '1'
    """, (parent_team_id,)).fetchall()
    aff_ids = [a[0] for a in aff_rows]
    if not aff_ids:
        return {"hitters": [], "pitchers": []}

    placeholders = ",".join("?" * len(aff_ids))
    rows = conn.execute(f"""
        SELECT p.player_id, p.name, p.age, p.pos, p.role, p.level,
               r.ovr, r.pot, r.composite_score, r.true_ceiling, r.ceiling_score,
               r.cntct, r.gap, r.pow, r.eye, r.speed,
               r.pot_cntct, r.pot_gap, r.pot_pow, r.pot_eye,
               r.stf, r.mov, r.ctrl, r.stm,
               r.pot_stf, r.pot_mov, r.pot_ctrl,
               r.bats, r.throws,
               r.c, r.ss, r.second_b, r.third_b, r.first_b, r.lf, r.cf, r.rf,
               r.fst, r.snk, r.crv, r.sld, r.chg, r.splt, r.cutt,
               r.cir_chg, r.scr, r.frk, r.kncrv, r.knbl,
               pf.fv, pf.fv_str, pf.risk, pf.prospect_surplus, pf.bucket,
               ps.surplus, pf.fv_continuous
        FROM players p
        LEFT JOIN latest_ratings r ON p.player_id = r.player_id
        LEFT JOIN prospect_fv pf ON p.player_id = pf.player_id
        LEFT JOIN player_surplus ps ON p.player_id = ps.player_id
        WHERE p.team_id IN ({placeholders})
        ORDER BY p.level, COALESCE(r.composite_score, r.ovr, 0) DESC
    """, aff_ids).fetchall()

    # 40-man roster lookup (contract with is_major=1 under this parent org)
    forty_man_pids = set()
    for r in conn.execute(
        "SELECT c.player_id FROM contracts c JOIN players p ON c.player_id=p.player_id "
        "WHERE p.parent_team_id=? AND c.is_major=1", (parent_team_id,)
    ).fetchall():
        forty_man_pids.add(r[0])

    n = _norm_rating
    _pm = pos_map()
    lmap = level_map()
    _role_pos = {11: "SP", 12: "SP", 13: "RP"}
    _pos_order = {"C": 1, "1B": 2, "2B": 3, "3B": 4, "SS": 5, "LF": 6, "CF": 7, "RF": 8, "OF": 9, "DH": 10}
    _role_order = {"SP": 1, "RP": 2}
    _level_order = {"2": 1, "3": 2, "4": 3, "5": 4, "6": 5, "8": 6, "0": 7}

    hitters = []
    pitchers = []

    for r in rows:
        pid, name, age, pos, role, level = r[0:6]
        ovr, pot, composite, true_ceil, ceil_score = r[6:11]
        cntct, gap, pw, eye, speed = r[11:16]
        pot_cntct, pot_gap, pot_pw, pot_eye = r[16:20]
        stf, mov, ctrl, stm = r[20:24]
        pot_stf, pot_mov, pot_ctrl = r[24:27]
        bats, throws = r[27:29]
        c, ss, second_b, third_b, first_b, lf, cf, rf = r[29:37]
        pitches_raw = r[37:49]
        fv, fv_str, risk, prospect_surplus, bucket = r[49:54]
        mlb_surplus = r[54]
        fv_continuous = r[55]

        ceiling = true_ceil or ceil_score
        is_pitcher = role in (11, 12, 13)
        level_name = lmap.get(str(level), str(level))
        on_40man = pid in forty_man_pids
        potential = true_ceil if true_ceil is not None else ceil_score

        # Handedness display
        bt = ""
        if bats and throws:
            bt = f"{bats}/{throws}"
        elif bats:
            bt = bats

        # Position display
        if bucket:
            display_p = _display_pos(bucket, pos)
        elif is_pitcher:
            display_p = _role_pos.get(role, "P")
        else:
            display_p = _pm.get(pos, "?")

        base = {
            "pid": pid, "name": name, "age": age,
            "pos": display_p, "bt": bt,
            "level": level_name, "level_num": int(level) if level else 99,
            "composite": composite, "ceiling": ceiling,
            "fv": fv, "fv_str": fv_str, "risk": risk,
            "surplus": round((prospect_surplus if prospect_surplus is not None else mlb_surplus) / _money_divisor(), 1)
                       if (prospect_surplus is not None or mlb_surplus is not None) else None,
            "peak_surplus": _peak_surplus(fv_continuous, age, level_name, bucket, ovr=composite, pot=potential),
            "on_40man": bool(on_40man),
        }

        if is_pitcher:
            num_pitches = sum(1 for p in pitches_raw if p and (n(p) or 0) >= 30)
            lvl_sort = _level_order.get(str(level), 99)
            base.update({
                "stf": n(stf), "pot_stf": n(pot_stf),
                "mov": n(mov), "pot_mov": n(pot_mov),
                "ctrl": n(ctrl), "pot_ctrl": n(pot_ctrl),
                "stm": n(stm),
                "pitches": num_pitches,
                "_sort": (lvl_sort, _role_order.get(display_p, 3), -(composite or 0)),
                "_pos_sort": _role_order.get(display_p, 3),
            })
            pitchers.append(base)
        else:
            _pos_def_map = {"C": c, "SS": ss, "2B": second_b, "3B": third_b,
                            "1B": first_b, "LF": lf, "CF": cf, "RF": rf}
            pos_def = _pos_def_map.get(display_p)
            lvl_sort = _level_order.get(str(level), 99)
            base.update({
                "con": n(cntct), "pot_con": n(pot_cntct),
                "gap": n(gap), "pot_gap": n(pot_gap),
                "pow": n(pw), "pot_pow": n(pot_pw),
                "eye": n(eye), "pot_eye": n(pot_eye),
                "spd": n(speed), "def": n(pos_def) if pos_def else None,
                "_sort": (lvl_sort, _pos_order.get(display_p, 99), -(composite or 0)),
                "_pos_sort": _pos_order.get(display_p, 99),
            })
            hitters.append(base)

    hitters.sort(key=lambda x: x["_sort"])
    pitchers.sort(key=lambda x: x["_sort"])

    # Compute promotion readiness and demotion risk for all players
    try:
        from promotion_readiness import compute_promotion_readiness, compute_demotion_risk
        _league_dir = get_cfg().league_dir
        for p in hitters + pitchers:
            p["promo"] = compute_promotion_readiness(p["pid"], conn, _league_dir)
            p["demotion"] = compute_demotion_risk(p["pid"], conn, _league_dir)
    except Exception:
        pass

    return {"hitters": hitters, "pitchers": pitchers}


def get_minor_league_notables(team_id):
    """Notable players on a minor league team: prospects + worth-tracking players."""
    conn = get_db()

    # Get player level for age norm lookup
    lvl_row = conn.execute(
        "SELECT level FROM players WHERE team_id=? LIMIT 1", (team_id,)
    ).fetchone()
    team_level = lvl_row[0] if lvl_row else "4"
    age_norm = _LEVEL_AGE_NORMS.get(str(team_level), 22)

    rows = conn.execute("""
        SELECT p.player_id, p.name, p.age, p.pos, p.role, p.level,
               r.ovr, r.pot, r.composite_score, r.true_ceiling, r.ceiling_score,
               r.cntct, r.gap, r.pow, r.eye, r.speed,
               r.stf, r.mov, r.ctrl,
               r.pot_cntct, r.pot_gap, r.pot_pow, r.pot_eye,
               r.pot_stf, r.pot_mov, r.pot_ctrl,
               pf.fv, pf.fv_str, pf.risk, pf.prospect_surplus, pf.bucket
        FROM players p
        LEFT JOIN latest_ratings r ON p.player_id = r.player_id
        LEFT JOIN prospect_fv pf ON p.player_id = pf.player_id
        WHERE p.team_id = ?
        ORDER BY COALESCE(pf.fv, 0) DESC, COALESCE(r.composite_score, r.ovr, 0) DESC
    """, (team_id,)).fetchall()

    # 40-man roster lookup
    forty_man_pids = set()
    for r in conn.execute(
        "SELECT c.player_id FROM contracts c JOIN players p ON c.player_id=p.player_id "
        "WHERE p.team_id=? AND c.is_major=1", (team_id,)
    ).fetchall():
        forty_man_pids.add(r[0])

    # ETA by level
    lmap = level_map()
    _eta_map = {"1": 0, "2": 0.5, "3": 1.5, "4": 2.5, "5": 3.5, "6": 4.5, "7": 4.5, "8": 5.0}

    notables = []
    for r in rows:
        pid, name, age, pos, role, level = r[0:6]
        ovr, pot, composite, true_ceil, ceil_score = r[6:11]
        cntct, gap, pw, eye, speed = r[11:16]
        stf, mov, ctrl = r[16:19]
        pot_cntct, pot_gap, pot_pow, pot_eye = r[19:23]
        pot_stf, pot_mov, pot_ctrl = r[23:26]
        fv, fv_str, risk, prospect_surplus, bucket = r[26:31]

        ceiling = true_ceil or ceil_score
        is_pitcher = role in (11, 12, 13)

        # Determine if this player is "notable"
        has_fv = fv is not None and fv >= NOTABLE_MIN_FV
        has_composite = composite is not None and composite >= NOTABLE_MIN_COMPOSITE
        has_ceiling = ceiling is not None and ceiling >= NOTABLE_MIN_CEILING
        is_young = (age is not None and age <= age_norm - NOTABLE_YOUNG_FOR_LEVEL_YEARS
                    and ceiling is not None and ceiling >= 45)

        if not (has_fv or has_composite or has_ceiling or is_young):
            continue

        # Determine why they're notable
        tags = []
        if has_fv:
            tags.append("prospect")
        if is_young:
            tags.append("young")
        if not has_fv and has_ceiling:
            tags.append("upside")
        if not has_fv and has_composite and not has_ceiling:
            tags.append("performer")

        # Build tool display
        if is_pitcher:
            tools = {"stf": stf, "mov": mov, "ctrl": ctrl,
                     "pot_stf": pot_stf, "pot_mov": pot_mov, "pot_ctrl": pot_ctrl}
        else:
            tools = {"con": cntct, "gap": gap, "pow": pw, "eye": eye, "spd": speed,
                     "pot_con": pot_cntct, "pot_gap": pot_gap, "pot_pow": pot_pow, "pot_eye": pot_eye}

        notables.append({
            "pid": pid, "name": name, "age": age,
            "pos": _display_pos(bucket, pos) if bucket else _display_pos(None, pos),
            "role": role, "is_pitcher": is_pitcher,
            "ovr": ovr, "pot": pot,
            "composite": composite, "ceiling": ceiling,
            "fv": fv, "fv_str": fv_str, "risk": risk,
            "surplus": round(prospect_surplus / _money_divisor(), 1) if prospect_surplus else None,
            "tools": tools, "tags": tags,
            "young_by": age_norm - age if age and age < age_norm else 0,
            "eta": _eta_map.get(str(team_level), 3.5),
            "on_40man": pid in forty_man_pids,
        })

    return notables


def get_head_to_head_matrix(year=None):
    """Full team-vs-team W-L matrix for the current year.

    Returns:
        {
            "teams": [(tid, abbr), ...],  # sorted by standings
            "matrix": {tid: {opp_tid: {"w": int, "l": int}, ...}, ...}
        }
    """
    from web_league_context import get_db
    state = _get_state()
    conn = get_db()
    year = year or state.get("stats_year", state["year"])

    # Get MLB team IDs and abbreviations
    cfg = get_cfg()
    abbr_map = cfg.team_abbr_map

    # Fetch all games for the year
    games = conn.execute("""
        SELECT home_team, away_team, runs0, runs1
        FROM games
        WHERE date LIKE ? AND played = 1 AND game_type = 0
    """, (f"{year}%",)).fetchall()

    # Fall back to prior year if no games (preseason)
    if not games:
        year = year - 1
        games = conn.execute("""
            SELECT home_team, away_team, runs0, runs1
            FROM games
            WHERE date LIKE ? AND played = 1 AND game_type = 0
        """, (f"{year}%",)).fetchall()

    if not games:
        return None

    # Build W-L matrix
    # runs0 = away team runs, runs1 = home team runs
    from collections import defaultdict
    matrix = defaultdict(lambda: defaultdict(lambda: {"w": 0, "l": 0}))
    team_wins = defaultdict(int)
    team_losses = defaultdict(int)

    for g in games:
        home, away, away_runs, home_runs = g[0], g[1], g[2], g[3]
        if home_runs > away_runs:
            # Home wins
            matrix[home][away]["w"] += 1
            matrix[away][home]["l"] += 1
            team_wins[home] += 1
            team_losses[away] += 1
        else:
            # Away wins
            matrix[away][home]["w"] += 1
            matrix[home][away]["l"] += 1
            team_wins[away] += 1
            team_losses[home] += 1

    # Sort teams by win pct (standings order)
    all_tids = sorted(set(team_wins) | set(team_losses))
    teams_sorted = sorted(all_tids, key=lambda t: team_wins[t] / max(1, team_wins[t] + team_losses[t]), reverse=True)

    teams_out = [(tid, abbr_map.get(tid, "?")) for tid in teams_sorted]
    matrix_out = {tid: dict(matrix[tid]) for tid in teams_sorted}

    return {"teams": teams_out, "matrix": matrix_out, "year": year}
