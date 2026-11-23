"""Trade Winners & Losers: every logged trade scored by current surplus.

Each side's haul = long-term surplus (today's model value) of every player it
received, plus cash received. Hindsight scoring: a player who has since
retired or been released counts $0; one since flipped elsewhere still counts
at his current value.
"""

from __future__ import annotations

import os

import acquisition
from web_league_context import get_db, get_cfg, team_abbr_map, team_names_map


def _surplus_map(conn) -> dict[int, float]:
    out = {}
    ed = conn.execute("SELECT MAX(eval_date) FROM player_surplus").fetchone()[0]
    if ed:
        out.update(conn.execute(
            "SELECT player_id, surplus FROM player_surplus WHERE eval_date=?", (ed,)).fetchall())
    ed = conn.execute("SELECT MAX(eval_date) FROM prospect_fv").fetchone()[0]
    if ed:  # prospects (incl. rookie-eligible MLB) use prospect surplus, like the profile
        out.update(conn.execute(
            "SELECT player_id, prospect_surplus FROM prospect_fv WHERE eval_date=?", (ed,)).fetchall())
    return {int(k): float(v or 0) for k, v in out.items()}


def verdict(a_val: float, b_val: float) -> tuple[str, str]:
    """(level, side): level in heavy/favors/neutral, side 'a'/'b'/None."""
    diff = a_val - b_val
    scale = abs(a_val) + abs(b_val)
    if abs(diff) < max(50_000, 0.2 * scale):
        return "neutral", None
    side = "a" if diff > 0 else "b"
    if abs(diff) >= max(250_000, 0.6 * scale):
        return "heavy", side
    return "favors", side


def build(team_filter: int | None = None) -> dict:
    conn = get_db()
    slug = os.path.basename(str(get_cfg().league_dir)).lower()
    sur = _surplus_map(conn)
    status = {pid: (ret, fa, org) for pid, ret, fa, org in conn.execute(
        "SELECT player_id, COALESCE(retired,0), COALESCE(free_agent,0), "
        "COALESCE(NULLIF(parent_team_id,0), team_id) FROM players")}
    abbr, names = team_abbr_map(), team_names_map()

    def side(pids, cash, nm):
        players = []
        for p in pids:
            ret, fa, org = status.get(p, (1, 0, 0))
            gone = "retired" if ret else "FA" if fa or not org else None
            v = 0.0 if gone else sur.get(p, 0.0)
            players.append({"pid": p, "name": nm.get(p, str(p)), "surplus": v,
                            "gone": gone, "now": None if gone else abbr.get(org, "")})
        return players, sum(x["surplus"] for x in players) + cash

    rows, tally = [], {}
    for t in acquisition.trades(slug):
        if team_filter and team_filter not in (t["a"], t["b"]):
            continue
        # A received what B sent, and vice versa.
        a_got, a_val = side(t["b_sent"], t["b_cash"], t["names"])
        b_got, b_val = side(t["a_sent"], t["a_cash"], t["names"])
        level, s = verdict(a_val, b_val)
        winner = t["a"] if s == "a" else t["b"] if s == "b" else None
        for team, val, opp in ((t["a"], a_val, b_val), (t["b"], b_val, a_val)):
            r = tally.setdefault(team, {"tid": team, "team": abbr.get(team, str(team)),
                                        "n": 0, "net": 0.0, "won": 0, "lost": 0})
            r["n"] += 1
            r["net"] += val - opp
            if winner == team:
                r["won"] += 1
            elif winner:
                r["lost"] += 1
        rows.append({
            "date": t["date"].isoformat(), "month": t["date"].strftime("%b %d, %Y"),
            "a": t["a"], "b": t["b"], "a_abbr": abbr.get(t["a"], t["a"]), "b_abbr": abbr.get(t["b"], t["b"]),
            "a_name": names.get(t["a"], ""), "b_name": names.get(t["b"], ""),
            "a_got": a_got, "a_cash": t["b_cash"], "a_val": a_val,
            "b_got": b_got, "b_cash": t["a_cash"], "b_val": b_val,
            "level": level, "winner": winner, "winner_abbr": abbr.get(winner, "") if winner else "",
            "margin": abs(a_val - b_val),
        })
    teams = sorted(tally.values(), key=lambda r: -r["net"])
    return {"rows": rows, "teams": teams}
