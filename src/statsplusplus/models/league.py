"""League configuration and structure models.

Types for league settings, averages, team metadata, and division structure.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass(slots=True)
class TeamInfo:
    """Basic team identity."""

    team_id: int
    name: str
    abbreviation: str
    division: str = ""
    league: str = ""
    level: int = 1  # 1=MLB


@dataclass(slots=True)
class DivisionInfo:
    """Division structure."""

    name: str
    league: str
    team_ids: list[int] = field(default_factory=list)


@dataclass(slots=True)
class BattingAverages:
    """League-wide batting averages."""

    avg: float = 0.0
    obp: float = 0.0
    slg: float = 0.0
    ops: float = 0.0
    woba: float = 0.0
    babip: float = 0.0
    iso: float = 0.0
    k_pct: float = 0.0
    bb_pct: float = 0.0


@dataclass(slots=True)
class PitchingAverages:
    """League-wide pitching averages."""

    era: float = 0.0
    fip: float = 0.0
    x_fip: float = 0.0
    k_pct: float = 0.0
    bb_pct: float = 0.0
    k_bb_pct: float = 0.0
    babip: float = 0.0
    avg: float = 0.0
    obp: float = 0.0


@dataclass(slots=True)
class LeagueAverages:
    """Computed league-wide statistical baselines."""

    year: int = 0
    teams_in_sample: int = 0
    batting: BattingAverages = field(default_factory=BattingAverages)
    pitching: PitchingAverages = field(default_factory=PitchingAverages)
    dollar_per_war: int = 0


@dataclass(slots=True)
class LeagueSettings:
    """League configuration — team/position/level mappings and financial settings.

    Loaded from league_settings.json. This replaces the raw dict access pattern
    used throughout the codebase.
    """

    # Identity
    league_name: str = ""
    statsplus_slug: str = ""
    ratings_scale: str = "1-100"  # "1-100", "20-80", or "1-20"

    # Structure
    teams: list[TeamInfo] = field(default_factory=list)
    divisions: list[DivisionInfo] = field(default_factory=list)

    # Mappings (populated from league_settings.json)
    team_names: dict[int, str] = field(default_factory=dict)
    team_abbr: dict[int, str] = field(default_factory=dict)
    pos_map: dict[int, str] = field(default_factory=dict)
    role_map: dict[int, str] = field(default_factory=dict)
    level_map: dict[str, str] = field(default_factory=dict)

    # Financial
    minimum_salary: int = 825_000
    perpetual_arb: bool = False

    # Gameplay
    pyth_exp: float = 1.83
    dh_rule: str = ""
    playoff_spots: int = 6

    # State (mutable per refresh)
    my_team_id: Optional[int] = None
    year: int = 0
    game_date: str = ""

    @property
    def mlb_team_ids(self) -> set[int]:
        """Set of all MLB-level team IDs configured in this league."""
        return set(self.team_names.keys())

    def team_name(self, team_id: int) -> str:
        return self.team_names.get(team_id, "?")

    def team_abbreviation(self, team_id: int) -> str:
        return self.team_abbr.get(team_id, "?")

    def division_for_team(self, team_id: int) -> str:
        for div in self.divisions:
            if team_id in div.team_ids:
                return div.name
        return ""
