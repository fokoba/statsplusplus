"""
tests/test_team_queries.py — integration tests for web/team_queries.py

Verifies each query function returns without error and produces the expected shape.
Uses the in-memory DB fixture from conftest.py.
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "web"))
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

import team_queries
from conftest import TEAM_ID, HITTER_ID, PITCHER_ID, YEAR


# ── get_summary ──────────────────────────────────────────────────────────────

def _phase_db(reg_first, reg_last, post_first=None, post_last=None):
    """In-memory games table with played regular + (optional) postseason games."""
    import sqlite3
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE games (date TEXT, game_type INTEGER, played INTEGER)")
    conn.execute("INSERT INTO games VALUES (?,0,1)", (reg_first,))
    conn.execute("INSERT INTO games VALUES (?,0,1)", (reg_last,))
    if post_first:
        conn.execute("INSERT INTO games VALUES (?,3,1)", (post_first,))
    if post_last:
        conn.execute("INSERT INTO games VALUES (?,3,1)", (post_last,))
    return conn


def test_phase_regular_season_in_progress():
    # Current date close to the last played reg game → in-season.
    conn = _phase_db("2034-03-30", "2034-04-09")
    assert team_queries._determine_phase(conn, "2034-04-10", 2034) == "Regular Season"


def test_phase_postseason_within_window():
    conn = _phase_db("1954-04-12", "1954-09-26", "1954-09-29", "1954-10-04")
    assert team_queries._determine_phase(conn, "1954-10-04", 1954) == "Postseason"


def test_phase_offseason_after_playoffs():
    # Well past the last playoff game → offseason (the December-Postseason bug).
    conn = _phase_db("1954-04-12", "1954-09-26", "1954-09-29", "1954-10-04")
    assert team_queries._determine_phase(conn, "1954-12-05", 1954) == "Offseason"


def test_phase_spring_training_new_year_no_games():
    conn = _phase_db("2033-04-01", "2033-09-28", "2033-10-01", "2033-10-10")
    # New calendar year, no 2034 games yet → spring ramp-up.
    assert team_queries._determine_phase(conn, "2034-03-15", 2034) == "Spring Training"


def test_get_summary_returns_dict():
    result = team_queries.get_summary(TEAM_ID)
    assert isinstance(result, dict)

def test_get_summary_has_keys():
    result = team_queries.get_summary(TEAM_ID)
    for key in ("game_date", "year", "mlb_surplus", "farm_surplus", "fv50_count"):
        assert key in result, f"Missing key: {key}"


# ── get_standings ────────────────────────────────────────────────────────────

def test_get_standings_returns_list():
    result = team_queries.get_standings()
    assert isinstance(result, list)

def test_get_standings_has_team():
    result = team_queries.get_standings()
    assert any(r["tid"] == TEAM_ID for r in result)

def test_get_standings_shape():
    result = team_queries.get_standings()
    row = next(r for r in result if r["tid"] == TEAM_ID)
    for key in ("tid", "name", "g", "w", "l", "pct", "rs", "ra", "diff"):
        assert key in row, f"Missing key: {key}"


# ── get_roster ───────────────────────────────────────────────────────────────

def test_get_roster_returns_two_lists():
    hitters, pitchers = team_queries.get_roster(TEAM_ID)
    assert isinstance(hitters, list)
    assert isinstance(pitchers, list)

def test_get_roster_hitter_shape():
    hitters, _ = team_queries.get_roster(TEAM_ID)
    assert len(hitters) >= 1
    row = hitters[0]
    for key in ("pid", "name", "age", "ovr", "war", "surplus"):
        assert key in row, f"Missing key: {key}"

def test_get_roster_pitcher_shape():
    _, pitchers = team_queries.get_roster(TEAM_ID)
    assert len(pitchers) >= 1
    row = pitchers[0]
    for key in ("pid", "name", "age", "ovr", "war", "surplus"):
        assert key in row, f"Missing key: {key}"


# ── get_roster_hitters ───────────────────────────────────────────────────────

def test_get_roster_hitters_returns_list():
    result = team_queries.get_roster_hitters(TEAM_ID)
    assert isinstance(result, list)

def test_get_roster_hitters_has_splits():
    result = team_queries.get_roster_hitters(TEAM_ID)
    assert len(result) >= 1
    row = result[0]
    assert "splits" in row
    assert "1" in row["splits"]
    s = row["splits"]["1"]
    for key in ("pa", "avg", "obp", "slg", "war"):
        assert key in s, f"Missing split key: {key}"


# ── get_roster_pitchers ──────────────────────────────────────────────────────

def test_get_roster_pitchers_returns_list():
    result = team_queries.get_roster_pitchers(TEAM_ID)
    assert isinstance(result, list)

def test_get_roster_pitchers_has_splits():
    result = team_queries.get_roster_pitchers(TEAM_ID)
    assert len(result) >= 1
    row = result[0]
    assert "splits" in row
    assert "1" in row["splits"]
    s = row["splits"]["1"]
    for key in ("ip", "era", "k", "war"):
        assert key in s, f"Missing split key: {key}"


# ── get_farm ─────────────────────────────────────────────────────────────────

def test_get_farm_returns_list():
    result = team_queries.get_farm(TEAM_ID)
    assert isinstance(result, list)

def test_get_farm_shape():
    result = team_queries.get_farm(TEAM_ID)
    assert len(result) >= 1
    row = result[0]
    for key in ("rank", "name", "age", "level", "fv", "fv_str", "bucket", "surplus", "pid"):
        assert key in row, f"Missing key: {key}"


# ── get_contracts ────────────────────────────────────────────────────────────

def test_get_contracts_returns_tuple():
    display, total = team_queries.get_contracts(TEAM_ID)
    assert isinstance(display, list)
    assert isinstance(total, (int, float))

def test_get_contracts_shape():
    display, _ = team_queries.get_contracts(TEAM_ID)
    assert len(display) >= 1
    row = display[0]
    for key in ("pid", "name", "salary", "years_left", "surplus"):
        assert key in row, f"Missing key: {key}"


# ── get_upcoming_fa ──────────────────────────────────────────────────────────

def test_get_upcoming_fa_returns_list():
    result = team_queries.get_upcoming_fa(TEAM_ID)
    assert isinstance(result, list)


# ── get_surplus_leaders ──────────────────────────────────────────────────────

def test_get_surplus_leaders_returns_list():
    result = team_queries.get_surplus_leaders(TEAM_ID)
    assert isinstance(result, list)

def test_get_surplus_leaders_shape():
    result = team_queries.get_surplus_leaders(TEAM_ID)
    assert len(result) >= 1
    row = result[0]
    for key in ("pid", "name", "pos", "surplus", "src"):
        assert key in row, f"Missing key: {key}"


# ── get_roster_summary ───────────────────────────────────────────────────────

def test_get_roster_summary_returns_dict():
    result = team_queries.get_roster_summary(TEAM_ID)
    assert isinstance(result, dict)

def test_get_roster_summary_has_groups():
    result = team_queries.get_roster_summary(TEAM_ID)
    for key in ("SP", "RP", "Pos"):
        assert key in result, f"Missing group: {key}"
        assert "count" in result[key] and "avg_age" in result[key]


# ── get_recent_games ─────────────────────────────────────────────────────────

def test_get_recent_games_returns_list():
    result = team_queries.get_recent_games(TEAM_ID)
    assert isinstance(result, list)

def test_get_recent_games_shape():
    result = team_queries.get_recent_games(TEAM_ID)
    if result:
        row = result[0]
        for key in ("date", "home", "opp", "team_runs", "opp_runs", "wl"):
            assert key in row, f"Missing key: {key}"


# ── get_stat_leaders ─────────────────────────────────────────────────────────

def test_get_stat_leaders_returns_dict():
    result = team_queries.get_stat_leaders(TEAM_ID)
    assert isinstance(result, dict)
    assert "batting" in result and "pitching" in result

def test_get_stat_leaders_batting_categories():
    result = team_queries.get_stat_leaders(TEAM_ID)
    for cat in ("HR", "RBI", "AVG", "WAR"):
        assert cat in result["batting"], f"Missing batting category: {cat}"

def test_get_stat_leaders_pitching_categories():
    result = team_queries.get_stat_leaders(TEAM_ID)
    for cat in ("ERA", "W", "K", "WAR"):
        assert cat in result["pitching"], f"Missing pitching category: {cat}"


# ── get_farm_depth ───────────────────────────────────────────────────────────

def test_get_farm_depth_returns_dict():
    result = team_queries.get_farm_depth(TEAM_ID)
    assert isinstance(result, dict)

def test_get_farm_depth_has_keys():
    result = team_queries.get_farm_depth(TEAM_ID)
    for key in ("buckets", "levels", "total_surplus", "lg_avg", "lg_rank", "lg_n"):
        assert key in result, f"Missing key: {key}"


# ── get_age_distribution ─────────────────────────────────────────────────────

def test_get_age_distribution_returns_dict():
    result = team_queries.get_age_distribution(TEAM_ID)
    assert isinstance(result, dict)

def test_get_age_distribution_has_keys():
    result = team_queries.get_age_distribution(TEAM_ID)
    for key in ("mlb", "farm", "lg_mlb", "lg_farm"):
        assert key in result, f"Missing key: {key}"


# ── get_record_breakdown ─────────────────────────────────────────────────────

def test_get_record_breakdown_returns_dict_or_none():
    result = team_queries.get_record_breakdown(TEAM_ID)
    assert result is None or isinstance(result, dict)

def test_get_record_breakdown_shape():
    result = team_queries.get_record_breakdown(TEAM_ID)
    if result:
        for key in ("overall", "home", "away", "streak"):
            assert key in result, f"Missing key: {key}"


# ── get_power_rankings ───────────────────────────────────────────────────────

def test_get_power_rankings_returns_list():
    result = team_queries.get_power_rankings()
    assert isinstance(result, list)

def test_get_power_rankings_has_rank():
    result = team_queries.get_power_rankings()
    if result:
        assert all("rank" in r and "score" in r for r in result)

# ── get_depth_chart ───────────────────────────────────────────────────────────

def test_get_depth_chart_returns_dict():
    result = team_queries.get_depth_chart(1)
    assert isinstance(result, dict)

def test_get_depth_chart_has_years():
    result = team_queries.get_depth_chart(1)
    assert "years" in result
    assert "by_year" in result

def test_get_depth_chart_by_year_shape():
    result = team_queries.get_depth_chart(1)
    if result["years"]:
        yr = result["years"][0]
        yr_data = result["by_year"][yr]
        assert "positions" in yr_data
        assert "sp" in yr_data
        assert "rp" in yr_data


# ── _resolve_depth_score (fallback behavior) ─────────────────────────────────

class TestResolveDepthScore:
    """Regression tests for depth chart score resolution fallback.

    Ensures leagues without OVR ratings (e.g. PPL) get meaningful WAR
    projections by falling through: composite_score > ovr > tool estimate.
    """

    def test_composite_score_preferred_over_ovr(self):
        """When composite_score is available, it takes priority over ovr."""
        row = {"composite_score": 60, "ovr": 45, "cntct": 70, "gap": 60, "pow": 65, "eye": 55}
        assert team_queries._resolve_depth_score(row) == 60

    def test_ovr_used_when_composite_null(self):
        """When composite_score is NULL, fall back to ovr."""
        row = {"composite_score": None, "ovr": 50, "cntct": 70, "gap": 60, "pow": 65, "eye": 55}
        assert team_queries._resolve_depth_score(row) == 50

    def test_tool_estimate_when_both_null_hitter(self):
        """When composite_score AND ovr are both NULL, estimate from hitting tools."""
        row = {"composite_score": None, "ovr": None, "cntct": 60, "gap": 50, "pow": 55, "eye": 50}
        result = team_queries._resolve_depth_score(row, is_pitcher=False)
        # Should produce a reasonable 20-80 scale score (not 0)
        assert result > 20, f"Expected meaningful score, got {result}"
        assert result < 80

    def test_tool_estimate_when_both_null_pitcher(self):
        """When composite_score AND ovr are both NULL, estimate from pitching tools."""
        row = {"composite_score": None, "ovr": None, "stf": 70, "mov": 60, "ctrl": 55}
        result = team_queries._resolve_depth_score(row, is_pitcher=True)
        assert result > 20, f"Expected meaningful score, got {result}"
        assert result < 80

    def test_returns_zero_when_nothing_available(self):
        """When all data is NULL, returns 0 (graceful degradation)."""
        row = {"composite_score": None, "ovr": None, "cntct": None, "gap": None, "pow": None, "eye": None}
        assert team_queries._resolve_depth_score(row, is_pitcher=False) == 0

    def test_composite_score_zero_falls_through(self):
        """composite_score=0 is treated as missing (falls through to ovr)."""
        row = {"composite_score": 0, "ovr": 50, "cntct": 60, "gap": 50, "pow": 55, "eye": 50}
        assert team_queries._resolve_depth_score(row) == 50

    def test_ovr_zero_falls_through_to_tools(self):
        """ovr=0 is treated as missing (falls through to tool estimate)."""
        row = {"composite_score": None, "ovr": 0, "cntct": 60, "gap": 50, "pow": 55, "eye": 50}
        result = team_queries._resolve_depth_score(row, is_pitcher=False)
        assert result > 20

    def test_pitcher_ignores_hitting_tools(self):
        """Pitcher resolution uses stf/mov/ctrl, not cntct/gap/pow/eye."""
        row = {"composite_score": None, "ovr": None,
               "cntct": 80, "gap": 80, "pow": 80, "eye": 80,  # high hitting tools
               "stf": 40, "mov": 35, "ctrl": 35}  # low pitching tools
        result_pitcher = team_queries._resolve_depth_score(row, is_pitcher=True)
        result_hitter = team_queries._resolve_depth_score(row, is_pitcher=False)
        # Pitcher score should be lower since it uses the weak pitching tools
        assert result_pitcher < result_hitter


# ── get_draft_org_depth ───────────────────────────────────────────────────────

def test_get_draft_org_depth_returns_dict():
    result = team_queries.get_draft_org_depth(1)
    assert isinstance(result, dict)

def test_get_draft_org_depth_has_positions():
    result = team_queries.get_draft_org_depth(1)
    for pos in ["C", "1B", "2B", "3B", "SS", "LF/RF", "CF", "SP", "RP"]:
        assert pos in result

def test_get_draft_org_depth_shape():
    result = team_queries.get_draft_org_depth(1)
    for pos, d in result.items():
        assert "mlb" in d and "farm" in d and "total" in d

# ── get_payroll_summary ───────────────────────────────────────────────────────

def test_get_payroll_summary_returns_dict():
    result = team_queries.get_payroll_summary(1)
    assert isinstance(result, dict)

def test_get_payroll_summary_has_total():
    result = team_queries.get_payroll_summary(1)
    assert "players" in result
    assert "totals" in result
    assert "years" in result
