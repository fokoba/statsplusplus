"""tests/test_refresh_date_gate.py — the /date-gate helper in refresh.

Verifies _stored_game_date reads the active league's state.json game date,
which drives the refresh short-circuit (skip a full pull when the game date
hasn't advanced, avoiding redundant rate-limited /ratings requests).
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from statsplusplus.data import refresh


def test_stored_game_date_reads_state(tmp_path, monkeypatch):
    league_dir = tmp_path / "lg"
    (league_dir / "config").mkdir(parents=True)
    (league_dir / "config" / "state.json").write_text(
        json.dumps({"game_date": "2034-04-10", "year": 2034}))
    monkeypatch.setattr(refresh, "get_league_dir", lambda: league_dir)
    assert refresh._stored_game_date() == "2034-04-10"


def test_stored_game_date_missing_returns_empty(tmp_path, monkeypatch):
    league_dir = tmp_path / "lg"
    (league_dir / "config").mkdir(parents=True)
    monkeypatch.setattr(refresh, "get_league_dir", lambda: league_dir)
    assert refresh._stored_game_date() == ""


def test_stored_game_date_bad_json_returns_empty(tmp_path, monkeypatch):
    league_dir = tmp_path / "lg"
    (league_dir / "config").mkdir(parents=True)
    (league_dir / "config" / "state.json").write_text("{not json")
    monkeypatch.setattr(refresh, "get_league_dir", lambda: league_dir)
    assert refresh._stored_game_date() == ""
