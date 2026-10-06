"""Team rollup of the league-wide projected end-of-season WAR."""
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "web"))

import pytest

from projected_war_queries import build_team_projection_table


def _p(team, base, floor, ceil, cur=1.0, sal=100, val=200, sur=100):
    return {"team_id": team, "baseline_war": base, "floor_war": floor, "ceiling_war": ceil,
            "current_war": cur, "salary": sal, "baseline_value": val, "surplus": sur}


def test_rollup_sums_and_combines_spread_as_independent_errors():
    pw = {"players": [_p(1, 4.0, 2.0, 6.0), _p(1, 3.0, 2.0, 5.0), _p(2, 1.0, 0.0, 2.0)]}
    rows = {r["team_id"]: r for r in build_team_projection_table(
        pw, standings={1: (30, 10), 2: (10, 30)}, names={1: "A", 2: "B"}, abbrs={1: "A", 2: "B"})}
    a = rows[1]
    assert a["baseline_war"] == 7.0 and a["n"] == 2 and a["current_war"] == 2.0
    # floor offsets 2 and 1 combine as sqrt(2^2 + 1^2), not 3; ceiling offsets 2 and 2 as sqrt(8), not 4
    assert a["floor_war"] == pytest.approx(7.0 - math.sqrt(5), abs=.05)
    assert a["ceiling_war"] == pytest.approx(7.0 + math.sqrt(8), abs=.05)
    assert a["pct"] == pytest.approx(.75) and a["w"] == 30
    assert a["observed_dpw"] == round(200 / 7.0)
    assert a["rank"] == 1 and rows[2]["rank"] == 2          # ranked by baseline WAR


def test_team_without_standings_or_positive_war_is_handled():
    pw = {"players": [_p(3, -0.5, -1.0, 0.0, sal=50)]}
    r = build_team_projection_table(pw)[0]
    assert r["pct"] is None and r["w"] is None
    assert r["observed_dpw"] is None
