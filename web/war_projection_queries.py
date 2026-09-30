"""Editable, role-aware 1955 PHA WAR planning projections.

All component estimates are in wins; historical total WAR is used only to
anchor pitcher rates. Hitter components are estimated independently so that
observed total WAR is never added to fielding or batting a second time.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

from statsplusplus.data.evaluation_engine import load_tool_weights
from statsplusplus.evaluation.facet_runs import (
    baserunning_runs, facet_stat_confidence, fielding_runs,
)
from statsplusplus.evaluation.war import peak_war_from_score
from statsplusplus.evaluation.constants import load_model_weights
from statsplusplus.evaluation.woba import player_woba
from statsplusplus.utils.positions import POSITIONAL_WAR_ADJUSTMENTS, LEVEL_DISPLAY_MAP
from web_league_context import get_cfg, get_db


TEAM_ID = 6
YEAR = 1955
DEFAULT_TEAM_AB = 5279          # PHA, 1954; includes pitcher batting
DEFAULT_POSITION_AB = 4848     # 8 positions at 606 AB; rest are pitcher/PH AB
DEFAULT_PH_AB = 64             # dedicated Thorne pinch-hit work
DEFAULT_TEAM_IP = 1393        # PHA, 1954
PITCHER_BATTING_AB = DEFAULT_TEAM_AB - DEFAULT_POSITION_AB - DEFAULT_PH_AB
BASE_RHP_SHARE = 3986 / 6028  # PHA, 1954 plate appearances vs RHP

# Exactly the 25-player plan agreed with the GM. AB sum = 4,912; IP sum = 1,393.
# The vR fraction is *this player's expected AB mix*, not the team-wide
# schedule mix; specialists are intentionally given asymmetric fractions.
SEED = {
    24747: ("SP1", "SP", 250, None),
    25181: ("SP2", "SP", 180, None),
    26188: ("SP3", "SP", 235, None),
    23856: ("SP4", "SP", 250, None),
    23718: ("Closer", "RP", 65, None),
    23663: ("High leverage", "RP", 65, None),
    23601: ("High leverage", "RP", 65, None),
    25360: ("High leverage L", "RP", 60, None),
    24627: ("Long relief L", "RP", 115, None),
    22312: ("Long relief R", "RP", 108, None),
    23752: ("C vs R", "C", 375, .88),
    24233: ("C vs L", "C", 231, .31),
    25260: ("1B vs R", "1B", 360, .96),
    23770: ("3B vs R / 1B vs L", "3B", 540, .67),
    24800: ("3B vs L", "3B", 185, .10),
    24790: ("Everyday 2B", "2B", 545, BASE_RHP_SHARE),
    25253: ("Everyday SS", "SS", 560, BASE_RHP_SHARE),
    25180: ("Everyday LF", "LF", 535, BASE_RHP_SHARE),
    25625: ("CF vs R", "CF", 400, .95),
    24216: ("CF vs L", "CF", 206, .10),
    27407: ("Everyday RF", "RF", 550, BASE_RHP_SHARE),
    24366: ("Corner OF / PH vs L", "LF", 127, .30),
    21573: ("Corner IF / PH vs L", "1B", 127, .30),
    25323: ("PH vs R", "PH", 64, .95),
    24723: ("Defensive 2B / SS", "2B", 107, BASE_RHP_SHARE),
}

# Expected defensive-position shares across each player's fielding work.
# These do not change with the AB split override: if the GM changes a role,
# a future role editor can update these explicitly.
FIELD_MIX = {
    23770: {"3B": 2/3, "1B": 1/3},
    21573: {"1B": .52, "3B": .48},
    24723: {"2B": .57, "SS": .43},
    24366: {"LF": .56, "RF": .44},
}

POSITION_CODE = {"C": 2, "1B": 3, "2B": 4, "3B": 5, "SS": 6,
                 "LF": 7, "CF": 8, "RF": 9}
PITCHER_IDS = {pid for pid, spec in SEED.items() if spec[1] in ("SP", "RP")}


def _settings_path(league_dir: Path) -> Path:
    return league_dir / "config" / f"war_projection_{YEAR}_team_{TEAM_ID}.json"


def load_overrides(league_dir: Path) -> dict:
    path = _settings_path(league_dir)
    if not path.exists():
        return {}
    try:
        obj = json.loads(path.read_text())
        return obj if isinstance(obj, dict) else {}
    except (OSError, ValueError):
        return {}


def validate_overrides(payload: object) -> dict:
    if not isinstance(payload, dict) or len(payload) > len(SEED):
        raise ValueError("Expected player overrides")
    cleaned = {}
    for raw_pid, value in payload.items():
        try:
            pid = int(raw_pid)
        except (TypeError, ValueError) as exc:
            raise ValueError("Unknown player") from exc
        if pid not in SEED or not isinstance(value, dict):
            raise ValueError("Unknown player")
        allowed = {"ip"} if pid in PITCHER_IDS else {"ab", "vr_share"}
        if set(value) - allowed:
            raise ValueError("Unknown projection field")
        out = {}
        if "ab" in value:
            v = value["ab"]
            if isinstance(v, bool) or not isinstance(v, int) or not 0 <= v <= 750:
                raise ValueError("AB must be 0–750")
            out["ab"] = v
        if "ip" in value:
            v = value["ip"]
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or not 0 <= v <= 350:
                raise ValueError("IP must be 0–350")
            out["ip"] = round(v, 1)
        if "vr_share" in value:
            v = value["vr_share"]
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or not 0 <= v <= 100:
                raise ValueError("vR share must be 0–100%")
            out["vr_share"] = round(v, 1)
        if out:
            cleaned[str(pid)] = out
    return cleaned


def save_overrides(league_dir: Path, overrides: object) -> dict:
    from statsplusplus.config.league_context import atomic_write_text
    cleaned = validate_overrides(overrides)
    path = _settings_path(league_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, json.dumps(cleaned, indent=2) + "\n")
    return cleaned


def _clamp(value, lo, hi):
    return min(hi, max(lo, value))


def _stat_woba(rows, weights):
    """Recent MLB split wOBA, pooled as weighted event counts."""
    fields = ("ab", "h", "d", "t", "hr", "bb", "ibb", "hbp", "sf")
    totals = {k: 0.0 for k in fields}
    weighted_pa = 0.0
    for row in rows:
        recency = {YEAR-1: 3, YEAR-2: 2, YEAR-3: 1}.get(row["year"], 0)
        if not recency:
            continue
        for k in fields:
            totals[k] += recency * (row[k] or 0)
        weighted_pa += recency * (row["pa"] or 0)
    return player_woba(totals, weights) if weighted_pa else None, weighted_pa / 3


def _hitter_components(player, ab, vr_share, history, field_history, run_space):
    rpw = float(run_space.get("anchor", {}).get("runs_per_win", 9.5))
    repl = float(run_space.get("anchor", {}).get("replacement_runs", 20.0))
    fit = run_space.get("tool_woba_fit") or [-.025, .0031, .0013, .002, .001]
    lg = float(run_space.get("lg_woba", .3306))
    scale = float(run_space.get("woba_scale", 1.3023))
    weights = run_space.get("woba_weights") or {"ubb": .70, "hbp": .74, "b1": .91,
                                                "b2": 1.30, "b3": 1.65, "hr": 2.14}
    pa = ab / _clamp(player.get("ab_pa_ratio") or .88, .78, .95)
    split_pa = {3: pa * vr_share, 2: pa * (1-vr_share)}
    bat_runs_total = 0.0
    effective_sample = 0.0
    for split_id, suffix in ((3, "r"), (2, "l")):
        grades = [player.get(f"{k}_{suffix}") or player.get(k) or 50
                  for k in ("cntct", "gap", "pow", "eye")]
        tool_woba = sum(a*b for a,b in zip(fit, [1] + grades))
        observed, obs_pa = _stat_woba(history.get(split_id, []), weights)
        # Separate split sample sizes: a strong-side season can be informative
        # while 10 weak-side PA should hardly move the rating-based prior.
        confidence = min(.78, obs_pa / (obs_pa + 300)) if observed is not None else 0
        blended = tool_woba * (1-confidence) + observed * confidence if observed is not None else tool_woba
        bat_runs_total += ((blended - lg) / scale) * split_pa[split_id]
        effective_sample += obs_pa

    br_600 = baserunning_runs({"speed": player.get("speed"), "steal": player.get("steal")},
                              run_space.get("br_curve"))
    overall = history.get(1, [])
    br_pa = sum((row["pa"] or 0) for row in overall)
    if br_pa:
        obs_br = sum((row["ubr"] or 0) for row in overall) * 600 / br_pa
        c = facet_stat_confidence("baserunning", br_pa)
        br_600 = br_600 * (1-c) + obs_br * c
    br_war = br_600 * (pa/600) / rpw

    primary = player["position"]
    if player["pid"] == 23770:  # Reib plays 3B vs R and 1B vs L.
        mix = {"3B": vr_share, "1B": 1-vr_share}
    else:
        mix = FIELD_MIX.get(player["pid"], {primary: 1.0}) if primary != "PH" else {}
    fielding_war = positional_war = 0.0
    fielding_fraction = 0.0 if primary == "PH" else .98
    for pos, share in mix.items():
        bucket = "COF" if pos in ("LF", "RF") else pos
        grade_tools = {"ofr": player.get("ofr"), "ifr": player.get("ifr"),
                       "c_arm": player.get("c_arm")}
        tool_runs = fielding_runs(grade_tools, bucket, run_space.get("def_curve"))
        recent = [f for f in field_history if f["position"] == POSITION_CODE[pos]]
        recent = sorted(recent, key=lambda x: x["year"], reverse=True)
        games = sum((f["g"] or 0) for f in recent)
        if games >= 20:
            zr_rate = sum((f["zr"] or 0) for f in recent) / games * 154
            c = min(.55, games / (games + 110))
            tool_runs = tool_runs * (1-c) + zr_rate * c
        fielding_war += tool_runs * share * fielding_fraction * (ab/606) / rpw
        positional_war += POSITIONAL_WAR_ADJUSTMENTS.get(bucket, 0.0) * share * fielding_fraction * (pa/600)
    replacement_war = repl / rpw * (pa/600)
    components = {
        "hitting": bat_runs_total/rpw, "pitching": 0.0,
        "fielding": fielding_war, "baserunning": br_war,
        "positional": positional_war, "replacement": replacement_war,
    }
    # Heuristic planning band, not a statistical confidence interval.
    half_width = (1.0 + .55 / math.sqrt(1 + effective_sample/400)) * math.sqrt(max(pa, 1)/600)
    return components, half_width, pa


def _pitcher_components(player, ip, history, weights):
    role = player["position"]
    full_ip = 210 if role == "SP" else 65
    tool_full = peak_war_from_score(player.get("composite_score") or 50, role, weights=weights)
    tool_rate = tool_full / full_ip
    rows = history.get(1, [])
    comparable = []
    for row in rows:
        innings = row["ip"] or 0
        if innings < 20:
            continue
        is_starting = (row["gs"] or 0) >= 10
        if (role == "SP") == is_starting:
            comparable.append(row)
    if comparable:
        weighted_ip = sum((3 if r["year"] == YEAR-1 else 2 if r["year"] == YEAR-2 else 1) * r["ip"] for r in comparable)
        weighted_war = sum((3 if r["year"] == YEAR-1 else 2 if r["year"] == YEAR-2 else 1) *
                           ((r["war"] or 0) + (r["ra9war"] if r["ra9war"] is not None else (r["war"] or 0))) / 2
                           for r in comparable)
        observed_rate = weighted_war / weighted_ip if weighted_ip else tool_rate
        confidence = min(.78, weighted_ip / (weighted_ip + (300 if role == "SP" else 110)))
        rate = tool_rate*(1-confidence) + observed_rate*confidence
        sample = weighted_ip
    else:
        rate, sample = tool_rate, 0.0
    total = rate * ip
    replacement = (1.0 if role == "SP" else .35) * ip/full_ip
    components = {"hitting": 0.0, "pitching": total-replacement,
                  "fielding": 0.0, "baserunning": 0.0,
                  "positional": 0.0, "replacement": replacement}
    half_width = (1.25 if role == "SP" else .65) * math.sqrt(max(ip, 1)/full_ip)
    if sample < full_ip:
        half_width *= 1.2
    return components, half_width, None


def build_projection(team_id: int, overrides: dict | None = None) -> dict:
    """Compute the 25-player plan; does not mutate the DB or saved settings."""
    if team_id != TEAM_ID or get_cfg().league_dir.name.lower() != "ppl":
        raise ValueError("This 25-player plan is for PPL Philadelphia")
    conn = get_db()
    cfg = get_cfg()
    tool_weights = load_tool_weights(cfg.league_dir)
    model_weights = load_model_weights(cfg.league_dir)
    run_space = tool_weights.get("run_space") or {}
    overrides = validate_overrides(overrides or {})
    roster = {}
    seed_placeholders = ",".join("?" for _ in SEED)
    for r in conn.execute(f"""
        SELECT p.player_id AS pid,p.name,p.age,p.level,p.is_active,p.is_on_secondary,p.role,
               r.composite_score,r.ovr,r.pot,r.cntct,r.gap,r.pow,r.eye,
               r.cntct_r,r.gap_r,r.pow_r,r.eye_r,r.cntct_l,r.gap_l,r.pow_l,r.eye_l,
               r.speed,r.steal,r.ofr,r.ifr,r.c_arm
        FROM players p JOIN latest_ratings r ON r.player_id=p.player_id
        WHERE (p.team_id=? OR p.parent_team_id=? OR p.organization_id=?)
          AND (p.is_on_secondary=1 OR p.player_id IN ({seed_placeholders}))
    """, (team_id, team_id, team_id, *SEED)):
        roster[r["pid"]] = dict(r)
    ids = set(SEED) & set(roster)
    bat_hist = {pid: {1: [], 2: [], 3: []} for pid in ids}
    pit_hist = {pid: {1: []} for pid in ids}
    field_hist = {pid: [] for pid in ids}
    for r in conn.execute("SELECT * FROM mlb_batting_stats WHERE year BETWEEN ? AND ? AND split_id IN (1,2,3)", (YEAR-3, YEAR-1)):
        if r["player_id"] in ids:
            bat_hist[r["player_id"]][r["split_id"]].append(dict(r))
    for r in conn.execute("SELECT * FROM mlb_pitching_stats WHERE year BETWEEN ? AND ? AND split_id=1", (YEAR-3, YEAR-1)):
        if r["player_id"] in ids:
            pit_hist[r["player_id"]][1].append(dict(r))
    for r in conn.execute("SELECT * FROM mlb_fielding_stats WHERE year BETWEEN ? AND ?", (YEAR-3, YEAR-1)):
        if r["player_id"] in ids:
            field_hist[r["player_id"]].append(dict(r))

    rows = []
    for pid, (role, position, amount, default_vr) in SEED.items():
        if pid not in roster:
            continue
        p = dict(roster[pid], position=position)
        over = overrides.get(str(pid), {})
        ip = float(over.get("ip", amount)) if position in ("SP", "RP") else None
        ab = int(over.get("ab", amount)) if ip is None else None
        vr_share = float(over.get("vr_share", default_vr*100)) / 100 if ab is not None else None
        overall_bat = bat_hist[pid][1]
        hist_ab = sum((r["ab"] or 0) for r in overall_bat)
        hist_pa = sum((r["pa"] or 0) for r in overall_bat)
        p["ab_pa_ratio"] = hist_ab / hist_pa if hist_pa else None
        if ip is None:
            components, half, pa = _hitter_components(p, ab, vr_share, bat_hist[pid], field_hist[pid], run_space)
        else:
            components, half, pa = _pitcher_components(p, ip, pit_hist[pid], model_weights)
        total = sum(components.values())
        rows.append({
            "pid": pid, "name": p["name"], "role": role, "position": position,
            "ab": ab, "ip": ip, "vr_share": round(vr_share*100, 1) if vr_share is not None else None,
            "vl_share": round((1-vr_share)*100, 1) if vr_share is not None else None,
            "pa_est": round(pa) if pa is not None else None,
            "components": {k: round(v, 2) for k,v in components.items()},
            "total": round(total, 2),
            "range": [round(total-half, 1), round(total+half, 1)],
            "sample": round(sum((r["pa"] or 0) for r in bat_hist[pid][1])) if ab is not None else
                      round(sum((r["ip"] or 0) for r in pit_hist[pid][1])),
        })
    outside = []
    for p in roster.values():
        if p["pid"] in SEED or not p["is_on_secondary"]:
            continue
        outside.append({"pid": p["pid"], "name": p["name"],
                        "level": LEVEL_DISPLAY_MAP.get(int(p["level"]), str(p["level"])),
                        "active": bool(p["is_active"]), "ovr": p["ovr"], "pot": p["pot"],
                        "composite": p["composite_score"]})
    outside.sort(key=lambda x: (x["level"] != "MLB", -(x["composite"] or 0), x["name"]))
    hitter_ab = sum(r["ab"] or 0 for r in rows)
    pitcher_ip = sum(r["ip"] or 0 for r in rows)
    vr_ab = sum((r["ab"] or 0) * (r["vr_share"] or 0) / 100 for r in rows)
    totals = {k: round(sum(r["components"][k] for r in rows), 2)
              for k in ("hitting", "pitching", "fielding", "baserunning", "positional", "replacement")}
    totals["total"] = round(sum(r["total"] for r in rows), 2)
    return {
        "year": YEAR, "team_id": team_id, "rows": rows, "outside": outside,
        "missing": [pid for pid in SEED if pid not in roster],
        "totals": totals,
        "workload": {"hitter_ab": hitter_ab, "hitter_ab_target": DEFAULT_POSITION_AB+DEFAULT_PH_AB,
                     "vr_ab": round(vr_ab), "vr_ab_target": round(hitter_ab*BASE_RHP_SHARE),
                     "pitcher_ip": round(pitcher_ip, 1), "pitcher_ip_target": DEFAULT_TEAM_IP,
                     "pitcher_batting_ab_assumption": PITCHER_BATTING_AB,
                     "team_ab_est": hitter_ab+PITCHER_BATTING_AB, "team_ab_target": DEFAULT_TEAM_AB},
        "source": "1952–54 MLB results blended with current tool ratings; 1954 PHA workload baseline",
    }
