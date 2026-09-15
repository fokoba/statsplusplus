"""tests/test_pctile_levels.py — MiLB level resolution for percentile views.

Regression coverage for the "Mobile L0" / skipped-percentile-year bug: minor
leagues get reorganized or removed over the years, but historical stat rows keep
referencing the league_id they were played in. Those orphaned league_ids must
still resolve to a level bucket (the synthetic "MiLB" bucket) so that:

  (1) available_pctile_levels surfaces a bucket for them, and
  (2) available_pctile_years does not silently drop seasons played in them.
"""

import json
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent / "web"))
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

import percentiles


def _db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE batting_stats (player_id INTEGER, year INTEGER, split_id INTEGER,
            league_id INTEGER, pa INTEGER);
        CREATE TABLE pitching_stats (player_id INTEGER, year INTEGER, split_id INTEGER,
            league_id INTEGER, ip REAL);
        CREATE VIEW mlb_batting_stats AS SELECT * FROM batting_stats WHERE league_id IS NULL;
        CREATE VIEW mlb_pitching_stats AS SELECT * FROM pitching_stats WHERE league_id IS NULL;
    """)
    # MLB (NULL) 1954, known-AAA league 225 in 1954, known-A league 207 in 1949-1950,
    # and ORPHANED league 221 (not in settings) in 1952-1953.
    rows = [
        (1, 1954, 1, None, 400),   # MLB
        (1, 1954, 1, 225, 100),    # AAA (level 2)
        (1, 1950, 1, 207, 300),    # A (level 4)
        (1, 1949, 1, 207, 300),    # A (level 4)
        (1, 1953, 1, 221, 300),    # orphaned
        (1, 1952, 1, 221, 300),    # orphaned
    ]
    conn.executemany("INSERT INTO batting_stats VALUES (?,?,?,?,?)", rows)
    conn.commit()
    return conn


def _patch(monkeypatch, tmp_path, conn, milb_league_map=None):
    settings = {
        "minor_leagues": [
            {"lid": 225, "name": "MiLB Triple A", "level": 2},
            {"lid": 207, "name": "South Atlantic League", "level": 4},
        ],
    }
    if milb_league_map is not None:
        settings["milb_league_map"] = milb_league_map
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    (cfg_dir / "league_settings.json").write_text(json.dumps(settings))
    cfg = SimpleNamespace(league_dir=tmp_path, level_map={"1": "MLB", "2": "AAA",
                          "4": "A"}, year=1954)
    monkeypatch.setattr(percentiles, "get_db", lambda: conn)
    monkeypatch.setattr(percentiles, "get_cfg", lambda: cfg)
    # _milb_league_map delegates to the shared accessor in web_league_context.
    import web_league_context
    monkeypatch.setattr(web_league_context, "get_cfg", lambda: cfg)


def test_orphaned_league_grouped_into_milb_bucket(monkeypatch, tmp_path):
    conn = _db()
    _patch(monkeypatch, tmp_path, conn)
    levels = percentiles.available_pctile_levels(1, is_pitcher=False)
    labels = {lbl for _, lbl in levels}
    assert "MLB" in labels
    assert "AAA" in labels
    assert "A" in labels
    # Orphaned league 221 must surface as the synthetic "MiLB" bucket.
    assert "MiLB" in labels
    # Ordering: MLB first, real levels ascending, unknown MiLB last.
    assert levels[0][1] == "MLB"
    assert levels[-1][1] == "MiLB"


def test_no_percentile_years_dropped(monkeypatch, tmp_path):
    conn = _db()
    _patch(monkeypatch, tmp_path, conn)
    all_years = set()
    for lv, _ in percentiles.available_pctile_levels(1, is_pitcher=False):
        all_years |= set(percentiles.available_pctile_years(1, is_pitcher=False, level=lv))
    # Every year with data appears — including 1952/1953 from the orphaned league.
    assert all_years == {1949, 1950, 1952, 1953, 1954}


def test_unknown_bucket_league_ids_from_db(monkeypatch, tmp_path):
    conn = _db()
    _patch(monkeypatch, tmp_path, conn)
    unknown = percentiles._get_level_league_ids(percentiles.UNKNOWN_MILB_LEVEL)
    assert set(unknown) == {221}  # only the orphaned league, not 225/207


def test_cumulative_map_overlays_snapshot(monkeypatch, tmp_path):
    # A league promoted since it was first seen: cumulative map is authoritative.
    conn = _db()
    _patch(monkeypatch, tmp_path, conn,
           milb_league_map={"221": {"name": "Southern League", "level": 3}})
    unknown = percentiles._get_level_league_ids(percentiles.UNKNOWN_MILB_LEVEL)
    assert 221 not in unknown  # now resolved to AA (level 3)
    aa = percentiles._get_level_league_ids(3)
    assert 221 in aa
