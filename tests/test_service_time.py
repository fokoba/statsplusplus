"""
tests/test_service_time.py — Service-time interpretation (single source of truth).

Covers arb.service_time() and the estimate_control() completed-years logic.

Grounded in the confirmed field semantics: the StatsPlus API's `mlb_service_days`
is the CUMULATIVE total days of MLB service (full year = 172 days), so
completed_years = days // 172. Only completed years reduce team control.
"""
import sqlite3

import pytest

from statsplusplus.evaluation.arb import service_time, estimate_control
from statsplusplus.evaluation.constants import SERVICE_DAYS_PER_YEAR

MIN_SAL = 825_000


def _db():
    """Minimal in-memory players table for service-time reads."""
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE players (player_id INTEGER PRIMARY KEY, mlb_service_days, "
        "has_received_arbitration INTEGER)"
    )
    # No stat tables — service_time falls back to games only when days is absent,
    # and the games query would fail without them; we only exercise the fallback
    # path for a player row we deliberately leave with NULL days after creating
    # the stat tables.
    conn.execute("CREATE TABLE mlb_batting_stats (player_id, year, g, split_id)")
    conn.execute("CREATE TABLE mlb_pitching_stats (player_id, year, g, gs, split_id)")
    return conn


def _add(conn, pid, days):
    conn.execute(
        "INSERT INTO players (player_id, mlb_service_days) VALUES (?, ?)", (pid, days)
    )


# ---------------------------------------------------------------------------
# Field semantics: cumulative days -> years / completed_years / display
# ---------------------------------------------------------------------------

def test_cumulative_days_to_completed_years():
    conn = _db()
    _add(conn, 1, 3183)  # ~18-year veteran
    st = service_time(conn, 1)
    assert st.total_days == 3183
    assert st.completed_years == 18
    assert 3183 // SERVICE_DAYS_PER_YEAR == 18
    assert round(st.years, 2) == round(3183 / 172, 2)
    assert st.exact is True


def test_display_is_years_dot_remainder_days():
    conn = _db()
    _add(conn, 1, 4 * 172 + 70)  # exactly 4 years, 70 days
    st = service_time(conn, 1)
    assert st.completed_years == 4
    assert st.remainder_days == 70
    assert st.display() == "4.070"


def test_zero_service():
    conn = _db()
    _add(conn, 1, 0)
    st = service_time(conn, 1)
    assert st.completed_years == 0 and st.years == 0.0 and st.display() == "0.000"


# ---------------------------------------------------------------------------
# Type coercion: the column is stored as empty text for non-MLB players
# ---------------------------------------------------------------------------

def test_empty_string_days_treated_as_no_service():
    conn = _db()
    _add(conn, 1, "")  # MiLB player: empty text
    st = service_time(conn, 1)
    # No MLB data -> games fallback (no stat rows) -> 0
    assert st.completed_years == 0
    assert st.exact is False


def test_null_days_uses_games_fallback():
    conn = _db()
    _add(conn, 1, None)
    st = service_time(conn, 1)
    assert st.exact is False
    assert st.completed_years == 0  # no game rows


# ---------------------------------------------------------------------------
# Free-agency threshold: 6 COMPLETED years
# ---------------------------------------------------------------------------

def test_fa_eligibility_needs_completed_years():
    conn = _db()
    _add(conn, 1, 5 * 172 + 150)  # 5.87 fractional, only 5 completed
    st = service_time(conn, 1)
    assert st.completed_years == 5
    assert st.is_free_agent_eligible is False  # still one control year left

    _add(conn, 2, 6 * 172)  # exactly 6 completed
    assert service_time(conn, 2).is_free_agent_eligible is True


# ---------------------------------------------------------------------------
# estimate_control: the bug case — 4 years + partial year = 2 control years
# ---------------------------------------------------------------------------

def test_control_counts_only_completed_years():
    """4 years 150 days (4.87 svc): completed=4 -> 6-4 = 2 control years.

    The old math.ceil(4.87)=5 gave 6-5 = 1, understating control by a year.
    """
    conn = _db()
    _add(conn, 1, 4 * 172 + 150)
    remaining, sched, pre_arb = estimate_control(
        conn, 1, age=27, salary=3_000_000, min_sal=MIN_SAL
    )
    assert remaining == 2
    assert len(sched) == 2
    assert pre_arb == 0  # arb-eligible (salary above min)


def test_control_free_agent_returns_none():
    conn = _db()
    _add(conn, 1, 6 * 172 + 40)  # 6 completed years, above min salary
    # 6+ completed years at age 30+ -> free agent
    remaining, sched, pre_arb = estimate_control(
        conn, 1, age=31, salary=3_000_000, min_sal=MIN_SAL
    )
    assert remaining is None and sched is None


def test_pre_arb_player_control():
    conn = _db()
    _add(conn, 1, 1 * 172 + 30)  # 1 completed year, min salary
    remaining, sched, pre_arb = estimate_control(
        conn, 1, age=24, salary=MIN_SAL, min_sal=MIN_SAL
    )
    assert remaining == 5  # 6 - 1
    assert pre_arb == 2    # 3 - 1
