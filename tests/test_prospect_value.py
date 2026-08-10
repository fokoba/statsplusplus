"""
tests/test_prospect_value.py — Unit tests for scripts/prospect_value.py

Covers: prospect_surplus(), prospect_surplus_with_option()
All tests are pure math — no DB or API required.

Financial inputs (dollars_per_war, league_minimum) are stubbed to fixed values
so tests are deterministic regardless of league_averages.json state.
"""
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

# Fixed financial constants for deterministic tests
_DPW = 7_000_000
_LG_MIN = 800_000


class _FakeCfg:
    def __init__(self, perpetual_arb):
        self.perpetual_arb = perpetual_arb


def _stub(perpetual_arb=False):
    """Return a context manager that stubs financial functions and the
    perpetual-arb league flag (which controls the projection horizon)."""
    return patch.multiple(
        "prospect_value",
        dollars_per_war=lambda: _DPW,
        league_minimum=lambda: _LG_MIN,
        _cfg=_FakeCfg(perpetual_arb),
    )


# ---------------------------------------------------------------------------
# prospect_surplus() — deterministic output tests
# ---------------------------------------------------------------------------

def test_surplus_sp_aa():
    """FV 55 SP in AA should produce consistent surplus."""
    from prospect_value import prospect_surplus
    with _stub(perpetual_arb=False):
        result = prospect_surplus(55, 21, 'AA', 'SP', fv_plus=False, ovr=55, pot=70)
    assert result['total_surplus'] > 0
    # Standard (non-perpetual-arb) leagues: 6-year rookie-control window.
    assert len(result['breakdown']) == 6


def test_surplus_horizon_perpetual_arb():
    """Perpetual-arb leagues (PPL) project a full career horizon (20 years),
    not the standard 6-year rookie-control window, since contracts auto-renew
    indefinitely with no free-agency exit."""
    from prospect_value import prospect_surplus
    with _stub(perpetual_arb=True):
        result = prospect_surplus(55, 21, 'AA', 'SP', fv_plus=False, ovr=55, pot=70)
    assert len(result['breakdown']) == 20


def test_surplus_ss_a():
    """FV 50+ SS in A-ball should have significant surplus."""
    from prospect_value import prospect_surplus
    with _stub():
        result = prospect_surplus(50, 20, 'A', 'SS', fv_plus=True, ovr=45, pot=65)
    assert result['total_surplus'] > 0


def test_surplus_rp_aaa():
    """FV 45 RP in AAA should have modest surplus (RP cap limits upside)."""
    from prospect_value import prospect_surplus
    with _stub():
        result = prospect_surplus(45, 23, 'AAA', 'RP', fv_plus=False, ovr=42, pot=55)
    assert result['total_surplus'] >= 0


def test_surplus_cof_ashort():
    """FV 60 COF in A-Short should have high surplus despite distance."""
    from prospect_value import prospect_surplus
    with _stub():
        result = prospect_surplus(60, 19, 'A-Short', 'COF', fv_plus=False, ovr=50, pot=70)
    assert result['total_surplus'] > 0


# ---------------------------------------------------------------------------
# prospect_surplus_with_option() — option value >= base
# ---------------------------------------------------------------------------

def test_option_value_sp():
    """Option value should exceed base surplus (option adds upside)."""
    from prospect_value import prospect_surplus, prospect_surplus_with_option
    with _stub():
        base = prospect_surplus(55, 21, 'AA', 'SP', fv_plus=False, ovr=55, pot=70)['total_surplus']
        opt = prospect_surplus_with_option(55, 21, 'AA', 'SP', fv_plus=False, ovr=55, pot=70)
    assert opt >= base
    assert opt > 0


def test_option_value_ss():
    """SS option value should be positive and exceed base."""
    from prospect_value import prospect_surplus, prospect_surplus_with_option
    with _stub():
        base = prospect_surplus(50, 20, 'A', 'SS', fv_plus=True, ovr=45, pot=65)['total_surplus']
        opt = prospect_surplus_with_option(50, 20, 'A', 'SS', fv_plus=True, ovr=45, pot=65)
    assert opt >= base
    assert opt > 0


def test_option_value_rp():
    """RP option value should be positive and exceed base."""
    from prospect_value import prospect_surplus, prospect_surplus_with_option
    with _stub():
        base = prospect_surplus(45, 23, 'AAA', 'RP', fv_plus=False, ovr=42, pot=55)['total_surplus']
        opt = prospect_surplus_with_option(45, 23, 'AAA', 'RP', fv_plus=False, ovr=42, pot=55)
    assert opt >= base
    assert opt > 0


def test_option_value_cof():
    """COF option value should be positive and exceed base."""
    from prospect_value import prospect_surplus, prospect_surplus_with_option
    with _stub():
        base = prospect_surplus(60, 19, 'A-Short', 'COF', fv_plus=False, ovr=50, pot=70)['total_surplus']
        opt = prospect_surplus_with_option(60, 19, 'A-Short', 'COF', fv_plus=False, ovr=50, pot=70)
    assert opt >= base
    assert opt > 0


# ---------------------------------------------------------------------------
# Structural / invariant tests
# ---------------------------------------------------------------------------

def test_surplus_non_negative():
    """Surplus should never be negative (floor at 0)."""
    from prospect_value import prospect_surplus
    with _stub():
        result = prospect_surplus(40, 24, 'AAA', 'RP', ovr=35, pot=42)
    assert result['total_surplus'] >= 0


def test_higher_fv_higher_surplus():
    """FV 60 prospect should be worth more than FV 50 at same age/level/bucket."""
    from prospect_value import prospect_surplus
    with _stub():
        s50 = prospect_surplus(50, 21, 'AA', 'SP', ovr=45, pot=60)['total_surplus']
        s60 = prospect_surplus(60, 21, 'AA', 'SP', ovr=55, pot=70)['total_surplus']
    assert s60 > s50


def test_younger_higher_surplus():
    """Younger prospect at same FV/level should be worth more."""
    from prospect_value import prospect_surplus
    with _stub():
        s_young = prospect_surplus(55, 19, 'AA', 'SP', ovr=50, pot=65)['total_surplus']
        s_old = prospect_surplus(55, 23, 'AA', 'SP', ovr=50, pot=65)['total_surplus']
    assert s_young > s_old


def test_option_gte_base():
    """Option value should always be >= base surplus."""
    from prospect_value import prospect_surplus, prospect_surplus_with_option
    with _stub():
        for fv, age, level, bucket in [(50, 21, 'AA', 'SP'), (45, 20, 'A', 'SS'), (55, 22, 'AAA', 'RP')]:
            base = prospect_surplus(fv, age, level, bucket)['total_surplus']
            opt = prospect_surplus_with_option(fv, age, level, bucket)
            assert opt >= base, f"Option < base for {bucket} FV{fv}"


def test_sp_surplus_exceeds_rp():
    """SP at same FV/age/level should produce more surplus than RP (WAR cap)."""
    from prospect_value import prospect_surplus
    with _stub():
        sp = prospect_surplus(55, 21, 'AA', 'SP', ovr=55, pot=70)['total_surplus']
        rp = prospect_surplus(55, 21, 'AA', 'RP', ovr=55, pot=70)['total_surplus']
    assert sp > rp


def test_closer_to_mlb_higher_surplus_same_fv():
    """AAA prospect should be worth more than A-ball at same FV (less risk)."""
    from prospect_value import prospect_surplus
    with _stub():
        aaa = prospect_surplus(50, 22, 'AAA', 'SS', ovr=50, pot=60)['total_surplus']
        a = prospect_surplus(50, 22, 'A', 'SS', ovr=50, pot=60)['total_surplus']
    assert aaa > a


def test_surplus_scales_with_dpw():
    """Higher $/WAR should produce higher surplus."""
    from prospect_value import prospect_surplus
    with patch.multiple("prospect_value", dollars_per_war=lambda: 5_000_000, league_minimum=lambda: _LG_MIN):
        s_low = prospect_surplus(55, 21, 'AA', 'SP', ovr=55, pot=70)['total_surplus']
    with patch.multiple("prospect_value", dollars_per_war=lambda: 10_000_000, league_minimum=lambda: _LG_MIN):
        s_high = prospect_surplus(55, 21, 'AA', 'SP', ovr=55, pot=70)['total_surplus']
    assert s_high > s_low
