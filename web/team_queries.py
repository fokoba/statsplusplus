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
from statsplusplus.config.league_config import dollars_per_war as _dpw_pkg, league_minimum as _lm_pkg, games_per_season as _gps_pkg
from statsplusplus.utils.positions import ROLE_MAP
from statsplusplus.evaluation.constants import DEFAULT_MINIMUM_SALARY
from statsplusplus.data.evaluation_engine import load_tool_weights
from statsplusplus.evaluation.composite import compute_batting_composite
from statsplusplus.data.db import ORG_ID_SQL
from web_league_context import (get_db, get_cfg, team_abbr_map, team_names_map,
                                 level_map, pos_map, pos_order, pyth_exp, my_team_id,
                                 mlb_team_ids, league_averages as _load_la,
                                 money_unit as _money_unit, money_divisor as _money_divisor,
                                 dev_cell as _dev_cell)

from statsplusplus.data.retained_salary import get_retention_map as _get_retention_map

# Local wrappers using request-scoped league_dir
def _dollars_per_war():
    return _dpw_pkg(get_cfg().league_dir)

def league_minimum():
    return _lm_pkg(get_cfg().league_dir)

def _games_per_season():
    return _gps_pkg(get_cfg().league_dir)


# SQL fragment + params to filter contracts to players currently in a given org.
# This is the *only* org filter these queries should use — contract_team_id
# is unreliable and must not be ANDed alongside it: Rule 5 picks and traded
# players both retain their contract row's original contract_team_id, so a
# hard `contract_team_id = ?` gate silently excludes them even though this
# players-table check (their actual current team_id/parent_team_id) already
# correctly identifies them as belonging to this org.
_CONTRACT_ORG_SQL = (
    f"AND {ORG_ID_SQL} = ?"
)
def _contract_org_params(team_id):
    return (team_id,)

# SQL fragment for farm/prospect queries (prospect_fv-driven): affiliate
# levels (AAA/AA/A/etc) get their own team_id with parent_team_id pointing
# back to the org, same as contracts above — but the International Complex
# has no affiliate team of its own in OOTP's structure at all, its players
# just sit directly on the MLB team_id with level='8' (parent_team_id=0),
# the same way a young MLB-level rookie still graded on the prospect scale
# does with level='1'. Without org attribution here, international
# prospects were silently excluded from Farm Surplus and the farm lists.
# ORG_ID_SQL (organization_id -> parent_team_id -> team_id) handles this.


def _pap_context(conn, tid, year):
    """Get shared context for PAP calculation: team games, $/WAR, salary map."""
    team_g = conn.execute(
        "SELECT COUNT(*) FROM games WHERE (home_team=? OR away_team=?) AND date>=? AND played=1",
        (tid, tid, f"{year}-01-01")).fetchone()[0]
    dpw = _dollars_per_war()
    sal_rows = conn.execute(
        "SELECT player_id, salary_0 FROM contracts WHERE player_id IN "
        "(SELECT player_id FROM players WHERE team_id=? AND level='1')", (tid,)).fetchall()
    retained = _get_retention_map(conn)
    salaries = {r["player_id"]: (r["salary_0"] or 0) * (1 - retained.get(r["player_id"], 0.0))
                for r in sal_rows}
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


def _get_all_war_paces_cached(conn):
    """Bulk {player_id: pace_dict} from war_pace.get_all_war_paces(), cached
    per request+league. That function does a full league-wide scan across
    mlb_batting_stats/mlb_pitching_stats; without this cache a single
    /team/<id> render calls it independently from multiple call sites
    (overview, league surplus rankings, hitters tab, pitchers tab)."""
    from flask import g as _g, has_request_context as _hrc
    league_dir = get_cfg().league_dir
    if _hrc():
        cache = getattr(_g, "_tq_war_paces_cache", None)
        if cache is not None and cache.get("league_dir") == league_dir:
            return cache["paces"]
    from war_pace import get_all_war_paces as _get_all_war_paces
    paces = _get_all_war_paces(league_dir=league_dir, conn=conn)
    if _hrc():
        _g._tq_war_paces_cache = {"league_dir": league_dir, "paces": paces}
    return paces


def _get_all_teams_combined_pace_cached(conn):
    """{team_id: {combined_pace, n_qualifying}}, built from the cached bulk
    WAR paces above instead of re-running war_pace.get_all_teams_combined_pace()
    (which would otherwise re-scan via get_all_war_paces on every call)."""
    from flask import g as _g, has_request_context as _hrc
    league_dir = get_cfg().league_dir
    if _hrc():
        cache = getattr(_g, "_tq_combined_pace_cache", None)
        if cache is not None and cache.get("league_dir") == league_dir:
            return cache["combined"]
    paces = _get_all_war_paces_cached(conn)
    out = {}
    if paces:
        pid_qs = ",".join("?" * len(paces))
        rows = conn.execute(
            f"SELECT player_id, team_id FROM players WHERE level='1' AND player_id IN ({pid_qs})",
            list(paces.keys()),
        ).fetchall()
        for r in rows:
            tid = r["team_id"]
            if not tid:
                continue
            pace = paces[r["player_id"]]["pace_war"]
            entry = out.setdefault(tid, {"combined_pace": 0.0, "n_qualifying": 0})
            entry["combined_pace"] = round(entry["combined_pace"] + pace, 2)
            entry["n_qualifying"] += 1
    if _hrc():
        _g._tq_combined_pace_cache = {"league_dir": league_dir, "combined": out}
    return out


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


def _determine_phase(conn, game_date, year):
    """Determine the season phase from actual game data, not month heuristics.

    Adapts to each league's own schedule/format (playoff length, calendar)
    because it reads that league's `games` rows rather than assuming month
    boundaries. Uses `games.game_type` (0 = regular season, 3 = postseason).

    IMPORTANT — the refresh only stores games UP TO the current sim date, so the
    future schedule is generally NOT in the table for a live league. We can't
    infer "season over" from "today is past the last stored game" (that's just
    the current date). Instead we use recency: if the current date is close to
    the most recent played game of a type, we're still in that phase; a large
    gap after the last played game means that phase is complete.

    Phases: Regular Season, Postseason, Offseason, Spring Training.
    """
    if not game_date or len(game_date) < 10:
        return "Regular Season"

    row = conn.execute("""
        SELECT
          MAX(CASE WHEN game_type=0 AND played=1 THEN date END) AS reg_last,
          MIN(CASE WHEN game_type=0 AND played=1 THEN date END) AS reg_first,
          MAX(CASE WHEN game_type=3 AND played=1 THEN date END) AS post_last,
          MIN(CASE WHEN game_type=3 AND played=1 THEN date END) AS post_first
        FROM games WHERE date LIKE ?
    """, (f"{year}%",)).fetchone()

    from datetime import date as _date

    def _d(s):
        try:
            return _date(int(s[0:4]), int(s[5:7]), int(s[8:10]))
        except Exception:
            return None

    today = _d(game_date)
    reg_last, reg_first = _d(row["reg_last"]) if row else None, _d(row["reg_first"]) if row else None
    post_last, post_first = _d(row["post_last"]) if row else None, _d(row["post_first"]) if row else None

    # "Recent" = within this many days of the last played game of a phase; the
    # sim advances a few times a week, so the current date trails the last game
    # by only a handful of days while a phase is live.
    _RECENT = 10

    if today is None:
        return "Regular Season"

    # Postseason: playoff games have been played and the current date is at/after
    # the first playoff game and still close to the latest played playoff game.
    if post_first and post_last and today >= post_first:
        if (today - post_last).days <= _RECENT:
            return "Postseason"
        return "Offseason"  # well past the last playoff game → season over

    # Regular season: reg games played and today is close to the latest one
    # (in-season). A large gap after the last reg game with no playoffs recorded
    # means the season has ended (playoffs not yet pulled, or between reg & post).
    if reg_last and today >= (reg_first or reg_last):
        if (today - reg_last).days <= _RECENT:
            return "Regular Season"
        # Season ended; playoffs haven't been recorded (or are between rounds).
        # If we're within a few weeks, call it Postseason; otherwise Offseason.
        return "Postseason" if (today - reg_last).days <= 30 else "Offseason"

    # No games played yet this year: preseason (Spring) if in the typical
    # ramp-up window, else Offseason (deep winter before spring).
    month = today.month
    return "Spring Training" if 2 <= month <= 4 else "Offseason"


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
        f"SELECT COALESCE(SUM(prospect_surplus),0) FROM prospect_fv pf JOIN players p ON pf.player_id=p.player_id WHERE pf.eval_date=? AND {ORG_ID_SQL}=?",
        (ed, tid)).fetchone()[0]
    fv50 = conn.execute(
        f"SELECT COUNT(*) FROM prospect_fv pf JOIN players p ON pf.player_id=p.player_id WHERE pf.eval_date=? AND {ORG_ID_SQL}=? AND pf.fv>=50 AND p.age<=25",
        (ed, tid)).fetchone()[0]
    # Determine season phase from actual game data (game_type boundaries).
    phase = _determine_phase(conn, state["game_date"], state["year"])

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

    _pace = _get_all_teams_combined_pace_cached(conn).get(
        tid, {"combined_pace": 0.0, "n_qualifying": 0}
    )

    return {
        "game_date": state["game_date"], "year": state["year"], "phase": phase,
        "mlb_surplus": round(mlb_surplus / _money_divisor(), 1),
        "farm_surplus": round(farm_surplus / _money_divisor(), 1),
        "current_year_surplus": round(cur_sum / _money_divisor(), 1) if have_any else None,
        "next_year_surplus": round(next_sum / _money_divisor(), 1) if have_any else None,
        "three_year_surplus": round(three_sum / _money_divisor(), 1) if have_any else None,
        "fv50_count": fv50,
        "combined_pace": _pace["combined_pace"], "combined_pace_n": _pace["n_qualifying"],
        "rank": _league_surplus_rankings(tid),
    }


def _league_surplus_rankings(team_id):
    """League rank + vs-median context for the 5 summary-bar surplus
    metrics, scoped to real MLB orgs only (not minor-league affiliate
    team_ids). Reuses one connection and one shared stat-history load
    across every team's contract-horizon calc (the same _conn/_hist batch
    mode contract_value() already supports elsewhere) — computing this with
    a fresh connection and a fresh stat-history reload per player, times
    every team in the league, would be far too slow for a page that loads
    on every visit.
    """
    conn = get_db()
    ed = _get_eval_date()
    org_tids = list(get_cfg().team_names_map.keys())
    if len(org_tids) < 2:
        return {}
    qs = ",".join("?" * len(org_tids))

    mlb_by_team = {t: 0.0 for t in org_tids}
    for r in conn.execute(
        f"SELECT team_id, COALESCE(SUM(surplus),0) FROM player_surplus "
        f"WHERE eval_date=? AND team_id IN ({qs}) GROUP BY team_id",
        (ed, *org_tids)
    ):
        mlb_by_team[r[0]] = r[1]

    farm_by_team = {t: 0.0 for t in org_tids}
    for r in conn.execute(
        "SELECT (CASE WHEN p.parent_team_id != 0 THEN p.parent_team_id ELSE p.team_id END) AS org_tid, "
        "SUM(pf.prospect_surplus) FROM prospect_fv pf JOIN players p ON pf.player_id=p.player_id "
        "WHERE pf.eval_date=? AND (p.parent_team_id != 0 OR p.level IN ('1','8')) GROUP BY org_tid",
        (ed,)
    ):
        if r[0] in farm_by_team:
            farm_by_team[r[0]] = r[1] or 0.0

    cur_by_team = {t: 0.0 for t in org_tids}
    next_by_team = {t: 0.0 for t in org_tids}
    three_by_team = {t: 0.0 for t in org_tids}
    try:
        from statsplusplus.evaluation.war import load_stat_history
        from statsplusplus.config.league_config import games_per_season
        from contract_value import contract_surplus_horizons as _csh
        state = _get_state()
        _game_year = get_cfg().year
        _league_dir = get_cfg().league_dir
        bat_hist, pit_hist, _tw = load_stat_history(
            conn, state["game_date"], games_per_season=games_per_season(_league_dir)
        )
        hist = (bat_hist, pit_hist)
        for pid, org_tid in conn.execute(
            f"SELECT c.player_id, p.team_id FROM contracts c JOIN players p ON c.player_id=p.player_id "
            f"WHERE c.is_major=1 AND p.team_id IN ({qs})", org_tids
        ):
            try:
                cs, ns, ts = _csh(pid, _game_year, _conn=conn, _hist=hist, league_dir=_league_dir)
            except Exception:
                cs, ns, ts = None, None, None
            if cs is not None:
                cur_by_team[org_tid] += cs
            if ns is not None:
                next_by_team[org_tid] += ns
            if ts is not None:
                three_by_team[org_tid] += ts
    except Exception:
        pass

    import statistics

    def _rank_ctx(by_team, divisor=None):
        vals = list(by_team.values())
        n = len(vals)
        my = by_team.get(team_id, 0.0)
        sorted_desc = sorted(vals, reverse=True)
        rank = sorted_desc.index(my) + 1 if my in sorted_desc else n
        med = statistics.median(vals) if vals else 0.0
        d = divisor if divisor is not None else _money_divisor()
        return {"rank": rank, "n": n, "vs_median": round((my - med) / d, 1)}

    pace_by_team = {t: 0.0 for t in org_tids}
    for t, d in _get_all_teams_combined_pace_cached(conn).items():
        if t in pace_by_team:
            pace_by_team[t] = d["combined_pace"]

    return {
        "mlb_surplus": _rank_ctx(mlb_by_team),
        "farm_surplus": _rank_ctx(farm_by_team),
        "current_year_surplus": _rank_ctx(cur_by_team),
        "next_year_surplus": _rank_ctx(next_by_team),
        "three_year_surplus": _rank_ctx(three_by_team),
        "combined_pace": _rank_ctx(pace_by_team, divisor=1),
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
    farm_map = dict(conn.execute(f"""
        SELECT {ORG_ID_SQL}, SUM(pf.prospect_surplus)
        FROM prospect_fv pf JOIN players p ON pf.player_id=p.player_id
        WHERE pf.eval_date=?
        GROUP BY {ORG_ID_SQL}
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
        r["tier"] = _gr_tier(i + 1, len(rows), "pill")
    return rows


def get_standings():
    state = _get_state()
    conn = get_db()
    year = state.get("stats_year", state["year"])

    bat = {r[0]: (r[1], r[2]) for r in conn.execute(
        "SELECT team_id, name, r FROM team_batting_stats WHERE year=? AND split_id=1", (year,)).fetchall()}
    # Fall back when the target year has no TEAM stats (preseason, or a year gap
    # where player stats exist but team-stat renders didn't land). Use the most
    # recent year that actually has team stats at or before the target, rather
    # than blindly stepping back one year (which could skip past the real last
    # completed season to an older one).
    if not bat:
        row = conn.execute(
            "SELECT MAX(year) FROM team_batting_stats WHERE split_id=1 AND year <= ?",
            (year,)).fetchone()
        if row and row[0]:
            year = row[0]
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
        babip_denom = ab - k - hr + sf
        babip = (h - hr) / babip_denom if babip_denom > 0 else None
        return {
            "pa": pa, "ab": ab, "avg": _r3(avg), "obp": _r3(obp), "slg": _r3(slg),
            "ops": _r3(ops), "hr": hr, "r": s["r"] or 0, "rbi": s["rbi"] or 0,
            "sb": s["sb"] or 0, "cs": s["cs"] or 0,
            "bb_pct": round(100 * bb / pa, 1) if pa else None,
            "k_pct": round(100 * k / pa, 1) if pa else None,
            "war": round(s["war"], 1) if s["war"] is not None else 0,
            "g": s["g"] or 0, "babip": _r3(babip),
        }

    # Career MLB BABIP (all years, split_id=1) — the baseline the current
    # season's BABIP is judged against for the luck label.
    roster_pids = [p["player_id"] for p in players]
    career_babip = {}
    if roster_pids:
        qs = ",".join("?" * len(roster_pids))
        for r in conn.execute(f"""
            SELECT player_id, SUM(ab), SUM(h), SUM(hr), SUM(k), SUM(sf)
            FROM mlb_batting_stats WHERE split_id=1 AND player_id IN ({qs})
            GROUP BY player_id
        """, roster_pids):
            pid, s_ab, s_h, s_hr, s_k, s_sf = r
            denom = (s_ab or 0) - (s_k or 0) - (s_hr or 0) + (s_sf or 0)
            career_babip[pid] = (s_h - s_hr) / denom if denom > 0 else None

    # Career BB%/K% — diagnostic only, shown alongside BABIP but not tiered
    # or folded into All-Up Luck: unlike BABIP, a hitter walking or striking
    # out more than his career rate isn't cleanly "lucky" or "unlucky" (could
    # be an approach change, aging, or how he's being pitched), so there's no
    # honest polarity to assign it.
    career_pct = {}
    if roster_pids:
        qs = ",".join("?" * len(roster_pids))
        for r in conn.execute(f"""
            SELECT player_id, SUM(bb), SUM(k), SUM(pa)
            FROM mlb_batting_stats WHERE split_id=1 AND player_id IN ({qs})
            GROUP BY player_id
        """, roster_pids):
            pid, s_bb, s_k, s_pa = r
            s_pa = s_pa or 0
            career_pct[pid] = (
                round(100 * (s_bb or 0) / s_pa, 1) if s_pa else None,
                round(100 * (s_k or 0) / s_pa, 1) if s_pa else None,
            )

    _paces = _get_all_war_paces_cached(conn)

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
        _c_babip = career_babip.get(pid)
        _cur_babip = _fmt_split(s1)["babip"] if s1 else None
        _cur_pa = (s1["pa"] or 0) if s1 else 0
        _luck_gap = (_cur_babip - _c_babip) if (_cur_babip is not None and _c_babip is not None
                                                  and _cur_pa >= _BABIP_MIN_PA) else None
        _babip_luck_tag = _babip_luck(_luck_gap)
        _c_bb_pct, _c_k_pct = career_pct.get(pid, (None, None))
        _cur_bb_pct = _fmt_split(s1)["bb_pct"] if s1 else None
        _cur_k_pct = _fmt_split(s1)["k_pct"] if s1 else None

        def _trend(cur, career):
            # Same 80+ PA gate as BABIP Luck — below that, a BB%/K% swing is
            # too small a sample to call one way or the other.
            if cur is None or career is None or _cur_pa < _BABIP_MIN_PA:
                return None
            if cur > career:
                return "higher"
            if cur < career:
                return "lower"
            return "same"

        _bb_pct_trend = _trend(_cur_bb_pct, _c_bb_pct)
        _k_pct_trend = _trend(_cur_k_pct, _c_k_pct)
        result.append({
            "pid": pid, "name": p["name"], "age": p["age"],
            "ovr": _display_ovr, "pos": pos,
            "pos_order": pos_order().get(pos, 99),
            "surplus": round(p["surplus_yr1"] / _money_divisor(), 1) if p["surplus_yr1"] else 0,
            "pap": calc_pap(war, salaries.get(pid, 0), team_g, dpw, games_per_season=_games_per_season()),
            "pace_war": _paces.get(pid, {}).get("pace_war"),
            "pace_tier": _paces.get(pid, {}).get("pace_tier"),
            "pace_tier_label": _paces.get(pid, {}).get("pace_tier_label"),
            "pace_confidence": _paces.get(pid, {}).get("pace_confidence"),
            "is_two_way": pid in twp_pids,
            "career_babip": _r3(_c_babip), "babip_diff": _r3(_luck_gap), "luck": _babip_luck_tag,
            "career_bb_pct": _c_bb_pct, "career_k_pct": _c_k_pct,
            "bb_pct_trend": _bb_pct_trend, "k_pct_trend": _k_pct_trend,
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
               hra, bf, hld, bs, qs, er, r AS runs, cg, sho, ir, irs, hp, fb
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


    # FIP constant (league-wide, same formula/convention used on player.html)
    # so current-season FIP is on the same scale as ERA.
    _tp = conn.execute(
        "SELECT SUM(era*ip)/SUM(ip), SUM(hra), SUM(bb), SUM(k), SUM(ip) "
        "FROM team_pitching_stats WHERE split_id=1"
    ).fetchone()
    _fip_const = (_tp[0] - ((13 * _tp[1] + 3 * _tp[2] - 2 * _tp[3]) / _tp[4])) if _tp and _tp[4] else 3.10

    def _fmt_split(s):
        if not s:
            return None
        ip, bf = s["ip"] or 0, s["bf"] or 0
        k, bb, ha, hra = s["k"] or 0, s["bb"] or 0, s["ha"] or 0, s["hra"] or 0
        ir, irs = s["ir"] or 0, s["irs"] or 0
        hp = s["hp"] or 0
        fb = s["fb"] or 0
        runs = s["runs"] or 0
        gs = s["gs"] or 0
        whip = (bb + ha) / ip if ip else None
        irs_pct = round(100 * irs / ir, 1) if ir else None
        babip_denom = bf - k - hra - bb - hp
        babip = (ha - hra) / babip_denom if babip_denom > 0 else None
        hr_fb = hra / fb if fb > 0 else None
        lob_denom = (ha + bb + hp) - 1.4 * hra
        lob_pct = ((ha + bb + hp) - runs) / lob_denom if lob_denom > 0 else None
        fip = (13 * hra + 3 * (bb + hp) - 2 * k) / ip + _fip_const if ip else None
        return {
            "ip": ip, "g": s["g"] or 0, "gs": gs,
            "w": s["w"] or 0, "l": s["l"] or 0, "sv": s["sv"] or 0,
            "era": round(s["era"], 2) if s["era"] is not None else None,
            "whip": round(whip, 2) if whip else None,
            "k": k, "bb": bb, "hra": hra,
            "k_pct": round(100 * k / bf, 1) if bf else None,
            "bb_pct": round(100 * bb / bf, 1) if bf else None,
            "k_bb_pct": round(100 * (k - bb) / bf, 1) if bf else None,
            "war": round(s["war"], 1) if s["war"] is not None else 0,
            "hld": s["hld"] or 0, "bs": s["bs"] or 0,
            "qs": s["qs"] or 0, "qs_pct": round(100 * (s["qs"] or 0) / gs, 1) if gs else None,
            "irs_pct": irs_pct, "babip": _r3(babip),
            "hr_fb": _r3(hr_fb), "lob_pct": _r3(lob_pct), "fip": round(fip, 2) if fip is not None else None,
        }

    # Career MLB rates (all years, split_id=1) — the baseline the current
    # season's BABIP/HR-FB/LOB% are each judged against for their luck label.
    roster_pids = [p["player_id"] for p in players]
    career_babip = {}
    career_hrfb = {}
    career_lob = {}
    if roster_pids:
        qs = ",".join("?" * len(roster_pids))
        for r in conn.execute(f"""
            SELECT player_id, SUM(bf), SUM(k), SUM(hra), SUM(bb), SUM(hp), SUM(ha), SUM(fb), SUM(r)
            FROM mlb_pitching_stats WHERE split_id=1 AND player_id IN ({qs})
            GROUP BY player_id
        """, roster_pids):
            pid, s_bf, s_k, s_hra, s_bb, s_hp, s_ha, s_fb, s_r = r
            s_bf, s_k, s_hra = s_bf or 0, s_k or 0, s_hra or 0
            s_bb, s_hp, s_ha, s_fb, s_r = s_bb or 0, s_hp or 0, s_ha or 0, s_fb or 0, s_r or 0
            babip_denom = s_bf - s_k - s_hra - s_bb - s_hp
            career_babip[pid] = (s_ha - s_hra) / babip_denom if babip_denom > 0 else None
            career_hrfb[pid] = s_hra / s_fb if s_fb > 0 else None
            lob_denom = (s_ha + s_bb + s_hp) - 1.4 * s_hra
            career_lob[pid] = ((s_ha + s_bb + s_hp) - s_r) / lob_denom if lob_denom > 0 else None

    _paces = _get_all_war_paces_cached(conn)

    result = []
    team_g, dpw, salaries = _pap_context(conn, tid, year)
    for p in players:
        splits = pit.get(p["player_id"])
        pid = p["player_id"]
        role_str = ROLE_MAP.get(p["role"], "P")
        s1 = splits.get(1) if splits else None
        war = s1["war"] if s1 and s1["war"] is not None else None
        _display_ovr = p["composite_score"] if p["composite_score"] is not None else (p["ovr"] or 0)
        _cur_fmt = _fmt_split(s1) if s1 else None
        _cur_bf = (s1["bf"] or 0) if s1 else 0
        # SP need the full 150 BF (~a starter's first month). RP/CL rarely
        # reach that in a season, so their bar rises with the team's games
        # played so far instead of sitting at a fixed number — a reliever
        # who's barely pitched shouldn't get a tag off 2 appearances in
        # April, but by August the same 2 appearances still shouldn't count.
        # Mirrors the existing get_pitcher_percentiles() RP-threshold pattern.
        if role_str in ("RP", "CL"):
            _min_bf = max(round(_RP_BF_PER_TEAM_GAME * team_g), _RP_BF_FLOOR)
        else:
            _min_bf = _BABIP_MIN_BF
        _sample_ok = _cur_bf >= _min_bf

        _c_babip = career_babip.get(pid)
        _cur_babip = _cur_fmt["babip"] if _cur_fmt else None
        # Inverted vs hitters: a LOWER current BABIP-against than career is
        # the pitcher's luck (suppressing hits on balls in play), so the
        # gap is career minus current, not current minus career.
        _babip_gap = (_c_babip - _cur_babip) if (_cur_babip is not None and _c_babip is not None and _sample_ok) else None
        _babip_luck_tag = _babip_luck(_babip_gap)

        _c_hrfb = career_hrfb.get(pid)
        _cur_hrfb = _cur_fmt["hr_fb"] if _cur_fmt else None
        # Same inversion as BABIP: fewer current HR/FB than career is luck.
        _hrfb_gap = (_c_hrfb - _cur_hrfb) if (_cur_hrfb is not None and _c_hrfb is not None and _sample_ok) else None
        _hrfb_luck_tag = _luck_tier(_hrfb_gap, _HRFB_SOMEWHAT, _HRFB_VERY)

        _c_lob = career_lob.get(pid)
        _cur_lob = _cur_fmt["lob_pct"] if _cur_fmt else None
        # NOT inverted: a HIGHER current LOB% than career is stranding more
        # runners than usual right now, which is the pitcher's luck.
        _lob_gap = (_cur_lob - _c_lob) if (_cur_lob is not None and _c_lob is not None and _sample_ok) else None
        _lob_luck_tag = _luck_tier(_lob_gap, _LOB_SOMEWHAT, _LOB_VERY)

        # ERA vs FIP, both current-season — not a vs-career comparison like
        # the other three, since that's not how this metric is normally
        # used. Positive gap (FIP > ERA) means results have outrun the
        # pitcher's fielding-independent skill level right now — luck.
        _cur_era = _cur_fmt["era"] if _cur_fmt else None
        _cur_fip = _cur_fmt["fip"] if _cur_fmt else None
        _fip_gap = (_cur_fip - _cur_era) if (_cur_era is not None and _cur_fip is not None and _sample_ok) else None
        _fip_luck_tag = _luck_tier(_fip_gap, _FIP_SOMEWHAT, _FIP_VERY)

        result.append({
            "pid": pid, "name": p["name"], "age": p["age"],
            "ovr": _display_ovr, "role": role_str,
            "role_order": pos_order().get(role_str, 99),
            "surplus": round(p["surplus_yr1"] / _money_divisor(), 1) if p["surplus_yr1"] else 0,
            "pap": calc_pap(war, salaries.get(pid, 0), team_g, dpw, games_per_season=_games_per_season()),
            "pace_war": _paces.get(pid, {}).get("pace_war"),
            "pace_tier": _paces.get(pid, {}).get("pace_tier"),
            "pace_tier_label": _paces.get(pid, {}).get("pace_tier_label"),
            "pace_confidence": _paces.get(pid, {}).get("pace_confidence"),
            "is_two_way": pid in twp_pids,
            "career_babip": _r3(_c_babip), "babip_diff": _r3(_babip_gap), "luck": _babip_luck_tag,
            "career_hrfb": _r3(_c_hrfb), "hrfb_diff": _r3(_hrfb_gap), "hrfb_luck": _hrfb_luck_tag,
            "career_lob": _r3(_c_lob), "lob_diff": _r3(_lob_gap), "lob_luck": _lob_luck_tag,
            "fip_diff": _r3(_fip_gap), "fip_luck": _fip_luck_tag,
            "all_up_luck": _all_up_luck(_babip_luck_tag, _hrfb_luck_tag, _lob_luck_tag, _fip_luck_tag),
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


# Minimum current-season sample before a luck label means anything — BABIP is
# extremely noisy in small samples, so a 15-PA sample running .050 hot isn't
# "Very Lucky," it's just noise. Matches the ~80 PA / 150 BF thresholds
# already used elsewhere in the app for "is this sample real yet."
_BABIP_MIN_PA = 80
_BABIP_MIN_BF = 150


def _luck_tier(gap, somewhat, very):
    """gap is already sign-adjusted so positive always means "playing
    better than their real established level right now" (Lucky) and
    negative always means the opposite (Unlucky). `somewhat`/`very` are
    the absolute-gap thresholds for this particular metric's typical
    season-to-season noise — different metrics wobble by different
    amounts, so these aren't shared across metrics. Rule-of-thumb bands,
    not a calibrated model.
    """
    if gap is None:
        return None
    if gap >= very:
        return "Very Lucky"
    if gap >= somewhat:
        return "Somewhat Lucky"
    if gap > -somewhat:
        return "Neutral"
    if gap > -very:
        return "Unlucky"
    return "Very Unlucky"


# BABIP typically wobbles +/-.020-.030 a season on luck alone.
_BABIP_SOMEWHAT, _BABIP_VERY = 0.020, 0.040
# HR/FB-against is noisier than BABIP — league average sits ~10-12%, and a
# full season can drift +/-3-5 points on luck (contact quality) alone.
_HRFB_SOMEWHAT, _HRFB_VERY = 0.03, 0.05
# LOB% (strand rate) is the noisiest of the three — league average ~70-72%,
# with single-season swings of +/-5-8 points common even for a true-talent-
# neutral pitcher.
_LOB_SOMEWHAT, _LOB_VERY = 0.05, 0.08
# ERA-FIP gap is the one metric here with an established, calibrated scale
# (not a rule of thumb): a 0.50 gap is a real signal, 1.00 is substantial.
_FIP_SOMEWHAT, _FIP_VERY = 0.50, 1.00

# Moving qualification bar for RP/CL luck tags, matching the existing
# RP-threshold pattern in get_pitcher_percentiles() (percentiles.py) —
# 0.35 IP per team game played so far, translated to ~1.5 BF/IP-equivalent,
# with a floor so 1-2 outings in April never earn a tag.
_RP_BF_PER_TEAM_GAME = 1.5
_RP_BF_FLOOR = 20

_LUCK_SCORE = {"Very Lucky": 2, "Somewhat Lucky": 1, "Neutral": 0, "Unlucky": -1, "Very Unlucky": -2}
_SCORE_LUCK = {v: k for k, v in _LUCK_SCORE.items()}


def _babip_luck(gap):
    return _luck_tier(gap, _BABIP_SOMEWHAT, _BABIP_VERY)


def _all_up_luck(*tiers):
    """Average the per-metric tiers (skipping any that are None for lack
    of sample) into one overall label, so a player who's lucky on BABIP
    but unlucky on strand rate doesn't read as simply "lucky" — sorts and
    eyeballs as a genuine net read across every luck signal available.
    """
    scores = [_LUCK_SCORE[t] for t in tiers if t is not None]
    if not scores:
        return None
    avg = sum(scores) / len(scores)
    return _SCORE_LUCK[round(avg)]


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
# This "Notes" column is purely informational text now — it no longer drives
# any dimming/highlighting decision (see _personality_type_info below, which
# owns that job via OOTP's own "Type" personality archetype instead).
_TRAIT_NOTES = {
    "wrk_ethic":    {"H": ("buff", "Hard Worker"), "L": ("concern", "Low Work Ethic")},
    "int_":         {"H": ("buff", "High IQ"), "L": ("concern", "Low IQ")},
    "lead":         {"H": ("buff", "Leader"), "L": ("concern", "Low Leadership")},
    "loy":          {"H": ("buff", "Loyal"), "L": ("concern", "Low Loyalty")},
    "greed":        {"H": ("concern", "Greedy"), "L": ("buff", "Not Greedy")},
    "adaptability": {"H": ("buff", "Adaptable"), "L": ("concern", "Low Adaptability")},
}


def _personality_notes(intel, wrk_ethic, lead, loy, greed, adaptability=None):
    """Return (buffs, concerns) label lists from the six personality traits.

    Purely informational — displayed in the "Personality Notes" column,
    separate from (and no longer driving) the dim/highlight decision, which
    is owned by _personality_type_info() instead.
    """
    buffs, concerns = [], []
    for field, value in (("wrk_ethic", wrk_ethic), ("int_", intel),
                         ("lead", lead), ("loy", loy), ("greed", greed),
                         ("adaptability", adaptability)):
        note = _TRAIT_NOTES.get(field, {}).get(value)
        if not note:
            continue
        kind, label = note
        (buffs if kind == "buff" else concerns).append(label)
    return buffs, concerns


# OOTP's "Type" column — personality archetype, distinct from the WE/INT/
# Lead/Loy/Greed trait ratings above. This is now the sole driver of
# dim-negative/highlight-positive-personality across the site (replacing
# the old trait-concern-based dimming) — confirmed against real exports:
# Unknown, Normal, Sparkplug, Humble, Captain, Selfish, Outspoken,
# Unmotivated, Prankster, Fan Fav, Disruptive.
_PERSONALITY_TYPE_POSITIVE = {"Fan Fav", "Sparkplug", "Captain", "Humble", "Prankster"}
_PERSONALITY_TYPE_NEGATIVE = {"Selfish", "Outspoken", "Unmotivated", "Disruptive"}


# Trait-combo inference for players OOTP hasn't assigned a real Type to yet
# (blank/"Never Scouted" or in-game "Unknown") — most valuable for 16-20
# year olds who haven't been scouted long enough for a Type to pop, even
# though the underlying traits driving it are already visible. A real
# OOTP-assigned Type always wins over inference; this only fills the gap
# when there isn't one yet.
#
# Rules + confidence tiers below are measured, not guessed: mined against
# every player across both PPL and eMLB with a real confirmed OOTP Type and
# fully-scouted traits, checked 2026-09-17 (re-checked same day against
# 7,228 players after fixing a real data-pipeline bug — see note below).
# For each rule, "precision" = P(real Type == target | traits match this
# rule) in that dataset, and "recall" = P(traits match this rule | real
# Type == target). Outspoken has NO rule here because nothing beat a coin
# flip (best combo found: <50% precision) — it appears to be close to
# random with respect to these six traits, so guessing at it would just be
# noise dressed up as a signal.
#
# CORRECTION 2026-09-17: the original mining run undercounted Disruptive
# (only 46 confirmed players) because import_fa_asking_prices() silently
# dropped the Type/Adaptability columns from every free-agent CSV export —
# a released/unsigned player never appears in the roster export that DOES
# capture Type, so his personality data was lost entirely. Caught when
# Forrest flagged that the game was confirming Armando Peraza (a free
# agent) as Disruptive with Low Work Ethic + Low Leadership + Low Loyalty —
# exactly the original pre-this-session rule — while this app still showed
# him as unscouted. Fixed in custom_upload.py's import_fa_asking_prices()
# to persist Type/Adaptability the same way the roster importer does, and
# backfilled both leagues from the current FA exports (PPL: 5,217 -> 5,826
# confirmed-Type players; eMLB: 8,724 -> 10,062). Re-running the mining
# query against the corrected data (58 confirmed Disruptive, up from 46)
# reproduced the *exact* original rule as the actual best one:
#   Disruptive: Work Ethic=L, Leadership=L, Loyalty=L
#     -> 58/147 = 39% precision, but 100% RECALL — every single confirmed
#        Disruptive player in the dataset satisfies this combo. Disruptive
#        is rare (~0.8% base rate), so 39% precision is actually a ~49x
#        lift over guessing; precision alone undersold it. Kept as "Low"
#        confidence (39% still means most matches aren't Disruptive) but
#        this is a necessary-condition flag worth surfacing early, not a
#        weak guess — restoring the original rule that inspired this
#        feature in the first place.
#
#   Unmotivated: Work Ethic=L, Baseball IQ=L, Adaptability=L
#     -> 110/145 = 76% precision (High), 44% recall.
#   Selfish: Leadership=L, Loyalty=L, Greed=H
#     -> 226/314 = 72% precision (Medium), and catches 100% of every
#        confirmed Selfish player in the dataset (full recall).
#
# Same mining run also covered the 5 positive Types. Two clear the bar:
#   Sparkplug: Work Ethic=H, Greed=N, Adaptability=H
#     -> 310/489 = 63% precision (Medium), 76% recall.
#   Captain: Work Ethic=H, Leadership=H, Adaptability=N
#     -> 102/211 = 48% precision (Low), 44% recall.
# Humble, Prankster, and Fan Fav have NO rule here — best combos found for
# each topped out well under 50% precision, so none of them show any real
# trait signature in this data; guessing would just be noise.
#
# Checked in this order (most confident first, negative before positive at
# the same tier since a personality "concern" is more actionable), except
# Disruptive is checked ahead of Selfish's small overlap zone (Work
# Ethic=L, Leadership=L, Loyalty=L, Greed=H matches both rules; of those,
# 43 are actually Disruptive vs 36 Selfish) so a player matching both rules
# gets the outcome that's actually more common in that overlap.
def _infer_personality_type(wrk_ethic, lead, loy, greed, intel=None, adaptability=None):
    if wrk_ethic == "L" and intel == "L" and adaptability == "L":
        return {"label": "Likely Unmotivated (High confidence)", "class": "neg"}
    if wrk_ethic == "L" and lead == "L" and loy == "L":
        return {"label": "Likely Disruptive (Low confidence)", "class": "neg"}
    if lead == "L" and loy == "L" and greed == "H":
        return {"label": "Likely Selfish (Medium confidence)", "class": "neg"}
    if wrk_ethic == "H" and greed == "N" and adaptability == "H":
        return {"label": "Likely Sparkplug (Medium confidence)", "class": "pos"}
    if wrk_ethic == "H" and lead == "H" and adaptability == "N":
        return {"label": "Likely Captain (Low confidence)", "class": "pos"}
    return None


def _personality_type_info(ptype, wrk_ethic=None, lead=None, loy=None, greed=None, intel=None, adaptability=None):
    """Classify a raw personality_type value for display + dim/highlight.

    Returns {"label", "class"} where class is one of:
      "pos"       - positive archetype (bold/highlight candidate)
      "neg"       - negative archetype (dim candidate) — real Type or
                    trait-combo inference (see _infer_personality_type)
      "neutral"   - "Normal", genuinely no personality quirk
      "unknown"   - OOTP itself hasn't determined a type yet, and traits
                    don't match an inference rule either
      "unscouted" - this app has never synced a Type for this player at all
                    (NULL/blank — distinct from OOTP's own "Unknown", since
                    once a CSV sync covers them we'll know which it is),
                    and traits don't match an inference rule either
    """
    if ptype and ptype != "Unknown":
        if ptype == "Normal":
            return {"label": "Normal", "class": "neutral"}
        if ptype in _PERSONALITY_TYPE_POSITIVE:
            return {"label": ptype, "class": "pos"}
        if ptype in _PERSONALITY_TYPE_NEGATIVE:
            return {"label": ptype, "class": "neg"}
        return {"label": ptype, "class": "neutral"}

    inferred = _infer_personality_type(wrk_ethic, lead, loy, greed, intel, adaptability)
    if inferred:
        return inferred
    if not ptype:
        return {"label": "Never Scouted", "class": "unscouted"}
    return {"label": "Unknown", "class": "unknown"}


def _development_flags(wrk_ethic, intel, adaptability):
    """(good, bad) booleans for the "development" checkboxes — any of Work
    Ethic/Baseball IQ/Adaptability at H makes it "good", any at L makes it
    "bad" (both can be true at once for a mixed profile)."""
    values = (wrk_ethic, intel, adaptability)
    return ("H" in values, "L" in values)


# Mirrors the draft board's confidencePill()/_horizonFlag() (league.html) —
# same signals (bust-risk, scouting-Accuracy, dev-horizon, makeup concerns),
# ported server-side for the Jinja-rendered Farm System / All MiLB tables
# rather than the client-JS tables the draft board uses.
_CONFIDENCE_RISK_PENALTY = {"Low": 0, "Medium": 15, "High": 30, "Extreme": 50}
_CONFIDENCE_ACC_PENALTY = {"VH": 0, "H": 7, "A": 10, "M": 10, "L": 15, "VL": 20, "EL": 20, "F": 20}

# Real rostered farmhands have a real level, so — unlike the draft board's
# amateur-only OVR-bucketing fallback in prospect_surplus() — this can use
# the actual YEARS_TO_MLB-by-level table directly. Rookie/A-Short/lower are
# the long-horizon levels (3.5+ years by the same table fv_calc.py uses).
_LONG_HORIZON_LEVEL_KEYS = {"a-short", "usl", "dsl", "intl", "rookie"}
_LEVEL_INT_TO_KEY = {0: "draft", 2: "aaa", 3: "aa", 4: "a", 5: "a-short", 6: "usl", 8: "intl", 10: "draft", 11: "draft"}


def confidence_tier(risk, acc, has_personality_concern):
    """Same A-F combined-confidence grade as the draft board, computed
    server-side. Returns {"tier", "score", "reasons"}."""
    score = 100
    reasons = []
    risk_pen = _CONFIDENCE_RISK_PENALTY.get(risk, 0)
    if risk_pen:
        score -= risk_pen
        reasons.append(f"{risk} bust risk (-{risk_pen})")
    acc_pen = _CONFIDENCE_ACC_PENALTY.get(acc, 10 if acc else 0)
    if acc_pen:
        score -= acc_pen
        reasons.append(f"Scouting Acc {acc or '?'} (-{acc_pen})")
    if has_personality_concern:
        score -= 15
        reasons.append("Makeup concern (-15)")
    score = max(0, min(100, score))
    tier = "A" if score >= 85 else "B" if score >= 70 else "C" if score >= 50 else "D" if score >= 30 else "F"
    return {"tier": tier, "score": score, "reasons": reasons}


def horizon_flag(level_int):
    """True if this player's real level puts them at fv_calc.py's longest
    development-horizon tier (3.5+ years to MLB by YEARS_TO_MLB) — the
    level-based equivalent of the draft board's OVR-proxy horizon flag,
    made possible because rostered farmhands (unlike amateur draftees)
    have a real level to key off of."""
    try:
        level_key = _LEVEL_INT_TO_KEY.get(int(level_int))
    except (TypeError, ValueError):
        return False
    return level_key in _LONG_HORIZON_LEVEL_KEYS


def _personality_fields(intel, wrk_ethic, lead, loy, greed, adaptability, ptype):
    """One-call bundle of every personality-derived display/dim field an
    entry needs — buffs/concerns (Notes column text), personality_type/
    personality_type_class (Type column + dim/highlight driver), and
    dev_good/dev_bad (the separate development dim/highlight driver)."""
    buffs, concerns = _personality_notes(intel, wrk_ethic, lead, loy, greed, adaptability)
    type_info = _personality_type_info(ptype, wrk_ethic, lead, loy, greed, intel, adaptability)
    dev_good, dev_bad = _development_flags(wrk_ethic, intel, adaptability)
    return {
        "buffs": buffs, "concerns": concerns,
        "personality_type": type_info["label"], "personality_type_class": type_info["class"],
        "dev_good": dev_good, "dev_bad": dev_bad,
    }


# Continuous 0-100 "bang for buck" score for a draft prospect's signing
# bonus ask vs their multi-year surplus value — used by the Draft board's
# "Bonus Value" column so prospects can be ranked/sorted against each
# other directly, not just bucketed. score = 100 * (1 - ask/surplus): a
# $10 ask against $100K surplus scores ~100 (barely gives up any value to
# sign him); a $75K ask against $100K surplus scores 25 (giving up 3/4 of
# the value just to sign him). Clamped to [0, 100] since a) an ask above
# the full surplus value is just "bad," not "negative infinity bad," and
# b) "Slot" (no bonus premium demanded) is a free 100.
_TIER_THRESHOLDS = [(80, "pos"), (50, "neutral")]  # else "neg"


def _draft_bonus_verdict(surplus_millions, ask_dollars, special=None):
    """Returns {"score": float|None, "label": str, "class": str} for the
    Bonus Value column. score is None only when there's no ask on file at
    all — every other case (including Unsignable) gets a real 0-100 number
    so the column stays sortable. class mirrors the ptype-* CSS used
    elsewhere (pos/neutral/neg/unscouted) for at-a-glance coloring; label
    is a short read of the score, not an independent judgment.
    """
    if special == "unsignable":
        return {"score": 0.0, "label": "Unsignable", "class": "neg"}
    if ask_dollars is None:
        return {"score": None, "label": "No Ask", "class": "unscouted"}
    if surplus_millions is None or surplus_millions <= 0:
        return {"score": 0.0, "label": "0", "class": "neg"}
    ask_millions = ask_dollars / 1e6
    ratio = ask_millions / surplus_millions
    score = max(0.0, min(100.0, 100.0 * (1.0 - ratio)))
    for cutoff, cls in _TIER_THRESHOLDS:
        if score >= cutoff:
            return {"score": round(score, 1), "label": str(round(score)), "class": cls}
    return {"score": round(score, 1), "label": str(round(score)), "class": "neg"}


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


# Scouting accuracy grades trustworthy enough to act on without a fresh
# report — same "confirmed" cutoff used elsewhere (Best Available/Scouting
# Targets). Used to bucket Add Candidates lists so the confirmed, ready-to-
# act-on rows lead and the ones that need a scouting report before you can
# trust the grade follow — display order only, never changes which players
# make each list.
_ACC_CONFIRMED = {"H", "VH"}


def _split_acc(pool, key):
    """Split into (confirmed, unconfirmed) sub-lists, each still sorted by
    `key` — powers the "High Scouting Confidence" / "Needs Scouting"
    subsections on Add Candidates. Splitting (not just reordering) so each
    half can render as its own small table under its own label."""
    confirmed = sorted((e for e in pool if e.get("acc") in _ACC_CONFIRMED), key=key)
    unconfirmed = sorted((e for e in pool if e.get("acc") not in _ACC_CONFIRMED), key=key)
    return confirmed, unconfirmed


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


# bucket -> DEFENSIVE_WEIGHTS key for the vR/vL composite's defense blend —
# same mapping scouting_queries._COMPOSITE_DEF_BUCKET uses, duplicated here
# (not imported) since team_queries.py can't import from scouting_queries
# at module level — scouting_queries already imports several names from
# this module, so a module-level import back would be circular.
_ADD_COMPOSITE_DEF_BUCKET = {"C": "C", "SS": "SS", "2B": "2B", "3B": "3B",
                             "CF": "CF", "LF": "COF_LF", "RF": "COF_RF"}


def _add_candidate_vr_vl_def(ratings_scale, hitter_weights_by_bucket, bucket, fit,
                              cntct_r, gap_r, pow_r, eye_r, cntct_l, gap_l, pow_l, eye_l,
                              speed, steal, stl_rt,
                              c_frm, c_blk, c_arm, ifr, ife, ifa, tdp, ofr, ofe, ofa,
                              c, first_b, second_b, third_b, ss, lf, cf, rf,
                              transforms=None):
    """vR/vL composite (same compute_composite_hitter blend Best Available/
    Defense use) plus a raw Def Rating at the "Fits At" position — only
    computed when a fit position exists at all, since Def Rating is
    meaningless without knowing which position to grade. A combined
    "LF/RF" fit shows whichever corner grades higher.

    speed/steal/stl_rt don't vary by pitcher handedness, so the same values
    go into both vr_tools and vl_tools — omitting them entirely (as this
    function used to) silently drops the whole baserunning share of the
    composite (compute_composite_hitter treats an absent tool as "no data"
    and skips it rather than renormalizing around it) and never lets the
    speed×contact synergy bonus fire, systematically understating any
    real base-stealing threat's vR/vL relative to his actual overall Comp.
    """
    from statsplusplus.config.ratings import norm as _norm_rating, norm_continuous as _normc2
    from statsplusplus.evaluation.composite import compute_composite_hitter
    from statsplusplus.evaluation.constants import DEFENSIVE_WEIGHTS

    weights = hitter_weights_by_bucket.get(bucket, hitter_weights_by_bucket.get("COF", {}))
    _base = {"speed": _normc2(speed, ratings_scale), "steal": _normc2(steal, ratings_scale),
              "stl_rt": _normc2(stl_rt, ratings_scale)}
    vr_tools = {**_base, "contact": _normc2(cntct_r, ratings_scale), "gap": _normc2(gap_r, ratings_scale),
                "power": _normc2(pow_r, ratings_scale), "eye": _normc2(eye_r, ratings_scale)}
    vl_tools = {**_base, "contact": _normc2(cntct_l, ratings_scale), "gap": _normc2(gap_l, ratings_scale),
                "power": _normc2(pow_l, ratings_scale), "eye": _normc2(eye_l, ratings_scale)}
    def_bucket = _ADD_COMPOSITE_DEF_BUCKET.get(bucket)
    if def_bucket:
        def_weights = DEFENSIVE_WEIGHTS.get(def_bucket, {})
        defense = {"CFrm": _normc2(c_frm, ratings_scale), "CBlk": _normc2(c_blk, ratings_scale),
                   "CArm": _normc2(c_arm, ratings_scale), "IFR": _normc2(ifr, ratings_scale),
                   "IFE": _normc2(ife, ratings_scale), "IFA": _normc2(ifa, ratings_scale),
                   "TDP": _normc2(tdp, ratings_scale), "OFR": _normc2(ofr, ratings_scale),
                   "OFE": _normc2(ofe, ratings_scale), "OFA": _normc2(ofa, ratings_scale)}
    else:
        def_weights, defense = {}, {}
    try:
        vr = compute_composite_hitter(vr_tools, weights, defense, def_weights, transforms)
        vl = compute_composite_hitter(vl_tools, weights, defense, def_weights, transforms)
    except Exception:
        vr = vl = None

    def_rating = None
    if fit:
        _pos_def_map = {"C": c, "1B": first_b, "2B": second_b, "3B": third_b,
                        "SS": ss, "LF": lf, "CF": cf, "RF": rf}
        grades = [g for g in (_norm_rating(_pos_def_map.get(p), ratings_scale)
                              for p in fit.split("/")) if g is not None]
        if grades:
            def_rating = max(grades)
    return vr, vl, def_rating


def _add_candidate_pitcher_vr_vl(ratings_scale, pitcher_weights, role,
                                  stf, mov, ctrl, stf_r, mov_r, ctrl_r, stf_l, mov_l, ctrl_l,
                                  arsenal, stamina, transforms=None):
    """Pitcher vR/vL composite — same compute_composite_pitcher blend Custom
    Upload/Scouting Targets use. Previously not computed at all on this page
    (every pitcher showed blank vR/vL here despite the data existing
    elsewhere in the app); this fills that gap.
    """
    from statsplusplus.config.ratings import norm_continuous as _normc2
    from statsplusplus.evaluation.composite import compute_composite_pitcher

    weights = pitcher_weights.get(role, {})
    _base = {"stuff": _normc2(stf, ratings_scale), "movement": _normc2(mov, ratings_scale),
             "control": _normc2(ctrl, ratings_scale)}
    vr_tools = dict(_base)
    if stf_r is not None:
        vr_tools["stuff"] = _normc2(stf_r, ratings_scale)
    if mov_r is not None:
        vr_tools["movement"] = _normc2(mov_r, ratings_scale)
    if ctrl_r is not None:
        vr_tools["control"] = _normc2(ctrl_r, ratings_scale)
    vl_tools = dict(_base)
    if stf_l is not None:
        vl_tools["stuff"] = _normc2(stf_l, ratings_scale)
    if mov_l is not None:
        vl_tools["movement"] = _normc2(mov_l, ratings_scale)
    if ctrl_l is not None:
        vl_tools["control"] = _normc2(ctrl_l, ratings_scale)
    try:
        vr = compute_composite_pitcher(vr_tools, weights, arsenal or {}, stamina or 50, role, transforms)
        vl = compute_composite_pitcher(vl_tools, weights, arsenal or {}, stamina or 50, role, transforms)
    except Exception:
        vr = vl = None
    return vr, vl


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
               pf.fv_continuous, ps.fv,
               r.cntct, r.gap, r.pow, r.eye, r.stf, r.mov, r.ctrl, r.bats,
               r.pot_cntct, r.pot_gap, r.pot_pow, r.pot_stf, r.pot_mov, r.pot_ctrl,
               r.adaptability, r.personality_type,
               r.cntct_r, r.gap_r, r.pow_r, r.eye_r, r.cntct_l, r.gap_l, r.pow_l, r.eye_l,
               r.speed, r.steal, r.stl_rt,
               r.stf_r, r.mov_r, r.ctrl_r, r.stf_l, r.mov_l, r.ctrl_l, r.stm,
               r.fst, r.snk, r.crv, r.sld, r.chg, r.splt, r.cutt, r.cir_chg, r.scr, r.frk, r.kncrv, r.knbl,
               r.c_frm, r.c_blk, r.c_arm, r.ifr, r.ife, r.ifa, r.tdp, r.ofr, r.ofe, r.ofa,
               r.c, r.first_b, r.second_b, r.third_b, r.ss, r.lf, r.cf, r.rf, r.pot_eye
        FROM players p
        LEFT JOIN latest_ratings r ON p.player_id = r.player_id
        LEFT JOIN prospect_fv pf ON pf.player_id = p.player_id AND pf.eval_date = ?
        LEFT JOIN teams t ON t.team_id = p.team_id
        LEFT JOIN player_surplus ps ON ps.player_id = p.player_id AND ps.eval_date = ?
        WHERE p.is_on_waivers = 1 AND p.team_id != ?
    """, (ed, ed_surplus, tid)).fetchall()

    from statsplusplus.config.ratings import norm_continuous as _normc
    from statsplusplus.evaluation.park_fit import (
        load_park_factors, compute_batter_park_fit, compute_batter_park_value_pct,
        compute_pitcher_park_fit_from_stats, compute_pitcher_park_fit_from_tools,
        compute_pitcher_park_value_pct_from_stats, compute_pitcher_park_value_pct_from_tools,
    )
    ratings_scale = get_cfg().ratings_scale
    park = load_park_factors(get_cfg().league_dir)
    # Tool weights (and the per-tool transform curves nested inside) drive
    # every vR/vL and Comp calc below — unrelated to whether park factors
    # loaded, so must NOT be gated behind `if park` (that used to silently
    # zero out every hitter's vR/vL/def rating whenever park factors were
    # unavailable, e.g. a brand-new league).
    _all_weights = load_tool_weights(get_cfg().league_dir)
    hitter_weights_by_bucket = _all_weights.get("hitter", {})
    pitcher_weights = _all_weights.get("pitcher", {})
    _transforms = _all_weights.get("tool_transforms", {}) or {}
    hitter_transforms = _transforms.get("hitter")

    # Same real-observed-vs-tools-proxy fallback as get_free_agent_candidates()
    # — home park fit for any waiver-wire pitcher with a real track record.
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

    out = []
    for r in rows:
        (pid, name, age, level, pos, role, cur_tid, intel, wrk_ethic, lead, loy,
         greed, acc, comp, ceil_score, true_ceil, fv, fv_str, pf_bucket, cur_name,
         surplus_raw, prospect_surplus_raw, fv_continuous, ps_fv,
         cntct, gap, pow_, eye, stf, mov, ctrl, bats,
         pot_cntct, pot_gap, pot_pow, pot_stf, pot_mov, pot_ctrl,
         adaptability, ptype,
         cntct_r, gap_r, pow_r, eye_r, cntct_l, gap_l, pow_l, eye_l,
         speed, steal, stl_rt,
         stf_r, mov_r, ctrl_r, stf_l, mov_l, ctrl_l, stamina,
         fst, snk, crv, sld, chg, splt, cutt, cir_chg, scr, frk, kncrv, knbl,
         c_frm, c_blk, c_arm, ifr, ife, ifa, tdp, ofr, ofe, ofa,
         def_c, def_1b, def_2b, def_3b, def_ss, def_lf, def_cf, def_rf, pot_eye) = r
        bucket = _bucket_for_display(pf_bucket, role, pos)
        potential = true_ceil if true_ceil is not None else ceil_score
        _pers = _personality_fields(intel, wrk_ethic, lead, loy, greed, adaptability, ptype)
        level_disp = level_map().get(str(level)) or ("FA" if str(level)=="0" else str(level))
        is_pitcher = role in ROLE_MAP
        _fit = _fit_position(bucket, weak_positions)
        bat_ovr = bat_pot = bat_vr = bat_vl = None

        if is_pitcher:
            _tools = {"stuff": _normc(stf, ratings_scale), "movement": _normc(mov, ratings_scale),
                      "control": _normc(ctrl, ratings_scale)}
            role_key = "RP" if ROLE_MAP[role] in ("RP", "CL") else "SP"
            _arsenal = {n: nv for n, v in (
                ("Fst", fst), ("Snk", snk), ("Crv", crv), ("Sld", sld), ("Chg", chg),
                ("Splt", splt), ("Cutt", cutt), ("CirChg", cir_chg), ("Scr", scr),
                ("Frk", frk), ("Kncrv", kncrv), ("Knbl", knbl)) for nv in [_normc(v, ratings_scale)] if nv is not None}
            _pitcher_transforms = _transforms.get(role_key)
            vr_composite, vl_composite = _add_candidate_pitcher_vr_vl(
                ratings_scale, pitcher_weights, role_key,
                stf, mov, ctrl, stf_r, mov_r, ctrl_r, stf_l, mov_l, ctrl_l,
                _arsenal, _normc(stamina, ratings_scale), _pitcher_transforms)
            def_rating = None
        else:
            _tools = {"contact": _normc(cntct, ratings_scale), "gap": _normc(gap, ratings_scale),
                      "power": _normc(pow_, ratings_scale), "eye": _normc(eye, ratings_scale)}
            vr_composite, vl_composite, def_rating = _add_candidate_vr_vl_def(
                ratings_scale, hitter_weights_by_bucket, bucket, _fit,
                cntct_r, gap_r, pow_r, eye_r, cntct_l, gap_l, pow_l, eye_l,
                speed, steal, stl_rt,
                c_frm, c_blk, c_arm, ifr, ife, ifa, tdp, ofr, ofe, ofa,
                def_c, def_1b, def_2b, def_3b, def_ss, def_lf, def_cf, def_rf,
                hitter_transforms)
            # Simple pure Contact/Gap/Power/Eye weighted average (no
            # defense/speed/transforms) — separate & simpler than
            # vr_composite/vl_composite above.
            _bw = hitter_weights_by_bucket.get(bucket, hitter_weights_by_bucket.get("COF", {}))
            bat_ovr = compute_batting_composite(
                _normc(cntct, ratings_scale), _normc(gap, ratings_scale),
                _normc(pow_, ratings_scale), _normc(eye, ratings_scale), _bw)
            bat_pot = compute_batting_composite(
                _normc(pot_cntct, ratings_scale), _normc(pot_gap, ratings_scale),
                _normc(pot_pow, ratings_scale), _normc(pot_eye, ratings_scale), _bw)
            bat_vr = compute_batting_composite(
                _normc(cntct_r, ratings_scale), _normc(gap_r, ratings_scale),
                _normc(pow_r, ratings_scale), _normc(eye_r, ratings_scale), _bw)
            bat_vl = compute_batting_composite(
                _normc(cntct_l, ratings_scale), _normc(gap_l, ratings_scale),
                _normc(pow_l, ratings_scale), _normc(eye_l, ratings_scale), _bw)

        # Not-yet-MLB players are scored on potential tools (their current
        # tools are barely developed and not the real signal) — same
        # convention as get_free_agent_candidates().
        if level_disp != "MLB":
            if is_pitcher:
                _park_tools = {"stuff": _normc(pot_stf, ratings_scale), "movement": _normc(pot_mov, ratings_scale),
                               "control": _normc(pot_ctrl, ratings_scale)}
            else:
                _park_tools = {"contact": _normc(pot_cntct, ratings_scale), "gap": _normc(pot_gap, ratings_scale),
                               "power": _normc(pot_pow, ratings_scale)}
        else:
            _park_tools = _tools

        park_fit = None
        park_value = None
        _value_pct = None
        if park:
            if is_pitcher:
                obs = pitcher_stats.get(pid)
                if obs and obs["gb_pct"] is not None and lg_gb_pct is not None:
                    park_fit = compute_pitcher_park_fit_from_stats(
                        obs["gb_pct"], obs["k_pct"], obs["bb_pct"],
                        lg_gb_pct, lg_k_pct, lg_bb_pct, park)
                    _value_pct = compute_pitcher_park_value_pct_from_stats(
                        obs["gb_pct"], obs["k_pct"], obs["bb_pct"],
                        lg_gb_pct, lg_k_pct, lg_bb_pct, park)
                else:
                    park_fit = compute_pitcher_park_fit_from_tools(_park_tools, park)
                    _value_pct = compute_pitcher_park_value_pct_from_tools(_park_tools, park)
            else:
                _hw = hitter_weights_by_bucket.get(bucket, hitter_weights_by_bucket.get("COF", {}))
                park_fit = compute_batter_park_fit(_park_tools, bats, _hw, park)
                _value_pct = compute_batter_park_value_pct(_park_tools, bats, _hw, park)

            _park_val_basis = surplus_raw if surplus_raw is not None else prospect_surplus_raw
            if _value_pct is not None and _park_val_basis is not None:
                park_value = round((_park_val_basis * _value_pct) / _money_divisor(), 1)

        # Waiver-wire players are established, currently-rostered MLB players
        # (real contracts) — prospect_fv has no row for them at all (that
        # table is prospects/FAs/rookie-eligible only), so fv_continuous is
        # always None here and _surplus_horizons_live/_peak_surplus (both
        # FV-projection based) would silently return None for every one of
        # these columns. Prefer the same real-contract-schedule calculation
        # the Contracts tab uses (contract_surplus_horizons); fall back to
        # the FV-based live estimate — using player_surplus.fv (ps_fv) as the
        # fv_continuous input, same substitution get_contracts already uses
        # for peak_surplus — only if no contract is on file.
        _fv_for_horizons = fv_continuous if fv_continuous is not None else ps_fv
        try:
            from contract_value import contract_surplus_horizons as _csh
            _cur_raw, _next_raw, _three_raw = _csh(pid, get_cfg().year, league_dir=get_cfg().league_dir)
        except Exception:
            _cur_raw = _next_raw = _three_raw = None
        if _cur_raw is None and _next_raw is None and _three_raw is None:
            _cur_s, _next_s, _three_s = _surplus_horizons_live(
                _fv_for_horizons, age, level_disp, bucket, ovr=comp, pot=potential)
        else:
            _cur_s = round(_cur_raw / _money_divisor(), 1) if _cur_raw is not None else None
            _next_s = round(_next_raw / _money_divisor(), 1) if _next_raw is not None else None
            _three_s = round(_three_raw / _money_divisor(), 1) if _three_raw is not None else None
        out.append({
            "pid": pid, "name": name, "age": age,
            "level": level_disp,
            "bucket": bucket,
            "cur_team_id": cur_tid, "cur_team_name": cur_name or str(cur_tid),
            "composite_score": comp, "potential": potential, "fv_str": fv_str,
            "acc": acc, **_pers,
            "fit": _fit,
            "vr_composite": vr_composite, "vl_composite": vl_composite, "def_rating": def_rating,
            "bat_ovr": bat_ovr, "bat_pot": bat_pot, "bat_vr": bat_vr, "bat_vl": bat_vl,
            "park_fit": park_fit, "park_value": park_value,
            "surplus": round((surplus_raw if surplus_raw is not None else prospect_surplus_raw) / _money_divisor(), 1)
                       if (surplus_raw is not None or prospect_surplus_raw is not None) else None,
            "peak_surplus": _peak_surplus(_fv_for_horizons, age, level_disp, bucket, ovr=comp, pot=potential),
            "current_year_surplus": _cur_s, "next_year_surplus": _next_s, "three_year_surplus": _three_s,
        })
    confirmed, unconfirmed = _split_acc(out, lambda e: -(e["composite_score"] or 0))
    return {"confirmed": confirmed, "unconfirmed": unconfirmed}


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

# Recommended international-signing bid, PPL only (25-year control makes a
# hit here worth uniquely more than in a standard-FA league — not meaningful
# under eMLB's normal arb/FA timeline). "Long-Term Surplus" for these
# entries is already a probability-weighted figure (prospect_surplus_with_
# option blends in dev-discount ~0.35 at Intl level plus upside-scenario
# option value — see scripts/prospect_value.py), so bidding a fixed
# fraction of it directly targets a fixed expected-return multiple on the
# signing bonus dollar, independent of hit rate: at 12%, expected return is
# ~1/0.12 ≈ 8x per dollar bid, in expectation, as long as that EV figure is
# unbiased. This is why the "budget for an ~8-9 in 10 bust rate" framing
# and "bid a fraction of expected surplus" framing are the same idea, not
# two separate risk controls to stack.
_INTL_BID_FRACTION = 0.12
# Scouting accuracy shifts confidence in the underlying grade — a 16yo dart
# throw with unusually high scout confidence is a stronger signal than the
# same grade at "Average," and a "Low" accuracy grade means the surplus
# estimate itself carries wider error than the model already assumes.
_INTL_BID_ACC_MULT = {"VH": 1.15, "H": 1.05, "A": 1.00, "L": 0.80, "VL": 0.65}


def _recommended_intl_bid(surplus_raw, acc):
    if surplus_raw is None or surplus_raw <= 0:
        return None
    mult = _INTL_BID_ACC_MULT.get(acc, 1.00)
    return round((surplus_raw * _INTL_BID_FRACTION * mult) / _money_divisor(), 1)


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
    _intl_bids_enabled = get_cfg().perpetual_arb

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
               pf.fv_continuous, fap.ask_raw, ps.fv,
               r.cntct, r.gap, r.pow, r.eye, r.stf, r.mov, r.ctrl, r.bats,
               r.pot_cntct, r.pot_gap, r.pot_pow, r.pot_stf, r.pot_mov, r.pot_ctrl,
               r.adaptability, r.personality_type,
               r.cntct_r, r.gap_r, r.pow_r, r.eye_r, r.cntct_l, r.gap_l, r.pow_l, r.eye_l,
               r.speed, r.steal, r.stl_rt,
               r.stf_r, r.mov_r, r.ctrl_r, r.stf_l, r.mov_l, r.ctrl_l, r.stm,
               r.fst, r.snk, r.crv, r.sld, r.chg, r.splt, r.cutt, r.cir_chg, r.scr, r.frk, r.kncrv, r.knbl,
               r.c_frm, r.c_blk, r.c_arm, r.ifr, r.ife, r.ifa, r.tdp, r.ofr, r.ofe, r.ofa,
               r.c, r.first_b, r.second_b, r.third_b, r.ss, r.lf, r.cf, r.rf, r.pot_eye
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
        load_park_factors, compute_batter_park_fit, compute_batter_park_value_pct,
        compute_pitcher_park_fit_from_stats, compute_pitcher_park_fit_from_tools,
        compute_pitcher_park_value_pct_from_stats, compute_pitcher_park_value_pct_from_tools,
    )
    ratings_scale = get_cfg().ratings_scale
    park = load_park_factors(get_cfg().league_dir)
    # See get_waiver_candidates for why this must not be gated behind `if park`.
    _all_weights = load_tool_weights(get_cfg().league_dir)
    hitter_weights_by_bucket = _all_weights.get("hitter", {})
    pitcher_weights = _all_weights.get("pitcher", {})
    _transforms = _all_weights.get("tool_transforms", {}) or {}
    hitter_transforms = _transforms.get("hitter")

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
         prospect_surplus_raw, fv_continuous, ask_raw, ps_fv,
         cntct, gap, pow_, eye, stf, mov, ctrl, bats,
         pot_cntct, pot_gap, pot_pow, pot_stf, pot_mov, pot_ctrl,
         adaptability, ptype,
         cntct_r, gap_r, pow_r, eye_r, cntct_l, gap_l, pow_l, eye_l,
         speed, steal, stl_rt,
         stf_r, mov_r, ctrl_r, stf_l, mov_l, ctrl_l, stamina,
         fst, snk, crv, sld, chg, splt, cutt, cir_chg, scr, frk, kncrv, knbl,
         c_frm, c_blk, c_arm, ifr, ife, ifa, tdp, ofr, ofe, ofa,
         def_c, def_1b, def_2b, def_3b, def_ss, def_lf, def_cf, def_rf, pot_eye) = r
        bucket = _bucket_for_display(pf_bucket, role, pos)
        potential = true_ceil if true_ceil is not None else ceil_score
        _pers = _personality_fields(intel, wrk_ethic, lead, loy, greed, adaptability, ptype)
        level_disp = level_map().get(str(level)) or ("FA" if str(level)=="0" else str(level))
        is_pitcher = role in ROLE_MAP
        _fit = _fit_position(bucket, weak_positions)
        bat_ovr = bat_pot = bat_vr = bat_vl = None
        if is_pitcher:
            _tools = {"stuff": _normc(stf, ratings_scale), "movement": _normc(mov, ratings_scale),
                      "control": _normc(ctrl, ratings_scale)}
            role_key = "RP" if ROLE_MAP[role] in ("RP", "CL") else "SP"
            _arsenal = {n: nv for n, v in (
                ("Fst", fst), ("Snk", snk), ("Crv", crv), ("Sld", sld), ("Chg", chg),
                ("Splt", splt), ("Cutt", cutt), ("CirChg", cir_chg), ("Scr", scr),
                ("Frk", frk), ("Kncrv", kncrv), ("Knbl", knbl)) for nv in [_normc(v, ratings_scale)] if nv is not None}
            _pitcher_transforms = _transforms.get(role_key)
            vr_composite, vl_composite = _add_candidate_pitcher_vr_vl(
                ratings_scale, pitcher_weights, role_key,
                stf, mov, ctrl, stf_r, mov_r, ctrl_r, stf_l, mov_l, ctrl_l,
                _arsenal, _normc(stamina, ratings_scale), _pitcher_transforms)
            def_rating = None
        else:
            _tools = {"contact": _normc(cntct, ratings_scale), "gap": _normc(gap, ratings_scale),
                      "power": _normc(pow_, ratings_scale), "eye": _normc(eye, ratings_scale)}
            vr_composite, vl_composite, def_rating = _add_candidate_vr_vl_def(
                ratings_scale, hitter_weights_by_bucket, bucket, _fit,
                cntct_r, gap_r, pow_r, eye_r, cntct_l, gap_l, pow_l, eye_l,
                speed, steal, stl_rt,
                c_frm, c_blk, c_arm, ifr, ife, ifa, tdp, ofr, ofe, ofa,
                def_c, def_1b, def_2b, def_3b, def_ss, def_lf, def_cf, def_rf,
                hitter_transforms)
            _bw = hitter_weights_by_bucket.get(bucket, hitter_weights_by_bucket.get("COF", {}))
            bat_ovr = compute_batting_composite(
                _normc(cntct, ratings_scale), _normc(gap, ratings_scale),
                _normc(pow_, ratings_scale), _normc(eye, ratings_scale), _bw)
            bat_pot = compute_batting_composite(
                _normc(pot_cntct, ratings_scale), _normc(pot_gap, ratings_scale),
                _normc(pot_pow, ratings_scale), _normc(pot_eye, ratings_scale), _bw)
            bat_vr = compute_batting_composite(
                _normc(cntct_r, ratings_scale), _normc(gap_r, ratings_scale),
                _normc(pow_r, ratings_scale), _normc(eye_r, ratings_scale), _bw)
            bat_vl = compute_batting_composite(
                _normc(cntct_l, ratings_scale), _normc(gap_l, ratings_scale),
                _normc(pow_l, ratings_scale), _normc(eye_l, ratings_scale), _bw)
        spec_score = compute_specialist_score(_tools, is_pitcher)

        # Park fit/value for a not-yet-MLB player should reflect what he'll
        # grow into, not his barely-developed current tools (a 16yo's
        # current Power is close to meaningless — his Pot is the signal).
        # Established MLB free agents use current tools as before: their
        # potential IS effectively already realized.
        if level_disp != "MLB":
            if is_pitcher:
                _park_tools = {"stuff": _normc(pot_stf, ratings_scale), "movement": _normc(pot_mov, ratings_scale),
                               "control": _normc(pot_ctrl, ratings_scale)}
            else:
                _park_tools = {"contact": _normc(pot_cntct, ratings_scale), "gap": _normc(pot_gap, ratings_scale),
                               "power": _normc(pot_pow, ratings_scale)}
        else:
            _park_tools = _tools

        park_fit = None
        park_value = None
        _value_pct = None
        if park:
            if is_pitcher:
                obs = pitcher_stats.get(pid)
                if obs and obs["gb_pct"] is not None and lg_gb_pct is not None:
                    park_fit = compute_pitcher_park_fit_from_stats(
                        obs["gb_pct"], obs["k_pct"], obs["bb_pct"],
                        lg_gb_pct, lg_k_pct, lg_bb_pct, park)
                    _value_pct = compute_pitcher_park_value_pct_from_stats(
                        obs["gb_pct"], obs["k_pct"], obs["bb_pct"],
                        lg_gb_pct, lg_k_pct, lg_bb_pct, park)
                else:
                    park_fit = compute_pitcher_park_fit_from_tools(_park_tools, park)
                    _value_pct = compute_pitcher_park_value_pct_from_tools(_park_tools, park)
            else:
                _hw = hitter_weights_by_bucket.get(bucket, hitter_weights_by_bucket.get("COF", {}))
                park_fit = compute_batter_park_fit(_park_tools, bats, _hw, park)
                _value_pct = compute_batter_park_value_pct(_park_tools, bats, _hw, park)

            # surplus_raw can be a genuine 0 (a real, below-replacement
            # valuation) — must check "is not None" rather than truthiness,
            # or a legitimate $0.0 park value silently renders blank instead
            # (this is exactly what happened before this fix).
            _park_val_basis = surplus_raw if surplus_raw is not None else prospect_surplus_raw
            if _value_pct is not None and _park_val_basis is not None:
                park_value = round((_park_val_basis * _value_pct) / _money_divisor(), 1)

        # Established MLB free agents (a released veteran, say) have no
        # prospect_fv row (that table is prospects/FAs/rookie-eligible by FV,
        # not established vets) so fv_continuous is None and the FV-based
        # horizon/peak projections would silently come back empty — fall back
        # to player_surplus.fv (ps_fv), same substitution get_contracts uses
        # for peak_surplus on the Contracts tab. True free agents have no
        # contract on file (contract_surplus_horizons would find nothing), so
        # unlike waiver candidates this always uses the FV-based estimate,
        # just with the right fv_continuous input for established players too.
        _fv_for_horizons = fv_continuous if fv_continuous is not None else ps_fv
        _cur_s, _next_s, _three_s = _surplus_horizons_live(_fv_for_horizons, age, level_disp,
                                                            bucket, ovr=comp, pot=potential)
        entry = {
            "pid": pid, "name": name, "age": age,
            "level": level_disp,
            "bucket": bucket, "composite_score": comp, "potential": potential,
            "fv": fv, "fv_str": fv_str, "acc": acc, **_pers,
            "fit": _fit,
            "vr_composite": vr_composite, "vl_composite": vl_composite, "def_rating": def_rating,
            "bat_ovr": bat_ovr, "bat_pot": bat_pot, "bat_vr": bat_vr, "bat_vl": bat_vl,
            "surplus": round((surplus_raw if surplus_raw is not None else prospect_surplus_raw) / _money_divisor(), 1)
                       if (surplus_raw is not None or prospect_surplus_raw is not None) else None,
            "peak_surplus": _peak_surplus(_fv_for_horizons, age, level_disp, bucket, ovr=comp, pot=potential),
            "current_year_surplus": _cur_s, "next_year_surplus": _next_s, "three_year_surplus": _three_s,
            "ask": ask_raw or "MiLC",
            "specialist_score": spec_score, "specialist_label": _spec_label(spec_score),
            "park_fit": park_fit, "park_value": park_value,
        }
        if age is not None and age <= _INTL_FA_AGE_MAX:
            if _intl_bids_enabled:
                _bid_surplus_raw = surplus_raw if surplus_raw is not None else prospect_surplus_raw
                entry["recommended_bid"] = _recommended_intl_bid(_bid_surplus_raw, acc)
            (intl_pitchers if is_pitcher else intl_hitters).append(entry)
        else:
            (pitchers if is_pitcher else hitters).append(entry)

    def _top_pct(pool, key, min_count=1):
        sorted_pool = sorted(pool, key=key)
        n = max(min_count, int(len(sorted_pool) * _FA_TOP_PCT)) if sorted_pool else 0
        # Selection (who makes the top-N% cut) stays purely by `key`; the
        # confidence split only divides that already-selected slice into
        # two display groups, so it can't bump anyone off the list.
        return _split_acc(sorted_pool[:n], key)

    def _ovr_key(e):
        return -(e["composite_score"] or 0)

    def _prospect_key(e):
        # FV is the more authoritative grade when available; fall back to
        # raw potential ceiling for players not in prospect_fv.
        return -(e["fv"] if e["fv"] is not None else (e["potential"] or 0))

    young_hitters = [e for e in hitters if e["age"] is not None and e["age"] <= _FA_PROSPECT_AGE_MAX]
    young_pitchers = [e for e in pitchers if e["age"] is not None and e["age"] <= _FA_PROSPECT_AGE_MAX]
    # "Clean" = not a negative personality Type (the site-wide dim signal),
    # not the old trait-concerns text.
    clean_hitters = [e for e in hitters if e["personality_type_class"] != "neg"]
    clean_pitchers = [e for e in pitchers if e["personality_type_class"] != "neg"]
    clean_young_hitters = [e for e in young_hitters if e["personality_type_class"] != "neg"]
    clean_young_pitchers = [e for e in young_pitchers if e["personality_type_class"] != "neg"]

    def _fv_min(pool):
        qualifying = [e for e in pool if e["fv"] is not None and e["fv"] >= _FA_PROSPECT_MIN_FV]
        return _split_acc(qualifying, _prospect_key)

    hitters_confirmed, hitters_unconfirmed = _top_pct(hitters, _ovr_key)
    pitchers_confirmed, pitchers_unconfirmed = _top_pct(pitchers, _ovr_key)
    young_hitters_confirmed, young_hitters_unconfirmed = _fv_min(young_hitters)
    young_pitchers_confirmed, young_pitchers_unconfirmed = _fv_min(young_pitchers)
    clean_hitters_confirmed, clean_hitters_unconfirmed = _top_pct(clean_hitters, _ovr_key, min_count=5)
    clean_pitchers_confirmed, clean_pitchers_unconfirmed = _top_pct(clean_pitchers, _ovr_key, min_count=5)
    clean_young_hitters_confirmed, clean_young_hitters_unconfirmed = _fv_min(clean_young_hitters)
    clean_young_pitchers_confirmed, clean_young_pitchers_unconfirmed = _fv_min(clean_young_pitchers)
    # No FV floor here — these are 15-17yo amateurs being browsed as a pool,
    # not a curated "worth signing now" cut, so an FV threshold tuned for
    # domestic prospects doesn't apply.
    international_hitters_confirmed, international_hitters_unconfirmed = _split_acc(intl_hitters, _prospect_key)
    international_pitchers_confirmed, international_pitchers_unconfirmed = _split_acc(intl_pitchers, _prospect_key)

    return {"hitters_confirmed": hitters_confirmed, "hitters_unconfirmed": hitters_unconfirmed,
            "pitchers_confirmed": pitchers_confirmed, "pitchers_unconfirmed": pitchers_unconfirmed,
            "young_hitters_confirmed": young_hitters_confirmed, "young_hitters_unconfirmed": young_hitters_unconfirmed,
            "young_pitchers_confirmed": young_pitchers_confirmed, "young_pitchers_unconfirmed": young_pitchers_unconfirmed,
            "clean_hitters_confirmed": clean_hitters_confirmed, "clean_hitters_unconfirmed": clean_hitters_unconfirmed,
            "clean_pitchers_confirmed": clean_pitchers_confirmed, "clean_pitchers_unconfirmed": clean_pitchers_unconfirmed,
            "clean_young_hitters_confirmed": clean_young_hitters_confirmed,
            "clean_young_hitters_unconfirmed": clean_young_hitters_unconfirmed,
            "clean_young_pitchers_confirmed": clean_young_pitchers_confirmed,
            "clean_young_pitchers_unconfirmed": clean_young_pitchers_unconfirmed,
            "international_hitters_confirmed": international_hitters_confirmed,
            "international_hitters_unconfirmed": international_hitters_unconfirmed,
            "international_pitchers_confirmed": international_pitchers_confirmed,
            "international_pitchers_unconfirmed": international_pitchers_unconfirmed,
            "intl_age_max": _INTL_FA_AGE_MAX,
            "prospect_min_fv": _FA_PROSPECT_MIN_FV,
            "prospect_age_max": _FA_PROSPECT_AGE_MAX,
            "top_pct": int(_FA_TOP_PCT * 100),
            "total_fa": total_fa, "nippon_excluded": nippon_excluded,
            "draft_pool_excluded": draft_pool_excluded, "signable_pool": signable_pool}


def get_farm(team_id=None, limit=None):
    """Farm system players who meaningfully contribute to farm surplus —
    age <= 25 (this app's standard prospect cutoff) and FV >= 40 (same
    "real prospect" bar the Farm Depth panel's counts already use), so this
    stays consistent with every other "prospect" list in the app rather than
    including every replacement-level farmhand with a prospect_fv row.

    limit=None returns the full list (sorted, ranked); pass a number to cap
    it (the Player Development tab's table defaults to showing the top 15
    client-side but loads the full list for its "show all" toggle).
    """
    conn = get_db()
    tid = team_id or my_team_id()
    ed = conn.execute("SELECT MAX(eval_date) FROM prospect_fv").fetchone()[0]

    rows = conn.execute(f"""
        SELECT p.name, p.age, p.level, pf.fv, pf.fv_str, pf.bucket, pf.prospect_surplus, p.player_id, p.pos,
               r.composite_score, r.ceiling_score, pf.risk,
               ds.available, ds.css_class, ds.label, ds.confidence, ds.z,
               ds.schedule_status, ds.schedule_label, ds.schedule_note,
               r.acc, r.int_, r.wrk_ethic, r.lead, r.loy, r.greed, r.adaptability, r.personality_type
        FROM prospect_fv pf
        JOIN players p ON pf.player_id=p.player_id
        LEFT JOIN latest_ratings r ON pf.player_id=r.player_id
        LEFT JOIN dev_speed ds ON pf.player_id=ds.player_id AND ds.eval_date=pf.eval_date
        WHERE pf.eval_date=? AND {ORG_ID_SQL}=?
              AND p.age <= 25 AND pf.fv >= 40
    """, (ed, tid)).fetchall()

    # Ranked by surplus (the app's value measure), FV only as the tiebreak —
    # FV-first ordering put older FV-50 depth ahead of younger, higher-surplus
    # FV-45 prospects.
    def sort_key(r):
        fv_val = r[3] + (0.1 if r[4].endswith("+") else 0)
        return (-(r[6] or 0), -fv_val)

    rows = sorted(rows, key=sort_key)
    if limit:
        rows = rows[:limit]
    out = []
    for i, r in enumerate(rows):
        pers = _personality_fields(r[21], r[22], r[23], r[24], r[25], r[26], r[27])
        conf = confidence_tier(r[11], r[20], pers["personality_type_class"] == "neg" or bool(pers["concerns"]))
        out.append({"rank": i + 1, "name": r[0], "age": r[1],
             "level": level_map().get(str(r[2]), str(r[2])),
             "fv": r[3], "fv_str": r[4],
             "bucket": _display_pos(r[5], r[8]),
             "pos_order": pos_order().get(_display_pos(r[5], r[8]), 99),
             "surplus": round(r[6] / _money_divisor(), 1) if r[6] else 0,
             "pid": r[7],
             "composite_score": r[9], "ceiling_score": r[10],
             "risk": r[11], "dev": _dev_cell(r, 12),
             "acc": r[20], "confidence": conf, "long_horizon": horizon_flag(r[2])})
    return out


def get_intl_complex(team_id=None):
    """International Complex roster — unlike AAA/AA/A/etc, OOTP gives this
    level no affiliate team of its own; its players just sit directly on
    the parent team_id with level='8' (parent_team_id=0), the same way a
    young MLB-level rookie still on the prospect scale does with level='1'.
    That's why it never showed up as a browsable affiliate before, and why
    it was silently excluded from Farm Surplus (see ORG_ID_SQL).
    """
    conn = get_db()
    tid = team_id or my_team_id()
    ed = conn.execute("SELECT MAX(eval_date) FROM prospect_fv").fetchone()[0]

    rows = conn.execute("""
        SELECT p.name, p.age, pf.fv, pf.fv_str, pf.bucket, pf.prospect_surplus, p.player_id, p.pos,
               r.composite_score, r.ceiling_score, r.acc, pf.risk,
               r.int_, r.wrk_ethic, r.lead, r.loy, r.greed, r.adaptability, r.personality_type
        FROM players p
        LEFT JOIN prospect_fv pf ON pf.player_id=p.player_id AND pf.eval_date=?
        LEFT JOIN latest_ratings r ON p.player_id=r.player_id
        WHERE p.team_id=? AND p.level='8' AND p.retired=0
    """, (ed, tid)).fetchall()

    out = []
    for (name, age, fv, fv_str, bucket, surplus, pid, pos, comp, ceil_score, acc, risk,
         intel, wrk_ethic, lead, loy, greed, adaptability, ptype) in rows:
        disp_bucket = _display_pos(bucket, pos) if bucket else pos_map().get(pos, "?")
        _pers = _personality_fields(intel, wrk_ethic, lead, loy, greed, adaptability, ptype)
        out.append({
            "pid": pid, "name": name, "age": age,
            "bucket": disp_bucket, "pos_order": pos_order().get(disp_bucket, 99),
            "fv": fv, "fv_str": fv_str or "-",
            "composite_score": comp, "ceiling_score": ceil_score,
            "surplus": round(surplus / _money_divisor(), 1) if surplus else 0,
            "acc": acc, "risk": risk,
            **_pers,
        })
    out.sort(key=lambda x: (-(x["fv"] if x["fv"] is not None else -1), x["age"] if x["age"] is not None else 99))
    return out


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

    # Real per-year figures from an uploaded "Team Salary" export override
    # the raw synced contract value wherever they cover a given calendar
    # year — same override get_payroll_summary() (Finances tab) already
    # applies. Without this here too, the Contracts tab's Salary column and
    # the top-bar Payroll tile silently drift out of sync with Finances
    # for any player whose uploaded figure differs from the raw sync.
    game_year = get_cfg().year
    uploaded_by_pid = {}
    try:
        for r in conn.execute("SELECT player_id, year, amount FROM salary_estimates"):
            uploaded_by_pid.setdefault(r["player_id"], {})[r["year"]] = r["amount"]
    except Exception:
        pass

    retained_by_pid = _get_retention_map(conn)

    out = []
    for r in rows:
        pid, name = r[0], r[1]
        years, cur_yr = r[2], r[3]
        salaries = [r[4 + i] for i in range(15)]
        pid_uploaded = uploaded_by_pid.get(pid)
        if pid_uploaded:
            for idx in range(cur_yr, min(years, 15)):
                abs_year = game_year + (idx - cur_yr)
                if abs_year in pid_uploaded:
                    salaries[idx] = pid_uploaded[abs_year]
        # Salary another team still pays (OOTP "Retained Salary") isn't ours.
        retained_pct = retained_by_pid.get(pid, 0.0)
        if retained_pct:
            salaries = [(s_ or 0) * (1 - retained_pct) for s_ in salaries]
        ntc, to, po = r[19], r[20], r[21]
        surplus, is_major = r[22], r[23]
        ps_fv, ps_age, ps_bucket = r[24], r[25], r[26]
        cur_sal = salaries[cur_yr] if cur_yr < len(salaries) else salaries[0]
        yrs_left = max(years - cur_yr, 1)
        total_left = sum(salaries[cur_yr:years]) if cur_yr < years else cur_sal
        out.append({
            "pid": pid, "name": name,
            "salary": cur_sal, "years_left": yrs_left, "total_left": total_left,
            "retained_pct": retained_pct,
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
    # Each cell also carries the game's own marker (see custom_upload.
    # import_team_salary): None = guaranteed/confirmed dollar figure, T/P/O =
    # a team/player/mutual option (the DOLLAR VALUE is still a known, fixed
    # contract term — only whether it gets exercised is uncertain), R = a
    # pre-arb renewal the team itself already set, and only A/A*/A# are the
    # game's own arbitration-model ESTIMATE for a not-yet-set future year.
    # Only that last group should ever be labeled "est" — everything else is
    # a real number just like a guaranteed contract year.
    _OPTION_MARKER_LABEL = {"T": "TO", "P": "PO", "O": "TO"}
    uploaded_by_pid = {}
    try:
        for r in conn.execute("SELECT player_id, year, amount, marker FROM salary_estimates"):
            marker = r["marker"]
            uploaded_by_pid.setdefault(r["player_id"], {})[r["year"]] = {
                "amount": r["amount"],
                "is_estimate": bool(marker) and marker.startswith("A"),
                "option": _OPTION_MARKER_LABEL.get(marker),
            }
    except Exception:
        pass

    horizon = 6
    future_years = [year + i for i in range(horizon)]
    min_sal = get_cfg().minimum_salary
    retained_by_pid = _get_retention_map(conn)
    players = []
    totals = [0] * horizon
    for r in rows:
        pid, name = r[0], r[1]
        keep = 1 - retained_by_pid.get(pid, 0.0)  # share of salary we actually pay
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
                cell = pid_uploaded[abs_year]
                sal = cell["amount"] * keep
                by_year.append({"sal": sal, "option": cell["option"], "projected": cell["is_estimate"]})
                totals[i] += sal
            elif i in proj_map:
                by_year.append({"sal": proj_map[i] * keep, "option": None, "projected": True})
                totals[i] += proj_map[i] * keep
            elif contract_yr < yrs_total:
                is_option = (contract_yr == yrs_total - 1) and (to or po)
                sal = sals[contract_yr] * keep
                by_year.append({"sal": sal, "option": "TO" if to and is_option else "PO" if po and is_option else None, "projected": False})
                totals[i] += sal
            else:
                by_year.append(None)
        if not any(s for s in by_year if s):
            continue
        players.append({"pid": pid, "name": name, "by_year": by_year, "ntc": ntc,
                        "retained_pct": retained_by_pid.get(pid, 0.0)})
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
    """Players whose contracts expire within two years — or None for a
    perpetual-arbitration league (PPL), where nobody actually reaches free
    agency: expiring contracts auto-renew through arbitration under league
    rules, so the list would be wrong."""
    if get_cfg().perpetual_arb:
        return None
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

    farm = conn.execute(f"""
        SELECT pf.player_id, p.name, pf.bucket, pf.prospect_surplus, 'Farm' as src
        FROM prospect_fv pf JOIN players p ON pf.player_id = p.player_id
        WHERE pf.eval_date = ? AND {ORG_ID_SQL} = ? AND p.level != '1'
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
    farm_ages = conn.execute(f"""
        SELECT p.age FROM prospect_fv pf
        JOIN players p ON pf.player_id = p.player_id
        WHERE pf.eval_date=? AND {ORG_ID_SQL}=? AND p.level!='1' AND pf.fv >= 40
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
        SELECT COALESCE(NULLIF(p.organization_id,0), NULLIF(p.parent_team_id,0), p.team_id), p.age FROM prospect_fv pf
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

    by_bucket = conn.execute(f"""
        SELECT pf.bucket, COUNT(*), COALESCE(SUM(pf.prospect_surplus), 0)
        FROM prospect_fv pf JOIN players p ON pf.player_id = p.player_id
        WHERE pf.eval_date=? AND {ORG_ID_SQL}=? AND pf.fv >= 40
              AND p.age <= 25
        GROUP BY pf.bucket
    """, (ed, team_id)).fetchall()

    by_level = conn.execute(f"""
        SELECT pf.level, COUNT(*)
        FROM prospect_fv pf JOIN players p ON pf.player_id = p.player_id
        WHERE pf.eval_date=? AND {ORG_ID_SQL}=? AND pf.fv >= 40
              AND p.age <= 25
        GROUP BY pf.level
    """, (ed, team_id)).fetchall()

    mlb_tids = mlb_team_ids()
    # Same FV >= 40 + age <= 25 "meaningfully contributes" bar get_farm() and
    # get_farm_system_rankings() use, so a team's rank agrees everywhere it's
    # shown (this panel's own Rank line, and the League page's full ranking).
    lg = conn.execute("""
        SELECT COALESCE(NULLIF(p.organization_id,0), NULLIF(p.parent_team_id,0), p.team_id), SUM(pf.prospect_surplus)
        FROM prospect_fv pf JOIN players p ON pf.player_id = p.player_id
        WHERE pf.eval_date=? AND p.age <= 25 AND pf.fv >= 40
        GROUP BY COALESCE(NULLIF(p.organization_id,0), NULLIF(p.parent_team_id,0), p.team_id)
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
        "lg_rank_tier": _gr_tier(lg_rank, len(lg_vals), "pill") if lg_vals else "gr-pill-mid",
    }


def _gr_tier(rank, n, suffix=""):
    """5-tier green-to-red bucket name for `rank` out of `n`, by percentile
    rather than a fixed rank cutoff so this reads sensibly at any group size
    (a 10-team league vs a 30-team one, a 9-position table, etc). `suffix`
    picks the CSS family: "" for gr-tier-* (row+cell tint), "pill" for
    gr-pill-* (standalone badge), "bar" for gr-bar-* (rank-bar background).
    Returns e.g. "gr-tier-elite" / "gr-pill-good" / "gr-bar-bad".
    """
    prefix = f"gr-{suffix}" if suffix else "gr-tier"
    if n <= 1:
        return f"{prefix}-mid"
    pct = rank / n
    if pct <= 0.15:
        return f"{prefix}-elite"
    if pct <= 0.40:
        return f"{prefix}-good"
    if pct <= 0.70:
        return f"{prefix}-mid"
    if pct <= 0.90:
        return f"{prefix}-poor"
    return f"{prefix}-bad"


def get_farm_system_rankings():
    """Every MLB org's farm system, ranked by total surplus of its
    meaningfully-contributing prospects (age <= 25, FV >= 40 — same bar
    get_farm()/get_farm_depth() use). Powers the League page's Farm System
    Rankings table, color-tiered green (best) to red (worst).
    """
    conn = get_db()
    ed = conn.execute("SELECT MAX(eval_date) FROM prospect_fv").fetchone()[0]
    mlb_tids = mlb_team_ids()

    rows = conn.execute("""
        SELECT COALESCE(NULLIF(p.parent_team_id,0), p.team_id) AS org_tid,
               COUNT(*), SUM(pf.prospect_surplus)
        FROM prospect_fv pf JOIN players p ON pf.player_id = p.player_id
        WHERE pf.eval_date=? AND p.age <= 25 AND pf.fv >= 40
        GROUP BY org_tid
    """, (ed,)).fetchall()

    names = team_names_map()
    abbrs = team_abbr_map()
    my_tid = my_team_id()

    entries = [{"tid": tid, "name": names.get(tid, str(tid)), "abbr": abbrs.get(tid, "?"),
                "count": count, "surplus": round((surplus or 0) / _money_divisor(), 1)}
               for tid, count, surplus in rows if tid in mlb_tids]
    # Orgs with zero qualifying prospects don't show up in the GROUP BY at
    # all — include them at the bottom rather than silently omitting them.
    seen = {e["tid"] for e in entries}
    for tid in mlb_tids:
        if tid not in seen:
            entries.append({"tid": tid, "name": names.get(tid, str(tid)),
                             "abbr": abbrs.get(tid, "?"), "count": 0, "surplus": 0})

    entries.sort(key=lambda e: -e["surplus"])
    n = len(entries)
    for i, e in enumerate(entries):
        e["rank"] = i + 1
        e["is_mine"] = e["tid"] == my_tid
        e["tier"] = _gr_tier(i + 1, n)
    return entries


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
        WHERE p.level = 1 AND f.year = ? AND f.position != 1
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
        WHERE p.level = 1 AND ps.year = ? AND ps.split_id = 1
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
    for r in conn.execute(f"""
        SELECT pf.bucket, SUM(pf.prospect_surplus)
        FROM prospect_fv pf JOIN players p ON pf.player_id = p.player_id
        WHERE pf.eval_date=? AND {ORG_ID_SQL}=? AND p.level != '1'
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


DEPTH_CHART_ROLES = ("starter", "platoon_vr", "platoon_vl", "bench")

# Pitcher-side manual roles, set on the pseudo-positions "SP" and "RP".
# "starter"/"spot_starter" apply to SP; the rest are bullpen usage tiers
# for RP. Unlike batting roles, these may carry an explicit `share` (see
# projections.allocate_pitcher_time / _manual_sp_entries for how it's used).
PITCHER_DEPTH_CHART_ROLES = (
    "starter", "spot_starter",
    "closer", "setup", "middle_relief", "long_relief",
)


def get_depth_chart_roles(team_id):
    """Manual depth-chart role overrides for a team: {position: {player_id: role}}.

    Batting positions only — see get_pitcher_depth_chart_roles for SP/RP.
    """
    conn = get_db()
    rows = conn.execute(
        "SELECT position, player_id, role FROM depth_chart_roles "
        "WHERE team_id=? AND position NOT IN ('SP', 'RP')",
        (team_id,)
    ).fetchall()
    out = {}
    for r in rows:
        out.setdefault(r["position"], {})[r["player_id"]] = r["role"]
    return out


def get_pitcher_slots(team_id, position):
    """Hard display order pinned by the user for 'SP' or 'RP': {player_id: slot} (1 = first)."""
    conn = get_db()
    rows = conn.execute(
        "SELECT player_id, slot FROM depth_chart_roles "
        "WHERE team_id=? AND position=? AND slot IS NOT NULL", (team_id, position)
    ).fetchall()
    return {r["player_id"]: r["slot"] for r in rows}


def get_batting_role_shares(team_id):
    """Explicit playing-time shares pinned on batting bench roles: {position: {player_id: share}}."""
    conn = get_db()
    rows = conn.execute(
        "SELECT position, player_id, share FROM depth_chart_roles "
        "WHERE team_id=? AND position NOT IN ('SP', 'RP') AND share IS NOT NULL AND role='bench'",
        (team_id,)
    ).fetchall()
    out = {}
    for r in rows:
        out.setdefault(r["position"], {})[r["player_id"]] = r["share"]
    return out


def get_pitcher_depth_chart_roles(team_id):
    """Manual pitcher role overrides: {'SP': {pid: (role, share)}, 'RP': {pid: (role, share)}}.

    `share` is the explicit playing-time fraction (0-1) if the user pinned
    one, else None — see _manual_sp_entries in projections.py for how a
    missing share is filled in automatically.
    """
    conn = get_db()
    rows = conn.execute(
        "SELECT position, player_id, role, share FROM depth_chart_roles "
        "WHERE team_id=? AND position IN ('SP', 'RP')",
        (team_id,)
    ).fetchall()
    out = {"SP": {}, "RP": {}}
    for r in rows:
        out[r["position"]][r["player_id"]] = (r["role"], r["share"])
    return out


def set_depth_chart_role(team_id, position, player_id, role, share=None, slot=None):
    """Set (or clear, if role is falsy/'auto') a manual depth-chart role.

    share: optional explicit playing-time fraction (0-1), only meaningful
    for pitcher positions ('SP'/'RP') and for batting 'bench' roles (e.g.
    7/154 for "seven games") — ignored (stored as NULL) otherwise.
    """
    import datetime
    conn = get_db()
    if not role or role == "auto":
        conn.execute(
            'DELETE FROM depth_chart_roles WHERE team_id=? AND position=? AND player_id=?',
            (team_id, position, player_id)
        )
    else:
        valid_roles = PITCHER_DEPTH_CHART_ROLES if position in ("SP", "RP") else DEPTH_CHART_ROLES
        if role not in valid_roles:
            raise ValueError(f"Unknown depth chart role for position {position!r}: {role!r}")
        if position not in ("SP", "RP") and role != "bench":
            share = None
        elif share is not None:
            share = float(share)
            if not (0.0 < share <= 1.0):
                raise ValueError(f"share must be in (0, 1], got {share!r}")
        if slot is not None:
            slot = int(slot)
            if position not in ("SP", "RP") or slot < 1:
                raise ValueError("slot is a 1-based order and only applies to positions 'SP' and 'RP'")
        conn.execute('''
            INSERT INTO depth_chart_roles (team_id, position, player_id, role, share, slot, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(team_id, position, player_id)
            DO UPDATE SET role=excluded.role, share=excluded.share, slot=excluded.slot,
                          updated_at=excluded.updated_at
        ''', (team_id, position, player_id, role, share, slot, datetime.datetime.now().isoformat()))
    conn.commit()


def list_retained_salaries():
    """Every player with salary retained by another team, for the API/UI."""
    conn = get_db()
    rows = conn.execute(
        """SELECT r.player_id, p.name, p.team_id, r.retained_by_team_id, r.pct, r.note, r.updated_at
           FROM retained_salary r LEFT JOIN players p ON p.player_id = r.player_id
           ORDER BY r.updated_at DESC"""
    ).fetchall()
    return [dict(r) for r in rows]


def set_retained_salary(player_id, pct, retained_by_team_id=None, note=None):
    """Set (or clear, if pct is falsy) the share of a player's salary another
    team keeps paying, then refresh his stored surplus so every page that
    reads player_surplus reflects it without waiting for the next full recalc.
    """
    from statsplusplus.data.retained_salary import set_retention
    from contract_value import contract_value as _cv
    conn = get_db()
    set_retention(conn, player_id, pct, retained_by_team_id, note)

    cv = _cv(player_id, league_dir=get_cfg().league_dir)
    if cv:
        surplus = cv["total_surplus"].get("base", 0)
        bd = cv.get("breakdown")
        surplus_yr1 = round(bd[0].get("surplus", 0)) if bd else 0
        conn.execute(
            "UPDATE player_surplus SET surplus=?, surplus_yr1=? WHERE player_id=? AND eval_date=?",
            (surplus, surplus_yr1, player_id, _get_eval_date()),
        )
        conn.commit()


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
        project_era, project_fip, project_ratings, set_peak_ages,
        assign_diamond_positions, allocate_playing_time, allocate_pitcher_time,
        roster_availability, LEVEL_DISCOUNT, DEFAULT_TEAM_PA, DEFAULT_TEAM_IP,
    )
    from statsplusplus.evaluation.constants import (
        load_model_weights, PEAK_AGE_HITTER as _PA_H_DEFAULT, PEAK_AGE_PITCHER as _PA_P_DEFAULT,
    )
    _peak_weights = load_model_weights(get_cfg().league_dir)
    set_peak_ages(
        _peak_weights.get_param("PEAK_AGE_HITTER", _PA_H_DEFAULT),
        _peak_weights.get_param("PEAK_AGE_PITCHER", _PA_P_DEFAULT),
    )
    from statsplusplus.evaluation.war import stat_peak_war, load_stat_history
    from contract_value import contract_value as _cv, _load_perp_arb_model
    from statsplusplus.evaluation.arb import estimate_control as _ec_raw
    _lmin2 = league_minimum()
    _perp2 = get_cfg().perpetual_arb
    _perp_model2 = _load_perp_arb_model() if _perp2 else None
    _has_dh = get_cfg().has_dh
    def _estimate_control(conn, pid, age, sal, bucket=None):
        return _ec_raw(conn, pid, age, sal, min_sal=_lmin2, perpetual_arb=_perp2, bucket=bucket)
    from prospect_value import prospect_surplus as _pv

    state = _get_state()
    year = state.get("stats_year", state["year"])
    conn = get_db()

    # Cumulative real-stats career WAR — only needed for perpetual-arb
    # leagues (PPL), where future arb salary is projected from career
    # production rather than a fixed 3-step formula. See roster_availability.
    _career_war_by_pid = {}
    if _perp2:
        for r in conn.execute(
            "SELECT player_id, COALESCE(SUM(war), 0) AS w FROM mlb_batting_stats "
            "WHERE split_id=1 GROUP BY player_id"
        ).fetchall():
            _career_war_by_pid[r["player_id"]] = _career_war_by_pid.get(r["player_id"], 0.0) + (r["w"] or 0.0)
        for r in conn.execute(
            "SELECT player_id, COALESCE(SUM((war + COALESCE(ra9war, war)) / 2.0), 0) AS w "
            "FROM mlb_pitching_stats WHERE split_id=1 GROUP BY player_id"
        ).fetchall():
            _career_war_by_pid[r["player_id"]] = _career_war_by_pid.get(r["player_id"], 0.0) + (r["w"] or 0.0)

    manual_roles = get_depth_chart_roles(team_id)
    manual_shares = get_batting_role_shares(team_id)
    sp_slots = get_pitcher_slots(team_id, "SP")
    rp_slots = get_pitcher_slots(team_id, "RP")

    lg = _load_la()
    lg_era = lg["pitching"]["era"]
    lg_fip = lg["pitching"]["fip"]

    from statsplusplus.config.league_config import games_per_season as _gps
    bat_hist, pit_hist, two_way = load_stat_history(
        conn, state["game_date"], games_per_season=_gps(get_cfg().league_dir)
    )

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
        WHERE p.team_id = ? AND p.level = 1
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
            fg, bg, has_dh=_has_dh)
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
            "career_war": _career_war_by_pid.get(pid, 0.0),
        })

    # ── Pre-compute WAR curves from surplus model ───────────────────────
    war_curves = {}  # {player_id: {year: war}}
    hist = (bat_hist, pit_hist)
    for p in all_players:
        cv = _cv(p["player_id"], _conn=conn, _hist=hist, league_dir=get_cfg().league_dir)
        if cv and cv.get("breakdown"):
            war_curves[p["player_id"]] = {
                b["year"]: round(b["war_base"], 2) for b in cv["breakdown"]
            }

    # ── Query org prospects ─────────────────────────────────────────────
    prospect_rows = conn.execute(f'''
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
        WHERE {ORG_ID_SQL} = ?
          AND pf.level != 'MLB'
          AND (pf.fv >= 50 OR (pf.fv >= 40 AND pf.level IN ('AAA', 'AA')))
          AND pf.eval_date = (SELECT MAX(pf2.eval_date) FROM prospect_fv pf2
                              WHERE pf2.player_id = pf.player_id)
        GROUP BY pf.player_id
    ''', (team_id,)).fetchall()

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
    avail = roster_availability(all_players, (0, 1, 2), perpetual_arb=_perp2,
                                 perp_model=_perp_model2, league_dir=get_cfg().league_dir)

    LEVEL_ORDER = ["Intl", "Rookie", "A", "A-Short", "AA", "AAA", "MLB"]

    def _promote(level, offset):
        idx = LEVEL_ORDER.index(level) if level in LEVEL_ORDER else 0
        return LEVEL_ORDER[min(idx + offset, len(LEVEL_ORDER) - 1)]

    # ── Per-year assembly ───────────────────────────────────────────────
    by_year = {}
    prev_names = set()
    year1_players_by_pos = {}

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
                positions = assign_diamond_positions(p, p.get("fielding"), p.get("bat_games", 0),
                                                       has_dh=_has_dh)
            else:
                positions = assign_diamond_positions(p, use_pot=use_pot, has_dh=_has_dh)
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
        # Manual role overrides only apply to the current year (off == 0) —
        # future years keep using the automatic WAR-ranked allocation, since
        # roles may change as players age/depart.
        pos_result = allocate_playing_time(
            players_by_pos, manual_roles=manual_roles if off == 0 else None,
            manual_shares=manual_shares if off == 0 else None)
        if off == 0:
            year1_players_by_pos = players_by_pos

        # Backfill DH: when the primary DH rests, a field player DHs.
        # Prefer bat-first players (high OPS+) at non-premium positions.
        # Elite defenders at CF/SS/C should almost never DH.
        # No-DH leagues (PPL) skip this entirely — there is no DH slot to
        # backfill, and doing so would double-count a fielder's WAR (once
        # at their real position, again in a fabricated DH share).
        dh_players = pos_result.get("DH", [])
        dh_used = sum(p["pt_pct"] for p in dh_players)
        dh_gap = 100.0 - dh_used
        if _has_dh and dh_gap > 1.0:
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
        # Manual pitcher role overrides only apply to the current year
        # (off == 0), same reasoning as the batting manual_roles above.
        manual_pitcher_roles = get_pitcher_depth_chart_roles(team_id) if off == 0 else None
        manual_rp_pids = set(manual_pitcher_roles["RP"]) if manual_pitcher_roles else set()

        # SP prospects who can't crack the rotation move to the bullpen.
        # Sort SP by effective WAR, keep top 5 MLB-caliber starters,
        # overflow SP prospects become RP candidates. An SP-bucket pitcher
        # the user has manually assigned an RP role (e.g. a starter moved
        # to long relief) is pulled out first and always goes to the
        # bullpen, regardless of level or WAR rank — a manual RP
        # designation always wins over the automatic SP/RP split.
        def _reproject_as_rp(p):
            rp_war = project_war(p["ovr"], p["pot"], p["age"], "RP", off)
            rp_era = project_era(p["ovr"], p["pot"], p["age"], "RP", off, lg_era)
            rp_fip = project_fip(p["ovr"], p["pot"], p["age"], "RP", off, lg_fip)
            return dict(p, war_proj=rp_war, _era=rp_era, _fip=rp_fip, bucket="RP", role=12)

        sp_candidates = []
        for p in sp_pool:
            if p["player_id"] in manual_rp_pids:
                rp_pool.append(_reproject_as_rp(p))
            else:
                sp_candidates.append(p)

        sp_candidates.sort(key=lambda x: x["war_proj"] * x.get("level_discount", 1.0),
                            reverse=True)
        rotation_size = 5
        sp_keep, sp_overflow = sp_candidates[:rotation_size], sp_candidates[rotation_size:]
        for p in sp_overflow:
            if p.get("level", "MLB") != "MLB":
                # Prospect — re-project as RP
                rp_pool.append(_reproject_as_rp(p))
            else:
                # MLB SP who didn't make top 5 stays as 6th starter / swingman
                sp_keep.append(p)
        sp_result, rp_result = allocate_pitcher_time(
            sp_keep, rp_pool,
            manual_sp_roles=manual_pitcher_roles["SP"] if manual_pitcher_roles else None,
            manual_rp_roles=manual_pitcher_roles["RP"] if manual_pitcher_roles else None,
        )

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
        _field_positions = ["C", "1B", "2B", "3B", "SS", "LF", "CF", "RF"]
        if _has_dh:
            _field_positions.append("DH")
        for pos in _field_positions:
            players = [_fmt_hitter(p) for p in pos_result.get(pos, [])
                       if p["pa"] > 0 and round(p.get("pt_pct", 0)) >= 2]
            positions[pos] = players
            pos_war_map[pos] = round(sum(p["war"] for p in players), 1)

        sp_fmt = [_fmt_pitcher(p) for p in sp_result if round(p.get("pt_pct", 0)) >= 2]
        if off == 0 and sp_slots:
            # Hard rotation order (SP1..SP5): pinned pitchers first, in slot
            # order; everyone else keeps their automatic order after them.
            sp_fmt.sort(key=lambda p: sp_slots.get(p["pid"], 10_000))
        rp_fmt = [_fmt_pitcher(p) for p in rp_result if round(p.get("pt_pct", 0)) >= 2]
        if off == 0 and rp_slots:
            rp_fmt.sort(key=lambda p: rp_slots.get(p["pid"], 10_000))
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

    # ── Role candidates (current year only) for the Depth Chart Roles tab ──
    role_candidates = {}
    for pos, players in year1_players_by_pos.items():
        cands = []
        for p in players:
            pid = p["player_id"]
            cands.append({
                "pid": pid, "name": p["name"],
                "level": p.get("_level", "MLB"), "age": p["age"],
                "war": round(p.get("war_proj", 0), 1),
                "ops_vs_l": round(p["ops_vs_l"]) if p.get("ops_vs_l") else None,
                "ops_vs_r": round(p["ops_vs_r"]) if p.get("ops_vs_r") else None,
                "ovr_ops_plus": round(p["ovr_ops_plus"]) if p.get("ovr_ops_plus") else None,
                "role": manual_roles.get(pos, {}).get(pid, "auto"),
            })
        cands.sort(key=lambda x: x["war"], reverse=True)
        role_candidates[pos] = cands

    return {"years": [year, year + 1, year + 2], "by_year": by_year,
            "pos_rank": pos_rank, "num_teams": num_teams,
            "role_candidates": role_candidates}


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
          AND (p.team_id = ? OR p.parent_team_id = ? OR p.organization_id = ?)
        ORDER BY f.player_id, f.g DESC
    """, (ed_s, year, year, team_id, year, team_id, team_id, team_id)).fetchall()
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
              AND (p.team_id = ? OR p.parent_team_id = ? OR p.organization_id = ?)
            ORDER BY b.war DESC
        """, (ed_s, team_id, year, team_id, team_id, team_id)).fetchall()
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
          AND (p.team_id = ? OR p.parent_team_id = ? OR p.organization_id = ?)
        ORDER BY pt.war DESC
    """, (ed_s, team_id, year, team_id, team_id, team_id)).fetchall()
    for r in pit_rows:
        bucket = "SP" if r["role"] == 11 else "RP"
        mlb_by_pos[bucket].append(_entry(r))

    for pos in mlb_by_pos:
        mlb_by_pos[pos].sort(key=lambda x: -x["ovr"])

    # Follow the user's manual depth chart (Depth Chart tab) where one is set:
    # hitters — manually-designated players lead each position (starter, then
    # vR/vL platoon, then bench); pitchers — a manual SP/RP list is the whole
    # list, in the pinned slot order (falling back to role tier, then share).
    _hit_rank = {"starter": 0, "platoon_vr": 1, "platoon_vl": 2, "bench": 3}
    for pos, roles in get_depth_chart_roles(team_id).items():
        lst = mlb_by_pos.get(pos)
        if not lst:
            continue
        lead = sorted((e for e in lst if e["pid"] in roles),
                      key=lambda e: _hit_rank.get(roles[e["pid"]], 9))
        mlb_by_pos[pos] = lead + [e for e in lst if e["pid"] not in roles]

    _manual_p = get_pitcher_depth_chart_roles(team_id)
    _pit_all = {e["pid"]: e for lst in (mlb_by_pos.get("SP", []), mlb_by_pos.get("RP", [])) for e in lst}
    _tier = {"starter": 0, "spot_starter": 1, "closer": 2, "setup": 3, "middle_relief": 4, "long_relief": 5}
    for pos in ("SP", "RP"):
        roles = _manual_p.get(pos) or {}
        if not roles:
            continue
        slots = get_pitcher_slots(team_id, pos)
        picked = [_pit_all[pid] for pid in roles if pid in _pit_all]
        picked.sort(key=lambda e: (slots.get(e["pid"], 10_000),
                                   _tier.get(roles[e["pid"]][0], 9),
                                   -(roles[e["pid"]][1] or 0)))
        mlb_by_pos[pos] = picked

    # Top prospects per bucket (collect all, sorted by FV then surplus)
    prospect_by_pos = defaultdict(list)
    # age <= 25 matches this app's standard "prospect" cutoff everywhere else
    # (Top Prospects, Farm Surplus's prospect counts, etc.) — prospect_fv
    # itself deliberately has NO age cap (org-depth players over 25 with no
    # MLB track record still get valued there), so this query needs its own
    # filter or a 26+ org-depth arm/bat would show up in a "Top Prospect"
    # slot, which isn't what that label means.
    prosp_rows = conn.execute(f"""
        SELECT pf.player_id, p.name, pf.bucket, pf.fv, pf.fv_str, pf.level,
               p.age, p.pos, pf.prospect_surplus, pf.risk
        FROM prospect_fv pf
        JOIN players p ON pf.player_id = p.player_id
        WHERE pf.eval_date = ? AND {ORG_ID_SQL} = ? AND p.level != '1'
          AND p.age <= 25
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
    # A manual SP/RP list shows in full (e.g. a 4-man rotation, a 6-man pen)
    mlb_slots = {pos: max(pos_slots[pos], len(mlb_by_pos.get(pos, []))) if _manual_p.get(pos) else pos_slots[pos]
                 for pos in pos_slots}
    for pos in ("SP", "RP"):
        if _manual_p.get(pos):
            mlb_slots[pos] = len(mlb_by_pos.get(pos, []))
    pos_order_list = ["C", "1B", "2B", "3B", "SS", "LF", "CF", "RF", "SP", "RP"]
    position_depth = []
    used_prospect_pids = set()  # deduplicate prospects across positions

    for pos in pos_order_list:
        n_mlb = mlb_slots.get(pos, 1)
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
        # Position players: the backup is the next man on the (manual-first)
        # MLB list — the other side of a platoon, else the top bench option.
        # SP/RP rows are already one pitcher each, so no backup column there.
        _all_mlb = mlb_by_pos.get(pos, [])
        backup = _all_mlb[1] if pos not in ("SP", "RP") and len(_all_mlb) > 1 else None
        for i in range(n_rows):
            mlb = [mlb_list[i]] if i < len(mlb_list) else []
            prosp = prosp_deduped[i] if i < len(prosp_deduped) else None
            position_depth.append({
                "pos": pos if i == 0 else "",
                "mlb": mlb, "prospect": prosp,
                "backup": backup if i == 0 else None,
                "is_first": i == 0,
                "parent_pos": pos,
            })

    # ── League-wide position rankings ──
    lg_rankings = _league_pos_rankings(conn, year)
    num_teams = max(len(v) for v in lg_rankings.values()) if lg_rankings else 34
    pos_rank = {}
    pos_rank_tier = {}
    for pos, tw in lg_rankings.items():
        for i, (tid, _war) in enumerate(tw):
            if tid == team_id:
                pos_rank[pos] = i + 1
                pos_rank_tier[pos] = _gr_tier(i + 1, len(tw), "pill")
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
    farm_surp = conn.execute(f"""
        SELECT pf.player_id, p.name, pf.bucket, pf.prospect_surplus, p.role, pf.level
        FROM prospect_fv pf JOIN players p ON pf.player_id = p.player_id
        WHERE pf.eval_date = ? AND {ORG_ID_SQL} = ? AND p.level != '1'
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
        "pos_rank_tier": pos_rank_tier,
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
    _hitter_weights_by_bucket = load_tool_weights(get_cfg().league_dir).get("hitter", {})

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
            _bc_bucket = bucket or ("COF" if display_p in ("LF", "RF") else display_p)
            _bw = _hitter_weights_by_bucket.get(_bc_bucket, _hitter_weights_by_bucket.get("COF", {}))
            base.update({
                "con": n(cntct), "pot_con": n(pot_cntct),
                "gap": n(gap), "pot_gap": n(pot_gap),
                "pow": n(pw), "pot_pow": n(pot_pw),
                "eye": n(eye), "pot_eye": n(pot_eye),
                "spd": n(speed), "def": n(pos_def) if pos_def else None,
                # Simple pure Contact/Gap/Power/Eye weighted average — no
                # defense/speed/transforms/recombination.
                "bat_ovr": compute_batting_composite(n(cntct), n(gap), n(pw), n(eye), _bw),
                "bat_pot": compute_batting_composite(n(pot_cntct), n(pot_gap), n(pot_pw), n(pot_eye), _bw),
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


def _org_vr_vl_composites(conn, team_id):
    """{pid: {"vr":, "vl":, "bat_vr":, "bat_vl":}} for EVERY player in the
    whole org (MLB + every affiliate, hitters and pitchers alike) — same
    underlying computation as _defense_hit_composites(), just not filtered
    down to position players only, since All Minor Leaguers needs both.

    bat_vr/bat_vl are the simple Contact/Gap/Power/Eye-only weighted average
    (see compute_batting_composite) — built from the same vr_tools/vl_tools
    split dicts _build_entries() already assembles for the full composite,
    so no extra querying is needed. Hitters only; None for pitchers.
    """
    from scouting_queries import _fetch_rows, _org_where, _build_entries
    from statsplusplus.evaluation.composite import compute_batting_composite
    ratings_scale = get_cfg().ratings_scale
    all_weights = load_tool_weights(get_cfg().league_dir)
    hitter_weights = all_weights.get("hitter", {})
    ed = conn.execute("SELECT MAX(eval_date) FROM prospect_fv").fetchone()[0]
    ed_surplus = conn.execute("SELECT MAX(eval_date) FROM player_surplus").fetchone()[0]
    where, params = _org_where(team_id, "org")
    rows = _fetch_rows(conn, where, params, ed, ed_surplus)
    entries = _build_entries(rows, ratings_scale, None, hitter_weights,
                              all_weights.get("pitcher", {}), set(), is_mine=True)
    out = {}
    for e in entries:
        d = {"vr": e["vr_score"], "vl": e["vl_score"], "bat_vr": None, "bat_vl": None}
        vrt, vlt = e.get("vr_tools"), e.get("vl_tools")
        if not e["is_pitcher"] and vrt and vlt:
            w = hitter_weights.get(e["group"], hitter_weights.get("COF", {}))
            d["bat_vr"] = compute_batting_composite(vrt.get("contact"), vrt.get("gap"), vrt.get("power"), vrt.get("eye"), w)
            d["bat_vl"] = compute_batting_composite(vlt.get("contact"), vlt.get("gap"), vlt.get("power"), vlt.get("eye"), w)
        out[e["pid"]] = d
    return out


def get_org_minor_league_roster(parent_team_id):
    """Full minor league roster for an entire org (all levels), split into
    hitters and pitchers — plus the org's own MLB roster, tagged is_pro,
    for the "show pro players" comparison toggle (still returned even
    though the page defaults to hiding them, filtering client-side)."""
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

    # Minor league affiliates plus the org's own MLB roster (p.team_id =
    # parent_team_id there) — the MLB rows are tagged is_pro below so the
    # template can filter them back out by default.
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
               ps.surplus, pf.fv_continuous, r.acc,
               r.int_, r.wrk_ethic, r.lead, r.loy, r.greed,
               r.adaptability, r.personality_type
        FROM players p
        LEFT JOIN latest_ratings r ON p.player_id = r.player_id
        LEFT JOIN prospect_fv pf ON p.player_id = pf.player_id
        LEFT JOIN player_surplus ps ON p.player_id = ps.player_id
        WHERE p.team_id IN ({placeholders}) OR (p.team_id = ? AND p.level = '1')
        ORDER BY p.level, COALESCE(r.composite_score, r.ovr, 0) DESC
    """, aff_ids + [parent_team_id]).fetchall()

    vr_vl = _org_vr_vl_composites(conn, parent_team_id)

    # 40-man roster lookup (contract with is_major=1 under this parent org)
    forty_man_pids = set()
    for r in conn.execute(
        f"SELECT c.player_id FROM contracts c JOIN players p ON c.player_id=p.player_id "
        f"WHERE {ORG_ID_SQL}=? AND c.is_major=1", (parent_team_id,)
    ).fetchall():
        forty_man_pids.add(r[0])

    from statsplusplus.config.ratings import norm_continuous as _normc
    from statsplusplus.evaluation.park_fit import (
        load_park_factors, compute_batter_park_fit, compute_batter_park_value_pct,
        compute_pitcher_park_fit_from_stats, compute_pitcher_park_fit_from_tools,
        compute_pitcher_park_value_pct_from_stats, compute_pitcher_park_value_pct_from_tools,
    )
    _rscale_c = get_cfg().ratings_scale
    park = load_park_factors(get_cfg().league_dir)
    hitter_weights_by_bucket = load_tool_weights(get_cfg().league_dir).get("hitter", {})

    # Real observed GB%/K%/BB% (all levels — this is exclusively a minor
    # league roster) for every org pitcher with a real track record,
    # preferred over the scouting-tool proxy — same convention as the free
    # agent/waiver pools.
    _PARK_FIT_BF_THRESHOLD = 150
    org_pids = [r[0] for r in rows]
    # Rule 5 eligibility (from the game's R5 column, via the Rule 5 import) for
    # the "Rule 5 eligible" filter.
    try:
        _r5_pids = {r[0] for r in conn.execute("SELECT player_id FROM rule5_eligible")}
    except Exception:
        _r5_pids = set()
    # The affiliate each player is actually assigned to right now (for the
    # Team logo column) — the row tuple above carries no team id.
    _cur_team = {}
    for _i in range(0, len(org_pids), 500):
        _chunk = org_pids[_i:_i + 500]
        for _r in conn.execute(
            f"SELECT player_id, team_id FROM players WHERE player_id IN ({','.join('?' * len(_chunk))})",
            _chunk,
        ).fetchall():
            _cur_team[_r[0]] = _r[1]
    pitcher_stats = {}
    lg_gb_pct = lg_k_pct = lg_bb_pct = None
    if park and org_pids:
        pid_qs = ",".join("?" * len(org_pids))
        for row in conn.execute(
            f"SELECT player_id, SUM(gb), SUM(fb), SUM(k), SUM(bb), SUM(bf) "
            f"FROM pitching_stats WHERE player_id IN ({pid_qs}) GROUP BY player_id",
            org_pids,
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

    # Real observed Zone Rating for hitters, same "prefer real stats over
    # the scouting-tool Def proxy" convention as the pitcher block above —
    # one row per player at whichever position they've played the most
    # games, most recent stats year with data (state["stats_year"]).
    _ZR_MIN_GAMES = 10
    zr_by_pid = {}
    if org_pids:
        stats_year = _get_state().get("stats_year")
        pid_qs = ",".join("?" * len(org_pids))
        best_g = {}
        for p_pid, g, zr in conn.execute(
            f"SELECT player_id, g, zr FROM fielding_stats "
            f"WHERE player_id IN ({pid_qs}) AND year=? AND zr IS NOT NULL",
            org_pids + [stats_year],
        ).fetchall():
            if g and g >= _ZR_MIN_GAMES and g > best_g.get(p_pid, 0):
                best_g[p_pid] = g
                zr_by_pid[p_pid] = round(zr, 1)

    # This function hardcoded scale="1-100" (norm()'s default) regardless of
    # the league's actual ratings_scale — silently wrong for any "20-80"
    # league (PPL): a raw value that's already a 20-80 grade (e.g. 80) was
    # being re-normalized as if it were a 1-100 raw score, understating it
    # (norm(80, "1-100") -> 70). Every grade on this page was affected, not
    # just the new Viable Positions column below.
    _rscale = get_cfg().ratings_scale
    n = lambda v: _norm_rating(v, _rscale)
    _pm = pos_map()
    lmap = level_map()
    _role_pos = {11: "SP", 12: "SP", 13: "RP"}
    _pos_order = {"C": 1, "1B": 2, "2B": 3, "3B": 4, "SS": 5, "LF": 6, "CF": 7, "RF": 8, "OF": 9, "DH": 10}
    _role_order = {"SP": 1, "RP": 2}
    _level_order = {"2": 1, "3": 2, "4": 3, "5": 4, "6": 5, "8": 6, "0": 7}
    _VIABLE_POS_THRESHOLD = 65
    _VIABLE_POS_ORDER = ["C", "1B", "2B", "3B", "SS", "LF", "CF", "RF"]

    # "On pace for a historic season" (2026-09-30) — bulk-computed once for
    # the whole org roster, not per-row, so the page load stays fast.
    _war_paces = _get_all_war_paces_cached(conn)

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
        acc = r[56]
        intel, wrk_ethic, lead, loy, greed = r[57:62]
        adaptability, ptype = r[62:64]

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

        _pers = _personality_fields(intel, wrk_ethic, lead, loy, greed, adaptability, ptype)
        _vrvl = vr_vl.get(pid, {})
        base = {
            "pid": pid, "name": name, "age": age,
            "pos": display_p, "bt": bt,
            "level": level_name, "level_num": int(level) if level else 99,
            "is_pro": str(level) == "1",
            "team_id": _cur_team.get(pid),
            "rule5": pid in _r5_pids,
            "composite": composite, "ceiling": ceiling,
            "vr": _vrvl.get("vr"), "vl": _vrvl.get("vl"),
            "fv": fv, "fv_str": fv_str, "risk": risk,
            "surplus": round((prospect_surplus if prospect_surplus is not None else mlb_surplus) / _money_divisor(), 1)
                       if (prospect_surplus is not None or mlb_surplus is not None) else None,
            "peak_surplus": _peak_surplus(fv_continuous, age, level_name, bucket, ovr=composite, pot=potential),
            "on_40man": bool(on_40man),
            "war_pace": _war_paces.get(pid, {}).get("pace_war"),
            "is_historic_pace": _war_paces.get(pid, {}).get("is_historic", False),
            "pace_confidence": _war_paces.get(pid, {}).get("pace_confidence"),
            "acc": acc, **_pers,
            "confidence": confidence_tier(risk, acc, _pers["personality_type_class"] == "neg" or bool(_pers["concerns"])),
            "long_horizon": horizon_flag(level),
        }

        # Park fit/value against your own home park — always scored on
        # POTENTIAL tools, never current, since every player on this page
        # is a minor leaguer by definition (this reflects who they'll grow
        # into, not their barely-developed current tools).
        park_fit = None
        park_value = None
        if park:
            if is_pitcher:
                _park_tools = {"stuff": _normc(pot_stf, _rscale_c), "movement": _normc(pot_mov, _rscale_c),
                               "control": _normc(pot_ctrl, _rscale_c)}
                obs = pitcher_stats.get(pid)
                if obs and obs["gb_pct"] is not None and lg_gb_pct is not None:
                    park_fit = compute_pitcher_park_fit_from_stats(
                        obs["gb_pct"], obs["k_pct"], obs["bb_pct"],
                        lg_gb_pct, lg_k_pct, lg_bb_pct, park)
                    _value_pct = compute_pitcher_park_value_pct_from_stats(
                        obs["gb_pct"], obs["k_pct"], obs["bb_pct"],
                        lg_gb_pct, lg_k_pct, lg_bb_pct, park)
                else:
                    park_fit = compute_pitcher_park_fit_from_tools(_park_tools, park)
                    _value_pct = compute_pitcher_park_value_pct_from_tools(_park_tools, park)
            else:
                _park_tools = {"contact": _normc(pot_cntct, _rscale_c), "gap": _normc(pot_gap, _rscale_c),
                               "power": _normc(pot_pw, _rscale_c)}
                _hw = hitter_weights_by_bucket.get(display_p, hitter_weights_by_bucket.get("COF", {}))
                park_fit = compute_batter_park_fit(_park_tools, bats, _hw, park)
                _value_pct = compute_batter_park_value_pct(_park_tools, bats, _hw, park)
            _park_val_basis = prospect_surplus if prospect_surplus is not None else mlb_surplus
            if _value_pct is not None and _park_val_basis is not None:
                park_value = round((_park_val_basis * _value_pct) / _money_divisor(), 1)
        base["park_fit"] = park_fit
        base["park_value"] = park_value

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
            viable_positions = [
                vp for vp in _VIABLE_POS_ORDER
                if (n(_pos_def_map.get(vp)) or 0) >= _VIABLE_POS_THRESHOLD
            ]
            _bc_bucket = bucket or ("COF" if display_p in ("LF", "RF") else display_p)
            _bw = hitter_weights_by_bucket.get(_bc_bucket, hitter_weights_by_bucket.get("COF", {}))
            base.update({
                "con": n(cntct), "pot_con": n(pot_cntct),
                "gap": n(gap), "pot_gap": n(pot_gap),
                "pow": n(pw), "pot_pow": n(pot_pw),
                "eye": n(eye), "pot_eye": n(pot_eye),
                "spd": n(speed), "def": n(pos_def) if pos_def else None,
                "zr": zr_by_pid.get(pid),
                # Simple pure Contact/Gap/Power/Eye weighted average — no
                # defense/speed/transforms/recombination (separate from the
                # vr/vl full-composite scores in vr_vl above).
                "bat_ovr": compute_batting_composite(n(cntct), n(gap), n(pw), n(eye), _bw) if _bw else None,
                "bat_pot": compute_batting_composite(n(pot_cntct), n(pot_gap), n(pot_pw), n(pot_eye), _bw) if _bw else None,
                "bat_vr": _vrvl.get("bat_vr"), "bat_vl": _vrvl.get("bat_vl"),
                "viable_positions": viable_positions,
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


# Position label -> (current-rating column, potential-rating column) in
# `ratings`. Shared by every section of the Defense page.
_DEF_POS_COLS = [
    ("C", "c", "pot_c"), ("1B", "first_b", "pot_first_b"), ("2B", "second_b", "pot_second_b"),
    ("3B", "third_b", "pot_third_b"), ("SS", "ss", "pot_ss"),
    ("LF", "lf", "pot_lf"), ("CF", "cf", "pot_cf"), ("RF", "rf", "pot_rf"),
]
_DEF_FIELD_POS_CODES = {2: "C", 3: "1B", 4: "2B", 5: "3B", 6: "SS", 7: "LF", 8: "CF", 9: "RF"}


def _def_level_disp(level, lmap):
    """level_map() has no "0" entry (free agent / released, not an actual
    minor-league level) — without this fallback a released player shows
    the literal string "0" instead of "FA"."""
    disp = lmap.get(str(level))
    if disp:
        return disp
    return "FA" if str(level) == "0" else str(level)


def _defense_hit_composites(conn, team_id):
    """{pid: {"composite":, "vr":, "vl":, "buffs":, "concerns":}} for
    every position player in the whole org (MLB + every affiliate) — reuses
    the exact same vR/vL composite computation as Best Available/Lineup
    Optimizer (scouting_queries._build_entries()) rather than a second,
    possibly-diverging implementation. Imported locally: scouting_queries
    already imports several names from this module at load time, so a
    module-level import here would be circular.
    """
    from scouting_queries import _fetch_rows, _org_where, _build_entries
    ratings_scale = get_cfg().ratings_scale
    all_weights = load_tool_weights(get_cfg().league_dir)
    ed = conn.execute("SELECT MAX(eval_date) FROM prospect_fv").fetchone()[0]
    ed_surplus = conn.execute("SELECT MAX(eval_date) FROM player_surplus").fetchone()[0]
    where, params = _org_where(team_id, "org")
    rows = _fetch_rows(conn, where, params, ed, ed_surplus)
    entries = _build_entries(rows, ratings_scale, None, all_weights.get("hitter", {}),
                              all_weights.get("pitcher", {}), set(), is_mine=True)
    return {
        e["pid"]: {"composite": e["composite_score"], "vr": e["vr_score"], "vl": e["vl_score"],
                   "buffs": e["buffs"], "concerns": e["concerns"]}
        for e in entries if not e["is_pitcher"]
    }


def _defense_ratings_rows(conn, team_id, composites):
    """Every MLB position player's ratings at all 8 positions (raw AND
    20-80-normalized together, since the Ratings tab displays raw numbers
    but still colors them on the 20-80 tier scale for visual consistency
    with the rest of the app), plus their catcher/infield/outfield
    specialty sub-ratings. One row per player.
    """
    from statsplusplus.config.ratings import norm as _norm_rating
    rscale = get_cfg().ratings_scale
    n = lambda v: _norm_rating(v, rscale)

    rows = conn.execute("""
        SELECT p.player_id, p.name, p.age, p.pos, r.bats, r.throws,
               r.c, r.first_b, r.second_b, r.third_b, r.ss, r.lf, r.cf, r.rf,
               r.c_arm, r.c_blk, r.c_frm, r.ifr, r.ife, r.ifa, r.tdp, r.ofr, r.ofe, r.ofa,
               r.int_, r.wrk_ethic, r.lead, r.loy, r.greed, r.adaptability, r.personality_type
        FROM players p
        LEFT JOIN latest_ratings r ON p.player_id = r.player_id
        WHERE p.team_id=? AND p.level='1' AND COALESCE(p.role,0) NOT IN (11,12,13)
    """, (team_id,)).fetchall()

    _pm = pos_map()
    _fields = ["c", "first_b", "second_b", "third_b", "ss", "lf", "cf", "rf",
               "c_arm", "c_blk", "c_frm", "ifr", "ife", "ifa", "tdp", "ofr", "ofe", "ofa"]
    out = []
    for r in rows:
        (pid, name, age, pos, bats, throws, *vals, intel, wrk_ethic, lead, loy, greed,
         adaptability, ptype) = r
        _dp = _pm.get(pos, "?")
        _pers = _personality_fields(intel, wrk_ethic, lead, loy, greed, adaptability, ptype)
        comp = composites.get(pid, {})
        row = {
            "pid": pid, "name": name, "age": age, "pos": _dp, "pos_sort": pos_order().get(_dp, 99),
            "bt": f"{bats}/{throws}" if bats and throws else (bats or ""),
            "composite": comp.get("composite"), "vr": comp.get("vr"), "vl": comp.get("vl"),
            **_pers,
        }
        for field, v in zip(_fields, vals):
            row[field] = v
            row[field + "_g"] = n(v)  # normalized companion, for data-g coloring only
        out.append(row)
    out.sort(key=lambda p: (pos_order().get(p["pos"], 99), p["name"]))
    return out


def _defense_potential_rows(conn, team_id, scope, composites):
    """cur/pot pairs at every position with any real potential rating at
    all (a raw value of 0 means "never evaluated there," which norm()
    already maps to None — anything else, however low, counts), for every
    position player in `scope` ("mlb", "milb", or "both") — the raw
    material for the Fielding Potential chart's per-position cur/
    checkmark cells.
    """
    from statsplusplus.config.ratings import norm as _norm_rating
    rscale = get_cfg().ratings_scale
    n = lambda v: _norm_rating(v, rscale)

    where = "p.team_id=? AND p.level='1'"
    params = [team_id]
    if scope == "milb":
        where = "p.parent_team_id=? AND p.level != '1'"
    elif scope == "both":
        where = "(p.team_id=? AND p.level='1') OR p.parent_team_id=?"
        params = [team_id, team_id]

    rows = conn.execute(f"""
        SELECT p.player_id, p.name, p.age, p.level, p.pos,
               r.c, r.pot_c, r.first_b, r.pot_first_b, r.second_b, r.pot_second_b,
               r.third_b, r.pot_third_b, r.ss, r.pot_ss,
               r.lf, r.pot_lf, r.cf, r.pot_cf, r.rf, r.pot_rf,
               r.int_, r.wrk_ethic, r.lead, r.loy, r.greed, r.adaptability, r.personality_type
        FROM players p
        LEFT JOIN latest_ratings r ON p.player_id = r.player_id
        WHERE {where} AND COALESCE(p.role,0) NOT IN (11,12,13)
    """, params).fetchall()

    lmap = level_map()
    _pm = pos_map()
    out = []
    for r in rows:
        pid, name, age, level, pos = r[0:5]
        pairs = r[5:21]
        intel, wrk_ethic, lead, loy, greed, adaptability, ptype = r[21:28]
        positions = {}
        any_learnable = False
        for i, (lbl, _cur_col, _pot_col) in enumerate(_DEF_POS_COLS):
            cur_raw, pot_raw = pairs[i * 2], pairs[i * 2 + 1]
            cur, pot = n(cur_raw), n(pot_raw)
            if pot is None:
                positions[lbl] = None
                continue
            any_learnable = True
            positions[lbl] = {"cur": cur, "pot": pot, "reached": cur is not None and cur >= pot}
        if not any_learnable:
            continue
        _dp = _pm.get(pos, "?")
        _pers = _personality_fields(intel, wrk_ethic, lead, loy, greed, adaptability, ptype)
        comp = composites.get(pid, {})
        out.append({
            "pid": pid, "name": name, "age": age,
            "level": _def_level_disp(level, lmap), "pos": _dp, "pos_sort": pos_order().get(_dp, 99),
            "composite": comp.get("composite"), "vr": comp.get("vr"), "vl": comp.get("vl"),
            **_pers,
            "positions": positions,
        })
    out.sort(key=lambda p: (pos_order().get(p["pos"], 99), p["name"]))
    return out


def _defense_observed_rows(conn, team_id, year, scope, composites):
    """Real fielding stats (ZR, Fielding %, Errors, Range Factor, and
    catcher/infield extras) for `scope` ("mlb", "milb", or "both") — one
    row per player per position actually played that season.
    """
    # The pro-club rows only cover players who are on the MLB roster right now
    # (p.team_id / p.level = current assignment): someone traded away (Yariv)
    # or optioned to the minors (Alvarez) still has this season's stats under
    # the club's team_id, but no longer belongs in the club's observed defense.
    # Optioned players appear under the MiLB scope instead.
    on_pro_roster = "f.team_id=? AND p.team_id=? AND p.level='1'"
    if scope == "milb":
        where = "p.parent_team_id=? AND f.league_id IS NOT NULL"
        params = [team_id]
    elif scope == "both":
        where = f"({on_pro_roster}) OR (p.parent_team_id=? AND f.league_id IS NOT NULL)"
        params = [team_id, team_id, team_id]
    else:
        where = f"{on_pro_roster} AND f.league_id IS NULL"
        params = [team_id, team_id]

    rows = conn.execute(f"""
        SELECT p.player_id, p.name, p.level, f.position, f.g, f.gs, f.ip,
               f.tc, f.a, f.po, f.e, f.dp, f.pb, f.sba, f.rto, f.zr,
               r.int_, r.wrk_ethic, r.lead, r.loy, r.greed, r.adaptability, r.personality_type
        FROM fielding_stats f
        JOIN players p ON p.player_id = f.player_id
        LEFT JOIN latest_ratings r ON r.player_id = p.player_id
        WHERE f.year=? AND {where} AND f.g > 0
        ORDER BY f.position, f.g DESC
    """, (year, *params)).fetchall()

    lmap = level_map()
    out = []
    for r in rows:
        (pid, name, level, fpos, gp, gs, ip, tc, a, po, e, dp, pb, sba, rto, zr,
         intel, wrk_ethic, lead, loy, greed, adaptability, ptype) = r
        lbl = _DEF_FIELD_POS_CODES.get(fpos)
        if not lbl:
            continue
        fpct = round((po + a) / tc, 3) if tc else None
        range_factor = round((po + a) / gp, 2) if gp else None
        cs_pct = round(100 * rto / sba, 1) if sba else None
        _pers = _personality_fields(intel, wrk_ethic, lead, loy, greed, adaptability, ptype)
        comp = composites.get(pid, {})
        out.append({
            "pid": pid, "name": name, "level": _def_level_disp(level, lmap),
            "pos": lbl, "pos_sort": pos_order().get(lbl, 99),
            "composite": comp.get("composite"), "vr": comp.get("vr"), "vl": comp.get("vl"),
            **_pers,
            "g": gp, "gs": gs, "ip": ip, "e": e or 0, "dp": dp or 0,
            "zr": round(zr, 1) if zr is not None else None,
            "fpct": fpct, "range_factor": range_factor,
            "pb": pb or 0 if lbl == "C" else None,
            "cs_pct": cs_pct if lbl == "C" else None,
        })
    return out


def get_defense_page(team_id=None):
    """Everything defensive in one place: current ratings at every
    position (raw, color-coded to the same 20-80 tiers as everywhere
    else), a Fielding Potential chart (who could still learn a new
    position), and real observed defensive stats — see the three
    _defense_*_rows() helpers above for each section's exact scope.
    """
    conn = get_db()
    tid = team_id or my_team_id()
    state = _get_state()
    year = state.get("stats_year", state["year"])
    composites = _defense_hit_composites(conn, tid)

    return {
        "ratings": _defense_ratings_rows(conn, tid, composites),
        "potential_mlb": _defense_potential_rows(conn, tid, "mlb", composites),
        "potential_milb": _defense_potential_rows(conn, tid, "milb", composites),
        "potential_both": _defense_potential_rows(conn, tid, "both", composites),
        "observed_mlb": _defense_observed_rows(conn, tid, year, "mlb", composites),
        "observed_milb": _defense_observed_rows(conn, tid, year, "milb", composites),
        "observed_both": _defense_observed_rows(conn, tid, year, "both", composites),
        "stats_year": year,
    }


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
