"""Empirical short-horizon development risk, separate from lifetime bust risk.

Runtime inference is dependency-free. Calibration uses dated raw tools rather
than historical composites, whose definitions changed between snapshots.
"""
from __future__ import annotations

import json
import math
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

RISK_NOTE = (
    "Calibrated development risk: chance of missing meaningful skill-gap closure "
    "over the next 12 months (10% of the raw-tool gap, at least 1 average tool "
    "point). This is not lifetime bust risk. L/M/H/E use progress-probability "
    "bands of 60%+, 40–60%, 20–40%, and below 20%; near-realized tools are Low."
)


@lru_cache(maxsize=16)
def _read_model(path: str, mtime_ns: int) -> Optional[dict[str, Any]]:
    try:
        model = json.loads(Path(path).read_text())
        if not isinstance(model, dict) or model.get("schema_version") != 1 or model.get("target") != "close_10pct_raw_gap_12m":
            return None
        return model
    except (OSError, ValueError):
        return None


def load_risk_model(league_dir: Path | None) -> Optional[dict[str, Any]]:
    if league_dir is None:
        return None
    path = Path(league_dir) / "config" / "prospect_risk_model.json"
    try:
        return _read_model(str(path.resolve()), path.stat().st_mtime_ns)
    except OSError:
        return None


def risk_features(age: int, current: float, gap: float) -> list[float]:
    a = (age - 21) / 4.0
    return [1.0, a, a * a, (current - 40) / 15.0, math.log1p(max(0.0, gap)) / 3.0]


def predict_development_risk(
    current_tools: list[float | None],
    potential_tools: list[float | None],
    age: int,
    is_pitcher: bool,
    model: dict[str, Any] | None,
    milb_signal: float = 0.0,
) -> Optional[dict[str, Any]]:
    if not model or not 16 <= age <= 25 or not current_tools:
        return None
    if len(current_tools) != len(potential_tools):
        return None
    if any(v is None for v in current_tools + potential_tools):
        return None
    current_values = [float(v) for v in current_tools if v is not None]
    potential_values = [float(v) for v in potential_tools if v is not None]
    if any(not math.isfinite(v) or not 20 <= v <= 80 for v in current_values + potential_values):
        return None
    current = sum(current_values) / len(current_values)
    potential = sum(potential_values) / len(potential_values)
    gap = max(0.0, potential - current)
    # The training cohort has >=5 raw-tool points of remaining room. Below
    # that floor label near-realization, without manufacturing a probability.
    if gap < 5:
        return {"risk": "Low", "progress_probability": None,
                "basis": "near_realized", "raw_gap": gap}
    kind = "P" if is_pitcher else "H"
    group = model.get("models", {}).get(kind, {})
    variant = "with_milb" if (model.get("milb", {}).get("enabled") and group.get("milb_accepted")) else "base"
    coeff = model.get("models", {}).get(kind, {}).get(variant, {}).get("coefficients")
    features = risk_features(age, current, gap)
    if variant == "with_milb":
        features.append(milb_signal if math.isfinite(milb_signal) else 0.0)
    if not coeff or len(coeff) != len(features) or not all(isinstance(v, (int, float)) and math.isfinite(v) for v in coeff):
        return None
    z = max(-25.0, min(25.0, sum(b * x for b, x in zip(coeff, features))))
    p = 1.0 / (1.0 + math.exp(-z))
    label = "Low" if p >= .60 else "Medium" if p >= .40 else "High" if p >= .20 else "Extreme"
    return {"risk": label, "progress_probability": p, "basis": variant,
            "raw_gap": gap, "raw_current": current}
