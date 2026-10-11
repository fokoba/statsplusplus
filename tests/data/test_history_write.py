"""Pipeline-invariant regression test for the engine's ratings_history write.

Guards the Session-97 dev-history bug (header 58/58 vs timeline 52/54): the
evaluation engine recomputes the current composite and writes it back to
``ratings_history``, but keyed the UPDATE on the ``ratings`` MAX snapshot date.
``ratings_history`` snapshots are taken once per in-game MONTH (dated to the
first refresh of the month) while ``ratings`` advances to the latest game date,
so after any mid-month second refresh the two dates diverge and the UPDATE
matched ZERO rows — a silent no-op that froze the newest dev-timeline point on a
stale (and, after a model change, off-scale) composite.

This is a cross-module, time-dependent, silent-no-op: no unit test can see it
(it lives in the write path, not a pure function) and a single-pass fixture run
misses it (dates coincide on first build). It only reproduces on a SECOND
refresh within the same in-game month, which is what this test simulates.

Invariant asserted: after the engine runs, a player's LATEST ratings_history
composite equals the current latest_ratings composite (the player-page header).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # tests/ for _fixture_league

from _fixture_league import build_fixture, remove_fixture  # noqa: E402
from statsplusplus.data import evaluation_engine  # noqa: E402
from statsplusplus.data.db import get_connection  # noqa: E402

_SLUG = "_history_write_test"
_PID = 100  # fixture MLB hitter (Joe Hitter)
_MONTH_START = "2033-04-02"   # ratings_history snapshot (first refresh of the month)
_LATER_SAME_MONTH = "2033-04-20"  # ratings advances here on a 2nd refresh
_STALE = 99  # an obviously-wrong composite planted in the month-start history row


@pytest.fixture()
def league_dir():
    ld = build_fixture(_SLUG, with_ovr=True)
    try:
        yield ld
    finally:
        remove_fixture(_SLUG)


def _setup_mid_month_second_refresh(ld: Path) -> None:
    """Plant a month-start history row with a stale composite, then advance the
    ratings snapshot to a later date in the SAME month (the 2nd-refresh state
    that makes ratings.MAX != ratings_history.MAX)."""
    conn = get_connection(ld)
    conn.execute(
        "INSERT OR IGNORE INTO ratings_history "
        "(player_id, snapshot_date, composite_score, ceiling_score, ovr, pot) "
        "VALUES (?, ?, ?, ?, 55, 60)",
        (_PID, _MONTH_START, _STALE, _STALE),
    )
    conn.execute(
        "UPDATE ratings SET snapshot_date=? WHERE player_id=?",
        (_LATER_SAME_MONTH, _PID),
    )
    conn.commit()
    conn.close()


def test_engine_history_write_lands_on_latest_snapshot(league_dir):
    """The newest ratings_history composite must equal the current header value
    after the engine runs, even when ratings has advanced past the month-start
    history row (the dead-write condition)."""
    _setup_mid_month_second_refresh(league_dir)

    # Precondition: the dates diverge (the exact condition that broke the write).
    conn = get_connection(league_dir)
    r_max = conn.execute("SELECT MAX(snapshot_date) FROM ratings").fetchone()[0]
    h_max = conn.execute(
        "SELECT MAX(snapshot_date) FROM ratings_history WHERE player_id=?", (_PID,)
    ).fetchone()[0]
    conn.close()
    assert r_max != h_max, "test setup must create the mid-month date divergence"

    evaluation_engine.run(league_dir=league_dir)

    conn = get_connection(league_dir)
    header = conn.execute(
        "SELECT composite_score FROM latest_ratings WHERE player_id=?", (_PID,)
    ).fetchone()[0]
    latest_hist = conn.execute(
        "SELECT composite_score FROM ratings_history WHERE player_id=? "
        "ORDER BY snapshot_date DESC LIMIT 1", (_PID,)
    ).fetchone()[0]
    conn.close()

    # The write must have landed on the month-start row (not matched zero rows).
    assert latest_hist != _STALE, (
        "ratings_history composite is still the stale planted value — the engine's "
        "history write matched zero rows (the dead-write regression)"
    )
    # And the newest timeline point must equal the player-page header.
    assert latest_hist == header, (
        f"newest ratings_history composite ({latest_hist}) != latest_ratings "
        f"composite ({header}); dev-timeline would disagree with the header"
    )
