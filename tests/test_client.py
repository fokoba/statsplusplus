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

def test_get_player_batting_stats_all():
    result = client.get_player_batting_stats(year=2033, split=1)
    assert_nonempty_list_of_dicts(result)

def test_get_player_batting_stats_single_player():
    result = client.get_player_batting_stats(pid=232, year=2033, split=1)
    assert len(result) == 1
    assert result[0]["player_id"] == 232

def test_get_player_batting_stats_numeric_fields():
    result = client.get_player_batting_stats(pid=232, year=2033, split=1)
    row = result[0]
    for field in ("ab", "h", "hr", "bb", "k"):
        assert isinstance(row[field], (int, float)), f"Field {field} not numeric"


# --- Pitching stats ---

def test_get_player_pitching_stats_all():
    result = client.get_player_pitching_stats(year=2033, split=1)
    assert_nonempty_list_of_dicts(result)

def test_get_player_pitching_stats_single_player():
    # Greg Briggs, Angels SP
    result = client.get_player_pitching_stats(pid=35149, year=2033, split=1)
    assert len(result) == 1
    assert result[0]["player_id"] == 35149


# --- Fielding stats ---

def test_get_player_fielding_stats_all():
    result = client.get_player_fielding_stats(year=2033, split=1)
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

def test_get_teams_angels_present():
    result = client.get_teams()
    ids = [t.get("ID") or t.get("id") for t in result]
    assert 44 in ids, "Angels (team 44) not found in teams list"


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

def test_get_team_batting_stats_returns_records():
    result = client.get_team_batting_stats(year=2033, split=1)
    assert_nonempty_list_of_dicts(result)

def test_get_team_batting_stats_splits():
    overall = client.get_team_batting_stats(year=2033, split=1)
    vsl     = client.get_team_batting_stats(year=2033, split=2)
    assert len(overall) > 0 and len(vsl) > 0


# --- Team pitching stats ---

def test_get_team_pitching_stats_returns_records():
    result = client.get_team_pitching_stats(year=2033, split=1)
    assert_nonempty_list_of_dicts(result)


# --- Draft ---

def test_get_draft_returns_list():
    result = client.get_draft()
    assert isinstance(result, list)  # may be empty pre-draft
