"""Projected-Role Surplus: Standard vs. role-based surplus for the Athletics.

Seeded from the 1956 spot check (web/role_surplus_1956.json, roster snapshot
Dec 25, 1955). Standard uses Dominator's WAR model; Projected Role uses the
1955-anchored roster review at the planned workload/handedness. Same salary
(including retention) for both, so the gap is WAR only.
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


def _summarise(p: dict) -> dict:
    yrs = p["years"]
    first, second = yrs[0], yrs[1] if len(yrs) > 1 else None
    return {
        **p,
        "horizons": {
            "y1": {"std": first["std_surplus"], "role": first["role_surplus"]},
            "y2": {"std": second["std_surplus"], "role": second["role_surplus"]} if second else None,
            "y3": {"std": sum(y["std_surplus"] for y in yrs),
                   "role": sum(y["role_surplus"] for y in yrs)},
        },
    }


def meta() -> dict:
    return _load().get("meta", {})


def for_player(pid: int, team_id: int | None = None) -> dict | None:
    data = _load()
    if team_id is not None and data.get("meta", {}).get("team_id") not in (None, team_id):
        return None
    p = data.get("players", {}).get(str(pid))
    return _summarise(p) if p else None


def roster(team_id: int) -> dict | None:
    data = _load()
    if data.get("meta", {}).get("team_id") != team_id or not data.get("players"):
        return None
    rows = [{"pid": int(pid), **_summarise(p)} for pid, p in data["players"].items()]
    rows.sort(key=lambda r: -r["horizons"]["y1"]["role"])
    return {"meta": data["meta"], "rows": rows}
