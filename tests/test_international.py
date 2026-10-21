"""International multi-scout valuation: parsing, blending, confidence, bids."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "web"))

import international_queries as iq


def test_parse_money():
    assert iq.parse_money("$850") == 850
    assert iq.parse_money("$2k") == 2000
    assert iq.parse_money("$1.2M") == 1_200_000
    assert iq.parse_money("-") is None


def test_scout_label_from_filename():
    assert iq.scout_label_from_filename("mlb_x_allcolumns_MIL.csv") == "MIL"


CSV = (
    "ID,POS,Name,First Name,Last Name,Age,SctAcc,WE,INT,DEM,DEM,Sign,CON,GAP,POW,EYE,STU,MOV,CON,CON P,CON P\n"
    "7,CF,A. Test,Al,Test,16,High,N,N,$2k,$2k,Easy,40,45,50,35,20,20,25,60,30\n"
)


def test_parse_scout_csv_uses_second_con_for_control():
    p = iq.parse_scout_csv(CSV)[7]
    assert p["name"] == "Al Test" and p["acc"] == "H" and p["demand"] == 2000
    r = p["ratings"]
    assert r["cntct"] == 40 and r["ctrl"] == 25            # hitting CON vs pitching CON
    assert r["pot_cntct"] == 60 and r["pot_ctrl"] == 30    # CON P (hitting) vs CON P (pitching)


def _rep(acc, **ratings):
    return {"acc": acc, "ratings": ratings}


def test_blend_weights_by_accuracy_and_median_is_middle():
    reps = [_rep("VH", cntct=60), _rep("L", cntct=40), _rep("A", cntct=50)]
    blend = iq.blend_ratings(reps)["cntct"]
    assert 50 < blend < 54          # pulled toward the accurate VH report
    assert iq.median_ratings(reps)["cntct"] == 50


def test_confidence_rewards_agreement_and_penalises_a_split():
    assert iq.confidence_level(["A", "A", "A"], [60, 61, 62], [45, 45, 45]) == 4
    assert iq.confidence_level(["A", "A", "A"], [50, 62, 70], [35, 45, 55]) == 2
    assert iq.confidence_level(["VH", "VH", "VH"], [60, 60, 60], [50, 50, 50]) == 5


def test_bids_are_shares_of_surplus():
    b = iq.bids_for(600_000)
    assert b == {"low": 100_000, "avg": 150_000, "high": 200_000}
    assert iq.bids_for(0) == {"low": 0, "avg": 0, "high": 0}
