"""League-minimum players in the minors are paid 16.5% of salary."""
import sqlite3

import pytest

from statsplusplus.data.minor_pay import exact_current_salary, minor_min_pay_map


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE players (player_id INTEGER PRIMARY KEY, level TEXT)")
    c.execute("CREATE TABLE contracts (player_id INTEGER, is_major INTEGER, salary_0 REAL)")
    rows = [
        (1, "1", 1, 6800),    # on the MLB roster: full pay
        (2, "2", 1, 6800),    # AAA at the minimum: discounted
        (3, "2", 1, 10000),   # AAA but above the minimum: full pay
        (4, "2", 0, 6800),    # minor-league contract: not in payroll at all
    ]
    for pid, lvl, major, sal in rows:
        c.execute("INSERT INTO players VALUES (?, ?)", (pid, lvl))
        c.execute("INSERT INTO contracts VALUES (?, ?, ?)", (pid, major, sal))
    yield c
    c.close()


def test_only_minor_leaguers_on_the_minimum_are_discounted(conn):
    assert minor_min_pay_map(conn, 6800) == {2: pytest.approx(6800 * 0.165)}


def test_no_league_minimum_means_no_discount(conn):
    assert minor_min_pay_map(conn, 0) == {}


def test_exact_salary_wins_over_export_rounding():
    assert exact_current_salary(24897, 25000) == 24897
    assert exact_current_salary(9500, 10000) == 9500       # exactly $500 off
    assert exact_current_salary(20000, 26000) == 26000     # a real difference
    assert exact_current_salary(20000, None) == 20000
