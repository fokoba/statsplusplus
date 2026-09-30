"""Tests for the pure wOBA module (evaluation/woba.py)."""

from statsplusplus.evaluation.woba import (
    player_woba,
    woba_weights_from_run_env,
)
from statsplusplus.evaluation.constants import CANONICAL_WOBA_WEIGHTS


def test_player_woba_matches_fangraphs_example():
    """FanGraphs' Mike Trout 2013 example: wOBA = .423 with 2013 weights.

    100 uBB, 9 HBP, 115 1B, 39 2B, 9 3B, 27 HR.
    Denominator AB + BB - IBB + SF + HBP. Trout 2013: 589 AB, 110 BB (100 uBB
    means 10 IBB), 9 HBP, 8 SF -> denom = 589 + 110 - 10 + 8 + 9 = 706.
    """
    # Reconstruct counts: h = 1B + 2B + 3B + HR = 115+39+9+27 = 190
    counts = {
        "ab": 589, "h": 190, "d": 39, "t": 9, "hr": 27,
        "bb": 110, "ibb": 10, "hbp": 9, "sf": 8,
    }
    w = player_woba(counts, CANONICAL_WOBA_WEIGHTS)
    assert w is not None
    # FanGraphs reports .423; allow small rounding tolerance.
    assert abs(w - 0.423) < 0.005


def test_player_woba_none_on_zero_denominator():
    counts = {"ab": 0, "h": 0, "bb": 0, "ibb": 0, "hbp": 0, "sf": 0,
              "d": 0, "t": 0, "hr": 0}
    assert player_woba(counts, CANONICAL_WOBA_WEIGHTS) is None


def test_player_woba_singles_derived_from_hits():
    """Singles are h - d - t - hr; only-singles line should weight ubb/b1."""
    counts = {"ab": 100, "h": 30, "d": 0, "t": 0, "hr": 0,
              "bb": 0, "ibb": 0, "hbp": 0, "sf": 0}
    w = player_woba(counts, CANONICAL_WOBA_WEIGHTS)
    # 30 singles / 100 denom * b1 weight
    assert abs(w - (30 * CANONICAL_WOBA_WEIGHTS["b1"] / 100)) < 1e-9


def test_weights_fallback_when_no_anchor():
    weights, src = woba_weights_from_run_env(
        totals={"ab": 50000, "h": 13000, "d": 2000, "t": 400, "hr": 1200,
                "bb": 5000, "ibb": 400, "hbp": 300, "sf": 400},
        ootp_league_woba=None, n_team_seasons=16, total_pa=96000,
    )
    assert src == "canonical-fallback"
    assert weights == CANONICAL_WOBA_WEIGHTS


def test_weights_fallback_when_thin_season():
    """Small PA/team (in-progress season) -> canonical fallback."""
    weights, src = woba_weights_from_run_env(
        totals={"ab": 5000, "h": 1300, "d": 200, "t": 40, "hr": 120,
                "bb": 500, "ibb": 40, "hbp": 30, "sf": 40},
        ootp_league_woba=0.330, n_team_seasons=16, total_pa=12000,  # 750 PA/team
    )
    assert src == "canonical-fallback"
    assert weights == CANONICAL_WOBA_WEIGHTS


def test_weights_fallback_when_too_few_teams():
    weights, src = woba_weights_from_run_env(
        totals={"ab": 50000, "h": 13000, "d": 2000, "t": 400, "hr": 1200,
                "bb": 5000, "ibb": 400, "hbp": 300, "sf": 400},
        ootp_league_woba=0.330, n_team_seasons=4, total_pa=96000,
    )
    assert src == "canonical-fallback"


def test_weights_derived_scales_to_anchor():
    """With a valid anchor, derived weights reproduce the anchor league wOBA."""
    totals = {"ab": 80000, "h": 21000, "d": 3500, "t": 830, "hr": 1900,
              "bb": 8900, "ibb": 820, "hbp": 460, "sf": 780}
    anchor = 0.345  # deliberately above canonical to force an upscale
    weights, src = woba_weights_from_run_env(
        totals=totals, ootp_league_woba=anchor,
        n_team_seasons=16, total_pa=96000,
    )
    assert src == "derived"
    # Applying derived weights to the league totals must reproduce the anchor.
    league = player_woba(totals, weights)
    assert abs(league - anchor) < 0.001
    # Weights stay monotonic and positive (canonical shape preserved).
    assert 0 < weights["ubb"] < weights["b1"] < weights["b2"] < weights["b3"] < weights["hr"]


def test_weights_derived_scale_factor_consistent():
    """All weights scale by the same factor (shape preserved)."""
    totals = {"ab": 80000, "h": 21000, "d": 3500, "t": 830, "hr": 1900,
              "bb": 8900, "ibb": 820, "hbp": 460, "sf": 780}
    weights, src = woba_weights_from_run_env(
        totals=totals, ootp_league_woba=0.340,
        n_team_seasons=16, total_pa=96000,
    )
    assert src == "derived"
    ratios = [weights[k] / CANONICAL_WOBA_WEIGHTS[k] for k in weights]
    assert max(ratios) - min(ratios) < 1e-3  # same scale for every event
