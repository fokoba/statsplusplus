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


def _eval_date(conn):
    row = conn.execute("SELECT MAX(eval_date) FROM player_evaluation").fetchone()
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
               pe.surplus, pe.composite, pe.ceiling, pe.bucket,
               pe.peak_war, pe.stat_war
        FROM contracts c
        JOIN players p ON p.player_id = c.player_id
        JOIN player_evaluation pe ON pe.player_id = c.player_id AND pe.eval_date = ?
        WHERE c.contract_team_id = ? AND c.is_major = 1
          AND p.level IN ('1', 1)
          AND (c.years - c.current_year) <= 1
    """, (ed, team_id)).fetchall()

    out = []
    for r in rows:
        pid, name, age, sal = r[0], r[1], r[2], r[3] or 0
        bucket = r[9] or "?"
        # Only above-minimum, controllable, not yet FA — i.e. arb-eligible.
        st = service_time(conn, pid)
        if st.is_free_agent_eligible:
            continue
        if sal <= min_sal:
            continue  # pre-arb (min salary) — not an arb tender decision

        # Projected next-year salary
        try:
            if perp:
                war = r[11] if r[11] is not None else (r[10] or 0)
                proj_sal = arb_salary_perpetual(
                    age, war or 0, dpw, min_sal,
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
        SELECT p.pos, p.role, MAX(pe.composite)
        FROM player_evaluation pe JOIN players p ON p.player_id = pe.player_id
        WHERE pe.eval_date = ? AND pe.team_id = ? AND pe.level = 'MLB'
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


def _proj_next_war(conn, pid, age, bucket, composite, ceiling, peak_war,
                   years_control, dpw, min_sal, weights):
    """Next-season projected WAR — the SAME number the player valuation page
    shows, by calling the shared compute_player_value and reading the first
    control-year of its breakdown. Single source of truth; do NOT re-derive
    (peak_war × aging_mult misses the development/confidence discount that
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
            career_pa=int(cpa), career_ip=float(cip), stat_war=peak_war,
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
    """
    conn = get_db()
    ed = _eval_date(conn)
    if ed is None:
        return []
    cfg = get_cfg()
    need_positions = _team_need_positions(team_id)
    incumbent = _incumbent_by_pos(conn, team_id, ed)
    from statsplusplus.evaluation.constants import load_model_weights
    from statsplusplus.config.league_config import dollars_per_war, league_minimum
    weights = load_model_weights(cfg.league_dir)
    dpw = dollars_per_war(cfg.league_dir)
    min_sal = league_minimum(cfg.league_dir)
    rows = conn.execute("""
        SELECT p.player_id, p.name, p.age, pe.composite, pe.ceiling, pe.peak_war,
               pe.bucket, lr.bats, lr.throws, p.pos, p.role, pe.years_control
        FROM players p
        JOIN player_evaluation pe ON pe.player_id = p.player_id AND pe.eval_date = ?
        LEFT JOIN latest_ratings lr ON lr.player_id = p.player_id
        WHERE p.free_agent = 1 AND p.team_id = 0
          AND pe.composite >= ?
          AND (EXISTS (SELECT 1 FROM mlb_batting_stats b WHERE b.player_id = p.player_id)
            OR EXISTS (SELECT 1 FROM mlb_pitching_stats pt WHERE pt.player_id = p.player_id))
        ORDER BY pe.composite DESC
        LIMIT ?
    """, (ed, _MARKET_MIN_COMPOSITE, limit)).fetchall()
    _hand = {1: "R", 2: "L", 3: "S", "R": "R", "L": "L", "S": "S"}
    out = []
    for r in rows:
        bucket = r[6] or "?"
        pos = _fa_pos(r[9], r[10], bucket)
        comp = r[3] or 0
        is_pitcher = pos in ("SP", "RP")
        inc = incumbent.get(pos)
        is_upgrade = inc is None or comp > inc
        fills_need = (_need_key(pos) in need_positions) and is_upgrade
        # Next-season projection — the SAME value the player valuation page shows
        # (shared compute_player_value; single source of truth).
        proj_war = _proj_next_war(conn, r[0], r[2], bucket, comp, r[4], r[5],
                                  r[11], dpw, min_sal, weights)
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
            "bats": _hand.get(r[7], "?"),
            "throws": _hand.get(r[8], "?"),
            "last": _last_season(conn, r[0], is_pitcher),
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
               pe.surplus, pe.composite, pe.ceiling, pe.bucket
        FROM contracts c
        JOIN players p ON p.player_id = c.player_id
        JOIN player_evaluation pe ON pe.player_id = c.player_id AND pe.eval_date = ?
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
            "pos": _display_pos(r[8]) if r[8] else "?",
            "yrs_left": max(1, r[3] - r[4]),
            "surplus": surplus,  # raw dollars — template uses the money filter
            "composite": r[6] or 0,
        })
    out.sort(key=lambda x: -x["surplus"])
    return out


# ---------------------------------------------------------------------------
# Contract options — this-offseason team/player/vesting option decisions
# ---------------------------------------------------------------------------

def _proj_value_at_year(conn, pid, age, bucket, composite, ceiling, peak_war,
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
            career_pa=int(cpa), career_ip=float(cip), stat_war=peak_war,
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
               pe.composite, pe.ceiling, pe.bucket, pe.peak_war
        FROM contracts c
        JOIN players p ON p.player_id = c.player_id
        JOIN player_evaluation pe ON pe.player_id = c.player_id AND pe.eval_date = ?
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
        composite, ceiling, bucket, peak_war = r[29], r[30], r[31] or "?", r[32]
        pos = _display_pos(bucket) if bucket != "?" else "?"

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
                                         peak_war, ctrl, dpw, min_sal, weights)
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
