"""Tests for statsplusplus.evaluation — pure computation layer.

These tests verify that the new evaluation module produces identical results
to the legacy scripts/ code. Critical for the migration: any divergence here
means we've introduced a regression.
"""

import sys
from pathlib import Path

import pytest

# New evaluation code
from statsplusplus.evaluation.composite import (
    compute_composite_hitter,
    compute_composite_pitcher,
    compute_composite_mlb,
    compute_offensive_grade,
    compute_baserunning_value,
    compute_defensive_value,
    compute_tool_only_score,
    stat_to_2080,
    pitcher_stat_to_2080,
    tool_transform,
    sub_mlb_floor_penalty,
)
from statsplusplus.evaluation.war import (
    peak_war_from_score,
    aging_mult,
    stat_peak_war,
    _weighted_war,
)
from statsplusplus.evaluation.constants import (
    ModelWeights,
    AGING_HITTER,
    AGING_PITCHER,
    OVR_TO_WAR_DEFAULT,
    load_model_weights,
)

# Legacy code for comparison
BASE = Path(__file__).parent.parent.parent
sys.path.insert(0, str(BASE / "scripts"))


# ---------------------------------------------------------------------------
# Tool Transform
# ---------------------------------------------------------------------------

class TestToolTransform:
    def test_linear_region(self):
        """40-60 should be linear (identity)."""
        assert tool_transform(50.0) == 50.0
        assert tool_transform(45.0) == 45.0
        assert tool_transform(55.0) == 55.0

    def test_below_40_penalty(self):
        """Below 40: each point below is worth 1.5x."""
        # 30 → 40 - (40-30)*1.5 = 40 - 15 = 25
        assert tool_transform(30.0) == 25.0
        # 35 → 40 - (40-35)*1.5 = 40 - 7.5 = 32.5
        assert tool_transform(35.0) == 32.5

    def test_above_60_bonus(self):
        """Above 60: each point above is worth 1.3x."""
        # 70 → 60 + (70-60)*1.3 = 60 + 13 = 73
        assert tool_transform(70.0) == 73.0
        # 65 → 60 + (65-60)*1.3 = 60 + 6.5 = 66.5
        assert tool_transform(65.0) == 66.5

    def test_boundary_values(self):
        assert tool_transform(40.0) == 40.0
        assert tool_transform(60.0) == 60.0

    def test_matches_legacy(self):
        """Verify identical to legacy _tool_transform."""
        from evaluation_engine import _tool_transform
        for val in [20, 25, 30, 35, 40, 45, 50, 55, 60, 65, 70, 75, 80]:
            assert tool_transform(float(val)) == _tool_transform(float(val)), f"Mismatch at {val}"


# ---------------------------------------------------------------------------
# Sub-MLB Floor Penalty
# ---------------------------------------------------------------------------

class TestSubMlbFloorPenalty:
    def test_no_penalty_above_floor(self):
        tools = {"contact": 50, "power": 45, "eye": 60}
        assert sub_mlb_floor_penalty(tools) == 0.0

    def test_penalty_below_floor(self):
        tools = {"contact": 30, "power": 50}
        # 5 points below floor × 0.25 = 1.25
        assert sub_mlb_floor_penalty(tools) == pytest.approx(1.25)

    def test_none_values_ignored(self):
        tools = {"contact": None, "power": 30}
        assert sub_mlb_floor_penalty(tools) == pytest.approx(1.25)

    def test_matches_legacy(self):
        from evaluation_engine import _sub_mlb_floor_penalty
        test_cases = [
            {"contact": 50, "power": 60, "eye": 45},
            {"contact": 25, "power": 30, "eye": 50},
            {"contact": 35, "power": None, "eye": 20},
        ]
        for tools in test_cases:
            assert sub_mlb_floor_penalty(tools) == _sub_mlb_floor_penalty(tools)


# ---------------------------------------------------------------------------
# Composite Hitter
# ---------------------------------------------------------------------------

class TestCompositeHitter:
    @pytest.fixture
    def standard_weights(self):
        return {"contact": 0.30, "gap": 0.18, "power": 0.22, "eye": 0.16,
                "speed": 0.03, "steal": 0.02, "stl_rt": 0.01, "defense": 0.05}

    @pytest.fixture
    def def_weights(self):
        return {"IFR": 0.40, "IFE": 0.20, "IFA": 0.20, "TDP": 0.20}

    def test_average_player(self, standard_weights, def_weights):
        tools = {"contact": 50, "gap": 50, "power": 50, "eye": 50,
                 "speed": 50, "steal": 50, "stl_rt": 50}
        defense = {"IFR": 50, "IFE": 50, "IFA": 50, "TDP": 50}
        result = compute_composite_hitter(tools, standard_weights, defense, def_weights)
        assert 45 <= result <= 55  # Average player should be near 50

    def test_elite_player(self, standard_weights, def_weights):
        tools = {"contact": 70, "gap": 65, "power": 75, "eye": 65,
                 "speed": 55, "steal": 50, "stl_rt": 50}
        defense = {"IFR": 60, "IFE": 55, "IFA": 55, "TDP": 55}
        result = compute_composite_hitter(tools, standard_weights, defense, def_weights)
        assert result >= 65

    def test_poor_player(self, standard_weights, def_weights):
        tools = {"contact": 30, "gap": 35, "power": 30, "eye": 35,
                 "speed": 40, "steal": 30, "stl_rt": 30}
        defense = {"IFR": 35, "IFE": 30, "IFA": 30, "TDP": 30}
        result = compute_composite_hitter(tools, standard_weights, defense, def_weights)
        assert result <= 35

    def test_missing_tools_handled(self, standard_weights, def_weights):
        tools = {"contact": 50, "gap": None, "power": 55, "eye": None,
                 "speed": None, "steal": None, "stl_rt": None}
        defense = {}
        result = compute_composite_hitter(tools, standard_weights, defense, def_weights)
        assert 20 <= result <= 80

    def test_all_none_returns_floor(self, standard_weights, def_weights):
        tools = {"contact": None, "gap": None, "power": None, "eye": None}
        result = compute_composite_hitter(tools, standard_weights, {}, def_weights)
        assert result == 20

    def test_result_range(self, standard_weights, def_weights):
        """Result is always in [20, 80]."""
        for _ in range(100):
            import random
            tools = {k: random.randint(20, 80) for k in
                     ["contact", "gap", "power", "eye", "speed", "steal", "stl_rt"]}
            defense = {k: random.randint(20, 80) for k in ["IFR", "IFE", "IFA", "TDP"]}
            result = compute_composite_hitter(tools, standard_weights, defense, def_weights)
            assert 20 <= result <= 80

    def test_matches_legacy(self, standard_weights, def_weights):
        """Verify identical output to legacy compute_composite_hitter."""
        from evaluation_engine import compute_composite_hitter as legacy_hitter
        test_profiles = [
            {"contact": 55, "gap": 50, "power": 60, "eye": 45, "speed": 50, "steal": 45, "stl_rt": 45},
            {"contact": 70, "gap": 65, "power": 40, "eye": 70, "speed": 60, "steal": 55, "stl_rt": 50},
            {"contact": 35, "gap": 40, "power": 70, "eye": 30, "speed": 45, "steal": 40, "stl_rt": 40},
        ]
        defense = {"IFR": 55, "IFE": 50, "IFA": 50, "TDP": 50}
        for tools in test_profiles:
            new = compute_composite_hitter(tools, standard_weights, defense, def_weights)
            old = legacy_hitter(tools, standard_weights, defense, def_weights)
            assert new == old, f"Mismatch for {tools}: new={new}, old={old}"


# ---------------------------------------------------------------------------
# Composite Pitcher
# ---------------------------------------------------------------------------

class TestCompositePitcher:
    @pytest.fixture
    def sp_weights(self):
        return {"stuff": 0.35, "movement": 0.25, "control": 0.30, "arsenal": 0.10}

    def test_average_sp(self, sp_weights):
        tools = {"stuff": 50, "movement": 50, "control": 50}
        arsenal = {"Fst": 55, "Sld": 50, "Chg": 45}
        result = compute_composite_pitcher(tools, sp_weights, arsenal, 55, "SP")
        assert 47 <= result <= 55

    def test_elite_sp(self, sp_weights):
        tools = {"stuff": 70, "movement": 65, "control": 65}
        arsenal = {"Fst": 75, "Sld": 70, "Crv": 60, "Chg": 55}
        result = compute_composite_pitcher(tools, sp_weights, arsenal, 60, "SP")
        assert result >= 65

    def test_low_stamina_penalty(self, sp_weights):
        tools = {"stuff": 55, "movement": 55, "control": 55}
        arsenal = {"Fst": 55, "Sld": 50}
        high_stm = compute_composite_pitcher(tools, sp_weights, arsenal, 55, "SP")
        low_stm = compute_composite_pitcher(tools, sp_weights, arsenal, 30, "SP")
        assert low_stm < high_stm

    def test_matches_legacy(self, sp_weights):
        from evaluation_engine import compute_composite_pitcher as legacy_pitcher
        tools = {"stuff": 60, "movement": 55, "control": 50}
        arsenal = {"Fst": 65, "Sld": 55, "Chg": 50}
        new = compute_composite_pitcher(tools, sp_weights, arsenal, 50, "SP")
        old = legacy_pitcher(tools, sp_weights, arsenal, 50, "SP")
        assert new == old


# ---------------------------------------------------------------------------
# Stat Conversion
# ---------------------------------------------------------------------------

class TestStatConversion:
    def test_stat_to_2080_average(self):
        """OPS+ 100 → 50 (league average)."""
        assert stat_to_2080(100.0) == pytest.approx(50.0)

    def test_stat_to_2080_elite(self):
        """OPS+ 150 → 65."""
        assert stat_to_2080(150.0) == pytest.approx(65.0)

    def test_stat_to_2080_poor(self):
        """OPS+ 50 → 35."""
        assert stat_to_2080(50.0) == pytest.approx(35.0)

    def test_stat_to_2080_clamped(self):
        assert stat_to_2080(0.0) == 20.0
        assert stat_to_2080(300.0) == 80.0

    def test_pitcher_stat_to_2080_average(self):
        assert pitcher_stat_to_2080(100.0) == pytest.approx(50.0)

    def test_pitcher_stat_asymmetric(self):
        """Above average uses steeper slope."""
        above = pitcher_stat_to_2080(120.0)  # 50 + 20*0.45 = 59
        below = pitcher_stat_to_2080(80.0)   # 50 + (-20)*0.30 = 44
        assert above == pytest.approx(59.0)
        assert below == pytest.approx(44.0)


# ---------------------------------------------------------------------------
# MLB Stat Blending
# ---------------------------------------------------------------------------

class TestCompositeMLB:
    def test_no_stats_returns_tool_score(self):
        assert compute_composite_mlb(55, []) == 55

    def test_stats_blend_in(self):
        result = compute_composite_mlb(55, [60.0, 58.0, 55.0])
        assert result > 55  # Stats pull up

    def test_young_player_trusts_tools_more(self):
        """Young player with tools > stats should lean toward tools."""
        old = compute_composite_mlb(65, [50.0, 50.0], peak_age=28, player_age=28)
        young = compute_composite_mlb(65, [50.0, 50.0], peak_age=28, player_age=23)
        assert young > old  # Young player trusts tools more

    def test_matches_legacy(self):
        from evaluation_engine import compute_composite_mlb as legacy_mlb
        cases = [
            (55, [60.0, 58.0], 28, 28, False),
            (65, [50.0, 45.0, 48.0], 28, 30, False),
            (50, [55.0], 27, 24, True),
        ]
        for tool, stats, peak, age, is_p in cases:
            new = compute_composite_mlb(tool, stats, peak, age, is_p)
            old = legacy_mlb(tool, stats, peak, age, is_p)
            assert new == old, f"Mismatch: tool={tool}, stats={stats}"


# ---------------------------------------------------------------------------
# WAR Projection
# ---------------------------------------------------------------------------

class TestWarProjection:
    def test_peak_war_defaults_hitter(self):
        """Without calibrated weights, uses default table."""
        war = peak_war_from_score(60, "SS")
        assert 2.5 < war < 4.0

    def test_peak_war_defaults_sp(self):
        war = peak_war_from_score(60, "SP")
        assert 2.0 < war < 3.5

    def test_peak_war_defaults_rp(self):
        war = peak_war_from_score(60, "RP")
        assert 0.5 < war < 1.5

    def test_peak_war_monotonic(self):
        """Higher score → higher WAR."""
        for bucket in ["SS", "SP", "RP"]:
            prev = 0.0
            for score in range(40, 81, 5):
                war = peak_war_from_score(score, bucket)
                assert war >= prev, f"Non-monotonic at {bucket}/{score}"
                prev = war

    def test_aging_mult_at_peak(self):
        assert aging_mult(27, "SS") == 1.0
        assert aging_mult(28, "SP") == 1.0

    def test_aging_mult_decline(self):
        assert aging_mult(32, "SS") < 1.0
        assert aging_mult(35, "SP") < aging_mult(32, "SP")

    def test_aging_mult_young(self):
        """Below peak age returns 1.0."""
        assert aging_mult(22, "SS") == 1.0
        assert aging_mult(24, "SP") == 1.0

    def test_matches_legacy(self):
        """Verify default-path WAR matches legacy _interp() on default table."""
        from war_model import _interp
        from statsplusplus.evaluation.constants import OVR_TO_WAR_DEFAULT
        for score in [40, 50, 55, 60, 65, 70, 75]:
            for bucket, col in [("SS", 1), ("SP", 2), ("RP", 3)]:
                new = peak_war_from_score(score, bucket)
                old = _interp(OVR_TO_WAR_DEFAULT, score, col)
                assert abs(new - old) < 0.01, f"WAR mismatch at {bucket}/{score}: {new} vs {old}"
        # Aging curve should always match (no calibrated version exists)
        from war_model import aging_mult as legacy_aging
        for age in [25, 28, 30, 33, 36, 40]:
            for bucket in ["SS", "SP"]:
                new = aging_mult(age, bucket)
                old = legacy_aging(age, bucket)
                assert abs(new - old) < 0.001, f"Aging mismatch at {bucket}/{age}"


# ---------------------------------------------------------------------------
# Stat Peak WAR
# ---------------------------------------------------------------------------

class TestStatPeakWar:
    def test_basic_hitter(self):
        bat_hist = {1: [
            {"year": 2033, "war": 4.0, "season_pct": 1.0, "incomplete": False},
            {"year": 2032, "war": 3.5, "season_pct": 1.0, "incomplete": False},
            {"year": 2031, "war": 3.0, "season_pct": 1.0, "incomplete": False},
        ]}
        result = stat_peak_war(1, "SS", bat_hist, {})
        assert result is not None
        # Weighted: (4*3 + 3.5*3 + 3*2) / (3+3+2) = 28.5/8 = 3.5625
        assert result == pytest.approx(3.5625)

    def test_no_history_returns_none(self):
        assert stat_peak_war(1, "SS", {}, {}) is None

    def test_partial_season_weighting(self):
        bat_hist = {1: [
            {"year": 2033, "war": 2.0, "season_pct": 0.5, "incomplete": False},
            {"year": 2032, "war": 4.0, "season_pct": 1.0, "incomplete": False},
        ]}
        result = stat_peak_war(1, "SS", bat_hist, {})
        assert result is not None
        # Weight[0] = 3 * 0.5 = 1.5, Weight[1] = 3
        # (2.0*1.5 + 4.0*3) / (1.5+3) = 15/4.5 = 3.333
        assert result == pytest.approx(15.0 / 4.5)


# ---------------------------------------------------------------------------
# Model Weights
# ---------------------------------------------------------------------------

class TestModelWeights:
    def test_empty_weights_uses_defaults(self):
        mw = ModelWeights()
        assert mw.fv_to_peak_war == {80: 10.0, 70: 7.0, 65: 5.5, 60: 4.2,
                                      55: 2.9, 50: 2.0, 45: 1.2, 40: 0.5}
        assert mw.arb_pct == {1: 0.20, 2: 0.22, 3: 0.33}

    def test_calibrated_ovr_to_war(self):
        mw = ModelWeights(raw={
            "OVR_TO_WAR": {"SS": {"60": 3.5, "70": 6.0}},
        })
        assert mw.ovr_to_war == {"SS": {60: 3.5, 70: 6.0}}

    def test_load_missing_file(self, tmp_path):
        mw = load_model_weights(tmp_path)
        assert mw.raw == {}
        assert mw.fv_to_peak_war is not None  # Falls back to default

# ---------------------------------------------------------------------------
# Ceiling Score
# ---------------------------------------------------------------------------

from statsplusplus.evaluation.ceiling import (
    compute_ceiling,
    compute_true_ceiling,
    compute_component_ceilings,
    _potential_weight,
)


class TestCeiling:
    @pytest.fixture
    def ss_weights(self):
        return {"contact": 0.30, "gap": 0.18, "power": 0.22, "eye": 0.16,
                "speed": 0.03, "steal": 0.02, "stl_rt": 0.01, "defense": 0.05}

    @pytest.fixture
    def def_weights(self):
        return {"IFR": 0.40, "IFE": 0.20, "IFA": 0.20, "TDP": 0.20}

    def test_potential_weight_young(self):
        """Age 16 → 0.95 potential weight."""
        assert _potential_weight(16) == 0.95

    def test_potential_weight_old(self):
        """Age 30+ → 0.30 potential weight."""
        assert _potential_weight(30) == 0.30
        assert _potential_weight(35) == 0.30

    def test_potential_weight_mid(self):
        """Age 22 → 0.70."""
        assert _potential_weight(22) == pytest.approx(0.70)

    def test_ceiling_never_below_composite(self, ss_weights, def_weights):
        pot_tools = {"contact": 55, "gap": 50, "power": 60, "eye": 50,
                     "speed": 50, "steal": 50, "stl_rt": 50}
        defense = {"IFR": 50, "IFE": 50, "IFA": 50, "TDP": 50}
        result = compute_ceiling(
            pot_tools, ss_weights, composite_score=55,
            defense=defense, def_weights=def_weights, age=22,
        )
        assert result >= 55

    def test_ceiling_higher_for_young_player(self, ss_weights, def_weights):
        pot_tools = {"contact": 70, "gap": 60, "power": 65, "eye": 60,
                     "speed": 55, "steal": 50, "stl_rt": 50}
        defense = {"IFR": 55, "IFE": 50, "IFA": 50, "TDP": 50}
        young = compute_ceiling(
            pot_tools, ss_weights, composite_score=45,
            defense=defense, def_weights=def_weights, age=18,
        )
        old = compute_ceiling(
            pot_tools, ss_weights, composite_score=45,
            defense=defense, def_weights=def_weights, age=28,
        )
        assert young > old

    def test_true_ceiling_no_age_blend(self, ss_weights, def_weights):
        """True ceiling should be higher than age-blended ceiling for young players."""
        pot_tools = {"contact": 70, "gap": 65, "power": 70, "eye": 60,
                     "speed": 55, "steal": 50, "stl_rt": 50}
        defense = {"IFR": 60, "IFE": 55, "IFA": 55, "TDP": 55}
        true_ceil = compute_true_ceiling(
            pot_tools, ss_weights, composite_score=50,
            defense=defense, def_weights=def_weights,
        )
        # True ceiling reflects pure potential — should be high
        assert true_ceil >= 60

    def test_ceiling_clamped(self, ss_weights, def_weights):
        pot_tools = {"contact": 80, "gap": 80, "power": 80, "eye": 80,
                     "speed": 80, "steal": 80, "stl_rt": 80}
        defense = {"IFR": 80, "IFE": 80, "IFA": 80, "TDP": 80}
        result = compute_ceiling(
            pot_tools, ss_weights, composite_score=75,
            defense=defense, def_weights=def_weights, age=18,
        )
        assert result <= 80

    def test_pitcher_ceiling(self):
        sp_weights = {"stuff": 0.35, "movement": 0.25, "control": 0.30, "arsenal": 0.10}
        pot_tools = {"stuff": 70, "movement": 65, "control": 60}
        result = compute_ceiling(
            pot_tools, sp_weights, composite_score=50,
            is_pitcher=True, arsenal={"Fst": 70, "Sld": 60, "Chg": 55},
            stamina=55, role="SP", age=20,
        )
        assert result >= 55


# ---------------------------------------------------------------------------
# FV Grade
# ---------------------------------------------------------------------------

from statsplusplus.evaluation.fv import calc_fv, dev_weight, age_development_mult


class TestFV:
    def test_basic_fv_average_prospect(self):
        """50 ceiling, 45 composite → FV around 45."""
        grade, risk, continuous = calc_fv(
            ovr=45, pot=52, age=21, bucket="SS", norm_age=24,
        )
        assert 40 <= grade <= 50
        assert risk in ("Low", "Medium", "High", "Extreme")

    def test_elite_prospect(self):
        """High ceiling, young age → high FV."""
        grade, risk, continuous = calc_fv(
            ovr=50, pot=70, age=19, bucket="SS", norm_age=22,
        )
        assert grade >= 55

    def test_rp_cap(self):
        """RP FV capped at 55."""
        grade, risk, continuous = calc_fv(
            ovr=55, pot=75, age=20, bucket="RP", norm_age=22,
        )
        assert grade <= 55

    def test_accuracy_penalty(self):
        """Acc=L reduces FV."""
        normal_grade, _, _ = calc_fv(
            ovr=50, pot=65, age=20, bucket="CF", norm_age=22,
        )
        low_acc_grade, _, _ = calc_fv(
            ovr=50, pot=65, age=20, bucket="CF", norm_age=22,
            accuracy="L",
        )
        assert low_acc_grade < normal_grade

    def test_risk_low_for_realized(self):
        """Small gap (ovr close to pot) → Low risk."""
        _, risk, _ = calc_fv(
            ovr=55, pot=57, age=24, bucket="SS", norm_age=26,
        )
        assert risk == "Low"

    def test_risk_extreme_for_raw(self):
        """Young player with massive gap → High/Extreme risk."""
        _, risk, _ = calc_fv(
            ovr=30, pot=70, age=17, bucket="SS", norm_age=22,
        )
        assert risk in ("High", "Extreme")

    def test_fv_continuous_returned(self):
        """Continuous FV should be close to the grade but not necessarily equal."""
        grade, _, continuous = calc_fv(
            ovr=48, pot=62, age=21, bucket="SS", norm_age=24,
        )
        assert abs(continuous - grade) <= 5  # Within one tier

    def test_offensive_ceiling_cap(self):
        """Bat-limited hitter capped at FV 50."""
        grade, _, _ = calc_fv(
            ovr=50, pot=65, age=20, bucket="SS", norm_age=22,
            offensive_ceiling=40,
        )
        assert grade <= 50


class TestDevWeight:
    def test_young_for_level(self):
        """3+ years young for level → high weight on ceiling."""
        w = dev_weight(age=18, norm_age=22)
        assert w >= 0.50

    def test_old_for_level(self):
        """2+ years old for level → low weight on ceiling."""
        w = dev_weight(age=26, norm_age=24)
        assert w <= 0.15

    def test_age_decay_applied(self):
        """Older prospects get less development credit."""
        young = dev_weight(age=20, norm_age=22)
        old = dev_weight(age=24, norm_age=24)
        assert young > old

    def test_low_level_boost(self):
        """Low-level players get a boost vs same age/norm without level."""
        without_level = dev_weight(age=19, norm_age=19)  # diff=0 → base 0.35
        with_low_level = dev_weight(age=19, norm_age=19, level="intl")  # +0.10 → 0.45
        assert with_low_level > without_level


class TestAgeDevelopmentMult:
    def test_young_returns_above_one(self):
        assert age_development_mult(18) > 1.0

    def test_21_returns_one(self):
        assert age_development_mult(21) == pytest.approx(1.0)

    def test_old_decays(self):
        assert age_development_mult(24) < 1.0
        assert age_development_mult(26) < age_development_mult(24)

    def test_pitcher_retains_more(self):
        """Pitchers retain more development runway at each age."""
        for age in [22, 23, 24, 25]:
            pitcher = age_development_mult(age, is_pitcher=True)
            hitter = age_development_mult(age, is_pitcher=False)
            assert pitcher >= hitter


# ---------------------------------------------------------------------------
# Arb Salary
# ---------------------------------------------------------------------------

from statsplusplus.evaluation.arb import arb_salary as new_arb_salary, arb_salary_perpetual


class TestArbSalary:
    def test_first_year_hitter(self):
        """First arb year salary is exponential from OVR."""
        sal = new_arb_salary(ovr=55, bucket="SS", arb_year=1, prior_salary=825_000, min_sal=825_000)
        assert sal > 825_000
        assert sal < 10_000_000

    def test_rp_uses_separate_model(self):
        """RP has its own exponential model."""
        rp_sal = new_arb_salary(ovr=55, bucket="RP", arb_year=1, prior_salary=825_000, min_sal=825_000)
        hit_sal = new_arb_salary(ovr=55, bucket="SS", arb_year=1, prior_salary=825_000, min_sal=825_000)
        # RP model produces different values (calibrated separately)
        assert rp_sal != hit_sal

    def test_arb_year_2_raises(self):
        """Arb year 2+ adds a raise to prior salary."""
        yr1 = new_arb_salary(ovr=55, bucket="SS", arb_year=1, prior_salary=825_000, min_sal=825_000)
        yr2 = new_arb_salary(ovr=55, bucket="SS", arb_year=2, prior_salary=yr1, min_sal=825_000)
        assert yr2 > yr1

    def test_higher_ovr_higher_salary(self):
        """Better player = higher arb salary."""
        low = new_arb_salary(ovr=50, bucket="SS", arb_year=1, prior_salary=825_000, min_sal=825_000)
        high = new_arb_salary(ovr=65, bucket="SS", arb_year=1, prior_salary=825_000, min_sal=825_000)
        assert high > low

    def test_matches_legacy(self):
        """Verify identical to legacy arb_salary."""
        from arb_model import arb_salary as legacy_arb
        cases = [
            (55, "SS", 1, 825_000, 825_000),
            (60, "SP", 2, 2_000_000, 825_000),
            (50, "RP", 1, 825_000, 825_000),
            (65, "CF", 3, 5_000_000, 825_000),
        ]
        for ovr, bucket, yr, prior, min_sal in cases:
            new = new_arb_salary(ovr, bucket, yr, prior, min_sal)
            old = legacy_arb(ovr, bucket, yr, prior, min_sal)
            assert new == old, f"Mismatch: ovr={ovr}, bucket={bucket}, yr={yr}"


class TestArbSalaryPerpetual:
    def test_minimum_floor(self):
        """Salary never drops below league minimum."""
        sal = arb_salary_perpetual(age=22, projected_war=0.5, dpw=8_000_000,
                                   min_sal=825_000, career_war=1.0)
        assert sal >= 825_000

    def test_career_war_matters(self):
        """More career WAR = higher salary."""
        low_career = arb_salary_perpetual(age=28, projected_war=3.0, dpw=8_000_000,
                                          min_sal=825_000, career_war=5.0)
        high_career = arb_salary_perpetual(age=28, projected_war=3.0, dpw=8_000_000,
                                           min_sal=825_000, career_war=20.0)
        assert high_career > low_career

    def test_ceiling_from_current_war(self):
        """Salary capped by current production level."""
        sal = arb_salary_perpetual(age=32, projected_war=1.0, dpw=8_000_000,
                                   min_sal=825_000, career_war=30.0)
        # Ceiling = 0.35 * 1.0 * 8M = $2.8M. Growth may be higher but ceiling caps it.
        assert sal <= 3_000_000


# ---------------------------------------------------------------------------
# Prospect Surplus
# ---------------------------------------------------------------------------

from statsplusplus.evaluation.surplus import (
    prospect_surplus as new_prospect_surplus,
    peak_war_from_fv,
    scarcity_multiplier,
    age_adjusted_discount,
    market_value as new_market_value,
    certainty_multiplier,
)


class TestProspectSurplus:
    def test_basic_surplus_positive(self):
        """FV 50 prospect at A-ball should have positive surplus."""
        result = new_prospect_surplus(
            fv=50.0, age=20, level="A", bucket="SS",
            dpw=8_000_000, min_sal=825_000,
        )
        assert result["total_surplus"] > 0
        assert len(result["breakdown"]) == 6

    def test_higher_fv_higher_surplus(self):
        """FV 60 > FV 50 surplus."""
        low = new_prospect_surplus(
            fv=50.0, age=20, level="AA", bucket="SS",
            dpw=8_000_000, min_sal=825_000,
        )
        high = new_prospect_surplus(
            fv=60.0, age=20, level="AA", bucket="SS",
            dpw=8_000_000, min_sal=825_000,
        )
        assert high["total_surplus"] > low["total_surplus"]

    def test_younger_higher_surplus(self):
        """Younger player at same level = higher surplus (more control)."""
        young = new_prospect_surplus(
            fv=55.0, age=19, level="A", bucket="CF",
            dpw=8_000_000, min_sal=825_000,
        )
        old = new_prospect_surplus(
            fv=55.0, age=23, level="A", bucket="CF",
            dpw=8_000_000, min_sal=825_000,
        )
        assert young["total_surplus"] > old["total_surplus"]

    def test_ss_surplus_gt_1b(self):
        """SS surplus > 1B surplus at same FV due to scarcity (with defense)."""
        ss = new_prospect_surplus(
            fv=50.0, age=20, level="AA", bucket="SS",
            dpw=8_000_000, min_sal=825_000, pot=50, def_rating=65,
        )
        first = new_prospect_surplus(
            fv=50.0, age=20, level="AA", bucket="1B",
            dpw=8_000_000, min_sal=825_000, pot=50, def_rating=40,
        )
        assert ss["total_surplus"] > first["total_surplus"]

    def test_six_year_breakdown(self):
        """Breakdown has exactly 6 control years."""
        result = new_prospect_surplus(
            fv=50.0, age=21, level="AAA", bucket="SP",
            dpw=8_000_000, min_sal=825_000,
        )
        assert len(result["breakdown"]) == 6
        for row in result["breakdown"]:
            assert "control_year" in row
            assert "war" in row
            assert "salary" in row


class TestPeakWarFromFv:
    def test_monotonic(self):
        """Higher FV → higher peak WAR."""
        prev = 0.0
        for fv in [40, 45, 50, 55, 60, 65, 70]:
            war = peak_war_from_fv(float(fv), "SS")
            assert war >= prev
            prev = war

    def test_rp_lower_than_hitter(self):
        """RP peak WAR lower than hitter at same FV."""
        rp = peak_war_from_fv(55.0, "RP")
        hit = peak_war_from_fv(55.0, "SS")
        assert rp < hit


class TestScarcityMultiplier:
    def test_low_ceiling_zero(self):
        """Very low ceiling → zero scarcity."""
        assert scarcity_multiplier(40.0) < 0.05

    def test_high_ceiling_full(self):
        """High ceiling → full scarcity."""
        assert scarcity_multiplier(55.0) >= 0.95

    def test_premium_position_boost(self):
        """SS gets a positive shift (higher effective ceiling)."""
        ss = scarcity_multiplier(50.0, bucket="SS", def_rating=60)
        generic = scarcity_multiplier(50.0)
        assert ss > generic

    def test_discount_position_penalty(self):
        """1B gets a negative shift."""
        first = scarcity_multiplier(50.0, bucket="1B")
        generic = scarcity_multiplier(50.0)
        assert first < generic


class TestMarketValue:
    def test_zero_war(self):
        assert new_market_value(0.0, 8_000_000, 825_000) == 825_000

    def test_negative_war(self):
        assert new_market_value(-1.0, 8_000_000, 825_000) == 825_000

    def test_above_one_war(self):
        assert new_market_value(3.0, 8_000_000, 825_000) == 24_000_000

    def test_linear_ramp_below_one(self):
        """Between 0 and 1 WAR: linear ramp from min_sal to dpw."""
        val = new_market_value(0.5, 8_000_000, 825_000)
        assert 825_000 < val < 8_000_000


class TestCertaintyMultiplier:
    def test_high_realization(self):
        """Near-maxed player → ~1.0."""
        assert certainty_multiplier(60, 62) >= 0.95

    def test_low_realization(self):
        """Very raw player → still above 0.85 (formula is gentle)."""
        mult = certainty_multiplier(30, 70)
        assert 0.85 <= mult <= 1.0

    def test_moderate_realization(self):
        """Mid-range realization → 0.90-1.0."""
        mult = certainty_multiplier(40, 65)
        assert 0.85 <= mult <= 1.0

    def test_no_data(self):
        assert certainty_multiplier(0, 0) == 1.0


# ---------------------------------------------------------------------------
# Carrying Tool Bonus
# ---------------------------------------------------------------------------

from statsplusplus.evaluation.carrying_tools import (
    compute_carrying_tool_bonus as new_ct_bonus,
    tool_scarcity_multiplier,
    CARRYING_TOOL_ELIGIBLE,
)


class TestCarryingToolBonus:
    @pytest.fixture
    def ss_config(self):
        return {
            "positions": {
                "SS": {
                    "carrying_tools": {
                        "contact": {"war_premium_factor": 0.30},
                        "power": {"war_premium_factor": 0.35},
                        "eye": {"war_premium_factor": 0.22},
                    }
                },
            },
            "scarcity_schedule": [
                {"threshold": 65, "multiplier": 1.0},
                {"threshold": 70, "multiplier": 1.5},
                {"threshold": 75, "multiplier": 2.0},
                {"threshold": 80, "multiplier": 3.0},
            ],
        }

    def test_no_bonus_below_threshold(self, ss_config):
        tools = {"contact": 60, "gap": 55, "power": 50, "eye": 55}
        bonus, breakdown = new_ct_bonus(tools, "SS", ss_config)
        assert bonus == 0.0
        assert breakdown == []

    def test_bonus_above_threshold(self, ss_config):
        tools = {"contact": 70, "gap": 55, "power": 55, "eye": 55}
        bonus, breakdown = new_ct_bonus(tools, "SS", ss_config)
        assert bonus > 0.0
        assert len(breakdown) == 1
        assert breakdown[0]["tool"] == "contact"

    def test_multiple_carrying_tools(self, ss_config):
        tools = {"contact": 70, "gap": 55, "power": 75, "eye": 70}
        bonus, breakdown = new_ct_bonus(tools, "SS", ss_config)
        assert len(breakdown) == 3  # contact, power, eye all qualify

    def test_unknown_position_no_bonus(self, ss_config):
        tools = {"contact": 70, "power": 70}
        bonus, breakdown = new_ct_bonus(tools, "DH", ss_config)
        assert bonus == 0.0

    def test_none_tools_handled(self, ss_config):
        tools = {"contact": None, "power": 70, "eye": None}
        bonus, breakdown = new_ct_bonus(tools, "SS", ss_config)
        assert len(breakdown) == 1
        assert breakdown[0]["tool"] == "power"

    def test_matches_legacy(self, ss_config):
        """Verify identical to legacy compute_carrying_tool_bonus."""
        from evaluation_engine import compute_carrying_tool_bonus as legacy_ct
        tools = {"contact": 72, "gap": 60, "power": 68, "eye": 65}
        new_bonus, new_bd = new_ct_bonus(tools, "SS", ss_config)
        old_bonus, old_bd = legacy_ct(tools, "SS", ss_config)
        assert abs(new_bonus - old_bonus) < 0.001
        assert len(new_bd) == len(old_bd)


class TestToolScarcityMultiplier:
    def test_below_first_breakpoint(self):
        schedule = [{"threshold": 65, "multiplier": 1.0}, {"threshold": 80, "multiplier": 3.0}]
        assert tool_scarcity_multiplier(60, schedule) == 1.0

    def test_above_last_breakpoint(self):
        schedule = [{"threshold": 65, "multiplier": 1.0}, {"threshold": 80, "multiplier": 3.0}]
        assert tool_scarcity_multiplier(85, schedule) == 3.0

    def test_interpolation(self):
        schedule = [{"threshold": 60, "multiplier": 1.0}, {"threshold": 80, "multiplier": 3.0}]
        # Midpoint: 70 → 2.0
        assert tool_scarcity_multiplier(70, schedule) == pytest.approx(2.0)

    def test_empty_schedule(self):
        assert tool_scarcity_multiplier(70, []) == 1.0
