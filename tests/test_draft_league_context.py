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
