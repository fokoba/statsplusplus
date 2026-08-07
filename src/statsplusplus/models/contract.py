"""Contract and surplus models.

Types for contract data, surplus calculations, and arbitration projections.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from statsplusplus.models.player import PositionalBucket


class ContractStatus(str, Enum):
    """Contract control classification."""

    PRE_ARB = "PRE_ARB"
    ARB_1 = "ARB_1"
    ARB_2 = "ARB_2"
    ARB_3 = "ARB_3"
    RENTAL = "RENTAL"
    RENTAL_EXT = "RENTAL+EXT"
    CONTROLLED = "CONTROLLED"
    OPTION = "OPTION"
    FREE_AGENT = "FA"


@dataclass(slots=True)
class ContractInfo:
    """A player's contract details."""

    player_id: int
    team_id: int
    years: int  # Total contract length
    current_year: int  # Which year we're in (1-indexed)
    salaries: list[int] = field(default_factory=list)  # Annual salaries (up to 15 years)
    no_trade: bool = False
    # Options
    last_year_team_option: bool = False
    last_year_player_option: bool = False
    last_year_vesting_option: bool = False
    last_year_option_buyout: int = 0
    next_last_year_team_option: bool = False
    next_last_year_player_option: bool = False
    next_last_year_vesting_option: bool = False
    next_last_year_option_buyout: int = 0
    # Incentives
    minimum_pa: int = 0
    minimum_pa_bonus: int = 0
    minimum_ip: int = 0
    minimum_ip_bonus: int = 0
    mvp_bonus: int = 0
    cyyoung_bonus: int = 0
    allstar_bonus: int = 0

    @property
    def years_remaining(self) -> int:
        """Years of team control remaining (including current)."""
        return max(0, self.years - self.current_year + 1)

    @property
    def current_salary(self) -> int:
        """This year's salary."""
        idx = self.current_year - 1
        if 0 <= idx < len(self.salaries):
            return self.salaries[idx]
        return 0

    @property
    def total_remaining(self) -> int:
        """Total salary commitment remaining."""
        start = self.current_year - 1
        return sum(self.salaries[start:])

    @property
    def has_option(self) -> bool:
        return (
            self.last_year_team_option
            or self.last_year_player_option
            or self.last_year_vesting_option
        )


@dataclass(slots=True)
class SurplusYear:
    """Single-year surplus breakdown."""

    year_num: int  # 1-indexed from current year
    age: int
    projected_war: float
    market_value: int  # WAR × $/WAR
    salary: int
    surplus: int  # market_value - salary

    @property
    def is_positive(self) -> bool:
        return self.surplus > 0


@dataclass(slots=True)
class SurplusBreakdown:
    """Complete surplus calculation result for an MLB player."""

    player_id: int
    total_surplus: int
    surplus_yr1: int  # Current-year surplus only
    years: list[SurplusYear] = field(default_factory=list)
    bucket: PositionalBucket = PositionalBucket.FIRST_BASE
    # Projection inputs
    peak_war: float = 0.0
    stat_peak_war: Optional[float] = None
    ratings_peak_war: Optional[float] = None
    blend_weight: float = 0.5  # How much stat history is trusted
    # Context
    eval_date: str = ""
    ovr: int = 0
    age: int = 0
    name: str = ""


@dataclass(slots=True)
class ArbProjection:
    """Arbitration salary projection for a player."""

    player_id: int
    service_years: float
    service_days: int
    arb_year: int  # 1, 2, or 3
    projected_salary: int
    method: str = ""  # "exact_service", "games_estimate", "salary_threshold"
