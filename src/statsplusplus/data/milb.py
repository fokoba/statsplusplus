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


def compute_milb_averages(conn, league_levels: dict[int, int]) -> dict[int, dict[str, Any]]:
    """Season-matched, opportunity-weighted league context, using every stint.

    Ratios of pooled counting stats avoid overweighting low-volume players.
    Sacrifice bunts are PA but not OBP opportunities. Outs are the authoritative
    pitching denominator (never baseball-decimal innings).
    """
    result = {lid: {"level": level, "seasons": {}} for lid, level in league_levels.items()}
    for r in conn.execute("""
        SELECT league_id, year, SUM(ab) ab, SUM(pa) pa, SUM(h) h,
               SUM(d) d, SUM(t) t, SUM(hr) hr, SUM(bb) bb, SUM(hbp) hbp, SUM(sf) sf
        FROM batting_stats WHERE league_id IS NOT NULL AND split_id=1
        GROUP BY league_id, year
    """):
        lid = r["league_id"]
        if lid not in result:
            continue
        ab, bb, hbp, sf = (r[k] or 0 for k in ("ab", "bb", "hbp", "sf"))
        denom = ab + bb + hbp + sf
        if ab <= 0 or denom <= 0:
            continue
        year = str(r["year"])
        result[lid]["seasons"].setdefault(year, {})["batting_avg"] = {
            "obp": ((r["h"] or 0) + bb + hbp) / denom,
            "slg": ((r["h"] or 0) + (r["d"] or 0) + 2*(r["t"] or 0) + 3*(r["hr"] or 0)) / ab,
            "pa_pool": r["pa"] or 0, "obp_opportunities": denom, "year": r["year"],
        }
    for r in conn.execute("""
        SELECT league_id, year, SUM(outs) outs, SUM(er) er
        FROM pitching_stats WHERE league_id IS NOT NULL AND split_id=1
        GROUP BY league_id, year
    """):
        lid = r["league_id"]
        if lid not in result or (r["outs"] or 0) <= 0:
            continue
        result[lid]["seasons"].setdefault(str(r["year"]), {})["pitching_avg"] = {
            "era": (r["er"] or 0) * 27.0 / r["outs"],
            "ip_pool": r["outs"] / 3.0, "year": r["year"],
        }
    return result


def completed_season_signal(seasons: list[dict], prediction_year: int,
                            is_pitcher: bool, config: dict) -> dict[str, float]:
    """Shrink a completed-season production signal; never use future seasons.

    Level factors discount sample reliability, not the normalized rate itself.
    This matches the dated calibration inputs and leaves ceilings untouched.
    """
    weights = config.get("level_weights", {})
    n_years = config.get("seasons", 3)
    recency = config.get("recency", .7)
    prior = config.get("prior_opportunities", 200.0)
    weighted, effective = 0.0, 0.0
    for s in seasons:
        lag = prediction_year - int(s["year"])
        if not 1 <= lag <= n_years:
            continue
        sample = (s.get("ip", 0) * 4.3) if is_pitcher else s.get("pa", 0)
        w = sample * float(weights.get(str(s["level"]), 0)) * recency ** (lag - 1)
        weighted += s["stat_2080"] * w
        effective += w
    score = weighted / effective if effective > 0 else 50.0
    signal = (score - 50.0) / 15.0 * effective / (effective + prior) if effective > 0 else 0.0
    return {"signal": signal, "effective_opportunities": effective, "stat_2080": score}


def load_milb_averages(league_dir: Path) -> dict[int, dict[str, Any]]:
    """Load MiLB league averages from league_settings.json.

    Returns dict mapping league_id → {batting_avg: {...}, pitching_avg: {...}, level: int}.
    """
    settings_path = league_dir / "config" / "league_settings.json"
    if not settings_path.exists():
        return {}
    try:
        s = json.loads(settings_path.read_text())
        # A dedicated artifact enables the audited prospect-only context. Build
        # it from current counting stats on each batch load so a later refresh
        # cannot silently erase averages or leave an old season's pool in use.
        # The evaluation engine's separate legacy context is not enabled here.
        if (league_dir / "config" / "milb_averages.json").exists():
            levels = {int(ml["lid"]): int(ml["level"]) for ml in s.get("minor_leagues", [])}
            path = league_dir / "league.db"
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
            try:
                return compute_milb_averages(conn, levels)
            finally:
                conn.close()
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
            HAVING SUM(outs) >= 60
            ORDER BY year DESC
        """, (player_id,)).fetchall()
    else:
        rows = conn.execute("""
            SELECT year, league_id, SUM(pa) as pa, SUM(ab) as ab,
                   SUM(h) as h, SUM(d) as d, SUM(t) as t, SUM(hr) as hr,
                   SUM(bb) as bb, SUM(hbp) as hbp, SUM(sf) as sf
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
        # Context must come from the SAME season as the player's performance.
        # A missing historical pool is unknown, not permission to borrow a
        # later league environment.
        if "seasons" in lg_info:
            context = lg_info["seasons"].get(str(row["year"]))
            if not context:
                continue
        else:
            context = lg_info

        if is_pitcher:
            lg_era = (context.get("pitching_avg") or {}).get("era")
            if not lg_era or lg_era <= 0:
                continue
            outs = row["outs"] or 0
            er = row["er"] or 0
            ip = outs / 3.0
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
            lg_obp = (context.get("batting_avg") or {}).get("obp")
            lg_slg = (context.get("batting_avg") or {}).get("slg")
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
            obp_denom = ab + bb + hbp + (row["sf"] or 0)
            if obp_denom <= 0:
                continue
            obp = (h + bb + hbp) / obp_denom
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
