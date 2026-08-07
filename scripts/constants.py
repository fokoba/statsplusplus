# constants.py — shared constants across all scripts
#
# MIGRATION NOTE: All constants are now defined in statsplusplus.evaluation.constants.
# This file re-exports them for backward compatibility. The _w() helper and
# _load_weights() remain for legacy callers that use them directly.

import json
from pathlib import Path

# Re-export everything from the package
from statsplusplus.evaluation.constants import (
    PEAK_AGE_PITCHER,
    PEAK_AGE_HITTER,
    SERVICE_GAMES_HITTER,
    SERVICE_STARTS_SP,
    SERVICE_GAMES_RP,
    DEFAULT_MINIMUM_SALARY,
    DEFAULT_DOLLARS_PER_WAR,
    ARB_HITTER_BASE,
    ARB_HITTER_EXP,
    ARB_RP_BASE,
    ARB_RP_EXP,
    ARB_RAISE_INTERCEPT,
    ARB_RAISE_SLOPE,
    ARB_RAISE_MIN,
    ARB_DEEP_SALARY_THRESHOLD,
    MLB_SCARCITY,
    RP_POT_DISCOUNT,
    PROSPECT_DISCOUNT_RATE,
    SCARCITY_MULT_DEFAULT,
    FV_TO_PEAK_WAR_DEFAULT,
    FV_TO_PEAK_WAR_RP_DEFAULT,
    ARB_PCT_DEFAULT,
    OVR_TO_WAR_DEFAULT,
    AGING_HITTER,
    AGING_PITCHER,
    MIN_REGRESSION_N,
    CALIBRATION_YEARS,
    ModelWeights,
    load_model_weights,
)
from statsplusplus.utils.positions import (
    PITCH_FIELDS,
    ROLE_MAP,
    LEVEL_NORM_AGE,
    DEVELOPMENT_DISCOUNT,
    YEARS_TO_MLB as _YEARS_TO_MLB_DEFAULT,
    POSITIONAL_WAR_ADJUSTMENTS,
)

# ---------------------------------------------------------------------------
# Legacy calibrated weight loader (global state — maintained for compat)
# ---------------------------------------------------------------------------

_weights = None


def _load_weights():
    """Load league-calibrated model weights. Returns dict or None."""
    global _weights
    if _weights is not None:
        return _weights
    try:
        from league_context import get_league_dir
        path = get_league_dir() / "config" / "model_weights.json"
        if path.exists():
            with open(path) as f:
                _weights = json.load(f)
            return _weights
    except Exception:
        pass
    _weights = {}
    return _weights


def _w(key, default):
    """Get a calibrated value, falling back to default."""
    w = _load_weights()
    if not w or key not in w:
        return default
    raw = w[key]
    if isinstance(raw, dict) and isinstance(default, dict):
        sample_key = next(iter(default), None)
        if isinstance(sample_key, int):
            return {int(k): v for k, v in raw.items()}
        return dict(raw)
    return raw


# ---------------------------------------------------------------------------
# Calibrated constants (loaded at import time via _w())
# ---------------------------------------------------------------------------

ARB_PCT = _w("ARB_PCT", dict(ARB_PCT_DEFAULT))

FV_TO_PEAK_WAR = _w("FV_TO_PEAK_WAR", dict(FV_TO_PEAK_WAR_DEFAULT))
FV_TO_PEAK_WAR_SP = _w("FV_TO_PEAK_WAR_SP", dict(FV_TO_PEAK_WAR_DEFAULT))
FV_TO_PEAK_WAR_RP = _w("FV_TO_PEAK_WAR_RP", dict(FV_TO_PEAK_WAR_RP_DEFAULT))

YEARS_TO_MLB = _w("YEARS_TO_MLB", dict(_YEARS_TO_MLB_DEFAULT))
SCARCITY_MULT = _w("SCARCITY_MULT", dict(SCARCITY_MULT_DEFAULT))

# Per-position hitter FV → peak WAR tables
def _load_fv_by_pos():
    w = _load_weights()
    raw = w.get("FV_TO_PEAK_WAR_BY_POS") if w else None
    if not raw or not isinstance(raw, dict):
        return None
    return {bucket: {int(k): v for k, v in tbl.items()}
            for bucket, tbl in raw.items() if isinstance(tbl, dict)}

FV_TO_PEAK_WAR_BY_POS = _load_fv_by_pos()

# WAR projection tables
OVR_TO_WAR = OVR_TO_WAR_DEFAULT  # Legacy alias

def _load_calibrated_ovr():
    w = _load_weights()
    raw = w.get("OVR_TO_WAR") if w else None
    if not raw or not isinstance(raw, dict):
        return None
    return {bucket: {int(k): v for k, v in tbl.items()}
            for bucket, tbl in raw.items() if isinstance(tbl, dict)}

OVR_TO_WAR_CALIBRATED = _load_calibrated_ovr()

def _load_composite_to_war():
    w = _load_weights()
    raw = w.get("COMPOSITE_TO_WAR") if w else None
    if not raw or not isinstance(raw, dict):
        return None
    return {bucket: {int(k): v for k, v in tbl.items()}
            for bucket, tbl in raw.items() if isinstance(tbl, dict)}

COMPOSITE_TO_WAR = _load_composite_to_war()

# Prospect surplus model
PROSPECT_WAR_RAMP = {1: 0.60, 2: 0.80}
NO_TRACK_RECORD_DISCOUNT = 0.50
LEVEL_AGE_DISCOUNT_RATE = 0.04
