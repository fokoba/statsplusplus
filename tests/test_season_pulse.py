"""Season Pulse: schedule-page parsing and record helpers."""
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "web"))

import season_pulse_queries as sp

PAGE = """<title>Philadelphia Athletics Schedule September 1955</title> Schedule September 1955
<td valign="top" class="sdl_home sdl_border_other"><td width="40" align="left" style="font-size:16px; font-weight:bold; color:#999999;">2</td>
<a href="../teams/team_8.html" style="font-size:13px; font-weight:bold;">vs BOS</a></td>
<td valign="top" class="sdl_away sdl_border_other"><td width="40" align="left" style="font-size:16px; font-weight:bold; color:#999999;">3</td>
<a class="sdl_won" href="x">WIN, 3-0</a>
<a href="../teams/team_1.html" style="font-size:13px; font-weight:bold;">@ WAS</a></td>
<td valign="top" class="sdl_home sdl_border_other"><td width="40" align="left" style="font-size:16px; font-weight:bold; color:#999999;">5</td>
<a href="../teams/team_5.html" style="font-size:13px; font-weight:bold;">vs NYY (2)</a></td>
"""


def test_parse_schedule_page_home_away_played_and_doubleheaders():
    rows = sp.parse_schedule_html(PAGE)
    assert rows == [
        (date(1955, 9, 2), 8, True, False),
        (date(1955, 9, 3), 1, False, True),
        (date(1955, 9, 5), 5, True, False),
        (date(1955, 9, 5), 5, True, False),
    ]


def test_pythagorean_and_wl():
    games = [{"rs": 5, "ra": 3}, {"rs": 2, "ra": 4}, {"rs": 1, "ra": 0}]
    s = sp._wl(games)
    assert (s["w"], s["l"], s["diff"]) == (2, 1, 1)
    assert abs(sp._pyth(500, 500) - 0.5) < 1e-12
    assert sp._pyth(600, 400) > 0.6
