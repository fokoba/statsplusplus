"""Position ratings from the game's exports survive API refreshes (pitchers)."""
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from statsplusplus.data.position_overrides import (
    POS_COLS, apply_position_overrides, save_position_overrides,
)


def _conn():
    c = sqlite3.connect(":memory:")
    cols = ", ".join(f"{col} INTEGER" for col in POS_COLS)
    c.executescript(f"""
    CREATE TABLE ratings (player_id INTEGER, snapshot_date TEXT, {cols});
    CREATE TABLE position_overrides (player_id INTEGER PRIMARY KEY, {cols}, updated_at TEXT);
    """)
    return c


def test_api_zero_is_refilled_from_export_but_real_api_values_win():
    c = _conn()
    # Pitcher: API left every position rating at 0. Hitter: API has a real value.
    c.execute("INSERT INTO ratings (player_id, snapshot_date, pot_lf, pot_cf, pot_rf) VALUES (1,'1955-06-13',0,0,0)")
    c.execute("INSERT INTO ratings (player_id, snapshot_date, pot_lf) VALUES (2,'1955-06-13',70)")
    save_position_overrides(c, 1, {"pot_lf": 95, "pot_cf": 85, "pot_rf": 85, "pot_first_b": 45})
    save_position_overrides(c, 2, {"pot_lf": 40})            # export disagrees; API value must win
    apply_position_overrides(c, "1955-06-13")
    p = c.execute("SELECT pot_lf, pot_cf, pot_rf, pot_first_b FROM ratings WHERE player_id=1").fetchone()
    assert p == (95, 85, 85, 45)
    assert c.execute("SELECT pot_lf FROM ratings WHERE player_id=2").fetchone()[0] == 70


def test_partial_export_does_not_clobber_stored_ratings():
    c = _conn()
    save_position_overrides(c, 1, {"pot_lf": 95, "pot_cf": 85})
    save_position_overrides(c, 1, {"pot_rf": 85})            # a later, narrower export
    row = c.execute("SELECT pot_lf, pot_cf, pot_rf FROM position_overrides WHERE player_id=1").fetchone()
    assert row == (95, 85, 85)
    assert save_position_overrides(c, 9, {"pot_lf": 0}) is False   # nothing positive to store


def test_new_snapshot_inherits_overrides_and_other_snapshots_are_untouched():
    c = _conn()
    c.execute("INSERT INTO ratings (player_id, snapshot_date, pot_cf) VALUES (1,'1955-05-02',0)")
    c.execute("INSERT INTO ratings (player_id, snapshot_date, pot_cf) VALUES (1,'1955-06-06',0)")
    save_position_overrides(c, 1, {"pot_cf": 85})
    apply_position_overrides(c, "1955-06-06")
    got = dict(c.execute("SELECT snapshot_date, pot_cf FROM ratings WHERE player_id=1").fetchall())
    assert got == {"1955-05-02": 0, "1955-06-06": 85}
