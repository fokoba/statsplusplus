"""
player_utils.py — Shared player evaluation utilities.

MIGRATION NOTE: Re-exports from the statsplusplus package where possible.
Remaining functions (assign_bucket, dollars_per_war, league_minimum, calc_pap)
stay here as they have complex dependencies on league config and DB state.

Used by farm_analysis.py, prospect_value.py, contract_value.py, trade_calculator.py, fv_calc.py.
"""

import os, json
from math import tanh

# Re-export from package
from statsplusplus.utils.positions import (
    PITCH_FIELDS,
    PITCH_NAMES,
    POSITIONAL_WAR_ADJUSTMENTS,
    LEVEL_NORM_AGE,
)
from statsplusplus.utils.formatting import height_str
from statsplusplus.evaluation.constants import (
    RP_POT_DISCOUNT,
    DEFAULT_DOLLARS_PER_WAR,
    DEFAULT_MINIMUM_SALARY,
    OVR_TO_WAR_DEFAULT as OVR_TO_WAR,
    AGING_HITTER,
    AGING_PITCHER,
    PEAK_AGE_PITCHER,
    PEAK_AGE_HITTER,
)

# Re-export from redirected modules — ratings uses global scale state
from ratings import (  # noqa: F401
    norm, norm_continuous, norm_floor,
    get_ratings_scale, init_ratings_scale, _get_ratings_scale,
)

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def display_pos(bucket, listed_pos=None):
    """Convert internal bucket to display position. COF -> OF."""
    return "OF" if bucket == "COF" else bucket


def fmt_table(headers, values):
    """Format a single-row markdown table."""
    col_w = [max(len(h), len(v)) for h, v in zip(headers, values)]
    h_row = "| " + " | ".join(h.ljust(w) for h, w in zip(headers, col_w)) + " |"
    s_row = "| " + " | ".join("-" * w for w in col_w) + " |"
    v_row = "| " + " | ".join(v.ljust(w) for v, w in zip(values, col_w)) + " |"
    return "\n".join([h_row, s_row, v_row])


def defensive_score(p, bucket):
    """Weighted defensive score on 20-80 scale for a position bucket."""
    from fv_model import DEFENSIVE_WEIGHTS
    def _n(val):
        return norm(val, get_ratings_scale()) or 0
    if bucket == "COF":
        lf = sum(_n(p.get(f, 0) or 0) * w for f, w in DEFENSIVE_WEIGHTS["COF_LF"].items())
        rf = sum(_n(p.get(f, 0) or 0) * w for f, w in DEFENSIVE_WEIGHTS["COF_RF"].items())
        return max(lf, rf)
    weights = DEFENSIVE_WEIGHTS.get(bucket)
    if not weights:
        return 0
    return sum(_n(p.get(f, 0) or 0) * w for f, w in weights.items())


# ---------------------------------------------------------------------------
# Positional rating estimation from defensive tools
# ---------------------------------------------------------------------------

_positional_models = None

def _load_positional_models():
    global _positional_models
    if _positional_models is not None:
        return _positional_models
    try:
        from league_context import get_league_dir
        mw_path = get_league_dir() / "config" / "model_weights.json"
        if mw_path.exists():
            with open(mw_path) as f:
                weights = json.load(f)
            _positional_models = weights.get("POSITIONAL_MODELS", {})
        else:
            _positional_models = {}
    except Exception:
        _positional_models = {}
    return _positional_models


def estimate_positional_rating(p, pos_col):
    """Estimate a positional rating from defensive tools using calibrated model."""
    models = _load_positional_models()
    model = models.get(pos_col)
    if not model:
        return None
    features = model["features"]
    coefficients = model["coefficients"]
    key_map = {
        "ifr": "IFR", "ifa": "IFA", "ife": "IFE", "tdp": "TDP",
        "ofr": "OFR", "ofa": "OFA", "ofe": "OFE",
        "c_arm": "CArm", "c_blk": "CBlk", "c_frm": "CFrm",
        "height": "Height",
    }
    vals = []
    for feat in features:
        k = key_map.get(feat, feat)
        v = p.get(k) or 0
        if not v:
            return None
        vals.append(float(v))
    result = coefficients[0] + sum(coefficients[i + 1] * vals[i] for i in range(len(vals)))
    return max(0, result)


def estimate_all_positions(p):
    """Estimate ratings at all positions."""
    col_to_bucket = {
        "pot_ss": "SS", "pot_second_b": "2B", "pot_third_b": "3B",
        "pot_cf": "CF", "pot_lf": "LF", "pot_rf": "RF",
        "pot_first_b": "1B", "pot_c": "C",
    }
    estimates = {}
    for pos_col, bucket in col_to_bucket.items():
        est = estimate_positional_rating(p, pos_col)
        if est is not None:
            estimates[bucket] = est
    return estimates


def assign_bucket(p, use_pot=None):
    """Assign positional bucket — determines most valuable defensive position."""
    if use_pot is None:
        use_pot = True

    def pgrade(field):
        key = ("Pot" + field) if use_pot else field
        v = p.get(key, 0)
        if isinstance(v, (int, float)):
            return v
        try:
            return int(v)
        except (ValueError, TypeError):
            return 0

    pos_str = str(p.get("Pos", ""))
    role_str = str(p.get("_role", ""))
    is_pitcher = (pos_str == "P" or role_str in ("starter", "reliever", "closer"))

    if is_pitcher:
        if not use_pot and role_str in ("reliever", "closer"):
            return "RP"
        if role_str == "starter" and (p.get("Stm") or 0) >= 20:
            return "SP"
        stm = p.get("Stm") or 0
        if stm >= 25 and ((p.get("PotKnbl") or 0) >= 45 or (p.get("PotKncrv") or 0) >= 45):
            return "SP"
        viable = sum(1 for f in PITCH_FIELDS if (p.get("Pot" + f) or 0) >= 45)
        return "RP" if (viable < 3 or stm < 30) else "SP"

    if pgrade("C") >= 45:                          return "C"
    if pgrade("SS") >= 50:
        ss_grade = pgrade("SS")
        if ss_grade <= 55:
            if pgrade("3B") >= ss_grade + 10:       return "3B"
            if pgrade("2B") >= ss_grade + 10:       return "2B"
        return "SS"
    if pgrade("2B") >= 50 or pgrade("SS") >= 50:   return "2B"
    if pgrade("CF") >= 55:
        cf_grade = pgrade("CF")
        if cf_grade <= 55:
            best_cof = max(pgrade("LF"), pgrade("RF"))
            if best_cof >= cf_grade + 10:           return "COF"
        return "CF"
    if pgrade("LF") >= 45 or pgrade("RF") >= 45:   return "COF"
    if pgrade("3B") >= 45:                          return "3B"
    if pgrade("1B") >= 45:                          return "1B"

    # Fallback: calibrated model estimation
    estimates = estimate_all_positions(p)
    if estimates:
        _EST_THRESHOLDS = {
            "C": 47, "SS": 52, "2B": 50, "CF": 53,
            "3B": 47, "LF": 45, "RF": 45, "1B": 40,
        }
        _PRIORITY = ["C", "SS", "CF", "2B", "3B", "LF", "RF", "1B"]
        for pos in _PRIORITY:
            if pos in estimates and estimates[pos] >= _EST_THRESHOLDS.get(pos, 50):
                if pos in ("LF", "RF"):
                    return "COF"
                return pos
        return "1B"

    pos_map = {"2": "C", "3": "1B", "4": "2B", "5": "3B", "6": "SS",
               "7": "COF", "8": "CF", "9": "COF", "10": "COF"}
    return pos_map.get(pos_str, "1B")


# ---------------------------------------------------------------------------
# FV calculation — re-exported from fv_model
# ---------------------------------------------------------------------------
from fv_model import (  # noqa: F401
    calc_fv, dev_weight, age_development_mult,
    defensive_score, DEFENSIVE_WEIGHTS,
)

# ---------------------------------------------------------------------------
# WAR estimation — re-exported from war_model
# ---------------------------------------------------------------------------
from war_model import (  # noqa: F401
    peak_war_from_ovr, aging_mult, load_stat_history, stat_peak_war
)

# Import calibrated tables (already loaded via constants)
from constants import OVR_TO_WAR_CALIBRATED  # noqa: F401

# ---------------------------------------------------------------------------
# League settings
# ---------------------------------------------------------------------------

def load_league_settings():
    from league_config import config
    return config.settings

def league_minimum():
    from league_config import config
    return config.minimum_salary

def dollars_per_war():
    from league_context import get_league_dir
    path = get_league_dir() / "config" / "league_averages.json"
    if path.exists():
        with open(path) as f:
            data = json.load(f)
        if "dollar_per_war" in data:
            return data["dollar_per_war"]
    min_sal = league_minimum()
    if min_sal and min_sal != DEFAULT_MINIMUM_SALARY:
        return round(DEFAULT_DOLLARS_PER_WAR * min_sal / DEFAULT_MINIMUM_SALARY)
    return DEFAULT_DOLLARS_PER_WAR


# ---------------------------------------------------------------------------
# PAP (Payroll Adjusted Performance)
# ---------------------------------------------------------------------------
from constants import _w as _cw

def calc_pap(war, salary, team_games, dpw):
    """PAP from actual production."""
    if war is None or team_games is None or team_games < 5:
        return None
    annualized = war * (162 / team_games)
    surplus = annualized * dpw - salary
    scale = _cw("PAP_SCALE", 25_000_000)
    return round(5 + 5 * tanh(surplus / scale), 2)
