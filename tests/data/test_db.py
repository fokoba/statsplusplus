"""Tests for statsplusplus.data.db — connection and schema management."""

import sqlite3

import pytest

from statsplusplus.data.db import get_connection, init_schema, SCHEMA


class TestGetConnection:
    def test_creates_db_file(self, tmp_path):
        """Connection creates the DB file if it doesn't exist."""
        league_dir = tmp_path / "test_league"
        league_dir.mkdir()
        conn = get_connection(league_dir)
        assert (league_dir / "league.db").exists()
        conn.close()

    def test_wal_mode(self, tmp_path):
        """Connection uses WAL journal mode."""
        league_dir = tmp_path / "test_league"
        league_dir.mkdir()
        conn = get_connection(league_dir)
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode == "wal"
        conn.close()

    def test_row_factory(self, tmp_path):
        """Connection uses sqlite3.Row factory."""
        league_dir = tmp_path / "test_league"
        league_dir.mkdir()
        conn = get_connection(league_dir)
        assert conn.row_factory == sqlite3.Row
        conn.close()

    def test_latest_ratings_view_exists(self, tmp_path):
        """Connection creates the latest_ratings view."""
        league_dir = tmp_path / "test_league"
        league_dir.mkdir()
        conn = get_connection(league_dir)
        conn.executescript(SCHEMA)
        # View should exist and be queryable (empty result is fine)
        rows = conn.execute("SELECT * FROM latest_ratings").fetchall()
        assert rows == []
        conn.close()

    def test_context_manager(self, tmp_path):
        """Connection works as context manager."""
        league_dir = tmp_path / "test_league"
        league_dir.mkdir()
        with get_connection(league_dir) as conn:
            conn.execute("SELECT 1")


class TestInitSchema:
    def test_creates_all_tables(self, tmp_path):
        """init_schema creates all expected tables."""
        league_dir = tmp_path / "test_league"
        league_dir.mkdir()
        init_schema(league_dir)

        conn = get_connection(league_dir)
        tables = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}

        expected_tables = {
            "teams", "players", "ratings", "contracts", "contract_extensions",
            "batting_stats", "pitching_stats", "fielding_stats",
            "team_batting_stats", "team_pitching_stats",
            "games", "ratings_history", "trade_block", "standings",
            "prospect_fv", "player_surplus", "org_reports",
        }
        assert expected_tables.issubset(tables)
        conn.close()

    def test_creates_mlb_views(self, tmp_path):
        """init_schema creates MLB-only stat views."""
        league_dir = tmp_path / "test_league"
        league_dir.mkdir()
        init_schema(league_dir)

        conn = get_connection(league_dir)
        views = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='view'"
        ).fetchall()}

        assert "mlb_batting_stats" in views
        assert "mlb_pitching_stats" in views
        assert "mlb_fielding_stats" in views
        assert "latest_ratings" in views
        conn.close()

    def test_idempotent(self, tmp_path):
        """init_schema can run multiple times without error."""
        league_dir = tmp_path / "test_league"
        league_dir.mkdir()
        init_schema(league_dir)
        init_schema(league_dir)  # Second call should not raise
        init_schema(league_dir)  # Third call should not raise

    def test_player_columns_migrated(self, tmp_path):
        """Players table has expanded fields after migration."""
        league_dir = tmp_path / "test_league"
        league_dir.mkdir()
        init_schema(league_dir)

        conn = get_connection(league_dir)
        cols = {row[1] for row in conn.execute("PRAGMA table_info(players)").fetchall()}

        # Check a sampling of expanded fields
        assert "injury_is_injured" in cols
        assert "mlb_service_years" in cols
        assert "designated_for_assignment" in cols
        assert "draft_year" in cols
        assert "date_of_birth" in cols
        conn.close()

    def test_contract_columns_migrated(self, tmp_path):
        """Contracts table has option/incentive fields after migration."""
        league_dir = tmp_path / "test_league"
        league_dir.mkdir()
        init_schema(league_dir)

        conn = get_connection(league_dir)
        cols = {row[1] for row in conn.execute("PRAGMA table_info(contracts)").fetchall()}

        assert "last_year_vesting_option" in cols
        assert "minimum_pa" in cols
        assert "mvp_bonus" in cols
        assert "allstar_bonus" in cols
        conn.close()

    def test_stats_have_league_id(self, tmp_path):
        """Stat tables have league_id column for MiLB stats."""
        league_dir = tmp_path / "test_league"
        league_dir.mkdir()
        init_schema(league_dir)

        conn = get_connection(league_dir)
        for table in ("batting_stats", "pitching_stats", "fielding_stats"):
            cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
            assert "league_id" in cols, f"Missing league_id in {table}"
        conn.close()

    def test_ratings_component_columns(self, tmp_path):
        """Ratings table has component score columns."""
        league_dir = tmp_path / "test_league"
        league_dir.mkdir()
        init_schema(league_dir)

        conn = get_connection(league_dir)
        cols = {row[1] for row in conn.execute("PRAGMA table_info(ratings)").fetchall()}

        assert "composite_score" in cols
        assert "ceiling_score" in cols
        assert "offensive_grade" in cols
        assert "defensive_value" in cols
        assert "true_ceiling" in cols
        conn.close()

    def test_insert_and_query(self, tmp_path):
        """Basic insert/query works after schema init."""
        league_dir = tmp_path / "test_league"
        league_dir.mkdir()
        init_schema(league_dir)

        conn = get_connection(league_dir)
        conn.execute(
            "INSERT INTO players (player_id, name, age, team_id, parent_team_id, level, pos, role) "
            "VALUES (1, 'Test Player', 25, 44, 44, '1', 6, 0)"
        )
        conn.commit()

        row = conn.execute("SELECT * FROM players WHERE player_id = 1").fetchone()
        assert row["name"] == "Test Player"
        assert row["age"] == 25
        assert row["team_id"] == 44
        conn.close()
