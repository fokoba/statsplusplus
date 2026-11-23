import json
import math

from statsplusplus.evaluation.development_risk import predict_development_risk, load_risk_model
from statsplusplus.evaluation.fv import calc_fv_from_dict


def model():
    return {"schema_version": 1, "target": "close_10pct_raw_gap_12m", "milb": {"enabled": False},
            "models": {k: {"base": {"coefficients": [0]*5},
                            "with_milb": {"coefficients": [0]*5+[1]}, "milb_accepted": True}
                       for k in ("H", "P")}}


def test_probability_and_bands_have_explicit_short_horizon_meaning():
    m = model()
    result = predict_development_risk([30]*4, [50]*4, 21, False, m)
    assert result["risk"] == "Medium"
    assert result["progress_probability"] == .5
    m["milb"]["enabled"] = True
    positive = predict_development_risk([30]*4, [50]*4, 21, False, m, 1)
    negative = predict_development_risk([30]*4, [50]*4, 21, False, m, -1)
    assert positive["risk"] == "Low"
    assert negative["risk"] == "High"


def test_rejected_milb_variant_is_not_used():
    m = model()
    m["milb"]["enabled"] = True
    m["models"]["H"]["milb_accepted"] = False
    result = predict_development_risk([30]*4, [50]*4, 21, False, m, 10)
    assert result["basis"] == "base"
    assert result["progress_probability"] == .5


def test_near_realized_does_not_invent_certain_development():
    result = predict_development_risk([50]*4, [52]*4, 24, False, model())
    assert result["risk"] == "Low"
    assert result["progress_probability"] is None
    assert result["basis"] == "near_realized"


def test_outside_training_domain_and_missing_inputs_fall_back():
    for age, tools in [(15, [30]*4), (26, [30]*4), (21, [30, None, 30, 30]), (21, [float("nan")]*4)]:
        assert predict_development_risk(tools, [50]*4, age, False, model()) is None


def test_extreme_predictor_values_are_finite():
    m = model()
    m["models"]["H"]["base"]["coefficients"] = [1e6]*5
    p = predict_development_risk([30]*4, [80]*4, 21, False, m)["progress_probability"]
    assert math.isfinite(p) and 0 < p < 1


def test_risk_changes_without_changing_fv_or_ceiling(tmp_path):
    p = {"Ovr": 35, "Pot": 60, "Age": 21, "_bucket": "COF", "_is_pitcher": False,
         "Cntct": 30, "Gap": 30, "Pow": 30, "Eye": 30,
         "PotCntct": 50, "PotGap": 50, "PotPow": 50, "PotEye": 50}
    grade_before, _ = calc_fv_from_dict(p, scale="20-80", league_dir=tmp_path)
    continuous_before = p["_fv_continuous"]
    (tmp_path / "config").mkdir()
    m = model()
    m["models"]["H"]["base"]["coefficients"][0] = 2
    (tmp_path / "config/prospect_risk_model.json").write_text(json.dumps(m))
    grade_after, risk = calc_fv_from_dict(p, scale="20-80", league_dir=tmp_path)
    assert risk == "Low"
    assert grade_after == grade_before
    assert p["_fv_continuous"] == continuous_before
    assert p["Pot"] == 60


def test_invalid_model_is_not_silently_treated_as_calibrated(tmp_path):
    (tmp_path / "config").mkdir()
    path = tmp_path / "config/prospect_risk_model.json"
    path.write_text('{"schema_version":99}')
    assert load_risk_model(tmp_path) is None
    path.write_text('[]')
    assert load_risk_model(tmp_path) is None
    path.write_text(json.dumps(model()))
    assert load_risk_model(tmp_path)["schema_version"] == 1
