"""Trade-specific queries for the web dashboard."""

import os, sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "scripts"))
from player_utils import display_pos as _display_pos
from contract_value import contract_value
from prospect_value import prospect_surplus_with_option, find_player, career_outcome_probs
from web_league_context import get_db, get_cfg, team_abbr_map, level_map, year

SENSITIVITY = {"pessimistic": 0.85, "base": 1.00, "optimistic": 1.15}


def get_org_players(team_id):
    """Full org roster (MLB + farm) for the trade tab roster table."""
    conn = get_db()
    conn.row_factory = None
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
        rookie_pids = {r[0] for r in rows}

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
            pid = r[0]
            if pid in rookie_pids:
                continue
            mlb.append({
                "pid": pid, "name": r[1],
                "pos": _display_pos(r[2], r[7]) if r[2] else _pm.get(str(r[7]), "?"),
                "age": r[3], "level": "MLB",
                "ovr": r[4], "pot": r[6],
                "fv": None, "fv_str": None,
                "surplus": r[5] or 0,
                "war": round(r[8], 1) if r[8] else None,
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
                "pid": r[0], "name": r[1],
                "pos": _display_pos(r[2], r[10]) if r[2] else _pm.get(str(r[10]), "?"),
                "age": r[3], "level": _lm.get(str(r[6]), r[6]) if r[6] else "?",
                "ovr": r[8], "pot": r[9],
                "fv": r[4], "fv_str": r[5],
                "surplus": r[7] or 0,
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
               p.parent_team_id, p.pos
        FROM prospect_fv pf JOIN players p ON p.player_id = pf.player_id
        WHERE pf.player_id = ? ORDER BY pf.eval_date DESC LIMIT 1
    """, (player_id,)).fetchone()

    if pf:
        fv, fv_str, level, bucket = pf[0], pf[1], pf[2], pf[3]
        age, name, tid, ptid, pos_code = pf[4], pf[5], pf[6], pf[7], pf[8]
        fv_plus = str(fv_str).endswith("+")
        rr = conn.execute("SELECT ovr, pot, pot_cf, pot_ss, pot_c, pot_second_b, pot_third_b "
                          "FROM latest_ratings WHERE player_id=?", (player_id,)).fetchone()
        ovr = rr[0] if rr else None
        pot = rr[1] if rr else None
        _dk = {'CF': 2, 'SS': 3, 'C': 4, '2B': 5, '3B': 6}
        def_rating = rr[_dk[bucket]] if rr and bucket in _dk else None

        base = prospect_surplus_with_option(fv, age, level, bucket,
                                            ovr=ovr, pot=pot, fv_plus=fv_plus,
                                            def_rating=def_rating)
        surplus = {s: max(0, round(base * m)) for s, m in SENSITIVITY.items()}

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

    # MLB contract path
    result = contract_value(player_id, retention_pct=retention_pct)
    if not result:
        return None

    d = result
    tid_row = conn.execute("SELECT team_id, pos FROM players WHERE player_id=?",
                           (player_id,)).fetchone()
    team = _tam.get(tid_row[0], "?") if tid_row else "?"
    rr = conn.execute("SELECT pot FROM latest_ratings WHERE player_id=?",
                      (player_id,)).fetchone()

    return {
        "player_id": player_id, "name": d["name"], "type": "contract",
        "team": team, "age": d["age"],
        "pos": _display_pos(d["bucket"], tid_row[1] if tid_row else 0),
        "ovr": d["ovr"], "pot": rr[0] if rr else None,
        "years_left": d["years_left"],
        "flags": d["flags"],
        "retention_pct": retention_pct,
        "surplus": d["total_surplus"],
        "breakdown": d["breakdown"],
    }
