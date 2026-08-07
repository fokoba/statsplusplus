"""Player data models.

Core representations of player identity, ratings, and positional classification.
These are the canonical types for passing player data between modules.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class PositionalBucket(str, Enum):
    """Positional evaluation bucket.

    Determines which weight set, WAR table, and defensive evaluation applies.
    """

    C = "C"
    SS = "SS"
    SECOND_BASE = "2B"
    THIRD_BASE = "3B"
    CF = "CF"
    COF = "COF"  # Corner outfield (LF/RF)
    FIRST_BASE = "1B"
    SP = "SP"
    RP = "RP"

    @property
    def is_pitcher(self) -> bool:
        return self in (PositionalBucket.SP, PositionalBucket.RP)

    @property
    def is_hitter(self) -> bool:
        return not self.is_pitcher

    @property
    def display_name(self) -> str:
        """Human-friendly display name (COF -> OF)."""
        return "OF" if self == PositionalBucket.COF else self.value


@dataclass(frozen=True, slots=True)
class PlayerInfo:
    """Immutable player identity and metadata."""

    player_id: int
    name: str
    age: int
    team_id: int
    parent_team_id: int
    level: int  # 1=MLB, 2=AAA, 3=AA, 4=A, 5=A-Short, 6=Rookie, 8=Intl
    pos: int  # Game position number (1=P, 2=C, ..., 10=DH)
    role: int  # OOTP role (11=SP, 12=RP, 13=CL, 0=position player)
    bats: str = ""
    throws: str = ""
    height: int = 0  # cm

    @property
    def is_pitcher(self) -> bool:
        return self.role in (11, 12, 13) or self.pos == 1

    @property
    def role_str(self) -> str:
        """Human-readable role string."""
        return {11: "starter", 12: "reliever", 13: "closer"}.get(self.role, "position_player")


@dataclass(slots=True)
class HitterRatings:
    """Offensive tool ratings (current ability)."""

    contact: int = 0
    gap: int = 0
    power: int = 0
    eye: int = 0
    avoid_k: int = 0
    speed: int = 0
    steal: int = 0
    steal_rate: int = 0
    bunt_hit: int = 0
    sac_bunt: int = 0


@dataclass(slots=True)
class PitcherRatings:
    """Pitching tool ratings (current ability)."""

    stuff: int = 0
    movement: int = 0
    control: int = 0
    stamina: int = 0
    velocity: str = ""
    # Individual pitches (0 = doesn't have this pitch)
    fastball: int = 0
    sinker: int = 0
    curveball: int = 0
    slider: int = 0
    changeup: int = 0
    splitter: int = 0
    cutter: int = 0
    circle_change: int = 0
    screwball: int = 0
    forkball: int = 0
    knuckle_curve: int = 0
    knuckleball: int = 0
    # Split ratings
    control_l: int = 0
    control_r: int = 0
    stuff_l: int = 0
    stuff_r: int = 0
    movement_l: int = 0
    movement_r: int = 0


@dataclass(slots=True)
class DefensiveRatings:
    """Defensive tool and positional ratings."""

    # Positional grades (current)
    c: int = 0
    first_b: int = 0
    second_b: int = 0
    third_b: int = 0
    ss: int = 0
    lf: int = 0
    cf: int = 0
    rf: int = 0
    # Positional grades (potential)
    pot_c: int = 0
    pot_first_b: int = 0
    pot_second_b: int = 0
    pot_third_b: int = 0
    pot_ss: int = 0
    pot_lf: int = 0
    pot_cf: int = 0
    pot_rf: int = 0
    # Underlying defensive tools
    infield_range: int = 0
    infield_arm: int = 0
    infield_error: int = 0
    turn_dp: int = 0
    outfield_range: int = 0
    outfield_arm: int = 0
    outfield_error: int = 0
    catcher_arm: int = 0
    catcher_block: int = 0
    catcher_frame: int = 0


@dataclass(slots=True)
class PersonalityTraits:
    """Character and personality ratings."""

    intelligence: str = "N"  # VL, L, N, H, VH
    work_ethic: str = "N"
    greed: str = "N"
    loyalty: str = "N"
    leadership: str = "N"
    accuracy: str = "N"  # Scouting accuracy: VL, L, N, H, VH


@dataclass(slots=True)
class PlayerRatings:
    """Complete player ratings snapshot.

    This is the primary input to the evaluation engine. Combines all rating
    categories into a single typed container that replaces the raw player dict.
    """

    info: PlayerInfo
    ovr: int = 0
    pot: int = 0
    hitter: HitterRatings = field(default_factory=HitterRatings)
    pitcher: PitcherRatings = field(default_factory=PitcherRatings)
    defense: DefensiveRatings = field(default_factory=DefensiveRatings)
    personality: PersonalityTraits = field(default_factory=PersonalityTraits)
    # Potential versions of hitter/pitcher tools
    pot_hitter: HitterRatings = field(default_factory=HitterRatings)
    pot_pitcher: PitcherRatings = field(default_factory=PitcherRatings)
    # Extended ratings (league-dependent; 0 = not available)
    babip: int = 0
    hra: int = 0
    pbabip: int = 0
    pot_babip: int = 0
    pot_hra: int = 0
    pot_pbabip: int = 0
    # Computed scores (populated by evaluation engine, stored in DB)
    composite_score: Optional[int] = None
    ceiling_score: Optional[int] = None
    tool_only_score: Optional[int] = None
    secondary_composite: Optional[int] = None
    true_ceiling: Optional[int] = None
    # Split hitting ratings
    contact_l: int = 0
    contact_r: int = 0
    gap_l: int = 0
    gap_r: int = 0
    power_l: int = 0
    power_r: int = 0
    eye_l: int = 0
    eye_r: int = 0
    avoid_k_l: int = 0
    avoid_k_r: int = 0

    @property
    def is_pitcher(self) -> bool:
        return self.info.is_pitcher

    @property
    def bucket(self) -> Optional[PositionalBucket]:
        """Return bucket if already computed, None otherwise."""
        # Bucket assignment requires logic — this is just a convenience
        # for when the bucket has been pre-assigned on the info level.
        return None
