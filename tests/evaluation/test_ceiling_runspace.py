"""Tests for run-space ceiling consistency (spec: run-space-facet-model).

Regression guards for bugs found in Session 92:
- a maxed player (potential == current) must not show phantom ceiling upside
  when the composite is on the run-space spine;
- ceiling must never fall below composite.
"""
from statsplusplus.evaluation.ceiling import compute_ceiling, compute_true_ceiling


# Minimal run-space calibration: identity-ish tool->wOBA fit + a comp mapping
# that returns a mid-scale composite, so we can assert relative behavior.
_RUN_SPACE = {
    "lg_woba": 0.320,
    "woba_scale": 1.28,
    "tool_woba_fit": [0.0, 0.004, 0.001, 0.002, 0.001],  # intercept + con/gap/pow/eye
    "br_curve": {"intercept": -10.0, "slope": 0.2},
    "def_curve": {"CF": {"intercept": -30.0, "slope": 0.5, "clamp_lo": -20, "clamp_hi": 20}},
    "anchor": {"runs_per_win": 9.0, "replacement_runs": 18.0},
    "comp_mapping": {"runs_mean": 0.0, "runs_sd": 20.0, "comp_mean": 50.0, "comp_sd": 10.0},
}


def test_maxed_player_ceiling_equals_composite():
    """A fully-developed player (potential == current) gets ceiling ~= composite
    under the run-space spine — no phantom grade-space upside."""
    from statsplusplus.evaluation.composite import compute_composite_hitter
    tools = {"contact": 65, "gap": 60, "power": 60, "eye": 65,
             "speed": 55, "steal": 55, "stl_rt": 55}
    defense = {"OFR": 60, "CF": 60}
    # Composite from the same run-space spine (no observed blend), so ceiling
    # (potential==current tools) should equal it up to age-blend rounding.
    composite = compute_composite_hitter(tools, {}, defense, {}, None,
                                         run_space=_RUN_SPACE, bucket="CF",
                                         positional_models={})
    ceil = compute_ceiling(tools, {}, composite, defense=defense, def_weights={},
                           age=30, run_space=_RUN_SPACE, bucket="CF",
                           positional_models={})
    # maxed: ceiling must equal composite (potential == current, same spine)
    assert abs(ceil - composite) <= 1


def test_true_ceiling_never_below_composite():
    """compute_true_ceiling floors at composite even if the run-space raw is lower
    (e.g. an over-performer whose blended composite exceeds the tool projection)."""
    tools = {"contact": 45, "gap": 45, "power": 45, "eye": 45,
             "speed": 45, "steal": 45, "stl_rt": 45}
    defense = {"OFR": 45, "CF": 45}
    composite = 62  # blended-up composite exceeds the modest tool projection
    tc = compute_true_ceiling(tools, {}, composite, defense=defense, def_weights={},
                              run_space=_RUN_SPACE, bucket="CF", positional_models={})
    assert tc >= composite


def test_run_space_ceiling_grade_space_fallback():
    """Without run_space calibration, ceiling still returns a valid 20-80 int."""
    tools = {"contact": 55, "gap": 50, "power": 55, "eye": 50,
             "speed": 50, "steal": 50, "stl_rt": 50}
    ceil = compute_ceiling(tools, {"contact": 0.3, "gap": 0.2, "power": 0.3, "eye": 0.2},
                           50, age=22)
    assert 20 <= ceil <= 80
