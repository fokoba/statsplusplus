"""
tests/test_arb_model.py — Unit tests for scripts/arb_model.py

Covers: arb_salary() for SP, RP, hitter across arb years.
All tests are pure math — no DB required.
"""
import sys, math
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from statsplusplus.evaluation.constants import (ARB_HITTER_BASE, ARB_HITTER_EXP, ARB_RP_BASE, ARB_RP_EXP,
                       ARB_RAISE_INTERCEPT, ARB_RAISE_SLOPE, ARB_RAISE_MIN)

MIN_SAL = 825_000


# ---------------------------------------------------------------------------
# arb_salary() — known output values
# ---------------------------------------------------------------------------

def test_arb_hitter_year1():
    from statsplusplus.evaluation.arb import arb_salary
    expected = round(ARB_HITTER_BASE * math.exp(ARB_HITTER_EXP * 60))
    assert arb_salary(60, 'SS', 1, MIN_SAL, MIN_SAL) == expected

def test_arb_hitter_year2():
    from statsplusplus.evaluation.arb import arb_salary
    yr1 = round(ARB_HITTER_BASE * math.exp(ARB_HITTER_EXP * 60))
    raise_amt = max(ARB_RAISE_MIN, round(ARB_RAISE_INTERCEPT + ARB_RAISE_SLOPE * 60))
    assert arb_salary(60, 'SS', 2, yr1, MIN_SAL) == yr1 + raise_amt

def test_arb_rp_year1():
    from statsplusplus.evaluation.arb import arb_salary
    expected = round(ARB_RP_BASE * math.exp(ARB_RP_EXP * 55) * 0.75)
    assert arb_salary(55, 'RP', 1, MIN_SAL, MIN_SAL) == expected

def test_arb_rp_year2():
    from statsplusplus.evaluation.arb import arb_salary
    expected = round(ARB_RP_BASE * math.exp(ARB_RP_EXP * 55) * 1.00)
    assert arb_salary(55, 'RP', 2, MIN_SAL, MIN_SAL) == expected

def test_arb_rp_year3():
    from statsplusplus.evaluation.arb import arb_salary
    expected = round(ARB_RP_BASE * math.exp(ARB_RP_EXP * 55) * 1.25)
    assert arb_salary(55, 'RP', 3, MIN_SAL, MIN_SAL) == expected


# ---------------------------------------------------------------------------
# Invariants
# ---------------------------------------------------------------------------

def test_arb_salary_increases_with_ovr():
    """Higher Ovr should produce higher arb salary."""
    from statsplusplus.evaluation.arb import arb_salary
    for bucket in ('SS', 'SP', 'RP'):
        s50 = arb_salary(50, bucket, 1, MIN_SAL, MIN_SAL)
        s60 = arb_salary(60, bucket, 1, MIN_SAL, MIN_SAL)
        assert s60 > s50, f"{bucket} arb salary not increasing with Ovr"

def test_arb_salary_increases_with_year():
    """Later arb years should produce higher salary."""
    from statsplusplus.evaluation.arb import arb_salary
    s1 = arb_salary(60, 'SS', 1, MIN_SAL, MIN_SAL)
    s2 = arb_salary(60, 'SS', 2, s1, MIN_SAL)
    s3 = arb_salary(60, 'SS', 3, s2, MIN_SAL)
    assert s3 > s2 > s1

def test_arb_salary_above_minimum():
    """Arb salary should always exceed league minimum."""
    from statsplusplus.evaluation.arb import arb_salary
    for bucket in ('SS', 'SP', 'RP', 'C', 'COF'):
        for ovr in (45, 55, 65):
            s = arb_salary(ovr, bucket, 1, MIN_SAL, MIN_SAL)
            assert s > MIN_SAL, f"{bucket} Ovr{ovr} arb yr1 below minimum"
