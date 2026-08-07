"""Tests for statsplusplus.models — typed data contracts."""

import pytest

from statsplusplus.models.player import (
    PlayerInfo,
    PlayerRatings,
    HitterRatings,
    PitcherRatings,
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
    ContractStatus,
    SurplusBreakdown,
    SurplusYear,
    ArbProjection,
)
from statsplusplus.models.league import (
    LeagueSettings,
    LeagueAverages,
    BattingAverages,
    PitchingAverages,
    TeamInfo,
    DivisionInfo,
)


# ---------------------------------------------------------------------------
# PositionalBucket
# ---------------------------------------------------------------------------

class TestPositionalBucket:
    def test_pitcher_detection(self):
        assert PositionalBucket.SP.is_pitcher is True
        assert PositionalBucket.RP.is_pitcher is True
        assert PositionalBucket.SS.is_pitcher is False
        assert PositionalBucket.CF.is_pitcher is False

    def test_hitter_detection(self):
        assert PositionalBucket.SS.is_hitter is True
        assert PositionalBucket.C.is_hitter is True
        assert PositionalBucket.SP.is_hitter is False

    def test_display_name(self):
        assert PositionalBucket.COF.display_name == "OF"
        assert PositionalBucket.SS.display_name == "SS"
        assert PositionalBucket.SP.display_name == "SP"

    def test_str_enum_value(self):
        assert PositionalBucket.SECOND_BASE == "2B"
        assert PositionalBucket.THIRD_BASE == "3B"
        assert PositionalBucket.FIRST_BASE == "1B"


# ---------------------------------------------------------------------------
# PlayerInfo
# ---------------------------------------------------------------------------

class TestPlayerInfo:
    def test_construction(self):
        info = PlayerInfo(
            player_id=12345, name="Mike Trout", age=31,
            team_id=44, parent_team_id=44, level=1, pos=8, role=0,
        )
        assert info.name == "Mike Trout"
        assert info.is_pitcher is False
        assert info.role_str == "position_player"

    def test_pitcher_by_role(self):
        info = PlayerInfo(
            player_id=1, name="Ace", age=25,
            team_id=1, parent_team_id=1, level=1, pos=1, role=11,
        )
        assert info.is_pitcher is True
        assert info.role_str == "starter"

    def test_pitcher_by_pos(self):
        info = PlayerInfo(
            player_id=2, name="Reliever", age=28,
            team_id=1, parent_team_id=1, level=1, pos=1, role=13,
        )
        assert info.is_pitcher is True
        assert info.role_str == "closer"

    def test_frozen(self):
        info = PlayerInfo(
            player_id=1, name="Test", age=25,
            team_id=1, parent_team_id=1, level=1, pos=2, role=0,
        )
        with pytest.raises(AttributeError):
            info.age = 26  # type: ignore[misc]


# ---------------------------------------------------------------------------
# PlayerRatings
# ---------------------------------------------------------------------------

class TestPlayerRatings:
    def test_default_construction(self):
        info = PlayerInfo(
            player_id=1, name="Test", age=20,
            team_id=1, parent_team_id=1, level=4, pos=6, role=0,
        )
        ratings = PlayerRatings(info=info, ovr=55, pot=70)
        assert ratings.ovr == 55
        assert ratings.pot == 70
        assert ratings.hitter.contact == 0
        assert ratings.pitcher.stuff == 0
        assert ratings.is_pitcher is False

    def test_hitter_ratings(self):
        hr = HitterRatings(contact=65, gap=50, power=70, eye=55, avoid_k=45)
        assert hr.contact == 65
        assert hr.power == 70

    def test_pitcher_ratings(self):
        pr = PitcherRatings(
            stuff=70, movement=60, control=55, stamina=50,
            fastball=75, slider=65, changeup=50,
        )
        assert pr.stuff == 70
        assert pr.fastball == 75
        assert pr.curveball == 0


# ---------------------------------------------------------------------------
# RiskLabel
# ---------------------------------------------------------------------------

class TestRiskLabel:
    def test_initial(self):
        assert RiskLabel.LOW.initial == "L"
        assert RiskLabel.EXTREME.initial == "E"

    def test_sort_order(self):
        labels = [RiskLabel.HIGH, RiskLabel.LOW, RiskLabel.EXTREME, RiskLabel.MEDIUM]
        sorted_labels = sorted(labels, key=lambda x: x.sort_order)
        assert sorted_labels == [RiskLabel.LOW, RiskLabel.MEDIUM, RiskLabel.HIGH, RiskLabel.EXTREME]

    def test_str_value(self):
        assert RiskLabel.LOW == "Low"
        assert RiskLabel.MEDIUM == "Medium"


# ---------------------------------------------------------------------------
# FVGrade
# ---------------------------------------------------------------------------

class TestFVGrade:
    def test_from_continuous_rounding(self):
        assert FVGrade.from_continuous(52.3) == FVGrade.FV_50
        assert FVGrade.from_continuous(53.0) == FVGrade.FV_55
        assert FVGrade.from_continuous(47.8) == FVGrade.FV_50
        assert FVGrade.from_continuous(67.1) == FVGrade.FV_65

    def test_from_continuous_clamps(self):
        assert FVGrade.from_continuous(15.0) == FVGrade.FV_20
        assert FVGrade.from_continuous(95.0) == FVGrade.FV_80

    def test_label(self):
        assert FVGrade.FV_50.label == "Average regular"
        assert FVGrade.FV_60.label == "All-Star caliber"
        assert FVGrade.FV_70.label == "MVP candidate"

    def test_int_value(self):
        assert FVGrade.FV_55 == 55
        assert FVGrade.FV_45 + 5 == 50


# ---------------------------------------------------------------------------
# ProspectEvaluation
# ---------------------------------------------------------------------------

class TestProspectEvaluation:
    def test_construction(self):
        pe = ProspectEvaluation(
            player_id=100, fv_grade=55, fv_continuous=53.7,
            risk=RiskLabel.MEDIUM, bucket=PositionalBucket.SS,
            level="AA", surplus=15_000_000,
        )
        assert pe.fv_str == "55"
        assert pe.tier == "plus"
        assert pe.risk.initial == "M"

    def test_tier_classification(self):
        def _tier(fv):
            return ProspectEvaluation(
                player_id=1, fv_grade=fv, fv_continuous=float(fv),
                risk=RiskLabel.LOW, bucket=PositionalBucket.SP,
                level="A", surplus=0,
            ).tier

        assert _tier(60) == "elite"
        assert _tier(55) == "plus"
        assert _tier(50) == "average"
        assert _tier(45) == "fringe"
        assert _tier(40) == "depth"


# ---------------------------------------------------------------------------
# ContractInfo
# ---------------------------------------------------------------------------

class TestContractInfo:
    def test_years_remaining(self):
        c = ContractInfo(
            player_id=1, team_id=1, years=5, current_year=3,
            salaries=[5_000_000, 6_000_000, 7_000_000, 8_000_000, 9_000_000],
        )
        assert c.years_remaining == 3
        assert c.current_salary == 7_000_000
        assert c.total_remaining == 24_000_000  # 7M + 8M + 9M

    def test_single_year(self):
        c = ContractInfo(
            player_id=1, team_id=1, years=1, current_year=1,
            salaries=[825_000],
        )
        assert c.years_remaining == 1
        assert c.current_salary == 825_000
        assert c.total_remaining == 825_000

    def test_has_option(self):
        c_no_opt = ContractInfo(player_id=1, team_id=1, years=2, current_year=1)
        assert c_no_opt.has_option is False

        c_opt = ContractInfo(
            player_id=1, team_id=1, years=2, current_year=1,
            last_year_team_option=True,
        )
        assert c_opt.has_option is True


# ---------------------------------------------------------------------------
# SurplusBreakdown
# ---------------------------------------------------------------------------

class TestSurplusBreakdown:
    def test_construction(self):
        years = [
            SurplusYear(year_num=1, age=28, projected_war=3.5, market_value=28_000_000, salary=10_000_000, surplus=18_000_000),
            SurplusYear(year_num=2, age=29, projected_war=3.2, market_value=25_600_000, salary=12_000_000, surplus=13_600_000),
        ]
        sb = SurplusBreakdown(
            player_id=1, total_surplus=31_600_000, surplus_yr1=18_000_000,
            years=years, bucket=PositionalBucket.SS, peak_war=4.0,
        )
        assert sb.total_surplus == 31_600_000
        assert len(sb.years) == 2
        assert sb.years[0].is_positive is True


# ---------------------------------------------------------------------------
# LeagueSettings
# ---------------------------------------------------------------------------

class TestLeagueSettings:
    def test_team_lookup(self):
        settings = LeagueSettings(
            league_name="EMLB",
            team_names={44: "Anaheim Angels", 45: "Baltimore Orioles"},
            team_abbr={44: "ANA", 45: "BAL"},
        )
        assert settings.team_name(44) == "Anaheim Angels"
        assert settings.team_abbreviation(45) == "BAL"
        assert settings.team_name(999) == "?"
        assert settings.mlb_team_ids == {44, 45}

    def test_division_lookup(self):
        settings = LeagueSettings(
            divisions=[
                DivisionInfo(name="AL West", league="AL", team_ids=[44, 50, 55]),
                DivisionInfo(name="AL East", league="AL", team_ids=[45, 46, 47]),
            ],
        )
        assert settings.division_for_team(44) == "AL West"
        assert settings.division_for_team(45) == "AL East"
        assert settings.division_for_team(999) == ""


# ---------------------------------------------------------------------------
# EvaluationContext
# ---------------------------------------------------------------------------

class TestEvaluationContext:
    def test_construction(self):
        ctx = EvaluationContext(
            bucket=PositionalBucket.SS,
            norm_age=24,
            level="aa",
            is_pitcher=False,
            composite_score=55,
            ceiling_score=65,
        )
        assert ctx.bucket == PositionalBucket.SS
        assert ctx.norm_age == 24
        assert ctx.stat_risk_modifier == 0.0


# ---------------------------------------------------------------------------
# StatSeason
# ---------------------------------------------------------------------------

class TestStatSeason:
    def test_partial_season(self):
        s = StatSeason(year=2033, war=2.5, season_pct=0.6, ab=350)
        assert s.season_pct == 0.6
        assert s.war == 2.5

    def test_full_season(self):
        s = StatSeason(year=2032, war=4.2, ab=580)
        assert s.season_pct == 1.0
        assert s.incomplete is False
