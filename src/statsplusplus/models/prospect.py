"""Prospect evaluation models.

Types for FV grades, risk labels, and prospect surplus calculations.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional

from statsplusplus.models.player import PositionalBucket


class RiskLabel(str, Enum):
    """Development risk classification."""

    LOW = "Low"
    MEDIUM = "Medium"
    HIGH = "High"
    EXTREME = "Extreme"

    @property
    def initial(self) -> str:
        """Single-character display initial."""
        return self.value[0]

    @property
    def sort_order(self) -> int:
        """Lower = less risky (for sorting)."""
        return {
            RiskLabel.LOW: 0,
            RiskLabel.MEDIUM: 1,
            RiskLabel.HIGH: 2,
            RiskLabel.EXTREME: 3,
        }[self]


class FVGrade(int, Enum):
    """Standard FV grades on the 20-80 scouting scale in 5-point increments.

    FV answers: "how good could this player become if he develops?"
    """

    FV_20 = 20
    FV_25 = 25
    FV_30 = 30
    FV_35 = 35
    FV_40 = 40
    FV_45 = 45
    FV_50 = 50
    FV_55 = 55
    FV_60 = 60
    FV_65 = 65
    FV_70 = 70
    FV_75 = 75
    FV_80 = 80

    @classmethod
    def from_continuous(cls, continuous_fv: float) -> "FVGrade":
        """Snap a continuous FV value to the nearest valid grade."""
        snapped = max(20, min(80, round(continuous_fv / 5) * 5))
        return cls(snapped)

    @property
    def label(self) -> str:
        """Human-readable label for this grade."""
        labels = {
            20: "Non-prospect",
            25: "Non-prospect",
            30: "Non-prospect",
            35: "Org filler",
            40: "Low-level depth",
            45: "MLB depth / platoon",
            50: "Average regular",
            55: "Above-average regular",
            60: "All-Star caliber",
            65: "Perennial All-Star",
            70: "MVP candidate",
            75: "Franchise player",
            80: "Generational talent",
        }
        return labels.get(self.value, "")


@dataclass(slots=True)
class ProspectEvaluation:
    """Complete prospect evaluation output.

    Combines FV grade, risk label, and surplus calculation into one result.
    This is what gets stored in the prospect_fv table and displayed in the UI.
    """

    player_id: int
    fv_grade: int  # Rounded to nearest 5 (20-80)
    fv_continuous: float  # Pre-rounding continuous FV for surplus interpolation
    risk: RiskLabel
    bucket: PositionalBucket
    level: str  # Display level (e.g., "AAA", "AA", "A")
    surplus: int  # Dollar surplus over 6 years of team control

    # Optional context
    eval_date: str = ""
    ceiling: int = 0
    composite: int = 0

    @property
    def fv_str(self) -> str:
        """Display string (e.g., '50', '55+')."""
        return str(self.fv_grade)

    @property
    def tier(self) -> str:
        """Tier label based on FV grade."""
        if self.fv_grade >= 60:
            return "elite"
        if self.fv_grade >= 55:
            return "plus"
        if self.fv_grade >= 50:
            return "average"
        if self.fv_grade >= 45:
            return "fringe"
        return "depth"
