"""Front Office pure helpers."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "web"))

import front_office_queries as fo


def test_ols_recovers_a_line():
    f = fo._ols([1, 2, 3, 4], [12, 14, 16, 18])
    assert abs(f["slope"] - 2) < 1e-9 and abs(f["intercept"] - 10) < 1e-9 and abs(f["r2"] - 1) < 1e-9
    assert fo._ols([1, 1, 1], [1, 2, 3]) is None


def test_num_parses_money_strings():
    assert fo._num("$517,701") == 517701
    assert fo._num("-") is None
