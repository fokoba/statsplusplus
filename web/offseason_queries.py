"""
web/offseason_queries.py — data for the phase-aware Offseason page (Phase A).

This is a *view* over existing data and evaluation functions, organized around
the decisions a GM makes during the offseason. It intentionally reuses the
established query/eval machinery (arb model, upcoming-FA, payroll projection,
surplus) rather than introducing new models.

Panels (Phase A / proof-of-concept):
  - Arbitration    : arb-eligible own players, projected salary, tender rec
  - Free agency    : your expiring players (retain priority) + market board
  - Payroll outlook : next-year committed + projected vs current
  - Extensions     : high-surplus players 1-2 years from free agency

Phase B (Rule 5 / 40-man) is gated on storing the /players Rule-5 fields and is
not built here.
"""

from web_league_context import get_db, get_cfg, my_team_id
from statsplusplus.utils.positions import display_pos as _display_pos
from statsplusplus.data.db import ORG_ID_SQL

# This fork keeps the contract_value.py/prospect_value.py table architecture
# (player_surplus for MLB roster, prospect_fv for everyone else, including
# free agents) rather than upstream's unified player_evaluation table — see
# the merge-commit note for why. Every query below sources composite/ceiling/
# bucket from ratings/player_surplus/prospect_fv directly instead.


def _eval_date(conn):
    row = conn.execute("SELECT MAX(eval_date) FROM player_surplus").fetchone()
    return row[0] if row and row[0] else None


# Chronological offseason phases (keys) — must match the OFFSEASON_PHASES list
# in api_routes. Kept here too so the panel-gating logic is a pure, testable
# function independent of the request layer.
PHASE_KEYS = ["playoffs", "arbitration", "options", "free_agency", "rule5", "spring"]


def panels_for_phase(phase):
    """Which panels to surface for a given sub-phase. Empty phase = show all.

    Trades is always shown (handled by the template), so it's not in this dict.
    """
    show_all = not phase
    return {
        "arbitration": show_all or phase == "arbitration",
        "options": show_all or phase == "options",
        "free_agency": show_all or phase == "free_agency",
        "extensions": show_all or phase in ("free_agency", "options"),
        "rule5": show_all or phase == "rule5",
    }


# ---------------------------------------------------------------------------
# Arbitration
# ---------------------------------------------------------------------------

def _load_perp_model(cfg):
    """Load the league's calibrated perpetual-arb salary model, or None.

    Without this, arb_salary_perpetual falls back to million-dollar-scaled
    defaults that floor to the minimum in low-salary-scale leagues.
    """
    import json
    mw_path = cfg.league_dir / "config" / "model_weights.json"
    if mw_path.exists():
        try:
            return json.load(open(mw_path)).get("ARB_SALARY_MODEL")
        except Exception:
            return None
    return None


def _recent_war(conn, pid, bucket):
    """Most recent MLB season's WAR (blended for pitchers) — a simple, direct
    proxy for "current production" without pulling in the full peak-WAR
    weighting machinery (which needs the `games` table populated). Same
    single-season blend _last_season() uses for display.
    """
    if bucket in ("SP", "RP"):
        r = conn.execute(
            "SELECT war, ra9war FROM mlb_pitching_stats "
            "WHERE player_id=? AND split_id=1 AND ip > 0 ORDER BY year DESC LIMIT 1",
            (pid,)).fetchone()
        if not r:
            return 0.0
        return round(((r[0] or 0) + (r[1] if r[1] is not None else r[0] or 0)) / 2.0, 1)
    r = conn.execute(
        "SELECT war FROM mlb_batting_stats WHERE player_id=? AND split_id=1 AND ab > 0 "
        "ORDER BY year DESC LIMIT 1", (pid,)).fetchone()
    return round(r[0] or 0, 1) if r else 0.0


def _career_war(conn, pid):
    """Cumulative career MLB WAR (batting + blended pitching) for the salary model."""
    bat = conn.execute(
        "SELECT COALESCE(SUM(war), 0) FROM mlb_batting_stats WHERE player_id=? AND split_id=1",
        (pid,)).fetchone()[0] or 0
    pit = conn.execute(
        "SELECT COALESCE(SUM((war + COALESCE(ra9war, war)) / 2.0), 0) "
        "FROM mlb_pitching_stats WHERE player_id=? AND split_id=1",
        (pid,)).fetchone()[0] or 0
    return bat + pit


def get_arbitration(team_id):
    """Arb-eligible players on this team, with projected salary and a
    tender / non-tender recommendation.

    Reuses arb.service_time (eligibility) + arb.arb_salary(_perpetual) using the
    league's calibrated model. All dollar thresholds scale with the league's
    $/WAR so this works at any salary scale (MLB millions or a retro league's
    thousands).
    """
    conn = get_db()
    ed = _eval_date(conn)
    if ed is None:
        return []
    cfg = get_cfg()
    min_sal = cfg.minimum_salary
    perp = cfg.perpetual_arb

    from statsplusplus.evaluation.arb import (
        service_time, arb_salary, arb_salary_perpetual,
    )
    from statsplusplus.config.league_config import dollars_per_war
    dpw = dollars_per_war(cfg.league_dir)
    perp_model = _load_perp_model(cfg) if perp else None

    # Tender tiers scaled to the league: a clear tender has surplus worth roughly
    # a win or more; anything positive is at least a marginal tender.
    strong_tender = 1.0 * dpw  # ~1 WAR of surplus

    rows = conn.execute("""
        SELECT c.player_id, p.name, p.age, c.salary_0, c.years, c.current_year,
               ps.surplus, ps.ovr, ps.bucket
        FROM contracts c
        JOIN players p ON p.player_id = c.player_id
        JOIN player_surplus ps ON ps.player_id = c.player_id AND ps.eval_date = ?
        WHERE c.contract_team_id = ? AND c.is_major = 1
          AND p.level IN ('1', 1)
          AND (c.years - c.current_year) <= 1
    """, (ed, team_id)).fetchall()

    out = []
    for r in rows:
        pid, name, age, sal = r[0], r[1], r[2], r[3] or 0
        bucket = r[8] or "?"
        # Only above-minimum, controllable, not yet FA — i.e. arb-eligible.
        st = service_time(conn, pid)
        if st.is_free_agent_eligible:
            continue
        if sal <= min_sal:
            continue  # pre-arb (min salary) — not an arb tender decision

        # Projected next-year salary
        try:
            if perp:
                war = _recent_war(conn, pid, bucket)
                proj_sal = arb_salary_perpetual(
                    age, war, dpw, min_sal,
                    career_war=_career_war(conn, pid), model=perp_model)
            else:
                proj_sal = arb_salary(r[7] or 50, bucket, 1, sal, min_sal)
        except Exception:
            proj_sal = sal

        surplus = r[6] or 0
        # Tender recommendation from surplus (value net of cost over control),
        # scaled to the league's $/WAR.
        if surplus >= strong_tender:
            rec, rec_class = "Tender", "good"
        elif surplus >= 0:
            rec, rec_class = "Tender (marginal)", "ok"
        else:
            rec, rec_class = "Consider non-tender", "bad"

        out.append({
            "pid": pid, "name": name, "age": age,
            "pos": _display_pos(bucket) if bucket != "?" else "?",
            "cur_salary": sal,
            "proj_salary": proj_sal,
            "raise": proj_sal - sal,
            "surplus": surplus,  # raw dollars — template uses the money filter
            "service": st.display(),
            "rec": rec, "rec_class": rec_class,
        })
    out.sort(key=lambda x: -x["proj_salary"])
    return out


# ---------------------------------------------------------------------------
# Free agency — the actionable open-market pool (team_id = 0 = unsigned FA),
# driven by actual roster state, not inferred from lingering contract rows.
# Correct at any point in the offseason: an unsigned FA is available whether
# filing just happened or not.
# ---------------------------------------------------------------------------

# Composite floor for the market board — filter out replacement-level/roster
# filler so the board is a usable shortlist rather than the whole FA pool.
_MARKET_MIN_COMPOSITE = 48


def _team_need_positions(team_id):
    """Positions where this team is below league-average org depth (MLB+farm
    surplus), reusing the draft board's get_draft_org_depth signal. Returns a
    set of display-position keys (e.g. {"SS", "SP"}). Used to flag FAs that fill
    a need.
    """
    try:
        from team_queries import get_draft_org_depth
        depth = get_draft_org_depth(team_id)
    except Exception:
        return set()
    needs = set()
    for pos, d in (depth or {}).items():
        # get_draft_org_depth returns `ratio` = position depth vs league average.
        # < 1.0 means below-average depth at that position → a need. (Its own
        # scale treats < 0.6 as a clear gap, 0.6-1.2 as thin.)
        if (d.get("ratio") or 0) < 1.0:
            needs.add(pos)
    return needs


def _need_key(pos_disp):
    """Map a display position to the get_draft_org_depth key (collapses corner OF)."""
    if pos_disp in ("LF", "RF", "COF"):
        return "LF/RF"
    return pos_disp


# Game listed-position codes → display position (players.pos).
_LISTED_POS = {1: "P", 2: "C", 3: "1B", 4: "2B", 5: "3B", 6: "SS",
               7: "LF", 8: "CF", 9: "RF", 10: "DH"}


def _incumbent_by_pos(conn, team_id, ed):
    """Best MLB-roster composite per LISTED position for this team — the bar an
    FA must clear to be an upgrade. Keyed off the game position (players.pos),
    not the eval bucket, so a listed 1B is compared against the team's 1B even
    when his defensive bucket is 2B/COF. Pitchers keyed as SP/RP by role.
    """
    inc = {}
    for r in conn.execute("""
        SELECT p.pos, p.role, MAX(ps.ovr)
        FROM player_surplus ps JOIN players p ON p.player_id = ps.player_id
        WHERE ps.eval_date = ? AND ps.team_id = ? AND ps.level = 'MLB'
        GROUP BY p.pos, p.role
    """, (ed, team_id)).fetchall():
        pos, role, comp = r[0], r[1], r[2]
        key = "SP" if role in (11, 12) else ("RP" if role == 13 else _LISTED_POS.get(pos))
        if key is None:
            continue
        inc[key] = max(inc.get(key, 0), comp or 0)
    return inc


def _fa_pos(pos, role, bucket):
    """Display position for a free agent, preferring the game listed position
    (with SP/RP from role), falling back to the eval bucket."""
    if role in (11, 12):
        return "SP"
    if role == 13:
        return "RP"
    listed = _LISTED_POS.get(pos)
    if listed and listed != "P":
        return listed
    return _display_pos(bucket) if bucket else "?"


def _last_season(conn, pid, is_pitcher):
    """Prior-season line as a dict, or None. Includes sample size (PA/IP), the
    rate line, and ACTUAL WAR for that season (this is the number that matches
    the player page's season stats — distinct from the projected peak_war).
    """
    if is_pitcher:
        r = conn.execute("""
            SELECT year, w, l, era, k, outs, war, ra9war FROM mlb_pitching_stats
            WHERE player_id=? AND split_id=1 AND ip > 0 ORDER BY year DESC LIMIT 1
        """, (pid,)).fetchone()
        if not r:
            return None
        ip = (r[5] or 0) / 3.0
        # Blended WAR to match how the app reports pitcher WAR elsewhere
        war = ((r[6] or 0) + (r[7] if r[7] is not None else r[6] or 0)) / 2.0
        return {"line": f"{r[1]}-{r[2]}, {r[3]:.2f} ERA, {r[4]} K",
                "sample": f"{int(ip)}.{int(round((ip - int(ip)) * 3))} IP",
                "war": round(war, 1), "year": r[0]}
    r = conn.execute("""
        SELECT year, ab, h, hr, rbi, pa, war FROM mlb_batting_stats
        WHERE player_id=? AND split_id=1 AND ab > 0 ORDER BY year DESC LIMIT 1
    """, (pid,)).fetchone()
    if not r:
        return None
    avg = (r[2] / r[1]) if r[1] else 0
    return {"line": f".{int(round(avg * 1000)):03d}, {r[3]} HR, {r[4]} RBI",
            "sample": f"{r[5] or 0} PA",
            "war": round(r[6] or 0, 1), "year": r[0]}


def _proj_next_war(conn, pid, age, bucket, composite, ceiling, stat_war,
                   years_control, dpw, min_sal, weights):
    """Next-season projected WAR — calls the shared compute_player_value and
    reads the first control-year of its breakdown, same single source of
    truth the player valuation page and trade calculator use. Do NOT re-derive
    (stat_war × aging_mult misses the development/confidence discount that
    dominates for unproven players).
    """
    from statsplusplus.evaluation.player_value import compute_player_value
    cpa = conn.execute(
        "SELECT COALESCE(SUM(ab + COALESCE(bb,0) + COALESCE(hbp,0) + COALESCE(sf,0)), 0) "
        "FROM mlb_batting_stats WHERE player_id=? AND split_id=1", (pid,)).fetchone()[0]
    cip = conn.execute(
        "SELECT COALESCE(SUM(ip), 0) FROM mlb_pitching_stats WHERE player_id=? AND split_id=1",
        (pid,)).fetchone()[0]
    try:
        res = compute_player_value(
            fv_continuous=0.0, bucket=bucket, age=age, level="MLB",
            composite=composite or 50, ceiling=ceiling or 50,
            career_pa=int(cpa), career_ip=float(cip), stat_war=stat_war,
            years_control=years_control or 1, salaries=None,
            dpw=dpw, min_sal=min_sal, weights=weights)
        bd = res.get("breakdown")
        if bd:
            return round(bd[0]["war"], 1)
    except Exception:
        pass
    return None


def get_market_board(team_id, limit=60):
    """The actual open-market free agent pool.

    Includes only genuinely signable players: currently unsigned
    (team_id = 0, free_agent = 1), above a composite floor, AND with prior stats
    in THIS league. Players from foreign leagues (e.g. NPB) appear in the API's
    global player dump with free_agent=1 but have never played here and can't be
    signed — the "has played in this league" check excludes them.

    "Fills a need" requires BOTH: the team is below league-average org depth at
    the player's position AND he is an actual upgrade over the team's current
    best there (or the team has no one). Position and incumbent are keyed off the
    game listed position, not the eval bucket, so a listed 1B is compared to the
    team's 1B.

    Composite/ceiling/handedness come straight from latest_ratings (populated
    for every player regardless of team/level); bucket from prospect_fv, which
    fv_calc.py writes for free agents too (level_label="FA") — this fork's
    equivalent of upstream's player_evaluation row for an unsigned player.
    """
    conn = get_db()
    ed = _eval_date(conn)
    cfg = get_cfg()
    need_positions = _team_need_positions(team_id)
    incumbent = _incumbent_by_pos(conn, team_id, ed) if ed else {}
    from statsplusplus.evaluation.constants import load_model_weights
    from statsplusplus.config.league_config import dollars_per_war, league_minimum
    weights = load_model_weights(cfg.league_dir)
    dpw = dollars_per_war(cfg.league_dir)
    min_sal = league_minimum(cfg.league_dir)
    rows = conn.execute("""
        SELECT p.player_id, p.name, p.age, lr.composite_score,
               COALESCE(lr.true_ceiling, lr.ceiling_score), pf.bucket,
               lr.bats, lr.throws, p.pos, p.role
        FROM players p
        JOIN latest_ratings lr ON lr.player_id = p.player_id
        LEFT JOIN prospect_fv pf ON pf.player_id = p.player_id
          AND pf.eval_date = (SELECT MAX(eval_date) FROM prospect_fv WHERE player_id = p.player_id)
        WHERE p.free_agent = 1 AND p.team_id = 0
          AND lr.composite_score >= ?
          AND (EXISTS (SELECT 1 FROM mlb_batting_stats b WHERE b.player_id = p.player_id)
            OR EXISTS (SELECT 1 FROM mlb_pitching_stats pt WHERE pt.player_id = p.player_id))
        ORDER BY lr.composite_score DESC
        LIMIT ?
    """, (_MARKET_MIN_COMPOSITE, limit)).fetchall()
    _hand = {1: "R", 2: "L", 3: "S", "R": "R", "L": "L", "S": "S"}
    out = []
    for r in rows:
        bucket = r[5] or "?"
        pos = _fa_pos(r[8], r[9], bucket)
        comp = r[3] or 0
        is_pitcher = pos in ("SP", "RP")
        inc = incumbent.get(pos)
        is_upgrade = inc is None or comp > inc
        fills_need = (_need_key(pos) in need_positions) and is_upgrade
        last = _last_season(conn, r[0], is_pitcher)
        # Next-season projection — the SAME value the player valuation page shows
        # (shared compute_player_value; single source of truth). years_control=1
        # since we're only projecting the first signable season for a free agent.
        proj_war = _proj_next_war(conn, r[0], r[2], bucket, comp, r[4],
                                  last["war"] if last else 0.0,
                                  1, dpw, min_sal, weights)
        # Recommended contract — a simple value-based cost estimate the FA cart
        # draws down against fa_budget (aav). Not the player's real demand.
        from statsplusplus.config.finance_settings import recommended_contract
        rec = recommended_contract(proj_war, dpw, r[2], min_sal)
        out.append({
            "pid": r[0], "name": r[1], "age": r[2],
            "pos": pos,
            "composite": comp,
            "ceiling": r[4] or 0,
            "proj_war": proj_war,
            "bats": _hand.get(r[6], "?"),
            "throws": _hand.get(r[7], "?"),
            "last": last,
            "fills_need": fills_need,
            "rec_aav": rec["aav"],
            "rec_years": rec["years"],
            "rec_total": rec["total"],
        })
    return out


# ---------------------------------------------------------------------------
# Extension candidates
# ---------------------------------------------------------------------------

def get_extension_candidates(team_id):
    """High-surplus own players within ~2 years of free agency — locking in
    now can beat arb escalation / an open-market bid.
    """
    conn = get_db()
    ed = _eval_date(conn)
    if ed is None:
        return []
    from statsplusplus.config.league_config import dollars_per_war
    cfg = get_cfg()
    threshold = 0.75 * dollars_per_war(cfg.league_dir)  # ~0.75 WAR of surplus, league-scaled
    rows = conn.execute("""
        SELECT c.player_id, p.name, p.age, c.years, c.current_year,
               ps.surplus, ps.ovr, ps.bucket
        FROM contracts c
        JOIN players p ON p.player_id = c.player_id
        JOIN player_surplus ps ON ps.player_id = c.player_id AND ps.eval_date = ?
        WHERE c.contract_team_id = ? AND c.is_major = 1 AND p.level IN ('1', 1)
          AND (c.years - c.current_year) BETWEEN 1 AND 2
    """, (ed, team_id)).fetchall()

    out = []
    for r in rows:
        surplus = r[5] or 0
        if surplus < threshold:  # only meaningful-value players (league-scaled)
            continue
        out.append({
            "pid": r[0], "name": r[1], "age": r[2],
            "pos": _display_pos(r[7]) if r[7] else "?",
            "yrs_left": max(1, r[3] - r[4]),
            "surplus": surplus,  # raw dollars — template uses the money filter
            "composite": r[6] or 0,
        })
    out.sort(key=lambda x: -x["surplus"])
    return out


# ---------------------------------------------------------------------------
# Contract options — this-offseason team/player/vesting option decisions
# ---------------------------------------------------------------------------

def _proj_value_at_year(conn, pid, age, bucket, composite, ceiling, stat_war,
                        target_control_year, dpw, min_sal, weights):
    """Projected market VALUE (dollars) for a player at a given control year
    ahead, via the shared compute_player_value (single source of truth). Reads
    the matching row of its breakdown. Returns None on failure.
    """
    from statsplusplus.evaluation.player_value import compute_player_value
    cpa = conn.execute(
        "SELECT COALESCE(SUM(ab + COALESCE(bb,0) + COALESCE(hbp,0) + COALESCE(sf,0)), 0) "
        "FROM mlb_batting_stats WHERE player_id=? AND split_id=1", (pid,)).fetchone()[0]
    cip = conn.execute(
        "SELECT COALESCE(SUM(ip), 0) FROM mlb_pitching_stats WHERE player_id=? AND split_id=1",
        (pid,)).fetchone()[0]
    try:
        res = compute_player_value(
            fv_continuous=0.0, bucket=bucket, age=age, level="MLB",
            composite=composite or 50, ceiling=ceiling or 50,
            career_pa=int(cpa), career_ip=float(cip), stat_war=stat_war,
            years_control=max(target_control_year, 1), salaries=None,
            dpw=dpw, min_sal=min_sal, weights=weights)
        bd = res.get("breakdown") or []
        if not bd:
            return None
        idx = min(max(target_control_year - 1, 0), len(bd) - 1)
        return bd[idx].get("market_value")
    except Exception:
        return None


def get_option_decisions(team_id):
    """Own-team players with a contract option, grouped by decision timing.

    An option for year Y is decided in the offseason *before* Y. We can't
    reliably read from the API whether this offseason's window has already
    passed, so we relabel honestly rather than guess:

    - **this_offseason** : option year == game_year + 1 (the decision that
      belongs to the current offseason — may already be resolved in-game).
    - **upcoming**        : option year >= game_year + 2 (future offseasons).

    - **Team options**: recommend Exercise vs Decline via the shared valuation
      model — exercising costs the option salary, declining costs the buyout, so
      the breakeven is ``proj_value > option_salary - buyout``. Shown for
      upcoming options too, framed as advance planning.
    - **Player / vesting options**: informational (the team doesn't decide).

    The option year is derived from the contract (``season_year + offset``), NOT
    the game year — a last-year option resolves at ``season_year + years - 1``,
    a next-last-year option one year earlier.

    All dollar figures are raw league dollars (template uses the money filter).
    """
    conn = get_db()
    ed = _eval_date(conn)
    if ed is None:
        return {"this_offseason": [], "upcoming": []}
    cfg = get_cfg()
    from statsplusplus.evaluation.constants import load_model_weights
    from statsplusplus.config.league_config import dollars_per_war, league_minimum
    weights = load_model_weights(cfg.league_dir)
    dpw = dollars_per_war(cfg.league_dir)
    min_sal = league_minimum(cfg.league_dir)

    rows = conn.execute("""
        SELECT c.player_id, p.name, p.age, c.years, c.current_year, c.season_year,
               c.salary_0, c.salary_1, c.salary_2, c.salary_3, c.salary_4,
               c.salary_5, c.salary_6, c.salary_7, c.salary_8, c.salary_9,
               c.salary_10, c.salary_11, c.salary_12, c.salary_13, c.salary_14,
               c.last_year_team_option, c.last_year_player_option,
               c.last_year_vesting_option, c.last_year_option_buyout,
               c.next_last_year_team_option, c.next_last_year_player_option,
               c.next_last_year_vesting_option, c.next_last_year_option_buyout,
               ps.ovr, ps.ovr, ps.bucket
        FROM contracts c
        JOIN players p ON p.player_id = c.player_id
        JOIN player_surplus ps ON ps.player_id = c.player_id AND ps.eval_date = ?
        WHERE c.contract_team_id = ? AND c.is_major = 1 AND p.level IN ('1', 1)
          AND (c.last_year_team_option = 1 OR c.last_year_player_option = 1
               OR c.last_year_vesting_option = 1
               OR c.next_last_year_team_option = 1 OR c.next_last_year_player_option = 1
               OR c.next_last_year_vesting_option = 1)
    """, (ed, team_id)).fetchall()

    game_year = int(cfg.year)
    entries = []
    for r in rows:
        pid, name, age = r[0], r[1], r[2]
        years, season_year = r[3] or 0, r[5] or game_year
        salaries = list(r[6:21])
        composite, ceiling, bucket = r[29], r[30], r[31] or "?"
        pos = _display_pos(bucket) if bucket != "?" else "?"
        stat_war = _recent_war(conn, pid, bucket)

        def _sal(idx):
            return (salaries[idx] or 0) if 0 <= idx < len(salaries) else 0

        # (salary index within the contract, option year, T/P/V flags, buyout)
        slots = [
            (years - 1, (r[21], r[22], r[23]), r[24] or 0),   # last-year option
            (years - 2, (r[25], r[26], r[27]), r[28] or 0),   # next-last-year option
        ]
        for opt_idx, (team_o, player_o, vesting_o), buyout in slots:
            if opt_idx < 0 or not (team_o or player_o or vesting_o):
                continue
            option_year = season_year + opt_idx
            opt_sal = _sal(opt_idx)
            if team_o:
                typ = "Team"
            elif player_o:
                typ = "Player"
            else:
                typ = "Vesting"
            entry = {
                "pid": pid, "name": name, "age": age, "pos": pos,
                "type": typ, "year": option_year,
                "option_salary": opt_sal, "buyout": buyout,
                "proj_value": None, "rec": None, "rec_class": "ok",
            }
            if typ == "Team":
                # control years from now (game_year) to the option year, 1-based
                ctrl = max(option_year - game_year, 1)
                pv = _proj_value_at_year(conn, pid, age, bucket, composite, ceiling,
                                         stat_war, ctrl, dpw, min_sal, weights)
                entry["proj_value"] = pv
                if pv is not None:
                    breakeven = opt_sal - buyout
                    if pv >= opt_sal:
                        entry["rec"], entry["rec_class"] = "Exercise", "good"
                    elif pv > breakeven:
                        entry["rec"], entry["rec_class"] = "Exercise (marginal)", "ok"
                    else:
                        entry["rec"], entry["rec_class"] = "Decline", "bad"
            elif typ == "Player":
                entry["rec"], entry["rec_class"] = "Player decides", "ok"
            else:
                entry["rec"], entry["rec_class"] = "Auto (performance)", "ok"
            entries.append(entry)

    # Split by decision timing: this offseason (option year == game_year + 1)
    # vs upcoming (game_year + 2 and beyond). Anything with an option year at or
    # before the current game year is a past/expiring edge case — group it with
    # "this offseason" so it isn't silently dropped.
    this_off = [e for e in entries if e["year"] <= game_year + 1]
    upcoming = [e for e in entries if e["year"] >= game_year + 2]
    this_off.sort(key=lambda x: (x["type"] != "Team", -(x["option_salary"] or 0)))
    upcoming.sort(key=lambda x: (x["year"], x["type"] != "Team", -(x["option_salary"] or 0)))
    return {"this_offseason": this_off, "upcoming": upcoming}

def _milb_level_map(cfg):
    """league_id -> {level:int, abbr:str} from the cumulative milb_league_map."""
    m = {}
    for lid, v in (cfg.settings.get("milb_league_map") or {}).items():
        try:
            m[int(lid)] = {"level": int(v.get("level")), "abbr": v.get("abbr")}
        except (TypeError, ValueError):
            continue
    return m


def _ensure_r5_pe(conn, ed):
    """Rule 5 reads upstream's unified `player_evaluation` shape. This fork keeps
    FV/surplus in prospect_fv + player_surplus (player_evaluation is empty here),
    so expose the same columns as a per-connection TEMP VIEW over those tables
    and the latest ratings. FV comes from prospect_fv (no age cap in this fork)
    with player_surplus as the fallback for MLB-level rows."""
    conn.execute("DROP VIEW IF EXISTS temp.r5_pe")
    conn.execute(f"""
        CREATE TEMP VIEW r5_pe AS
        SELECT p.player_id AS player_id, '{ed}' AS eval_date,
               COALESCE(pf.bucket, ps.bucket) AS bucket,
               lr.composite_score AS composite,
               COALESCE(lr.true_ceiling, lr.ceiling_score) AS ceiling,
               COALESCE(pf.fv, ps.fv) AS fv,
               COALESCE(pf.fv_str, ps.fv_str) AS fv_str,
               COALESCE(pf.level, ps.level) AS level,
               pf.risk AS risk,
               COALESCE(pf.prospect_surplus, ps.surplus) AS surplus,
               p.team_id AS team_id
        FROM players p
        LEFT JOIN prospect_fv pf ON pf.player_id = p.player_id AND pf.eval_date = '{ed}'
        LEFT JOIN player_surplus ps ON ps.player_id = p.player_id AND ps.eval_date = '{ed}'
        LEFT JOIN latest_ratings lr ON lr.player_id = p.player_id
        WHERE pf.player_id IS NOT NULL OR ps.player_id IS NOT NULL
    """)




# ---------------------------------------------------------------------------
# Rule 5 — protect (your eligibles worth a 40-man spot) + targets (others')
# ---------------------------------------------------------------------------
#
# `years_protected_from_rule_5` (from /players) is the protection WINDOW length —
# how many pro seasons a player is shielded before exposure — NOT a countdown.
# Live vMLB /players (Session 98): the field holds only 5 (signed age ≤18) or 4
# (signed older), the two real Rule 5 thresholds; it does not tick down to 0.
# The large `ypr=0` population is the level-0 retired/FA pool, not eligibles.
# Eligibility is service-based and was pinned by diffing against OOTP's exported
# draft pool (1,919 players): a player is exposed once his accumulated
# `pro_service_years` reaches ONE SHORT of his `years_protected_from_rule_5`
# window (OOTP exposes a year earlier than the raw window). `draft_eligible` is
# amateur-draft eligibility (dormant outside the pre-draft window), NOT Rule 5.
#
# Eligible & exposed = pro_service_years >= years_protected_from_rule_5 - 1, not
# on the 40-man (is_on_secondary != 1), and not the level-0 historical/FA pool.
# MLB-level players (level '1') CAN be eligible — 31 pool members are on the
# active roster but off the 40-man — so only level '0' is excluded, not '1'.

# MLB-viable floor for the target side: a Rule 5 pick must stick on the active
# roster all year, so only players with real MLB projection are worth taking.
_RULE5_TARGET_MIN_FV = 45


def _rule5_readiness(level, age, mlb_service, bucket=None):
    """Classify how DRAFTABLE a Rule 5-eligible player is — i.e. how realistically
    a rival could carry him on their active 26-man roster all season (the Rule 5
    retention rule). This is the exposure-risk lens, orthogonal to raw talent.

    Hitters — must hold a bench/bat spot, so the bar is level + age:
      MLB-ready  : AAA / MLB, OR AA age ≥ 24, OR prior MLB service.
      Near-ready : AA (younger), or a young AAA bat.
      Distant    : A / Rookie and young.

    PITCHERS — RP-as-floor. A Rule 5 arm doesn't need to stick in the ROTATION;
    the classic Rule 5 move is to carry a live arm in the BULLPEN for the year.
    Relief needs less polish than starting, so the bar to "usable in relief" is
    lower than the hitter bar: any arm at AA+ (or with MLB service) is MLB-ready
    as a reliever; only A/Rookie arms are Near-ready/Distant. This is a stickiness
    (display/eligibility) judgement — it does NOT re-value the pitcher as an RP;
    the SP↔RP *valuation* (stuff-by-role) is a separate tracked model item.

    Returns one of "MLB-ready" / "Near-ready" / "Distant".
    """
    lvl = (level or "").upper()
    if (mlb_service or 0) > 0:
        return "MLB-ready"

    is_pitcher = bucket in _PITCHER_BUCKETS
    if is_pitcher:
        # Relief floor: AA+ arms can hold a bullpen spot now.
        if lvl in ("MLB", "AAA", "AA"):
            return "MLB-ready"
        if lvl in ("A", "A-SHORT", "A+"):
            return "Near-ready"
        return "Distant"

    # Hitters — need the bat to play, so hold the stiffer level/age bar.
    if lvl in ("MLB", "AAA"):
        return "MLB-ready"
    if lvl == "AA":
        return "MLB-ready" if (age or 0) >= 24 else "Near-ready"
    return "Distant"


def _rule5_role_label(bucket, listed_role):
    """Realistic Rule 5 role for the card/table. For arms, surface the bullpen
    path explicitly since that's how most Rule 5 pitchers actually stick:

      bucket RP                      -> "RP"
      bucket SP, listed as starter   -> "SP → RP"   (carry him in relief this year)
      bucket SP, listed as reliever  -> "RP"        (already a pen arm)

    Hitters just show their position bucket. `listed_role`: 11=SP, 12=RP, 13=CL.
    """
    if bucket == "RP":
        return "RP"
    if bucket == "SP":
        # listed_role 12/13 = already a reliever/closer in-game → just "RP".
        return "RP" if listed_role in (12, 13) else "SP → RP"
    return _display_pos(bucket) if bucket else "?"


# Protect recommendation matrix — value (FV) × draftability (readiness tier).
# Each cell is (rec_label, rec_class). "Expose" is styled "bad" only as a visual
# cue (red = letting him go), not a knock on the player. See the design note in
# get_rule5. The 40-man-crunch modifier (advisory) is applied separately.
_PROTECT_MATRIX = {
    # (value_tier, readiness) -> (rec, rec_class)
    ("high",   "MLB-ready"):  ("Protect",  "good"),
    ("high",   "Near-ready"): ("Protect",  "good"),
    ("high",   "Distant"):    ("Consider", "ok"),
    ("solid",  "MLB-ready"):  ("Protect",  "good"),
    ("solid",  "Near-ready"): ("Consider", "ok"),
    ("solid",  "Distant"):    ("Expose",   "bad"),
    ("fringe", "MLB-ready"):  ("Consider", "ok"),
    ("fringe", "Near-ready"): ("Expose",   "bad"),
    ("fringe", "Distant"):    ("Expose",   "bad"),
}


def _rule5_protect_rec(fv, readiness, roster_full):
    """Protect-vs-expose recommendation for one eligible player.

    value tier: FV ≥ 50 = high, 45–49 = solid, ≤ 44 = fringe.
    40-man modifier (advisory): when the roster is full, a Protect costs a
    corresponding move, so we FLAG that cost on the chip ("Protect (needs a
    40-man move)", styled ok) without demoting the call — the GM decides who
    they'd cut. Clear Expose/Consider calls are unaffected.

    Returns (rec, rec_class, readiness, why).
    """
    vt = "high" if fv >= 50 else "solid" if fv >= 45 else "fringe"
    rec, rec_class = _PROTECT_MATRIX[(vt, readiness)]

    why = {
        "MLB-ready": "a rival could carry him on their active roster now",
        "Near-ready": "a rebuilding team might stash him",
        "Distant": "too far from the majors for a rival to keep him all year",
    }[readiness]
    why = f"FV {fv}, {readiness} — {why}"

    if roster_full and rec == "Protect":
        rec = "Protect (needs a 40-man move)"
        rec_class = "ok"
        why += "; your 40-man is full, so protecting means a corresponding move"

    return rec, rec_class, readiness, why


# The 40-man (secondary) roster cap. Standard across OOTP leagues.
_FORTY_MAN_CAP = 40


def _forty_man_count(conn, team_id):
    """How many players this org currently has on its 40-man (secondary) roster.
    Uses the standard org attribution (ORG_ID_SQL) so minor-league affiliates
    count toward the parent club."""
    row = conn.execute(
        f"SELECT COUNT(*) n FROM players p "
        f"WHERE p.is_on_secondary = 1 AND {ORG_ID_SQL} = ?",
        (team_id,)
    ).fetchone()
    return row["n"] if row else 0


def _rule5_cut_candidates(conn, team_id, ed, lvl_by_league, level_names, limit=6):
    """"Low-hanging fruit" to clear a 40-man spot — players low on BOTH value axes.

    Neither metric works alone (verified against vMLB):
      • surplus alone  — the WAR projection reads off noisy small-sample reliever
        stats (stat_confidence ramp), so good RPs like Gaytan/Shackleton show
        near-zero surplus and look cuttable (tracked model item). A high-composite
        arm must NOT be flagged on broken surplus.
      • composite alone — flags premium young prospects (low current ability, huge
        FUTURE surplus) as cuttable. A high-surplus prospect must NOT be flagged.

    So a genuine cut candidate is low on BOTH: little current ability (composite)
    AND little projected value (surplus). That gate neutralizes each metric's blind
    spot — Gaytan survives on composite, a top prospect survives on surplus. Ranked
    by the combined normalized score (lowest first). Each carries comp/ceiling (the
    value axes shown), FV (for prospects), and a level-qualified stat line. Still
    advisory; the GM weighs contract/options/role.
    """
    rows = conn.execute(f"""
        SELECT p.player_id, p.name, p.age, pe.fv, pe.fv_str, pe.bucket,
               pe.composite, pe.ceiling, pe.surplus, pe.level
        FROM r5_pe pe
        JOIN players p ON pe.player_id = p.player_id
        WHERE pe.eval_date = ? AND {ORG_ID_SQL} = ? AND p.is_on_secondary = 1
    """, (ed, team_id)).fetchall()

    _SURP_CAP = 5_000_000  # surplus above this is clearly a keeper on value alone

    def score(r):
        comp_n = (r["composite"] or 0) / 80.0
        surp_n = min(max(r["surplus"] or 0, 0), _SURP_CAP) / _SURP_CAP
        return comp_n + surp_n  # low on both → low score

    chosen = sorted(rows, key=score)[:limit]
    stat_lines = _rule5_stat_lines(conn, [r["player_id"] for r in chosen],
                                   lvl_by_league, level_names)
    out = []
    for r in chosen:
        fv = r["fv"]
        pos = _display_pos(r["bucket"]) if r["bucket"] else "?"
        sl = stat_lines.get(r["player_id"])
        out.append({
            "pid": r["player_id"], "name": r["name"], "age": r["age"],
            "pos": pos, "fv": fv or 0,
            # FV string only when it's a prospect (graduated MLBers have fv NULL).
            "fv_str": r["fv_str"] if fv else None,
            "level": r["level"] or "", "surplus": r["surplus"] or 0,
            "composite": r["composite"] or 0, "ceiling": r["ceiling"] or 0,
            "stat_line": sl["line"] if sl else None,
            "stat_level": sl["level"] if sl else None,
        })
    return out


def _rule5_draft_position(conn, team_id):
    """Projected Rule 5 pick slot: reverse regular-season standings (worst record
    picks first), scoped to the MLB clubs only (the standings table also carries
    minor-league affiliates, which must be excluded or the 0-0 affiliates sort to
    the top). Returns {"pick": int, "of": int} or None if standings unavailable.

    Approximate — OOTP's exact order can apply tie-breaks (prior-year record etc.)
    we don't model, so the UI should present it as "~Nth", not a hard slot.
    """
    try:
        mlb_ids = set(int(k) for k in get_cfg().team_abbr_map.keys())
    except Exception:
        return None
    if not mlb_ids:
        return None
    rows = conn.execute(
        f"SELECT team_id, w, l, pct FROM standings "
        f"WHERE team_id IN ({','.join('?' * len(mlb_ids))}) "
        f"ORDER BY pct ASC, l DESC",
        list(mlb_ids)
    ).fetchall()
    if not rows:
        return None
    for i, r in enumerate(rows, 1):
        if r["team_id"] == team_id:
            return {"pick": i, "of": len(rows)}
    return None


# Hitter buckets (anything not a pitcher role) — for stat-line selection.
_PITCHER_BUCKETS = {"SP", "RP"}


def _rule5_stat_lines(conn, pids, lvl_by_league, level_names):
    """Compact most-recent-season stat line per player, QUALIFIED BY the level the
    stats were accrued at (a .360 in AAA is worth more than in AA — and a player's
    current `level` isn't necessarily where he played last year). Picks the overall
    (split_id=1) row with the most PA (hitters) / IP (pitchers) in the player's
    latest year. Returns {pid: {kind, line, war, year, level}}.

      lvl_by_league : {league_id: level_int}  (from the milb_league_map)
      level_names   : {level_int: name}       (level_map; e.g. 2->"AAA")

    A NULL league_id = MLB. `level` is the resolved display label (AAA/AA/MLB/…).
    """
    if not pids:
        return {}

    def level_label(league_id):
        if league_id is None:
            return "MLB"
        lv = lvl_by_league.get(league_id)
        return level_names.get(lv, "MiLB") if lv is not None else "MiLB"

    def level_int(league_id):
        # 1=MLB … higher = lower level; None → treat as deep MiLB (6).
        if league_id is None:
            return 1
        return lvl_by_league.get(league_id, 6)

    out = {}
    ph = ",".join("?" * len(pids))
    # Hitters — latest year, overall split, most PA.
    for r in conn.execute(
        f"""SELECT b.player_id, b.year, b.league_id, b.pa, b.avg, b.obp, b.slg,
                   b.hr, b.sb, b.war
            FROM batting_stats b
            JOIN (SELECT player_id, MAX(year) y FROM batting_stats
                  WHERE split_id=1 AND player_id IN ({ph}) GROUP BY player_id) m
              ON b.player_id=m.player_id AND b.year=m.y
            WHERE b.split_id=1
            ORDER BY b.pa DESC""", list(pids)):
        if r["player_id"] in out or (r["pa"] or 0) < 1:
            continue
        obp = r["obp"] or 0
        slg = r["slg"] or 0
        out[r["player_id"]] = {
            "kind": "bat", "year": r["year"], "war": r["war"],
            "level": level_label(r["league_id"]), "level_int": level_int(r["league_id"]),
            "ops": obp + slg, "pa": r["pa"] or 0,
            "line": f"{_slash(r['avg'])}/{_slash(r['obp'])}/{_slash(r['slg'])}, "
                    f"{r['hr'] or 0} HR, {r['sb'] or 0} SB ({r['pa']} PA)",
        }
    # Pitchers — latest year, overall split, most IP.
    for r in conn.execute(
        f"""SELECT p.player_id, p.year, p.league_id, p.ip, p.era, p.k, p.bb,
                   p.gs, p.war
            FROM pitching_stats p
            JOIN (SELECT player_id, MAX(year) y FROM pitching_stats
                  WHERE split_id=1 AND player_id IN ({ph}) GROUP BY player_id) m
              ON p.player_id=m.player_id AND p.year=m.y
            WHERE p.split_id=1
            ORDER BY p.ip DESC""", list(pids)):
        if r["player_id"] in out or (r["ip"] or 0) < 1:
            continue
        ip = r["ip"] or 0
        era = r["era"] if r["era"] is not None else None
        k = r["k"] or 0
        bb = r["bb"] or 0
        out[r["player_id"]] = {
            "kind": "pit", "year": r["year"], "war": r["war"],
            "level": level_label(r["league_id"]), "level_int": level_int(r["league_id"]),
            "era": era, "ip": ip, "k9": (k * 9.0 / ip) if ip else 0,
            "k_bb": (k / bb) if bb else (k if k else 0),
            "line": f"{(era if era is not None else 0):.2f} ERA, "
                    f"{ip:.0f} IP, {k}/{bb} K/BB",
        }
    return out


def _slash(v):
    """Format a rate stat as a leading-dot slash number (.274)."""
    try:
        return f"{v:.3f}".lstrip("0") or ".000"
    except (TypeError, ValueError):
        return "—"


# Level credit — a dominant line at a lower level is worth much less (user: "that
# statline in AAA is worth more than in AA"). For a RULE 5 pick, who must jump
# STRAIGHT to the majors, A-ball and below are barely predictive, so the discount
# is steep: only AAA/AA/MLB production carries real weight.
# 1=MLB, 2=AAA, 3=AA, 4=A, 5=A-Short, 6=Rookie.
_LEVEL_CREDIT = {1: 1.0, 2: 1.0, 3: 0.80, 4: 0.40, 5: 0.30, 6: 0.25}

# The top-targets strip only headlines players whose readiness is backed by
# UPPER-level reps (the only realistic straight-to-MLB sticks). Lower-level
# players still appear in the full pool, just not in the headline strip.
_RULE5_STRIP_LEVELS = {"MLB", "AAA", "AA"}


def _rule5_perf_score(sl):
    """Level-adjusted recent-performance score in [0,1] from the attached stat
    line (`sl` from _rule5_stat_lines). This is the signal that separates a real
    Rule 5 pick from an FV mirage — a shiny FV with a 5.84 ERA shouldn't rank over
    a proven arm. Returns 0.5 (neutral) when there's no usable sample.

    Arms  : K/BB (the user's explicit signal) + ERA, each mapped to [0,1].
    Bats  : OPS vs a rough replacement baseline, PA-gated.
    All scaled by level credit so a lower-level line counts for less.
    """
    if not sl:
        return 0.5
    cred = _LEVEL_CREDIT.get(sl.get("level_int", 3), 0.7)

    if sl["kind"] == "pit":
        if (sl.get("ip") or 0) < 10:            # too few innings to read
            return 0.5
        # K/BB: 1.5→weak, 3.0→good, 5.0→elite. ERA: 5.0→weak, 3.5→good, 2.5→elite.
        kbb = sl.get("k_bb") or 0
        era = sl.get("era")
        kbb_s = _clamp01((kbb - 1.5) / (5.0 - 1.5))
        era_s = 0.5 if era is None else _clamp01((5.0 - era) / (5.0 - 2.5))
        raw = 0.6 * kbb_s + 0.4 * era_s        # K/BB leads (skill signal)
    else:
        if (sl.get("pa") or 0) < 60:            # tiny bat sample
            return 0.5
        ops = sl.get("ops") or 0
        # OPS: .650→weak, .780→good, .950→elite (level-agnostic baseline; the
        # level credit below handles the "where" discount).
        raw = _clamp01((ops - 0.650) / (0.950 - 0.650))

    # Apply level credit, but keep a neutral floor so a strong lower-level line
    # isn't crushed to zero — it's discounted, not disqualified.
    return 0.5 + (raw - 0.5) * (0.5 + 0.5 * cred) if raw >= 0.5 \
        else raw * (0.5 + 0.5 * cred) + 0.5 * (1 - (0.5 + 0.5 * cred))


def _clamp01(x):
    return 0.0 if x < 0 else 1.0 if x > 1 else x


def _rule5_target_score(fv, age, perf):
    """Composite ranking score for a Rule 5 target (readiness already gates the
    pool to MLB-ready for the top strip). Blends:
      performance (recent, level-adjusted) — RESULTS-FIRST (user direction),
      upside (FV + youth)                  — the tiebreak among similar performers.
    Performance is weighted enough to reorder across a ~5-point FV gap so a
    proven FV-45 arm outranks an FV-50 arm with a poor line, while FV/age still
    tilt between comparable performers. Higher = better.
    """
    fv_s = _clamp01((fv - 40) / (65 - 40))          # FV 40→0, 65→1
    age_s = _clamp01((26 - (age or 26)) / (26 - 20))  # 26→0, 20→1 (youth = upside)
    upside = 0.75 * fv_s + 0.25 * age_s
    return 0.60 * perf + 0.40 * upside              # results-first blend


def get_rule5(team_id):
    """Rule 5 decisions for this offseason.

    Returns {"protect": [...], "targets": [...], "available": bool}:
      - protect  : your org's Rule 5-eligible players (service window elapsed,
                   off the 40-man), ranked by FV/surplus — add-to-40-man candidates.
      - targets  : other orgs' eligible + MLB-viable (FV >= 45) players you could
                   draft, ranked by FV.

    `available` is False when the field isn't populated yet (pre-refresh), so the
    template can show an informative message rather than empty tables.
    """
    conn = get_db()
    ed = _eval_date(conn)
    if ed is not None:
        _ensure_r5_pe(conn, ed)

    # Has the ypr field been populated at all? (NULL everywhere pre-refresh.)
    has_field = conn.execute(
        "SELECT 1 FROM players WHERE years_protected_from_rule_5 IS NOT NULL LIMIT 1"
    ).fetchone() is not None
    if ed is None or not has_field:
        return {"protect": [], "targets": [], "available": has_field}

    # Base eligibility predicate (shared by both sides).
    #
    # `years_protected_from_rule_5` is the player's protection WINDOW length — the
    # number of pro seasons he is shielded before exposure — NOT a live countdown.
    # Confirmed against the vMLB live /players feed (Session 98): the field only
    # ever holds 5 (signed young, age ≤18) or 4 (signed older), the two real Rule 5
    # thresholds; it does not decrement toward 0. (The large `ypr=0` population is
    # the level-0 retired/FA pool, not eligible players.) `pro_service_years` is a
    # well-populated accumulated-service count. A player is exposed once his
    # accumulated pro service reaches ONE SHORT OF his protection window:
    #       pro_service_years >= years_protected_from_rule_5 - 1
    #
    # The off-by-one and the level test below were both pinned by diffing against
    # OOTP's own exported draft pool (1,919 players): the strict `>= ypr` boundary
    # under-counted by 842 (OOTP exposes a year earlier than the raw window), and
    # 31 pool members sit at level '1' (on the active MLB roster but OFF the 40-man)
    # — so MLB-level players ARE eligible when off the 40-man; only the level-0
    # historical/retired/FA pool is excluded. Final rule reproduces the OOTP pool
    # at 1,919/1,919 recall with 3 boundary false positives.
    #
    # Eligible & exposed =
    #   pro_service_years >= years_protected_from_rule_5 - 1   (window all but elapsed)
    #   AND not on the 40-man (is_on_secondary != 1)           (40-man players are safe)
    #   AND not the level-0 historical/retired/FA pool
    #
    # NB: do NOT gate on is_active — in the /players feed is_active = 1 means
    # "on the active MLB roster", so every minor-leaguer (most of the Rule 5 pool)
    # is is_active = 0.
    eligible = (
        "p.pro_service_years IS NOT NULL "
        "AND p.years_protected_from_rule_5 IS NOT NULL "
        "AND p.pro_service_years >= p.years_protected_from_rule_5 - 1 "
        "AND (p.is_on_secondary IS NULL OR p.is_on_secondary != 1) "
        "AND p.level != '0'"
    )

    # 40-man roster state — gates and contextualizes every protect decision.
    forty_count = _forty_man_count(conn, team_id)
    spots_open = max(0, _FORTY_MAN_CAP - forty_count)
    roster_full = spots_open == 0

    # Protect side — your org's eligibles, ranked by FV then surplus.
    # Source FV from player_evaluation (the full table), NOT the prospect_fv view:
    # prospect_fv is age-gated at ≤ 25, but Rule 5 eligibility spans older org
    # depth too (a 27-yo AAA arm is still a real protect/expose decision).
    # The recommendation weighs FV against DRAFTABILITY (readiness tier) and the
    # 40-man crunch — see _rule5_protect_rec.
    protect = []
    for r in conn.execute(f"""
        SELECT p.player_id, p.name, p.age, p.mlb_service_years, p.role,
               pe.fv, pe.fv_str, pe.bucket, pe.risk, pe.surplus, pe.level
        FROM r5_pe pe
        JOIN players p ON pe.player_id = p.player_id
        WHERE pe.eval_date = ? AND {ORG_ID_SQL} = ? AND {eligible}
        ORDER BY pe.fv DESC, pe.surplus DESC, p.age ASC
    """, (ed, team_id)).fetchall():
        fv = r["fv"] or 0
        readiness = _rule5_readiness(r["level"], r["age"], r["mlb_service_years"], r["bucket"])
        rec, rec_class, readiness, why = _rule5_protect_rec(fv, readiness, roster_full)
        protect.append({
            "pid": r["player_id"], "name": r["name"], "age": r["age"],
            "pos": _rule5_role_label(r["bucket"], r["role"]),
            "fv": fv, "fv_str": r["fv_str"] or str(fv), "risk": r["risk"] or "",
            "level": r["level"] or "",
            "surplus": r["surplus"] or 0,  # raw $ — template money filter
            "readiness": readiness,
            "rec": rec, "rec_class": rec_class, "rec_why": why,
        })

    # Attach a compact season stat line to each eligible — the evidence behind
    # the protect/expose call, qualified by the level it was accrued at (a strong
    # AAA line argues for protecting; the same line in AA is worth less).
    cfg = get_cfg()
    lvl_by_league = {lid: v["level"] for lid, v in _milb_level_map(cfg).items()}
    level_names = {int(k): v for k, v in (cfg.settings.get("level_map") or {}).items()}
    stat_lines = _rule5_stat_lines(conn, [p["pid"] for p in protect],
                                   lvl_by_league, level_names)
    for p in protect:
        sl = stat_lines.get(p["pid"])
        p["stat_line"] = sl["line"] if sl else None
        p["stat_war"] = sl["war"] if sl else None
        p["stat_year"] = sl["year"] if sl else None
        p["stat_level"] = sl["level"] if sl else None

    # Display tiers for the UI — triage the flat protect list into decision bands
    # so the template leads with real decisions and collapses the rest:
    #   protect  : rec Protect        — act now (prominent rows)
    #   consider : rec Consider, FV≥40 — genuine judgement calls (prominent rows)
    #   fringe   : rec Consider, FV≤39 — technically exposed but not real dilemmas
    #              (collapsed behind "show N more fringe")
    #   exposed  : rec starts with Expose — "do nothing"; collapsed to a count
    # (The flat `protect` list is kept too, for callers/tests that want everything.)
    protect_tiers = {
        "protect": [p for p in protect if p["rec"].startswith("Protect")],
        "consider": [p for p in protect if p["rec"] == "Consider" and p["fv"] >= 40],
        "fringe": [p for p in protect if p["rec"] == "Consider" and p["fv"] < 40],
        "exposed": [p for p in protect if p["rec"].startswith("Expose")],
    }

    # Target side — other orgs' eligible, MLB-viable players.
    # Each carries a readiness tier (a Rule 5 pick MUST stick on the active roster
    # all year, so MLB-ready targets are the only truly realistic picks) and a
    # need-fill flag against your org's positional holes (reused from the FA board).
    abbr = get_cfg().team_abbr_map
    need_positions = _team_need_positions(team_id)
    targets = []
    for r in conn.execute(f"""
        SELECT p.player_id, p.name, p.age, p.mlb_service_years, p.role,
               pe.fv, pe.fv_str, pe.bucket, pe.risk, pe.level, {ORG_ID_SQL} AS org_id
        FROM r5_pe pe
        JOIN players p ON pe.player_id = p.player_id
        WHERE pe.eval_date = ? AND {ORG_ID_SQL} != ? AND {eligible}
          AND pe.fv >= ?
        ORDER BY pe.fv DESC, p.age ASC
    """, (ed, team_id, _RULE5_TARGET_MIN_FV)).fetchall():
        pos = _rule5_role_label(r["bucket"], r["role"])
        readiness = _rule5_readiness(r["level"], r["age"], r["mlb_service_years"], r["bucket"])
        targets.append({
            "pid": r["player_id"], "name": r["name"], "age": r["age"],
            "pos": pos,
            "fv": r["fv"] or 0, "fv_str": r["fv_str"] or str(r["fv"] or 0),
            "risk": r["risk"] or "", "level": r["level"] or "",
            "team": abbr.get(r["org_id"], "?"),
            "readiness": readiness,
            "fills_need": _need_key(_display_pos(r["bucket"]) if r["bucket"] else "?") in need_positions,
        })

    # Level-qualified season stat line for each target too — backs the "can he
    # stick?" read with his actual production and where he earned it. Reuses the
    # same helper + maps built for the protect side above.
    tgt_stats = _rule5_stat_lines(conn, [t["pid"] for t in targets],
                                  lvl_by_league, level_names)
    for t in targets:
        sl = tgt_stats.get(t["pid"])
        t["stat_line"] = sl["line"] if sl else None
        t["stat_war"] = sl["war"] if sl else None
        t["stat_level"] = sl["level"] if sl else None
        # Results-first ranking signal (level-adjusted recent performance) + the
        # composite target score that blends it with FV/age upside.
        t["perf"] = _rule5_perf_score(sl)
        t["score"] = _rule5_target_score(t["fv"], t["age"], t["perf"])

    # Rank the whole pool by the composite score (results-first), not raw FV —
    # so a proven arm outranks a shiny-FV mirage with a poor line.
    targets.sort(key=lambda t: -t["score"])

    return {
        "protect": protect,
        "protect_tiers": protect_tiers,
        "targets": targets,
        # Curated shortlist for the "top realistic targets" strip: MLB-ready AND
        # backed by UPPER-level reps (AAA/AA/MLB) — the only realistic straight-to-
        # majors sticks. Ranked by the composite results-first score. Lower-level
        # players stay in the full pool below, just not the headline. Need-fill is
        # a ★ flag only, not a ranking input (user decision).
        "top_targets": sorted(
            [t for t in targets
             if t["readiness"] == "MLB-ready"
             and (t.get("stat_level") in _RULE5_STRIP_LEVELS)],
            key=lambda t: -t["score"],
        )[:6],
        "available": True,
        # 40-man roster state — the headline constraint on both sides.
        "forty_man": {
            "count": forty_count,
            "cap": _FORTY_MAN_CAP,
            "open": spots_open,
            "full": roster_full,
            # "Low-hanging fruit" to clear a spot — fringe minor-league 40-man
            # depth only (never active-roster contributors). May be empty when
            # the 40-man is all on the active MLB roster (e.g. deep offseason).
            "cut_candidates": _rule5_cut_candidates(conn, team_id, ed,
                                                     lvl_by_league, level_names),
        },
        # Projected Rule 5 pick slot (reverse regular-season standings over the
        # MLB clubs). Approximate — ties/edge rules aren't modeled. None if the
        # standings aren't available.
        "draft_pos": _rule5_draft_position(conn, team_id),
        # Your positional holes — doubles as the draft-board quick-filter strip.
        "needs": sorted(need_positions),
    }
