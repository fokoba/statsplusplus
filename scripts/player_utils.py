"""
player_utils.py — Shared player evaluation utilities.

Rating normalization, positional bucketing, display helpers, league settings,
and PAP calculation. Imports and re-exports from fv_model and war_model for
backward compatibility.

Used by farm_analysis.py, prospect_value.py, contract_value.py, trade_calculator.py, fv_calc.py.
"""

import os, json
from constants import PITCH_FIELDS, RP_POT_DISCOUNT  # noqa: F401 (re-exported)

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ---------------------------------------------------------------------------
# Constants (re-exported for backward compat)
# ---------------------------------------------------------------------------

PITCH_NAMES = {
    "Fst":"Fastball","Snk":"Sinker","Crv":"Curveball","Sld":"Slider",
    "Chg":"Changeup","Splt":"Splitter","Cutt":"Cutter","CirChg":"Circle Change",
    "Scr":"Screwball","Frk":"Forkball","Kncrv":"Knuckle Curve","Knbl":"Knuckleball"
}

POSITIONAL_WAR_ADJUSTMENTS = {
    "C":   1.2, "SS":  0.7, "2B":  0.3, "CF":  0.25, "3B":  0.2,
    "COF": -0.7, "1B": -1.2, "DH": -1.7, "SP":  0.0,  "RP": -1.0,
}

# ---------------------------------------------------------------------------
# Rating normalization — re-exported from ratings.py for backward compatibility
# ---------------------------------------------------------------------------
from ratings import (  # noqa: F401
    norm, norm_floor, get_ratings_scale, init_ratings_scale,
    _get_ratings_scale  # kept for any legacy internal callers
)


def height_str(cm):
    """Convert height in cm to feet'inches" string."""
    if not cm:
        return None
    feet = int(cm / 30.48)
    inches = round((cm % 30.48) / 2.54)
    return f"{feet}'{inches}\""


def display_pos(bucket, listed_pos=None):
    """Convert internal bucket to display position. COF -> OF, keep CF distinct."""
    return "OF" if bucket == "COF" else bucket


def fmt_table(headers, values):
    """Format a single-row markdown table with header, separator, and value rows."""
    col_w = [max(len(h), len(v)) for h, v in zip(headers, values)]
    h_row = "| " + " | ".join(h.ljust(w) for h, w in zip(headers, col_w)) + " |"
    s_row = "| " + " | ".join("-" * w for w in col_w) + " |"
    v_row = "| " + " | ".join(v.ljust(w) for v, w in zip(values, col_w)) + " |"
    return "\n".join([h_row, s_row, v_row])


def defensive_score(p, bucket):
    """Weighted defensive score on 20-80 scale for a position bucket.
    Returns the position-weighted average of underlying defensive tools."""
    def _n(val):
        return norm(val) or 0
    if bucket == "COF":
        lf = sum(_n(p.get(f, 0) or 0) * w for f, w in DEFENSIVE_WEIGHTS["COF_LF"].items())
        rf = sum(_n(p.get(f, 0) or 0) * w for f, w in DEFENSIVE_WEIGHTS["COF_RF"].items())
        return max(lf, rf)
    weights = DEFENSIVE_WEIGHTS.get(bucket)
    if not weights:
        return 0
    return sum(_n(p.get(f, 0) or 0) * w for f, w in weights.items())


def _pos_composite(p, bucket, age):
    """Normalized positional composite for defensive bonus (uses Pot grades for age <= 23)."""
    if bucket == "COF":
        return norm(max(p.get("LF", 0), p.get("RF", 0)))
    pot_map = {"C": "PotC", "SS": "PotSS", "CF": "PotCF",
               "2B": "Pot2B", "3B": "Pot3B"}
    cur_map = {"C": "C", "SS": "SS", "CF": "CF", "2B": "2B", "3B": "3B"}
    field = pot_map.get(bucket) if age <= 23 else cur_map.get(bucket)
    if not field:
        return 0
    return norm(p.get(field, 0))


# ---------------------------------------------------------------------------
# Positional rating estimation from defensive tools
# ---------------------------------------------------------------------------

# Loaded lazily from model_weights.json
_positional_models = None

def _load_positional_models():
    """Load calibrated positional models from model_weights.json."""
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
    """Estimate a positional rating from defensive tools using calibrated model.

    Args:
        p: Player dict with keys like IFR, IFA, IFE, TDP, OFR, OFA, OFE, etc.
        pos_col: Column name like 'pot_ss', 'pot_third_b', etc.

    Returns:
        Estimated rating (float), or None if model unavailable or tools missing.
    """
    models = _load_positional_models()
    model = models.get(pos_col)
    if not model:
        return None
    features = model["features"]
    coefficients = model["coefficients"]
    # Map feature names to player dict keys (DB columns → RATINGS_SQL aliases)
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
            return None  # can't estimate without all tools
        vals.append(float(v))
    # intercept + sum(coeff_i * val_i)
    result = coefficients[0] + sum(coefficients[i + 1] * vals[i] for i in range(len(vals)))
    return max(0, result)


def estimate_all_positions(p):
    """Estimate ratings at all positions. Returns dict {bucket: estimated_rating}.

    Only returns positions where the model exists and all required tools are present.
    Maps DB column names to bucket names for easy comparison.
    """
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
    """Assign positional bucket — determines most valuable defensive position.

    Always uses potential ratings by default since bucketing is about where a
    player COULD play, not where they currently grade. Age should not prevent
    a player from being assigned to a position they have the tools for.
    """
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

    pos_str  = str(p.get("Pos", ""))
    role_str = str(p.get("_role", ""))
    is_pitcher = (pos_str == "P" or role_str in ("starter", "reliever", "closer"))

    if is_pitcher:
        # For MLB players evaluated on current value (use_pot=False),
        # respect actual deployment role — a reliever is valued as RP
        # regardless of ratings that might suggest SP viability.
        if not use_pot and role_str in ("reliever", "closer"):
            return "RP"
        # Trust the game's starter assignment broadly. Empirical analysis shows
        # stamina barely correlates with IP/GS in most leagues — pitchers with
        # raw stm 30+ still average 5.5+ IP/GS (clearly starters). Only override
        # the game's role when stamina is egregiously low (sub-20 raw, where a
        # pitcher literally cannot get through 3-4 innings).
        if role_str == "starter" and (p.get("Stm") or 0) >= 20:
            return "SP"
        stm    = p.get("Stm") or 0
        # Knuckleball/knuckle-curve alone qualifies as SP if stamina is sufficient
        if stm >= 25 and ((p.get("PotKnbl") or 0) >= 45 or (p.get("PotKncrv") or 0) >= 45):
            return "SP"
        viable = sum(1 for f in PITCH_FIELDS if (p.get("Pot" + f) or 0) >= 45)
        return "RP" if (viable < 3 or stm < 20) else "SP"

    if pgrade("C")  >= 45:                          return "C"
    if pgrade("SS") >= 50:
        # Check if player would be more valuable at a less premium position.
        # A borderline SS (50-55) with much better 3B/2B defense will produce
        # more WAR at the alternative position despite the positional downgrade.
        ss_grade = pgrade("SS")
        if ss_grade <= 55:
            if pgrade("3B") >= ss_grade + 10:       return "3B"
            if pgrade("2B") >= ss_grade + 10:       return "2B"
        return "SS"
    if pgrade("2B") >= 50 or pgrade("SS") >= 50:   return "2B"
    if pgrade("CF") >= 55:
        # Same logic: borderline CF with much better corner OF defense
        cf_grade = pgrade("CF")
        if cf_grade <= 55:
            best_cof = max(pgrade("LF"), pgrade("RF"))
            if best_cof >= cf_grade + 10:           return "COF"
        return "CF"
    if pgrade("LF") >= 45 or pgrade("RF") >= 45:   return "COF"
    if pgrade("3B") >= 45:                          return "3B"
    if pgrade("1B") >= 45:                          return "1B"

    # Fallback: no positional grade meets thresholds.
    # Use calibrated models to estimate positional ratings from defensive tools.
    estimates = estimate_all_positions(p)
    if estimates:
        # Apply the same thresholds to estimated ratings (with a small buffer
        # since estimates have ~2pt MAE)
        _EST_THRESHOLDS = {
            "C": 47, "SS": 52, "2B": 50, "CF": 53,
            "3B": 47, "LF": 45, "RF": 45, "1B": 40,
        }
        # Priority order: most valuable position first
        _PRIORITY = ["C", "SS", "CF", "2B", "3B", "LF", "RF", "1B"]
        for pos in _PRIORITY:
            if pos in estimates and estimates[pos] >= _EST_THRESHOLDS.get(pos, 50):
                if pos in ("LF", "RF"):
                    return "COF"
                return pos
        # No estimated position meets its threshold — player lacks the defensive
        # tools to be viable anywhere premium. Default to 1B.
        return "1B"

    # No model available — use simple heuristic based on game position
    pos_map = {"2": "C", "3": "1B", "4": "2B", "5": "3B", "6": "SS",
               "7": "COF", "8": "CF", "9": "COF", "10": "COF"}
    return pos_map.get(pos_str, "1B")

# ---------------------------------------------------------------------------
# FV calculation — re-exported from fv_model for backward compatibility
# ---------------------------------------------------------------------------
from fv_model import (  # noqa: F401
    calc_fv, dev_weight, age_development_mult,
    defensive_score, LEVEL_NORM_AGE, DEFENSIVE_WEIGHTS
)

# ---------------------------------------------------------------------------
# WAR estimation — re-exported from war_model for backward compatibility
# ---------------------------------------------------------------------------
from war_model import (  # noqa: F401
    peak_war_from_ovr, aging_mult, load_stat_history, stat_peak_war
)
from constants import OVR_TO_WAR, OVR_TO_WAR_CALIBRATED, AGING_HITTER, AGING_PITCHER, PEAK_AGE_PITCHER, PEAK_AGE_HITTER  # noqa: F401

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
    from constants import DEFAULT_DOLLARS_PER_WAR, DEFAULT_MINIMUM_SALARY
    path = get_league_dir() / "config" / "league_averages.json"
    if path.exists():
        with open(path) as f:
            data = json.load(f)
        if "dollar_per_war" in data:
            return data["dollar_per_war"]
    # Scale default by league salary level when no calibrated value exists
    min_sal = league_minimum()
    if min_sal and min_sal != DEFAULT_MINIMUM_SALARY:
        return round(DEFAULT_DOLLARS_PER_WAR * min_sal / DEFAULT_MINIMUM_SALARY)
    return DEFAULT_DOLLARS_PER_WAR


# ---------------------------------------------------------------------------
# Stat history helpers — re-exported from war_model for backward compatibility
# ---------------------------------------------------------------------------
# (already re-exported above via war_model import)

# ── PAP (Payroll Adjusted Performance) ──────────────────────────────────
from math import tanh
from constants import _w as _cw

def calc_pap(war, salary, team_games, dpw):
    """PAP from actual production. war=season WAR so far, salary=annual,
    team_games=team GP this season, dpw=$/WAR."""
    if war is None or team_games is None or team_games < 5:
        return None
    annualized = war * (162 / team_games)
    surplus = annualized * dpw - salary
    scale = _cw("PAP_SCALE", 25_000_000)
    return round(5 + 5 * tanh(surplus / scale), 2)
