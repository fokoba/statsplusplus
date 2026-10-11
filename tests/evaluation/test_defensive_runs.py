"""Multi-tool defensive-runs model tests (facet_runs) — catcher + infield.

Guards the Session-97 catcher-defense fix: catcher value is framing + blocking +
arm (NOT ZR/range), so the catcher fielding-runs path must (a) blend the full
catcher profile via calibrated per-tool weights, (b) produce a realistic run
spread (not the ±3 ZR clamp that crushed catcher defense to ~1 run), and (c)
leave the IF/OF range-only path untouched.
"""

from __future__ import annotations

from statsplusplus.evaluation import facet_runs as fr


def _mk_catcher_rows(n: int = 60):
    """Synthetic catcher-seasons where observed runs track framing+blocking."""
    rows, obs = [], []
    for i in range(n):
        frm = 30 + (i % 50)          # 30..79
        blk = 35 + (i % 40)
        arm = 40 + (i % 30)
        rows.append({"c_frm": frm, "c_blk": blk, "c_arm": arm})
        # runs driven mostly by framing+blocking, small noise, arm weak
        obs.append(0.25 * (frm - 50) + 0.20 * (blk - 50) + 0.03 * (arm - 50)
                   + (1.5 if i % 7 == 0 else -0.5))
    return rows, obs


def test_catcher_curve_weights_framing_and_blocking_over_arm():
    rows, obs = _mk_catcher_rows()
    curve = fr.calibrate_multitool_curve(fr.CATCHER_DEF_TOOLS, rows, obs)
    assert curve is not None
    assert curve.get("multitool") is True
    w = curve["weights"]
    # framing + blocking should carry the weight; arm should be the smallest.
    assert w["c_frm"] > w["c_arm"]
    assert w["c_blk"] > w["c_arm"]
    assert abs(sum(w.values()) - 1.0) < 1e-6


def test_catcher_fielding_runs_spread_exceeds_zr_clamp():
    """An elite catcher must earn meaningfully more fielding runs than a poor one
    — a spread wider than the old ±3.3 ZR clamp allowed."""
    rows, obs = _mk_catcher_rows()
    curve = fr.calibrate_multitool_curve(fr.CATCHER_DEF_TOOLS, rows, obs)
    def_curve = {"C": curve}
    elite = {"c_frm": 75, "c_blk": 70, "c_arm": 65}
    poor = {"c_frm": 35, "c_blk": 40, "c_arm": 40}
    r_elite = fr.fielding_runs(elite, "C", def_curve, None)
    r_poor = fr.fielding_runs(poor, "C", def_curve, None)
    assert r_elite > r_poor
    assert (r_elite - r_poor) > 3.3, "catcher defensive spread should exceed the old ZR clamp"


def test_catcher_uses_full_profile_not_arm_alone():
    """Two catchers with identical arm but very different framing/blocking must
    get different fielding runs (the old arm-only path gave them the same)."""
    rows, obs = _mk_catcher_rows()
    curve = fr.calibrate_multitool_curve(fr.CATCHER_DEF_TOOLS, rows, obs)
    def_curve = {"C": curve}
    good_frame = {"c_frm": 75, "c_blk": 70, "c_arm": 50}
    bad_frame = {"c_frm": 40, "c_blk": 40, "c_arm": 50}
    assert fr.fielding_runs(good_frame, "C", def_curve, None) > \
           fr.fielding_runs(bad_frame, "C", def_curve, None)


def test_catcher_tool_key_casing_both_accepted():
    """The engine passes CamelCase (CFrm/CBlk/CArm); calibration uses lowercase.
    Both must resolve to the same blended grade."""
    w = {"c_frm": 0.4, "c_blk": 0.4, "c_arm": 0.2}
    lower = fr._blended_def_grade({"c_frm": 70, "c_blk": 60, "c_arm": 50}, w)
    camel = fr._blended_def_grade({"CFrm": 70, "CBlk": 60, "CArm": 50}, w)
    assert lower == camel is not None


def test_if_of_path_unchanged_by_catcher_branch():
    """A non-multitool curve (no 'weights') must still use the range-grade path."""
    def_curve = {"SS": {"intercept": -25.0, "slope": 0.5, "clamp_lo": -8.0, "clamp_hi": 8.0}}
    runs = fr.fielding_runs({"SS": 60, "ifr": 60}, "SS", def_curve, None)
    # 0.5*60 - 25 = 5.0, within clamp
    assert abs(runs - 5.0) < 1e-6


def _mk_infield_rows(n: int = 80, kind: str = "range"):
    """Synthetic infield-seasons. kind='range' -> runs track range; kind='3b'
    -> runs track error+arm more than range (the 3B pattern)."""
    rows, obs = [], []
    for i in range(n):
        ifr = 30 + (i % 50)
        ife = 35 + ((i * 3) % 40)
        ifa = 40 + ((i * 7) % 35)
        tdp = 40 + ((i * 5) % 30)
        rows.append({"ifr": ifr, "ife": ife, "ifa": ifa, "tdp": tdp})
        noise = 1.0 if i % 6 == 0 else -0.4
        if kind == "range":
            obs.append(0.4 * (ifr - 50) + 0.1 * (ife - 50) + noise)
        else:  # 3b: error + arm lead
            obs.append(0.1 * (ifr - 50) + 0.3 * (ife - 50) + 0.3 * (ifa - 50) + noise)
    return rows, obs


def test_infield_multitool_weights_track_signal():
    """Range-led data -> range top weight; error/arm-led data -> those lead."""
    rows, obs = _mk_infield_rows(kind="range")
    c = fr.calibrate_multitool_curve(fr.INFIELD_DEF_TOOLS, rows, obs)
    assert c and c["weights"]["ifr"] == max(c["weights"].values())

    rows2, obs2 = _mk_infield_rows(kind="3b")
    c2 = fr.calibrate_multitool_curve(fr.INFIELD_DEF_TOOLS, rows2, obs2)
    # error + arm should each outweigh range in the 3B-style data
    assert c2["weights"]["ife"] > c2["weights"]["ifr"]
    assert c2["weights"]["ifa"] > c2["weights"]["ifr"]


def test_prior_shrinkage_protects_thin_samples():
    """A tiny, noisy sample should lean on the prior rather than fit noise."""
    rows, obs = _mk_infield_rows(n=10, kind="3b")  # below/near min_n, noisy
    prior = {"ifr": 0.42, "ife": 0.24, "ifa": 0.18, "tdp": 0.16}
    c = fr.calibrate_multitool_curve(fr.INFIELD_DEF_TOOLS, rows, obs,
                                     weight_prior=prior, prior_strength=40.0, min_n=8)
    if c:  # with n<min_n it returns None; with n>=min_n it should be near the prior
        # heavy prior weight (alpha = 10/50 = 0.2) -> range still the top weight
        assert c["weights"]["ifr"] == max(c["weights"].values())


def test_infield_curve_routes_through_blend():
    """A fitted infield curve with weights must use the multi-tool blend in
    fielding_runs (two IFs with equal range but different error/arm differ)."""
    rows, obs = _mk_infield_rows(kind="3b")
    curve = fr.calibrate_multitool_curve(fr.INFIELD_DEF_TOOLS, rows, obs)
    def_curve = {"3B": curve}
    good = {"ifr": 55, "ife": 75, "ifa": 70, "tdp": 55}
    bad = {"ifr": 55, "ife": 40, "ifa": 40, "tdp": 55}
    assert fr.fielding_runs(good, "3B", def_curve, None) > \
           fr.fielding_runs(bad, "3B", def_curve, None)


def _mk_outfield_rows(n: int = 80):
    """Synthetic OF-seasons where runs track range + error + arm (arm = the OF
    throwing-value dimension OOTP's `arm` column measures)."""
    rows, obs = [], []
    for i in range(n):
        ofr = 30 + (i % 50)
        ofe = 35 + ((i * 3) % 40)
        ofa = 40 + ((i * 7) % 35)
        rows.append({"ofr": ofr, "ofe": ofe, "ofa": ofa})
        noise = 1.0 if i % 6 == 0 else -0.4
        obs.append(0.45 * (ofr - 50) + 0.2 * (ofe - 50) + 0.12 * (ofa - 50) + noise)
    return rows, obs


def test_outfield_multitool_includes_arm():
    """OF blend fits range-led weights and gives arm non-zero weight (arm value
    comes from the arm-runs component of the target, not ZR)."""
    rows, obs = _mk_outfield_rows()
    c = fr.calibrate_multitool_curve(fr.OUTFIELD_DEF_TOOLS, rows, obs)
    assert c and c["weights"]["ofr"] == max(c["weights"].values())
    assert c["weights"]["ofa"] > 0.0


def test_outfield_arm_separates_equal_range_corners():
    """Two corner OF with equal range/error but different arm must differ in
    fielding runs (the RF-cannon case)."""
    rows, obs = _mk_outfield_rows()
    def_curve = {"COF": fr.calibrate_multitool_curve(fr.OUTFIELD_DEF_TOOLS, rows, obs)}
    cannon = {"ofr": 60, "ofe": 60, "ofa": 80}
    weak = {"ofr": 60, "ofe": 60, "ofa": 40}
    assert fr.fielding_runs(cannon, "COF", def_curve, None) > \
           fr.fielding_runs(weak, "COF", def_curve, None)


