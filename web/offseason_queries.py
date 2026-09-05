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
