"""WAR pace must use only the player's current team's stats (traded players)."""
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import war_pace


def _conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript("""
    CREATE TABLE players (player_id INTEGER PRIMARY KEY, team_id INTEGER, parent_team_id INTEGER,
                          pos INTEGER, role INTEGER);
    CREATE TABLE mlb_batting_stats (player_id INT, year INT, team_id INT, split_id INT, g INT, war REAL);
    CREATE TABLE mlb_pitching_stats (player_id INT, year INT, team_id INT, split_id INT, ip REAL, war REAL);
    """)
    return c


def test_traded_hitter_pace_uses_new_team_only(tmp_path):
    c = _conn()
    c.execute("INSERT INTO players VALUES (1, 6, 0, 2, 0)")          # now on team 6
    c.execute("INSERT INTO mlb_batting_stats VALUES (1, 1955, 13, 1, 29, 1.1471)")  # old team, hot
    c.execute("INSERT INTO mlb_batting_stats VALUES (1, 1955, 6, 1, 8, 0.2262)")    # new team, tiny sample
    assert war_pace.get_war_pace(1, tmp_path, c) is None             # 8 G < minimum: no pace
    assert 1 not in war_pace.get_all_war_paces(tmp_path, c)


def test_traded_hitter_with_enough_games_paces_on_new_team(tmp_path):
    c = _conn()
    c.execute("INSERT INTO players VALUES (1, 6, 0, 2, 0)")
    c.execute("INSERT INTO mlb_batting_stats VALUES (1, 1955, 13, 1, 29, 5.0)")     # old team, huge
    c.execute("INSERT INTO mlb_batting_stats VALUES (1, 1955, 6, 1, 40, 1.0)")      # new team
    pace = war_pace.get_war_pace(1, tmp_path, c)
    bulk = war_pace.get_all_war_paces(tmp_path, c)[1]
    assert pace["sample"] == 40 and pace["war_to_date"] == 1.0
    assert bulk["sample"] == 40 and bulk["war_to_date"] == 1.0


def test_optioned_player_uses_parent_club(tmp_path):
    c = _conn()
    c.execute("INSERT INTO players VALUES (2, 19, 6, 2, 0)")         # AAA affiliate of team 6
    c.execute("INSERT INTO mlb_batting_stats VALUES (2, 1955, 6, 1, 30, 1.5)")
    assert war_pace.get_war_pace(2, tmp_path, c)["sample"] == 30
