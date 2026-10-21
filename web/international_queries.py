"""International amateur free-agent valuation from multiple scouting reports.

Upload the "all columns" player view exported from OOTP under up to three
scouts' eyes (each scout rates the same players differently). Every report is run
through the SAME evaluation engine and FV/surplus code the rest of Dominator uses
(composite, potential, FV, risk, prospect surplus), then combined:

  * per scout        - current composite, potential (true ceiling), FV, surplus
  * blended          - the three rating sets averaged, weighted by each report's
                       own scouting-accuracy label, then evaluated as a player
  * median scout     - the middle value of each rating across the three scouts
  * confidence       - accuracy labels adjusted by how closely the scouts agree
  * bids             - low / average / high shares of the blended surplus

The engine is reused by building an in-memory copy of the league schema, inserting
one synthetic player per (player, variant), and calling the real batch evaluator
on it, so nothing here re-implements the model.
"""

from __future__ import annotations

import csv
import io
import json
import math
import re
import sqlite3
import sys
from pathlib import Path
from statistics import median

# ── CSV parsing ──────────────────────────────────────────────────────────────

ACC_CODE = {"Very Low": "VL", "Low": "L", "Average": "A", "High": "H", "Very High": "VH"}
ACC_SCORE = {"VL": 1, "L": 2, "A": 3, "H": 4, "VH": 5}
CONF_LABEL = {1: "Very Low", 2: "Low", 3: "Average", 4: "High", 5: "Very High"}
# Weight of a report in the blend, by its scouting accuracy (same scale the app
# already uses to adjust international bids).
ACC_WEIGHT = {"VL": 0.65, "L": 0.80, "A": 1.00, "H": 1.05, "VH": 1.15}

POS_CODE = {"P": 1, "SP": 1, "RP": 1, "CL": 1, "C": 2, "1B": 3, "2B": 4, "3B": 5,
            "SS": 6, "LF": 7, "CF": 8, "RF": 9, "DH": 10}
ROLE_CODE = {"SP": 11, "RP": 12, "CL": 13}

# (CSV column, occurrence) -> ratings-table column. "CON" appears twice: first in
# the hitting block (contact), second in the pitching block (control).
_RATING_COLS = {
    # hitting, current / vL / vR / potential
    "cntct": ("CON", 0), "gap": ("GAP", 0), "pow": ("POW", 0), "eye": ("EYE", 0),
    "ks": ("K's", 0), "babip": ("BABIP", 0),
    "cntct_l": ("CON vL", 0), "gap_l": ("GAP vL", 0), "pow_l": ("POW vL", 0),
    "eye_l": ("EYE vL", 0), "ks_l": ("K vL", 0), "babip_l": ("BA vL", 0),
    "cntct_r": ("CON vR", 0), "gap_r": ("GAP vR", 0), "pow_r": ("POW vR", 0),
    "eye_r": ("EYE vR", 0), "ks_r": ("K vR", 0), "babip_r": ("BA vR", 0),
    "pot_cntct": ("CON P", 0), "pot_gap": ("GAP P", 0), "pot_pow": ("POW P", 0),
    "pot_eye": ("EYE P", 0), "pot_ks": ("K P", 0),
    # pitching, current / vL / vR / potential
    "stf": ("STU", 0), "mov": ("MOV", 0), "ctrl": ("CON", 1),
    "pbabip": ("PBABIP", 0), "hra": ("HRR", 0),
    "stf_l": ("STU vL", 0), "mov_l": ("MOV vL", 0), "ctrl_l": ("CON vL", 1),
    "pbabip_l": ("PBABIP vL", 0), "hra_l": ("HRR vL", 0),
    "stf_r": ("STU vR", 0), "mov_r": ("MOV vR", 0), "ctrl_r": ("CON vR", 1),
    "pbabip_r": ("PBABIP vR", 0), "hra_r": ("HRR vR", 0),
    "pot_stf": ("STU P", 0), "pot_mov": ("MOV P", 0), "pot_ctrl": ("CON P", 1),
    "pot_pbabip": ("PBABIP P", 0), "pot_hra": ("HRR P", 0),
    # pitches (current / potential)
    "fst": ("FB", 0), "pot_fst": ("FBP", 0), "chg": ("CH", 0), "pot_chg": ("CHP", 0),
    "crv": ("CB", 0), "pot_crv": ("CBP", 0), "sld": ("SL", 0), "pot_sld": ("SLP", 0),
    "snk": ("SI", 0), "pot_snk": ("SIP", 0), "splt": ("SP", 0), "pot_splt": ("SPP", 0),
    "cutt": ("CT", 0), "pot_cutt": ("CTP", 0), "frk": ("FO", 0), "pot_frk": ("FOP", 0),
    "cir_chg": ("CC", 0), "pot_cir_chg": ("CCP", 0), "scr": ("SC", 0), "pot_scr": ("SCP", 0),
    "kncrv": ("KC", 0), "pot_kncrv": ("KCP", 0), "knbl": ("KN", 0), "pot_knbl": ("KNP", 0),
    "stm": ("STM", 0),
    # running (verified against the app's own ratings: SR = stealing rate)
    "speed": ("SPE", 0), "steal": ("STE", 0), "stl_rt": ("SR", 0), "run": ("RUN", 0),
    # defence
    "c_blk": ("C ABI", 0), "c_frm": ("C FRM", 0), "c_arm": ("C ARM", 0),
    "ifr": ("IF RNG", 0), "ife": ("IF ERR", 0), "ifa": ("IF ARM", 0), "tdp": ("TDP", 0),
    "ofr": ("OF RNG", 0), "ofe": ("OF ERR", 0), "ofa": ("OF ARM", 0),
    # position ratings, current / potential
    "c": ("C", 0), "first_b": ("1B", 0), "second_b": ("2B", 0), "third_b": ("3B", 0),
    "ss": ("SS", 0), "lf": ("LF", 0), "cf": ("CF", 0), "rf": ("RF", 0), "p": ("P", 0),
    "pot_c": ("C Pot", 0), "pot_first_b": ("1B Pot", 0), "pot_second_b": ("2B Pot", 0),
    "pot_third_b": ("3B Pot", 0), "pot_ss": ("SS Pot", 0), "pot_lf": ("LF Pot", 0),
    "pot_cf": ("CF Pot", 0), "pot_rf": ("RF Pot", 0), "pot_p": ("P Pot", 0),
}


def _num(v):
    if v is None:
        return None
    v = str(v).strip().rstrip("%")
    if v in ("", "-"):
        return None
    try:
        return int(float(v))
    except ValueError:
        return None


def _trait(v):
    v = (v or "").strip().upper()
    return v if v in ("H", "N", "L") else None


def combine_traits(reports: list[dict]) -> dict:
    """One value per makeup trait across scouts: the accuracy-weighted majority
    of the scouts that actually know it (None if none do; ties resolve to N)."""
    out = {}
    for k in ("lead", "loy", "adaptability", "greed", "wrk_ethic", "int_"):
        score: dict[str, float] = {}
        for r in reports:
            v = (r.get("traits") or {}).get(k)
            if v:
                score[v] = score.get(v, 0.0) + ACC_WEIGHT.get(r["acc"], 1.0)
        if not score:
            out[k] = None
            continue
        best = max(score.values())
        winners = [v for v, sc in score.items() if abs(sc - best) < 1e-9]
        out[k] = winners[0] if len(winners) == 1 else "N"
    return out


def personality_summary(reports: list[dict]) -> dict:
    """The two display fields: makeup traits (buffs / concerns) and personality
    type. The game hides the real Type for amateurs, so when it is Unknown the
    app's existing trait-combination inference supplies a 'Likely ...' guess."""
    from team_queries import _personality_fields
    t = combine_traits(reports)
    raw = next((r["raw_type"] for r in reports if r.get("raw_type") and r["raw_type"] != "Unknown"), "Unknown")
    f = _personality_fields(t["int_"], t["wrk_ethic"], t["lead"], t["loy"], t["greed"],
                            t["adaptability"], raw)
    known = sum(1 for v in t.values() if v)
    return {"buffs": f["buffs"], "concerns": f["concerns"],
            "type": f["personality_type"], "type_class": f["personality_type_class"],
            "known": known, "traits": t}


def parse_money(text):
    """'$850' -> 850, '$2k' -> 2000, '$1.2M' -> 1_200_000; None if not a figure."""
    m = re.match(r"^\$?\s*([\d.,]+)\s*([kKmM]?)$", (text or "").strip())
    if not m:
        return None
    n = float(m.group(1).replace(",", ""))
    return int(round(n * {"": 1, "k": 1_000, "m": 1_000_000}[m.group(2).lower()]))


def scout_label_from_filename(name: str) -> str:
    """'..._allcolumns_MIL.csv' -> 'MIL'."""
    stem = Path(name or "").stem
    tail = stem.rsplit("_", 1)[-1]
    return tail.upper() if 1 < len(tail) <= 5 else (stem[:12] or "Scout")


def parse_scout_csv(text: str) -> dict:
    """Parse one scout's export. Returns {pid: {...}} with db-style rating keys."""
    rows = list(csv.reader(io.StringIO(text.lstrip("﻿"))))
    if not rows:
        return {}
    header = rows[0]
    occ: dict[str, list[int]] = {}
    for j, h in enumerate(header):
        occ.setdefault(h, []).append(j)

    def ix(name, k=0):
        lst = occ.get(name) or []
        return lst[k] if k < len(lst) else None

    def cell(r, name, k=0):
        j = ix(name, k)
        return r[j] if j is not None and j < len(r) else None

    out = {}
    for r in rows[1:]:
        pid = _num(cell(r, "ID"))
        if pid is None:
            continue
        ratings = {}
        for col, (src, k) in _RATING_COLS.items():
            v = _num(cell(r, src, k))
            if v is not None:
                ratings[col] = v
        acc_label = cell(r, "SctAcc") or ""
        out[pid] = {
            "pid": pid,
            "name": f"{cell(r, 'First Name') or ''} {cell(r, 'Last Name') or ''}".strip() or (cell(r, "Name") or str(pid)),
            "pos": (cell(r, "POS") or "").strip(),
            "age": _num(cell(r, "Age")),
            "acc": ACC_CODE.get(acc_label.strip(), "A"),
            "acc_label": acc_label.strip() or "Average",
            "wrk_ethic": (cell(r, "WE") or "N").strip() or "N",
            "int_": (cell(r, "INT") or "N").strip() or "N",
            # Makeup traits as the scout sees them: H / N / L, or U = not yet known.
            "traits": {
                "lead": _trait(cell(r, "LEA")), "loy": _trait(cell(r, "LOY")),
                "adaptability": _trait(cell(r, "AD")), "greed": _trait(cell(r, "FIN")),
                "wrk_ethic": _trait(cell(r, "WE")), "int_": _trait(cell(r, "INT")),
            },
            "raw_type": (cell(r, "Type") or "").strip(),
            "demand": parse_money(cell(r, "DEM") or ""),
            "difficulty": (cell(r, "Sign") or "").strip(),
            "ratings": ratings,
        }
    return out


# ── Combining reports ────────────────────────────────────────────────────────

def blend_ratings(reports: list[dict]) -> dict:
    """Accuracy-weighted mean of each rating across scouts (rounded to int)."""
    out = {}
    keys = set().union(*(r["ratings"].keys() for r in reports))
    for k in keys:
        num = den = 0.0
        for r in reports:
            v = r["ratings"].get(k)
            if v is None:
                continue
            w = ACC_WEIGHT.get(r["acc"], 1.0)
            num += v * w
            den += w
        if den:
            out[k] = int(round(num / den))
    return out


def median_ratings(reports: list[dict]) -> dict:
    """Middle value of each rating across scouts (rounded to int)."""
    out = {}
    keys = set().union(*(r["ratings"].keys() for r in reports))
    for k in keys:
        vals = [r["ratings"][k] for r in reports if r["ratings"].get(k) is not None]
        if vals:
            out[k] = int(round(median(vals)))
    return out


def confidence_level(accs: list[str], ceilings: list[int | None], fvs: list[int | None]) -> int:
    """1-5 confidence: average accuracy label, nudged by how closely the scouts
    agree on potential (true ceiling) and FV. Tight agreement raises it a level,
    a wide split lowers it."""
    scores = [ACC_SCORE.get(a, 3) for a in accs] or [3]
    level = int(round(sum(scores) / len(scores)))
    cs = [c for c in ceilings if c is not None]
    fs = [f for f in fvs if f is not None]
    c_spread = (max(cs) - min(cs)) if len(cs) > 1 else 0
    f_spread = (max(fs) - min(fs)) if len(fs) > 1 else 0
    if c_spread <= 3 and f_spread <= 5:
        level += 1
    elif c_spread >= 10 or f_spread >= 15:
        level -= 1
    return max(1, min(5, level))


DEFAULT_TIERS = {"low": 1 / 6, "avg": 1 / 4, "high": 1 / 3}


def bids_for(surplus, tiers=None):
    """Low/average/high bid as a share of (probability-weighted) surplus."""
    t = tiers or DEFAULT_TIERS
    if not surplus or surplus <= 0:
        return {"low": 0, "avg": 0, "high": 0}
    return {k: int(round(surplus * float(t[k]))) for k in ("low", "avg", "high")}


# ── Evaluation through the real engine ───────────────────────────────────────

_SCOUT_OFFSET = {"blend": 3, "median": 4}


def _scripts_on_path():
    base = Path(__file__).resolve().parents[1]
    for p in (base / "scripts", base / "src"):
        if str(p) not in sys.path:
            sys.path.insert(0, str(p))


def _memory_league_db(league_dir: Path) -> sqlite3.Connection:
    """In-memory copy of the league's SCHEMA (no data)."""
    src = sqlite3.connect(str(Path(league_dir) / "league.db"))
    ddl = src.execute(
        "SELECT type, sql FROM sqlite_master WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    src.close()
    mem = sqlite3.connect(":memory:")
    mem.row_factory = sqlite3.Row
    pending = [s for t, s in sorted(ddl, key=lambda x: 0 if x[0] == "table" else 1)]
    for _ in range(4):                 # views may depend on other views: retry
        failed = []
        for s in pending:
            try:
                mem.execute(s)
            except sqlite3.OperationalError:
                failed.append(s)
        pending = failed
        if not pending:
            break
    return mem


def evaluate_variants(league_dir: Path, variants: list[dict]) -> dict:
    """Run variants through the engine + FV + surplus.

    variants: [{"vid", "pid", "name", "pos", "age", "acc", "wrk_ethic", "int_", "ratings"}]
    Returns {vid: {composite, ceiling, tool_only, fv, fv_str, risk, surplus, bucket}}.
    """
    _scripts_on_path()
    league_dir = Path(league_dir)
    from statsplusplus.data import evaluation_engine as eng
    from statsplusplus.config.league_config import LeagueConfig
    from statsplusplus.utils.positions import assign_bucket, LEVEL_NORM_AGE
    from statsplusplus.data.fv_calc import LEVEL_INT_KEY, LEVEL_INT_LABEL
    from statsplusplus.evaluation.fv import calc_fv_from_dict
    from prospect_value import prospect_surplus_with_option

    cfg = LeagueConfig(base_dir=league_dir)
    scale = cfg.ratings_scale
    game_date = json.loads((league_dir / "config" / "state.json").read_text()).get("game_date", "1900-01-01")
    LEVEL = 8                                   # international complex
    level_key = LEVEL_INT_KEY.get(LEVEL)
    level_label = LEVEL_INT_LABEL.get(LEVEL, "International")

    mem = _memory_league_db(league_dir)
    rcols = {r[1] for r in mem.execute("PRAGMA table_info(ratings)").fetchall()}
    pcols = {r[1] for r in mem.execute("PRAGMA table_info(players)").fetchall()}
    meta = {}
    for v in variants:
        pos = v["pos"]
        pos_int = POS_CODE.get(pos, 8)
        role = ROLE_CODE.get(pos, 0)
        prow = {"player_id": v["vid"], "name": v["name"], "age": v["age"], "team_id": 0,
                "parent_team_id": 0, "level": str(LEVEL), "pos": pos_int, "role": role,
                "retired": 0, "free_agent": 1}
        mem.execute(
            f"INSERT INTO players ({','.join(k for k in prow if k in pcols)}) "
            f"VALUES ({','.join('?' * sum(1 for k in prow if k in pcols))})",
            [prow[k] for k in prow if k in pcols])
        rrow = {"player_id": v["vid"], "snapshot_date": game_date, "acc": v["acc"],
                "wrk_ethic": v["wrk_ethic"], "int_": v["int_"], **v["ratings"]}
        keys = [k for k in rrow if k in rcols]
        mem.execute(f"INSERT INTO ratings ({','.join(keys)}) VALUES ({','.join('?' * len(keys))})",
                    [rrow[k] for k in keys])
        meta[v["vid"]] = v
    mem.commit()

    eng._run_impl(mem, league_dir)

    out = {}
    rows = mem.execute(
        "SELECT r.*, p.age AS p_age, p.pos AS p_pos, p.role AS p_role, p.level AS p_level "
        "FROM ratings r JOIN players p ON p.player_id = r.player_id").fetchall()
    for row in rows:
        d = dict(row)
        vid = d["player_id"]
        v = meta[vid]
        p = eng._build_player_dict({**d, "age": d["p_age"], "pos": d["p_pos"], "role": d["p_role"]})
        comp = d.get("composite_score") or 0
        pot = d.get("true_ceiling") or d.get("ceiling_score") or comp
        p["Ovr"], p["Pot"], p["Age"] = comp, pot, d["p_age"]
        p["_offensive_ceiling"] = d.get("offensive_ceiling")
        p["Acc"] = d.get("acc") or "N"
        p["WrkEthic"] = d.get("wrk_ethic") or "N"
        p["Int"] = d.get("int_") or "N"
        for src, dst in (("cntct_l", "Cntct_L"), ("cntct_r", "Cntct_R"),
                         ("stf_l", "Stf_L"), ("stf_r", "Stf_R")):
            p[dst] = d.get(src) or 0
        p["_is_pitcher"] = str(d["p_pos"]) == "1"
        bucket = assign_bucket(p)
        p["_bucket"] = bucket
        p["_norm_age"] = LEVEL_NORM_AGE[level_key]
        p["_level"] = level_key
        fv_base, risk = calc_fv_from_dict(p, scale=scale, league_dir=league_dir)
        if bucket == "RP":                       # surplus uses the SP-equivalent FV (as fv_calc does)
            p["_bucket"] = "SP"
            calc_fv_from_dict(p, scale=scale, league_dir=league_dir)
            p["_bucket"] = bucket
        fv_cont = p.get("_fv_continuous", fv_base)
        _DEF_KEY = {"CF": "pot_cf", "SS": "pot_ss", "C": "pot_c", "2B": "pot_second_b", "3B": "pot_third_b"}
        def_rating = d.get(_DEF_KEY.get(bucket, "")) or 0
        try:
            surplus = prospect_surplus_with_option(
                fv_cont, d["p_age"], level_label, bucket, ovr=comp, pot=pot,
                def_rating=def_rating, offensive_grade=d.get("offensive_grade"),
                offensive_ceiling=d.get("offensive_ceiling"),
                defensive_value=d.get("defensive_value"),
                durability_score=d.get("durability_score"), league_dir=league_dir)
        except Exception:
            surplus = 0
        out[vid] = {"composite": comp, "ceiling": pot, "tool_only": d.get("tool_only_score"),
                    "fv": fv_base, "fv_continuous": round(float(fv_cont), 2),
                    "risk": risk, "surplus": int(surplus or 0), "bucket": bucket}
    mem.close()
    return out


# ── Storage ──────────────────────────────────────────────────────────────────

def _store_path(league_dir) -> Path:
    return Path(league_dir) / "config" / "intl_scouts.json"


def load_store(league_dir) -> dict:
    try:
        d = json.loads(_store_path(league_dir).read_text())
    except (OSError, ValueError):
        d = {}
    d.setdefault("slots", [None, None, None])
    d["slots"] = (d["slots"] + [None] * 3)[:3]
    d.setdefault("cash", 1_000_000)
    d.setdefault("tiers", dict(DEFAULT_TIERS))
    return d


def save_store(league_dir, store: dict) -> None:
    p = _store_path(league_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(store))
    tmp.replace(p)


_cache: dict = {}


def build_report(league_dir) -> dict:
    """Full page payload from whatever is stored."""
    league_dir = Path(league_dir)
    store = load_store(league_dir)
    scouts = []
    for i, slot in enumerate(store["slots"]):
        if not slot:
            continue
        parsed = parse_scout_csv(slot["csv"])
        scouts.append({"slot": i, "label": slot["label"], "uploaded_at": slot.get("uploaded_at"),
                       "filename": slot.get("filename"), "players": parsed})
    result = {"scouts": [{"slot": s["slot"], "label": s["label"], "filename": s["filename"],
                          "uploaded_at": s["uploaded_at"], "n": len(s["players"])} for s in scouts],
              "cash": store["cash"], "tiers": store["tiers"], "rows": [], "slots": store["slots"]}
    result["slots"] = [({"label": s["label"], "filename": s.get("filename"),
                         "uploaded_at": s.get("uploaded_at")} if s else None) for s in store["slots"]]
    if not scouts:
        return result

    key = (str(league_dir), tuple((s["slot"], hash(store["slots"][s["slot"]]["csv"])) for s in scouts),
           _store_path(league_dir).parent.joinpath("state.json").stat().st_mtime_ns
           if (league_dir / "config" / "state.json").exists() else 0,
           (league_dir / "config" / "tool_weights.json").stat().st_mtime_ns
           if (league_dir / "config" / "tool_weights.json").exists() else 0)
    if key not in _cache:
        pids = sorted(set().union(*(s["players"].keys() for s in scouts)))
        variants, by_pid = [], {}
        for pid in pids:
            reports = [s["players"][pid] for s in scouts if pid in s["players"]]
            base = reports[0]
            by_pid[pid] = {"reports": reports, "labels": [s["label"] for s in scouts if pid in s["players"]]}
            for k, rep in enumerate(reports):
                variants.append({**rep, "vid": pid * 10 + k})
            if len(reports) >= 2:
                ct = combine_traits(reports)
                for tag, ratings in (("blend", blend_ratings(reports)), ("median", median_ratings(reports))):
                    variants.append({**base, "vid": pid * 10 + _SCOUT_OFFSET[tag], "ratings": ratings,
                                     "acc": "A", "wrk_ethic": ct["wrk_ethic"] or "N", "int_": ct["int_"] or "N"})
            else:
                variants.append({**base, "vid": pid * 10 + 3})
                variants.append({**base, "vid": pid * 10 + 4})
        ev = evaluate_variants(league_dir, variants)
        _cache.clear()
        _cache[key] = (by_pid, ev)
    by_pid, ev = _cache[key]

    rows = []
    for pid, info in by_pid.items():
        reports = info["reports"]
        base = reports[0]
        per = []
        for k, (rep, label) in enumerate(zip(reports, info["labels"])):
            e = ev[pid * 10 + k]
            per.append({"label": label, "acc": rep["acc"], "acc_label": rep["acc_label"],
                        "comp": e["composite"], "pot": e["ceiling"], "fv": e["fv"],
                        "surplus": e["surplus"]})
        bl, md = ev[pid * 10 + 3], ev[pid * 10 + 4]
        conf = confidence_level([p["acc"] for p in per], [p["pot"] for p in per], [p["fv"] for p in per])
        demands = [r["demand"] for r in reports if r.get("demand") is not None]
        rows.append({
            "pid": pid, "name": base["name"], "pos": base["pos"], "bucket": bl["bucket"],
            "age": base["age"], "per": per,
            "blend": {"comp": bl["composite"], "pot": bl["ceiling"], "fv": bl["fv"],
                      "risk": bl["risk"], "surplus": bl["surplus"]},
            "median": {"comp": md["composite"], "pot": md["ceiling"], "fv": md["fv"],
                       "surplus": md["surplus"]},
            "spread": (max(p["pot"] for p in per) - min(p["pot"] for p in per)) if per else 0,
            "conf": conf, "conf_label": CONF_LABEL[conf],
            "demand": max(demands) if demands else None,
            "difficulty": base.get("difficulty"),
            "personality": personality_summary(reports),
            "bids": bids_for(bl["surplus"], store["tiers"]),
        })
    rows.sort(key=lambda r: (-r["blend"]["surplus"], -r["blend"]["fv"], r["name"]))
    result["rows"] = rows
    return result
