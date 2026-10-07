"""Draft-day potential / FV / ExpRd stay frozen once the draft starts."""
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "web"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import queries


def _conn(drafted=False):
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript("""
    CREATE TABLE players (player_id INTEGER, draft_year INTEGER, draft_round INTEGER);
    CREATE TABLE draft_day_snapshot (draft_year INTEGER NOT NULL, player_id INTEGER NOT NULL,
        pot INTEGER, fv INTEGER, fv_str TEXT, exp_round INTEGER, source TEXT, captured_at TEXT,
        PRIMARY KEY (draft_year, player_id));
    """)
    if drafted:
        c.execute("INSERT INTO players VALUES (99, 1955, 1)")
    return c


def _row(pid, pot, fv_str, rd):
    return {"pid": pid, "pot": pot, "fv": int(fv_str.rstrip("+")), "fv_str": fv_str, "adp": {"exp_round": rd}}


def test_pre_draft_snapshot_tracks_the_live_board():
    c = _conn(drafted=False)
    r = [_row(1, 60, "55", 2)]
    queries._attach_draft_day(c, r, 1955)
    assert (r[0]["dd_pot"], r[0]["dd_fv_str"], r[0]["dd_exp_round"], r[0]["dd_source"]) == (60, "55", 2, "captured")
    r2 = [_row(1, 64, "60", 1)]                       # ratings improved before the draft
    queries._attach_draft_day(c, r2, 1955)
    assert (r2[0]["dd_pot"], r2[0]["dd_fv_str"], r2[0]["dd_exp_round"]) == (64, "60", 1)


def test_snapshot_freezes_once_the_draft_has_started():
    c = _conn(drafted=True)
    c.execute("INSERT INTO draft_day_snapshot VALUES (1955, 1, 70, 65, '65', 1, 'captured', 'x')")
    r = [_row(1, 52, "50", 5)]                        # board later moved after a model change
    queries._attach_draft_day(c, r, 1955)
    assert (r[0]["dd_pot"], r[0]["dd_fv_str"], r[0]["dd_exp_round"]) == (70, "65", 1)


def test_reconstructed_rows_are_never_overwritten_and_other_years_are_separate():
    c = _conn(drafted=False)
    c.execute("INSERT INTO draft_day_snapshot VALUES (1955, 1, 74, 65, '65', 1, 'reconstructed', 'x')")
    c.execute("INSERT INTO draft_day_snapshot VALUES (1954, 1, 40, 40, '40', 9, 'captured', 'x')")
    r = [_row(1, 50, "45", 4)]
    queries._attach_draft_day(c, r, 1955)
    assert (r[0]["dd_pot"], r[0]["dd_fv_str"], r[0]["dd_source"]) == (74, "65", "reconstructed")
    assert c.execute("SELECT pot FROM draft_day_snapshot WHERE draft_year=1954").fetchone()[0] == 40


def test_missing_table_or_year_is_harmless():
    r = [_row(1, 50, "45", 4)]
    queries._attach_draft_day(sqlite3.connect(":memory:"), r, 1955)   # no tables: swallowed
    queries._attach_draft_day(_conn(), r, None)
    assert "dd_pot" not in r[0]
