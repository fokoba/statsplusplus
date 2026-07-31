# constants.py — shared constants across all scripts
#
# Sections:
#   1. Shared identifiers
#   2. Calibrated model weight loader
#   3. Prospect surplus model constants
#   4. MLB contract surplus model constants
#   5. WAR projection tables
#   6. Aging curves

import json
from pathlib import Path

# ---------------------------------------------------------------------------
# 1. Shared identifiers
# ---------------------------------------------------------------------------

PITCH_FIELDS = ["Fst","Snk","Crv","Sld","Chg","Splt","Cutt","CirChg","Scr","Frk","Kncrv","Knbl"]

# Role ID → positional bucket (OOTP role codes: 11=SP, 12=RP, 13=CL)
ROLE_MAP = {11: "SP", 12: "RP", 13: "CL"}

# Default minimum salary fallback when league_settings.json is absent.
# Matches OOTP default minimum; overridden by league_settings.json in practice.
DEFAULT_MINIMUM_SALARY = 825_000

# Default $/WAR fallback used before the first league refresh populates
# league_averages.json. Computed from actual league contracts by refresh.py.
# This value is a reasonable starting estimate only — do not tune it manually.
DEFAULT_DOLLARS_PER_WAR = 9_000_000

# Peak performance age by role. Players before peak age are on the development
# ramp; players after are on the decline curve.
PEAK_AGE_PITCHER = 27
PEAK_AGE_HITTER  = 28

# Service time denominators for fractional service year estimation.
# Hitters: games / full season. SP: starts / full rotation. RP: appearances / full bullpen.
SERVICE_GAMES_HITTER = 162
SERVICE_STARTS_SP    = 32
SERVICE_GAMES_RP     = 65

# ---------------------------------------------------------------------------
# 2. Calibrated model weight loader
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
    # Convert string keys to int when the default uses int keys
    if isinstance(raw, dict) and isinstance(default, dict):
        sample_key = next(iter(default), None)
        if isinstance(sample_key, int):
            return {int(k): v for k, v in raw.items()}
        return dict(raw)
    return raw

# ---------------------------------------------------------------------------
# 3. Prospect surplus model constants
# ---------------------------------------------------------------------------

# Arbitration salary as % of market value by arb year.
# Calibrated to OOTP arb outcomes: 86 arb-eligible players with WAR > 0.5.
# OOTP arb pays ~20-33% of FA market rate (vs ~45-80% in real MLB).
_ARB_PCT_DEFAULT = {1: 0.20, 2: 0.22, 3: 0.33}
ARB_PCT = _w("ARB_PCT", _ARB_PCT_DEFAULT)

# FV → expected peak WAR/year (midpoint of each FV band per farm_analysis_guide.md)
# Interpolate linearly between defined points.
_FV_TO_PEAK_WAR_DEFAULT = {
    80: 10.0, 70: 7.0, 65: 5.5, 60: 4.2, 55: 2.9, 50: 2.0, 45: 1.2, 40: 0.5
}
FV_TO_PEAK_WAR = _w("FV_TO_PEAK_WAR", _FV_TO_PEAK_WAR_DEFAULT)

# SP-specific FV → peak WAR table. When calibrated, SP and hitter tables diverge
# because pitchers produce less WAR per OVR point than hitters in most leagues.
# Falls back to the generic hitter table when uncalibrated.
FV_TO_PEAK_WAR_SP = _w("FV_TO_PEAK_WAR_SP", _FV_TO_PEAK_WAR_DEFAULT)

# Per-position hitter FV → peak WAR tables. When calibrated, each hitter bucket
# gets its own table (COF produces less WAR per FV grade than SS). Falls back to
# the generic hitter table when uncalibrated.
def _load_fv_by_pos():
    w = _load_weights()
    raw = w.get("FV_TO_PEAK_WAR_BY_POS")
    if not raw or not isinstance(raw, dict):
        return None
    return {bucket: {int(k): v for k, v in tbl.items()}
            for bucket, tbl in raw.items() if isinstance(tbl, dict)}

FV_TO_PEAK_WAR_BY_POS = _load_fv_by_pos()

# RP-specific FV → peak WAR table. Calibrated from regression on 1582 qualifying
# RP seasons (IP>=20, GS<=3): WAR = -1.37 + 0.040 * Ovr, cross-referenced with
# peak-season WAR by Pot bucket. Replaces the old flat RP_WAR_CAP=2.0 which
# collapsed FV 50-80 RPs to identical surplus values.
_FV_TO_PEAK_WAR_RP_DEFAULT = {
    80: 3.2, 70: 2.6, 65: 2.3, 60: 2.0, 55: 1.6, 50: 1.2, 45: 0.8, 40: 0.5
}
FV_TO_PEAK_WAR_RP = _w("FV_TO_PEAK_WAR_RP", _FV_TO_PEAK_WAR_RP_DEFAULT)

# Development discount — bust probability only (P(reaches projected FV)).
# Time value of money is handled separately by PROSPECT_DISCOUNT_RATE.
# Flattened curve (Session 33): old values dropped too steeply below AAA,
# causing 81/100 top prospects to be AAA. A high-ceiling A-ball arm is still
# very valuable — the old 0.60 discount was too harsh.
DEVELOPMENT_DISCOUNT = {
    "MLB": 1.00, "AAA": 0.88, "AA": 0.78, "A": 0.68,
    "A-Short": 0.55, "USL": 0.45, "DSL": 0.45, "Intl": 0.35,
}

# Estimated years until MLB debut by current level
_YEARS_TO_MLB_DEFAULT = {
    "MLB": 0, "AAA": 0.5, "AA": 1.5, "A": 2.5,
    "A-Short": 3.5, "USL": 4.5, "DSL": 4.5, "Intl": 5.0,
}
YEARS_TO_MLB = _w("YEARS_TO_MLB", _YEARS_TO_MLB_DEFAULT)

# Annual discount rate for time value + residual development risk
PROSPECT_DISCOUNT_RATE = 0.05

# Age-vs-level discount adjustment rate: +/- per year vs level norm age.
# Steeper than original 3%/yr (Session 33) to better reward young-for-level prospects.
LEVEL_AGE_DISCOUNT_RATE = 0.04

# WAR production ramp for early control years (players don't immediately produce
# peak WAR on MLB debut — adjustment period + development variance).
# Year 3+ = 1.0 (full production).
PROSPECT_WAR_RAMP = {1: 0.60, 2: 0.80}

# Discount applied to ratings-based WAR projection for players with no MLB track record.
# Unproven players produce ~50% of ratings-based WAR on average.
NO_TRACK_RECORD_DISCOUNT = 0.50

# RP FV Pot discount factor. RPs produce less WAR per FV grade than other positions.
# Scales Pot down so only elite RPs earn high FV grades.
# 0.80 calibrated to produce ~5% RP share in top prospect lists (real-baseball norm).
RP_POT_DISCOUNT = 0.85

# Scarcity multiplier by talent tier. Low-ceiling players are freely available (waivers,
# minor league FA) so their theoretical surplus has no trade value. Applied using
# true_ceiling (composite scale) so developing players are valued for what they'll become.
# Smooth S-curve shape — no single-point cliffs.
# Calibrated for composite ceiling scale where:
#   MLB P10 = ceiling 48, MLB P50 = ceiling 52, Prospect P90 = ceiling 51
#   Below 45 = below MLB floor (freely available)
#   52+ = above-average MLB ceiling (full scarcity)
_SCARCITY_MULT_DEFAULT = {42: 0.0, 44: 0.03, 45: 0.10, 46: 0.20, 47: 0.35, 48: 0.50, 49: 0.65, 50: 0.80, 51: 0.90, 52: 0.97, 53: 1.0, 80: 1.0}
SCARCITY_MULT = _w("SCARCITY_MULT", _SCARCITY_MULT_DEFAULT)

# Minimum data points required for position-specific regression in calibrate.py.
MIN_REGRESSION_N = 40

# Number of complete seasons used for calibration regression.
CALIBRATION_YEARS = 3

# ---------------------------------------------------------------------------
# 4. MLB contract surplus model constants
# ---------------------------------------------------------------------------

# MLB positional scarcity premium: multiplier on market value (WAR * $/WAR).
# Premium positions are harder to replace on the open market.
# Centered near 1.0; SS/CF/SP get a premium, 1B/COF a discount.
# Calibrated Session 37 from positional supply analysis.
MLB_SCARCITY = {
    'SS': 1.10, 'CF': 1.06, 'SP': 1.06, 'C': 1.03, '2B': 1.03, '3B': 1.03,
    'COF': 0.94, 'RP': 0.94, '1B': 0.91,
}

# Arb salary model — hitter/SP exponential base formula.
# Calibrated from OOTP arb outcomes. Formula: base * exp(exp_coeff * ovr).
# Represents first arb year salary; subsequent years use ARB_RAISE_* below.
ARB_HITTER_BASE  = 318_400
ARB_HITTER_EXP   = 0.0495

# Arb salary model — RP exponential base formula.
# RPs calibrated separately (35 RP arb contracts): 25% annual raises applied.
ARB_RP_BASE = 566_254
ARB_RP_EXP  = 0.0294

# Annual arb raise formula for hitters/SPs: max(ARB_RAISE_MIN, intercept + slope * ovr).
# Applied to prior year salary for arb years 2+.
ARB_RAISE_INTERCEPT = -2_500_000
ARB_RAISE_SLOPE     =    110_000
ARB_RAISE_MIN       =  1_000_000

# Salary threshold above which a player is assumed to be in deep arb (year 4+).
# Used when service time is ambiguous from games data alone.
ARB_DEEP_SALARY_THRESHOLD = 5_500_000

# ---------------------------------------------------------------------------
# 5. WAR projection tables
# ---------------------------------------------------------------------------

# OVR → peak WAR/year by bucket.
# Used for MLB players without sufficient stat history.
# When model_weights.json exists, OVR_TO_WAR_CALIBRATED provides position-specific
# tables (per bucket). Otherwise, falls back to the generic 3-column table below.
OVR_TO_WAR = [
    # (Ovr, hitter_WAR, SP_WAR, RP_WAR) — default fallback
    (80, 9.0, 8.0, 2.5),
    (75, 7.5, 6.5, 2.0),
    (70, 6.0, 5.5, 1.5),
    (65, 4.5, 4.0, 1.2),
    (60, 3.2, 2.8, 1.0),
    (55, 2.2, 1.9, 0.7),
    (50, 2.0, 1.7, 0.5),
    (45, 1.0, 0.8, 0.3),
    (40, 0.2, 0.2, 0.1),
]

def _load_calibrated_ovr():
    """Load position-specific OVR_TO_WAR from model_weights.json."""
    w = _load_weights()
    raw = w.get("OVR_TO_WAR")
    if not raw or not isinstance(raw, dict):
        return None
    # Convert: {"SS": {"80": 7.83, ...}} -> {"SS": {80: 7.83, ...}}
    return {bucket: {int(k): v for k, v in tbl.items()}
            for bucket, tbl in raw.items() if isinstance(tbl, dict)}

OVR_TO_WAR_CALIBRATED = _load_calibrated_ovr()


def _load_composite_to_war():
    """Load position-specific COMPOSITE_TO_WAR from model_weights.json."""
    w = _load_weights()
    raw = w.get("COMPOSITE_TO_WAR")
    if not raw or not isinstance(raw, dict):
        return None
    # Convert: {"SS": {"80": 7.83, ...}} -> {"SS": {80: 7.83, ...}}
    return {bucket: {int(k): v for k, v in tbl.items()}
            for bucket, tbl in raw.items() if isinstance(tbl, dict)}

COMPOSITE_TO_WAR = _load_composite_to_war()

# ---------------------------------------------------------------------------
# 6. Aging curves
# ---------------------------------------------------------------------------

# Multiplier on peak WAR by age. Interpolated for intermediate ages.
# Calibrated 2033-04-25 from Marcel/BP/FanGraphs consensus aging research.
# Decline rates calibrated from league data (Session 37):
# ~3%/yr at 29-30, ~9%/yr 31-32, ~11%/yr 33-35, ~16%/yr 36+.
# Steeper than MLB IRL — OOTP aging mechanics are more aggressive.
AGING_HITTER = {
    27: 1.00, 28: 1.00, 29: 0.97, 30: 0.92, 31: 0.84,
    32: 0.76, 33: 0.68, 34: 0.60, 35: 0.51, 36: 0.42,
    37: 0.34, 38: 0.25, 39: 0.17, 40: 0.10, 42: 0.04
}

# Pitchers steeper from 31+ (injury/velocity risk).
AGING_PITCHER = {
    27: 1.00, 28: 1.00, 29: 0.97, 30: 0.93, 31: 0.85,
    32: 0.76, 33: 0.66, 34: 0.54, 35: 0.43, 36: 0.33,
    37: 0.24, 38: 0.16, 39: 0.10, 40: 0.05
}
