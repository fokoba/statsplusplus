"""Player detail query — builds the full player dict for the player page."""

import os, sys, json

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _fmt_money_py(val):
    """Python-side money formatter for discount_note strings."""
    if val is None:
        return "—"
    if abs(val) >= 1_000_000:
        return f"${val / 1e6:.1f}M"
    if abs(val) >= 1_000:
        return f"${val / 1e3:.0f}K"
    return f"${val:,.0f}"
sys.path.insert(0, os.path.join(BASE, "scripts"))
from statsplusplus.config.ratings import norm as _norm_raw, norm_floor as _norm_floor_raw
from statsplusplus.utils.formatting import height_str as _height_str
from statsplusplus.utils.positions import display_pos as _display_pos
from statsplusplus.evaluation.surplus import calc_pap
from statsplusplus.config.league_config import dollars_per_war as _dpw_pkg
from statsplusplus.utils.positions import ROLE_MAP
from percentiles import get_hitter_percentiles, get_pitcher_percentiles, get_fielding_percentiles, available_pctile_years, available_pctile_levels, get_percentile_history, get_percentile_history_all_levels, get_fielding_percentile_history
from web_league_context import (get_db, get_cfg, team_abbr_map, team_names_map, level_map, pos_map,
                                 money_divisor as _money_divisor)

def _norm(val):
    return _norm_raw(val, get_cfg().ratings_scale)

def _norm_floor(val, floor=20):
    return _norm_floor_raw(val, get_cfg().ratings_scale, floor)

def _dollars_per_war():
    return _dpw_pkg(get_cfg().league_dir)


# ---------------------------------------------------------------------------
# MLB context: percentile + tier for composite/ceiling vs MLB at position
# ---------------------------------------------------------------------------

def _mlb_context(conn, bucket, composite, ceiling):
    """Compute MLB rank and tier label for composite and ceiling.

    Ranks the player's composite/ceiling against MLB peers at the same
    position bucket. Composite and ceiling are ratings-based values, so the
    peer pool is NOT gated by a playing-time threshold — the pool is every MLB
    player who has appeared this season at the bucket. (An earlier design gated
    the pool by a full-season IP/PA floor, which collapsed the peer group to a
    handful of players in the season's first weeks, e.g. "#5 of 11 RPs".)

    Pitchers are split into SP/RP by usage (games-started ratio), not raw role
    codes, consistent with the league positional-rankings page — role codes are
    unreliable across leagues.
    """
    from statsplusplus.utils.positions import assign_bucket as _ab
    from statsplusplus.config.league_config import LeagueConfig; _lc = LeagueConfig()
    _rm = {str(k): v for k, v in _lc.role_map.items()}

    _year = conn.execute("SELECT MAX(year) FROM mlb_batting_stats").fetchone()[0]
    if not _year:
        return None

    if bucket in ("SP", "RP"):
        # All MLB pitchers who have appeared this season, with usage split.
        rows = conn.execute("""
            SELECT r.composite_score, p.role, ps.gs, ps.g
            FROM latest_ratings r
            JOIN players p ON r.player_id = p.player_id
            JOIN mlb_pitching_stats ps ON ps.player_id = p.player_id AND ps.split_id = 1
            WHERE p.level = 1 AND r.composite_score IS NOT NULL
              AND ps.year = ? AND ps.g > 0
        """, (_year,)).fetchall()
        # Fall back to prior year if the season hasn't started (spring training).
        if not rows:
            rows = conn.execute("""
                SELECT r.composite_score, p.role, ps.gs, ps.g
                FROM latest_ratings r
                JOIN players p ON r.player_id = p.player_id
                JOIN mlb_pitching_stats ps ON ps.player_id = p.player_id AND ps.split_id = 1
                WHERE p.level = 1 AND r.composite_score IS NOT NULL
                  AND ps.year = ? AND ps.g > 0
            """, (_year - 1,)).fetchall()
        vals = []
        for r in rows:
            g = r["g"] or 0
            gs = r["gs"] or 0
            # SP = starts the majority of appearances; ratio only, no GS floor.
            is_sp = (gs > 0 and gs / g > 0.5) if g > 0 else (r["role"] == 11)
            if (bucket == "SP") == is_sp:
                vals.append(r["composite_score"])
    else:
        # All MLB position players who have appeared this season, bucketed.
        rows = conn.execute("""
            SELECT r.composite_score, p.pos, p.role
            FROM latest_ratings r
            JOIN players p ON r.player_id = p.player_id
            JOIN mlb_batting_stats bs ON bs.player_id = p.player_id AND bs.split_id = 1
            WHERE p.level = 1 AND r.composite_score IS NOT NULL
              AND p.role NOT IN ('11','12','13')
              AND bs.year = ? AND bs.pa > 0
        """, (_year,)).fetchall()
        vals = []
        for r in rows:
            p = {"Pos": str(r["pos"] or ""), "_role": _rm.get(str(r["role"] or 0), "position_player")}
            p["_is_pitcher"] = False
            try:
                b = _ab(p, use_pot=False)
            except Exception:
                continue
            if b == bucket:
                vals.append(r["composite_score"])

    if len(vals) < 5:
        return None

    vals_sorted = sorted(vals)
    n = len(vals_sorted)
    median = vals_sorted[n // 2]

    # Rank: 1 = best. Count how many are strictly above this score + 1.
    def rank(score):
        return min(n, sum(1 for v in vals_sorted if v > score) + 1)

    def tier(score, med):
        diff = score - med
        if diff >= 8: return "Elite"
        if diff >= 4: return "Plus"
        if diff >= -3: return "Average"
        if diff >= -7: return "Below Avg"
        return "Fringe"

    comp_rank = rank(composite) if composite else None
    ceil_rank = rank(ceiling) if ceiling else None

    return {
        "comp_rank": comp_rank,
        "ceil_rank": ceil_rank,
        "comp_tier": tier(composite, median) if composite else None,
        "ceil_tier": tier(ceiling, median) if ceiling else None,
        "n": n,
        "median": median,
    }


# ---------------------------------------------------------------------------
# Evaluation data helper — extracts composite scores, divergence, archetype,
# carrying/red-flag tools, and two-way info from a ratings row.
# ---------------------------------------------------------------------------

def _build_evaluation_data(rd: dict | None, is_pitcher: bool, norm_fn,
                           position_bucket: str | None = None,
                           league_dir=None) -> dict:
    """Build evaluation engine data from a ratings row dict.

    Extracts composite scores, runs divergence detection, classifies archetype,
    identifies carrying/red-flag tools, computes two-way combined value, and
    enriches with positional context (carrying tool bonus, positional percentile).

    Args:
        rd: Ratings row as a dict (or None).
        is_pitcher: Whether the player is a pitcher.
        norm_fn: Normalization function for raw tool grades.
        position_bucket: Position bucket string (e.g. "SS", "C", "CF") for
            carrying tool bonus computation.  When ``None``, positional context
            is skipped.
        league_dir: Path to the league data directory.  Used to load the
            carrying tool config.  When ``None``, the default config is used.

    Returns a dict with keys: composite_score, ceiling_score, tool_only_score,
    secondary_composite, divergence, ceiling_divergence, archetype,
    carrying_tools, red_flag_tools, two_way_scores, carrying_tool_bonus,
    carrying_tool_breakdown, positional_percentile, positional_median.
    """
    result = {
        "composite_score": None, "ceiling_score": None,
        "tool_only_score": None, "secondary_composite": None,
        "divergence": None, "ceiling_divergence": None,
        "archetype": None, "carrying_tools": [], "red_flag_tools": [],
        "two_way_scores": None,
        "offensive_grade": None, "baserunning_value": None,
        "defensive_value": None, "durability_score": None,
        "offensive_ceiling": None,
        "carrying_tool_bonus": 0.0, "carrying_tool_breakdown": [],
        "positional_percentile": None, "positional_median": None,
    }
    if not rd:
        return result

    composite_score = rd.get("composite_score")
    ceiling_score = rd.get("ceiling_score")
    tool_only_score = rd.get("tool_only_score")
    secondary_composite = rd.get("secondary_composite")

    result["composite_score"] = composite_score
    result["ceiling_score"] = ceiling_score
    result["true_ceiling"] = rd.get("true_ceiling")
    result["tool_only_score"] = tool_only_score
    result["secondary_composite"] = secondary_composite

    # Component scores
    result["offensive_grade"] = rd.get("offensive_grade")
    result["baserunning_value"] = rd.get("baserunning_value")
    result["defensive_value"] = rd.get("defensive_value")
    result["durability_score"] = rd.get("durability_score")
    result["offensive_ceiling"] = rd.get("offensive_ceiling")

    # Positional context: percentile and median from stored ratings columns
    result["positional_percentile"] = rd.get("positional_percentile")
    result["positional_median"] = rd.get("positional_median")

    # Carrying tool bonus: compute on-the-fly for hitters with offensive grade
    if not is_pitcher and position_bucket and result["offensive_grade"] is not None:
        try:
            from pathlib import Path as _Path
            from statsplusplus.data.evaluation_engine import compute_carrying_tool_bonus, load_carrying_tool_config
            # Map display position back to internal bucket for config lookup
            _internal_bucket = "COF" if position_bucket == "OF" else position_bucket
            _ct_config = load_carrying_tool_config(_Path(league_dir)) if league_dir else load_carrying_tool_config(_Path("."))
            _hitter_tools = {
                "contact": norm_fn(rd.get("cntct")),
                "gap": norm_fn(rd.get("gap")),
                "power": norm_fn(rd.get("pow")),
                "eye": norm_fn(rd.get("eye")),
            }
            _ct_bonus, _ct_breakdown = compute_carrying_tool_bonus(
                _hitter_tools, _internal_bucket, _ct_config
            )
            result["carrying_tool_bonus"] = _ct_bonus
            result["carrying_tool_breakdown"] = _ct_breakdown
        except Exception:
            pass

    # Divergence detection (tool_only vs OVR, ceiling vs POT)
    try:
        from statsplusplus.data.evaluation_engine import detect_divergence
        _ovr = rd.get("ovr")
        _pot = rd.get("pot")
        if tool_only_score is not None and _ovr is not None:
            components = {
                "offensive_grade": result["offensive_grade"],
                "baserunning_value": result["baserunning_value"],
                "defensive_value": result["defensive_value"],
            }
            result["divergence"] = detect_divergence(tool_only_score, _ovr, components=components)
        if ceiling_score is not None and _pot is not None:
            result["ceiling_divergence"] = detect_divergence(ceiling_score, _pot)
    except Exception:
        pass

    # Tool profile analysis: archetype, carrying tools, red-flag tools
    if composite_score is not None:
        try:
            from statsplusplus.data.evaluation_engine import classify_archetype, identify_carrying_tools, identify_red_flag_tools
            if is_pitcher:
                _tools = {
                    "stuff": norm_fn(rd.get("stf")),
                    "movement": norm_fn(rd.get("mov")),
                    "control": norm_fn(rd.get("ctrl") or (
                        round(((rd.get("ctrl_r", 0) or 0) + (rd.get("ctrl_l", 0) or 0)) / 2)
                        if rd.get("ctrl_r") and rd.get("ctrl_l")
                        else rd.get("ctrl_r") or rd.get("ctrl_l") or 0
                    )),
                }
                _arsenal = {}
                for col, label in [("fst", "Fastball"), ("snk", "Sinker"), ("crv", "Curveball"),
                                    ("sld", "Slider"), ("chg", "Changeup"), ("splt", "Splitter"),
                                    ("cutt", "Cutter"), ("cir_chg", "Circle Change")]:
                    v = norm_fn(rd.get(col))
                    if v and v >= 20:
                        _arsenal[label] = v
                result["archetype"] = classify_archetype(_tools, composite_score, is_pitcher=True, arsenal=_arsenal)
                result["carrying_tools"] = identify_carrying_tools(_tools, composite_score)
                result["red_flag_tools"] = identify_red_flag_tools(_tools, composite_score)
            else:
                _tools = {
                    "contact": norm_fn(rd.get("cntct")),
                    "gap": norm_fn(rd.get("gap")),
                    "power": norm_fn(rd.get("pow")),
                    "eye": norm_fn(rd.get("eye")),
                    "avoid_k": norm_fn(rd.get("ks")),
                    "speed": norm_fn(rd.get("speed")),
                }
                result["archetype"] = classify_archetype(_tools, composite_score, is_pitcher=False)
                result["carrying_tools"] = identify_carrying_tools(_tools, composite_score)
                result["red_flag_tools"] = identify_red_flag_tools(_tools, composite_score)
        except Exception:
            pass

    # Two-way player: include both role scores and combined value
    if secondary_composite is not None and composite_score is not None:
        try:
            from statsplusplus.data.evaluation_engine import compute_combined_value
            combined = compute_combined_value(composite_score, secondary_composite)
            if is_pitcher:
                result["two_way_scores"] = {
                    "pitcher_composite": composite_score,
                    "hitter_composite": secondary_composite,
                    "combined_value": combined,
                }
            else:
                result["two_way_scores"] = {
                    "hitter_composite": composite_score,
                    "pitcher_composite": secondary_composite,
                    "combined_value": combined,
                }
        except Exception:
            pass

    return result


# ---------------------------------------------------------------------------
# Insights — conditional, actionable observations about a player
# ---------------------------------------------------------------------------

_PLATOON_GAP_THRESHOLD = 15  # min gap between L/R split (on 20-80 scale) to flag


def _compute_insights(rd: dict | None, is_pitcher: bool, composite: int | None,
                      norm_fn) -> list[dict]:
    """Generate conditional insights for the player evaluation panel.

    Each insight is a dict with:
        - icon: emoji/symbol
        - text: short description
        - tone: "positive", "negative", or "neutral"

    Only returns insights when the data strongly supports them.
    """
    if not rd or composite is None:
        return []

    insights = []

    # ── Platoon candidate ──
    if not is_pitcher:
        splits = [
            ("contact", "cntct_l", "cntct_r"),
            ("power", "pow_l", "pow_r"),
            ("gap", "gap_l", "gap_r"),
            ("eye", "eye_l", "eye_r"),
        ]
        strong_side = None
        max_gap = 0
        for tool, l_col, r_col in splits:
            lv = norm_fn(rd.get(l_col))
            rv = norm_fn(rd.get(r_col))
            if lv is not None and rv is not None:
                gap = abs(lv - rv)
                if gap > max_gap:
                    max_gap = gap
                    strong_side = "vs RHP" if rv > lv else "vs LHP"

        if max_gap >= _PLATOON_GAP_THRESHOLD:
            insights.append({
                "icon": "⚔️", "tone": "neutral",
                "text": f"Platoon candidate — significantly better {strong_side}",
            })
    else:
        # Pitcher platoon: stuff L/R split
        stf_l = norm_fn(rd.get("stf_l"))
        stf_r = norm_fn(rd.get("stf_r"))
        if stf_l is not None and stf_r is not None:
            gap = abs(stf_l - stf_r)
            if gap >= _PLATOON_GAP_THRESHOLD:
                weak = "LHH" if stf_l < stf_r else "RHH"
                insights.append({
                    "icon": "⚔️", "tone": "negative",
                    "text": f"Platoon-vulnerable — exposed vs {weak}",
                })

    # ── Elite tool at position (only when truly exceptional) ──
    # A tool 20+ above composite is rare enough to note
    if not is_pitcher:
        tool_cols = {"Contact": "cntct", "Power": "pow", "Speed": "speed", "Eye": "eye"}
        for label, col in tool_cols.items():
            val = norm_fn(rd.get(col))
            if val is not None and val >= composite + 20:
                insights.append({
                    "icon": "⭐", "tone": "positive",
                    "text": f"Elite {label.lower()} — standout tool for any position",
                })
                break  # only show one elite tool note

    # ── Severe weakness ──
    if not is_pitcher:
        weak_tools = {"Contact": "cntct", "Power": "pow", "Eye": "eye"}
        for label, col in weak_tools.items():
            val = norm_fn(rd.get(col))
            if val is not None and val <= 25 and composite >= 45:
                insights.append({
                    "icon": "⚠️", "tone": "negative",
                    "text": f"Severe {label.lower()} weakness — limits offensive ceiling",
                })
                break
    else:
        ctrl = norm_fn(rd.get("ctrl"))
        if ctrl is not None and ctrl <= 30 and composite >= 45:
            insights.append({
                "icon": "⚠️", "tone": "negative",
                "text": "Severe control weakness — high walk rate risk",
            })

    return insights


def _surplus_horizons(rows, game_year):
    """Current-year, next-year, and 3-year (years 1-3 ahead) surplus —
    pulled directly from the same per-year breakdown that already feeds the
    long-term total, not a separate calculation. Each row's surplus is
    already "value minus expected cost" for that specific year (real
    contract salary where known, projected arb/perpetual-arb salary where
    estimated), so slicing by year is all that's needed here.

    Returns (current, next, three_year), any of which is None when the
    breakdown doesn't cover that year at all — e.g. a prospect who won't
    debut for several more years has no "current year" surplus to show.
    """
    by_year = {r["year"]: r["surplus"] for r in rows}
    current = by_year.get(game_year)
    nxt = by_year.get(game_year + 1)
    three_yr_years = [y for y in (game_year + 1, game_year + 2, game_year + 3) if y in by_year]
    three_yr = sum(by_year[y] for y in three_yr_years) if three_yr_years else None
    return current, nxt, three_yr


def get_player(pid):
    conn = get_db()
    year = get_cfg().year

    # Bio
    p = conn.execute("SELECT player_id, name, age, team_id, parent_team_id, level, pos, role FROM players WHERE player_id=?", (pid,)).fetchone()
    if not p:
        return None

    player_id, name, age, team_id, parent_team_id, level, pos, role = p["player_id"], p["name"], p["age"], p["team_id"], p["parent_team_id"], p["level"], p["pos"], p["role"]
    is_pitcher = role in (11, 12, 13)
    org_id = team_id if parent_team_id == 0 else parent_team_id
    level_str = level_map().get(str(level), str(level))

    # Injury/status info
    _status_row = conn.execute(
        "SELECT injury_is_injured, injury_left, injury_dl_left, is_on_dl, is_on_dl60, "
        "designated_for_assignment, is_on_waivers, days_on_waivers_left "
        "FROM players WHERE player_id=?", (pid,)).fetchone()
    player_status = None
    if _status_row:
        _inj = _status_row["injury_is_injured"] or 0
        _inj_left = _status_row["injury_left"] or 0
        _dl_left = _status_row["injury_dl_left"] or 0
        _on_dl = _status_row["is_on_dl"] or 0
        _on_dl60 = _status_row["is_on_dl60"] or 0
        _dfa = _status_row["designated_for_assignment"] or 0
        _waivers = _status_row["is_on_waivers"] or 0
        _waivers_left = _status_row["days_on_waivers_left"] or 0

        if _dfa:
            player_status = {"type": "DFA", "label": "Designated for Assignment", "severity": "high"}
        elif _waivers:
            player_status = {"type": "waivers", "label": f"On Waivers ({_waivers_left}d left)" if _waivers_left else "On Waivers", "severity": "high"}
        elif _on_dl60:
            if _inj_left >= 1000:
                player_status = {"type": "dl60", "label": "60-Day DL — Out Indefinitely", "severity": "high"}
            else:
                player_status = {"type": "dl60", "label": f"60-Day DL — {_inj_left} days remaining", "severity": "high"}
        elif _on_dl:
            if _inj_left >= 1000:
                player_status = {"type": "dl", "label": "DL — Out Indefinitely", "severity": "high"}
            else:
                player_status = {"type": "dl", "label": f"DL — {_inj_left} days remaining", "severity": "medium"}
        elif _inj and _inj_left >= 1000:
            player_status = {"type": "out", "label": "Out Indefinitely", "severity": "high"}
        elif _inj and _inj_left > 7:
            player_status = {"type": "injured", "label": f"Injured — {_inj_left} days", "severity": "medium"}
        elif _inj and _inj_left > 0:
            player_status = {"type": "dtd", "label": f"Day-to-Day ({_inj_left}d)", "severity": "low"}

    # Ratings (latest snapshot) — SELECT * + dict to handle leagues with/without extended columns
    r = conn.execute("SELECT * FROM ratings WHERE player_id=? ORDER BY snapshot_date DESC LIMIT 1", (pid,)).fetchone()
    # Build dict from row
    rd = {}
    if r:
        rd = dict(r)

    ratings = None
    if rd:
        def g(k): return rd.get(k)

        ovr, pot = g("ovr"), g("pot")
        cntct, gap, pw, eye, ks = g("cntct"), g("gap"), g("pow"), g("eye"), g("ks")
        speed, steal = g("speed"), g("steal")
        stf, mov, ctrl_ovr = g("stf"), g("mov"), g("ctrl")
        ctrl_r, ctrl_l = g("ctrl_r"), g("ctrl_l")
        stm, vel, gb = g("stm"), g("vel"), g("gb")
        ofa, ifa, c_arm, c_blk, c_frm = g("ofa"), g("ifa"), g("c_arm"), g("c_blk"), g("c_frm")
        ifr, ofr, ife, ofe, tdp = g("ifr"), g("ofr"), g("ife"), g("ofe"), g("tdp")
        height, bats, throws = g("height"), g("bats"), g("throws")
        # Extended ratings (may be None if league doesn't have them)
        babip, babip_l, babip_r, pot_babip = g("babip"), g("babip_l"), g("babip_r"), g("pot_babip")
        hra, hra_l, hra_r, pot_hra = g("hra"), g("hra_l"), g("hra_r"), g("pot_hra")
        pbabip, pbabip_l, pbabip_r, pot_pbabip = g("pbabip"), g("pbabip_l"), g("pbabip_r"), g("pot_pbabip")
        prone = g("prone")

        def _char_label(v):
            if v in ("VL", "L", "N", "H", "VH"):
                return {"VL": "Very Low", "L": "Low", "N": "Normal", "H": "High", "VH": "Very High"}[v]
            return None

        # Personality: prefer text values (VL/L/N/H/VH) — numeric values from league export are unreliable
        pers_row = conn.execute(
            "SELECT int_, wrk_ethic, greed, loy, lead FROM ratings "
            "WHERE player_id=? AND wrk_ethic IN ('VL','L','N','H','VH') "
            "ORDER BY snapshot_date DESC LIMIT 1", (pid,)).fetchone()
        if pers_row:
            p_int, p_ethic, p_greed, p_loy, p_lead = pers_row["int_"], pers_row["wrk_ethic"], pers_row["greed"], pers_row["loy"], pers_row["lead"]
        else:
            p_int, p_ethic, p_greed, p_loy, p_lead = g("int_"), g("wrk_ethic"), g("greed"), g("loy"), g("lead")

        ratings = {"ovr": ovr, "pot": pot, "height": _height_str(height), "bats": bats, "throws": throws,
                   "personality": {"int": _char_label(p_int), "ethic": _char_label(p_ethic),
                                   "greed": _char_label(p_greed), "loy": _char_label(p_loy), "lead": _char_label(p_lead)},
                   "prone": prone if prone else None}

        if is_pitcher:
            ctrl = ctrl_ovr or (round((ctrl_r + ctrl_l) / 2) if ctrl_r and ctrl_l else ctrl_r or ctrl_l)
            ratings["stuff"] = (_norm(stf), _norm(g("pot_stf")))
            ratings["movement"] = (_norm(mov), _norm(g("pot_mov")))
            ratings["control"] = (_norm(ctrl), _norm(g("pot_ctrl")))
            ratings["stamina"] = _norm(stm)
            ratings["velocity"] = vel
            if gb:
                # GB% context from league averages (computed during refresh)
                from web_league_context import league_averages as _load_la
                _la = _load_la()
                _gb_mean = _la.get("pitching", {}).get("gb_pct_mean")
                _gb_stdev = _la.get("pitching", {}).get("gb_pct_stdev")
                if _gb_mean and _gb_stdev and _gb_stdev > 0:
                    _z = (gb - _gb_mean) / _gb_stdev
                    if _z >= 2.0:
                        gb_label = "Extreme GB"
                    elif _z >= 1.0:
                        gb_label = "High GB"
                    elif _z <= -2.0:
                        gb_label = "Extreme FB"
                    elif _z <= -1.0:
                        gb_label = "Fly ball"
                    else:
                        gb_label = "Average"
                else:
                    gb_label = ""
                ratings["gb"] = gb
                ratings["gb_label"] = gb_label
            if hra is not None:
                ratings["hra"] = (_norm(hra), _norm(pot_hra))
            if pbabip is not None:
                ratings["pbabip"] = (_norm(pbabip), _norm(pot_pbabip))
            ratings["splits"] = {
                "stuff": (_norm(g("stf_l")), _norm(g("stf_r"))),
                "movement": (_norm(g("mov_l")), _norm(g("mov_r"))),
                "control": (_norm(ctrl_l), _norm(ctrl_r)),
            }
            if hra_l is not None:
                ratings["splits"]["hra"] = (_norm(hra_l), _norm(hra_r))
            if pbabip_l is not None:
                ratings["splits"]["pbabip"] = (_norm(pbabip_l), _norm(pbabip_r))
            pitches = []
            pitch_raw = [
                ("fst", "pot_fst", "Fastball"), ("snk", "pot_snk", "Sinker"), ("crv", "pot_crv", "Curveball"),
                ("sld", "pot_sld", "Slider"), ("chg", "pot_chg", "Changeup"), ("splt", "pot_splt", "Splitter"),
                ("cutt", "pot_cutt", "Cutter"), ("cir_chg", "pot_cir_chg", "Circle Change"),
                ("scr", "pot_scr", "Screwball"), ("frk", "pot_frk", "Forkball"),
                ("kncrv", "pot_kncrv", "Knuckle Curve"), ("knbl", "pot_knbl", "Knuckleball"),
            ]
            for cur_k, fut_k, label in pitch_raw:
                cur, fut = g(cur_k), g(fut_k)
                if cur or fut:
                    pitches.append({"name": label, "cur": _norm(cur), "fut": _norm(fut)})
            pitches.sort(key=lambda x: -(x["cur"] or 0))
            ratings["pitches"] = pitches
        else:
            ratings["hit"] = (_norm(cntct), _norm(g("pot_cntct")))
            ratings["gap"] = (_norm(gap), _norm(g("pot_gap")))
            ratings["power"] = (_norm(pw), _norm(g("pot_pow")))
            ratings["eye"] = (_norm(eye), _norm(g("pot_eye")))
            ratings["krate"] = (_norm(ks), _norm(g("pot_ks")))
            if babip is not None:
                ratings["babip"] = (_norm(babip), _norm(pot_babip))
            ratings["speed"] = _norm(speed)
            ratings["steal"] = _norm(steal)
            ratings["splits"] = {
                "hit": (_norm(g("cntct_l")), _norm(g("cntct_r"))),
                "gap": (_norm(g("gap_l")), _norm(g("gap_r"))),
                "power": (_norm(g("pow_l")), _norm(g("pow_r"))),
                "eye": (_norm(g("eye_l")), _norm(g("eye_r"))),
                "krate": (_norm(g("ks_l")), _norm(g("ks_r"))),
            }
            if babip_l is not None:
                ratings["splits"]["babip"] = (_norm(babip_l), _norm(babip_r))
            c_def, ss_def = g("c"), g("ss")
            second_b, third_b, first_b = g("second_b"), g("third_b"), g("first_b")
            lf, cf_def, rf = g("lf"), g("cf"), g("rf")
            pot_c, pot_ss, pot_2b = g("pot_c"), g("pot_ss"), g("pot_second_b")
            pot_3b, pot_1b = g("pot_third_b"), g("pot_first_b")
            pot_lf, pot_cf, pot_rf = g("pot_lf"), g("pot_cf"), g("pot_rf")
            def_grades = [
                ("C", c_def, pot_c), ("1B", first_b, pot_1b), ("2B", second_b, pot_2b),
                ("3B", third_b, pot_3b), ("SS", ss_def, pot_ss),
                ("LF", lf, pot_lf), ("CF", cf_def, pot_cf), ("RF", rf, pot_rf),
            ]
            ratings["defense"] = [{"pos": lbl, "cur": _norm(c), "fut": _norm(f)}
                                  for lbl, c, f in def_grades if (c and c >= 20) or (f and f >= 20)]
            is_of = pos in (7, 8, 9)
            is_c = pos == 2
            if is_c:
                ratings["arm"] = _norm(c_arm)
            elif is_of:
                ratings["arm"] = _norm(ofa)
                if ofr: ratings["range"] = _norm(ofr)
                if ofe: ratings["error"] = _norm(ofe)
            else:
                ratings["arm"] = _norm(ifa)
                if ifr: ratings["range"] = _norm(ifr)
                if ife: ratings["error"] = _norm(ife)
                if tdp and pos in (3, 4, 5, 6):
                    ratings["tdp"] = _norm(tdp)
            if c_def and c_def >= 20:
                ratings["blocking"] = _norm(c_blk)
                ratings["framing"] = _norm(c_frm)

    # Two-way: build a full hitter ratings dict so the template can render the standard hitter view
    hit_ratings = None
    if is_pitcher and rd and cntct and cntct >= 20:
        c_def, ss_def = g("c"), g("ss")
        second_b, third_b, first_b = g("second_b"), g("third_b"), g("first_b")
        lf, cf_def, rf = g("lf"), g("cf"), g("rf")
        pot_c, pot_ss, pot_2b = g("pot_c"), g("pot_ss"), g("pot_second_b")
        pot_3b, pot_1b = g("pot_third_b"), g("pot_first_b")
        pot_lf, pot_cf, pot_rf = g("pot_lf"), g("pot_cf"), g("pot_rf")
        hit_ratings = {
            "ovr": ratings["ovr"] if ratings else None, "pot": ratings["pot"] if ratings else None,
            "height": ratings.get("height"), "bats": ratings.get("bats"), "throws": ratings.get("throws"),
            "personality": ratings.get("personality") if ratings else None,
            "hit": (_norm(cntct), _norm(g("pot_cntct"))),
            "gap": (_norm(gap), _norm(g("pot_gap"))),
            "power": (_norm(pw), _norm(g("pot_pow"))),
            "eye": (_norm(eye), _norm(g("pot_eye"))),
            "krate": (_norm(ks), _norm(g("pot_ks"))),
            **({"babip": (_norm(babip), _norm(pot_babip))} if babip is not None else {}),
            "speed": _norm(speed), "steal": _norm(steal),
            "splits": {
                "hit": (_norm(g("cntct_l")), _norm(g("cntct_r"))),
                "gap": (_norm(g("gap_l")), _norm(g("gap_r"))),
                "power": (_norm(g("pow_l")), _norm(g("pow_r"))),
                "eye": (_norm(g("eye_l")), _norm(g("eye_r"))),
                "krate": (_norm(g("ks_l")), _norm(g("ks_r"))),
                **({"babip": (_norm(babip_l), _norm(babip_r))} if babip_l is not None else {}),
            },
            "defense": [{"pos": lbl, "cur": _norm(c), "fut": _norm(f)}
                        for lbl, c, f in [("C", c_def, pot_c), ("1B", first_b, pot_1b), ("2B", second_b, pot_2b),
                                           ("3B", third_b, pot_3b), ("SS", ss_def, pot_ss),
                                           ("LF", lf, pot_lf), ("CF", cf_def, pot_cf), ("RF", rf, pot_rf)]
                        if c and c >= 20],
            "arm": _norm(ofa) if pos in (7,8,9) else _norm(ifa),
        }

    # Fielding stats
    fielding_stats = []
    for row in conn.execute(
        "SELECT year, position, g, gs, ip, tc, a, po, e, dp, pb, sba, rto, zr, framing, arm "
        "FROM mlb_fielding_stats WHERE player_id=? ORDER BY year DESC, position", (pid,)).fetchall():
        yr, fpos, g, gs, ip, tc, a, po, e, dp, pb, sba, rto, zr, framing, arm = row
        if g == 0:
            continue
        fpct = (po + a) / tc if tc else 0
        fielding_stats.append({
            "year": yr, "pos": pos_map().get(fpos, str(fpos)), "g": g, "gs": gs, "ip": ip,
            "tc": tc, "a": a, "po": po, "e": e, "dp": dp,
            "fpct": fpct, "zr": zr,
            "pb": pb if fpos == 2 else None,
            "sba": sba if fpos == 2 else None,
            "rto": rto if fpos == 2 else None,
            "framing": framing if fpos == 2 else None,
            "arm": arm,
        })

    # Per-position fielding career totals
    fielding_career = []
    if len(fielding_stats) > 1:
        from collections import defaultdict
        _fld_by_pos = defaultdict(list)
        for f in fielding_stats:
            _fld_by_pos[f["pos"]].append(f)
        for fpos in _fld_by_pos:
            entries = _fld_by_pos[fpos]
            if len(entries) < 2:
                continue
            _f_g = sum(f["g"] for f in entries)
            _f_ip = sum(f["ip"] for f in entries)
            _f_tc = sum(f["tc"] for f in entries)
            _f_a = sum(f["a"] for f in entries)
            _f_po = sum(f["po"] for f in entries)
            _f_e = sum(f["e"] for f in entries)
            _f_dp = sum(f["dp"] for f in entries)
            _f_fpct = (_f_po + _f_a) / _f_tc if _f_tc else 0
            # Weight ZR and arm by IP
            _f_zr = sum(f["zr"] * f["ip"] for f in entries if f["zr"] is not None)
            _f_zr_ip = sum(f["ip"] for f in entries if f["zr"] is not None)
            _f_arm = sum(f["arm"] * f["ip"] for f in entries if f["arm"] is not None)
            _f_arm_ip = sum(f["ip"] for f in entries if f["arm"] is not None)
            fielding_career.append({
                "pos": fpos, "g": _f_g, "ip": _f_ip, "tc": _f_tc,
                "a": _f_a, "e": _f_e, "dp": _f_dp, "fpct": _f_fpct,
                "zr": _f_zr / _f_zr_ip if _f_zr_ip else None,
                "arm": _f_arm / _f_arm_ip if _f_arm_ip else None,
            })
        fielding_career.sort(key=lambda x: -x["g"])

    # Surplus / FV
    ed = conn.execute("SELECT MAX(eval_date) FROM player_surplus").fetchone()[0]
    surplus_row = conn.execute(
        "SELECT bucket, ovr, surplus, fv_str, surplus_yr1 FROM player_surplus WHERE player_id=? AND eval_date=?",
        (pid, ed)).fetchone()
    prospect_row = conn.execute(
        "SELECT bucket, fv, fv_str, prospect_surplus, level, risk, fv_continuous FROM prospect_fv WHERE player_id=? AND eval_date=?",
        (pid, ed)).fetchone()

    valuation = {}
    # For rookie-eligible MLB players (in both tables), prefer prospect surplus —
    # the contract model only sees the current 1-year pre-arb deal and drastically
    # undervalues years of remaining team control.
    if prospect_row and surplus_row:
        # Player is in both: use prospect valuation (more complete for pre-arb players)
        valuation["bucket"] = _display_pos(prospect_row[0])
        valuation["fv"] = prospect_row[1]
        valuation["fv_str"] = prospect_row[2]
        valuation["surplus"] = round(prospect_row[3] / _money_divisor(), 1) if prospect_row[3] else 0
        valuation["type"] = "prospect"
        valuation["level"] = prospect_row[4]
        valuation["risk"] = prospect_row[5]
        valuation["fv_continuous"] = prospect_row[6]
        valuation["ovr"] = (rd.get("composite_score") if rd else None) or (ratings["ovr"] if ratings else None)
        valuation["pot"] = (rd.get("true_ceiling") or rd.get("ceiling_score") if rd else None) or (ratings["pot"] if ratings else None)
        _def_keys = {'CF':'pot_cf','SS':'pot_ss','C':'pot_c','2B':'pot_second_b','3B':'pot_third_b'}
        valuation["def_rating"] = rd.get(_def_keys.get(prospect_row[0], "")) or 0 if rd else 0
    elif surplus_row:
        valuation["bucket"] = _display_pos(surplus_row[0])
        valuation["ovr"] = surplus_row[1]
        valuation["surplus"] = round(surplus_row[2] / _money_divisor(), 1) if surplus_row[2] else 0
        valuation["fv_str"] = surplus_row[3]
        valuation["type"] = "MLB"
    elif prospect_row:
        valuation["bucket"] = _display_pos(prospect_row[0])
        valuation["fv"] = prospect_row[1]
        valuation["fv_str"] = prospect_row[2]
        valuation["surplus"] = round(prospect_row[3] / _money_divisor(), 1) if prospect_row[3] else 0
        valuation["type"] = "prospect"
        valuation["level"] = prospect_row[4]
        valuation["risk"] = prospect_row[5]
        valuation["fv_continuous"] = prospect_row[6]
        valuation["ovr"] = (rd.get("composite_score") if rd else None) or (ratings["ovr"] if ratings else None)
        valuation["pot"] = (rd.get("true_ceiling") or rd.get("ceiling_score") if rd else None) or (ratings["pot"] if ratings else None)
        _def_keys = {'CF':'pot_cf','SS':'pot_ss','C':'pot_c','2B':'pot_second_b','3B':'pot_third_b'}
        valuation["def_rating"] = rd.get(_def_keys.get(prospect_row[0], "")) or 0 if rd else 0

    # Contract
    contract = None
    c = conn.execute("""SELECT years, current_year, salary_0, salary_1, salary_2, salary_3, salary_4,
        salary_5, salary_6, salary_7, no_trade, last_year_team_option, last_year_player_option,
        last_year_vesting_option, last_year_option_buyout,
        next_last_year_team_option, next_last_year_player_option,
        next_last_year_vesting_option, next_last_year_option_buyout,
        minimum_pa, minimum_pa_bonus, minimum_ip, minimum_ip_bonus,
        mvp_bonus, cyyoung_bonus, allstar_bonus
        FROM contracts WHERE player_id=?""", (pid,)).fetchone()
    if c:
        yrs, cur_yr = c[0], c[1]
        game_year = get_cfg().year
        salaries = [c[i] for i in range(2, 10)]
        remaining = [(i, salaries[i]) for i in range(cur_yr, min(yrs, 8)) if salaries[i]]
        contract = {
            "years": yrs, "current_year": cur_yr + 1,
            "remaining": [(str(game_year + i - cur_yr), f"${s/1e6:.1f}M" if s >= 1e6 else f"${s/1e3:.0f}K") for i, s in remaining],
            "no_trade": c[10], "team_option": c[11], "player_option": c[12],
            "vesting_option": c[13], "option_buyout": c[14],
            "next_team_option": c[15], "next_player_option": c[16],
            "next_vesting_option": c[17], "next_option_buyout": c[18],
            "incentives": {},
        }
        # Collect non-zero incentives
        if c[19]:  # minimum_pa
            contract["incentives"]["PA bonus"] = f"{c[19]} PA → ${c[20]/1e6:.1f}M" if c[20] else f"{c[19]} PA"
        if c[21]:  # minimum_ip
            contract["incentives"]["IP bonus"] = f"{c[21]} IP → ${c[22]/1e6:.1f}M" if c[22] else f"{c[21]} IP"
        if c[23]:  # mvp_bonus
            contract["incentives"]["MVP"] = f"${c[23]/1e6:.1f}M"
        if c[24]:  # cyyoung_bonus
            contract["incentives"]["Cy Young"] = f"${c[24]/1e6:.1f}M"
        if c[25]:  # allstar_bonus
            contract["incentives"]["All-Star"] = f"${c[25]/1e6:.1f}M"
        # Pending extension
        try:
            ext = conn.execute("SELECT years, salary_0, salary_1, salary_2, salary_3, salary_4, salary_5, salary_6, salary_7, salary_8, salary_9, salary_10, salary_11, salary_12, salary_13, salary_14, no_trade, last_year_team_option, last_year_player_option FROM contract_extensions WHERE player_id=?", (pid,)).fetchone()
        except Exception:
            ext = None
        if ext and ext[0] > 0:
            ext_yrs = ext[0]
            cur_remaining = yrs - cur_yr
            ext_start_year = game_year + cur_remaining
            ext_sals = [(str(ext_start_year + i), f"${ext[1+i]/1e6:.1f}M" if ext[1+i] >= 1e6 else f"${ext[1+i]/1e3:.0f}K")
                        for i in range(ext_yrs) if i < 15]
            contract["extension"] = {
                "years": ext_yrs,
                "salaries": ext_sals,
                "no_trade": ext[16],
                "team_option": ext[17],
                "player_option": ext[18],
            }

    # League averages for ERA+/OPS+
    from web_league_context import league_averages as _load_la
    _la = _load_la()
    lg_era = _la["pitching"]["era"]
    lg_ops = _la["batting"]["obp"] + _la["batting"]["slg"]

    def _bat_row(row):
        yr, ab, h, d, t, hr, rbi, bb, k, sb, pa, war, hbp, sf, g, cs = row
        hbp = hbp or 0; sf = sf or 0; g = g or 0; cs = cs or 0
        avg = h / ab if ab else 0
        obp = (h + bb + hbp) / (ab + bb + hbp + sf) if (ab + bb + hbp + sf) else 0
        slg = (h + d + 2 * t + 3 * hr) / ab if ab else 0
        ops = obp + slg
        iso = slg - avg
        babip_denom = ab - k - hr + sf
        babip = (h - hr) / babip_denom if babip_denom > 0 else 0
        bb_pct = bb / pa * 100 if pa else 0
        so_pct = k / pa * 100 if pa else 0
        ops_plus = round(ops / lg_ops * 100) if lg_ops and ops else 0
        return {
            "year": yr, "g": g, "pa": pa, "ab": ab, "h": h, "hr": hr, "rbi": rbi,
            "bb": bb, "k": k, "sb": sb, "cs": cs,
            "war": round(war, 1) if war else 0,
            "avg": avg, "obp": obp, "slg": slg, "iso": iso,
            "ops": ops, "ops_plus": ops_plus, "babip": babip,
            "bb_pct": bb_pct, "so_pct": so_pct,
            # raw counts preserved for multi-stint aggregation
            "_d": d, "_t": t, "_hbp": hbp, "_sf": sf,
        }

    _bat_sql = "SELECT year, ab, h, d, t, hr, rbi, bb, k, sb, pa, war, hbp, sf, g, cs FROM batting_stats WHERE player_id=? AND split_id=? AND league_id IS NULL ORDER BY year"

    def _aggregate_bat_stints(rows):
        """Aggregate multi-team stints into one row per year, preserving per-team breakdown."""
        from collections import defaultdict
        by_year = defaultdict(list)
        for r in rows:
            by_year[r["year"]].append(r)
        result = []
        for yr in sorted(by_year):
            stints = by_year[yr]
            if len(stints) == 1:
                result.append(stints[0])
                continue
            # Aggregate counting stats
            ab  = sum(s["ab"]  for s in stints)
            h   = sum(s["h"]   for s in stints)
            hr  = sum(s["hr"]  for s in stints)
            rbi = sum(s["rbi"] for s in stints)
            bb  = sum(s["bb"]  for s in stints)
            k   = sum(s["k"]   for s in stints)
            sb  = sum(s["sb"]  for s in stints)
            cs  = sum(s["cs"]  for s in stints)
            pa  = sum(s["pa"]  for s in stints)
            war = sum(s["war"] for s in stints)
            g   = sum(s["g"]   for s in stints)
            d   = sum(s.get("_d", 0) for s in stints)
            t   = sum(s.get("_t", 0) for s in stints)
            avg = h / ab if ab else 0
            obp_num = h + bb + sum(s.get("_hbp", 0) for s in stints)
            obp_den = ab + bb + sum(s.get("_hbp", 0) for s in stints) + sum(s.get("_sf", 0) for s in stints)
            obp = obp_num / obp_den if obp_den else 0
            slg = (h + d + 2*t + 3*hr) / ab if ab else 0
            ops = obp + slg
            iso = slg - avg
            babip_denom = ab - k - hr
            babip = (h - hr) / babip_denom if babip_denom > 0 else 0
            bb_pct = bb / pa * 100 if pa else 0
            so_pct = k / pa * 100 if pa else 0
            ops_plus = round(ops / lg_ops * 100) if lg_ops and ops else 0
            agg = {
                "year": yr, "g": g, "pa": pa, "ab": ab, "h": h, "hr": hr, "rbi": rbi,
                "bb": bb, "k": k, "sb": sb, "cs": cs,
                "war": round(war, 1),
                "avg": avg, "obp": obp, "slg": slg, "iso": iso,
                "ops": ops, "ops_plus": ops_plus, "babip": babip,
                "bb_pct": bb_pct, "so_pct": so_pct,
                "stints": stints,
            }
            result.append(agg)
        return result

    bat_stats_raw = [_bat_row(r) for r in conn.execute(_bat_sql, (pid, 1)).fetchall()]
    # Attach team info to each stint row
    _team_rows = conn.execute(
        "SELECT year, team_id, pa, ab, h, d, t, hr, rbi, bb, k, sb, pa, war, hbp, sf, g, cs "
        "FROM batting_stats WHERE player_id=? AND split_id=1 AND league_id IS NULL ORDER BY year, stint",
        (pid,)).fetchall()
    _bat_with_teams = []
    for r in _team_rows:
        row_dict = _bat_row((r[0], r[3], r[4], r[5], r[6], r[7], r[8], r[9], r[10], r[11], r[2], r[13], r[14], r[15], r[16], r[17]))
        row_dict["team_id"] = r[1]
        row_dict["team"] = team_abbr_map().get(r[1], str(r[1]))
        _bat_with_teams.append(row_dict)
    bat_stats = _aggregate_bat_stints(_bat_with_teams)
    bat_splits = {
        "vl": [_bat_row(r) for r in conn.execute(_bat_sql, (pid, 2)).fetchall()],
        "vr": [_bat_row(r) for r in conn.execute(_bat_sql, (pid, 3)).fetchall()],
    }

    # FIP constant
    tp = conn.execute(
        "SELECT SUM(era*ip)/SUM(ip), SUM(hra), SUM(bb), SUM(k), SUM(ip) FROM team_pitching_stats WHERE split_id=1"
    ).fetchone()
    fip_const = (tp[0] - ((13 * tp[1] + 3 * tp[2] - 2 * tp[3]) / tp[4])) if tp and tp[4] else 3.1

    def _pit_row(row):
        yr, ip, era, k, bb, w, l, sv, war, gs, g, hra, bf, hp, ha, hld, bs, qs, gb, fb, r_field, er = row
        hra = hra or 0; bf = bf or 0; hp = hp or 0; ha = ha or 0
        hld = hld or 0; bs = bs or 0; qs = qs or 0
        gb = gb or 0; fb = fb or 0; r_field = r_field or 0; er = er or 0
        k9 = k * 9 / ip if ip else 0
        bb9 = bb * 9 / ip if ip else 0
        hr9 = hra * 9 / ip if ip else 0
        fip = ((13 * hra + 3 * (bb + hp) - 2 * k) / ip + fip_const) if ip else 0
        era_plus = round(lg_era / era * 100) if era else 0
        babip_denom = bf - k - hra - bb - hp
        p_babip = (ha - hra) / babip_denom if babip_denom > 0 else 0
        k_pct = k / bf * 100 if bf else 0
        bb_pct_p = bb / bf * 100 if bf else 0
        k_bb_pct = k_pct - bb_pct_p
        gb_pct = gb / (gb + fb) * 100 if (gb + fb) else 0
        siera_k = k / bf if bf else 0
        siera_bb = bb / bf if bf else 0
        siera = (6.145 - 16.986 * siera_k + 11.434 * siera_bb
                 + 7.653 * siera_k**2 + 6.664 * siera_bb**2
                 + 0.9) if bf else 0
        return {
            "year": yr, "ip": ip, "era": era, "w": w, "l": l, "sv": sv,
            "k": k, "bb": bb, "war": round(war, 1) if war else 0,
            "gs": gs, "g": g, "hld": hld, "bs": bs, "qs": qs,
            "fip": fip, "siera": siera, "babip": p_babip,
            "hr9": hr9, "bb9": bb9, "k9": k9, "era_plus": era_plus,
            "k_pct": k_pct, "bb_pct": bb_pct_p, "k_bb_pct": k_bb_pct,
            "gb_pct": gb_pct,
            # raw counts preserved for multi-stint aggregation
            "_er": er, "_hra": hra, "_bf": bf, "_hp": hp, "_ha": ha,
            "_gb": gb, "_fb": fb,
        }

    _pit_sql = "SELECT year, ip, era, k, bb, w, l, sv, war, gs, g, hra, bf, hp, ha, hld, bs, qs, gb, fb, r, er FROM pitching_stats WHERE player_id=? AND split_id=? AND league_id IS NULL ORDER BY year"

    def _aggregate_pit_stints(rows):
        from collections import defaultdict
        by_year = defaultdict(list)
        for r in rows:
            by_year[r["year"]].append(r)
        result = []
        for yr in sorted(by_year):
            stints = by_year[yr]
            if len(stints) == 1:
                result.append(stints[0])
                continue
            ip   = sum(s["ip"]  for s in stints)
            k    = sum(s["k"]   for s in stints)
            bb   = sum(s["bb"]  for s in stints)
            w    = sum(s["w"]   for s in stints)
            l    = sum(s["l"]   for s in stints)
            sv   = sum(s["sv"]  for s in stints)
            war  = sum(s["war"] for s in stints)
            gs   = sum(s["gs"]  for s in stints)
            g    = sum(s["g"]   for s in stints)
            hld  = sum(s["hld"] for s in stints)
            bs   = sum(s["bs"]  for s in stints)
            qs   = sum(s["qs"]  for s in stints)
            er   = sum(s.get("_er", 0) for s in stints)
            hra  = sum(s.get("_hra", 0) for s in stints)
            bf   = sum(s.get("_bf", 0) for s in stints)
            hp   = sum(s.get("_hp", 0) for s in stints)
            ha   = sum(s.get("_ha", 0) for s in stints)
            gb   = sum(s.get("_gb", 0) for s in stints)
            fb   = sum(s.get("_fb", 0) for s in stints)
            era  = er * 27 / (ip * 3) if ip else 0
            k9   = k * 9 / ip if ip else 0
            bb9  = bb * 9 / ip if ip else 0
            hr9  = hra * 9 / ip if ip else 0
            fip  = ((13 * hra + 3 * (bb + hp) - 2 * k) / ip + fip_const) if ip else 0
            era_plus = round(lg_era / era * 100) if era else 0
            babip_denom = bf - k - hra - bb - hp
            p_babip = (ha - hra) / babip_denom if babip_denom > 0 else 0
            k_pct = k / bf * 100 if bf else 0
            bb_pct_p = bb / bf * 100 if bf else 0
            k_bb_pct = k_pct - bb_pct_p
            gb_pct = gb / (gb + fb) * 100 if (gb + fb) else 0
            siera_k = k / bf if bf else 0
            siera_bb = bb / bf if bf else 0
            siera = (6.145 - 16.986*siera_k + 11.434*siera_bb
                     + 7.653*siera_k**2 + 6.664*siera_bb**2 + 0.9) if bf else 0
            agg = {
                "year": yr, "ip": ip, "era": era, "w": w, "l": l, "sv": sv,
                "k": k, "bb": bb, "war": round(war, 1),
                "gs": gs, "g": g, "hld": hld, "bs": bs, "qs": qs,
                "fip": fip, "siera": siera, "babip": p_babip,
                "hr9": hr9, "bb9": bb9, "k9": k9, "era_plus": era_plus,
                "k_pct": k_pct, "bb_pct": bb_pct_p, "k_bb_pct": k_bb_pct,
                "gb_pct": gb_pct,
                "stints": stints,
            }
            result.append(agg)
        return result

    _pit_team_rows = conn.execute(
        "SELECT year, team_id, ip, era, k, bb, w, l, sv, war, gs, g, hra, bf, hp, ha, hld, bs, qs, gb, fb, r, er "
        "FROM pitching_stats WHERE player_id=? AND split_id=1 AND league_id IS NULL ORDER BY year, stint",
        (pid,)).fetchall()
    _pit_with_teams = []
    for r in _pit_team_rows:
        row_dict = _pit_row((r[0], r[2], r[3], r[4], r[5], r[6], r[7], r[8], r[9], r[10], r[11], r[12], r[13], r[14], r[15], r[16], r[17], r[18], r[19], r[20], r[21], r[22]))
        row_dict["team_id"] = r[1]
        row_dict["team"] = team_abbr_map().get(r[1], str(r[1]))
        _pit_with_teams.append(row_dict)
    pit_stats = _aggregate_pit_stints(_pit_with_teams)
    pit_splits = {
        "vl": [_pit_row(r) for r in conn.execute(_pit_sql, (pid, 2)).fetchall()],
        "vr": [_pit_row(r) for r in conn.execute(_pit_sql, (pid, 3)).fetchall()],
    }

    # PAP inputs (gather before conn.close)
    _pap_sal = 0
    _pap_tg = 0
    _pap_year = get_cfg().year
    if surplus_row:
        c_row = conn.execute("SELECT salary_0 FROM contracts WHERE player_id=?", (pid,)).fetchone()
        _pap_sal = c_row[0] if c_row else 0
        _pap_tg = conn.execute(
            "SELECT COUNT(*) FROM games WHERE (home_team=? OR away_team=?) AND date>=? AND played=1",
            (org_id, org_id, f"{_pap_year}-01-01")).fetchone()[0]

    # Development tracking: snapshot deltas between two most recent ratings_history rows
    snapshot_deltas = None
    try:
        _hist_cols_info = conn.execute("PRAGMA table_info(ratings_history)").fetchall()
        _hist_col_names = [c[1] for c in _hist_cols_info]
        if "composite_score" in _hist_col_names:
            _hist_rows = conn.execute(
                "SELECT * FROM ratings_history WHERE player_id=? ORDER BY snapshot_date DESC LIMIT 2",
                (pid,)
            ).fetchall()
            if len(_hist_rows) == 2:
                _cur_snap = dict(zip(_hist_col_names, _hist_rows[0]))
                _prev_snap = dict(zip(_hist_col_names, _hist_rows[1]))
                from statsplusplus.data.evaluation_engine import compute_snapshot_deltas
                snapshot_deltas = compute_snapshot_deltas(_cur_snap, _prev_snap)
                # Pre-compute display-ready tool breakdown for the template
                # Tool deltas are on raw (1-100) scale; normalize to 20-80 for display
                from statsplusplus.config.ratings import norm as _norm_r
                _TOOL_LABELS = {
                    "cntct":"Con", "gap":"Gap", "pow":"Pow", "eye":"Eye", "ks":"Avoid K", "speed":"Spd",
                    "stf":"Stf", "mov":"Mov", "ctrl":"Ctrl", "stm":"Stm",
                    "pot_cntct":"pCon", "pot_gap":"pGap", "pot_pow":"pPow", "pot_eye":"pEye", "pot_ks":"pAvK",
                    "pot_stf":"pStf", "pot_mov":"pMov", "pot_ctrl":"pCtrl",
                    "fst":"FB", "snk":"SNK", "crv":"CRV", "sld":"SLD", "chg":"CHG", "splt":"SPL", "cutt":"CUT",
                    "pot_fst":"pFB", "pot_snk":"pSNK", "pot_crv":"pCRV", "pot_sld":"pSLD",
                    "pot_chg":"pCHG", "pot_splt":"pSPL", "pot_cutt":"pCUT",
                    "babip":"BABIP", "hra":"HR Avd", "pbabip":"pBABIP", "pot_babip":"pBABIP", "pot_hra":"pHR Avd",
                }
                sig = []
                for k in _TOOL_LABELS:
                    cur_raw = _cur_snap.get(k)
                    prev_raw = _prev_snap.get(k)
                    if cur_raw is not None and prev_raw is not None:
                        cur_norm = _norm_r(cur_raw) or 0
                        prev_norm = _norm_r(prev_raw) or 0
                        d = cur_norm - prev_norm
                        if abs(d) >= 5:
                            sig.append({"name": _TOOL_LABELS[k], "delta": d})
                # Sort by magnitude descending, keep top 5
                sig.sort(key=lambda x: abs(x["delta"]), reverse=True)
                snapshot_deltas["top_changes"] = sig[:5]
    except Exception:
        pass

    # Development history — full timeline from ratings_history
    dev_history = None
    try:
        from statsplusplus.config.ratings import norm as _norm_hist
        _dh_rows = conn.execute(
            "SELECT * FROM ratings_history WHERE player_id=? ORDER BY snapshot_date",
            (pid,)
        ).fetchall()
        if len(_dh_rows) >= 2:
            _dh_cols = [c[1] for c in conn.execute("PRAGMA table_info(ratings_history)").fetchall()]
            # Determine which tools to show based on role
            if is_pitcher:
                _dh_tools = [
                    ("stf", "Stuff"), ("mov", "Movement"), ("ctrl", "Control"), ("stm", "Stamina"),
                    ("pot_stf", "pStuff"), ("pot_mov", "pMov"), ("pot_ctrl", "pCtrl"),
                ]
                # Tool pairs for table: (cur_key, pot_key, label)
                _dh_pairs = [
                    ("stf", "pot_stf", "Stuff"), ("mov", "pot_mov", "Mov"),
                    ("ctrl", "pot_ctrl", "Ctrl"), ("stm", None, "Stm"),
                ]
                # Add top 3 pitches by current potential
                _pitch_keys = ["fst","snk","crv","sld","chg","splt","cutt","cir_chg","scr","frk","kncrv","knbl"]
                _pitch_labels = {"fst":"FB","snk":"Sinker","crv":"Curve","sld":"Slider","chg":"Change",
                                 "splt":"Splitter","cutt":"Cutter","cir_chg":"Circle","scr":"Screwball",
                                 "frk":"Forkball","kncrv":"Kn-Curve","knbl":"Knuckleball"}
                # Get latest snapshot to find top pitches
                _latest = dict(zip(_dh_cols, _dh_rows[-1]))
                _pitch_vals = [(k, _latest.get("pot_" + k) or 0) for k in _pitch_keys]
                _pitch_vals.sort(key=lambda x: x[1], reverse=True)
                for pk, pv in _pitch_vals[:3]:
                    if pv and (_norm_hist(pv) or 0) >= 30:
                        _dh_tools.append((pk, _pitch_labels.get(pk, pk)))
                        _dh_tools.append(("pot_" + pk, "p" + _pitch_labels.get(pk, pk)))
                        _dh_pairs.append((pk, "pot_" + pk, _pitch_labels.get(pk, pk)))
            else:
                _dh_tools = [
                    ("cntct", "Contact"), ("gap", "Gap"), ("pow", "Power"), ("eye", "Eye"),
                    ("speed", "Speed"),
                    ("pot_cntct", "pContact"), ("pot_gap", "pGap"), ("pot_pow", "pPower"), ("pot_eye", "pEye"),
                ]
                _dh_pairs = [
                    ("cntct", "pot_cntct", "Con"), ("gap", "pot_gap", "Gap"),
                    ("pow", "pot_pow", "Pow"), ("eye", "pot_eye", "Eye"),
                    ("speed", None, "Spd"),
                ]

            snapshots = []
            for i, row in enumerate(_dh_rows):
                d = dict(zip(_dh_cols, row))
                snap = {
                    "date": d["snapshot_date"],
                    "date_short": d["snapshot_date"][5:],  # "MM-DD"
                    "composite": d.get("composite_score"),
                    "ceiling": d.get("ceiling_score"),
                    "tools": {},
                }
                # Days since previous snapshot
                if i > 0:
                    from datetime import date as _dt
                    prev_d = _dt.fromisoformat(snapshots[-1]["date"])
                    cur_d = _dt.fromisoformat(d["snapshot_date"])
                    snap["days_since_prev"] = (cur_d - prev_d).days
                else:
                    snap["days_since_prev"] = None

                for key, label in _dh_tools:
                    raw = d.get(key)
                    snap["tools"][key] = _norm_hist(raw) if raw is not None else None
                # Also store all pitch ratings for charts (may not be in _dh_tools)
                if is_pitcher:
                    for pk in ["fst","snk","crv","sld","chg","splt","cutt","cir_chg","scr","frk","kncrv","knbl"]:
                        for prefix in ("", "pot_"):
                            k = prefix + pk
                            if k not in snap["tools"]:
                                raw = d.get(k)
                                snap["tools"][k] = _norm_hist(raw) if raw is not None else None
                snapshots.append(snap)

            dev_history = {
                "snapshots": snapshots,
                "tools": _dh_tools,  # [(key, label), ...]
                "tool_pairs": _dh_pairs,  # [(cur_key, pot_key, label), ...]
            }

            # Compute chart-ready data: x positions proportional to time
            from datetime import date as _dtc
            _dates = [_dtc.fromisoformat(s["date"]) for s in snapshots]
            _total_days = (_dates[-1] - _dates[0]).days
            if _total_days > 0:
                x_positions = [(_d - _dates[0]).days / _total_days for _d in _dates]
            else:
                x_positions = [i / max(1, len(snapshots) - 1) for i in range(len(snapshots))]

            def _make_series(key, label, color):
                pts = [{"x": x_positions[i], "y": s["tools"].get(key), "date": s["date_short"]}
                       for i, s in enumerate(snapshots) if s["tools"].get(key) is not None]
                return {"key": key, "label": label, "color": color, "points": pts} if pts else None

            def _auto_range(series_list):
                """Compute y_min/y_max from data, snapped to 5-grade increments."""
                all_vals = [pt["y"] for s in series_list for pt in s["points"]]
                if not all_vals:
                    return 20, 80
                lo = min(all_vals) - 5
                hi = max(all_vals) + 5
                lo = max(20, (lo // 5) * 5)
                hi = min(80, ((hi + 4) // 5) * 5)
                if hi - lo < 15:
                    mid = (hi + lo) // 2
                    lo, hi = max(20, mid - 10), min(80, mid + 10)
                return int(lo), int(hi)

            # Panel 1: Overview (composite + ceiling)
            overview_series = [
                {"key": "composite", "label": "Composite", "color": "#42a5f5",
                 "points": [{"x": x_positions[i], "y": s["composite"], "date": s["date_short"]}
                            for i, s in enumerate(snapshots) if s["composite"] is not None]},
                {"key": "ceiling", "label": "Ceiling", "color": "#ffc107",
                 "points": [{"x": x_positions[i], "y": s["ceiling"], "date": s["date_short"]}
                            for i, s in enumerate(snapshots) if s["ceiling"] is not None]},
            ]

            # Strong, distinct colors for each tool
            _primary_colors = {"stf": "#66bb6a", "mov": "#42a5f5", "ctrl": "#ffc107", "stm": "#ff7043",
                               "cntct": "#66bb6a", "gap": "#42a5f5", "pow": "#ffc107", "eye": "#ff7043", "speed": "#ab47bc"}
            _pitch_colors = ["#66bb6a", "#42a5f5", "#ffc107", "#ff7043", "#ab47bc", "#26c6da", "#ec407a", "#8d6e63"]

            if is_pitcher:
                # Panel 2: Primary current (Stuff, Mov, Ctrl, Stm)
                primary_cur = [s for s in [
                    _make_series("stf", "Stuff", "#66bb6a"),
                    _make_series("mov", "Movement", "#42a5f5"),
                    _make_series("ctrl", "Control", "#ffc107"),
                    _make_series("stm", "Stamina", "#ff7043"),
                ] if s]

                # Panel 3: Primary potential (pStuff, pMov, pCtrl)
                primary_pot = [s for s in [
                    _make_series("pot_stf", "Stuff", "#66bb6a"),
                    _make_series("pot_mov", "Movement", "#42a5f5"),
                    _make_series("pot_ctrl", "Control", "#ffc107"),
                ] if s]

                # Panel 4: Pitch arsenal - ALL pitches with rating >= 20
                _all_pitch_keys = ["fst","snk","crv","sld","chg","splt","cutt","cir_chg","scr","frk","kncrv","knbl"]
                _all_pitch_labels = {"fst":"Fastball","snk":"Sinker","crv":"Curve","sld":"Slider","chg":"Change",
                                     "splt":"Splitter","cutt":"Cutter","cir_chg":"Circle","scr":"Screwball",
                                     "frk":"Forkball","kncrv":"Kn-Curve","knbl":"Knuckleball"}
                pitch_cur = []
                pitch_pot = []
                _pi = 0
                for pk in _all_pitch_keys:
                    # Check if this pitch exists (any snapshot has a value >= 20)
                    has_pitch = any(s["tools"].get(pk) and s["tools"][pk] >= 20 for s in snapshots)
                    if has_pitch:
                        color = _pitch_colors[_pi % len(_pitch_colors)]
                        s_cur = _make_series(pk, _all_pitch_labels[pk], color)
                        s_pot = _make_series("pot_" + pk, _all_pitch_labels[pk], color)
                        if s_cur:
                            pitch_cur.append(s_cur)
                        if s_pot:
                            pitch_pot.append(s_pot)
                        _pi += 1

                panels = [
                    {"title": "Overview", "series": overview_series, "y_min": None, "y_max": None},
                    {"title": "Primary Ratings", "series": primary_cur, "y_min": None, "y_max": None},
                    {"title": "Primary Potential", "series": primary_pot, "y_min": None, "y_max": None},
                    {"title": "Pitch Arsenal", "series": pitch_cur, "y_min": None, "y_max": None},
                    {"title": "Pitch Potential", "series": pitch_pot, "y_min": None, "y_max": None},
                ]
            else:
                # Hitters: Panel 2: Current tools
                cur_series = [s for s in [
                    _make_series("cntct", "Contact", "#66bb6a"),
                    _make_series("gap", "Gap", "#42a5f5"),
                    _make_series("pow", "Power", "#ffc107"),
                    _make_series("eye", "Eye", "#ff7043"),
                    _make_series("speed", "Speed", "#ab47bc"),
                ] if s]

                # Panel 3: Potential tools
                pot_series = [s for s in [
                    _make_series("pot_cntct", "Contact", "#66bb6a"),
                    _make_series("pot_gap", "Gap", "#42a5f5"),
                    _make_series("pot_pow", "Power", "#ffc107"),
                    _make_series("pot_eye", "Eye", "#ff7043"),
                ] if s]

                panels = [
                    {"title": "Overview", "series": overview_series, "y_min": None, "y_max": None},
                    {"title": "Current Tools", "series": cur_series, "y_min": None, "y_max": None},
                    {"title": "Potential Tools", "series": pot_series, "y_min": None, "y_max": None},
                ]

            # Auto-scale y-axis for each panel
            for panel in panels:
                if panel["series"]:
                    panel["y_min"], panel["y_max"] = _auto_range(panel["series"])

            # Remove empty panels
            panels = [p for p in panels if p["series"]]

            # Compute dot nudges for overlapping points and right-edge label offsets
            _nudge_px = 6  # pixels to offset overlapping dots
            for panel in panels:
                # Group points by x-position index to detect overlaps
                for xi, xpos in enumerate(x_positions):
                    # Collect all series that have a point at this x
                    vals_at_x = []  # [(series_idx, point_idx, y_value)]
                    for si, series in enumerate(panel["series"]):
                        for pi, pt in enumerate(series["points"]):
                            if abs(pt["x"] - xpos) < 0.001:
                                vals_at_x.append((si, pi, pt["y"]))
                    # Group by y value
                    by_y = {}
                    for si, pi, yv in vals_at_x:
                        by_y.setdefault(yv, []).append((si, pi))
                    # Apply nudges to groups with >1 point
                    for yv, group in by_y.items():
                        if len(group) <= 1:
                            continue
                        # Spread symmetrically: -6, +6 for 2; -6, 0, +6 for 3
                        n = len(group)
                        for i, (si, pi) in enumerate(group):
                            offset = (i - (n - 1) / 2) * _nudge_px
                            panel["series"][si]["points"][pi]["nudge"] = offset

                # Compute right-edge label offsets (based on last point y-values)
                label_positions = []  # [(series_idx, y_value)]
                for si, series in enumerate(panel["series"]):
                    if series["points"]:
                        label_positions.append((si, series["points"][-1]["y"]))
                # Group labels by y-value (within 2 grades = overlap)
                label_groups = {}  # y_bucket -> [(si, actual_y)]
                for si, yv in label_positions:
                    # Find existing bucket within 2 grades
                    placed = False
                    for bucket_y in list(label_groups.keys()):
                        if abs(yv - bucket_y) <= 2:
                            label_groups[bucket_y].append((si, yv))
                            placed = True
                            break
                    if not placed:
                        label_groups[yv] = [(si, yv)]
                # Apply symmetric spread to groups with >1 label
                _label_spread = 11  # pixels between stacked labels
                for bucket_y, group in label_groups.items():
                    if len(group) <= 1:
                        continue
                    n = len(group)
                    for i, (si, actual_y) in enumerate(group):
                        offset = (i - (n - 1) / 2) * _label_spread
                        panel["series"][si]["label_nudge"] = offset

            dev_history["charts"] = {
                "x_positions": x_positions,
                "date_labels": [s["date_short"] for s in snapshots],
                "panels": panels,
            }
    except Exception:
        pass

    # Composite scores, divergence, archetype, carrying/red-flag tools
    # Determine position bucket for positional context
    _pos_bucket = None
    if valuation:
        _pos_bucket = valuation.get("bucket")
    _league_dir = str(get_cfg().league_dir)
    eval_data = _build_evaluation_data(rd, is_pitcher, _norm,
                                       position_bucket=_pos_bucket,
                                       league_dir=_league_dir)
    composite_score = eval_data["composite_score"]
    ceiling_score = eval_data["ceiling_score"]
    tool_only_score = eval_data["tool_only_score"]
    secondary_composite = eval_data["secondary_composite"]
    divergence = eval_data["divergence"]
    ceiling_divergence = eval_data["ceiling_divergence"]
    archetype = eval_data["archetype"]
    carrying_tools = eval_data["carrying_tools"]
    red_flag_tools = eval_data["red_flag_tools"]
    two_way_scores = eval_data["two_way_scores"]


    # Surplus breakdown
    surplus_detail = None
    outcome_probs = None
    try:
        if valuation.get("type") == "MLB":
            import contract_value as _cv
            cv = _cv.contract_value(pid)
            if cv and cv.get("breakdown"):
                surplus_detail = {
                    "rows": [{"year": b["year"], "age": b["age"], "war": round(b["war_base"], 1),
                              "value": b["market_value"],
                              "salary": b["salary_net"],
                              "surplus": b["surplus"]}
                             for b in cv["breakdown"]],
                    "total": {k: v for k, v in cv["total_surplus"].items()},
                    "flags": cv.get("flags", []),
                }
        elif valuation.get("type") == "prospect":
            import prospect_value as _pv
            fv = valuation.get("fv", 0)
            # Use fv_continuous (pre-rounding) for accurate surplus — matches fv_calc.py
            fv_for_surplus = valuation.get("fv_continuous") or fv
            bucket_val = valuation.get("bucket", "")
            level_val = valuation.get("level", level_str)
            _dr = valuation.get("def_rating")
            _comp_kw = dict(offensive_grade=eval_data.get("offensive_grade"),
                            offensive_ceiling=eval_data.get("offensive_ceiling"),
                            defensive_value=eval_data.get("defensive_value"),
                            durability_score=eval_data.get("durability_score"))
            pv = _pv.prospect_surplus(fv_for_surplus, age, level_val, bucket_val,
                                      ovr=valuation.get("ovr"), pot=valuation.get("pot"),
                                      def_rating=_dr)
            opt_total = _pv.prospect_surplus_with_option(
                fv_for_surplus, age, level_val, bucket_val,
                ovr=valuation.get("ovr"), pot=valuation.get("pot"),
                def_rating=_dr, **_comp_kw)
            # Use stored surplus as authoritative when available (computed by
            # fv_calc.py with full context: fv_continuous + component scores).
            # Fall back to live calculation for on-the-fly evaluations.
            stored_surplus = valuation.get("surplus")
            if stored_surplus and stored_surplus != 0:
                authoritative_total = round(stored_surplus * _money_divisor())
            else:
                authoritative_total = opt_total
            if pv and pv.get("breakdown"):
                cert = pv.get("certainty_mult", 1.0)
                scar = pv.get("scarcity_mult", 1.0)
                combined = pv["dev_discount"] * cert * scar
                raw_total = sum(b["market_value"] - b["salary"] for b in pv["breakdown"])
                eta_yr = int(get_cfg().year + pv["years_to_mlb"])
                surplus_detail = {
                    "rows": [{"year": eta_yr + b['control_year'] - 1, "age": b["player_age"],
                              "war": round(b["war"], 1),
                              "value": b["market_value"],
                              "salary": b["salary"],
                              "surplus": b["surplus"]}
                             for b in pv["breakdown"]],
                    "total": {"base": authoritative_total},
                    "flags": [f"ETA: {pv['years_to_mlb']:.1f} yrs"],
                    "discount_note": f"× {pv['dev_discount']:.0%} dev"
                                     + (f" × {scar:.2f} scarcity" if scar < 1.0 else "")
                                     + (f" × {cert:.2f} certainty" if cert != 1.0 else "")
                                     + f" = {_fmt_money_py(authoritative_total)}",
                    "raw_total": raw_total,
                }
            # Career outcome probabilities
            outcome_probs = _pv.career_outcome_probs(
                fv, age, level_val, bucket_val,
                ovr=valuation.get("ovr") or eval_data.get("composite_score"),
                pot=valuation.get("pot") or eval_data.get("ceiling_score"),
                def_rating=_dr, **_comp_kw)
        # MLB player who is also rookie-eligible (in prospect_fv)
        if valuation.get("type") == "MLB" and prospect_row and outcome_probs is None:
            import prospect_value as _pv
            _fv = prospect_row[1]
            _bucket = _display_pos(prospect_row[0])
            _level = prospect_row[4]
            _comp_kw = dict(offensive_grade=eval_data.get("offensive_grade"),
                            offensive_ceiling=eval_data.get("offensive_ceiling"),
                            defensive_value=eval_data.get("defensive_value"),
                            durability_score=eval_data.get("durability_score"))
            outcome_probs = _pv.career_outcome_probs(
                _fv, age, _level, _bucket,
                ovr=ratings["ovr"] if ratings else None,
                pot=ratings["pot"] if ratings else None,
                def_rating=valuation.get("def_rating"),
                **_comp_kw)
        # Affiliated minor leaguer with no prospect_fv row — this happens for
        # anyone over the batch pipeline's age<=24 prospect cutoff (fv_calc.py),
        # e.g. an org veteran in A-ball. Compute a valuation on the fly using
        # their real level/age rather than leaving the page blank. Also covers
        # true amateurs/draft-eligible players (who never had a real level).
        if not valuation and outcome_probs is None and level_str != 'MLB':
            try:
                import prospect_value as _pv
                from statsplusplus.utils.positions import assign_bucket, LEVEL_NORM_AGE; from statsplusplus.evaluation.fv import calc_fv_from_dict as calc_fv
                from statsplusplus.data.fv_calc import RATINGS_SQL, LEVEL_INT_KEY
                _conn2 = get_db()
                _rat = _conn2.execute(RATINGS_SQL + " AND r.player_id = ?", (pid,)).fetchone()
                if _rat:
                    _p = dict(_rat)
                    # RATINGS_SQL's Ovr/Pot are the legacy OOTP columns, which this
                    # evaluation engine never populates — use composite_score/
                    # true_ceiling (falling back to ceiling_score) instead, same
                    # override fv_calc.py applies for the batch pipeline.
                    _p["Ovr"] = _p.get("composite_score") or _p.get("Ovr") or 0
                    _p["Pot"] = _p.get("true_ceiling") or _p.get("ceiling_score") or _p.get("Pot") or 0
                    _role_map = {str(k): v for k, v in get_cfg().role_map.items()}
                    _p["_role"] = _role_map.get(str(_p.get("role") or 0), "position_player")
                    _p["Pos"] = str(_p.get("pos") or "")
                    _p["_is_pitcher"] = (_p["Pos"] == "P" or _p["_role"] in ("starter","reliever","closer"))
                    _bucket = assign_bucket(_p)
                    _p["_bucket"] = _bucket
                    _lvl_key = LEVEL_INT_KEY.get(int(_p.get("level") or 0), "a-short")
                    _p["_norm_age"] = LEVEL_NORM_AGE.get(_lvl_key, 22)
                    _p["_level"] = _lvl_key
                    _fv, _fv_plus = calc_fv(_p)
                    _fv_str = f"{_fv}+" if _fv_plus else str(_fv)
                    _fv_continuous = _p.get("_fv_continuous", _fv)
                    _dr = _p.get({'CF':'PotCF','SS':'PotSS','C':'PotC','2B':'Pot2B','3B':'Pot3B'}.get(_bucket, ""), 0)
                    valuation = {
                        "type": "prospect", "bucket": _display_pos(_bucket),
                        "fv": _fv, "fv_str": _fv_str,
                        "fv_continuous": _fv_continuous,
                        "ovr": _p["Ovr"], "pot": _p["Pot"],
                        "surplus": 0, "level": _lvl_key,
                    }
                    outcome_probs = _pv.career_outcome_probs(
                        _fv, age, _lvl_key, _bucket, ovr=_p["Ovr"], pot=_p["Pot"], def_rating=_dr)
                    pv = _pv.prospect_surplus(_fv_continuous, age, _lvl_key, _bucket,
                                              ovr=_p["Ovr"], pot=_p["Pot"], def_rating=_dr)
                    opt_total = _pv.prospect_surplus_with_option(
                        _fv_continuous, age, _lvl_key, _bucket,
                        ovr=_p["Ovr"], pot=_p["Pot"], def_rating=_dr)
                    if pv and pv.get("breakdown"):
                        cert = pv.get("certainty_mult", 1.0)
                        scar = pv.get("scarcity_mult", 1.0)
                        raw_total = sum(b["market_value"] - b["salary"] for b in pv["breakdown"])
                        eta_yr = int(get_cfg().year + pv["years_to_mlb"])
                        valuation["surplus"] = round(opt_total / _money_divisor(), 1)
                        surplus_detail = {
                            "rows": [{"year": eta_yr + b['control_year'] - 1, "age": b["player_age"],
                                      "war": round(b["war"], 1),
                                      "value": b["market_value"],
                                      "salary": b["salary"],
                                      "surplus": b["surplus"]}
                                     for b in pv["breakdown"]],
                            "total": {"base": opt_total},
                            "flags": [f"ETA: {pv['years_to_mlb']:.1f} yrs"],
                            "discount_note": f"× {pv['dev_discount']:.0%} dev"
                                             + (f" × {scar:.2f} scarcity" if scar < 1.0 else "")
                                             + (f" × {cert:.2f} certainty" if cert != 1.0 else "")
                                             + f" = {_fmt_money_py(opt_total)}",
                            "raw_total": raw_total,
                        }
            except Exception:
                pass
    except Exception:
        pass

    if surplus_detail and surplus_detail.get("rows"):
        try:
            cur_s, next_s, three_s = _surplus_horizons(surplus_detail["rows"], get_cfg().year)
            surplus_detail["current_year_surplus"] = cur_s
            surplus_detail["next_year_surplus"] = next_s
            surplus_detail["three_year_surplus"] = three_s
        except Exception:
            pass

    # Scouting summary
    summary = None
    league_dir = str(get_cfg().league_dir)
    for fname in ("prospects.json", "roster_notes.json"):
        path = os.path.join(league_dir, "history", fname)
        if os.path.exists(path):
            with open(path) as f:
                notes = json.load(f)
            entry = notes.get(str(pid))
            if entry and entry.get("summary"):
                summary = entry["summary"]
                break

    pos_str = ROLE_MAP.get(role, pos_map().get(pos, "?")) if is_pitcher else pos_map().get(pos, "?")

    # Two-way detection: has both meaningful batting and pitching stats
    is_two_way = bool(bat_stats and pit_stats and
                      any(s["pa"] >= 30 for s in bat_stats) and
                      any(s["ip"] >= 15 for s in pit_stats))

    # For two-way players, find their hitting position
    if is_two_way and is_pitcher:
        _POS_LABELS = {3: "1B", 4: "2B", 5: "3B", 6: "SS", 7: "LF", 8: "CF", 9: "RF", 10: "DH", 2: "C"}
        _conn2 = get_db()
        _field = _conn2.execute(
            "SELECT position, SUM(g) as games FROM mlb_fielding_stats WHERE player_id=? AND position != 1 GROUP BY position ORDER BY games DESC LIMIT 1",
            (pid,)).fetchone()
        if _field:
            pos_str = f"SP/{_POS_LABELS.get(_field[0], 'DH')}"
        else:
            pos_str = "SP/DH"

    percentiles = None
    pctile_splits = {}
    bat_percentiles = None
    bat_pctile_splits = {}
    fielding_pctiles = None
    pctile_year = None
    pctile_years_available = []
    pctile_levels = []  # [(level_int, label)] where player has stats
    pctile_level = None  # currently displayed level
    pctile_year_levels = {}  # {year: [(level_int, label)]} — for JS to drive dropdown interaction

    # Build the year→levels map: for each year, which levels have data
    _all_levels = available_pctile_levels(pid, is_pitcher=is_pitcher)
    _all_years_set = set()
    for lv, lv_label in _all_levels:
        yrs = available_pctile_years(pid, is_pitcher=is_pitcher, level=lv)
        for yr in yrs:
            _all_years_set.add(yr)
            if yr not in pctile_year_levels:
                pctile_year_levels[yr] = []
            pctile_year_levels[yr].append((lv, lv_label))
    pctile_years_available = sorted(_all_years_set, reverse=True)

    # Default: most recent year (prefer current year if data exists)
    current_year = get_cfg().year
    if current_year in pctile_year_levels:
        pctile_year = current_year
    elif pctile_years_available:
        pctile_year = pctile_years_available[0]

    # Default level within the selected year: player's current level if available, else highest
    if pctile_year:
        _year_levels = pctile_year_levels.get(pctile_year, [])
        pctile_levels = _year_levels
        _player_level = int(level) if level and str(level).isdigit() else None
        if _player_level and any(lv == _player_level for lv, _ in _year_levels):
            pctile_level = _player_level
        elif _year_levels:
            pctile_level = _year_levels[0][0]

    if pctile_level is not None and pctile_year is not None:
        if not is_pitcher:
            percentiles = get_hitter_percentiles(pid, level=pctile_level, year=pctile_year)
            if percentiles:
                for sid, key in ((2, "vl"), (3, "vr")):
                    sp = get_hitter_percentiles(pid, split_id=sid, level=pctile_level, year=pctile_year)
                    if sp:
                        pctile_splits[key] = sp
        elif is_pitcher:
            percentiles = get_pitcher_percentiles(pid, level=pctile_level, year=pctile_year)
            if percentiles:
                for sid, key in ((2, "vl"), (3, "vr")):
                    sp = get_pitcher_percentiles(pid, split_id=sid, level=pctile_level, year=pctile_year)
                    if sp:
                        pctile_splits[key] = sp
                # Two-way: also get hitter percentiles
                if is_two_way:
                    bat_percentiles = get_hitter_percentiles(pid, level=pctile_level, year=pctile_year)
                    if bat_percentiles:
                        for sid, key in ((2, "vl"), (3, "vr")):
                            sp = get_hitter_percentiles(pid, split_id=sid, level=pctile_level, year=pctile_year)
                            if sp:
                                bat_pctile_splits[key] = sp

    if fielding_stats:
        fielding_pctiles = get_fielding_percentiles(pid)

    # Available fielding percentile years
    fld_pctile_years = []
    if fielding_stats:
        conn = get_db()
        fld_pctile_years = [r[0] for r in conn.execute(
            "SELECT DISTINCT year FROM mlb_fielding_stats WHERE player_id=? ORDER BY year DESC",
            (pid,)).fetchall()]

    # Percentile history for Advanced tab
    pctile_history = get_percentile_history(pid, is_pitcher=is_pitcher)
    pctile_history_all = get_percentile_history_all_levels(pid, is_pitcher=is_pitcher)
    fld_pctile_history = get_fielding_percentile_history(pid) if fielding_stats else None

    # Prospect comps
    prospect_comps = None
    comp_stats = None
    if valuation and valuation.get("type") == "prospect":
        from queries import get_prospect_comps, get_prospect_comp_stats
        prospect_comps = get_prospect_comps(pid)
        try:
            comp_stats = get_prospect_comp_stats(pid)
        except Exception:
            pass
    elif valuation and valuation.get("type") == "MLB" and prospect_row:
        from queries import get_prospect_comps
        prospect_comps = get_prospect_comps(pid)

    # PAP score (MLB players only — from actual production)
    pap = None
    if surplus_row and (bat_stats or pit_stats):
        _war = 0
        if bat_stats and bat_stats[-1]["year"] == _pap_year:
            _war += bat_stats[-1]["war"]
        if pit_stats and pit_stats[-1]["year"] == _pap_year:
            _war += pit_stats[-1]["war"]
        _dpw = _dollars_per_war()
        pap = calc_pap(_war, _pap_sal, _pap_tg, _dpw)

    # MLB context: percentile + tier for composite/ceiling vs MLB at position
    mlb_ctx = None
    _ctx_bucket = _pos_bucket
    if not _ctx_bucket and composite_score is not None:
        # Derive bucket from player position/role for prospects
        _POS_TO_BUCKET = {"2": "C", "3": "1B", "4": "2B", "5": "3B", "6": "SS",
                          "7": "COF", "8": "CF", "9": "COF", "10": "COF"}
        if role in (11, 12, 13):
            _ctx_bucket = "SP" if role == 11 else "RP"
        else:
            _ctx_bucket = _POS_TO_BUCKET.get(str(pos), "COF")
    if composite_score is not None and _ctx_bucket:
        _internal_bucket = "COF" if _ctx_bucket == "OF" else _ctx_bucket
        try:
            _ctx_conn = get_db()
            mlb_ctx = _mlb_context(_ctx_conn, _internal_bucket, composite_score,
                                   eval_data.get("true_ceiling") or ceiling_score)
        except Exception:
            pass

    # ── Insights ──
    insights = _compute_insights(rd, is_pitcher, composite_score, _norm)

    # ── Minor league stats ──
    milb_bat_stats = []
    milb_pit_stats = []
    try:
        _milb_conn = get_db()
        # Build league_id → name/level mapping from league_settings.json
        _lg_map = {}
        _settings_path = get_cfg().league_dir / "config" / "league_settings.json"
        if _settings_path.exists():
            import json as _json_m
            _ls = _json_m.loads(_settings_path.read_text())
            for _ml in _ls.get("minor_leagues", []):
                _lg_map[_ml["lid"]] = {"name": _ml["name"], "level": _ml["level"]}

        _milb_bat = _milb_conn.execute("""
            SELECT b.year, b.league_id, b.ab, b.h, b.hr, b.rbi, b.bb, b.k, b.sb,
                   b.pa, b.war, b.g, b.d, b.t, b.hbp, b.sf, b.team_id,
                   t.name AS team_name
            FROM batting_stats b
            LEFT JOIN teams t ON b.team_id = t.team_id
            WHERE b.player_id=? AND b.split_id=1 AND b.league_id IS NOT NULL
            ORDER BY b.year, b.league_id
        """, (pid,)).fetchall()
        for r in _milb_bat:
            ab = r["ab"] or 0
            h = r["h"] or 0
            hr = r["hr"] or 0
            bb = r["bb"] or 0
            k = r["k"] or 0
            pa = r["pa"] or 0
            hbp = r["hbp"] or 0
            sf = r["sf"] or 0
            _lid = r["league_id"]
            _lg_info = _lg_map.get(_lid, {})
            _level_num = _lg_info.get("level", 0)
            _level_label = level_map().get(str(_level_num), f"L{_level_num}")
            _team_name = r["team_name"] or ""
            avg = h / ab if ab else 0
            obp = (h + bb + hbp) / (ab + bb + hbp + sf) if (ab + bb + hbp + sf) else 0
            slg = (h + (r["d"] or 0) + 2*(r["t"] or 0) + 3*hr) / ab if ab else 0
            babip_denom = ab - k - hr + sf
            milb_bat_stats.append({
                "year": r["year"], "league_id": _lid,
                "league_name": _lg_info.get("name", f"League {_lid}"),
                "level": _level_num,
                "level_label": _level_label,
                "team_name": _team_name,
                "g": r["g"] or 0, "pa": pa,
                "avg": round(avg, 3),
                "obp": round(obp, 3),
                "slg": round(slg, 3),
                "ops": round(obp + slg, 3),
                "iso": round(slg - avg, 3),
                "bb_pct": round(bb / pa * 100, 1) if pa else 0,
                "so_pct": round(k / pa * 100, 1) if pa else 0,
                "babip": round((h - hr) / babip_denom, 3) if babip_denom > 0 else 0,
                "hr": hr, "rbi": r["rbi"] or 0, "sb": r["sb"] or 0,
                "war": round(r["war"], 1) if r["war"] else 0,
            })

        _milb_pit = _milb_conn.execute("""
            SELECT p.year, p.league_id, p.ip, p.era, p.k, p.bb, p.w, p.l, p.sv,
                   p.war, p.gs, p.g, p.hra, p.ha, p.er, p.r, p.bf, p.team_id,
                   p.hp, p.gb, p.fb, p.hld,
                   t.name AS team_name
            FROM pitching_stats p
            LEFT JOIN teams t ON p.team_id = t.team_id
            WHERE p.player_id=? AND p.split_id=1 AND p.league_id IS NOT NULL
            ORDER BY p.year, p.league_id
        """, (pid,)).fetchall()
        for r in _milb_pit:
            ip = r["ip"] or 0
            k = r["k"] or 0
            bb = r["bb"] or 0
            bf = r["bf"] or 0
            hra = r["hra"] or 0
            ha = r["ha"] or 0
            hp = r["hp"] or 0
            gb = r["gb"] or 0
            fb = r["fb"] or 0
            _lid = r["league_id"]
            _lg_info = _lg_map.get(_lid, {})
            _level_num = _lg_info.get("level", 0)
            _level_label = level_map().get(str(_level_num), f"L{_level_num}")
            _team_name = r["team_name"] or ""
            k_pct = k / bf * 100 if bf else 0
            bb_pct = bb / bf * 100 if bf else 0
            gb_pct = 100.0 * gb / (gb + fb) if (gb + fb) > 0 else 0
            babip_denom = bf - k - hra - bb - hp
            babip = (ha - hra) / babip_denom if babip_denom > 0 else 0
            milb_pit_stats.append({
                "year": r["year"], "league_id": _lid,
                "league_name": _lg_info.get("name", f"League {_lid}"),
                "level": _level_num,
                "level_label": _level_label,
                "team_name": _team_name,
                "g": r["g"] or 0, "gs": r["gs"] or 0,
                "ip": round(ip, 1), "era": round(r["era"], 2) if r["era"] else 0,
                "k_pct": round(k_pct, 1), "bb_pct": round(bb_pct, 1),
                "k_bb_pct": round(k_pct - bb_pct, 1),
                "gb_pct": round(gb_pct, 1),
                "babip": round(babip, 3),
                "w": r["w"] or 0, "l": r["l"] or 0, "sv": r["sv"] or 0,
                "hld": r["hld"] or 0,
                "war": round(r["war"], 1) if r["war"] else 0,
                "k9": round(k * 9 / ip, 1) if ip else 0,
                "bb9": round(bb * 9 / ip, 1) if ip else 0,
            })
    except Exception:
        pass

    # ── MiLB performance context (for Scout vs Performance panel) ──
    milb_perf = None
    try:
        if level != 1 or (age and age <= 25):
            import sys as _sys
            _sys.path.insert(0, "scripts") if "scripts" not in _sys.path else None
            from statsplusplus.data.evaluation_engine import _load_milb_stat_seasons, _load_milb_averages
            from statsplusplus.evaluation.fv import compute_performance_adjusted_ceiling, compute_stat_risk_modifier
            _league_dir = get_cfg().league_dir
            _ma = _load_milb_averages(_league_dir)
            if _ma:
                _mp_conn = get_db()
                _milb_s = _load_milb_stat_seasons(_mp_conn, pid, is_pitcher, _ma)
                if _milb_s:
                    import json as _json_perf
                    _mw_path = _league_dir / "config" / "model_weights.json"
                    _disc_map = {}
                    _norm_ages = {}
                    if _mw_path.exists():
                        _mw = _json_perf.loads(_mw_path.read_text())
                        _disc_map = _mw.get("MILB_LEVEL_DISCOUNTS", {})
                        _norm_ages = _mw.get("MILB_NORM_AGES", {})
                    _disc_key = "pitcher" if is_pitcher else "hitter"
                    _ws = 0.0
                    _tw = 0.0
                    _best_level = None
                    _best_ops = None
                    for _ms in _milb_s[:3]:
                        _lv = str(_ms.get("level", 0))
                        _d = float(_disc_map.get(_disc_key, {}).get(_lv, 0.0))
                        if _d <= 0:
                            continue
                        _pa = _ms.get("pa", 0) if not is_pitcher else _ms.get("ip", 0) * 4.3
                        _w = _pa * _d
                        _ws += _ms["stat_2080"] * _w
                        _tw += _w
                        if _best_level is None:
                            _best_level = int(_lv)
                            _best_ops = _ms.get("ops_plus", _ms.get("era_minus_inv"))
                    if _tw > 0:
                        _stat_2080 = _ws / _tw
                        _eff_pa = _tw
                        _p_age = age or 22
                        _norm_age = int(_norm_ages.get(str(_best_level), 23))
                        _tonly = tool_only_score or composite_score or 0
                        _raw_ceil = eval_data.get("true_ceiling") or ceiling_score or 0
                        _pac = compute_performance_adjusted_ceiling(
                            _raw_ceil, _stat_2080, _p_age, _norm_age, _eff_pa, _tonly
                        )
                        _risk_mod = compute_stat_risk_modifier(
                            _stat_2080, _p_age, _norm_age, _eff_pa, _tonly
                        )
                        # Promotion readiness
                        _promo_ready = (
                            _p_age <= _norm_age
                            and _stat_2080 >= 55  # above-average production at level
                            and _eff_pa >= 50
                            and (_tonly or 0) >= 45  # tools support next level
                        )
                        _level_names = {2: "AAA", 3: "AA", 4: "A", 6: "Rookie"}
                        milb_perf = {
                            "stat_2080": round(_stat_2080, 1),
                            "tool_only": _tonly,
                            "delta": round(_stat_2080 - _tonly, 1),
                            "pac": _pac,
                            "raw_ceiling": _raw_ceil,
                            "pac_delta": _pac - _raw_ceil,
                            "risk_modifier": round(_risk_mod, 3),
                            "effective_pa": round(_eff_pa),
                            "level": _best_level,
                            "level_name": _level_names.get(_best_level, f"Lv{_best_level}"),
                            "age_vs_level": _norm_age - _p_age,  # positive = young for level
                            "promo_ready": _promo_ready,
                            "ops_plus": round(_best_ops) if _best_ops else None,
                        }
    except Exception as _mp_err:
        import logging as _mp_log
        _mp_log.getLogger("statspp").warning("milb_perf computation failed for pid=%s: %s", pid, _mp_err)

    # ── MLB career totals ─────────────────────────────────────────────────
    bat_career = None
    if bat_stats and len(bat_stats) > 1:
        _c_ab = sum(s["ab"] for s in bat_stats)
        _c_h = sum(s["h"] for s in bat_stats)
        _c_d = sum(s.get("_d", 0) for s in bat_stats)
        _c_t = sum(s.get("_t", 0) for s in bat_stats)
        _c_hr = sum(s["hr"] for s in bat_stats)
        _c_rbi = sum(s["rbi"] for s in bat_stats)
        _c_bb = sum(s["bb"] for s in bat_stats)
        _c_k = sum(s["k"] for s in bat_stats)
        _c_sb = sum(s["sb"] for s in bat_stats)
        _c_cs = sum(s["cs"] for s in bat_stats)
        _c_pa = sum(s["pa"] for s in bat_stats)
        _c_war = sum(s["war"] for s in bat_stats)
        _c_g = sum(s["g"] for s in bat_stats)
        _c_hbp = sum(s.get("_hbp", 0) for s in bat_stats)
        _c_sf = sum(s.get("_sf", 0) for s in bat_stats)
        _c_avg = _c_h / _c_ab if _c_ab else 0
        _c_obp = (_c_h + _c_bb + _c_hbp) / (_c_ab + _c_bb + _c_hbp + _c_sf) if (_c_ab + _c_bb + _c_hbp + _c_sf) else 0
        _c_slg = (_c_h + _c_d + 2 * _c_t + 3 * _c_hr) / _c_ab if _c_ab else 0
        _c_ops = _c_obp + _c_slg
        _c_iso = _c_slg - _c_avg
        _c_babip_d = _c_ab - _c_k - _c_hr + _c_sf
        _c_babip = (_c_h - _c_hr) / _c_babip_d if _c_babip_d > 0 else 0
        _c_bb_pct = _c_bb / _c_pa * 100 if _c_pa else 0
        _c_so_pct = _c_k / _c_pa * 100 if _c_pa else 0
        _c_ops_plus = round(_c_ops / lg_ops * 100) if lg_ops and _c_ops else 0
        bat_career = {
            "g": _c_g, "pa": _c_pa, "hr": _c_hr, "rbi": _c_rbi, "sb": _c_sb, "cs": _c_cs,
            "war": round(_c_war, 1), "avg": _c_avg, "obp": _c_obp, "slg": _c_slg,
            "ops": _c_ops, "iso": _c_iso, "babip": _c_babip,
            "bb_pct": _c_bb_pct, "so_pct": _c_so_pct, "ops_plus": _c_ops_plus,
        }

    pit_career = None
    if pit_stats and len(pit_stats) > 1:
        _c_ip = sum(s["ip"] for s in pit_stats)
        _c_k = sum(s["k"] for s in pit_stats)
        _c_bb = sum(s["bb"] for s in pit_stats)
        _c_w = sum(s["w"] for s in pit_stats)
        _c_l = sum(s["l"] for s in pit_stats)
        _c_sv = sum(s["sv"] for s in pit_stats)
        _c_war = sum(s["war"] for s in pit_stats)
        _c_gs = sum(s["gs"] for s in pit_stats)
        _c_g = sum(s["g"] for s in pit_stats)
        _c_hld = sum(s["hld"] for s in pit_stats)
        _c_er = sum(s.get("_er", 0) for s in pit_stats)
        _c_hra = sum(s.get("_hra", 0) for s in pit_stats)
        _c_bf = sum(s.get("_bf", 0) for s in pit_stats)
        _c_hp = sum(s.get("_hp", 0) for s in pit_stats)
        _c_ha = sum(s.get("_ha", 0) for s in pit_stats)
        _c_gb = sum(s.get("_gb", 0) for s in pit_stats)
        _c_fb = sum(s.get("_fb", 0) for s in pit_stats)
        _c_era = _c_er * 27 / (_c_ip * 3) if _c_ip else 0
        _c_fip = ((13 * _c_hra + 3 * (_c_bb + _c_hp) - 2 * _c_k) / _c_ip + fip_const) if _c_ip else 0
        _c_era_plus = round(lg_era / _c_era * 100) if _c_era else 0
        _c_babip_d = _c_bf - _c_k - _c_hra - _c_bb - _c_hp
        _c_babip = (_c_ha - _c_hra) / _c_babip_d if _c_babip_d > 0 else 0
        _c_k_pct = _c_k / _c_bf * 100 if _c_bf else 0
        _c_bb_pct = _c_bb / _c_bf * 100 if _c_bf else 0
        _c_k_bb = _c_k_pct - _c_bb_pct
        _c_gb_pct = _c_gb / (_c_gb + _c_fb) * 100 if (_c_gb + _c_fb) else 0
        _c_siera_k = _c_k / _c_bf if _c_bf else 0
        _c_siera_bb = _c_bb / _c_bf if _c_bf else 0
        _c_siera = (6.145 - 16.986 * _c_siera_k + 11.434 * _c_siera_bb
                    + 7.653 * _c_siera_k**2 + 6.664 * _c_siera_bb**2 + 0.9) if _c_bf else 0
        pit_career = {
            "g": _c_g, "gs": _c_gs, "ip": _c_ip, "era": _c_era, "era_plus": _c_era_plus,
            "fip": _c_fip, "siera": _c_siera, "k_pct": _c_k_pct, "bb_pct": _c_bb_pct,
            "k_bb_pct": _c_k_bb, "gb_pct": _c_gb_pct, "babip": _c_babip,
            "w": _c_w, "l": _c_l, "sv": _c_sv, "hld": _c_hld, "war": round(_c_war, 1),
        }

    # ── Promotion readiness ───────────────────────────────────────────────
    promotion_readiness = None
    demotion_risk = None
    if level_str and level_str != "MLB":
        try:
            from promotion_readiness import compute_promotion_readiness, compute_demotion_risk
            from statsplusplus.data import db as _pr_db
            _pr_league_dir = get_cfg().league_dir
            _pr_conn = _pr_db.get_connection(_pr_league_dir)
            promotion_readiness = compute_promotion_readiness(pid, _pr_conn, _pr_league_dir)
            demotion_risk = compute_demotion_risk(pid, _pr_conn, _pr_league_dir)
            _pr_conn.close()
        except Exception:
            pass
    elif level_str == "MLB":
        try:
            from promotion_readiness import compute_demotion_risk
            from statsplusplus.data import db as _pr_db
            _pr_league_dir = get_cfg().league_dir
            _pr_conn = _pr_db.get_connection(_pr_league_dir)
            demotion_risk = compute_demotion_risk(pid, _pr_conn, _pr_league_dir)
            _pr_conn.close()
        except Exception:
            pass

    return {
        "pid": pid, "player_id": pid, "name": name, "age": age, "pos": pos_str,
        "year": get_cfg().year,
        "team": team_names_map().get(org_id, "?"), "team_abbr": team_abbr_map().get(org_id, "?"), "tid": org_id,
        "actual_team_id": team_id if team_id != org_id else None,
        "level": level_str, "is_pitcher": is_pitcher, "is_two_way": is_two_way,
        "player_status": player_status,
        "ratings": ratings, "hit_ratings": hit_ratings, "valuation": valuation, "contract": contract,
        "bat_stats": bat_stats, "pit_stats": pit_stats, "summary": summary,
        "bat_career": bat_career, "pit_career": pit_career,
        "bat_splits": bat_splits, "pit_splits": pit_splits,
        "surplus_detail": surplus_detail, "outcome_probs": outcome_probs, "percentiles": percentiles,
        "pctile_splits": pctile_splits, "fielding_stats": fielding_stats, "fielding_career": fielding_career,
        "fielding_pctiles": fielding_pctiles, "fld_pctile_years": fld_pctile_years,
        "bat_percentiles": bat_percentiles, "bat_pctile_splits": bat_pctile_splits,
        "pctile_year": pctile_year, "pctile_years": pctile_years_available,
        "pctile_history": pctile_history, "pctile_history_all": pctile_history_all, "fld_pctile_history": fld_pctile_history,
        "prospect_comps": prospect_comps, "comp_stats": comp_stats, "pap": pap,
        "snapshot_deltas": snapshot_deltas,
        "dev_history": dev_history,
        "composite_score": composite_score,
        "ceiling_score": ceiling_score,
        "true_ceiling": eval_data.get("true_ceiling"),
        "tool_only_score": tool_only_score,
        "secondary_composite": secondary_composite,
        "divergence": divergence,
        "ceiling_divergence": eval_data.get("ceiling_divergence"),
        "archetype": archetype,
        "carrying_tools": carrying_tools,
        "red_flag_tools": red_flag_tools,
        "two_way_scores": two_way_scores,
        "offensive_grade": eval_data["offensive_grade"],
        "baserunning_value": eval_data["baserunning_value"],
        "defensive_value": eval_data["defensive_value"],
        "durability_score": eval_data["durability_score"],
        "offensive_ceiling": eval_data["offensive_ceiling"],
        "carrying_tool_bonus": eval_data["carrying_tool_bonus"],
        "carrying_tool_breakdown": eval_data["carrying_tool_breakdown"],
        "positional_percentile": eval_data["positional_percentile"],
        "positional_median": eval_data["positional_median"],
        "mlb_context": mlb_ctx,
        "insights": insights,
        "milb_bat_stats": milb_bat_stats,
        "milb_pit_stats": milb_pit_stats,
        "pctile_levels": pctile_levels,
        "pctile_level": pctile_level,
        "pctile_year_levels": pctile_year_levels,
        "milb_perf": milb_perf,
        "promotion_readiness": promotion_readiness,
        "demotion_risk": demotion_risk,
    }


def get_player_popup(pid):
    """Lightweight player data for hover popup."""
    conn = get_db()
    year = get_cfg().year

    p = conn.execute(
        "SELECT name, age, team_id, parent_team_id, level, pos, role FROM players WHERE player_id=?",
        (pid,)
    ).fetchone()
    if not p:
        return None

    is_pitcher = p["role"] in (11, 12, 13)
    org_id = p["team_id"] if p["parent_team_id"] == 0 else p["parent_team_id"]

    r = conn.execute(
        "SELECT ovr, pot, height, bats, throws, "
        "cntct, gap, pow, eye, ks, speed, "
        "stf, mov, ctrl, ctrl_r, ctrl_l, stm, vel, "
        "fst, snk, crv, sld, chg, splt, cutt, cir_chg, scr, frk, kncrv, knbl, "
        "pot_fst, pot_snk, pot_crv, pot_sld, pot_chg, pot_splt, pot_cutt, pot_cir_chg, pot_scr, pot_frk, pot_kncrv, pot_knbl, "
        "pot_cntct, pot_gap, pot_pow, pot_eye, pot_ks, "
        "pot_stf, pot_mov, pot_ctrl, "
        "c, ss, second_b, third_b, first_b, lf, cf, rf "
        "FROM ratings WHERE player_id=? ORDER BY snapshot_date DESC LIMIT 1",
        (pid,)
    ).fetchone()

    # Current year stats
    stats = None
    bat_stats = None
    if is_pitcher:
        s = conn.execute(
            "SELECT SUM(ip), SUM(era*ip)/NULLIF(SUM(ip),0), SUM(k), SUM(bb), SUM(war), SUM(sv), SUM(hld), SUM(g), SUM(gs) "
            "FROM mlb_pitching_stats WHERE player_id=? AND year=? AND split_id=1",
            (pid, year)
        ).fetchone()
        if s and s[0]:
            stats = {"ip": s[0], "era": round(s[1], 2) if s[1] else None,
                     "k": s[2], "bb": s[3], "war": round(s[4], 1) if s[4] else 0,
                     "sv": s[5], "hld": s[6], "g": s[7], "gs": s[8]}
        # Two-way: also fetch batting stats
        bs = conn.execute(
            "SELECT SUM(pa), SUM(h)*1.0/NULLIF(SUM(ab),0), "
            "(SUM(h)+SUM(bb)+SUM(hbp))*1.0/NULLIF(SUM(ab)+SUM(bb)+SUM(hbp)+SUM(sf),0), "
            "(SUM(h)+SUM(d)+2*SUM(t)+3*SUM(hr))*1.0/NULLIF(SUM(ab),0), "
            "SUM(hr), SUM(war), SUM(sb) "
            "FROM mlb_batting_stats WHERE player_id=? AND year=? AND split_id=1",
            (pid, year)
        ).fetchone()
        if bs and (bs[0] or 0) >= 30:
            bat_stats = {"pa": bs[0], "avg": round(bs[1], 3) if bs[1] else None,
                         "obp": round(bs[2], 3) if bs[2] else None,
                         "slg": round(bs[3], 3) if bs[3] else None,
                         "hr": bs[4], "war": round(bs[5], 1) if bs[5] else 0, "sb": bs[6]}
    else:
        s = conn.execute(
            "SELECT SUM(pa), SUM(h)*1.0/NULLIF(SUM(ab),0), "
            "(SUM(h)+SUM(bb)+SUM(hbp))*1.0/NULLIF(SUM(ab)+SUM(bb)+SUM(hbp)+SUM(sf),0), "
            "(SUM(h)+SUM(d)+2*SUM(t)+3*SUM(hr))*1.0/NULLIF(SUM(ab),0), "
            "SUM(hr), SUM(war), SUM(sb) "
            "FROM mlb_batting_stats WHERE player_id=? AND year=? AND split_id=1",
            (pid, year)
        ).fetchone()
        if s and s[0]:
            stats = {"pa": s[0], "avg": round(s[1], 3) if s[1] else None,
                     "obp": round(s[2], 3) if s[2] else None,
                     "slg": round(s[3], 3) if s[3] else None,
                     "hr": s[4], "war": round(s[5], 1) if s[5] else 0, "sb": s[6]}

    # Surplus
    ed = conn.execute("SELECT MAX(eval_date) FROM player_surplus").fetchone()[0]
    sur = conn.execute(
        "SELECT surplus, surplus_yr1, bucket FROM player_surplus WHERE player_id=? AND eval_date=?", (pid, ed)
    ).fetchone()
    # Prospect FV
    fv_row = conn.execute(
        "SELECT fv, fv_str, level, bucket FROM prospect_fv WHERE player_id=? AND eval_date=?", (pid, ed)
    ).fetchone()

    # PAP from actual production
    _pap = None
    if sur:
        _war = 0
        if stats and "war" in stats:
            _war += stats["war"] or 0
        if bat_stats and "war" in bat_stats:
            _war += bat_stats["war"] or 0
        _tg = conn.execute(
            "SELECT COUNT(*) FROM games WHERE (home_team=? OR away_team=?) AND date>=? AND played=1",
            (org_id, org_id, f"{year}-01-01")).fetchone()[0]
        _dpw = _dollars_per_war()
        _sal = conn.execute("SELECT salary_0 FROM contracts WHERE player_id=?", (pid,)).fetchone()
        _pap = calc_pap(_war, _sal[0] if _sal else 0, _tg, _dpw)


    pos_str = ROLE_MAP.get(p["role"], pos_map().get(p["pos"], "?")) if is_pitcher else pos_map().get(p["pos"], "?")
    level_str = level_map().get(str(p["level"]), str(p["level"]))
    team_name = team_abbr_map().get(org_id, team_names_map().get(org_id, "?"))

    ratings = None
    if r:
        n = _norm
        if is_pitcher:
            ctrl = r["ctrl"] or (round((r["ctrl_r"] + r["ctrl_l"]) / 2) if r["ctrl_r"] is not None else None)
            pot_ctrl_val = r["pot_ctrl"]
            pitches = []
            for fld, name in [("fst","FB"),("snk","SI"),("crv","CB"),("sld","SL"),
                               ("chg","CH"),("splt","SPL"),("cutt","CUT"),("cir_chg","CC"),
                               ("scr","SCR"),("frk","FRK"),("kncrv","KC"),("knbl","KN")]:
                v = r[fld]
                pot_v = r["pot_" + fld] if ("pot_" + fld) in r.keys() else None
                if (v and v >= 25) or (pot_v and pot_v >= 25):
                    pitches.append({"name": name, "cur": n(v or 0), "pot": n(pot_v or v or 0)})
            pitches.sort(key=lambda x: x["pot"], reverse=True)
            ratings = {
                "stf": [n(r["stf"]), n(r["pot_stf"])] if r["stf"] else None,
                "mov": [n(r["mov"]), n(r["pot_mov"])] if r["mov"] else None,
                "ctl": [n(ctrl), n(pot_ctrl_val)] if ctrl else None,
                "stm": n(r["stm"]) if r["stm"] else None,
                "vel": r["vel"],
                "pitches": pitches[:4],
            }
            # Two-way: also include batting tools
            if bat_stats and r["cntct"] and r["cntct"] >= 20:
                ratings["bat"] = {
                    "con": [n(r["cntct"]), n(r["pot_cntct"])],
                    "pow": [n(r["pow"]), n(r["pot_pow"])],
                    "eye": [n(r["eye"]), n(r["pot_eye"])],
                    "spd": n(r["speed"]) if r["speed"] else None,
                }
        else:
            ratings = {
                "con": [n(r["cntct"]), n(r["pot_cntct"])] if r["cntct"] else None,
                "pow": [n(r["pow"]), n(r["pot_pow"])] if r["pow"] else None,
                "eye": [n(r["eye"]), n(r["pot_eye"])] if r["eye"] else None,
                "spd": n(r["speed"]) if r["speed"] else None,
            }
            # Primary position defense
            _def_map = {"c":"C","ss":"SS","second_b":"2B","third_b":"3B","first_b":"1B","lf":"LF","cf":"CF","rf":"RF"}
            best_def_pos = None
            best_def_grade = 0
            for col, label in _def_map.items():
                pot_col = "pot_" + col
                v = (r[pot_col] if pot_col in r.keys() else None) or (r[col] if col in r.keys() else None) or 0
                g = n(v)
                if g and g > best_def_grade:
                    best_def_grade = g
                    best_def_pos = label
            if best_def_pos and best_def_grade > 20:
                ratings["def"] = {"pos": best_def_pos, "grade": best_def_grade}

    result = {
        "name": p["name"], "age": p["age"], "pos": pos_str,
        "level": level_str, "team": team_name, "tid": org_id, "is_pitcher": is_pitcher,
        "ovr": r["ovr"] if r else None, "pot": r["pot"] if r else None,
        "height": _height_str(r["height"]) if r and r["height"] else None,
        "bats": r["bats"] if r else None, "throws": r["throws"] if r else None,
        "stats": stats, "bat_stats": bat_stats, "ratings": ratings,
        "is_two_way": bat_stats is not None,
        "surplus": round(sur["surplus"] / _money_divisor(), 1) if sur and sur["surplus"] else None,
        "pap": _pap,
        "bucket": (sur["bucket"] if sur else fv_row["bucket"] if fv_row else None),
        "fv": fv_row["fv_str"] if fv_row else None,
    }
    return result
