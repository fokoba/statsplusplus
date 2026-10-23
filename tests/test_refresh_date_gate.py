"""tests/test_refresh_date_gate.py — the /date-gate helper in refresh.

Verifies _stored_game_date reads the active league's state.json game date,
which drives the refresh short-circuit (skip a full pull when the game date
hasn't advanced, avoiding redundant rate-limited /ratings requests).
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from statsplusplus.data import refresh


def test_stored_game_date_reads_state(tmp_path, monkeypatch):
    league_dir = tmp_path / "lg"
    (league_dir / "config").mkdir(parents=True)
    (league_dir / "config" / "state.json").write_text(
        json.dumps({"game_date": "2034-04-10", "year": 2034}))
    monkeypatch.setattr(refresh, "get_league_dir", lambda: league_dir)
    assert refresh._stored_game_date() == "2034-04-10"


def test_stored_game_date_missing_returns_empty(tmp_path, monkeypatch):
    league_dir = tmp_path / "lg"
    (league_dir / "config").mkdir(parents=True)
    monkeypatch.setattr(refresh, "get_league_dir", lambda: league_dir)
    assert refresh._stored_game_date() == ""


def test_stored_game_date_bad_json_returns_empty(tmp_path, monkeypatch):
    league_dir = tmp_path / "lg"
    (league_dir / "config").mkdir(parents=True)
    (league_dir / "config" / "state.json").write_text("{not json")
    monkeypatch.setattr(refresh, "get_league_dir", lambda: league_dir)
    assert refresh._stored_game_date() == ""


# ---------------------------------------------------------------------------
# Offseason game-history render skip (_should_skip_game_history)
# ---------------------------------------------------------------------------
import sqlite3


def _mini_conn():
    """Minimal in-memory DB with just the two tables the predicate reads."""
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE team_batting_stats (tid INT, year INT, split_id INT)")
    conn.execute("CREATE TABLE games (game_id INT)")
    return conn


def test_skip_game_history_offseason_with_stored_history():
    """Offseason: no current-year team stats but games already stored → skip the
    paced render and reuse stored history."""
    conn = _mini_conn()
    conn.execute("INSERT INTO team_batting_stats VALUES (1, 2050, 1)")  # prior year
    conn.execute("INSERT INTO games VALUES (100)")
    assert refresh._should_skip_game_history(conn, 2051) is True


def test_no_skip_in_season():
    """In-season: current-year team stats exist → always render game history."""
    conn = _mini_conn()
    conn.execute("INSERT INTO team_batting_stats VALUES (1, 2051, 1)")  # current year
    conn.execute("INSERT INTO games VALUES (100)")
    assert refresh._should_skip_game_history(conn, 2051) is False


def test_no_skip_first_refresh_empty_games():
    """First refresh: games table empty → never skip (history must be fetched)."""
    conn = _mini_conn()
    # no current-year team stats, no games yet
    assert refresh._should_skip_game_history(conn, 2051) is False


# ---------------------------------------------------------------------------
# Per-league refresh lock (acquire/release with stale reclaim)
# ---------------------------------------------------------------------------
import os


def test_lock_acquire_then_contention(tmp_path, monkeypatch):
    """A second acquire while a live lock is held is refused."""
    lg = tmp_path / "lg"
    lg.mkdir()
    ok, _ = refresh.acquire_refresh_lock(lg)
    assert ok
    # Simulate a different live process already holding it: our own PID is alive,
    # so a re-acquire (same fresh lock, alive PID) must refuse.
    ok2, detail = refresh.acquire_refresh_lock(lg)
    assert ok2 is False
    assert "already running" in detail


def test_lock_release_is_pid_scoped(tmp_path):
    """release only removes a lock we own; a foreign-PID lock is left intact."""
    lg = tmp_path / "lg"
    lg.mkdir()
    # Plant a foreign, LIVE-looking lock (use our PID so it isn't reclaimed, but
    # pretend release is called from a different process by rewriting the pid).
    import json as _json
    from datetime import datetime, timezone
    (lg / ".refresh.lock").write_text(_json.dumps({
        "pid": os.getpid() + 1,  # not us
        "started_at": datetime.now(timezone.utc).isoformat(),
    }))
    refresh.release_refresh_lock(lg)
    assert (lg / ".refresh.lock").exists()  # foreign lock untouched


def test_lock_reclaims_dead_pid(tmp_path):
    """A lock held by a dead PID is reclaimed on acquire."""
    lg = tmp_path / "lg"
    lg.mkdir()
    import json as _json
    from datetime import datetime, timezone
    # PID 2^31-1 is effectively never a live process.
    (lg / ".refresh.lock").write_text(_json.dumps({
        "pid": 2_147_483_646,
        "started_at": datetime.now(timezone.utc).isoformat(),
    }))
    ok, _ = refresh.acquire_refresh_lock(lg)
    assert ok  # dead holder → reclaimed
    held = _json.loads((lg / ".refresh.lock").read_text())
    assert held["pid"] == os.getpid()


def test_lock_reclaims_stale_age(tmp_path):
    """A lock older than the max-age ceiling is reclaimed even if the PID looks
    alive (guards against a wedged-but-not-dead process)."""
    lg = tmp_path / "lg"
    lg.mkdir()
    import json as _json
    from datetime import datetime, timezone, timedelta
    old = datetime.now(timezone.utc) - timedelta(
        seconds=refresh._REFRESH_LOCK_MAX_AGE_SECS + 120)
    (lg / ".refresh.lock").write_text(_json.dumps({
        "pid": os.getpid(),  # alive, but...
        "started_at": old.isoformat(),  # ...too old
    }))
    ok, _ = refresh.acquire_refresh_lock(lg)
    assert ok  # stale age → reclaimed


def test_lock_force_overrides_live_lock(tmp_path):
    """--force (force=True) overrides even a fresh, live lock."""
    lg = tmp_path / "lg"
    lg.mkdir()
    ok, _ = refresh.acquire_refresh_lock(lg)
    assert ok
    ok2, _ = refresh.acquire_refresh_lock(lg, force=True)
    assert ok2  # forced through


def test_lock_release_then_reacquire(tmp_path):
    """After release, a fresh acquire succeeds (clean lifecycle)."""
    lg = tmp_path / "lg"
    lg.mkdir()
    ok, _ = refresh.acquire_refresh_lock(lg)
    assert ok
    refresh.release_refresh_lock(lg)
    assert not (lg / ".refresh.lock").exists()
    ok2, _ = refresh.acquire_refresh_lock(lg)
    assert ok2


def test_lock_corrupt_file_is_reclaimable(tmp_path):
    """An unreadable/corrupt lock file does not wedge refresh — it is reclaimed."""
    lg = tmp_path / "lg"
    lg.mkdir()
    (lg / ".refresh.lock").write_text("{not valid json")
    ok, _ = refresh.acquire_refresh_lock(lg)
    assert ok
