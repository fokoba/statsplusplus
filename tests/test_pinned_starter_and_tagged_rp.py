"""Explicit starter share (plays every day) and tagged relievers always make the pen."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import pytest

from projections import RP_POOL_SIZE, allocate_pitcher_time, allocate_playing_time, _manual_position_entries


def _p(pid, war=2.0):
    return {"player_id": pid, "name": f"P{pid}", "war_proj": war, "level_discount": 1.0,
            "pos_weight": 1.0, "pa": 0}


def test_starter_share_replaces_the_baseline():
    entries = dict((p["player_id"], s) for p, s in
                   _manual_position_entries([_p(1)], {1: "starter"}, "2B", shares={1: 1.0}))
    assert entries[1] == pytest.approx(1.0)


def test_starter_without_a_share_keeps_the_baseline():
    entries = dict((p["player_id"], s) for p, s in
                   _manual_position_entries([_p(1)], {1: "starter"}, "2B"))
    assert entries[1] == pytest.approx(0.90)


def test_pinned_starter_is_not_shaved_by_the_rest_day_cap():
    out = allocate_playing_time({"2B": [_p(1)]}, manual_roles={"2B": {1: "starter"}},
                                manual_shares={"2B": {1: 1.0}})
    assert out["2B"][0]["pt_pct"] == pytest.approx(100.0)
    unpinned = allocate_playing_time({"2B": [_p(1)]}, manual_roles={"2B": {1: "starter"}})
    assert unpinned["2B"][0]["pt_pct"] == pytest.approx(90.0)


def test_tagged_reliever_makes_the_pen_ahead_of_better_untagged_arms():
    arms = [_p(i, war=3.0 - i * 0.1) for i in range(1, RP_POOL_SIZE + 3)]
    weak_tagged = _p(99, war=0.1)
    _sp, rp = allocate_pitcher_time([], arms + [weak_tagged],
                                    manual_rp_roles={99: ("long_relief", None)})
    ids = {p["player_id"] for p in rp}
    assert 99 in ids
    assert len(ids) == RP_POOL_SIZE


def test_more_tagged_relievers_than_pool_size_all_make_the_pen():
    n = RP_POOL_SIZE + 1
    rp = [_p(i, war=1.0 - 0.1 * i) for i in range(1, n + 3)]
    tagged = {i: ("setup", None) for i in range(1, n + 1)}
    _, rp_result = allocate_pitcher_time([], rp, team_ip=1400, manual_rp_roles=tagged)
    assert {p["player_id"] for p in rp_result} >= set(tagged)
