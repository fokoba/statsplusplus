"""
tests/test_queries.py — integration tests for web/queries.py

Verifies each query function returns without error and produces the expected shape.
Uses the in-memory DB fixture from conftest.py.
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "web"))
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

import queries
from conftest import TEAM_ID, HITTER_ID, PITCHER_ID, PROSPECT_ID, YEAR


# ── get_top_prospects ────────────────────────────────────────────────────────

def test_get_top_prospects_returns_list():
    result = queries.get_top_prospects()
    assert isinstance(result, list)

def test_get_top_prospects_shape():
    result = queries.get_top_prospects()
    assert len(result) >= 1
    row = result[0]
    for key in ("rank", "name", "fv", "fv_str", "bucket", "level", "surplus", "pid"):
        assert key in row, f"Missing key: {key}"

def test_get_top_prospects_ranked():
    result = queries.get_top_prospects()
    assert result[0]["rank"] == 1


# ── get_all_prospects ────────────────────────────────────────────────────────

def test_get_all_prospects_returns_list():
    result = queries.get_all_prospects()
    assert isinstance(result, list)

def test_get_all_prospects_fv_filter():
    result = queries.get_all_prospects()
    assert all(r["fv"] >= 40 for r in result)


# ── get_batting_leaders ──────────────────────────────────────────────────────

def test_get_batting_leaders_returns_dict():
    result = queries.get_batting_leaders(yr=YEAR)
    assert isinstance(result, dict)
    assert "All" in result

def test_get_batting_leaders_has_categories():
    result = queries.get_batting_leaders(yr=YEAR)
    for cat in ("HR", "RBI", "AVG", "OPS", "SB", "WAR"):
        assert cat in result["All"], f"Missing batting category: {cat}"

def test_get_batting_leaders_entries_have_shape():
    result = queries.get_batting_leaders(yr=YEAR)
    for cat, entries in result["All"].items():
        for e in entries:
            assert "pid" in e and "name" in e and "val" in e


# ── get_pitching_leaders ─────────────────────────────────────────────────────

def test_get_pitching_leaders_returns_dict():
    result = queries.get_pitching_leaders(yr=YEAR)
    assert isinstance(result, dict)
    assert "All" in result

def test_get_pitching_leaders_has_categories():
    result = queries.get_pitching_leaders(yr=YEAR)
    for cat in ("ERA", "W", "K", "SV", "WHIP", "WAR"):
        assert cat in result["All"], f"Missing pitching category: {cat}"


# ── early-season leaders (regression: floors hid everyone) ───────────────────
#
# Six games in, no hitter clears a full-season PA threshold and almost no
# reliever clears a full-season IP threshold. Counting-stat panels (HR, SB,
# SV, W) are ungated, and rate-stat panels use a games-scaled qualifier — so
# leaders still populate in the season's first weeks. There is no playing-time
# floor on the row set (an earlier hard floor hid nearly all early-season
# hitters and save leaders).

import sqlite3


def _early_season_db():
    """Minimal in-memory DB shaped like a league ~6 games in."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE players (player_id INTEGER PRIMARY KEY, name TEXT, team_id INTEGER);
        CREATE TABLE batting_stats (player_id INTEGER, year INTEGER, split_id INTEGER,
            team_id INTEGER, league_id INTEGER,
            ab INTEGER, h INTEGER, d INTEGER, t INTEGER, hr INTEGER, rbi INTEGER,
            bb INTEGER, k INTEGER, sb INTEGER, pa INTEGER, war REAL, r INTEGER, hbp INTEGER);
        CREATE TABLE pitching_stats (player_id INTEGER, year INTEGER, split_id INTEGER,
            team_id INTEGER, league_id INTEGER,
            ip REAL, era REAL, k INTEGER, bb INTEGER, w INTEGER, l INTEGER,
            sv INTEGER, war REAL, ha INTEGER, hld INTEGER);
        CREATE TABLE team_pitching_stats (team_id INTEGER, year INTEGER, split_id INTEGER, ip REAL);
        CREATE VIEW mlb_batting_stats AS SELECT * FROM batting_stats WHERE league_id IS NULL;
        CREATE VIEW mlb_pitching_stats AS SELECT * FROM pitching_stats WHERE league_id IS NULL;
    """)
    # ~6 games: team pitched ~54 IP -> team_g ~ 6
    conn.execute("INSERT INTO team_pitching_stats VALUES (1, ?, 1, 54.0)", (YEAR,))
    conn.execute("INSERT INTO players VALUES (10, 'Slugger', 1)")
    conn.execute("INSERT INTO players VALUES (11, 'Closer', 1)")
    # Full-time hitter after 6 games: ~26 PA, 4 HR, 2 SB — below any full-season floor.
    conn.execute("INSERT INTO batting_stats VALUES (10, ?, 1, 1, NULL, "
                 "24, 9, 2, 0, 4, 9, 2, 6, 2, 26, 0.4, 8, 0)", (YEAR,))
    # Closer after 6 games: ~5 IP, 3 saves — below any full-season IP floor.
    conn.execute("INSERT INTO pitching_stats VALUES (11, ?, 1, 1, NULL, "
                 "5.0, 0.00, 8, 1, 0, 0, 3, 0.3, 2, 0)", (YEAR,))
    conn.commit()
    return conn


def test_batting_leaders_populate_early_season(monkeypatch):
    conn = _early_season_db()
    monkeypatch.setattr(queries, "get_db", lambda: conn)
    monkeypatch.setattr(queries, "team_abbr_map", lambda: {1: "TST"})
    monkeypatch.setattr(queries, "_build_league_team_sets", lambda: {})
    result = queries.get_batting_leaders(yr=YEAR)
    # Counting-stat panels must include the low-PA slugger.
    assert any(e["pid"] == 10 for e in result["All"]["HR"]), "HR leader hidden early season"
    assert any(e["pid"] == 10 for e in result["All"]["SB"])


def test_pitching_leaders_populate_early_season(monkeypatch):
    conn = _early_season_db()
    monkeypatch.setattr(queries, "get_db", lambda: conn)
    monkeypatch.setattr(queries, "team_abbr_map", lambda: {1: "TST"})
    monkeypatch.setattr(queries, "_build_league_team_sets", lambda: {})
    result = queries.get_pitching_leaders(yr=YEAR)
    # The low-IP closer with 3 saves must appear in the SV panel.
    sv = result["All"]["SV"]
    assert any(e["pid"] == 11 for e in sv), "save leader hidden early season"
    assert sv[0]["val"] == "3"


# ── positional rankings SP/RP classification (regression) ────────────────────
#
# SP/RP is classified from the gs/g ratio, NOT an absolute GS count. Six games
# in, an ace has only 1-2 starts, so any absolute GS floor (e.g. gs > 3) would
# misclassify every starter as a reliever, flooding the RP rankings and leaving
# SP empty. A reliever's occasional spot start keeps gs/g below 0.5.

def _pos_rankings_pitcher_db():
    """Minimal in-memory DB with two pitchers, ~2 games into the season:
    an ace SP (2 GS / 2 G) and a reliever who made 1 spot start (1 GS / 8 G)."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE players (player_id INTEGER PRIMARY KEY, name TEXT, age INTEGER,
            pos INTEGER, role INTEGER, team_id INTEGER, parent_team_id INTEGER, level TEXT,
            free_agent INTEGER DEFAULT 0);
        CREATE TABLE latest_ratings (player_id INTEGER, composite_score INTEGER,
            true_ceiling INTEGER, tool_only_score INTEGER, offensive_grade INTEGER,
            defensive_value INTEGER,
            ctrl INTEGER, c INTEGER, first_b INTEGER, second_b INTEGER, third_b INTEGER,
            ss INTEGER, lf INTEGER, cf INTEGER, rf INTEGER);
        CREATE TABLE pitching_stats (player_id INTEGER, year INTEGER, split_id INTEGER,
            league_id INTEGER, gs INTEGER, g INTEGER);
        CREATE TABLE batting_stats (player_id INTEGER, year INTEGER, split_id INTEGER,
            league_id INTEGER);
        CREATE TABLE prospect_fv (player_id INTEGER, bucket TEXT, fv INTEGER, fv_str TEXT,
            risk TEXT, prospect_surplus REAL);
        CREATE VIEW mlb_pitching_stats AS SELECT * FROM pitching_stats WHERE league_id IS NULL;
        CREATE VIEW mlb_batting_stats AS SELECT * FROM batting_stats WHERE league_id IS NULL;
    """)
    # Ace starter: 2 starts in 2 appearances.
    conn.execute("INSERT INTO players VALUES (20, 'Ace SP', 27, 1, 11, 1, NULL, '1', 0)")
    conn.execute("INSERT INTO latest_ratings VALUES (20, 70, 72, NULL, NULL, NULL, 60,"
                 "NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL)")
    conn.execute("INSERT INTO pitching_stats VALUES (20, ?, 1, NULL, 2, 2)", (YEAR,))
    # Reliever who made one spot start: 1 GS in 8 appearances -> gs/g = 0.125.
    conn.execute("INSERT INTO players VALUES (21, 'Setup RP', 29, 1, 13, 1, NULL, '1', 0)")
    conn.execute("INSERT INTO latest_ratings VALUES (21, 55, 57, NULL, NULL, NULL, 55,"
                 "NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL)")
    conn.execute("INSERT INTO pitching_stats VALUES (21, ?, 1, NULL, 1, 8)", (YEAR,))
    conn.commit()
    return conn


def _patch_pos_rankings(monkeypatch, conn):
    from types import SimpleNamespace
    monkeypatch.setattr(queries, "get_db", lambda: conn)
    monkeypatch.setattr(queries, "team_abbr_map", lambda: {1: "TST"})
    monkeypatch.setattr(queries, "get_cfg",
                        lambda: SimpleNamespace(year=YEAR, ratings_scale=20))
    # get_positional_rankings filters to the request-scoped mlb_team_ids();
    # make our fixture team (id 1) an MLB org so players aren't filtered out.
    monkeypatch.setattr(queries, "mlb_team_ids", lambda: {1})
    import statsplusplus.config.league_config as _lc
    monkeypatch.setattr(_lc, "LeagueConfig",
                        lambda *a, **k: SimpleNamespace(mlb_team_ids={1}))


def test_pos_rankings_includes_free_agents(monkeypatch):
    """Unsigned free agents (team_id=0, free_agent=1) with league stats appear
    in the rankings, tagged is_fa, interleaved by composite."""
    conn = _pos_rankings_pitcher_db()
    # Add an unsigned FA reliever with a prior season of MLB pitching in-league.
    conn.execute("INSERT INTO players VALUES (30, 'Free Reliever', 30, 1, 13, 0, NULL, '0', 1)")
    conn.execute("INSERT INTO latest_ratings VALUES (30, 60, 60, NULL, NULL, NULL, 55,"
                 "NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL)")
    conn.execute("INSERT INTO pitching_stats VALUES (30, ?, 1, NULL, 0, 40)", (YEAR,))
    conn.commit()
    _patch_pos_rankings(monkeypatch, conn)
    res = dict(queries.get_positional_rankings())
    rp = {p["pid"]: p for p in res["RP"]["mlb"]}
    assert 30 in rp, "unsigned FA reliever missing from RP rankings"
    assert rp[30]["is_fa"] is True
    assert rp[30]["team"] == "FA"
    # a rostered player is not tagged FA
    assert rp.get(21, {}).get("is_fa", False) is False


def test_pos_rankings_ace_classified_sp_early_season(monkeypatch):
    conn = _pos_rankings_pitcher_db()
    _patch_pos_rankings(monkeypatch, conn)
    res = dict(queries.get_positional_rankings())
    sp_pids = {p["pid"] for p in res["SP"]["mlb"]}
    rp_pids = {p["pid"] for p in res["RP"]["mlb"]}
    # Ace (2GS/2G) belongs in SP, not RP — even with only 2 starts on the season.
    assert 20 in sp_pids, "ace starter misclassified out of SP early season"
    assert 20 not in rp_pids


def test_pos_rankings_spot_starter_stays_rp(monkeypatch):
    conn = _pos_rankings_pitcher_db()
    _patch_pos_rankings(monkeypatch, conn)
    res = dict(queries.get_positional_rankings())
    sp_pids = {p["pid"] for p in res["SP"]["mlb"]}
    rp_pids = {p["pid"] for p in res["RP"]["mlb"]}
    # Reliever with one spot start (gs/g=0.125) stays in RP.
    assert 21 in rp_pids, "reliever with a spot start leaked into SP"
    assert 21 not in sp_pids


# ── search_players ───────────────────────────────────────────────────────────

def test_search_players_returns_list():
    result = queries.search_players("Joe")
    assert isinstance(result, list)

def test_search_players_finds_hitter():
    result = queries.search_players("Joe")
    assert any(r["pid"] == HITTER_ID for r in result)

def test_search_players_shape():
    result = queries.search_players("Joe")
    assert len(result) >= 1
    row = result[0]
    for key in ("pid", "name", "age", "level", "team", "pos"):
        assert key in row, f"Missing key: {key}"

def test_search_players_short_query_returns_empty():
    result = queries.search_players("J")
    assert result == []

def test_search_players_no_match_returns_empty():
    result = queries.search_players("zzznomatch")
    assert result == []
