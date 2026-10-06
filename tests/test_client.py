"""
Integration tests for statsplusplus.client.statsplus — hits the live API.
Run from the project root: python3 -m pytest tests/test_client.py -v
"""

import pytest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
from statsplusplus.client import statsplus as client

# Every test here hits the real StatsPlus API (needs a configured cookie/token
# and is subject to live rate limits). Marked so CI can deselect with
# `-m "not live_api"`; run manually as a contract canary.
pytestmark = pytest.mark.live_api


# --- Helpers ---

# These tests run against whichever league is active (PPL is 1955, eMLB is
# 2034, ...), so the season and sample player are derived from the live league
# instead of hardcoding one league's data.

@pytest.fixture(scope="module")
def season():
    return int(client.get_date()[:4])


def _single_stint_pid(rows):
    """A player with exactly one stats row (a player traded mid-season has one
    row per team, which would make a "returns one row" check flaky)."""
    assert rows, "No stats for the current season to sample a player from"
    counts = {}
    for r in rows:
        counts[r["player_id"]] = counts.get(r["player_id"], 0) + 1
    return next(pid for pid, n in counts.items() if n == 1)


@pytest.fixture(scope="module")
def sample_pid(season):
    return _single_stint_pid(client.get_player_batting_stats(year=season, split=1))


def assert_nonempty_list_of_dicts(result):
    assert isinstance(result, list), f"Expected list, got {type(result)}"
    assert len(result) > 0, "Expected non-empty list"
    assert isinstance(result[0], dict), f"Expected dict rows, got {type(result[0])}"


# --- Players ---

def test_get_players_returns_records():
    result = client.get_players()
    assert_nonempty_list_of_dicts(result)

def test_get_players_has_expected_fields():
    result = client.get_players()
    row = result[0]
    for field in ("ID", "First Name", "Last Name", "Team ID", "Level"):
        assert field in row, f"Missing field: {field}"

def test_get_players_ids_are_integers():
    result = client.get_players()
    assert all(isinstance(p["ID"], int) for p in result)


# --- Batting stats ---

def test_get_player_batting_stats_all(season):
    result = client.get_player_batting_stats(year=season, split=1)
    assert_nonempty_list_of_dicts(result)

def test_get_player_batting_stats_single_player(season, sample_pid):
    result = client.get_player_batting_stats(pid=sample_pid, year=season, split=1)
    assert len(result) == 1
    assert result[0]["player_id"] == sample_pid

def test_get_player_batting_stats_numeric_fields(season, sample_pid):
    result = client.get_player_batting_stats(pid=sample_pid, year=season, split=1)
    row = result[0]
    for field in ("ab", "h", "hr", "bb", "k"):
        assert isinstance(row[field], (int, float)), f"Field {field} not numeric"


# --- Pitching stats ---

def test_get_player_pitching_stats_all(season):
    result = client.get_player_pitching_stats(year=season, split=1)
    assert_nonempty_list_of_dicts(result)

def test_get_player_pitching_stats_single_player(season):
    pid = _single_stint_pid(client.get_player_pitching_stats(year=season, split=1))
    result = client.get_player_pitching_stats(pid=pid, year=season, split=1)
    assert len(result) == 1
    assert result[0]["player_id"] == pid


# --- Fielding stats ---

def test_get_player_fielding_stats_all(season):
    result = client.get_player_fielding_stats(year=season, split=1)
    assert_nonempty_list_of_dicts(result)


# --- Contracts ---

def test_get_contracts_returns_records():
    result = client.get_contracts()
    assert_nonempty_list_of_dicts(result)

def test_get_contracts_has_expected_fields():
    result = client.get_contracts()
    row = result[0]
    for field in ("player_id", "contract_team_id", "salary0"):
        assert field in row, f"Missing field: {field}"


# --- Contract extensions ---

def test_get_contract_extensions_returns_list():
    result = client.get_contract_extensions()
    assert isinstance(result, list)  # may be empty, that's valid


# --- Teams ---

def test_get_teams_returns_records():
    result = client.get_teams()
    assert_nonempty_list_of_dicts(result)

def test_get_teams_have_ids():
    """Every team row carries an ID (the old check looked for eMLB's Angels,
    team 44, which only exists in that league)."""
    result = client.get_teams()
    ids = [t.get("ID") or t.get("id") for t in result]
    assert ids and all(isinstance(i, int) for i in ids), "Teams missing integer IDs"
    assert len(set(ids)) == len(ids), "Duplicate team IDs"


# --- Date ---

def test_get_date_returns_string():
    result = client.get_date()
    assert isinstance(result, str) and len(result) > 0

def test_get_date_looks_like_date():
    result = client.get_date()
    import re
    assert re.match(r'\d{4}-\d{2}-\d{2}', result), f"Unexpected date format: {result}"


# --- Exports ---

def test_get_exports_returns_dict():
    result = client.get_exports()
    assert isinstance(result, dict)

def test_get_exports_has_current_date():
    result = client.get_exports()
    assert "current_date" in result


# --- Team batting stats ---

def test_get_team_batting_stats_returns_records(season):
    result = client.get_team_batting_stats(year=season, split=1)
    assert_nonempty_list_of_dicts(result)

def test_get_team_batting_stats_splits(season):
    overall = client.get_team_batting_stats(year=season, split=1)
    vsl     = client.get_team_batting_stats(year=season, split=2)
    assert len(overall) > 0 and len(vsl) > 0


# --- Team pitching stats ---

def test_get_team_pitching_stats_returns_records(season):
    result = client.get_team_pitching_stats(year=season, split=1)
    assert_nonempty_list_of_dicts(result)


# --- Draft ---

def test_get_draft_returns_list():
    result = client.get_draft()
    assert isinstance(result, list)  # may be empty pre-draft
