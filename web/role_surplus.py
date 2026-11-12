"""Projected-Role Surplus: Standard vs. role-based surplus for the Athletics.

Live inputs (recomputed on every WAR Planner preview/save):
  * Role WAR, first season — the WAR Planner's own Total for that player at
    his planned AB / vR share / IP (war_projection_queries.build_projection).
  * Later seasons — that figure scaled by the same year-over-year trajectory
    the contract engine uses for the player's Standard WAR (PPL aging curves,
    scripts/contract_value.py), so workload and handedness stay constant.
  * Standard WAR and market value — scripts/contract_value.contract_value(),
    rows 0..2, aligned to the 1956 contract year.
  * $ per WAR × positional scarcity — same as the engine
    (league_averages.json dollar_per_war; constants.MLB_SCARCITY).

Fixed inputs (web/role_surplus_1956.json, the Dec 25 1955 spot check):
  * Salary schedule per season: signed / arb estimate / option, gross,
    retention %, net Philadelphia salary. Identical for both columns.
  * Players outside the planner's 25 (Stancliff, Emanuel): their role WAR
    stays at the spot check's pre-cutdown scenario.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

_SEED = Path(__file__).with_name("role_surplus_1956.json")


@lru_cache(maxsize=1)
def _load() -> dict:
    try:
        return json.loads(_SEED.read_text())
    except (OSError, json.JSONDecodeError):
        return {"meta": {}, "players": {}}


def meta() -> dict:
    return _load().get("meta", {})


def _workload(row: dict) -> str:
    if row.get("ip"):
        return f"{row['ip']:g} IP"
    return f"{row['ab']:g} AB · {round(row['vr_share'] or 0)}% vR"


def _engine(pids, league_dir):
    """Standard rows from the contract engine, one stat-history load."""
    try:
        import contract_value as cv
        from statsplusplus.evaluation.constants import MLB_SCARCITY
        from statsplusplus.data.db import get_connection
        cv._ensure_league_context(league_dir)
        conn = get_connection(league_dir)
        game_date = cv._get_state()["game_date"]
        hist = cv.load_stat_history(conn, game_date)
        dpw = cv.dollars_per_war()
        out = {}
        for pid in pids:
            r = cv.contract_value(pid, _conn=conn, _hist=hist, league_dir=league_dir)
            if r and r.get("breakdown"):
                out[pid] = (r["breakdown"], MLB_SCARCITY.get(r["bucket"], 1.0))
        return out, dpw
    except Exception:
        return {}, None


def build(team_id: int, planner_rows: list[dict], league_dir) -> dict | None:
    data = _load()
    if data.get("meta", {}).get("team_id") != team_id or not data.get("players"):
        return None
    plan = {r["pid"]: r for r in planner_rows}
    seed = {int(k): v for k, v in data["players"].items()}
    engine, dpw = _engine(list(seed), Path(league_dir))

    rows = []
    for pid, s in seed.items():
        p_row = plan.get(pid)
        eng = engine.get(pid)
        years = []
        role0 = p_row["total"] if p_row else None
        for k, sy in enumerate(s["years"]):
            net = sy["net"]
            y = dict(sy)
            if eng and dpw and k < len(eng[0]):
                b, scarcity = eng[0][k], eng[1]
                y["std_war"] = b["war_base"]
                y["std_surplus"] = round(b["market_value"] - net)
                if role0 is not None:
                    w0 = eng[0][0]["war_base"]
                    ratio = (b["war_base"] / w0) if w0 > 0 else 1.0
                    y["role_war"] = round(role0 * ratio, 2)
                    y["role_surplus"] = round(max(y["role_war"], 0) * dpw * scarcity - net)
            elif eng and dpw and role0 is not None and s["years"][0]["std_war"] > 0:
                # Engine's control window ends earlier than the spot check's
                # salary schedule: keep the seed's Standard row and scale
                # role WAR by the seed's own aging trajectory.
                ratio = sy["std_war"] / s["years"][0]["std_war"]
                y["role_war"] = round(role0 * ratio, 2)
                y["role_surplus"] = round(max(y["role_war"], 0) * dpw * eng[1] - net)
            years.append(y)
        live = bool(p_row and eng and dpw)
        row = {
            "pid": pid, "name": s["name"],
            "role": (p_row or {}).get("role") or s["role"],
            "workload": _workload(p_row) if p_row else s["workload"],
            "scenario_note": s.get("scenario_note") if not p_row else None,
            "live": live, "years": years,
            "horizons": {
                "y1": {"std": years[0]["std_surplus"], "role": years[0]["role_surplus"]},
                "y2": {"std": years[1]["std_surplus"], "role": years[1]["role_surplus"]} if len(years) > 1 else None,
                "y3": {"std": sum(y["std_surplus"] for y in years),
                       "role": sum(y["role_surplus"] for y in years)},
            },
        }
        rows.append(row)
    rows.sort(key=lambda r: -r["horizons"]["y1"]["role"])
    return {"meta": data["meta"], "rows": rows}


def for_player(pid: int, team_id: int | None = None) -> dict | None:
    """One player's entry, built from the saved WAR Planner plan."""
    if str(pid) not in _load().get("players", {}):
        return None
    if team_id is not None and _load()["meta"].get("team_id") != team_id:
        return None
    from web_league_context import get_cfg
    from war_projection_queries import build_projection, load_overrides
    league_dir = get_cfg().league_dir
    proj = build_projection(team_id, load_overrides(league_dir))
    for r in (proj.get("role_surplus") or {}).get("rows", []):
        if r["pid"] == pid:
            return r
    return None
