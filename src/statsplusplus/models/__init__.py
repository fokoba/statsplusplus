"""Typed data models — contracts between layers.

All cross-module data flows through these types. No raw dicts at boundaries.
"""

from statsplusplus.models.player import (
    PlayerInfo,
    PlayerRatings,
    PitcherRatings,
    HitterRatings,
    DefensiveRatings,
    PersonalityTraits,
    PositionalBucket,
)
from statsplusplus.models.evaluation import (
    EvaluationResult,
    EvaluationContext,
    CompositeWeights,
    RecombinationWeights,
    StatSeason,
)
from statsplusplus.models.prospect import (
    ProspectEvaluation,
    RiskLabel,
    FVGrade,
)
from statsplusplus.models.contract import (
    ContractInfo,
    SurplusBreakdown,
    SurplusYear,
    ArbProjection,
)
from statsplusplus.models.league import (
    LeagueSettings,
    LeagueAverages,
    TeamInfo,
    DivisionInfo,
)

__all__ = [
    # player
    "PlayerInfo",
    "PlayerRatings",
    "PitcherRatings",
    "HitterRatings",
    "DefensiveRatings",
    "PersonalityTraits",
    "PositionalBucket",
    # evaluation
    "EvaluationResult",
    "EvaluationContext",
    "CompositeWeights",
    "RecombinationWeights",
    "StatSeason",
    # prospect
    "ProspectEvaluation",
    "RiskLabel",
    "FVGrade",
    # contract
    "ContractInfo",
    "SurplusBreakdown",
    "SurplusYear",
    "ArbProjection",
    # league
    "LeagueSettings",
    "LeagueAverages",
    "TeamInfo",
    "DivisionInfo",
]
