"""Explicit game-count share on a batting bench role (e.g. 7 of 154 games)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import pytest

from projections import _manual_position_entries


def _p(pid, war=2.0):
    return {"player_id": pid, "name": f"P{pid}", "war_proj": war, "level_discount": 1.0}


def test_pinned_bench_share_is_honoured_and_starter_takes_the_rest():
    players = [_p(1), _p(2, 0.5)]
    roles = {1: "starter", 2: "bench"}
    entries = dict((p["player_id"], s) for p, s in
                   _manual_position_entries(players, roles, "2B", shares={2: 7 / 154}))
    assert entries[2] == pytest.approx(7 / 154)
    assert entries[1] == pytest.approx(1 - 7 / 154)


def test_unpinned_bench_still_splits_leftover_bucket():
    players = [_p(1), _p(2, 0.5), _p(3, 0.5)]
    roles = {1: "starter", 2: "bench", 3: "bench"}
    entries = dict((p["player_id"], s) for p, s in _manual_position_entries(players, roles, "2B"))
    assert entries[2] + entries[3] == pytest.approx(0.10)


def test_platoon_gives_up_time_to_pinned_bench_and_position_sums_to_one():
    players = [_p(1), _p(2), _p(3, 0.5)]
    roles = {1: "platoon_vr", 2: "platoon_vl", 3: "bench"}
    entries = _manual_position_entries(players, roles, "LF", shares={3: 0.27})
    assert sum(s for _p2, s in entries) == pytest.approx(1.0)


def test_catcher_platoon_with_no_bench_fills_the_position():
    entries = _manual_position_entries([_p(1), _p(2)], {1: "platoon_vr", 2: "platoon_vl"}, "C")
    assert sum(s for _p2, s in entries) == pytest.approx(1.0)
