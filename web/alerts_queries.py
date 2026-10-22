"""Alerts: waiver / DFA / free-agent pickups that beat your roster, and big
ratings changes since an earlier snapshot.

Pickups
    Every player currently on waivers or designated for assignment, plus
    signable free agents, compared with your weakest active player and with your
    starter at his position (same slot logic as the Trade Center). Players not
    seen on an earlier visit are marked new; the "seen" list is kept in
    config/alerts_state.json.

Ratings changes
    Current ratings against an earlier monthly ratings-history snapshot. Only
    the raw tool ratings are compared (composites in history were written by
    older model versions, so they aren't on one scale); each change is weighted
    by how much that tool drives value, and the current composite / FV /
    surplus are shown alongside.
"""

from __future__ import annotations

import json
from pathlib import Path

from web_league_context import get_db, get_cfg
import trade_center_queries as tc

HIT_TOOLS = [("cntct", "Contact", 1.0), ("gap", "Gap", 0.6), ("pow", "Power", 1.0), ("eye", "Eye", 0.8),
             ("speed", "Speed", 0.3)]
PIT_TOOLS = [("stf", "Stuff", 1.0), ("mov", "Movement", 1.0), ("ctrl", "Control", 1.0), ("stm", "Stamina", 0.3)]
POT_HIT = [("pot_cntct", "Pot Contact"), ("pot_gap", "Pot Gap"), ("pot_pow", "Pot Power"), ("pot_eye", "Pot Eye")]
POT_PIT = [("pot_stf", "Pot Stuff"), ("pot_mov", "Pot Movement"), ("pot_ctrl", "Pot Control")]


def _state_path(league_dir):
    return Path(league_dir) / "config" / "alerts_state.json"


def _load_state(league_dir):
    try:
        return json.loads(_state_path(league_dir).read_text())
    except (OSError, ValueError):
        return {"seen": []}


def _save_state(league_dir, st):
    p = _state_path(league_dir)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(st))
    tmp.replace(p)


# ── pickups ──────────────────────────────────────────────────────────────────

def pickups(conn, team_id, league_dir, weights, mark_seen=False):
    cfg = get_cfg()
    names = cfg.team_abbr_map or {}
    players = tc._mlb_players(conn)
    my_slots = tc._team_slots(players, weights).get(team_id, {})
    active = sorted((p for p in players if p["team_id"] == team_id), key=lambda p: p["comp"])
    fi = min(len(active) - 1, max(0, len(active) - 25))   # the 25th-best active player
    floor = active[fi]["comp"] if active else 40
    floor_name = active[fi]["name"] if active else ""
    rows = [dict(r) for r in conn.execute("""
        SELECT p.player_id, p.name, p.age, p.pos, p.role, p.team_id, p.level,
               p.is_on_waivers, p.designated_for_assignment, p.free_agent,
               r.composite_score AS comp, r.true_ceiling AS ceil,
               ps.surplus, pf.fv, pf.prospect_surplus
        FROM players p JOIN latest_ratings r ON r.player_id = p.player_id
        LEFT JOIN player_surplus ps ON ps.player_id=p.player_id AND ps.eval_date=(SELECT MAX(eval_date) FROM player_surplus)
        LEFT JOIN prospect_fv pf ON pf.player_id=p.player_id AND pf.eval_date=(SELECT MAX(eval_date) FROM prospect_fv)
        WHERE COALESCE(p.retired,0)=0 AND COALESCE(p.team_id,0) != ?
          AND (p.is_on_waivers=1 OR p.designated_for_assignment=1
               OR (p.free_agent=1 AND COALESCE(p.draft_eligible,0)=0 AND p.age >= 19
                   AND COALESCE(p.nation_id,0) != 98))
          AND r.composite_score IS NOT NULL
    """, (team_id,)).fetchall()]
    st = _load_state(league_dir)
    seen = set(st.get("seen") or [])
    out = []
    for p in rows:
        slot = tc._slot_of(p)
        if not slot:
            continue
        mine = my_slots.get(slot) or {}
        gain = tc._war_rate(p["comp"], slot, weights) - (mine.get("war") or 0)
        beats_floor = p["comp"] > floor
        is_fa = not (p["is_on_waivers"] or p["designated_for_assignment"])
        # Free agents are a big pool: only clear upgrades (a real WAR gain at his
        # slot, or well above your 25th man) make the list.
        if is_fa and not (gain > 0.3 or p["comp"] >= floor + 5):
            continue
        if not is_fa and not (beats_floor or gain > 0.3):
            continue
        status = ("Waivers" if p["is_on_waivers"] else "DFA" if p["designated_for_assignment"] else "Free agent")
        out.append({"pid": p["player_id"], "name": p["name"], "age": p["age"], "slot": slot,
                    "comp": p["comp"], "ceil": p["ceil"], "fv": p["fv"],
                    "surplus": p["surplus"] if p["surplus"] is not None else p["prospect_surplus"],
                    "status": status, "team": names.get(p["team_id"], "FA") if p["team_id"] else "FA",
                    "gain": round(gain, 1), "vs": mine.get("name"), "vs_comp": mine.get("comp"),
                    "beats_floor": beats_floor, "new": p["player_id"] not in seen})
    order = {"Waivers": 0, "DFA": 1, "Free agent": 2}
    out.sort(key=lambda x: (not x["new"], order[x["status"]], -x["gain"], -x["comp"]))
    if mark_seen:
        st["seen"] = sorted(seen | {x["pid"] for x in out})
        _save_state(league_dir, st)
    return {"rows": out, "floor": floor, "floor_name": floor_name,
            "n_new": sum(1 for x in out if x["new"])}


# ── ratings changes ──────────────────────────────────────────────────────────

def ratings_changes(conn, team_id, baseline=None, scope="org", min_change=5):
    dates = [r[0] for r in conn.execute(
        "SELECT DISTINCT snapshot_date FROM ratings_history ORDER BY snapshot_date DESC").fetchall()]
    cur_date = conn.execute("SELECT MAX(snapshot_date) FROM ratings").fetchone()[0]
    choices = [d for d in dates if d < cur_date] if cur_date else dates
    if not choices:
        return {"rows": [], "dates": [], "baseline": None, "current": cur_date}
    base = baseline if baseline in choices else choices[0]
    where = {"org": "(p.team_id=? OR p.parent_team_id=?)",
             "mlb": "p.level='1'",
             "league": "COALESCE(p.retired,0)=0"}[scope if scope in ("org", "mlb", "league") else "org"]
    params = (team_id, team_id) if scope == "org" else ()
    cols = sorted({c for c, *_ in HIT_TOOLS + PIT_TOOLS} | {c for c, _ in POT_HIT + POT_PIT})
    sel_now = ", ".join(f"r.{c} AS n_{c}" for c in cols)
    sel_old = ", ".join(f"h.{c} AS o_{c}" for c in cols)
    names = get_cfg().team_abbr_map or {}
    rows = conn.execute(f"""
        SELECT p.player_id, p.name, p.age, p.role, p.pos, p.level,
               COALESCE(NULLIF(p.parent_team_id,0), p.team_id) AS org,
               r.composite_score AS comp, r.true_ceiling AS ceil, pf.fv,
               COALESCE(ps.surplus, pf.prospect_surplus) AS surplus,
               {sel_now}, {sel_old}
        FROM players p JOIN latest_ratings r ON r.player_id=p.player_id
        JOIN ratings_history h ON h.player_id=p.player_id AND h.snapshot_date=?
        LEFT JOIN player_surplus ps ON ps.player_id=p.player_id AND ps.eval_date=(SELECT MAX(eval_date) FROM player_surplus)
        LEFT JOIN prospect_fv pf ON pf.player_id=p.player_id AND pf.eval_date=(SELECT MAX(eval_date) FROM prospect_fv)
        WHERE {where}
    """, (base, *params)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        pitcher = d["role"] in (11, 12, 13)
        tools = PIT_TOOLS if pitcher else HIT_TOOLS
        pots = POT_PIT if pitcher else POT_HIT
        changes, score = [], 0.0
        for c, label, w in tools:
            a, b = d.get(f"o_{c}"), d.get(f"n_{c}")
            if a is not None and b is not None and abs(b - a) >= min_change:
                changes.append({"tool": label, "from": a, "to": b, "d": b - a, "pot": False})
                score += w * (b - a)
        for c, label in pots:
            a, b = d.get(f"o_{c}"), d.get(f"n_{c}")
            if a is not None and b is not None and abs(b - a) >= min_change:
                changes.append({"tool": label, "from": a, "to": b, "d": b - a, "pot": True})
                score += 0.5 * (b - a)
        if not changes:
            continue
        out.append({"pid": d["player_id"], "name": d["name"], "age": d["age"], "level": d["level"],
                    "team": names.get(d["org"], ""), "pitcher": pitcher, "comp": d["comp"], "ceil": d["ceil"],
                    "fv": d["fv"], "surplus": d["surplus"], "changes": changes, "score": round(score, 1)})
    out.sort(key=lambda x: -abs(x["score"]))
    return {"rows": out, "dates": choices, "baseline": base, "current": cur_date,
            "risers": sum(1 for x in out if x["score"] > 0), "fallers": sum(1 for x in out if x["score"] < 0)}


def alert_count(team_id, league_dir):
    from statsplusplus.evaluation.constants import load_model_weights
    conn = get_db()
    weights = load_model_weights(Path(league_dir))
    return pickups(conn, team_id, league_dir, weights)["n_new"]
