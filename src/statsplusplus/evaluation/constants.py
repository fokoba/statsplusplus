"""Model constants and calibrated weight loading.

All valuation model constants live here. Calibrated values are loaded from
model_weights.json via load_model_weights(). No module-level I/O — the
loader must be called explicitly with a league directory path.

Design: Instead of the old pattern (module-level global `_weights` loaded
lazily on first access), this module provides:
  - Default constant values (always available, no I/O)
  - A `load_model_weights(league_dir)` function that returns calibrated values
  - A `ModelWeights` dataclass that bundles all calibrated tables together
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional


# ---------------------------------------------------------------------------
# Peak ages
# ---------------------------------------------------------------------------

PEAK_AGE_PITCHER: int = 27
PEAK_AGE_HITTER: int = 28

# ---------------------------------------------------------------------------
# Aging curves
# ---------------------------------------------------------------------------

AGING_HITTER: dict[int, float] = {
    27: 1.00, 28: 1.00, 29: 0.97, 30: 0.92, 31: 0.84,
    32: 0.76, 33: 0.68, 34: 0.60, 35: 0.51, 36: 0.42,
    37: 0.34, 38: 0.25, 39: 0.17, 40: 0.10, 42: 0.04,
}

AGING_PITCHER: dict[int, float] = {
    27: 1.00, 28: 1.00, 29: 0.97, 30: 0.93, 31: 0.85,
    32: 0.76, 33: 0.66, 34: 0.54, 35: 0.43, 36: 0.33,
    37: 0.24, 38: 0.16, 39: 0.10, 40: 0.05,
}

# ---------------------------------------------------------------------------
# OVR → WAR fallback table (used when no calibrated tables exist)
# ---------------------------------------------------------------------------

OVR_TO_WAR_DEFAULT: list[tuple[int, float, float, float]] = [
    # (Ovr, hitter_WAR, SP_WAR, RP_WAR)
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

# ---------------------------------------------------------------------------
# FV → peak WAR tables (defaults, overridden by calibration)
# ---------------------------------------------------------------------------

FV_TO_PEAK_WAR_DEFAULT: dict[int, float] = {
    80: 10.0, 70: 7.0, 65: 5.5, 60: 4.2, 55: 2.9, 50: 2.0, 45: 1.2, 40: 0.5,
}

FV_TO_PEAK_WAR_RP_DEFAULT: dict[int, float] = {
    80: 3.2, 70: 2.6, 65: 2.3, 60: 2.0, 55: 1.6, 50: 1.2, 45: 0.8, 40: 0.5,
}

# ---------------------------------------------------------------------------
# Prospect surplus model defaults
# ---------------------------------------------------------------------------

ARB_PCT_DEFAULT: dict[int, float] = {1: 0.20, 2: 0.22, 3: 0.33}

PROSPECT_DISCOUNT_RATE: float = 0.05
LEVEL_AGE_DISCOUNT_RATE: float = 0.04

PROSPECT_WAR_RAMP: dict[int, float] = {1: 0.60, 2: 0.80}

NO_TRACK_RECORD_DISCOUNT: float = 0.50

RP_POT_DISCOUNT: float = 0.85

SCARCITY_MULT_DEFAULT: dict[int, float] = {
    42: 0.0, 44: 0.03, 45: 0.10, 46: 0.20, 47: 0.35, 48: 0.50,
    49: 0.65, 50: 0.80, 51: 0.90, 52: 0.97, 53: 1.0, 80: 1.0,
}

# ---------------------------------------------------------------------------
# MLB contract surplus model defaults
# ---------------------------------------------------------------------------

DEFAULT_MINIMUM_SALARY: int = 825_000
DEFAULT_DOLLARS_PER_WAR: int = 9_000_000

MLB_SCARCITY: dict[str, float] = {
    "SS": 1.10, "CF": 1.06, "SP": 1.06, "C": 1.03, "2B": 1.03, "3B": 1.03,
    "COF": 0.94, "RP": 0.94, "1B": 0.91,
}

# Arb salary model constants
ARB_HITTER_BASE: int = 318_400
ARB_HITTER_EXP: float = 0.0495
ARB_RP_BASE: int = 566_254
ARB_RP_EXP: float = 0.0294
ARB_RAISE_INTERCEPT: int = -2_500_000
ARB_RAISE_SLOPE: int = 110_000
ARB_RAISE_MIN: int = 1_000_000
ARB_DEEP_SALARY_THRESHOLD: int = 5_500_000

# ---------------------------------------------------------------------------
# Service time
# ---------------------------------------------------------------------------

SERVICE_GAMES_HITTER: int = 162
SERVICE_STARTS_SP: int = 32
SERVICE_GAMES_RP: int = 65

# ---------------------------------------------------------------------------
# Calibration settings
# ---------------------------------------------------------------------------

MIN_REGRESSION_N: int = 40
CALIBRATION_YEARS: int = 3

# ---------------------------------------------------------------------------
# Tool transform constants
# ---------------------------------------------------------------------------

TOOL_TRANSFORM_LOW_THRESHOLD: float = 40.0
TOOL_TRANSFORM_HIGH_THRESHOLD: float = 60.0
TOOL_TRANSFORM_LOW_PENALTY: float = 1.5
TOOL_TRANSFORM_HIGH_BONUS: float = 1.3
MLB_TOOL_FLOOR: int = 35
FLOOR_PENALTY_RATE: float = 0.25

# ---------------------------------------------------------------------------
# Composite imbalance penalty parameters
# ---------------------------------------------------------------------------

# Hitter: penalizes one-tool profiles (e.g., elite power + poor everything else)
HITTER_IMBALANCE_SPREAD_THRESHOLD: int = 25
HITTER_IMBALANCE_SPREAD_RATE: float = 0.15
HITTER_IMBALANCE_WEAKNESS_THRESHOLD: int = 45
HITTER_IMBALANCE_WEAKNESS_RATE: float = 0.12

# Pitcher: penalizes one-dimensional pitchers (e.g., elite stuff + poor control)
PITCHER_IMBALANCE_SPREAD_THRESHOLD: int = 20
PITCHER_IMBALANCE_SPREAD_RATE: float = 0.20
PITCHER_IMBALANCE_WEAKNESS_THRESHOLD: int = 50
PITCHER_IMBALANCE_WEAKNESS_RATE: float = 0.15

# ---------------------------------------------------------------------------
# Stat confidence curve parameters
# ---------------------------------------------------------------------------

STAT_CONFIDENCE_PA_FULL: float = 400.0
STAT_CONFIDENCE_IP_FULL: float = 120.0
STAT_CONFIDENCE_PA_MIN: int = 50
STAT_CONFIDENCE_IP_MIN: float = 15.0
STAT_CONFIDENCE_EXPONENT: float = 1.4

# ---------------------------------------------------------------------------
# Player value model parameters
# ---------------------------------------------------------------------------

# Near-maxed prospect blend: fades FV-based projection when a player has
# nearly reached their ceiling (prevents inflation for average established players)
NEAR_MAXED_REALIZATION_THRESHOLD: float = 0.7
NEAR_MAXED_DENOMINATOR: float = 0.3

# Option value: premium for young, high-ceiling prospects with unrealized upside
OPTION_VALUE_FV_FLOOR: float = 45.0      # No option value below this FV
OPTION_VALUE_FV_FULL: float = 55.0       # Full option value above this FV
OPTION_VALUE_GAP_DIVISOR: float = 25.0   # Normalizes ceiling-composite gap
OPTION_VALUE_YOUTH_PIVOT: int = 22       # Age at which youth factor starts fading
OPTION_VALUE_YOUTH_RANGE: float = 5.0    # Age range over which youth fades
OPTION_VALUE_MULTIPLIER: float = 0.30    # Max upside premium (30%)

# RP surplus discount: reliever production is volatile and replaceable
RP_DISCOUNT_BASE: float = 0.35          # Discount for replacement-level RPs
RP_DISCOUNT_RANGE: float = 0.30         # Additional discount for elite RPs
RP_DISCOUNT_WAR_FLOOR: float = 0.5      # WAR below which base discount applies
RP_DISCOUNT_WAR_CEILING: float = 2.5    # WAR above which max discount applies

# ---------------------------------------------------------------------------
# Calibrated model weights container
# ---------------------------------------------------------------------------


@dataclass
class ModelWeights:
    """Container for all league-calibrated model weights.

    Loaded from model_weights.json. Provides typed access to calibrated
    tables with fallback to defaults when keys are missing.
    """

    raw: dict[str, Any] = field(default_factory=dict)

    # Cached parsed tables
    _ovr_to_war: Optional[dict[str, dict[int, float]]] = field(default=None, repr=False)
    _composite_to_war: Optional[dict[str, dict[int, float]]] = field(default=None, repr=False)
    _fv_to_peak_war: Optional[dict[int, float]] = field(default=None, repr=False)
    _fv_to_peak_war_sp: Optional[dict[int, float]] = field(default=None, repr=False)
    _fv_to_peak_war_rp: Optional[dict[int, float]] = field(default=None, repr=False)
    _fv_to_peak_war_by_pos: Optional[dict[str, dict[int, float]]] = field(default=None, repr=False)
    _arb_pct: Optional[dict[int, float]] = field(default=None, repr=False)
    _scarcity_mult: Optional[dict[int, float]] = field(default=None, repr=False)
    _years_to_mlb: Optional[dict[str, float]] = field(default=None, repr=False)

    def get(self, key: str, default: object = None) -> object:
        """Get a raw value from the weights dict with fallback."""
        return self.raw.get(key, default)

    @property
    def ovr_to_war(self) -> Optional[dict[str, dict[int, float]]]:
        """Position-specific OVR_TO_WAR tables."""
        if self._ovr_to_war is None:
            raw = self.raw.get("OVR_TO_WAR")
            if raw and isinstance(raw, dict):
                self._ovr_to_war = {
                    bucket: {int(k): v for k, v in tbl.items()}
                    for bucket, tbl in raw.items() if isinstance(tbl, dict)
                }
        return self._ovr_to_war

    @property
    def composite_to_war(self) -> Optional[dict[str, dict[int, float]]]:
        """Position-specific COMPOSITE_TO_WAR tables."""
        if self._composite_to_war is None:
            raw = self.raw.get("COMPOSITE_TO_WAR")
            if raw and isinstance(raw, dict):
                self._composite_to_war = {
                    bucket: {int(k): v for k, v in tbl.items()}
                    for bucket, tbl in raw.items() if isinstance(tbl, dict)
                }
        return self._composite_to_war

    @property
    def fv_to_peak_war(self) -> dict[int, float]:
        """FV → peak WAR table (hitter default)."""
        if self._fv_to_peak_war is None:
            raw = self.raw.get("FV_TO_PEAK_WAR")
            if raw and isinstance(raw, dict):
                self._fv_to_peak_war = {int(k): v for k, v in raw.items()}
            else:
                self._fv_to_peak_war = dict(FV_TO_PEAK_WAR_DEFAULT)
        return self._fv_to_peak_war

    @property
    def fv_to_peak_war_sp(self) -> dict[int, float]:
        """SP-specific FV → peak WAR table."""
        if self._fv_to_peak_war_sp is None:
            raw = self.raw.get("FV_TO_PEAK_WAR_SP")
            if raw and isinstance(raw, dict):
                self._fv_to_peak_war_sp = {int(k): v for k, v in raw.items()}
            else:
                self._fv_to_peak_war_sp = dict(FV_TO_PEAK_WAR_DEFAULT)
        return self._fv_to_peak_war_sp

    @property
    def fv_to_peak_war_rp(self) -> dict[int, float]:
        """RP-specific FV → peak WAR table."""
        if self._fv_to_peak_war_rp is None:
            raw = self.raw.get("FV_TO_PEAK_WAR_RP")
            if raw and isinstance(raw, dict):
                self._fv_to_peak_war_rp = {int(k): v for k, v in raw.items()}
            else:
                self._fv_to_peak_war_rp = dict(FV_TO_PEAK_WAR_RP_DEFAULT)
        return self._fv_to_peak_war_rp

    @property
    def fv_to_peak_war_by_pos(self) -> Optional[dict[str, dict[int, float]]]:
        """Per-position hitter FV → peak WAR tables."""
        if self._fv_to_peak_war_by_pos is None:
            raw = self.raw.get("FV_TO_PEAK_WAR_BY_POS")
            if raw and isinstance(raw, dict):
                self._fv_to_peak_war_by_pos = {
                    bucket: {int(k): v for k, v in tbl.items()}
                    for bucket, tbl in raw.items() if isinstance(tbl, dict)
                }
        return self._fv_to_peak_war_by_pos

    @property
    def arb_pct(self) -> dict[int, float]:
        """Arb salary as % of market value by year."""
        if self._arb_pct is None:
            raw = self.raw.get("ARB_PCT")
            if raw and isinstance(raw, dict):
                self._arb_pct = {int(k): v for k, v in raw.items()}
            else:
                self._arb_pct = dict(ARB_PCT_DEFAULT)
        return self._arb_pct

    @property
    def scarcity_mult(self) -> dict[int, float]:
        """Scarcity multiplier by ceiling tier."""
        if self._scarcity_mult is None:
            raw = self.raw.get("SCARCITY_MULT")
            if raw and isinstance(raw, dict):
                self._scarcity_mult = {int(k): v for k, v in raw.items()}
            else:
                self._scarcity_mult = dict(SCARCITY_MULT_DEFAULT)
        return self._scarcity_mult

    @property
    def years_to_mlb(self) -> dict[str, float]:
        """Years to MLB by level."""
        if self._years_to_mlb is None:
            from statsplusplus.utils.positions import YEARS_TO_MLB as _default
            raw = self.raw.get("YEARS_TO_MLB")
            if raw and isinstance(raw, dict):
                self._years_to_mlb = dict(raw)
            else:
                self._years_to_mlb = dict(_default)
        return self._years_to_mlb

    @property
    def aging_curve_hitter(self) -> dict[int, float]:
        """League-specific hitter aging curve. Calibrated or default."""
        raw = self.raw.get("AGING_CURVE_HITTER")
        if raw and isinstance(raw, dict):
            return {int(k): v for k, v in raw.items()}
        return dict(AGING_HITTER)

    @property
    def aging_curve_pitcher(self) -> dict[int, float]:
        """League-specific pitcher aging curve. Calibrated or default."""
        raw = self.raw.get("AGING_CURVE_PITCHER")
        if raw and isinstance(raw, dict):
            return {int(k): v for k, v in raw.items()}
        return dict(AGING_PITCHER)

    def get_param(self, key: str, default: float) -> float:
        """Get a tuning parameter: league-calibrated if available, else default.

        Tuning parameters are stored in model_weights.json under a
        "MODEL_PARAMS" dict. This provides a single override path for all
        parameters that may vary by league (aging, imbalance rates, stat
        confidence curve, etc.).

        Args:
            key: Parameter name (matches the constant name in constants.py).
            default: Fallback value if not calibrated for this league.

        Returns:
            Float parameter value.
        """
        params = self.raw.get("MODEL_PARAMS")
        if params and isinstance(params, dict) and key in params:
            return float(params[key])
        return default


def load_model_weights(league_dir: Path) -> ModelWeights:
    """Load calibrated model weights from a league's config directory.

    Reads model_weights.json. Returns an empty ModelWeights (with all
    defaults) if the file is missing or invalid.

    Args:
        league_dir: Path to the league data directory (e.g., data/emlb).

    Returns:
        ModelWeights instance with parsed calibrated tables.
    """
    path = league_dir / "config" / "model_weights.json"
    if not path.exists():
        return ModelWeights()
    try:
        with open(path) as f:
            raw = json.load(f)
        return ModelWeights(raw=raw)
    except (json.JSONDecodeError, OSError):
        return ModelWeights()


# ---------------------------------------------------------------------------
# Defensive weights per positional bucket
# ---------------------------------------------------------------------------

DEFENSIVE_WEIGHTS: dict[str, dict[str, float]] = {
    "C":      {"CFrm": 0.45, "CBlk": 0.35, "CArm": 0.20},
    "SS":     {"IFR": 0.40, "IFE": 0.20, "IFA": 0.20, "TDP": 0.20},
    "2B":     {"IFR": 0.35, "TDP": 0.30, "IFE": 0.20, "IFA": 0.15},
    "3B":     {"IFA": 0.35, "IFE": 0.30, "IFR": 0.25, "TDP": 0.10},
    "CF":     {"OFR": 0.55, "OFE": 0.25, "OFA": 0.20},
    "COF_LF": {"OFR": 0.50, "OFE": 0.30, "OFA": 0.20},
    "COF_RF": {"OFR": 0.40, "OFA": 0.35, "OFE": 0.25},
}
