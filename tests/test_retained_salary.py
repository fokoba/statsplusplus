"""Retained salary: another team keeps paying part of a contract we hold."""
import sqlite3

import pytest

from statsplusplus.data.retained_salary import (
    effective_retention, get_retention, get_retention_map, set_retention,
)


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.execute(
        "CREATE TABLE retained_salary (player_id INTEGER PRIMARY KEY, "
        "retained_by_team_id INTEGER, pct REAL NOT NULL, note TEXT, updated_at TEXT)"
    )
    yield c
    c.close()


def test_no_row_means_no_retention(conn):
    assert get_retention(conn, 1) == 0.0
    assert get_retention_map(conn) == {}


def test_set_get_and_overwrite(conn):
    set_retention(conn, 22403, 1.0, retained_by_team_id=13, note="CIN")
    assert get_retention(conn, 22403) == 1.0
    set_retention(conn, 22403, 0.5)
    assert get_retention_map(conn) == {22403: 0.5}


def test_falsy_pct_clears(conn):
    set_retention(conn, 7, 0.75)
    set_retention(conn, 7, 0)
    assert get_retention(conn, 7) == 0.0


@pytest.mark.parametrize("bad", [-0.1, 1.5])
def test_out_of_range_pct_rejected(conn, bad):
    with pytest.raises(ValueError):
        set_retention(conn, 1, bad)


def test_missing_table_is_treated_as_no_retention():
    bare = sqlite3.connect(":memory:")
    assert get_retention(bare, 1) == 0.0
    assert get_retention_map(bare) == {}


def test_effective_retention_compounds_on_what_is_left():
    assert effective_retention(0.0, 0.0) == 0.0
    assert effective_retention(1.0, 0.0) == 1.0
    assert effective_retention(0.5, 0.5) == pytest.approx(0.75)
    assert effective_retention(1.0, 0.3) == 1.0
