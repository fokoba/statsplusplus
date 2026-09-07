"""Tests for statsplusplus.config.finance_settings (v2) — the user enters the
game's authoritative FA/extension budgets; we store them and derive available."""

import pytest

from statsplusplus.config import finance_settings as fs


# ---------------------------------------------------------------------------
# load / save round-trip + validation
# ---------------------------------------------------------------------------

def test_load_missing_returns_defaults(tmp_path):
    s = fs.load_settings(tmp_path)
    assert s["version"] == 2
    assert s["fa_budget"] is None
    assert s["ext_budget"] is None


def test_save_then_load_round_trip(tmp_path):
    fs.save_settings(tmp_path, {"fa_budget": 1_232_320, "ext_budget": 1_401_880})
    s = fs.load_settings(tmp_path)
    assert s["fa_budget"] == 1_232_320
    assert s["ext_budget"] == 1_401_880
    assert (tmp_path / "config" / "finance_settings.json").exists()


def test_save_returns_normalized(tmp_path):
    out = fs.save_settings(tmp_path, {"fa_budget": "500000"})
    assert out["fa_budget"] == 500_000.0  # string coerced
    assert out["ext_budget"] is None


def test_blank_and_negative_coercion(tmp_path):
    out = fs.save_settings(tmp_path, {"fa_budget": "", "ext_budget": -5})
    assert out["fa_budget"] is None      # blank → unset
    assert out["ext_budget"] == 0.0      # negative clamped


def test_garbage_value_is_unset(tmp_path):
    out = fs.save_settings(tmp_path, {"fa_budget": "not a number"})
    assert out["fa_budget"] is None


def test_legacy_v1_shape_migrated(tmp_path):
    """A pre-v2 file (total_budget + pools) loads without error and discards the
    obsolete inputs — those can't map to the game's FA/extension figures."""
    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / "finance_settings.json").write_text(
        '{"version":1,"total_budget":1920000,"pools":{"scouting":229500}}')
    s = fs.load_settings(tmp_path)
    assert s["version"] == 2
    assert s["fa_budget"] is None and s["ext_budget"] is None
    assert "pools" not in s and "total_budget" not in s


def test_corrupt_file_falls_back_to_defaults(tmp_path):
    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / "finance_settings.json").write_text("{ not valid json")
    assert fs.load_settings(tmp_path) == fs.DEFAULT_SETTINGS


def test_validate_rejects_non_dict():
    with pytest.raises(ValueError):
        fs._validate_and_normalize([1, 2, 3])


# ---------------------------------------------------------------------------
# available_for_fa
# ---------------------------------------------------------------------------

def test_available_none_when_no_budget():
    assert fs.available_for_fa({"fa_budget": None}) is None


def test_available_equals_budget_with_no_spend():
    assert fs.available_for_fa({"fa_budget": 1_000_000}) == 1_000_000


def test_available_draws_down_committed_spend():
    assert fs.available_for_fa({"fa_budget": 1_000_000}, committed_spent=250_000) == 750_000


def test_available_can_go_negative():
    assert fs.available_for_fa({"fa_budget": 100_000}, committed_spent=150_000) == -50_000


# ---------------------------------------------------------------------------
# recommended_contract — simple value-based cost estimate (draw-down)
# ---------------------------------------------------------------------------

def test_recommended_contract_basic():
    c = fs.recommended_contract(proj_war=2.0, dpw=1_000_000, age=30)
    assert c["aav"] == 2_000_000     # 2 WAR × $1M/WAR
    assert c["years"] == 3           # age 30 → 3-yr default
    assert c["total"] == 6_000_000   # aav × years


def test_recommended_contract_floors_at_min_salary():
    """Even a replacement-level FA costs at least the league minimum, not $0."""
    c = fs.recommended_contract(proj_war=0.0, dpw=1_000_000, age=33, min_sal=700_000)
    assert c["aav"] == 700_000
    assert c["total"] == 1_400_000  # 700k × 2yr (age 33)


def test_recommended_contract_value_above_min_wins():
    c = fs.recommended_contract(proj_war=3.0, dpw=1_000_000, age=30, min_sal=700_000)
    assert c["aav"] == 3_000_000  # value exceeds the floor


def test_recommended_contract_negative_war_floors_at_min():
    c = fs.recommended_contract(proj_war=-1.0, dpw=1_000_000, age=33, min_sal=700_000)
    assert c["aav"] == 700_000  # negative WAR → floored at minimum


def test_recommended_contract_no_min_floors_at_zero():
    c = fs.recommended_contract(proj_war=-1.0, dpw=1_000_000, age=33)
    assert c["aav"] == 0  # no min_sal passed → 0 floor (back-compat)


def test_recommended_contract_none_war():
    c = fs.recommended_contract(proj_war=None, dpw=1_000_000, age=None)
    assert c["aav"] == 0
    assert c["years"] == 3  # unknown age → default 3


@pytest.mark.parametrize("age,years", [
    (24, 4), (28, 4), (30, 3), (32, 3), (34, 2), (35, 2), (38, 1), (None, 3),
])
def test_default_years_curve(age, years):
    assert fs._default_years(age) == years
