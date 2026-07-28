"""
tests/conftest.py — shared fixtures for web layer integration tests.

Provides an in-memory SQLite DB seeded with minimal data and a mock LeagueConfig,
patched into web_league_context so query functions run without a real league on disk.
"""

import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

import pytest

BASE = Path(__file__).parent.parent
sys.path.insert(0, str(BASE / "scripts"))
sys.path.insert(0, str(BASE / "web"))

# ── Schema (subset of db.SCHEMA needed by web queries) ──────────────────────

_SCHEMA = """
CREATE TABLE IF NOT EXISTS teams (
    team_id INTEGER PRIMARY KEY, name TEXT, level TEXT,
    parent_team_id INTEGER, league TEXT
);
CREATE TABLE IF NOT EXISTS players (
    player_id INTEGER PRIMARY KEY, name TEXT, age INTEGER,
    team_id INTEGER, parent_team_id INTEGER, level TEXT,
    pos INTEGER, role INTEGER,
    injury_is_injured INTEGER, injury_dl_left INTEGER, injury_left INTEGER,
    is_on_dl INTEGER, is_on_dl60 INTEGER, dl_days_this_year INTEGER,
    mlb_service_years INTEGER, mlb_service_days INTEGER, mlb_service_days_this_year INTEGER,
    pro_service_years INTEGER, pro_service_days INTEGER,
    is_active INTEGER, is_on_secondary INTEGER, is_on_waivers INTEGER,
    designated_for_assignment INTEGER, free_agent INTEGER, was_traded INTEGER,
    days_on_waivers INTEGER, days_on_waivers_left INTEGER, has_received_arbitration INTEGER,
    draft_year INTEGER, draft_round INTEGER, draft_pick INTEGER,
    draft_overall_pick INTEGER, draft_team_id INTEGER,
    date_of_birth TEXT, weight INTEGER, nation_id INTEGER, uniform_number INTEGER
);
CREATE TABLE IF NOT EXISTS ratings (
    player_id INTEGER, snapshot_date TEXT,
    ovr INTEGER, pot INTEGER,
    cntct INTEGER, gap INTEGER, pow INTEGER, eye INTEGER, ks INTEGER,
    babip INTEGER, speed INTEGER, steal INTEGER,
    stf INTEGER, mov INTEGER, ctrl INTEGER, ctrl_r INTEGER, ctrl_l INTEGER,
    hra INTEGER, pbabip INTEGER,
    fst INTEGER, snk INTEGER, crv INTEGER, sld INTEGER, chg INTEGER,
    splt INTEGER, cutt INTEGER, cir_chg INTEGER, scr INTEGER,
    frk INTEGER, kncrv INTEGER, knbl INTEGER, stm INTEGER, vel TEXT,
    pot_stf INTEGER, pot_mov INTEGER, pot_ctrl INTEGER,
    pot_hra INTEGER, pot_pbabip INTEGER,
    pot_fst INTEGER, pot_snk INTEGER, pot_crv INTEGER, pot_sld INTEGER,
    pot_chg INTEGER, pot_splt INTEGER, pot_cutt INTEGER,
    pot_cir_chg INTEGER, pot_scr INTEGER, pot_frk INTEGER,
    pot_kncrv INTEGER, pot_knbl INTEGER,
    pot_cntct INTEGER, pot_gap INTEGER, pot_pow INTEGER, pot_eye INTEGER, pot_ks INTEGER,
    pot_babip INTEGER,
    c INTEGER, ss INTEGER, second_b INTEGER, third_b INTEGER,
    first_b INTEGER, lf INTEGER, cf INTEGER, rf INTEGER,
    pot_c INTEGER, pot_ss INTEGER, pot_second_b INTEGER, pot_third_b INTEGER,
    pot_first_b INTEGER, pot_lf INTEGER, pot_cf INTEGER, pot_rf INTEGER,
    p INTEGER, pot_p INTEGER,
    ofa INTEGER, ifa INTEGER, c_arm INTEGER, c_blk INTEGER, c_frm INTEGER,
    ifr INTEGER, ofr INTEGER, ife INTEGER, ofe INTEGER, tdp INTEGER, gb INTEGER,
    cntct_l INTEGER, cntct_r INTEGER, gap_l INTEGER, gap_r INTEGER,
    pow_l INTEGER, pow_r INTEGER, eye_l INTEGER, eye_r INTEGER,
    ks_l INTEGER, ks_r INTEGER,
    babip_l INTEGER, babip_r INTEGER,
    stf_l INTEGER, stf_r INTEGER, mov_l INTEGER, mov_r INTEGER,
    hra_l INTEGER, hra_r INTEGER, pbabip_l INTEGER, pbabip_r INTEGER,
    int_ TEXT, wrk_ethic TEXT, greed TEXT, loy TEXT, lead TEXT,
    prone TEXT, acc TEXT, league_id INTEGER,
    height INTEGER, bats TEXT, throws TEXT,
    stl_rt INTEGER, run INTEGER, sac_bunt INTEGER, bunt_hit INTEGER, hold INTEGER,
    composite_score INTEGER, ceiling_score INTEGER, tool_only_score INTEGER, secondary_composite INTEGER,
    offensive_grade INTEGER, baserunning_value INTEGER, defensive_value INTEGER,
    durability_score INTEGER, offensive_ceiling INTEGER,
    PRIMARY KEY (player_id, snapshot_date)
);
CREATE VIEW IF NOT EXISTS latest_ratings AS
    SELECT * FROM ratings
    WHERE snapshot_date = (SELECT MAX(snapshot_date) FROM ratings);
CREATE TABLE IF NOT EXISTS ratings_history (
    player_id INTEGER, snapshot_date TEXT,
    composite_score INTEGER, ceiling_score INTEGER,
    ovr INTEGER, pot INTEGER,
    cntct INTEGER, gap INTEGER, pow INTEGER, eye INTEGER, ks INTEGER,
    speed INTEGER, stf INTEGER, mov INTEGER, ctrl INTEGER,
    PRIMARY KEY (player_id, snapshot_date)
);
CREATE TABLE IF NOT EXISTS contracts (
    player_id INTEGER PRIMARY KEY, team_id INTEGER, contract_team_id INTEGER,
    is_major INTEGER, season_year INTEGER, years INTEGER, current_year INTEGER,
    salary_0 INTEGER, salary_1 INTEGER, salary_2 INTEGER, salary_3 INTEGER,
    salary_4 INTEGER, salary_5 INTEGER, salary_6 INTEGER, salary_7 INTEGER,
    salary_8 INTEGER, salary_9 INTEGER, salary_10 INTEGER, salary_11 INTEGER,
    salary_12 INTEGER, salary_13 INTEGER, salary_14 INTEGER,
    no_trade INTEGER, last_year_team_option INTEGER, last_year_player_option INTEGER
);
CREATE TABLE IF NOT EXISTS contract_extensions (
    player_id INTEGER PRIMARY KEY, team_id INTEGER, years INTEGER, current_year INTEGER,
    salary_0 INTEGER, salary_1 INTEGER, salary_2 INTEGER, salary_3 INTEGER,
    salary_4 INTEGER, salary_5 INTEGER, salary_6 INTEGER, salary_7 INTEGER,
    salary_8 INTEGER, salary_9 INTEGER, salary_10 INTEGER, salary_11 INTEGER,
    salary_12 INTEGER, salary_13 INTEGER, salary_14 INTEGER,
    no_trade INTEGER, last_year_team_option INTEGER, last_year_player_option INTEGER
);
CREATE TABLE IF NOT EXISTS batting_stats (
    player_id INTEGER, year INTEGER, team_id INTEGER, split_id INTEGER,
    ab INTEGER, h INTEGER, d INTEGER, t INTEGER, hr INTEGER,
    r INTEGER, rbi INTEGER, sb INTEGER, bb INTEGER, k INTEGER,
    avg REAL, obp REAL, slg REAL, war REAL,
    pa INTEGER, stint INTEGER, hbp INTEGER, sf INTEGER,
    g INTEGER, gs INTEGER, cs INTEGER, gdp INTEGER, ibb INTEGER,
    sh INTEGER, ci INTEGER, pitches_seen INTEGER, ubr REAL, wpa REAL,
    league_id INTEGER,
    PRIMARY KEY (player_id, year, split_id, team_id)
);
CREATE TABLE IF NOT EXISTS pitching_stats (
    player_id INTEGER, year INTEGER, team_id INTEGER, split_id INTEGER,
    ip REAL, g INTEGER, gs INTEGER, w INTEGER, l INTEGER, sv INTEGER,
    era REAL, k INTEGER, bb INTEGER, ha INTEGER, war REAL,
    outs INTEGER, stint INTEGER, ra9war REAL, hra INTEGER, bf INTEGER, hp INTEGER,
    ab INTEGER, er INTEGER, r INTEGER, cg INTEGER, sho INTEGER, gf INTEGER,
    hld INTEGER, bs INTEGER, svo INTEGER, qs INTEGER,
    gb INTEGER, fb INTEGER, pi INTEGER, wp INTEGER, bk INTEGER,
    iw INTEGER, ir REAL, irs REAL, rs INTEGER, dp INTEGER,
    sb INTEGER, cs INTEGER, sf INTEGER, sh INTEGER, ci INTEGER,
    tb INTEGER, li REAL, wpa REAL, relief_app INTEGER, md INTEGER, sd INTEGER,
    league_id INTEGER,
    PRIMARY KEY (player_id, year, split_id, team_id)
);
CREATE TABLE IF NOT EXISTS fielding_stats (
    player_id INTEGER, year INTEGER, team_id INTEGER, position INTEGER,
    g INTEGER, gs INTEGER, ip REAL, tc INTEGER, a INTEGER, po INTEGER,
    e INTEGER, dp INTEGER, pb INTEGER, sba INTEGER, rto INTEGER,
    zr REAL, framing REAL, arm REAL,
    league_id INTEGER,
    PRIMARY KEY (player_id, year, team_id, position)
);
CREATE TABLE IF NOT EXISTS team_batting_stats (
    team_id INTEGER, year INTEGER, split_id INTEGER, name TEXT,
    pa INTEGER, ab INTEGER, h INTEGER, k INTEGER, hr INTEGER,
    r INTEGER, rbi INTEGER, bb INTEGER, sb INTEGER,
    avg REAL, obp REAL, slg REAL, ops REAL, iso REAL,
    k_pct REAL, bb_pct REAL, babip REAL, woba REAL,
    PRIMARY KEY (team_id, year, split_id)
);
CREATE TABLE IF NOT EXISTS team_pitching_stats (
    team_id INTEGER, year INTEGER, split_id INTEGER, name TEXT,
    ip REAL, era REAL, k INTEGER, bb INTEGER, ha INTEGER,
    r INTEGER, er INTEGER, hra INTEGER, g INTEGER,
    k_pct REAL, bb_pct REAL, fip REAL, babip REAL, avg REAL, obp REAL,
    PRIMARY KEY (team_id, year, split_id)
);
CREATE TABLE IF NOT EXISTS games (
    game_id INTEGER PRIMARY KEY, home_team INTEGER, away_team INTEGER,
    date TEXT, runs0 INTEGER, runs1 INTEGER, game_type INTEGER, played INTEGER,
    winning_pitcher INTEGER, losing_pitcher INTEGER, save_pitcher INTEGER
);
CREATE TABLE IF NOT EXISTS prospect_fv (
    player_id INTEGER, eval_date TEXT, fv INTEGER, fv_str TEXT,
    level TEXT, bucket TEXT, prospect_surplus INTEGER, risk TEXT,
    PRIMARY KEY (player_id, eval_date)
);
CREATE TABLE IF NOT EXISTS player_surplus (
    player_id INTEGER, eval_date TEXT, name TEXT, bucket TEXT,
    age INTEGER, ovr INTEGER, fv INTEGER, fv_str TEXT,
    surplus INTEGER, surplus_yr1 INTEGER, level TEXT,
    team_id INTEGER, parent_team_id INTEGER,
    PRIMARY KEY (player_id, eval_date)
);
"""

# ── Seed data ────────────────────────────────────────────────────────────────

TEAM_ID = 1
HITTER_ID = 101
PITCHER_ID = 102
PROSPECT_ID = 201
EVAL_DATE = "2033-04-01"
YEAR = 2033


def _r(conn, table, **kwargs):
    """Insert a row into table using named columns."""
    cols = ", ".join(kwargs.keys())
    placeholders = ", ".join("?" * len(kwargs))
    conn.execute(f"INSERT INTO {table} ({cols}) VALUES ({placeholders})", list(kwargs.values()))


def _ratings(pid, is_pitcher=False, include_components=True):
    base = dict(
        player_id=pid, snapshot_date="2033-04-01",
        ovr=55 if not is_pitcher else 58,
        pot=60 if not is_pitcher else 65,
        league_id=1, height=183, bats="R", throws="R",
        int_="N", wrk_ethic="H", greed="N", loy="N", lead="N", acc="A",
        composite_score=56 if not is_pitcher else 59,
        ceiling_score=62 if not is_pitcher else 66,
        tool_only_score=54 if not is_pitcher else 57,
        secondary_composite=None,
    )
    if is_pitcher:
        base.update(stf=65, mov=60, ctrl=55, ctrl_r=55, ctrl_l=50, stm=55,
                    pot_stf=65, pot_mov=60, pot_ctrl=55,
                    fst=70, crv=55, sld=60, chg=50,
                    pot_fst=65, pot_crv=50, pot_sld=55, pot_chg=45,
                    stf_l=55, stf_r=60, mov_l=55, mov_r=60)
        if include_components:
            base.update(offensive_grade=59, durability_score=55, offensive_ceiling=66)
    else:
        base.update(cntct=55, gap=50, pow=50, eye=55, ks=50, speed=55, steal=50,
                    pot_cntct=55, pot_gap=60, pot_pow=55, pot_eye=55, pot_ks=50,
                    c=30, ss=55, second_b=50, third_b=45, first_b=40,
                    lf=45, cf=50, rf=45,
                    ofa=50, ifa=50,
                    cntct_l=50, cntct_r=55, gap_l=45, gap_r=50,
                    pow_l=45, pow_r=50, eye_l=50, eye_r=55, ks_l=45, ks_r=50)
        if include_components:
            base.update(offensive_grade=55, baserunning_value=50, defensive_value=45,
                        offensive_ceiling=60)
    return base


def _seed(conn):
    _r(conn, "teams", team_id=TEAM_ID, name="Test Team", level="1", parent_team_id=0, league="TL")

    _r(conn, "players", player_id=HITTER_ID, name="Joe Hitter", age=27,
       team_id=TEAM_ID, parent_team_id=0, level="1", pos=6, role=0)
    _r(conn, "players", player_id=PITCHER_ID, name="Sam Pitcher", age=25,
       team_id=TEAM_ID, parent_team_id=0, level="1", pos=1, role=11)
    _r(conn, "players", player_id=PROSPECT_ID, name="Bob Prospect", age=21,
       team_id=TEAM_ID, parent_team_id=TEAM_ID, level="3", pos=6, role=0)

    conn.execute("INSERT INTO ratings ({}) VALUES ({})".format(
        ", ".join(_ratings(HITTER_ID).keys()),
        ", ".join("?" * len(_ratings(HITTER_ID)))),
        list(_ratings(HITTER_ID).values()))
    conn.execute("INSERT INTO ratings ({}) VALUES ({})".format(
        ", ".join(_ratings(PITCHER_ID, True).keys()),
        ", ".join("?" * len(_ratings(PITCHER_ID, True)))),
        list(_ratings(PITCHER_ID, True).values()))
    conn.execute("INSERT INTO ratings ({}) VALUES ({})".format(
        ", ".join(_ratings(PROSPECT_ID, include_components=False).keys()),
        ", ".join("?" * len(_ratings(PROSPECT_ID, include_components=False)))),
        list(_ratings(PROSPECT_ID, include_components=False).values()))

    _r(conn, "contracts", player_id=HITTER_ID, team_id=TEAM_ID, contract_team_id=TEAM_ID,
       is_major=1, season_year=YEAR, years=3, current_year=0,
       salary_0=5000000, salary_1=6000000, salary_2=7000000)
    _r(conn, "contracts", player_id=PITCHER_ID, team_id=TEAM_ID, contract_team_id=TEAM_ID,
       is_major=1, season_year=YEAR, years=2, current_year=0,
       salary_0=4000000, salary_1=5000000)

    _r(conn, "batting_stats", player_id=HITTER_ID, year=YEAR, team_id=TEAM_ID, split_id=1,
       ab=200, h=58, d=12, t=1, hr=8, r=30, rbi=35, sb=5, bb=22, k=40,
       avg=0.290, obp=0.360, slg=0.450, war=1.8, pa=230, hbp=5, sf=3, g=25, cs=2)
    _r(conn, "batting_stats", player_id=HITTER_ID, year=YEAR, team_id=TEAM_ID, split_id=2,
       ab=90, h=25, d=5, t=0, hr=3, r=12, rbi=14, sb=2, bb=10, k=18,
       avg=0.278, obp=0.350, slg=0.422, war=0.8, pa=102, hbp=2, sf=1, g=11, cs=1)
    _r(conn, "batting_stats", player_id=HITTER_ID, year=YEAR, team_id=TEAM_ID, split_id=3,
       ab=110, h=33, d=7, t=1, hr=5, r=18, rbi=21, sb=3, bb=12, k=22,
       avg=0.300, obp=0.368, slg=0.473, war=1.0, pa=128, hbp=3, sf=2, g=14, cs=1)

    _r(conn, "pitching_stats", player_id=PITCHER_ID, year=YEAR, team_id=TEAM_ID, split_id=1,
       ip=80.0, g=14, gs=14, w=6, l=4, sv=0, era=3.60, k=88, bb=25, ha=70, war=2.5,
       outs=240, ra9war=2.8, hra=8, bf=300, hp=3, er=32, r=35, hld=0, bs=2, qs=5,
       gb=120, fb=80)

    _r(conn, "fielding_stats", player_id=HITTER_ID, year=YEAR, team_id=TEAM_ID, position=6,
       g=25, gs=25, ip=220.0, tc=65, a=20, po=45, e=1, dp=3, zr=0.05, arm=55.0)

    _r(conn, "team_batting_stats", team_id=TEAM_ID, year=YEAR, split_id=1, name="Test Team",
       pa=5800, ab=5200, h=1430, k=1100, hr=160, r=720, rbi=690, bb=520, sb=110,
       avg=0.275, obp=0.340, slg=0.440, ops=0.780, iso=0.165,
       k_pct=19.0, bb_pct=9.0, babip=0.295, woba=0.330)
    _r(conn, "team_pitching_stats", team_id=TEAM_ID, year=YEAR, split_id=1, name="Test Team",
       ip=1440.0, era=3.90, k=1350, bb=480, ha=1300, r=650, er=620, hra=155, g=160,
       k_pct=22.0, bb_pct=8.0, fip=4.10, babip=0.290, avg=0.248, obp=0.315)

    _r(conn, "games", game_id=1, home_team=TEAM_ID, away_team=2, date="2033-04-01",
       runs0=3, runs1=5, game_type=0, played=1, winning_pitcher=PITCHER_ID)
    _r(conn, "games", game_id=2, home_team=2, away_team=TEAM_ID, date="2033-04-02",
       runs0=2, runs1=4, game_type=0, played=1, losing_pitcher=PITCHER_ID)

    _r(conn, "prospect_fv", player_id=PROSPECT_ID, eval_date=EVAL_DATE,
       fv=50, fv_str="50", level="AA", bucket="SS", prospect_surplus=8000000, risk="Medium")

    _r(conn, "player_surplus", player_id=HITTER_ID, eval_date=EVAL_DATE,
       name="Joe Hitter", bucket="SS", age=27, ovr=55,
       surplus=12000000, surplus_yr1=4000000, level="1", team_id=TEAM_ID, parent_team_id=0)
    _r(conn, "player_surplus", player_id=PITCHER_ID, eval_date=EVAL_DATE,
       name="Sam Pitcher", bucket="SP", age=25, ovr=58,
       surplus=8000000, surplus_yr1=3000000, level="1", team_id=TEAM_ID, parent_team_id=0)

    conn.commit()


# ── Fixtures ─────────────────────────────────────────────────────────────────

class _ConnProxy:
    """Wraps a shared sqlite3 connection, preventing row_factory mutations and close().

    Query functions set conn.row_factory = None for tuple rows, and some call
    conn.close(). Since we share one in-memory connection, we must prevent both.
    """
    def __init__(self, conn):
        object.__setattr__(self, "_conn", conn)

    def close(self):
        pass  # never close the shared test connection

    def __getattr__(self, name):
        return getattr(object.__getattribute__(self, "_conn"), name)

    def __setattr__(self, name, value):
        if name == "row_factory":
            return  # ignore — always use sqlite3.Row on the underlying conn
        setattr(object.__getattribute__(self, "_conn"), name, value)


@pytest.fixture(scope="session")
def db_conn():
    """In-memory SQLite connection seeded with minimal test data."""
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    _seed(conn)
    yield _ConnProxy(conn)
    conn.close()


def _make_cfg():
    """Minimal LeagueConfig-like namespace for tests."""
    cfg = MagicMock()
    cfg.year = YEAR
    cfg.my_team_id = TEAM_ID
    cfg.game_date = "2033-04-02"
    cfg.minimum_salary = 575000
    cfg.pyth_exp = 1.83
    cfg.ratings_scale = "1-100"
    cfg.team_abbr_map = {TEAM_ID: "TST"}
    cfg.team_names_map = {TEAM_ID: "Test Team"}
    cfg.team_div_map = {TEAM_ID: "Test Division"}
    cfg.mlb_team_ids = {TEAM_ID}
    cfg.pos_map = {1:"P",2:"C",3:"1B",4:"2B",5:"3B",6:"SS",7:"LF",8:"CF",9:"RF",10:"DH"}
    cfg.role_map = {11:"starter",12:"reliever",13:"closer"}
    cfg.level_map = {"1":"MLB","2":"AAA","3":"AA","4":"A","5":"A-Short","6":"Rookie"}
    cfg.pos_order = {"SP":1,"RP":2,"C":4,"1B":5,"2B":6,"3B":7,"SS":8,"LF":9,"CF":10,"RF":11,"DH":12}
    cfg.divisions = {"Test Division": [TEAM_ID]}
    cfg.leagues = [{"name":"TL","short":"TL","color":"#fff","divisions":{"East":[TEAM_ID]}}]
    cfg.team_abbr = lambda tid: cfg.team_abbr_map.get(tid, "?")
    cfg.team_div_map = {TEAM_ID: "Test Division"}
    cfg.settings = {"statsplus_slug": ""}
    cfg.state_path = "/tmp/state.json"
    cfg.league_dir = Path("/tmp")
    return cfg


@pytest.fixture(scope="session")
def mock_cfg():
    return _make_cfg()


@pytest.fixture(autouse=True)
def patch_web_context(db_conn, mock_cfg):
    """Patch get_db and get_cfg in all web query modules for every test."""
    targets = [
        "web_league_context.get_db",
        "web_league_context.get_cfg",
        "team_queries.get_db",
        "team_queries.get_cfg",
        "team_queries.team_abbr_map",
        "team_queries.team_names_map",
        "team_queries.level_map",
        "team_queries.pos_map",
        "team_queries.pos_order",
        "team_queries.pyth_exp",
        "team_queries.my_team_id",
        "team_queries.mlb_team_ids",
        "team_queries._load_la",
        "player_queries.get_db",
        "player_queries.get_cfg",
        "player_queries.team_abbr_map",
        "player_queries.team_names_map",
        "player_queries.level_map",
        "player_queries.pos_map",
        "queries.get_db",
        "queries.get_cfg",
        "queries.team_abbr_map",
        "queries.team_names_map",
        "queries.level_map",
        "queries.pos_order",
        "queries.mlb_team_ids",
        "queries.year",
        "percentiles.get_db",
        "percentiles.get_cfg",
    ]

    _la = {
        "year": YEAR, "teams_in_sample": 1,
        "batting": {"avg": 0.260, "obp": 0.330, "slg": 0.420, "ops": 0.750,
                    "woba": 0.320, "babip": 0.295, "iso": 0.160, "k_pct": 20.0, "bb_pct": 8.5},
        "pitching": {"era": 4.10, "fip": 4.00, "x_fip": 4.05, "k_pct": 21.0, "bb_pct": 8.0,
                     "k_bb_pct": 13.0, "babip": 0.290, "avg": 0.248, "obp": 0.315},
        "dollar_per_war": 7000000,
    }

    _state = {"year": YEAR, "game_date": "2033-04-02", "my_team_id": TEAM_ID}

    patches = [
        patch("web_league_context.get_db", return_value=db_conn),
        patch("web_league_context.get_cfg", return_value=mock_cfg),
        patch("team_queries.get_db", return_value=db_conn),
        patch("team_queries.get_cfg", return_value=mock_cfg),
        patch("team_queries._get_state", return_value=_state),
        patch("team_queries.team_abbr_map", return_value=mock_cfg.team_abbr_map),
        patch("team_queries.team_names_map", return_value=mock_cfg.team_names_map),
        patch("team_queries.level_map", return_value=mock_cfg.level_map),
        patch("team_queries.pos_map", return_value=mock_cfg.pos_map),
        patch("team_queries.pos_order", return_value=mock_cfg.pos_order),
        patch("team_queries.pyth_exp", return_value=mock_cfg.pyth_exp),
        patch("team_queries.my_team_id", return_value=TEAM_ID),
        patch("team_queries.mlb_team_ids", return_value=mock_cfg.mlb_team_ids),
        patch("team_queries._load_la", return_value=_la),
        patch("player_queries.get_db", return_value=db_conn),
        patch("player_queries.get_cfg", return_value=mock_cfg),
        patch("player_queries.team_abbr_map", return_value=mock_cfg.team_abbr_map),
        patch("player_queries.team_names_map", return_value=mock_cfg.team_names_map),
        patch("player_queries.level_map", return_value=mock_cfg.level_map),
        patch("player_queries.pos_map", return_value=mock_cfg.pos_map),
        patch("queries.get_db", return_value=db_conn),
        patch("queries.get_cfg", return_value=mock_cfg),
        patch("queries.team_abbr_map", return_value=mock_cfg.team_abbr_map),
        patch("queries.team_names_map", return_value=mock_cfg.team_names_map),
        patch("queries.level_map", return_value=mock_cfg.level_map),
        patch("queries.pos_order", return_value=mock_cfg.pos_order),
        patch("queries.mlb_team_ids", return_value=mock_cfg.mlb_team_ids),
        patch("queries.year", return_value=YEAR),
        patch("percentiles.get_db", return_value=db_conn),
        patch("percentiles.get_cfg", return_value=mock_cfg),
    ]

    started = [p.start() for p in patches]
    yield
    for p in patches:
        p.stop()
