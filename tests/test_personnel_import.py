"""import_personnel_sync: two-team cities resolve by abbreviation, and coaches
missing from a fresh export are unassigned."""

import json

from custom_upload import get_conn, import_personnel_sync
from statsplusplus.data.db import init_schema

HEADER = "Job,Name,TM,TM,Lev,Lev,DOB,Type,POS,NEG"


def _league(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "league_settings.json").write_text(json.dumps(
        {"team_abbr": {"5": "NYY", "12": "NYG"}}))
    init_schema(tmp_path)
    conn = get_conn(tmp_path)
    conn.executemany("INSERT INTO teams (team_id, name) VALUES (?, ?)",
                     [(5, "New York"), (12, "New York")])
    conn.commit()
    conn.close()
    return tmp_path


def _csv(*lines):
    return ("\n".join((HEADER,) + lines) + "\n").encode()


def test_same_city_teams_resolve_by_abbr(tmp_path):
    ld = _league(tmp_path)
    import_personnel_sync(_csv(
        "Manager,Arnold Kruse,New York,NYY,Major League,MLB,01/01/1900,Normal,,",
        "Manager,Hector Heras,New York,NYG,Major League,MLB,01/01/1901,Normal,,",
    ), league_dir=ld)
    conn = get_conn(ld)
    got = dict(conn.execute("SELECT name, team_id FROM personnel"))
    assert got == {"Arnold Kruse": 5, "Hector Heras": 12}


def test_coach_missing_from_export_is_unassigned(tmp_path):
    ld = _league(tmp_path)
    import_personnel_sync(_csv(
        "Manager,Arnold Kruse,New York,NYY,Major League,MLB,01/01/1900,Normal,,",
        "Bench Coach,Bill Old,New York,NYY,Major League,MLB,01/01/1890,Normal,,",
    ), league_dir=ld)
    import_personnel_sync(_csv(
        "Manager,Arnold Kruse,New York,NYY,Major League,MLB,01/01/1900,Normal,,",
    ), league_dir=ld)
    conn = get_conn(ld)
    got = dict(conn.execute("SELECT name, team_id FROM personnel"))
    assert got == {"Arnold Kruse": 5, "Bill Old": None}
