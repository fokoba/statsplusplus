"""Numeric regression checks for prospect level aliases and ETA calibration.

These protect the actual readiness assumptions, rather than merely asserting
that the surplus is positive. Financial context is fixed; no live DB is used.
"""
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from statsplusplus.evaluation import surplus as package_surplus
from statsplusplus.evaluation.constants import ModelWeights

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import prospect_value as legacy


@pytest.fixture
def fixed_context():
    class Config:
        perpetual_arb = True

    weights = ModelWeights()
    with patch.multiple(
        legacy,
        _ensure_league_context=lambda *a, **k: None,
        _cfg=Config(),
        dollars_per_war=lambda: 20218,
        league_minimum=lambda: 7000,
        YEARS_TO_MLB=weights.years_to_mlb,
    ):
        yield


@pytest.mark.parametrize("level,age,eta,discount", [
    ("AAA", 26, 0.5, 0.88),
    ("AA", 24, 1.5, 0.78),
    ("A", 22, 2.5, 0.68),
    ("A-Short", 21, 3.5, 0.55),
    ("Rookie", 19, 4.5, 0.45),
    ("USL", 19, 4.5, 0.45),
    ("DSL", 18, 4.5, 0.45),
    ("Intl", 17, 5.0, 0.35),
    ("International", 17, 5.0, 0.35),
    ("MLB", 26, 0.0, 0.95),
    ("FA", 26, 0.0, 0.95),
])
def test_level_numeric_assumptions(fixed_context, level, age, eta, discount):
    # No OVR here: test the literal level, not the separate amateur proxy.
    expected = None
    for spelling in (level, level.lower(), level.upper()):
        result = legacy.prospect_surplus(50, age, spelling, "COF")
        assert result["years_to_mlb"] == pytest.approx(eta)
        assert result["dev_discount"] == pytest.approx(discount)
        assert result["debut_age"] == pytest.approx(age + eta)
        if expected is None:
            expected = result["total_surplus"]
        assert result["total_surplus"] == expected

        pure = package_surplus.prospect_surplus(
            50, age, spelling, "COF", dpw=20218, min_sal=7000,
        )
        assert pure["years_to_mlb"] == pytest.approx(eta)
        assert pure["dev_discount"] == pytest.approx(discount)


@pytest.mark.parametrize("level", ["Draft", "DSL", "Intl", "International", "College", "HS"])
@pytest.mark.parametrize("ovr,eta,discount", [
    (50, 0.5, 0.95), (42, 1.5, 0.90),
    (35, 2.5, 0.72), (34, 3.5, 0.55),
])
def test_amateur_readiness_proxy_is_preserved(fixed_context, level, ovr, eta, discount):
    # Existing policy uses ability to select a proxy level for amateurs.
    # This fix repairs lookups without changing those OVR thresholds.
    for spelling in (level, level.lower(), level.upper()):
        result = legacy.prospect_surplus(50, 21, spelling, "COF", ovr=ovr)
        pure = package_surplus.prospect_surplus(
            50, 21, spelling, "COF", dpw=20218, min_sal=7000, ovr=ovr,
        )
        for r in (result, pure):
            assert r["years_to_mlb"] == pytest.approx(eta)
            assert r["dev_discount"] == pytest.approx(discount)


@pytest.mark.parametrize("table", [
    {"AAA": 0.7, "AA": 1.8, "A-Short": 3.8, "USL": 4.8, "MLB": 0.0},
    {"aaa": 0.7, "aa": 1.8, "a-short": 3.8, "usl": 4.8, "mlb": 0.0},
])
def test_calibrated_and_default_eta_keys_share_one_convention(fixed_context, table):
    weights = ModelWeights(raw={"YEARS_TO_MLB": table})
    assert weights.years_to_mlb == {
        "aaa": 0.7, "aa": 1.8, "a-short": 3.8, "usl": 4.8, "mlb": 0.0,
    }
    with patch.object(legacy, "YEARS_TO_MLB", weights.years_to_mlb):
        for level, expected in [("AAA", 0.7), ("AA", 1.8), ("A-Short", 3.8), ("Rookie", 4.8), ("FA", 0.0)]:
            assert legacy.prospect_surplus(50, 23, level, "COF")["years_to_mlb"] == expected
            assert package_surplus.prospect_surplus(
                50, 23, level, "COF", dpw=20218, min_sal=7000, weights=weights,
            )["years_to_mlb"] == expected


def test_age_adjustment_and_bounds_are_preserved(fixed_context):
    for fn in (legacy._age_adjusted_discount, package_surplus.age_adjusted_discount):
        assert fn("AAA", 25) == pytest.approx(0.92)
        assert fn("AAA", 27) == pytest.approx(0.84)
        assert fn("AAA", 17) == pytest.approx(0.95)
        assert fn("A", 40) == pytest.approx(0.15)


def test_unknown_level_retains_documented_fallback(fixed_context):
    for result in (
        legacy.prospect_surplus(50, 23, "Unknown", "COF"),
        package_surplus.prospect_surplus(50, 23, "Unknown", "COF", dpw=20218, min_sal=7000),
    ):
        assert result["years_to_mlb"] == 3.5
        assert result["dev_discount"] == 0.45


def test_no_calibrated_table_does_not_modify_shared_defaults():
    from statsplusplus.utils.positions import YEARS_TO_MLB
    before = dict(YEARS_TO_MLB)
    weights = ModelWeights()
    weights.years_to_mlb["aaa"] = 9
    assert YEARS_TO_MLB == before
