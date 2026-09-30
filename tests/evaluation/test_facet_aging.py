"""Tests for per-facet aging (spec: per-facet-aging-projection, P1)."""
from statsplusplus.evaluation.facet_runs import facet_aging_mult, AGING_BAT, AGING_BASERUNNING, AGING_DEFENSE


def test_bat_first_ages_slower_than_glove_speed_first():
    """Past peak, a bat-first player should retain more value than a
    glove/speed-first player of the same age (bat ages latest)."""
    age = 34
    bat_first = facet_aging_mult(age, bat_runs=40, br_runs=0, fld_runs=0)
    glove_speed = facet_aging_mult(age, bat_runs=10, br_runs=5, fld_runs=15)
    assert bat_first > glove_speed


def test_facet_aging_monotonic_decline_past_peak():
    m30 = facet_aging_mult(30, 20, 3, 8)
    m34 = facet_aging_mult(34, 20, 3, 8)
    m38 = facet_aging_mult(38, 20, 3, 8)
    assert m30 > m34 > m38


def test_facet_aging_full_at_peak():
    # At/below the youngest peak, multipliers are ~1.0
    m = facet_aging_mult(24, 20, 3, 8)
    assert m >= 0.95


def test_no_positive_runs_falls_back_to_bat_curve():
    # A player with no positive facet runs uses the bat curve (no div-by-zero).
    m = facet_aging_mult(33, bat_runs=-5, br_runs=-2, fld_runs=-3)
    from statsplusplus.evaluation.facet_runs import _aging_mult
    assert abs(m - _aging_mult(33, AGING_BAT)) < 1e-9


def test_baserunning_curve_steepest():
    """Baserunning should decline earliest/steepest of the three facets."""
    age = 30
    from statsplusplus.evaluation.facet_runs import _aging_mult
    assert _aging_mult(age, AGING_BASERUNNING) < _aging_mult(age, AGING_DEFENSE) < _aging_mult(age, AGING_BAT)
