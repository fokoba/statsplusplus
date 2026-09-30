"""Tests for cross-league (multi-top-league) scoping — Session 90.

A co-resident, non-primary top-level league (e.g. NPB in PPL — level=1,
player_league_id != primary) must NOT contaminate "our MLB": calibration,
positional medians, org-needs, and the mlb_* views. "MLB" = the primary league.

Covers:
  - primary_league_predicate(): no-op when None, correct clause+params otherwise.
  - mlb_* views exclude non-primary players' top-level stats when a primary is
    set (league_meta), and are a no-op when it isn't (backward compat).
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from statsplusplus.data.db import primary_league_predicate, init_schema


# ---------------------------------------------------------------------------
# primary_league_predicate
# ---------------------------------------------------------------------------

def test_predicate_noop_when_no_primary():
    clause, params = primary_league_predicate(None)
    assert clause == "1=1" and params == []


def test_predicate_scopes_when_primary_set():
    clause, params = primary_league_predicate(200)
    assert "player_league_id = ?" in clause
    assert "IS NULL" in clause  # backward-compat NULL allowance
    assert params == [200]


def test_predicate_custom_alias():
    clause, _ = primary_league_predicate(200, alias="pl")
    assert "pl.player_league_id" in clause


# ---------------------------------------------------------------------------
# mlb_* view scoping (via league_meta)
# ---------------------------------------------------------------------------

def _seed(tmp_path):
    """Minimal DB: 2 primary players, 1 NPB, 1 NULL-league; each with a
    top-level (league_id NULL) batting row."""
    ld = tmp_path
    (ld / "config").mkdir(parents=True, exist_ok=True)
    init_schema(ld)  # creates tables + league_meta + primary-scoped views
    conn = sqlite3.connect(ld / "league.db")
    conn.execute("INSERT INTO players (player_id, name, level, player_league_id) VALUES (1,'Prim A','1',200)")
    conn.execute("INSERT INTO players (player_id, name, level, player_league_id) VALUES (2,'Prim B','1',200)")
    conn.execute("INSERT INTO players (player_id, name, level, player_league_id) VALUES (3,'NPB',  '1',228)")
    conn.execute("INSERT INTO players (player_id, name, level, player_league_id) VALUES (4,'NullLg','1',NULL)")
    for pid in (1, 2, 3, 4):
        conn.execute("INSERT INTO batting_stats (player_id, year, team_id, split_id, league_id) "
                     "VALUES (?, 2034, 1, 1, NULL)", (pid,))
    conn.commit()
    return conn


def test_mlb_view_noop_without_primary(tmp_path):
    """No primary_league_id set → view includes all top-level stats (backward
    compat for single-top-league DBs)."""
    conn = _seed(tmp_path)
    # league_meta empty
    n = conn.execute("SELECT COUNT(*) FROM mlb_batting_stats").fetchone()[0]
    assert n == 4


def test_mlb_view_excludes_non_primary_when_set(tmp_path):
    """With primary=200, the NPB (228) player's top-level stats are excluded;
    primary and NULL-league players are kept."""
    conn = _seed(tmp_path)
    conn.execute("INSERT INTO league_meta (id, primary_league_id) VALUES (1, 200)")
    conn.commit()
    pids = {r[0] for r in conn.execute("SELECT player_id FROM mlb_batting_stats")}
    assert pids == {1, 2, 4}  # NPB (3) excluded; NULL-league (4) kept
