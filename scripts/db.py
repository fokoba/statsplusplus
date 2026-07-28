"""
db.py — SQLite connection and schema initialization.

Resolves DB path from the active league directory (data/<league>/league.db).
Falls back to legacy emlb.db at project root for pre-migration compat.
"""

import sqlite3
from pathlib import Path

from league_context import get_league_dir

_LEGACY_DB = Path(__file__).parent.parent / "emlb.db"


def _resolve_db_path(league_dir: Path | None = None, create: bool = False) -> Path:
    if league_dir is None:
        league_dir = get_league_dir()
        explicit = False
    else:
        explicit = True
    db_path = league_dir / "league.db"
    if db_path.exists() or explicit or create:
        return db_path
    # Legacy fallback — only for implicit resolution (pre-migration compat)
    if _LEGACY_DB.exists():
        return _LEGACY_DB
    return db_path

SCHEMA = """
CREATE TABLE IF NOT EXISTS teams (
    team_id        INTEGER PRIMARY KEY,
    name           TEXT,
    level          TEXT,
    parent_team_id INTEGER,
    league         TEXT
);

CREATE TABLE IF NOT EXISTS players (
    player_id      INTEGER PRIMARY KEY,
    name           TEXT,
    age            INTEGER,
    team_id        INTEGER,
    parent_team_id INTEGER,
    level          TEXT,
    pos            INTEGER,
    role           INTEGER
);

CREATE TABLE IF NOT EXISTS ratings (
    player_id     INTEGER,
    snapshot_date TEXT,
    ovr           INTEGER,
    pot           INTEGER,
    cntct INTEGER, gap INTEGER, pow INTEGER, eye INTEGER, ks INTEGER,
    babip INTEGER,
    speed INTEGER, steal INTEGER,
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
    ifr INTEGER, ofr INTEGER,
    ife INTEGER, ofe INTEGER, tdp INTEGER, gb INTEGER,
    cntct_l INTEGER, cntct_r INTEGER, gap_l INTEGER, gap_r INTEGER,
    pow_l INTEGER, pow_r INTEGER, eye_l INTEGER, eye_r INTEGER,
    ks_l INTEGER, ks_r INTEGER,
    babip_l INTEGER, babip_r INTEGER,
    stf_l INTEGER, stf_r INTEGER, mov_l INTEGER, mov_r INTEGER,
    hra_l INTEGER, hra_r INTEGER, pbabip_l INTEGER, pbabip_r INTEGER,
    int_ TEXT, wrk_ethic TEXT, greed TEXT, loy TEXT, lead TEXT,
    prone TEXT, acc TEXT,
    league_id INTEGER,
    height INTEGER, bats TEXT, throws TEXT,
    stl_rt INTEGER, run INTEGER, sac_bunt INTEGER, bunt_hit INTEGER, hold INTEGER,
    composite_score INTEGER, ceiling_score INTEGER, tool_only_score INTEGER, secondary_composite INTEGER,
    true_ceiling INTEGER, positional_percentile REAL, positional_median INTEGER,
    PRIMARY KEY (player_id, snapshot_date)
);

CREATE TABLE IF NOT EXISTS contract_extensions (
    player_id               INTEGER PRIMARY KEY,
    team_id                 INTEGER,
    years                   INTEGER,
    current_year            INTEGER,
    salary_0  INTEGER, salary_1  INTEGER, salary_2  INTEGER, salary_3  INTEGER,
    salary_4  INTEGER, salary_5  INTEGER, salary_6  INTEGER, salary_7  INTEGER,
    salary_8  INTEGER, salary_9  INTEGER, salary_10 INTEGER, salary_11 INTEGER,
    salary_12 INTEGER, salary_13 INTEGER, salary_14 INTEGER,
    no_trade                INTEGER,
    last_year_team_option   INTEGER,
    last_year_player_option INTEGER
);

CREATE TABLE IF NOT EXISTS contracts (
    player_id               INTEGER PRIMARY KEY,
    team_id                 INTEGER,
    contract_team_id        INTEGER,
    is_major                INTEGER,
    season_year             INTEGER,
    years                   INTEGER,
    current_year            INTEGER,
    salary_0  INTEGER, salary_1  INTEGER, salary_2  INTEGER, salary_3  INTEGER,
    salary_4  INTEGER, salary_5  INTEGER, salary_6  INTEGER, salary_7  INTEGER,
    salary_8  INTEGER, salary_9  INTEGER, salary_10 INTEGER, salary_11 INTEGER,
    salary_12 INTEGER, salary_13 INTEGER, salary_14 INTEGER,
    no_trade                INTEGER,
    last_year_team_option   INTEGER,
    last_year_player_option INTEGER
);

CREATE TABLE IF NOT EXISTS batting_stats (
    player_id INTEGER, year INTEGER, team_id INTEGER, split_id INTEGER,
    ab INTEGER, h INTEGER, d INTEGER, t INTEGER, hr INTEGER,
    r INTEGER, rbi INTEGER, sb INTEGER, bb INTEGER, k INTEGER,
    avg REAL, obp REAL, slg REAL, war REAL,
    pa INTEGER, stint INTEGER, hbp INTEGER, sf INTEGER,
    g INTEGER, gs INTEGER, cs INTEGER, gdp INTEGER, ibb INTEGER,
    sh INTEGER, ci INTEGER, pitches_seen INTEGER, ubr REAL, wpa REAL,
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
    PRIMARY KEY (player_id, year, split_id, team_id)
);

CREATE TABLE IF NOT EXISTS prospect_fv (
    player_id       INTEGER,
    eval_date       TEXT,
    fv              INTEGER,
    fv_str          TEXT,
    level           TEXT,
    bucket          TEXT,
    prospect_surplus INTEGER,
    risk            TEXT,
    PRIMARY KEY (player_id, eval_date)
);

CREATE TABLE IF NOT EXISTS org_reports (
    team_id     INTEGER,
    report_date TEXT,
    report_md   TEXT,
    PRIMARY KEY (team_id, report_date)
);

CREATE TABLE IF NOT EXISTS player_surplus (
    player_id      INTEGER,
    eval_date      TEXT,
    name           TEXT,
    bucket         TEXT,
    age            INTEGER,
    ovr            INTEGER,
    fv             INTEGER,
    fv_str         TEXT,
    surplus        INTEGER,
    surplus_yr1    INTEGER,
    level          TEXT,
    team_id        INTEGER,
    parent_team_id INTEGER,
    PRIMARY KEY (player_id, eval_date)
);

CREATE TABLE IF NOT EXISTS fielding_stats (
    player_id INTEGER,
    year      INTEGER,
    team_id   INTEGER,
    position  INTEGER,
    g INTEGER, gs INTEGER, ip REAL, tc INTEGER, a INTEGER, po INTEGER,
    e INTEGER, dp INTEGER, pb INTEGER, sba INTEGER, rto INTEGER,
    zr REAL, framing REAL, arm REAL,
    PRIMARY KEY (player_id, year, team_id, position)
);

CREATE TABLE IF NOT EXISTS team_batting_stats (
    team_id  INTEGER,
    year     INTEGER,
    split_id INTEGER,
    name     TEXT,
    pa INTEGER, ab INTEGER, h INTEGER, k INTEGER, hr INTEGER,
    r INTEGER, rbi INTEGER, bb INTEGER, sb INTEGER,
    avg REAL, obp REAL, slg REAL, ops REAL, iso REAL,
    k_pct REAL, bb_pct REAL, babip REAL, woba REAL,
    PRIMARY KEY (team_id, year, split_id)
);

CREATE TABLE IF NOT EXISTS team_pitching_stats (
    team_id  INTEGER,
    year     INTEGER,
    split_id INTEGER,
    name     TEXT,
    ip REAL, era REAL, k INTEGER, bb INTEGER, ha INTEGER,
    r INTEGER, er INTEGER, hra INTEGER, g INTEGER,
    k_pct REAL, bb_pct REAL, fip REAL, babip REAL, avg REAL, obp REAL,
    PRIMARY KEY (team_id, year, split_id)
);

CREATE TABLE IF NOT EXISTS games (
    game_id   INTEGER PRIMARY KEY,
    home_team INTEGER,
    away_team INTEGER,
    date      TEXT,
    runs0     INTEGER,  -- AWAY team runs (team batting first)
    runs1     INTEGER,  -- HOME team runs (team batting second)
    game_type INTEGER,
    played    INTEGER,
    winning_pitcher INTEGER,
    losing_pitcher  INTEGER,
    save_pitcher    INTEGER
);

CREATE TABLE IF NOT EXISTS ratings_history (
    player_id     INTEGER,
    snapshot_date TEXT,
    ovr           INTEGER,
    pot           INTEGER,
    cntct         INTEGER,
    gap           INTEGER,
    pow           INTEGER,
    eye           INTEGER,
    ks            INTEGER,
    speed         INTEGER,
    stm           INTEGER,
    stf           INTEGER,
    mov           INTEGER,
    ctrl          INTEGER,
    fst           INTEGER,
    snk           INTEGER,
    crv           INTEGER,
    sld           INTEGER,
    chg           INTEGER,
    splt          INTEGER,
    cutt          INTEGER,
    cir_chg       INTEGER,
    scr           INTEGER,
    frk           INTEGER,
    kncrv         INTEGER,
    knbl          INTEGER,
    pot_cntct     INTEGER,
    pot_gap       INTEGER,
    pot_pow       INTEGER,
    pot_eye       INTEGER,
    pot_ks        INTEGER,
    pot_stf       INTEGER,
    pot_mov       INTEGER,
    pot_ctrl      INTEGER,
    pot_fst       INTEGER,
    pot_snk       INTEGER,
    pot_crv       INTEGER,
    pot_sld       INTEGER,
    pot_chg       INTEGER,
    pot_splt      INTEGER,
    pot_cutt      INTEGER,
    pot_cir_chg   INTEGER,
    pot_scr       INTEGER,
    pot_frk       INTEGER,
    pot_kncrv     INTEGER,
    pot_knbl      INTEGER,
    babip         INTEGER,
    hra           INTEGER,
    pbabip        INTEGER,
    pot_babip     INTEGER,
    pot_hra       INTEGER,
    pot_pbabip    INTEGER,
    prone         TEXT,
    composite_score INTEGER,
    ceiling_score   INTEGER,
    PRIMARY KEY (player_id, snapshot_date)
);

CREATE VIEW IF NOT EXISTS latest_ratings AS
SELECT * FROM ratings
WHERE snapshot_date = (SELECT MAX(snapshot_date) FROM ratings);
"""


def get_conn(league_dir: Path | None = None) -> sqlite3.Connection:
    db_path = _resolve_db_path(league_dir)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        "CREATE VIEW IF NOT EXISTS latest_ratings AS "
        "SELECT * FROM ratings "
        "WHERE snapshot_date = (SELECT MAX(snapshot_date) FROM ratings)"
    )
    conn.row_factory = sqlite3.Row
    return conn


def _migrate_ratings(conn: sqlite3.Connection):
    """Add columns introduced after the initial schema."""
    existing = {row[1] for row in conn.execute("PRAGMA table_info(ratings)").fetchall()}
    new_cols = [
        ("ctrl", "INTEGER", "AFTER mov"),
        ("p", "INTEGER", "AFTER pot_rf"),
        ("pot_p", "INTEGER", "AFTER p"),
        ("stl_rt", "INTEGER", "AFTER throws"),
        ("run", "INTEGER", "AFTER stl_rt"),
        ("sac_bunt", "INTEGER", "AFTER run"),
        ("bunt_hit", "INTEGER", "AFTER sac_bunt"),
        ("hold", "INTEGER", "AFTER bunt_hit"),
        ("babip", "INTEGER", None),
        ("babip_l", "INTEGER", None),
        ("babip_r", "INTEGER", None),
        ("pot_babip", "INTEGER", None),
        ("hra", "INTEGER", None),
        ("hra_l", "INTEGER", None),
        ("hra_r", "INTEGER", None),
        ("pot_hra", "INTEGER", None),
        ("pbabip", "INTEGER", None),
        ("pbabip_l", "INTEGER", None),
        ("pbabip_r", "INTEGER", None),
        ("pot_pbabip", "INTEGER", None),
        ("prone", "TEXT", None),
        ("composite_score", "INTEGER", None),
        ("ceiling_score", "INTEGER", None),
        ("tool_only_score", "INTEGER", None),
        ("secondary_composite", "INTEGER", None),
    ]
    for col, typ, _ in new_cols:
        if col not in existing:
            # SQLite ignores AFTER clause but we include it for documentation
            conn.execute(f"ALTER TABLE ratings ADD COLUMN {col} {typ}")


def _migrate_ratings_history(conn: sqlite3.Connection):
    """Add columns introduced after the initial ratings_history schema."""
    existing = {row[1] for row in conn.execute("PRAGMA table_info(ratings_history)").fetchall()}
    new_cols = [
        ("composite_score", "INTEGER"),
        ("ceiling_score", "INTEGER"),
    ]
    for col, typ in new_cols:
        if col not in existing:
            conn.execute(f"ALTER TABLE ratings_history ADD COLUMN {col} {typ}")


def _migrate_ratings_components(conn: sqlite3.Connection):
    """Add component score columns to ratings and ratings_history."""
    new_cols = [
        ("offensive_grade", "INTEGER"),
        ("baserunning_value", "INTEGER"),
        ("defensive_value", "INTEGER"),
        ("durability_score", "INTEGER"),
        ("offensive_ceiling", "INTEGER"),
    ]
    for table in ("ratings", "ratings_history"):
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        for col, typ in new_cols:
            if col not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")


def _migrate_positional_context(conn: sqlite3.Connection):
    """Add positional context columns to ratings table.

    Adds nullable ``positional_percentile`` (REAL) and
    ``positional_median`` (INTEGER) columns used by the two-pass
    evaluation pipeline to store per-player positional context.
    """
    existing = {row[1] for row in conn.execute("PRAGMA table_info(ratings)").fetchall()}
    new_cols = [
        ("positional_percentile", "REAL"),
        ("positional_median", "INTEGER"),
    ]
    for col, typ in new_cols:
        if col not in existing:
            conn.execute(f"ALTER TABLE ratings ADD COLUMN {col} {typ}")


def _migrate_players(conn: sqlite3.Connection):
    """Add expanded player fields from StatsPlus API (Phase 1, July 2026)."""
    existing = {row[1] for row in conn.execute("PRAGMA table_info(players)").fetchall()}
    new_cols = [
        # Injury
        ("injury_is_injured", "INTEGER"),
        ("injury_dl_left", "INTEGER"),
        ("injury_left", "INTEGER"),
        ("is_on_dl", "INTEGER"),
        ("is_on_dl60", "INTEGER"),
        ("dl_days_this_year", "INTEGER"),
        # Service time
        ("mlb_service_years", "INTEGER"),
        ("mlb_service_days", "INTEGER"),
        ("mlb_service_days_this_year", "INTEGER"),
        ("pro_service_years", "INTEGER"),
        ("pro_service_days", "INTEGER"),
        # Roster status
        ("is_active", "INTEGER"),
        ("is_on_secondary", "INTEGER"),
        ("is_on_waivers", "INTEGER"),
        ("designated_for_assignment", "INTEGER"),
        ("free_agent", "INTEGER"),
        ("was_traded", "INTEGER"),
        ("days_on_waivers", "INTEGER"),
        ("days_on_waivers_left", "INTEGER"),
        ("has_received_arbitration", "INTEGER"),
        # Draft info
        ("draft_year", "INTEGER"),
        ("draft_round", "INTEGER"),
        ("draft_pick", "INTEGER"),
        ("draft_overall_pick", "INTEGER"),
        ("draft_team_id", "INTEGER"),
        # Demographics
        ("date_of_birth", "TEXT"),
        ("weight", "INTEGER"),
        ("nation_id", "INTEGER"),
        ("uniform_number", "INTEGER"),
    ]
    for col, typ in new_cols:
        if col not in existing:
            conn.execute(f"ALTER TABLE players ADD COLUMN {col} {typ}")


def init_schema(league_dir: Path | None = None):
    with get_conn(league_dir) as conn:
        conn.executescript(SCHEMA)
        _migrate_ratings(conn)
        _migrate_ratings_history(conn)
        _migrate_ratings_components(conn)
        _migrate_positional_context(conn)
        # player_surplus: add surplus_yr1 if missing
        ps_cols = {r[1] for r in conn.execute("PRAGMA table_info(player_surplus)").fetchall()}
        if "surplus_yr1" not in ps_cols:
            conn.execute("ALTER TABLE player_surplus ADD COLUMN surplus_yr1 INTEGER")
        _migrate_players(conn)
        _migrate_stats_league_id(conn)


def _migrate_stats_league_id(conn: sqlite3.Connection):
    """Add league_id column to stat tables for minor league stats (Phase 2, July 2026).

    NULL = MLB (backward compatible). Non-null = minor league ID from /lgdata.
    """
    for table in ("batting_stats", "pitching_stats", "fielding_stats"):
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if "league_id" not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN league_id INTEGER")
