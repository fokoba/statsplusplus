"""Behavior checks for the editable Philadelphia WAR planning view."""

import pytest

from war_projection_queries import (
    SEED, PITCHER_IDS, DEFAULT_POSITION_AB, DEFAULT_PH_AB,
    DEFAULT_TEAM_IP, _hitter_components, load_overrides, save_overrides,
    validate_overrides,
)


def test_suggested_25_has_complete_workload():
    assert len(SEED) == 25
    assert len(PITCHER_IDS) == 10
    assert sum(v[2] for pid,v in SEED.items() if pid in PITCHER_IDS) == DEFAULT_TEAM_IP
    assert sum(v[2] for pid,v in SEED.items() if pid not in PITCHER_IDS) == DEFAULT_POSITION_AB + DEFAULT_PH_AB
    assert {24366, 21573, 25323, 24723} <= set(SEED)  # Ayala/Cano/Thorne/Rivera


def test_split_override_changes_batting_only_for_ph():
    player = {"pid": 25323, "position": "PH", "cntct_r": 65, "gap_r": 60,
              "pow_r": 75, "eye_r": 55, "cntct_l": 40, "gap_l": 40,
              "pow_l": 35, "eye_l": 40, "speed": 40, "steal": 40}
    model = {"tool_woba_fit": [-.025, .0031, .0013, .002, .001],
             "lg_woba": .33, "woba_scale": 1.3,
             "anchor": {"runs_per_win": 9.2, "replacement_runs": 10}}
    vs_r, _, _ = _hitter_components(player, 100, 1.0, {1: [], 2: [], 3: []}, [], model)
    vs_l, _, _ = _hitter_components(player, 100, 0.0, {1: [], 2: [], 3: []}, [], model)
    assert vs_r["hitting"] > vs_l["hitting"]
    assert vs_r["fielding"] == vs_l["fielding"] == 0
    assert vs_r["positional"] == vs_l["positional"] == 0


def test_reib_split_override_moves_positional_credit():
    player = {"pid": 23770, "position": "3B", "cntct": 55, "gap": 55,
              "pow": 55, "eye": 55, "speed": 40, "steal": 40,
              "ifr": 65, "ofr": 45, "c_arm": 20}
    model = {"tool_woba_fit": [-.025, .0031, .0013, .002, .001],
             "lg_woba": .33, "woba_scale": 1.3,
             "anchor": {"runs_per_win": 9.2, "replacement_runs": 10}}
    mostly_third, _, _ = _hitter_components(player, 500, .9, {1: [], 2: [], 3: []}, [], model)
    mostly_first, _, _ = _hitter_components(player, 500, .1, {1: [], 2: [], 3: []}, [], model)
    assert mostly_third["positional"] > mostly_first["positional"]


def test_settings_roundtrip_and_validation(tmp_path):
    plan = {"24747": {"ip": 252.5}, "25323": {"ab": 82, "vr_share": 97}}
    assert save_overrides(tmp_path, plan) == plan
    assert load_overrides(tmp_path) == plan
    for bad in ({"999": {"ab": 5}}, {"25323": {"ab": -1}},
                {"24747": {"ip": float("nan")}}, {"25323": {"vr_share": 101}}):
        with pytest.raises(ValueError):
            validate_overrides(bad)
