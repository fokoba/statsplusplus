"""Backtest the prospect development assumptions against a league's own history.

Pairs every ratings_history snapshot with the one ~12 months later and measures,
for young players, how much of their current-to-ceiling gap actually closed, how
much the ceiling itself moved (talent-change randomness), and how many reached the
majors. Compares against the gap-closure table the FV model uses.

Stored composites come from whatever model version ran at the time, so each pair
is drift-corrected: the median change among established veterans (28-31, who
barely develop) is subtracted from everyone's change.

Usage: STATSPP_LEAGUE=ppl python scripts/backtest_development.py [--out docs/x.md]
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import statistics as st
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from statsplusplus.config.league_context import get_active_league_slug, get_league_dir  # noqa: E402

PITCHER_POS = {1}
MIN_GAP = 5


def _d(s):
    return date.fromisoformat(s[:10])


def _pairs(dates):
    """Each snapshot with the first one at least 11.5 months later."""
    out = []
    for i, a in enumerate(dates):
        for b in dates[i + 1:]:
            if (_d(b) - _d(a)).days >= 350:
                out.append((a, b))
                break
    return out


def _q(xs, p):
    xs = sorted(xs)
    if not xs:
        return None
    k = (len(xs) - 1) * p
    lo = int(k)
    return xs[lo] + (xs[min(lo + 1, len(xs) - 1)] - xs[lo]) * (k - lo)


def _interp(tbl, age):
    ks = sorted(int(k) for k in tbl)
    a = max(ks[0], min(ks[-1], age))
    for lo, hi in zip(ks, ks[1:]):
        if lo <= a <= hi:
            t = (a - lo) / (hi - lo) if hi > lo else 0
            return tbl[str(lo)] + t * (tbl[str(hi)] - tbl[str(lo)])
    return tbl[str(ks[-1])]


def implied_annual(tbl, age):
    """One-year closure the eventual-closure table implies: the share x of today's
    gap that must close this year for p(a) of it to close eventually, given p(a+1)
    of the remainder closes from next year on."""
    pa, pb = _interp(tbl, age), _interp(tbl, age + 1)
    return (pa - pb) / (1 - pb) if pb < 1 else pa


def run(league_dir: Path) -> dict:
    conn = sqlite3.connect(league_dir / "league.db")
    conn.row_factory = sqlite3.Row
    mw = json.loads((league_dir / "config" / "model_weights.json").read_text())
    params = mw.get("MODEL_PARAMS", {})
    tables = {"H": params.get("GAP_CLOSURE_HITTER_OVERRIDE"), "P": params.get("GAP_CLOSURE_PITCHER_OVERRIDE")}

    info = {r["player_id"]: r for r in conn.execute(
        "SELECT player_id, pos, date_of_birth, level, mlb_service_days, retired FROM players")}
    mlb_ever = {r[0] for r in conn.execute("SELECT DISTINCT player_id FROM mlb_batting_stats "
                                           "UNION SELECT DISTINCT player_id FROM mlb_pitching_stats")}
    dates = [r[0] for r in conn.execute("SELECT DISTINCT snapshot_date FROM ratings_history ORDER BY 1")]

    def snap(d):
        return {r["player_id"]: (r["composite_score"], r["ceiling_score"]) for r in conn.execute(
            "SELECT player_id, composite_score, ceiling_score FROM ratings_history WHERE snapshot_date=?", (d,))
            if r["composite_score"] is not None and r["ceiling_score"] is not None}

    obs = []      # one row per player per 12-month pair
    pairs = _pairs(dates)
    for a, b in pairs:
        A, B = snap(a), snap(b)
        common = [p for p in A if p in B and p in info and info[p]["date_of_birth"]]
        age_at = {p: int((_d(a) - _d(info[p]["date_of_birth"])).days / 365.25) for p in common}
        vets = {"H": [], "P": []}
        for p in common:
            if 28 <= age_at[p] <= 31:
                k = "P" if info[p]["pos"] in PITCHER_POS else "H"
                vets[k].append((B[p][0] - A[p][0], B[p][1] - A[p][1]))
        drift = {k: (st.median(x for x, _ in v) if v else 0, st.median(y for _, y in v) if v else 0)
                 for k, v in vets.items()}
        for p in common:
            age = age_at[p]
            if not 16 <= age <= 25:
                continue
            k = "P" if info[p]["pos"] in PITCHER_POS else "H"
            c0, x0 = A[p]
            c1, x1 = B[p]
            gap = x0 - c0
            if gap < MIN_GAP:
                continue
            dc = (c1 - c0) - drift[k][0]
            dx = (x1 - x0) - drift[k][1]
            obs.append({"pid": p, "kind": k, "age": age, "gap": gap, "c0": c0, "x0": x0,
                        "closure": dc / gap, "dceil": dx, "start": a})

    def summarise(rows, k, age):
        cl = [r["closure"] for r in rows]
        dx = [r["dceil"] for r in rows]
        tbl = tables[k]
        return {
            "kind": k, "age": age, "n": len(rows), "players": len({r["pid"] for r in rows}),
            "gap": round(st.mean(r["gap"] for r in rows), 1),
            "closure_mean": round(st.mean(cl), 3), "closure_median": round(st.median(cl), 3),
            "closure_p10": round(_q(cl, .1), 3), "closure_p90": round(_q(cl, .9), 3),
            "model_annual": round(implied_annual(tbl, age), 3) if tbl else None,
            "model_eventual": round(_interp(tbl, age), 3) if tbl else None,
            "ceil_mean": round(st.mean(dx), 2), "ceil_sd": round(st.pstdev(dx), 2),
            "ceil_down5": round(sum(d <= -5 for d in dx) / len(dx), 3),
            "ceil_up5": round(sum(d >= 5 for d in dx) / len(dx), 3),
        }

    by_age = []
    for k in ("H", "P"):
        for age in range(16, 26):
            rows = [r for r in obs if r["kind"] == k and r["age"] == age]
            if len(rows) >= 20:
                by_age.append(summarise(rows, k, age))

    # Reached the majors: young non-MLB players in the first snapshot.
    first = snap(dates[0])
    arrive = []
    for p, (c0, x0) in first.items():
        i = info.get(p)
        if not i or not i["date_of_birth"] or p in mlb_ever and i["level"] == 1 and (i["mlb_service_days"] or 0) > 400:
            continue
        age = int((_d(dates[0]) - _d(i["date_of_birth"])).days / 365.25)
        if 17 <= age <= 25 and x0 - c0 >= MIN_GAP:
            arrive.append({"band": 5 * int(x0 // 5), "age": age, "made": p in mlb_ever or i["level"] == 1})
    bands = []
    for band in sorted({r["band"] for r in arrive}):
        rows = [r for r in arrive if r["band"] == band]
        if len(rows) >= 20:
            bands.append({"ceiling_band": band, "n": len(rows),
                          "reached_mlb": round(sum(r["made"] for r in rows) / len(rows), 3)})

    # Chain the yearly medians to age 26: how much of today's gap is eventually
    # realised once the ceiling's own drift is included, vs the model's table.
    t = {(r["kind"], r["age"]): r for r in by_age}
    eventual = []
    for k in ("H", "P"):
        for a0 in range(17, 24):
            if (k, a0) not in t:
                continue
            g0 = t[(k, a0)]["gap"]
            closed, gap = 0.0, g0
            for age in range(a0, 26):
                row = t.get((k, age))
                if not row:
                    break
                closed += row["closure_median"] * (gap - closed)
                gap += row["ceil_mean"]
            eventual.append({"kind": k, "age": a0, "gap": g0, "realised": round(closed / g0, 2),
                             "model": round(_interp(tables[k], a0), 2) if tables[k] else None,
                             "ceil_drift": round(gap - g0, 1)})

    return {"eventual": eventual, "league": league_dir.name, "snapshots": [dates[0], dates[-1]], "pairs": len(pairs),
            "observations": len(obs), "by_age": by_age, "arrival": bands}


def to_markdown(res: dict) -> str:
    L = [f"# Development backtest — {res['league'].upper()}",
         "",
         f"Snapshots {res['snapshots'][0]} → {res['snapshots'][1]}; {res['pairs']} twelve-month pairs; "
         f"{res['observations']} player-years (age 16-25, ceiling ≥ {MIN_GAP} above current). "
         "Changes are drift-corrected against veterans aged 28-31.",
         "",
         "**Closure** = share of the start-of-year gap closed in 12 months. "
         "**Model annual** = the one-year closure the FV model's gap-closure table implies.",
         "",
         "| Type | Age | Player-yrs | Avg gap | Closure mean | median | p10 | p90 | Model annual | Ceiling Δ mean | Ceiling Δ sd | Ceiling −5+ | Ceiling +5+ |",
         "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in res["by_age"]:
        L.append(f"| {'Hit' if r['kind'] == 'H' else 'Pit'} | {r['age']} | {r['n']} | {r['gap']} | {r['closure_mean']:.0%} | "
                 f"{r['closure_median']:.0%} | {r['closure_p10']:.0%} | {r['closure_p90']:.0%} | "
                 f"{(r['model_annual'] or 0):.0%} | {r['ceil_mean']:+.1f} | {r['ceil_sd']:.1f} | "
                 f"{r['ceil_down5']:.0%} | {r['ceil_up5']:.0%} |")
    L += ["", "## Eventual realisation (yearly medians chained to age 26)", "",
          "| Type | Start age | Start gap | Realised share | Model table | Ceiling drift by 26 |", "|---|---|---|---|---|---|"]
    for e in res["eventual"]:
        L.append(f"| {'Hit' if e['kind'] == 'H' else 'Pit'} | {e['age']} | {e['gap']} | {e['realised']:.0%} | "
                 f"{(e['model'] or 0):.0%} | {e['ceil_drift']:+.1f} |")
    L += ["", "## Reached the majors (from the first snapshot)", "",
          "| Ceiling band | Players | Reached MLB |", "|---|---|---|"]
    for b in res["arrival"]:
        L.append(f"| {b['ceiling_band']}-{b['ceiling_band'] + 4} | {b['n']} | {b['reached_mlb']:.0%} |")
    return "\n".join(L) + "\n"


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--out")
    a = ap.parse_args()
    res = run(get_league_dir(get_active_league_slug()))
    md = to_markdown(res)
    if a.out:
        Path(a.out).write_text(md)
    print(md)
