"""Pure evaluation computation — no DB access, no I/O, no side effects.

All functions take typed inputs and return typed outputs. This package
is the computational core of Stats++ player evaluation.

Public API:
    Composite scoring:
        compute_composite_hitter(tools, weights, defense, def_weights) -> int
        compute_composite_pitcher(tools, weights, arsenal, stamina, role) -> int
        compute_tool_only_score(player_type, tools, weights, ...) -> int
        compute_composite_mlb(tool_score, stat_seasons, ...) -> int
        compute_offensive_grade(tools, weights) -> int | None
        compute_baserunning_value(tools, weights) -> int | None
        compute_defensive_value(defense, def_weights) -> int | None

    Ceiling scoring:
        compute_ceiling(potential_tools, weights, composite, ...) -> int
        compute_true_ceiling(potential_tools, weights, composite, ...) -> int

    FV grades:
        calc_fv(ovr, pot, age, bucket, ...) -> tuple[int, RiskLabel]

    WAR projection:
        peak_war_from_score(score, bucket) -> float
        aging_mult(age, bucket) -> float
        stat_peak_war(pid, bucket, bat_hist, pit_hist, ...) -> float | None

    Stat conversion:
        stat_to_2080(stat_plus) -> float
        pitcher_stat_to_2080(stat_plus) -> float
"""
