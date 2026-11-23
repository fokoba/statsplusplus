"""Fit and validate PPL's next-year development-risk model, without publishing.

Needs numpy for offline fitting; runtime inference uses only the standard
library. Writes two staged model files plus validation evidence to --out.
No live DB or production config is modified.
"""
from __future__ import annotations

import argparse
import json
import math
import sqlite3
from datetime import date
from pathlib import Path

import numpy as np

from statsplusplus.data.milb import compute_milb_averages, load_milb_stat_seasons, completed_season_signal
from statsplusplus.evaluation.development_risk import risk_features

MILB_CONFIG = {"enabled": False, "level_weights": {"2": .75, "3": .50, "4": .30, "5": .15, "6": .10},
               "seasons": 3, "recency": .7, "prior_opportunities": 200.0,
               "interpretation": "Conservative sample-reliability priors, not MLB equivalency rates"}


def _fit(X, y, penalty):
    beta = np.zeros(X.shape[1])
    reg = np.eye(X.shape[1]) * penalty
    reg[0, 0] = .001
    for _ in range(60):
        p = _predict(beta, X)
        w = np.maximum(p * (1 - p), 1e-5)
        step = np.linalg.solve(X.T @ (w[:, None] * X) + reg, X.T @ (y - p) - reg @ beta)
        beta += step
        if np.max(abs(step)) < 1e-7:
            break
    return beta


def _predict(beta, X):
    return 1 / (1 + np.exp(-np.clip(X @ beta, -25, 25)))


def _matrix(rows, extra):
    return np.array([r["features"] + ([r["milb_signal"]] if extra else []) for r in rows])


def _metrics(y, p):
    return {"n": len(y), "brier": float(np.mean((y-p)**2)),
            "logloss": float(np.mean(-y*np.log(np.maximum(p, 1e-8))-(1-y)*np.log(np.maximum(1-p, 1e-8)))),
            "actual_progress": float(np.mean(y)), "mean_prediction": float(np.mean(p))}


def calibrate(conn, settings, dates):
    """One dated training pair and one later pair, one row/player per pair."""
    levels = {int(m["lid"]): int(m["level"]) for m in settings["minor_leagues"]}
    context = compute_milb_averages(conn, levels)
    players = {r["player_id"]: dict(r) for r in conn.execute("SELECT player_id,pos,date_of_birth FROM players")}
    snapshots = {d: {r["player_id"]: dict(r) for r in conn.execute("SELECT * FROM ratings_history WHERE snapshot_date=?", (d,))} for d in dates}
    stat_cache = {}

    def dataset(a, b):
        rows = []
        eligible = missing = unusable = 0
        for pid, x in snapshots[a].items():
            p = players.get(pid)
            if not p or not p["date_of_birth"]:
                continue
            age = int((date.fromisoformat(a)-date.fromisoformat(p["date_of_birth"][:10])).days / 365.25)
            if not 16 <= age <= 25:
                continue
            kind = "P" if p["pos"] == 1 else "H"
            keys = ("stf", "mov", "ctrl") if kind == "P" else ("cntct", "gap", "pow", "eye")
            if any(x[k] is None or x["pot_"+k] is None for k in keys):
                continue
            cur = sum(x[k] for k in keys) / len(keys)
            gap = sum(x["pot_"+k] for k in keys) / len(keys) - cur
            if gap < 5:
                continue
            eligible += 1
            y = snapshots[b].get(pid)
            if y is None:
                missing += 1
                continue
            if any(y[k] is None for k in keys):
                unusable += 1
                continue
            end = sum(y[k] for k in keys) / len(keys)
            key = (pid, kind)
            if key not in stat_cache:
                stat_cache[key] = load_milb_stat_seasons(conn, pid, kind == "P", context)
            sig = completed_season_signal(stat_cache[key], int(a[:4]), kind == "P", MILB_CONFIG)
            rows.append({"pid": pid, "kind": kind, "features": risk_features(age, cur, gap),
                         "y": float(end-cur >= max(1.0, gap*.10)), "age": age,
                         "milb_signal": sig["signal"], "milb_sample": sig["effective_opportunities"]})
        return rows, {"eligible_start": eligible, "missing_end": missing, "unusable_end": unusable, "scored": len(rows)}

    assert dates[0] < dates[1] < dates[2] < dates[3]
    train, train_coverage = dataset(dates[0], dates[1])
    test, test_coverage = dataset(dates[2], dates[3])
    model = {"schema_version": 1, "target": "close_10pct_raw_gap_12m",
             "target_definition": "At least 10% of initial mean raw-tool gap closed, with a minimum 1-point mean-tool gain, over approximately 12 months",
             "training_dates": dates[:2], "validation_dates": dates[2:],
             "age_domain": [16, 25], "raw_gap_domain_min": 5,
             "feature_names": ["intercept", "age_centered", "age_squared", "current_centered", "log_gap"],
             "milb": dict(MILB_CONFIG), "models": {},
             "limitations": ["One validation year", "Complete-case endpoints; missing players are not successful cases",
                             "Historical pitcher/hitter classification uses current listed position",
                             "Observed scouted tools are not true engine talent", "Does not predict lifetime bust probability"]}
    evidence = {"train_coverage": train_coverage, "validation_coverage": test_coverage, "by_type": {}}
    rng = np.random.default_rng(1956)
    for kind in ("H", "P"):
        a = [r for r in train if r["kind"] == kind]
        b = [r for r in test if r["kind"] == kind]
        ya, yb = np.array([r["y"] for r in a]), np.array([r["y"] for r in b])
        if len(a) < 200 or len(b) < 200:
            raise ValueError("Insufficient dated PPL evidence to fit this risk model")
        model["models"][kind] = {}
        evidence["by_type"][kind] = {"train_n": len(a), "validation_n": len(b),
            "constant": _metrics(yb, np.full(len(yb), np.mean(ya))),
            "milb_validation_n": sum(r["milb_sample"] > 0 for r in b)}
        predictions = {}
        for extra, name in ((False, "base"), (True, "with_milb")):
            fitrows = [r for r in a if r["pid"] % 5 != 0]
            tune = [r for r in a if r["pid"] % 5 == 0]
            choices = []
            for penalty in (1.0, 10.0, 100.0):
                beta = _fit(_matrix(fitrows, extra), np.array([r["y"] for r in fitrows]), penalty)
                m = _metrics(np.array([r["y"] for r in tune]), _predict(beta, _matrix(tune, extra)))
                choices.append((m["logloss"], penalty))
            penalty = min(choices)[1]
            beta = _fit(_matrix(a, extra), ya, penalty)
            predictions[name] = pred = _predict(beta, _matrix(b, extra))
            model["models"][kind][name] = {"coefficients": beta.tolist(), "regularization": penalty}
            metrics = _metrics(yb, pred)
            metrics["bands"] = {}
            for lo, hi, label in ((0, .2, "Extreme"), (.2, .4, "High"), (.4, .6, "Medium"), (.6, 1.01, "Low")):
                ix = (pred >= lo) & (pred < hi)
                if ix.any():
                    metrics["bands"][label] = _metrics(yb[ix], pred[ix])
            evidence["by_type"][kind][name] = metrics
        diff = (yb-predictions["with_milb"])**2 - (yb-predictions["base"])**2
        ci = np.quantile([float(np.mean(rng.choice(diff, len(diff), replace=True))) for _ in range(600)], [.025, .975]).tolist()
        evidence["by_type"][kind]["stat_minus_base_brier_95pct"] = ci
        model["models"][kind]["milb_accepted"] = ci[1] < 0
        if evidence["by_type"][kind]["base"]["brier"] >= evidence["by_type"][kind]["constant"]["brier"]:
            raise ValueError("Risk calibration failed chronological baseline gate")
    return model, evidence, context


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--league-dir", required=True, type=Path)
    ap.add_argument("--db", type=Path)
    ap.add_argument("--out", required=True, type=Path)
    args = ap.parse_args()
    conn = sqlite3.connect(f"file:{args.db or args.league_dir / 'league.db'}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    settings = json.loads((args.league_dir / "config/league_settings.json").read_text())
    dates = ["1954-02-17", "1955-02-06", "1955-03-07", "1956-03-06"]
    model, evidence, context = calibrate(conn, settings, dates)
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "stage2_prospect_risk_model.json").write_text(json.dumps(model, indent=2))
    model["milb"]["enabled"] = True
    (args.out / "stage3_prospect_risk_model.json").write_text(json.dumps(model, indent=2))
    (args.out / "risk_validation.json").write_text(json.dumps(evidence, indent=2))
    (args.out / "milb_averages.json").write_text(json.dumps({"schema_version": 1, "leagues": context}, indent=2))
    print(json.dumps(evidence, indent=2))


if __name__ == "__main__":
    main()
