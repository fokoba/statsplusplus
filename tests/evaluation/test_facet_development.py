"""Tests for per-facet development projection + dev_pace (P2/P3)."""
from statsplusplus.evaluation.facet_runs import (
    dev_pace_from_z, project_facet_runs, DEV_BAT, DEV_BASERUNNING, _dev_progress,
)


def test_dev_pace_clamped_modifier():
    """dev_pace is a bounded modifier around 1.0, never a driver."""
    assert dev_pace_from_z(0.0) == 1.0
    assert dev_pace_from_z(None) == 1.0
    assert 0.6 <= dev_pace_from_z(10.0) <= 1.4   # extreme positive clamped
    assert 0.6 <= dev_pace_from_z(-10.0) <= 1.4  # extreme negative clamped
    # fast > neutral > stalled
    assert dev_pace_from_z(3.0) > 1.0 > dev_pace_from_z(-3.0)


def test_dev_pace_low_confidence_pulls_to_neutral():
    """Low dev_speed confidence dampens the adjustment toward 1.0."""
    hi = dev_pace_from_z(4.0, confidence=1.0)
    lo = dev_pace_from_z(4.0, confidence=0.3)
    assert abs(lo - 1.0) < abs(hi - 1.0)


def test_project_facet_never_below_current_or_above_ceiling():
    r = project_facet_runs("bat", current_runs=10, ceiling_runs=30, age=20, dev_pace=1.0)
    assert 10 <= r <= 30


def test_project_facet_no_room_returns_current():
    # ceiling <= current: no development room
    assert project_facet_runs("bat", current_runs=25, ceiling_runs=20, age=20) == 25


def test_faster_pace_closes_gap_more():
    slow = project_facet_runs("bat", 10, 30, age=22, dev_pace=0.7)
    fast = project_facet_runs("bat", 10, 30, age=22, dev_pace=1.3)
    assert fast > slow


def test_bat_develops_later_than_baserunning():
    """At a young age, more of the baserunning gap is realized than the bat gap
    (baserunning is near-fixed early; bat develops latest)."""
    assert _dev_progress(19, DEV_BASERUNNING) > _dev_progress(19, DEV_BAT)
