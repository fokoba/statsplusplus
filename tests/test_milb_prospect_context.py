import sqlite3
import json

import pytest

from statsplusplus.data.milb import compute_milb_averages, load_milb_averages, load_milb_stat_seasons, completed_season_signal
from statsplusplus.data.fv_calc import _apply_milb_context
from statsplusplus.evaluation.composite import stat_to_2080


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript("""
        CREATE TABLE batting_stats(player_id INTEGER,year INTEGER,league_id INTEGER,split_id INTEGER,
          ab INTEGER,pa INTEGER,h INTEGER,d INTEGER DEFAULT 0,t INTEGER DEFAULT 0,hr INTEGER DEFAULT 0,
          bb INTEGER DEFAULT 0,hbp INTEGER DEFAULT 0,sf INTEGER DEFAULT 0);
        CREATE TABLE pitching_stats(player_id INTEGER,year INTEGER,league_id INTEGER,split_id INTEGER,
          ip REAL,outs INTEGER,er INTEGER,k INTEGER DEFAULT 0,bb INTEGER DEFAULT 0,hra INTEGER DEFAULT 0,
          g INTEGER DEFAULT 0,gs INTEGER DEFAULT 0);
    """)
    yield c
    c.close()


def test_pool_ratios_are_weighted_and_sacrifice_bunts_do_not_lower_obp(conn):
    # The first player's PA include 50 sacrifice bunts. The smaller player
    # should not count equally in the league average, nor should split2 count.
    conn.executemany("INSERT INTO batting_stats(player_id,year,league_id,split_id,ab,pa,h,bb) VALUES(?,?,?,?,?,?,?,?)", [
        (1, 1955, 225, 1, 900, 1050, 300, 100),
        (2, 1955, 225, 1, 50, 50, 0, 0),
        (1, 1955, 225, 2, 100, 100, 100, 0),
    ])
    ctx = compute_milb_averages(conn, {225: 2})
    avg = ctx[225]["seasons"]["1955"]["batting_avg"]
    assert avg["obp"] == pytest.approx(400/1050)
    assert avg["slg"] == pytest.approx(300/950)
    rows = load_milb_stat_seasons(conn, 1, False, ctx)
    expected = stat_to_2080(100*(.4/(400/1050)+(300/900)/(300/950)-1))
    assert rows[0]["stat_2080"] == pytest.approx(expected)


def test_each_season_uses_its_own_environment(conn):
    conn.executemany("INSERT INTO batting_stats(player_id,year,league_id,split_id,ab,pa,h) VALUES(?,?,?,?,?,?,?)", [
        (1, 1954, 225, 1, 100, 100, 20),
        (1, 1955, 225, 1, 100, 100, 60),
    ])
    ctx = compute_milb_averages(conn, {225: 2})
    rows = load_milb_stat_seasons(conn, 1, False, ctx)
    assert [r["stat_2080"] for r in rows] == [stat_to_2080(100)]*2
    del ctx[225]["seasons"]["1954"]
    assert [r["year"] for r in load_milb_stat_seasons(conn, 1, False, ctx)] == [1955]


def test_pitching_samples_use_outs_not_baseball_decimal_innings(conn):
    conn.execute("INSERT INTO pitching_stats(player_id,year,league_id,split_id,ip,outs,er) VALUES(1,1955,225,1,20.1,61,7)")
    ctx = compute_milb_averages(conn, {225: 2})
    row = load_milb_stat_seasons(conn, 1, True, ctx)[0]
    assert row["ip"] == pytest.approx(61/3)
    assert row["era_minus_inv"] == pytest.approx(100)


def test_completed_season_signal_excludes_current_and_future_data():
    cfg = {"level_weights": {"2": .75}, "seasons": 3, "recency": .7, "prior_opportunities": 200}
    rows = [{"year": 1955, "level": 2, "pa": 100, "stat_2080": 60},
            {"year": 1956, "level": 2, "pa": 10000, "stat_2080": 80},
            {"year": 1957, "level": 2, "pa": 10000, "stat_2080": 80}]
    signal = completed_season_signal(rows, 1956, False, cfg)
    assert signal["effective_opportunities"] == 75
    assert signal["stat_2080"] == 60
    assert signal["signal"] == pytest.approx((10/15)*(75/275))


def test_unknown_leagues_and_absent_history_are_neutral(conn):
    assert load_milb_stat_seasons(conn, 999, False, {}) == []
    assert completed_season_signal([], 1956, False, {})["signal"] == 0


def test_calibrated_context_refines_risk_without_fabricating_ceiling(conn):
    p = {"Pot": 65, "Age": 22, "_norm_age": 26, "_is_pitcher": False, "level": 2}
    model = {"milb": {"enabled": True, "level_weights": {"2": .75}}}
    rows = [{"year": 1955, "level": 2, "pa": 400, "stat_2080": 65}]
    _apply_milb_context(p, conn, 1, {225: {}}, {}, {}, lambda *a: rows,
                        risk_model=model, prediction_year=1956)
    assert p["Pot"] == 65
    assert p["_milb_risk_signal"] > 0
    assert "_stat_risk_modifier" not in p


def test_context_survives_missing_refresh_averages_and_tracks_new_counts(conn, tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "config/league_settings.json").write_text(json.dumps({"minor_leagues": [{"lid": 225, "level": 2}]}))
    (tmp_path / "config/milb_averages.json").write_text('{"schema_version":1}')
    conn.execute("INSERT INTO batting_stats(player_id,year,league_id,split_id,ab,pa,h) VALUES(1,1955,225,1,100,100,20)")
    conn.commit()
    dest = sqlite3.connect(tmp_path / "league.db")
    conn.backup(dest)
    dest.close()
    assert load_milb_averages(tmp_path)[225]["seasons"]["1955"]["batting_avg"]["obp"] == .2
    dest = sqlite3.connect(tmp_path / "league.db")
    dest.execute("UPDATE batting_stats SET h=40")
    dest.commit()
    dest.close()
    assert load_milb_averages(tmp_path)[225]["seasons"]["1955"]["batting_avg"]["obp"] == .4
