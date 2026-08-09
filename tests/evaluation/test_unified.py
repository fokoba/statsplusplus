"""Tests for the unified evaluation model.

Covers stat_confidence() and unified_surplus() with cases for:
- Pure prospects (0 MLB time)
- Crossover players (partial MLB time)
- Established veterans (full MLB career)
- Edge cases (spring training, tiny samples, two-way)
"""

import pytest
from unittest.mock import patch

from statsplusplus.evaluation.unified import (
    stat_confidence,
    unified_surplus,
    _PA_FULL_CONFIDENCE,
    _IP_FULL_CONFIDENCE,
    _PA_MIN_SIGNAL,
    _IP_MIN_SIGNAL,
)


# ---------------------------------------------------------------------------
# stat_confidence tests
# ---------------------------------------------------------------------------

class TestStatConfidence:
    """Test the stat_confidence ramp function."""

    def test_zero_pa_zero_ip(self):
        """No MLB time → 0 confidence."""
        assert stat_confidence(0, 0.0) == 0.0

    def test_below_minimum_threshold(self):
        """Below minimum signal threshold → 0 confidence."""
        assert stat_confidence(20, 0.0) == 0.0
        assert stat_confidence(0, 10.0) == 0.0
        assert stat_confidence(_PA_MIN_SIGNAL - 1, _IP_MIN_SIGNAL - 1) == 0.0

    def test_at_minimum_threshold(self):
        """At minimum threshold → small but nonzero confidence."""
        pa_conf = stat_confidence(_PA_MIN_SIGNAL, 0.0)
        assert 0.0 < pa_conf < 0.2
        ip_conf = stat_confidence(0, _IP_MIN_SIGNAL)
        assert 0.0 < ip_conf < 0.2

    def test_midpoint(self):
        """Half of full confidence PA → ~0.5."""
        mid_pa = int(_PA_FULL_CONFIDENCE / 2)
        conf = stat_confidence(mid_pa, 0.0)
        assert 0.4 <= conf <= 0.6

    def test_full_confidence_pa(self):
        """At full confidence threshold → 1.0."""
        assert stat_confidence(int(_PA_FULL_CONFIDENCE), 0.0) == 1.0

    def test_full_confidence_ip(self):
        """At full IP confidence threshold → 1.0."""
        assert stat_confidence(0, _IP_FULL_CONFIDENCE) == 1.0

    def test_above_threshold_caps_at_one(self):
        """Above threshold still returns 1.0."""
        assert stat_confidence(800, 0.0) == 1.0
        assert stat_confidence(0, 300.0) == 1.0

    def test_pa_dominates_when_higher(self):
        """PA-based confidence used when it's higher than IP-based."""
        # 300 PA > 30 IP in terms of confidence
        conf = stat_confidence(300, 30.0)
        pa_only = stat_confidence(300, 0.0)
        assert conf == pa_only

    def test_ip_dominates_when_higher(self):
        """IP-based confidence used when it's higher than PA-based."""
        # 100 IP > 60 PA in terms of confidence
        conf = stat_confidence(60, 100.0)
        ip_only = stat_confidence(0, 100.0)
        assert conf == ip_only

    def test_monotonically_increasing(self):
        """Confidence never decreases as PA increases."""
        prev = 0.0
        for pa in range(0, 600, 25):
            curr = stat_confidence(pa, 0.0)
            assert curr >= prev
            prev = curr

    def test_two_way_player(self):
        """Two-way player gets max of PA and IP confidence."""
        # 200 PA and 80 IP — both meaningful
        conf = stat_confidence(200, 80.0)
        pa_only = stat_confidence(200, 0.0)
        ip_only = stat_confidence(0, 80.0)
        assert conf == max(pa_only, ip_only)


# ---------------------------------------------------------------------------
# unified_surplus tests — pure prospect cases
# ---------------------------------------------------------------------------

class TestUnifiedSurplusPureProspect:
    """Players with 0 MLB time should produce prospect-like evaluations."""

    def _base_args(self, **overrides):
        """Default arguments for a typical FV 55 prospect."""
        args = dict(
            fv_continuous=55.0,
            bucket="SS",
            age=20,
            level="AA",
            composite=45,
            ceiling=62,
            career_pa=0,
            career_ip=0.0,
            stat_war=None,
            years_control=6,
            salaries=[840_000] * 6,
            dpw=7_000_000,
            min_sal=840_000,
        )
        args.update(overrides)
        return args

    def test_zero_stat_confidence(self):
        """Pure prospect has stat_confidence = 0."""
        result = unified_surplus(**self._base_args())
        assert result["stat_confidence"] == 0.0

    def test_stat_war_is_none(self):
        """Pure prospect has no stat_war."""
        result = unified_surplus(**self._base_args())
        assert result["stat_war"] is None

    def test_peak_war_equals_tool_war(self):
        """With no stats, peak_war should equal tool_war."""
        result = unified_surplus(**self._base_args())
        assert result["peak_war"] == result["tool_war"]

    def test_positive_surplus(self):
        """FV 55 SS prospect should have significant positive surplus."""
        result = unified_surplus(**self._base_args())
        assert result["surplus"] > 10_000_000  # At least $10M

    def test_higher_fv_higher_surplus(self):
        """FV 60 should produce more surplus than FV 55."""
        result_55 = unified_surplus(**self._base_args(fv_continuous=55.0))
        result_60 = unified_surplus(**self._base_args(fv_continuous=60.0))
        assert result_60["surplus"] > result_55["surplus"]

    def test_younger_higher_surplus(self):
        """Younger prospect at same FV should have more surplus (more runway)."""
        result_20 = unified_surplus(**self._base_args(age=20))
        result_24 = unified_surplus(**self._base_args(age=24))
        assert result_20["surplus"] > result_24["surplus"]

    def test_six_year_breakdown(self):
        """Should produce a 6-year breakdown."""
        result = unified_surplus(**self._base_args())
        assert len(result["breakdown"]) == 6

    def test_dev_discount_applied(self):
        """Dev discount should be less than 1.0 for a prospect."""
        result = unified_surplus(**self._base_args())
        assert result["dev_discount"] < 1.0

    def test_certainty_mult_applied(self):
        """Certainty mult should be < 1.0 when composite << ceiling."""
        # Realization = 30/75 = 0.40 → 0.8 + 0.4*0.4 = 0.96 (below 1.0)
        result = unified_surplus(**self._base_args(composite=30, ceiling=75))
        assert result["certainty_mult"] < 1.0


# ---------------------------------------------------------------------------
# unified_surplus tests — established MLB cases
# ---------------------------------------------------------------------------

class TestUnifiedSurplusEstablishedMLB:
    """Players with 600+ PA should produce MLB-model-like evaluations."""

    def _base_args(self, **overrides):
        """Default arguments for an established 3-WAR hitter."""
        args = dict(
            fv_continuous=55.0,
            bucket="SS",
            age=27,
            level="MLB",
            composite=60,
            ceiling=62,
            career_pa=1500,
            career_ip=0.0,
            stat_war=3.0,
            years_control=3,
            salaries=[5_000_000, 8_000_000, 12_000_000],
            dpw=7_000_000,
            min_sal=840_000,
        )
        args.update(overrides)
        return args

    def test_high_stat_confidence(self):
        """Established player has stat_confidence = 1.0."""
        result = unified_surplus(**self._base_args())
        assert result["stat_confidence"] == 1.0

    def test_peak_war_equals_stat_war(self):
        """With full stat confidence, peak_war should equal stat_war."""
        result = unified_surplus(**self._base_args())
        assert result["peak_war"] == result["stat_war"]

    def test_dev_discount_fully_faded(self):
        """Dev discount should be ~1.0 for established player."""
        result = unified_surplus(**self._base_args())
        assert result["dev_discount"] >= 0.99

    def test_certainty_mult_fully_faded(self):
        """Certainty mult should be ~1.0 for established player."""
        result = unified_surplus(**self._base_args())
        assert result["certainty_mult"] >= 0.99

    def test_surplus_reflects_contract_cost(self):
        """Higher salary should reduce surplus."""
        cheap = unified_surplus(**self._base_args(
            salaries=[1_000_000, 2_000_000, 3_000_000]))
        expensive = unified_surplus(**self._base_args(
            salaries=[15_000_000, 18_000_000, 20_000_000]))
        assert cheap["surplus"] > expensive["surplus"]

    def test_overpaid_player_low_surplus(self):
        """A 1.0 WAR player on $15M/yr should have near-zero surplus."""
        result = unified_surplus(**self._base_args(
            stat_war=1.0,
            salaries=[15_000_000, 15_000_000, 15_000_000]))
        assert result["surplus"] < 5_000_000

    def test_three_year_breakdown(self):
        """Should produce breakdown matching control length."""
        result = unified_surplus(**self._base_args())
        assert len(result["breakdown"]) == 3


# ---------------------------------------------------------------------------
# unified_surplus tests — crossover cases
# ---------------------------------------------------------------------------

class TestUnifiedSurplusCrossover:
    """Players in the transition zone between prospect and established."""

    def _base_args(self, **overrides):
        """Default: a promising rookie with ~150 PA."""
        args = dict(
            fv_continuous=55.0,
            bucket="CF",
            age=23,
            level="MLB",
            composite=50,
            ceiling=62,
            career_pa=150,
            career_ip=0.0,
            stat_war=2.5,
            years_control=6,
            salaries=[840_000] * 6,
            dpw=7_000_000,
            min_sal=840_000,
        )
        args.update(overrides)
        return args

    def test_partial_stat_confidence(self):
        """150 PA should give partial confidence."""
        result = unified_surplus(**self._base_args())
        assert 0.2 < result["stat_confidence"] < 0.6

    def test_blended_peak_war(self):
        """Peak WAR should be between tool_war and stat_war."""
        result = unified_surplus(**self._base_args())
        lower = min(result["tool_war"], result["stat_war"])
        upper = max(result["tool_war"], result["stat_war"])
        assert lower <= result["peak_war"] <= upper

    def test_partial_discount_fading(self):
        """Discounts should be partially faded (between raw and 1.0)."""
        result = unified_surplus(**self._base_args())
        # Dev discount should be above the raw prospect value but below 1.0
        # (unless MLB level already gives high raw discount)
        assert result["dev_discount"] < 1.0 or result["stat_confidence"] > 0.9

    def test_spring_training_invite(self):
        """Level=MLB with 0 PA should use tools only (no 50% flat discount)."""
        result = unified_surplus(**self._base_args(
            career_pa=0, career_ip=0.0, stat_war=None,
            level="MLB"))
        assert result["stat_confidence"] == 0.0
        assert result["peak_war"] == result["tool_war"]
        # Should NOT be half of tool_war (the old NO_TRACK_RECORD_DISCOUNT behavior)
        assert result["peak_war"] > 1.5  # FV 55 CF should project > 1.5 WAR

    def test_twenty_pa_noise_suppressed(self):
        """20 PA should give near-zero stat confidence (noise suppressed)."""
        result = unified_surplus(**self._base_args(
            career_pa=20, career_ip=0.0, stat_war=5.0))  # Noisy hot start
        assert result["stat_confidence"] == 0.0  # Below minimum threshold
        assert result["peak_war"] == result["tool_war"]  # Stats ignored

    def test_gradient_smooth(self):
        """Surplus should change smoothly as PA increases (no cliffs)."""
        surpluses = []
        for pa in [0, 50, 100, 150, 200, 300, 400, 500]:
            result = unified_surplus(**self._base_args(
                career_pa=pa, stat_war=3.0))
            surpluses.append(result["surplus"])

        # Check no wild jumps — since both WAR blend and discount fading
        # compound, allow up to 50% of range per step
        total_range = max(surpluses) - min(surpluses)
        if total_range > 0:
            for i in range(1, len(surpluses)):
                step = abs(surpluses[i] - surpluses[i - 1])
                assert step < total_range * 0.55, (
                    f"Jump at PA step {i}: {step} vs range {total_range}")


# ---------------------------------------------------------------------------
# unified_surplus tests — edge cases
# ---------------------------------------------------------------------------

class TestUnifiedSurplusEdgeCases:
    """Edge cases that caused problems in the dual-model system."""

    def test_low_fv_low_surplus(self):
        """FV 35 player should have very low surplus."""
        result = unified_surplus(
            fv_continuous=35.0, bucket="1B", age=22, level="A",
            composite=35, ceiling=45,
            career_pa=0, career_ip=0.0, stat_war=None,
            years_control=6, salaries=[840_000] * 6,
            dpw=7_000_000, min_sal=840_000,
        )
        assert result["surplus"] < 5_000_000

    def test_elite_prospect(self):
        """FV 65 SS prospect should have very high surplus."""
        result = unified_surplus(
            fv_continuous=65.0, bucket="SS", age=19, level="A",
            composite=50, ceiling=70,
            career_pa=0, career_ip=0.0, stat_war=None,
            years_control=6, salaries=[840_000] * 6,
            dpw=7_000_000, min_sal=840_000,
        )
        assert result["surplus"] > 30_000_000

    def test_pitcher_uses_ip_confidence(self):
        """Pitcher with 80 IP should get IP-based confidence."""
        result = unified_surplus(
            fv_continuous=55.0, bucket="SP", age=24, level="MLB",
            composite=52, ceiling=60,
            career_pa=5, career_ip=80.0, stat_war=2.0,
            years_control=6, salaries=[840_000] * 6,
            dpw=7_000_000, min_sal=840_000,
        )
        expected_conf = 80.0 / _IP_FULL_CONFIDENCE
        assert abs(result["stat_confidence"] - expected_conf) < 0.01

    def test_rp_lower_surplus_than_sp(self):
        """RP at same FV should have lower surplus than SP."""
        sp = unified_surplus(
            fv_continuous=55.0, bucket="SP", age=22, level="AA",
            composite=48, ceiling=60,
            career_pa=0, career_ip=0.0, stat_war=None,
            years_control=6, salaries=[840_000] * 6,
            dpw=7_000_000, min_sal=840_000,
        )
        rp = unified_surplus(
            fv_continuous=55.0, bucket="RP", age=22, level="AA",
            composite=48, ceiling=60,
            career_pa=0, career_ip=0.0, stat_war=None,
            years_control=6, salaries=[840_000] * 6,
            dpw=7_000_000, min_sal=840_000,
        )
        assert sp["surplus"] > rp["surplus"]

    def test_zero_years_control(self):
        """Player with 0 years control should have 0 surplus."""
        result = unified_surplus(
            fv_continuous=60.0, bucket="CF", age=32, level="MLB",
            composite=62, ceiling=63,
            career_pa=3000, career_ip=0.0, stat_war=3.5,
            years_control=0, salaries=[],
            dpw=7_000_000, min_sal=840_000,
        )
        assert result["surplus"] == 0
        assert result["breakdown"] == []

    def test_surplus_yr1_matches_first_year(self):
        """surplus_yr1 should equal the first year's surplus in breakdown."""
        result = unified_surplus(
            fv_continuous=55.0, bucket="SS", age=22, level="AAA",
            composite=48, ceiling=60,
            career_pa=0, career_ip=0.0, stat_war=None,
            years_control=6, salaries=[840_000] * 6,
            dpw=7_000_000, min_sal=840_000,
        )
        assert result["surplus_yr1"] == result["breakdown"][0]["surplus"]
