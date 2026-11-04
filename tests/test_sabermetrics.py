"""Unit tests for the shared sabermetric formulas.

These pin the canonical FanGraphs SIERA coefficients and the None-handling
contract (undefined when there is no batters-faced or no batted-ball data) so a
future refactor can't silently revert to the old made-up K/BB-only formula.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from statsplusplus.utils.sabermetrics import siera, SIERA_CONST


def _manual(k, bb, gb, fb, bf, const=SIERA_CONST):
    """Reference implementation of the canonical formula for cross-checking."""
    so_pa = k / bf
    bb_pa = bb / bf
    ngb_pa = (gb - fb) / bf
    ngb2 = 4.920 if ngb_pa < 0 else -4.920
    return (
        const
        - 15.518 * so_pa
        + 9.146 * so_pa ** 2
        + 8.648 * bb_pa
        + 27.252 * bb_pa ** 2
        - 2.298 * ngb_pa
        + ngb2 * ngb_pa ** 2
        - 4.036 * so_pa * bb_pa
        + 5.155 * so_pa * ngb_pa
        + 4.546 * bb_pa * ngb_pa
    )


def test_matches_canonical_formula_groundballer():
    # Ground-ball pitcher: netGB > 0, quadratic coef is -4.920.
    got = siera(k=150, bb=40, gb=200, fb=120, bf=700)
    assert got == _manual(150, 40, 200, 120, 700)


def test_matches_canonical_formula_flyballer():
    # Fly-ball pitcher: netGB < 0, quadratic coef flips to +4.920.
    got = siera(k=150, bb=40, gb=100, fb=220, bf=700)
    assert got == _manual(150, 40, 100, 220, 700)


def test_none_when_no_batters_faced():
    assert siera(k=0, bb=0, gb=0, fb=0, bf=0) is None
    assert siera(k=10, bb=5, gb=20, fb=15, bf=None) is None


def test_none_when_no_batted_ball_data():
    # bf present but gb and fb both zero/absent -> netGB undefined.
    assert siera(k=50, bb=10, gb=0, fb=0, bf=200) is None
    assert siera(k=50, bb=10, gb=None, fb=None, bf=200) is None


def test_elite_lower_than_replacement():
    # High-K, low-BB, ground-ball arm should grade well below a
    # high-walk, fly-ball arm of the same workload.
    elite = siera(k=240, bb=30, gb=250, fb=150, bf=800)
    poor = siera(k=90, bb=90, gb=150, fb=260, bf=800)
    assert elite < poor


def test_const_override_shifts_level():
    base = siera(k=150, bb=40, gb=200, fb=120, bf=700)
    shifted = siera(k=150, bb=40, gb=200, fb=120, bf=700, const=SIERA_CONST + 0.5)
    assert abs((shifted - base) - 0.5) < 1e-9
