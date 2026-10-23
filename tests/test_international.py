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


def test_combine_traits_takes_known_values_and_weights_by_accuracy():
    reps = [
        {"acc": "VH", "traits": {"lead": "H", "loy": None, "adaptability": "N", "greed": None, "wrk_ethic": "H", "int_": None}},
        {"acc": "L", "traits": {"lead": "L", "loy": "N", "adaptability": None, "greed": None, "wrk_ethic": "H", "int_": None}},
    ]
    t = iq.combine_traits(reps)
    assert t["lead"] == "H"                    # the more accurate scout wins the split
    assert t["loy"] == "N" and t["adaptability"] == "N"
    assert t["greed"] is None and t["int_"] is None   # nobody knows


def test_parse_marks_unknown_trait_as_none():
    csv_text = "ID,POS,Name,First Name,Last Name,Age,SctAcc,LEA,LOY,AD,FIN,WE,INT,Type\n1,SS,x,A,B,16,High,U,H,N,L,H,U,Unknown\n"
    p = iq.parse_scout_csv(csv_text)[1]
    assert p["traits"] == {"lead": None, "loy": "H", "adaptability": "N", "greed": "L", "wrk_ethic": "H", "int_": None}


def _row(pid, surplus, conf=3, demand=1000, ptype="Unknown"):
    return {"pid": pid, "name": f"P{pid}", "blend": {"surplus": surplus}, "conf": conf, "demand": demand,
            "bids": iq.bids_for(surplus), "personality": {"type": ptype, "type_class": "neg" if "Selfish" in ptype else "unknown",
                                                           "concerns": [], "buffs": []}}


def test_mixed_plan_excludes_types_and_fits_budget():
    rows = [_row(1, 400_000, conf=4), _row(2, 100_000, ptype="Likely Selfish (Medium confidence)"),
            _row(3, 60_000), _row(4, 20_000, conf=2), _row(5, 3_000, demand=5_000)]
    plan = iq.mixed_plan(rows, cash=250_000, buffer=50_000, exclude_types=["Selfish", "Disruptive"])
    picked = {r["pid"] for r in rows if r["plan_tier"]}
    assert picked == {1, 3, 4}                       # selfish and over-ask players are out
    assert plan["total"] <= 200_000
    assert all(r["plan_bid"] >= r["demand"] for r in rows if r["plan_tier"])
