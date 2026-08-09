"""Tests for statsplusplus.web.context — request-scoped connection caching."""

import json
import sqlite3

import pytest

from statsplusplus.web.app import create_app
from statsplusplus.data.db import get_connection, init_schema


@pytest.fixture
def league_dir(tmp_path):
    """Create a minimal league directory with config and DB."""
    ld = tmp_path / "test_league"
    ld.mkdir()
    config_dir = ld / "config"
    config_dir.mkdir()

    # state.json
    (config_dir / "state.json").write_text(json.dumps({
        "game_date": "2033-07-15",
        "year": 2033,
        "my_team_id": 44,
    }))

    # league_settings.json
    (config_dir / "league_settings.json").write_text(json.dumps({
        "league": "Test League",
        "ratings_scale": "1-100",
        "team_names": {"44": "Test Team"},
        "team_abbr": {"44": "TST"},
        "divisions": {"Division": [44]},
        "pos_map": {"1": "P", "2": "C"},
        "role_map": {"11": "SP"},
        "level_map": {"1": "MLB"},
        "minimum_salary": 825000,
    }))

    # league_averages.json
    (config_dir / "league_averages.json").write_text(json.dumps({
        "year": 2033, "teams_in_sample": 20,
        "batting": {"avg": 0.255, "obp": 0.320, "slg": 0.410, "ops": 0.730,
                    "woba": 0.315, "babip": 0.295, "iso": 0.155, "k_pct": 0.22, "bb_pct": 0.08},
        "pitching": {"era": 4.10, "fip": 4.05, "x_fip": 4.10, "k_pct": 0.22,
                     "bb_pct": 0.08, "k_bb_pct": 0.14, "babip": 0.295, "avg": 0.255, "obp": 0.320},
        "dollar_per_war": 8000000,
    }))

    # Initialize DB with schema
    init_schema(ld)

    # Insert some test data
    conn = get_connection(ld)
    conn.execute(
        "INSERT INTO players (player_id, name, age, team_id, parent_team_id, level, pos, role) "
        "VALUES (1, 'Test Player', 25, 44, 44, '1', 6, 0)"
    )
    conn.execute(
        "INSERT INTO player_evaluation (player_id, eval_date, name, bucket, age, level, "
        "team_id, parent_team_id, composite, ceiling, fv, fv_str, surplus, surplus_yr1, "
        "stat_confidence, peak_war, years_control, ctrl_type) "
        "VALUES (1, '2033-07-15', 'Test Player', 'SS', 25, 'MLB', "
        "44, 44, 55, 60, 55, '55', 10000000, 5000000, "
        "1.0, 3.0, 3, 'contract')"
    )
    conn.execute(
        "INSERT INTO batting_stats (player_id, year, team_id, split_id, ab, h, pa) "
        "VALUES (1, 2033, 44, 1, 300, 80, 350)"
    )
    conn.commit()
    conn.close()

    return ld


@pytest.fixture
def app(league_dir, monkeypatch):
    """Create a test Flask app pointed at the test league."""
    import os
    monkeypatch.setenv("STATSPP_LEAGUE", "test_league")
    # Monkey-patch the league dir resolution to use our tmp dir
    monkeypatch.setattr(
        "statsplusplus.config.league_context.APP_CONFIG_PATH",
        league_dir.parent / "app_config.json",
    )
    # Write app config pointing to our test league
    (league_dir.parent / "app_config.json").write_text(json.dumps({
        "active_league": "test_league",
    }))
    # Patch _project_root to return the parent of our league dir
    monkeypatch.setattr(
        "statsplusplus.config.league_context._project_root",
        lambda: league_dir.parent,
    )
    # Also patch the data.db module's league resolution
    monkeypatch.setattr(
        "statsplusplus.data.db.get_league_dir",
        lambda slug=None: league_dir,
    )

    test_app = create_app(league_dir.parent)
    test_app.config["TESTING"] = True
    return test_app


class TestRequestContext:
    def test_get_conn_returns_same_connection(self, app, league_dir):
        """Within a request, get_conn() returns the same connection object."""
        from statsplusplus.web.context import get_conn

        with app.test_request_context("/"):
            from flask import g
            g.league_dir = league_dir
            g.league_config = None  # Not needed for this test

            conn1 = get_conn()
            conn2 = get_conn()
            assert conn1 is conn2

    def test_conn_closed_on_teardown(self, app, league_dir):
        """Connection is closed after request teardown."""
        from statsplusplus.web.context import get_conn, close_conn

        with app.test_request_context("/"):
            from flask import g
            g.league_dir = league_dir

            conn = get_conn()
            # Verify it's open
            conn.execute("SELECT 1")
            close_conn()
            # Connection should be closed now
            # (sqlite3 doesn't raise on closed cursor in all cases,
            # but g._db_conn should be None)
            assert g._db_conn is None

    def test_get_eval_date_cached(self, app, league_dir):
        """get_eval_date() returns cached value on second call."""
        from statsplusplus.web.context import get_eval_date, get_conn

        with app.test_request_context("/"):
            from flask import g
            g.league_dir = league_dir

            ed1 = get_eval_date()
            ed2 = get_eval_date()
            assert ed1 == ed2
            assert ed1 == "2033-07-15"

    def test_get_league_averages_cached(self, app, league_dir):
        """get_league_averages() caches per request."""
        from statsplusplus.web.context import get_league_averages

        with app.test_request_context("/"):
            from flask import g
            g.league_dir = league_dir

            # Need a minimal cfg for league_dir access
            class FakeCfg:
                def __init__(self, ld):
                    self._league_dir = ld
                @property
                def league_dir(self):
                    return self._league_dir
                @property
                def year(self):
                    return 2033
            g.league_config = FakeCfg(league_dir)

            la1 = get_league_averages()
            la2 = get_league_averages()
            assert la1 is la2  # Same object (cached)
            assert la1["dollar_per_war"] == 8_000_000


# ---------------------------------------------------------------------------
# App Factory + Blueprints
# ---------------------------------------------------------------------------

class TestAppFactory:
    def test_blueprints_registered(self, app):
        """All blueprints should be registered."""
        assert "team" in app.blueprints
        assert "league" in app.blueprints
        assert "player" in app.blueprints

    def test_blueprint_routes_exist(self, app):
        """Blueprint routes should be in the URL map."""
        rules = [r.rule for r in app.url_map.iter_rules()]
        assert "/team/<int:tid>" in rules
        assert "/team/<int:tid>/minors" in rules
        assert "/league" in rules
        assert "/player/<int:pid>" in rules
