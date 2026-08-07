"""
test_db_migration.py — Tests for database schema migration logic.

Verifies that new columns for the custom player evaluation feature
are correctly added to ratings and ratings_history tables.
"""

import sqlite3
import sys
from pathlib import Path

import pytest

BASE = Path(__file__).parent.parent
sys.path.insert(0, str(BASE / "scripts"))

from statsplusplus.data.db import _migrate_ratings, _migrate_ratings_history, _migrate_ratings_components


class TestRatingsMigration:
    """Task 1.1: New columns in ratings table."""

    def test_migration_adds_composite_columns(self):
        """Migration adds composite_score, ceiling_score, tool_only_score, secondary_composite."""
        conn = sqlite3.connect(":memory:")
        conn.execute(
            "CREATE TABLE ratings (player_id INTEGER, snapshot_date TEXT, ovr INTEGER, "
            "PRIMARY KEY (player_id, snapshot_date))"
        )
        _migrate_ratings(conn)
        cols = {row[1] for row in conn.execute("PRAGMA table_info(ratings)").fetchall()}
        assert "composite_score" in cols
        assert "ceiling_score" in cols
        assert "tool_only_score" in cols
        assert "secondary_composite" in cols

    def test_migration_is_idempotent(self):
        """Running migration twice does not raise errors."""
        conn = sqlite3.connect(":memory:")
        conn.execute(
            "CREATE TABLE ratings (player_id INTEGER, snapshot_date TEXT, ovr INTEGER, "
            "PRIMARY KEY (player_id, snapshot_date))"
        )
        _migrate_ratings(conn)
        _migrate_ratings(conn)  # second run — should not error
        cols = {row[1] for row in conn.execute("PRAGMA table_info(ratings)").fetchall()}
        assert "composite_score" in cols

    def test_migration_preserves_existing_columns(self):
        """Migration does not drop or modify existing columns."""
        conn = sqlite3.connect(":memory:")
        conn.execute(
            "CREATE TABLE ratings (player_id INTEGER, snapshot_date TEXT, ovr INTEGER, pot INTEGER, "
            "PRIMARY KEY (player_id, snapshot_date))"
        )
        conn.execute("INSERT INTO ratings VALUES (1, '2033-01-01', 55, 60)")
        _migrate_ratings(conn)
        row = conn.execute("SELECT ovr, pot FROM ratings WHERE player_id = 1").fetchone()
        assert row[0] == 55
        assert row[1] == 60

    def test_new_columns_default_to_null(self):
        """New columns are NULL for existing rows after migration."""
        conn = sqlite3.connect(":memory:")
        conn.execute(
            "CREATE TABLE ratings (player_id INTEGER, snapshot_date TEXT, ovr INTEGER, "
            "PRIMARY KEY (player_id, snapshot_date))"
        )
        conn.execute("INSERT INTO ratings VALUES (1, '2033-01-01', 55)")
        _migrate_ratings(conn)
        row = conn.execute(
            "SELECT composite_score, ceiling_score, tool_only_score, secondary_composite "
            "FROM ratings WHERE player_id = 1"
        ).fetchone()
        assert row == (None, None, None, None)


class TestRatingsHistoryMigration:
    """Task 1.2: New columns in ratings_history table."""

    def test_migration_adds_composite_columns(self):
        """Migration adds composite_score and ceiling_score to ratings_history."""
        conn = sqlite3.connect(":memory:")
        conn.execute(
            "CREATE TABLE ratings_history (player_id INTEGER, snapshot_date TEXT, ovr INTEGER, "
            "PRIMARY KEY (player_id, snapshot_date))"
        )
        _migrate_ratings_history(conn)
        cols = {row[1] for row in conn.execute("PRAGMA table_info(ratings_history)").fetchall()}
        assert "composite_score" in cols
        assert "ceiling_score" in cols

    def test_migration_is_idempotent(self):
        """Running migration twice does not raise errors."""
        conn = sqlite3.connect(":memory:")
        conn.execute(
            "CREATE TABLE ratings_history (player_id INTEGER, snapshot_date TEXT, ovr INTEGER, "
            "PRIMARY KEY (player_id, snapshot_date))"
        )
        _migrate_ratings_history(conn)
        _migrate_ratings_history(conn)
        cols = {row[1] for row in conn.execute("PRAGMA table_info(ratings_history)").fetchall()}
        assert "composite_score" in cols

    def test_migration_preserves_existing_data(self):
        """Migration does not drop or modify existing data."""
        conn = sqlite3.connect(":memory:")
        conn.execute(
            "CREATE TABLE ratings_history (player_id INTEGER, snapshot_date TEXT, ovr INTEGER, pot INTEGER, "
            "PRIMARY KEY (player_id, snapshot_date))"
        )
        conn.execute("INSERT INTO ratings_history VALUES (1, '2033-01-01', 55, 60)")
        _migrate_ratings_history(conn)
        row = conn.execute("SELECT ovr, pot FROM ratings_history WHERE player_id = 1").fetchone()
        assert row[0] == 55
        assert row[1] == 60

    def test_new_columns_default_to_null(self):
        """New columns are NULL for existing rows after migration."""
        conn = sqlite3.connect(":memory:")
        conn.execute(
            "CREATE TABLE ratings_history (player_id INTEGER, snapshot_date TEXT, ovr INTEGER, "
            "PRIMARY KEY (player_id, snapshot_date))"
        )
        conn.execute("INSERT INTO ratings_history VALUES (1, '2033-01-01', 55)")
        _migrate_ratings_history(conn)
        row = conn.execute(
            "SELECT composite_score, ceiling_score FROM ratings_history WHERE player_id = 1"
        ).fetchone()
        assert row == (None, None)


class TestFreshSchema:
    """Verify fresh SCHEMA string includes all new columns."""

    def test_ratings_schema_has_new_columns(self):
        from statsplusplus.data.db import SCHEMA
        conn = sqlite3.connect(":memory:")
        conn.executescript(SCHEMA)
        cols = {row[1] for row in conn.execute("PRAGMA table_info(ratings)").fetchall()}
        assert "composite_score" in cols
        assert "ceiling_score" in cols
        assert "tool_only_score" in cols
        assert "secondary_composite" in cols

    def test_ratings_history_schema_has_new_columns(self):
        from statsplusplus.data.db import SCHEMA
        conn = sqlite3.connect(":memory:")
        conn.executescript(SCHEMA)
        cols = {row[1] for row in conn.execute("PRAGMA table_info(ratings_history)").fetchall()}
        assert "composite_score" in cols
        assert "ceiling_score" in cols


COMPONENT_COLS = {
    "offensive_grade",
    "baserunning_value",
    "defensive_value",
    "durability_score",
    "offensive_ceiling",
}


class TestRatingsComponentsMigration:
    """Task 6.4: Component score columns added idempotently to ratings and ratings_history."""

    def _make_base_db(self) -> sqlite3.Connection:
        """Create an in-memory DB with ratings and ratings_history tables lacking component columns."""
        conn = sqlite3.connect(":memory:")
        conn.execute(
            "CREATE TABLE ratings (player_id INTEGER, snapshot_date TEXT, ovr INTEGER, "
            "composite_score INTEGER, ceiling_score INTEGER, "
            "PRIMARY KEY (player_id, snapshot_date))"
        )
        conn.execute(
            "CREATE TABLE ratings_history (player_id INTEGER, snapshot_date TEXT, ovr INTEGER, "
            "composite_score INTEGER, ceiling_score INTEGER, "
            "PRIMARY KEY (player_id, snapshot_date))"
        )
        return conn

    def test_migrate_ratings_components_adds_columns(self):
        """Migration adds all 5 component columns to both ratings and ratings_history."""
        conn = self._make_base_db()
        _migrate_ratings_components(conn)

        for table in ("ratings", "ratings_history"):
            cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
            for col in COMPONENT_COLS:
                assert col in cols, f"{col} missing from {table}"

    def test_migrate_ratings_components_idempotent(self):
        """Running migration twice does not raise errors and columns still exist."""
        conn = self._make_base_db()
        _migrate_ratings_components(conn)
        _migrate_ratings_components(conn)  # second run — should not error

        for table in ("ratings", "ratings_history"):
            cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
            for col in COMPONENT_COLS:
                assert col in cols, f"{col} missing from {table} after second migration"

    def test_migrate_ratings_components_preserves_existing_data(self):
        """Existing rows retain their data and new columns default to NULL."""
        conn = self._make_base_db()
        conn.execute(
            "INSERT INTO ratings VALUES (1, '2033-06-01', 55, 60, 65)"
        )
        _migrate_ratings_components(conn)

        row = conn.execute(
            "SELECT ovr, composite_score, ceiling_score, "
            "offensive_grade, baserunning_value, defensive_value, "
            "durability_score, offensive_ceiling "
            "FROM ratings WHERE player_id = 1"
        ).fetchone()
        # Original data preserved
        assert row[0] == 55   # ovr
        assert row[1] == 60   # composite_score
        assert row[2] == 65   # ceiling_score
        # New component columns are NULL
        assert row[3] is None  # offensive_grade
        assert row[4] is None  # baserunning_value
        assert row[5] is None  # defensive_value
        assert row[6] is None  # durability_score
        assert row[7] is None  # offensive_ceiling
