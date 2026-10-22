"""Trade Center pure helpers: log5, series odds, league detection."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "web"))

import trade_center_queries as tc


def test_log5_symmetry_and_edges():
    assert tc._log5(0.5, 0.5) == 0.5
    assert abs(tc._log5(0.6, 0.4) + tc._log5(0.4, 0.6) - 1) < 1e-12
    assert abs(tc._log5(0.6, 0.5) - 0.6) < 1e-12


def test_series_odds():
    assert abs(tc._series_win_prob(0.5) - 0.5) < 1e-12
    assert tc._series_win_prob(0.6) > 0.6          # a best-of-7 amplifies an edge


def test_leagues_from_schedule_graph():
    games = [(1, 2, 0, 0), (2, 3, 0, 0), (4, 5, 0, 0)]
    assert tc._leagues(games, [1, 2, 3, 4, 5]) == [[1, 2, 3], [4, 5]]


def test_status_buckets():
    assert tc._status(0.5)[1] == "buyer"
    assert tc._status(0.1)[1] == "bubble"
    assert tc._status(0.0)[1] == "seller"
