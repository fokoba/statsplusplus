"""Pay for league-minimum players while they're in the minors.

OOTP only pays a player on a league-minimum major-league deal 16.5% of his
salary while he's in the minors (see the legend on the "Team Salary" export).
The synced contract still carries the full salary, so payroll built straight
from it overstates what the game charges until the player is called up.
"""

from typing import Dict

MINOR_MIN_PAY_PCT = 0.165


def minor_min_pay_map(conn, league_min) -> Dict[int, float]:
    """{player_id: current-year salary actually paid} for every major-contract
    player who is in the minors on a league-minimum deal. Uses the raw synced
    salary_0, since the Team Salary export rounds it (e.g. 6,800 -> 7,000).
    """
    if not league_min:
        return {}
    try:
        rows = conn.execute(
            "SELECT c.player_id, c.salary_0 FROM contracts c "
            "JOIN players p ON p.player_id = c.player_id "
            "WHERE c.is_major = 1 AND CAST(p.level AS INTEGER) != 1 "
            "AND c.salary_0 IS NOT NULL AND c.salary_0 <= ?",
            (league_min,),
        ).fetchall()
    except Exception:
        return {}
    return {int(r[0]): float(r[1]) * MINOR_MIN_PAY_PCT for r in rows if r[1]}


def exact_current_salary(raw, uploaded):
    """The Team Salary export rounds dollar figures to the nearest $1,000
    (a 24,897 contract shows as 25,000). When it's within that rounding of the
    synced current-year contract, the synced figure is the exact one."""
    if uploaded is None:
        return raw
    if raw and abs(float(raw) - float(uploaded)) <= 500:
        return float(raw)
    return uploaded
