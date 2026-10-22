"""WAR pace for injured players: banked WAR + rate x games he can still play."""
from statsplusplus.evaluation.war import injury_adjusted_pace, war_pace


def test_long_term_injured_hitter_is_not_projected_for_a_full_season():
    # 3.28 WAR in 60 G; out for the rest of the schedule.
    assert war_pace(3.28, 60, "HIT", 154) > 8                       # the old, misleading number
    assert injury_adjusted_pace(3.28, 60, "HIT", 154, 0) == 3.28    # banked only


def test_partial_availability_adds_the_games_he_can_play():
    p = injury_adjusted_pace(3.0, 60, "HIT", 154, 20)               # 3.0 + 0.05/G * 20
    assert p == 4.0


def test_pitcher_uses_remaining_innings_share():
    # SP: 2.0 WAR in 100 IP, 36 games left of 154, all available -> ~200*36/154 IP more
    p = injury_adjusted_pace(2.0, 100, "SP", 154, 36)
    assert 2.0 < p < 3.2
    assert injury_adjusted_pace(2.0, 100, "SP", 154, 0) == 2.0


def test_no_sample_returns_none():
    assert injury_adjusted_pace(1.0, 0, "HIT", 154, 10) is None
