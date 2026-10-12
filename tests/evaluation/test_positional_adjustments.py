import json
from pathlib import Path

from statsplusplus.evaluation.constants import load_model_weights
from statsplusplus.utils.positions import POSITIONAL_WAR_ADJUSTMENTS, apply_positional_adjustments


def _league(tmp_path, raw):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "model_weights.json").write_text(json.dumps(raw))
    return tmp_path


def test_derived_adjustments_apply_and_reset(tmp_path):
    try:
        load_model_weights(_league(tmp_path, {"POSITIONAL_WAR_ADJ_DERIVED": {"C": 0.36, "1B": -0.7}}))
        assert POSITIONAL_WAR_ADJUSTMENTS["C"] == 0.36 and POSITIONAL_WAR_ADJUSTMENTS["1B"] == -0.7
        assert POSITIONAL_WAR_ADJUSTMENTS["SS"] == 0.7        # untouched keeps default
        load_model_weights(Path(tmp_path / "nowhere"))         # no file -> defaults
        assert POSITIONAL_WAR_ADJUSTMENTS["C"] == 1.2
    finally:
        apply_positional_adjustments(None)


def test_model_params_override_beats_derived(tmp_path):
    try:
        load_model_weights(_league(tmp_path, {
            "POSITIONAL_WAR_ADJ_DERIVED": {"C": 0.36},
            "MODEL_PARAMS": {"POSITIONAL_WAR_ADJ": {"C": 0.9}}}))
        assert POSITIONAL_WAR_ADJUSTMENTS["C"] == 0.9
    finally:
        apply_positional_adjustments(None)
