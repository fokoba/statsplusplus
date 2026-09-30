"""Regression test for the cross-league draft leak (Session 90).

The web draft endpoints call draft_board.load_board() etc. Those must resolve
all data from the REQUEST's league (session-aware, passed as league_dir), not
the process-global active league (app_config.json / STATSPP_LEAGUE). A mismatch
built the auto-draft list from the wrong league — names from league A paired
with player-page links resolving in league B.

These tests verify the plumbing: the data helpers read from the passed
league_dir even when the global active league points elsewhere.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE / "scripts"))
sys.path.insert(0, str(BASE / "src"))

import draft_board as db  # noqa: E402


def test_load_pool_ids_uses_passed_league_dir(tmp_path, monkeypatch):
    """_load_pool_ids reads draft_pool.json from the passed dir, not the global
    active league."""
    # Global active league points somewhere else entirely.
    monkeypatch.setenv("STATSPP_LEAGUE", "some_other_league")

    league_dir = tmp_path / "league_a"
    (league_dir / "config").mkdir(parents=True)
    (league_dir / "config" / "draft_pool.json").write_text(
        json.dumps({"player_ids": [101, 102, 103]}))

    assert db._load_pool_ids(league_dir) == [101, 102, 103]


def test_connect_uses_passed_league_dir(tmp_path, monkeypatch):
    """_connect opens the DB under the passed dir, not the global active league."""
    import sqlite3
    monkeypatch.setenv("STATSPP_LEAGUE", "some_other_league")

    league_dir = tmp_path / "league_b"
    league_dir.mkdir()
    # Seed a trivial DB so we can prove it's the one we opened.
    conn0 = sqlite3.connect(str(league_dir / "league.db"))
    conn0.execute("CREATE TABLE marker (id INTEGER)")
    conn0.execute("INSERT INTO marker VALUES (42)")
    conn0.commit()
    conn0.close()

    conn = db._connect(league_dir)
    assert conn.execute("SELECT id FROM marker").fetchone()[0] == 42
    conn.close()


def test_no_arg_falls_back_to_global(tmp_path, monkeypatch):
    """Backward compat: with no league_dir, helpers fall back to the active
    league (CLI usage path)."""
    league_dir = tmp_path / "active"
    (league_dir / "config").mkdir(parents=True)
    (league_dir / "config" / "draft_pool.json").write_text(
        json.dumps({"player_ids": [7, 8]}))
    monkeypatch.setattr(db, "get_league_dir", lambda slug=None: league_dir)

    assert db._load_pool_ids() == [7, 8]


# ---------------------------------------------------------------------------
# Stale-pool detection (auto-draft list showed a prior draft's players)
# ---------------------------------------------------------------------------

def _mk_conn_with_levels(tmp_path, id_levels):
    """A minimal DB with a players(player_id, level) table seeded from a dict."""
    import sqlite3
    p = tmp_path / "league.db"
    conn = sqlite3.connect(str(p))
    conn.execute("CREATE TABLE players (player_id INTEGER, level TEXT)")
    conn.executemany("INSERT INTO players VALUES (?,?)",
                     [(pid, lvl) for pid, lvl in id_levels.items()])
    conn.commit()
    conn.row_factory = sqlite3.Row
    return conn


def test_pool_is_stale_when_players_drafted(tmp_path):
    """A pool whose players have mostly moved off amateur levels (drafted into
    orgs) is stale."""
    # 8 of 10 now on org levels (1/3/4), only 2 still amateur (0) → stale.
    id_levels = {i: ("0" if i < 2 else str((i % 4) + 1)) for i in range(10)}
    conn = _mk_conn_with_levels(tmp_path, id_levels)
    assert db._pool_is_stale(conn, list(id_levels)) is True


def test_pool_is_fresh_when_mostly_amateur(tmp_path):
    """A pool that's mostly still draft-eligible is fresh (not stale)."""
    id_levels = {i: ("0" if i < 8 else "1") for i in range(10)}  # 80% amateur
    conn = _mk_conn_with_levels(tmp_path, id_levels)
    assert db._pool_is_stale(conn, list(id_levels)) is False


def test_pool_is_stale_when_ids_dont_resolve(tmp_path):
    """Pool IDs that don't exist in this DB (foreign/wrong league) → stale."""
    conn = _mk_conn_with_levels(tmp_path, {1: "0"})
    assert db._pool_is_stale(conn, [9001, 9002, 9003]) is True


def test_empty_pool_not_stale(tmp_path):
    conn = _mk_conn_with_levels(tmp_path, {1: "0"})
    assert db._pool_is_stale(conn, []) is False
