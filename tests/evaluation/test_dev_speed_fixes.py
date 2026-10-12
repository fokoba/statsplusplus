"""Dev-speed fixes: scale-drift neutralisation, pitcher stagnation, rookie tags, peer cuts."""
from statsplusplus.evaluation import dev_speed as ds


def _snap(d, comp, stf=40):
    return {"snapshot_date": d, "composite_score": comp, "ceiling_score": comp + 20,
            "offensive_grade": None, "cntct": None, "gap": None, "pow": None, "eye": None,
            "stf": stf, "mov": 40, "ctrl": 40}


def test_neutralize_removes_league_wide_step():
    by = {}
    for pid in range(20):  # tools never change; composite steps -6 for all in Dec
        by[pid] = [_snap("1954-10-01", 40), _snap("1954-11-01", 40), _snap("1954-12-01", 34),
                   _snap("1955-01-01", 34)]
    # one real developer: tools change in Jan, composite +3 on top of the step
    by[99] = [_snap("1954-10-01", 40), _snap("1954-11-01", 40), _snap("1954-12-01", 34),
              _snap("1955-01-01", 37, stf=50)]
    ds.neutralize_scale_drift(by, {pid: ("SP", True) for pid in by})
    assert [s["composite_score"] for s in by[0]] == [34, 34, 34, 34]
    assert by[99][0]["composite_score"] == 34 and by[99][-1]["composite_score"] == 37


def _tag(stagnant, z, bucket="SP"):
    return ds.schedule_tag(z=z, gap=10, gap_first=10, gap_closed_pct_yr=0.2, gap_trend=-2.0,
                           stagnant=stagnant, age=21, bucket=bucket)[0]


def test_two_flat_tools_do_not_flag_a_pitcher():
    assert _tag(["stf", "mov"], 1.8) != "behind"
    assert _tag(["stf", "mov"], 1.8, bucket="SS") == "behind"   # hitters unchanged


def test_three_flat_tools_with_negative_z_still_flags_pitcher():
    assert _tag(["stf", "mov", "ctrl"], -0.8) == "behind"


def _win():
    return [{"snapshot_date": f"1955-0{m}-01", "composite_score": 40, "ceiling_score": 60,
             "stf": 40, "mov": 40, "ctrl": 40} for m in range(1, 8)]


def test_rookie_gets_no_schedule_tag():
    base = ds.build_baseline([{"age": 21, "bucket": "SP", "band": "20-21", "annual_comp": x / 10, "annual_off": None}
                              for x in range(-15, 15)])
    r = ds.compute_dev_speed(bucket="SP", age=21, window=_win(), baseline=base, rookie=True)
    assert r["available"] and r["schedule_status"] is None
    r2 = ds.compute_dev_speed(bucket="SP", age=21, window=_win(), baseline=base)
    assert r2["schedule_status"] is not None


def test_peer_cuts_only_loosen_the_absolute_defaults():
    rows = [{"bucket": "SP", "band": "20-21", "gap_closed_pct_yr": x / 100, "gap_trend": 2 - x / 10}
            for x in range(-30, 30)]
    cut = ds.peer_gap_cuts(rows)[("SP", "20-21")]
    assert cut["behind_closed_cut"] <= ds.BEHIND_GAP_CLOSED_PCT
    assert cut["not_closing_cut"] >= ds.GAP_NOT_CLOSING
