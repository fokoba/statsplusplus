"""Trade-specific queries for the web dashboard."""

import os, sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "scripts"))
from statsplusplus.utils.positions import display_pos as _display_pos
from statsplusplus.evaluation.outcomes import career_outcome_probs
from statsplusplus.evaluation.player_value import compute_player_value
from web_league_context import get_db, get_cfg, team_abbr_map, level_map, year

SENSITIVITY = {"pessimistic": 0.85, "base": 1.00, "optimistic": 1.15}


def get_org_players(team_id):
    """Full org roster (MLB + farm) for the trade tab roster table."""
    conn = get_db()
    yr = year()
    _lm = level_map()
    _pm = {str(k): v for k, v in get_cfg().pos_map.items()}

    ed_s = conn.execute("SELECT MAX(eval_date) FROM player_surplus").fetchone()[0]
    ed_p = conn.execute("SELECT MAX(eval_date) FROM prospect_fv").fetchone()[0]

    # Rookie-eligible player IDs (in both tables) — these go in prospect section
    rookie_pids = set()
    if ed_s and ed_p:
        rows = conn.execute("""
            SELECT ps.player_id FROM player_surplus ps
            JOIN prospect_fv pf ON ps.player_id = pf.player_id
            WHERE ps.eval_date = ? AND pf.eval_date = ?
        """, (ed_s, ed_p)).fetchall()
        rookie_pids = {r["player_id"] for r in rows}

    # MLB players (exclude rookie-eligible — they'll appear as prospects)
    mlb = []
    if ed_s:
        mlb_rows = conn.execute("""
            SELECT ps.player_id, ps.name, ps.bucket, ps.age, ps.ovr, ps.surplus,
                   r.pot, p.pos,
                   COALESCE(b.war, 0) + COALESCE(
                       CASE WHEN pit.war IS NOT NULL THEN (pit.war + COALESCE(pit.ra9war, pit.war)) / 2.0
                            ELSE 0 END, 0) AS war
            FROM player_surplus ps
            JOIN players p ON ps.player_id = p.player_id
            LEFT JOIN latest_ratings r ON ps.player_id = r.player_id
            LEFT JOIN mlb_batting_stats b ON ps.player_id = b.player_id
                AND b.year = ? AND b.split_id = 1 AND b.team_id = ?
            LEFT JOIN mlb_pitching_stats pit ON ps.player_id = pit.player_id
                AND pit.year = ? AND pit.split_id = 1 AND pit.team_id = ?
            WHERE ps.eval_date = ? AND ps.team_id = ?
        """, (yr, team_id, yr, team_id, ed_s, team_id)).fetchall()

        for r in mlb_rows:
            pid = r["player_id"]
            if pid in rookie_pids:
                continue
            mlb.append({
                "pid": pid, "name": r["name"],
                "pos": _display_pos(r["bucket"], r["pos"]) if r["bucket"] else _pm.get(str(r["pos"]), "?"),
                "age": r["age"], "level": "MLB",
                "ovr": r["ovr"], "pot": r["pot"],
                "fv": None, "fv_str": None,
                "surplus": r["surplus"] or 0,
                "war": round(r["war"], 1) if r["war"] else None,
            })
    mlb.sort(key=lambda x: -(x["surplus"] or 0))

    # Prospects (includes rookie-eligible)
    prospects = []
    if ed_p:
        pro_rows = conn.execute("""
            SELECT pf.player_id, p.name, pf.bucket, p.age, pf.fv, pf.fv_str,
                   pf.level, pf.prospect_surplus, r.ovr, r.pot, p.pos
            FROM prospect_fv pf
            JOIN players p ON pf.player_id = p.player_id
            LEFT JOIN latest_ratings r ON pf.player_id = r.player_id
            WHERE pf.eval_date = ?
              AND (p.parent_team_id = ? OR (p.team_id = ? AND p.level = '1'))
        """, (ed_p, team_id, team_id)).fetchall()

        for r in pro_rows:
            prospects.append({
                "pid": r["player_id"], "name": r["name"],
                "pos": _display_pos(r["bucket"], r["pos"]) if r["bucket"] else _pm.get(str(r["pos"]), "?"),
                "age": r["age"], "level": _lm.get(str(r["level"]), r["level"]) if r["level"] else "?",
                "ovr": r["ovr"], "pot": r["pot"],
                "fv": r["fv"], "fv_str": r["fv_str"],
                "surplus": r["prospect_surplus"] or 0,
                "war": None,
            })

    def _pro_sort(x):
        fv = x["fv"] or 0
        fv_val = fv + (0.1 if (x["fv_str"] or "").endswith("+") else 0)
        return (-fv_val, -(x["surplus"] or 0))
    prospects.sort(key=_pro_sort)

    return mlb + prospects


def get_trade_value(player_id, retention_pct=0.0):
    """Compute trade valuation for a single player. Returns dict or None."""
    conn = get_db()
    _tam = team_abbr_map()
    _lm = level_map()

    # Check prospect_fv first (covers rookie-eligible)
    pf = conn.execute("""
        SELECT pf.fv, pf.fv_str, pf.level, pf.bucket, p.age, p.name, p.team_id,
               p.parent_team_id, p.pos, pf.fv_continuous
        FROM prospect_fv pf JOIN players p ON p.player_id = pf.player_id
        WHERE pf.player_id = ? ORDER BY pf.eval_date DESC LIMIT 1
    """, (player_id,)).fetchone()

    if pf:
        fv = pf["fv"]
        fv_str = pf["fv_str"]
        level = pf["level"]
        bucket = pf["bucket"]
        age = pf["age"]
        name = pf["name"]
        tid = pf["team_id"]
        ptid = pf["parent_team_id"]
        pos_code = pf["pos"]
        fv_continuous = pf["fv_continuous"]
        fv_plus = str(fv_str).endswith("+")
        # Use fv_continuous (pre-rounding) for surplus — matches fv_calc.py
        fv_for_surplus = fv_continuous if fv_continuous is not None else fv
        fv_plus_for_surplus = False if fv_continuous is not None else fv_plus
        rr = conn.execute("""
            SELECT ovr, pot, pot_cf, pot_ss, pot_c, pot_second_b, pot_third_b,
                   composite_score, true_ceiling, ceiling_score
            FROM latest_ratings WHERE player_id=?
        """, (player_id,)).fetchone()
        # Use model scores for certainty/option value and scarcity
        ovr = (rr["composite_score"] if rr and rr["composite_score"] else None) or (rr["ovr"] if rr else None)
        pot = (rr["true_ceiling"] or rr["ceiling_score"] if rr else None) or (rr["pot"] if rr else None)
        _dk = {'CF': 'pot_cf', 'SS': 'pot_ss', 'C': 'pot_c', '2B': 'pot_second_b', '3B': 'pot_third_b'}
        def_rating = rr[_dk[bucket]] if rr and bucket in _dk else None

        base = conn.execute(
            "SELECT surplus FROM player_evaluation WHERE player_id=? ORDER BY eval_date DESC LIMIT 1",
            (player_id,)).fetchone()
        base_surplus = base["surplus"] if base else 0
        surplus = {s: max(0, round(base_surplus * m)) for s, m in SENSITIVITY.items()}

        outcome = career_outcome_probs(fv, age, level, bucket,
                                       ovr=ovr, pot=pot, def_rating=def_rating)
        level_display = _lm.get(str(level), level) if level else "?"
        team = _tam.get(ptid or tid, "?")

        return {
            "player_id": player_id, "name": name, "type": "prospect",
            "team": team, "age": age,
            "pos": _display_pos(bucket, pos_code) if bucket else "?",
            "level": level_display, "fv": fv, "fv_str": fv_str,
            "ovr": ovr, "pot": pot,
            "surplus": surplus,
            "outcome": {
                "thresholds": outcome["thresholds"],
                "likely_range": list(outcome["likely_range"]),
                "confidence": outcome["confidence"],
            },
        }

    # MLB contract path — read from player_evaluation
    pe = conn.execute(
        "SELECT * FROM player_evaluation WHERE player_id=? ORDER BY eval_date DESC LIMIT 1",
        (player_id,)).fetchone()
    if not pe:
        return None

    tid_row = conn.execute("SELECT team_id, pos FROM players WHERE player_id=?",
                           (player_id,)).fetchone()
    team = _tam.get(tid_row["team_id"], "?") if tid_row else "?"
    rr = conn.execute("SELECT pot FROM latest_ratings WHERE player_id=?",
                      (player_id,)).fetchone()

    base_surplus = pe["surplus"] or 0
    # Apply retention if specified
    if retention_pct > 0:
        _c = conn.execute("SELECT salary_0, years, current_year FROM contracts WHERE player_id=?", (player_id,)).fetchone()
        if _c and _c["salary_0"]:
            remaining_yrs = max(1, (_c["years"] or 1) - (_c["current_year"] or 0))
            base_surplus += int(retention_pct * _c["salary_0"] * remaining_yrs)

    return {
        "player_id": player_id, "name": pe["name"], "type": "contract",
        "team": team, "age": pe["age"],
        "pos": _display_pos(pe["bucket"], tid_row["pos"] if tid_row else 0),
        "ovr": pe["composite"], "pot": rr["pot"] if rr else None,
        "years_left": pe["years_control"],
        "flags": [],
        "retention_pct": retention_pct,
        "surplus": {s: max(0, round(base_surplus * m)) for s, m in SENSITIVITY.items()},
        "breakdown": None,
    }
