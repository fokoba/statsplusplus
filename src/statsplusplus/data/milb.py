"""MiLB stat data loading for prospect evaluation.

Pure data-access functions that load and normalize MiLB stat seasons
for use in performance-adjusted ceiling and stat risk calculations.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from statsplusplus.evaluation.composite import pitcher_stat_to_2080, stat_to_2080


def load_milb_averages(league_dir: Path) -> dict[int, dict[str, Any]]:
    """Load MiLB league averages from league_settings.json.

    Returns dict mapping league_id → {batting_avg: {...}, pitching_avg: {...}, level: int}.
    """
    settings_path = league_dir / "config" / "league_settings.json"
    if not settings_path.exists():
        return {}
    try:
        s = json.loads(settings_path.read_text())
        result = {}
        for ml in s.get("minor_leagues", []):
            result[ml["lid"]] = {
                "batting_avg": ml.get("batting_avg"),
                "pitching_avg": ml.get("pitching_avg"),
                "level": ml.get("level", 0),
            }
        return result
    except (json.JSONDecodeError, OSError):
        return {}


def load_milb_stat_seasons(
    conn: sqlite3.Connection,
    player_id: int,
    is_pitcher: bool,
    milb_averages: dict[int, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Load MiLB stat seasons for a player, normalized to level-relative OPS+/ERA-.

    Args:
        conn: DB connection.
        player_id: Player ID.
        is_pitcher: Whether to load pitching or batting stats.
        milb_averages: Dict mapping league_id → {batting_avg: {obp, slg}, pitching_avg: {era}}.

    Returns:
        List of dicts, each containing:
            year: Season year
            league_id: The MiLB league ID
            level: Integer level (2=AAA, 3=AA, 4=A, 6=Rookie)
            pa/ip: Sample size
            stat_2080: The production value converted to 20-80 scale
    """
    if is_pitcher:
        rows = conn.execute("""
            SELECT year, league_id, SUM(ip) as ip, SUM(er) as er, SUM(outs) as outs,
                   SUM(k) as k, SUM(bb) as bb, SUM(hra) as hra, SUM(g) as g, SUM(gs) as gs
            FROM pitching_stats
            WHERE player_id = ? AND split_id = 1 AND league_id IS NOT NULL
            GROUP BY year, league_id
            HAVING SUM(ip) >= 20
            ORDER BY year DESC
        """, (player_id,)).fetchall()
    else:
        rows = conn.execute("""
            SELECT year, league_id, SUM(pa) as pa, SUM(ab) as ab,
                   SUM(h) as h, SUM(d) as d, SUM(t) as t, SUM(hr) as hr,
                   SUM(bb) as bb, SUM(hbp) as hbp
            FROM batting_stats
            WHERE player_id = ? AND split_id = 1 AND league_id IS NOT NULL
            GROUP BY year, league_id
            HAVING SUM(pa) >= 50
            ORDER BY year DESC
        """, (player_id,)).fetchall()

    results = []
    for row in rows:
        lid = row["league_id"]
        lg_info = milb_averages.get(lid)
        if not lg_info:
            continue

        if is_pitcher:
            lg_era = (lg_info.get("pitching_avg") or {}).get("era")
            if not lg_era or lg_era <= 0:
                continue
            outs = row["outs"] or 0
            er = row["er"] or 0
            ip = row["ip"] or 0
            if outs <= 0:
                continue
            era = er * 27.0 / outs
            era_minus = (era / lg_era) * 100.0
            stat_plus = 200.0 - era_minus
            s2080 = pitcher_stat_to_2080(stat_plus)
            results.append({
                "year": row["year"],
                "league_id": lid,
                "level": lg_info.get("level", 0),
                "era_minus_inv": stat_plus,
                "ip": ip,
                "stat_2080": s2080,
            })
        else:
            lg_obp = (lg_info.get("batting_avg") or {}).get("obp")
            lg_slg = (lg_info.get("batting_avg") or {}).get("slg")
            if not lg_obp or not lg_slg or lg_obp <= 0 or lg_slg <= 0:
                continue
            pa = row["pa"] or 0
            ab = row["ab"] or 0
            h = row["h"] or 0
            bb = row["bb"] or 0
            hbp = row["hbp"] or 0
            d = row["d"] or 0
            t = row["t"] or 0
            hr = row["hr"] or 0
            if pa <= 0 or ab <= 0:
                continue
            obp = (h + bb + hbp) / pa
            slg = (h + d + 2 * t + 3 * hr) / ab
            ops_plus = 100.0 * (obp / lg_obp + slg / lg_slg - 1.0)
            s2080 = stat_to_2080(ops_plus)
            results.append({
                "year": row["year"],
                "league_id": lid,
                "level": lg_info.get("level", 0),
                "ops_plus": ops_plus,
                "pa": pa,
                "stat_2080": s2080,
            })

    return results
