"""Alerts helpers."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "web"))

import alerts_queries as aq


def test_state_roundtrip(tmp_path):
    (tmp_path / "config").mkdir()
    assert aq._load_state(tmp_path) == {"seen": []}
    aq._save_state(tmp_path, {"seen": [1, 2]})
    assert aq._load_state(tmp_path)["seen"] == [1, 2]


def test_tool_lists_cover_hitters_and_pitchers():
    assert {c for c, *_ in aq.HIT_TOOLS} >= {"cntct", "pow", "eye"}
    assert {c for c, *_ in aq.PIT_TOOLS} >= {"stf", "mov", "ctrl"}
