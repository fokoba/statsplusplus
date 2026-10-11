"""Tests for the hybrid runs->composite mapping (decompression fix).

Covers facet_runs.calibrate_hybrid_mapping + runs_to_composite's hybrid branch:
per-position centering, WAR-anchored slope, soft top saturation, display floor,
and backward compatibility with the legacy affine mapping.
"""

from statsplusplus.evaluation import facet_runs as fr
from statsplusplus.utils.positions import POSITIONAL_WAR_ADJUSTMENTS


def _mapping(ppw=4.0, rpw=9.44):
    return fr.calibrate_hybrid_mapping(
        pos_run_means={"CF": -0.2, "1B": -9.5, "SS": 0.4, "C": 2.6},
        global_run_mean=0.0,
        runs_per_win=rpw,
        pts_per_war=ppw,
    )


def test_hybrid_mapping_shape():
    m = _mapping()
    assert m["pts_per_war"] == 4.0
    assert abs(m["slope"] - 4.0 / 9.44) < 1e-9
    assert m["pos_centers"]["CF"] == -0.2
    assert m["cap"] == 80.0 and m["floor"] == 20.0


def test_position_average_player_maps_to_center_plus_posadj():
    """A player at his bucket's population run-mean -> 50 + positional credit."""
    m = _mapping()
    for bk in ("CF", "1B", "SS", "C"):
        center = m["pos_centers"][bk]
        expected = 50.0 + POSITIONAL_WAR_ADJUSTMENTS[bk] * m["pts_per_war"]
        got = fr.runs_to_composite(center, m, bk)
        assert abs(got - round(expected)) <= 1, (bk, got, expected)


def test_decompression_one_war_is_about_four_points():
    """A ~1 WAR (≈9.44 run) gap renders as ~4 composite points (not ~2)."""
    m = _mapping(ppw=4.0, rpw=9.44)
    lo = fr.runs_to_composite(0.0, m, "CF")
    hi = fr.runs_to_composite(9.44, m, "CF")
    assert 3 <= (hi - lo) <= 5


def test_dunn_rosner_gap_widens():
    """The motivating case: a strong bat (13.7 runs) clearly above a weak bat
    (4.0 runs) at the same position, not a 1-point tie."""
    m = _mapping()
    dunn = fr.runs_to_composite(13.7, m, "CF")
    rosner = fr.runs_to_composite(4.0, m, "CF")
    assert dunn - rosner >= 3


def test_top_tail_soft_saturates_below_cap():
    """Extreme run totals asymptote toward 80 without a hard clip pile-up."""
    m = _mapping()
    v80 = fr.runs_to_composite(80.0, m, "CF")
    v120 = fr.runs_to_composite(120.0, m, "CF")
    assert v80 <= 80 and v120 <= 80
    # strictly increasing region before the asymptote; both near but under cap
    assert v120 >= v80
    assert v80 >= 76  # genuinely high, not clipped down


def test_bottom_clamps_to_floor_for_display():
    """Deeply sub-replacement run totals clamp to the display floor (20)."""
    m = _mapping()
    assert fr.runs_to_composite(-100.0, m, "1B") == 20
    assert fr.runs_to_composite(-60.0, m, "CF") >= 20


def test_legacy_affine_mapping_still_works():
    """Backward compat: a mapping without 'slope' uses the old z-score affine."""
    legacy = {"runs_mean": 0.0, "runs_sd": 16.0, "comp_mean": 50.0, "comp_sd": 4.0}
    v = fr.runs_to_composite(16.0, legacy)  # +1 sd -> +4 composite
    assert v == 54


def test_unknown_bucket_falls_back_to_global_center():
    m = _mapping()
    # a bucket not in pos_centers uses global_center (0.0) and no positional adj
    v = fr.runs_to_composite(0.0, m, "DH")
    expected = 50.0 + POSITIONAL_WAR_ADJUSTMENTS.get("DH", 0.0) * m["pts_per_war"]
    assert abs(v - round(expected)) <= 1
