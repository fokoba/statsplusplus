"""Behavior checks for the editable Philadelphia WAR planning view."""

import pytest

from war_projection_queries import (
    SEED, PITCHER_IDS, DEFAULT_POSITION_AB, DEFAULT_PH_AB,
    DEFAULT_TEAM_IP, _hitter_components, _in_season_outlook, load_overrides,
    save_overrides, validate_overrides,
)


def test_suggested_25_has_complete_workload():
    assert len(SEED) == 25
    assert len(PITCHER_IDS) == 9
    assert sum(v[2] for pid,v in SEED.items() if pid in PITCHER_IDS) == DEFAULT_TEAM_IP
    assert sum(v[2] for pid,v in SEED.items() if pid not in PITCHER_IDS) == DEFAULT_POSITION_AB + DEFAULT_PH_AB
    assert {24366, 21573, 25323, 22403, 25258} <= set(SEED)  # Ayala/Cano/Thorne/Mines/Thrift
    # Departed or optioned: Yariv, Davis, McGaha, Rivera, Austin
    assert not ({24800, 22312, 24233, 24723, 25350} & set(SEED))


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


def test_reib_is_everyday_third_baseman_regardless_of_split():
    """Reib no longer splits 3B/1B (Cano takes 1B vs L) — his positional
    credit must not depend on his vR share."""
    player = {"pid": 23770, "position": "3B", "cntct": 55, "gap": 55,
              "pow": 55, "eye": 55, "speed": 40, "steal": 40,
              "ifr": 65, "ofr": 45, "c_arm": 20}
    model = {"tool_woba_fit": [-.025, .0031, .0013, .002, .001],
             "lg_woba": .33, "woba_scale": 1.3,
             "anchor": {"runs_per_win": 9.2, "replacement_runs": 10}}
    mostly_r, _, _ = _hitter_components(player, 500, .9, {1: [], 2: [], 3: []}, [], model)
    mostly_l, _, _ = _hitter_components(player, 500, .1, {1: [], 2: [], 3: []}, [], model)
    assert mostly_r["positional"] == pytest.approx(mostly_l["positional"])


def test_in_season_outlook_blends_pace_and_narrows_with_games_played():
    obs = {"war": 2.0, "sample": 300.0, "ab": 270, "g": 50}
    o = _in_season_outlook(4.0, 1.0, obs, 540, "3B", 0.5)
    # pace_full = 2/270*540 = 4.0 == plan, so the blend is 4.0 and median = 2 + .5*4
    assert o["pace_full"] == pytest.approx(4.0)
    assert o["median"] == pytest.approx(4.0)
    assert o["floor"] == pytest.approx(4.0 - 0.5 ** .5, abs=.01)
    assert o["ceiling"] == pytest.approx(4.0 + 0.5 ** .5, abs=.01)
    # hot pace pulls the median above plan, in proportion to the sample
    hot = _in_season_outlook(4.0, 1.0, dict(obs, war=4.0), 540, "3B", 0.5)
    assert hot["median"] > 4.0 + 2.0
    # no sample: observed 0, median is just the remaining share of the plan
    none = _in_season_outlook(4.0, 1.0, {"war": 0.0, "sample": 0.0, "ab": 0, "g": 0}, 540, "3B", 0.25)
    assert none["pace_full"] is None and none["median"] == pytest.approx(3.0)
    # full season played: observed is final, band collapses
    done = _in_season_outlook(4.0, 1.0, obs, 540, "3B", 1.0)
    assert done["median"] == pytest.approx(2.0) and done["floor"] == done["ceiling"] == done["median"]


def test_settings_roundtrip_and_validation(tmp_path):
    plan = {"24747": {"ip": 252.5}, "25323": {"ab": 82, "vr_share": 97}}
    assert save_overrides(tmp_path, plan) == plan
    assert load_overrides(tmp_path) == plan
    for bad in ({"999": {"ab": 5}}, {"25323": {"ab": -1}},
                {"24747": {"ip": float("nan")}}, {"25323": {"vr_share": 101}}):
        with pytest.raises(ValueError):
            validate_overrides(bad)
