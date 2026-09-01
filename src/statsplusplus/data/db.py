"""SQLite connection management and schema initialization.

Provides connection factory and schema migration. All table definitions
and column additions live here — the single source of truth for DB schema.

Public API:
    get_connection(league_dir) -> sqlite3.Connection
    init_schema(league_dir) -> None

Design:
    - get_connection() can be used as a context manager
    - WAL mode enabled for concurrent reads during writes
    - latest_ratings view created on every connection for convenience
    - Migrations are idempotent (safe to run repeatedly)
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Optional

from statsplusplus.config.league_context import get_league_dir


def _resolve_db_path(league_dir: Optional[Path] = None) -> Path:
    """Resolve the database file path for a league."""
    if league_dir is None:
        league_dir = get_league_dir()
    return league_dir / "league.db"


def get_connection(league_dir: Optional[Path] = None) -> sqlite3.Connection:
    """Get a database connection for a league.

    The connection is configured with:
    - WAL journal mode (concurrent reads during writes)
    - sqlite3.Row factory (dict-like access by column name)
    - latest_ratings view (most recent snapshot only)

    Can be used as a context manager:
        with get_connection(league_dir) as conn:
            rows = conn.execute("SELECT ...").fetchall()

    Args:
        league_dir: Path to the league data directory. If None, uses active league.

    Returns:
        Configured sqlite3.Connection.
    """
    db_path = _resolve_db_path(league_dir)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        "CREATE VIEW IF NOT EXISTS latest_ratings AS "
        "SELECT * FROM ratings "
        "WHERE snapshot_date = (SELECT MAX(snapshot_date) FROM ratings)"
    )
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

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
    composite_score INTEGER, ceiling_score INTEGER, tool_only_score INTEGER,
    secondary_composite INTEGER, true_ceiling INTEGER,
    positional_percentile REAL, positional_median INTEGER,
    offensive_grade INTEGER, baserunning_value INTEGER,
    defensive_value INTEGER, durability_score INTEGER, offensive_ceiling INTEGER,
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
    last_year_player_option INTEGER,
    last_year_vesting_option INTEGER,
    last_year_option_buyout INTEGER,
    next_last_year_team_option INTEGER,
    next_last_year_player_option INTEGER,
    next_last_year_vesting_option INTEGER,
    next_last_year_option_buyout INTEGER,
    minimum_pa INTEGER, minimum_pa_bonus INTEGER,
    minimum_ip INTEGER, minimum_ip_bonus INTEGER,
    mvp_bonus INTEGER, cyyoung_bonus INTEGER, allstar_bonus INTEGER
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
    player_id INTEGER,
    year      INTEGER,
    team_id   INTEGER,
    position  INTEGER,
    g INTEGER, gs INTEGER, ip REAL, tc INTEGER, a INTEGER, po INTEGER,
    e INTEGER, dp INTEGER, pb INTEGER, sba INTEGER, rto INTEGER,
    zr REAL, framing REAL, arm REAL,
    league_id INTEGER,
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
    runs0     INTEGER,
    runs1     INTEGER,
    game_type INTEGER,
    played    INTEGER,
    winning_pitcher INTEGER,
    losing_pitcher  INTEGER,
    save_pitcher    INTEGER
);

CREATE TABLE IF NOT EXISTS ratings_history (
    player_id     INTEGER,
    snapshot_date TEXT,
    ovr INTEGER, pot INTEGER,
    cntct INTEGER, gap INTEGER, pow INTEGER, eye INTEGER, ks INTEGER,
    speed INTEGER, stm INTEGER,
    stf INTEGER, mov INTEGER, ctrl INTEGER,
    fst INTEGER, snk INTEGER, crv INTEGER, sld INTEGER, chg INTEGER,
    splt INTEGER, cutt INTEGER, cir_chg INTEGER, scr INTEGER, frk INTEGER,
    kncrv INTEGER, knbl INTEGER,
    pot_stf INTEGER, pot_mov INTEGER, pot_ctrl INTEGER,
    pot_fst INTEGER, pot_snk INTEGER, pot_crv INTEGER, pot_sld INTEGER,
    pot_chg INTEGER, pot_splt INTEGER, pot_cutt INTEGER,
    pot_cir_chg INTEGER, pot_scr INTEGER, pot_frk INTEGER,
    pot_kncrv INTEGER, pot_knbl INTEGER,
    pot_cntct INTEGER, pot_gap INTEGER, pot_pow INTEGER, pot_eye INTEGER, pot_ks INTEGER,
    babip INTEGER, hra INTEGER, pbabip INTEGER,
    pot_babip INTEGER, pot_hra INTEGER, pot_pbabip INTEGER,
    prone TEXT,
    composite_score INTEGER, ceiling_score INTEGER,
    offensive_grade INTEGER, baserunning_value INTEGER,
    defensive_value INTEGER, durability_score INTEGER, offensive_ceiling INTEGER,
    PRIMARY KEY (player_id, snapshot_date)
);

CREATE TABLE IF NOT EXISTS trade_block (
    player_id   INTEGER PRIMARY KEY,
    fetched_date TEXT
);

CREATE TABLE IF NOT EXISTS standings (
    team_id      INTEGER PRIMARY KEY,
    w            INTEGER,
    l            INTEGER,
    t            INTEGER,
    pct          REAL,
    gb           REAL,
    pos          INTEGER,
    streak       INTEGER,
    magic_number INTEGER,
    fetched_date TEXT
);

-- prospect_fv and player_surplus are views on player_evaluation.
-- They provide backward compatibility with existing queries.
CREATE VIEW IF NOT EXISTS prospect_fv AS
    SELECT player_id, eval_date, fv, fv_str, level, bucket,
           surplus AS prospect_surplus, risk, fv_continuous
    FROM player_evaluation
    WHERE stat_confidence < 0.5 AND fv IS NOT NULL AND age <= 25;

CREATE VIEW IF NOT EXISTS player_surplus AS
    SELECT player_id, eval_date, name, bucket, age,
           composite AS ovr, fv, fv_str, surplus, surplus_yr1,
           level, team_id, parent_team_id
    FROM player_evaluation
    WHERE level = 'MLB';

CREATE TABLE IF NOT EXISTS org_reports (
    team_id     INTEGER,
    report_date TEXT,
    report_md   TEXT,
    PRIMARY KEY (team_id, report_date)
);

CREATE TABLE IF NOT EXISTS player_evaluation (
    player_id       INTEGER,
    eval_date       TEXT,
    name            TEXT,
    bucket          TEXT,
    age             INTEGER,
    level           TEXT,
    team_id         INTEGER,
    parent_team_id  INTEGER,
    composite       INTEGER,
    ceiling         INTEGER,
    fv              INTEGER,
    fv_str          TEXT,
    fv_continuous   REAL,
    risk            TEXT,
    tool_war        REAL,
    stat_war        REAL,
    stat_confidence REAL,
    peak_war        REAL,
    surplus         INTEGER,
    surplus_yr1     INTEGER,
    years_control   INTEGER,
    ctrl_type       TEXT,
    PRIMARY KEY (player_id, eval_date)
);

CREATE VIEW IF NOT EXISTS mlb_batting_stats AS
    SELECT * FROM batting_stats WHERE league_id IS NULL;

CREATE VIEW IF NOT EXISTS mlb_pitching_stats AS
    SELECT * FROM pitching_stats WHERE league_id IS NULL;

CREATE VIEW IF NOT EXISTS mlb_fielding_stats AS
    SELECT * FROM fielding_stats WHERE league_id IS NULL;
"""

# ---------------------------------------------------------------------------
# Migrations (idempotent column additions)
# ---------------------------------------------------------------------------

def _migrate_players(conn: sqlite3.Connection) -> None:
    """Add expanded player fields from StatsPlus API."""
    existing = {row[1] for row in conn.execute("PRAGMA table_info(players)").fetchall()}
    new_cols = [
        ("injury_is_injured", "INTEGER"),
        ("injury_dl_left", "INTEGER"),
        ("injury_left", "INTEGER"),
        ("is_on_dl", "INTEGER"),
        ("is_on_dl60", "INTEGER"),
        ("dl_days_this_year", "INTEGER"),
        ("mlb_service_years", "INTEGER"),
        ("mlb_service_days", "INTEGER"),
        ("mlb_service_days_this_year", "INTEGER"),
        ("pro_service_years", "INTEGER"),
        ("pro_service_days", "INTEGER"),
        ("is_active", "INTEGER"),
        ("is_on_secondary", "INTEGER"),
        ("is_on_waivers", "INTEGER"),
        ("designated_for_assignment", "INTEGER"),
        ("free_agent", "INTEGER"),
        ("was_traded", "INTEGER"),
        ("days_on_waivers", "INTEGER"),
        ("days_on_waivers_left", "INTEGER"),
        ("has_received_arbitration", "INTEGER"),
        ("draft_year", "INTEGER"),
        ("draft_round", "INTEGER"),
        ("draft_pick", "INTEGER"),
        ("draft_overall_pick", "INTEGER"),
        ("draft_team_id", "INTEGER"),
        ("date_of_birth", "TEXT"),
        ("weight", "INTEGER"),
        ("nation_id", "INTEGER"),
        ("uniform_number", "INTEGER"),
    ]
    for col, typ in new_cols:
        if col not in existing:
            conn.execute(f"ALTER TABLE players ADD COLUMN {col} {typ}")


def _migrate_stats_league_id(conn: sqlite3.Connection) -> None:
    """Add league_id column to stat tables for minor league stats."""
    for table in ("batting_stats", "pitching_stats", "fielding_stats"):
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if "league_id" not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN league_id INTEGER")


def _migrate_contracts(conn: sqlite3.Connection) -> None:
    """Add expanded contract fields (options, incentives)."""
    existing = {row[1] for row in conn.execute("PRAGMA table_info(contracts)").fetchall()}
    new_cols = [
        ("last_year_vesting_option", "INTEGER"),
        ("last_year_option_buyout", "INTEGER"),
        ("next_last_year_team_option", "INTEGER"),
        ("next_last_year_player_option", "INTEGER"),
        ("next_last_year_vesting_option", "INTEGER"),
        ("next_last_year_option_buyout", "INTEGER"),
        ("minimum_pa", "INTEGER"),
        ("minimum_pa_bonus", "INTEGER"),
        ("minimum_ip", "INTEGER"),
        ("minimum_ip_bonus", "INTEGER"),
        ("mvp_bonus", "INTEGER"),
        ("cyyoung_bonus", "INTEGER"),
        ("allstar_bonus", "INTEGER"),
    ]
    for col, typ in new_cols:
        if col not in existing:
            conn.execute(f"ALTER TABLE contracts ADD COLUMN {col} {typ}")


def _migrate_misc(conn: sqlite3.Connection) -> None:
    """Miscellaneous column additions across tables."""
    # player_surplus.surplus_yr1 (skip if it's now a view)
    ps_cols = {r[1] for r in conn.execute("PRAGMA table_info(player_surplus)").fetchall()}
    if ps_cols and "surplus_yr1" not in ps_cols:
        conn.execute("ALTER TABLE player_surplus ADD COLUMN surplus_yr1 INTEGER")

    # prospect_fv.fv_continuous and risk (skip if it's now a view)
    pf_cols = {r[1] for r in conn.execute("PRAGMA table_info(prospect_fv)").fetchall()}
    if pf_cols and "fv_continuous" not in pf_cols:
        conn.execute("ALTER TABLE prospect_fv ADD COLUMN fv_continuous REAL")
    if pf_cols and "risk" not in pf_cols:
        conn.execute("ALTER TABLE prospect_fv ADD COLUMN risk TEXT")

    # ratings.true_ceiling
    r_cols = {r[1] for r in conn.execute("PRAGMA table_info(ratings)").fetchall()}
    if "true_ceiling" not in r_cols:
        conn.execute("ALTER TABLE ratings ADD COLUMN true_ceiling INTEGER")

    # contracts.last_year_player_option (base field, not expansion)
    c_cols = {r[1] for r in conn.execute("PRAGMA table_info(contracts)").fetchall()}
    if "last_year_player_option" not in c_cols:
        conn.execute("ALTER TABLE contracts ADD COLUMN last_year_player_option INTEGER")

    # games.runs0/runs1
    g_cols = {r[1] for r in conn.execute("PRAGMA table_info(games)").fetchall()}
    if "runs0" not in g_cols:
        conn.execute("ALTER TABLE games ADD COLUMN runs0 INTEGER")
    if "runs1" not in g_cols:
        conn.execute("ALTER TABLE games ADD COLUMN runs1 INTEGER")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def init_schema(league_dir: Optional[Path] = None) -> None:
    """Initialize database schema and run all migrations.

    Creates tables if they don't exist, adds any missing columns.
    Idempotent — safe to call on every startup.

    Args:
        league_dir: League data directory. If None, uses active league.
    """
    conn = get_connection(league_dir)
    conn.executescript(SCHEMA)
    _migrate_players(conn)
    _migrate_stats_league_id(conn)
    _migrate_contracts(conn)
    _migrate_misc(conn)
    _migrate_ratings(conn)
    _migrate_ratings_history(conn)
    _migrate_ratings_components(conn)
    conn.commit()
    conn.close()


def _migrate_ratings(conn: sqlite3.Connection) -> None:
    """Add columns introduced after the initial ratings schema."""
    existing = {row[1] for row in conn.execute("PRAGMA table_info(ratings)").fetchall()}
    new_cols = [
        "ctrl", "p", "pot_p", "stl_rt", "run", "sac_bunt", "bunt_hit", "hold",
        "babip", "babip_l", "babip_r", "pot_babip",
        "hra", "hra_l", "hra_r", "pot_hra",
        "pbabip", "pbabip_l", "pbabip_r", "pot_pbabip", "prone",
        "composite_score", "ceiling_score", "tool_only_score", "secondary_composite",
        "true_ceiling", "positional_percentile", "positional_median",
        "offensive_grade", "baserunning_value", "defensive_value",
        "durability_score", "offensive_ceiling",
    ]
    for col in new_cols:
        if col not in existing:
            typ = "REAL" if col == "positional_percentile" else (
                "TEXT" if col == "prone" else "INTEGER"
            )
            conn.execute(f"ALTER TABLE ratings ADD COLUMN {col} {typ}")


def _migrate_ratings_history(conn: sqlite3.Connection) -> None:
    """Add columns introduced after the initial ratings_history schema."""
    existing = {row[1] for row in conn.execute("PRAGMA table_info(ratings_history)").fetchall()}
    new_cols = [
        "composite_score", "ceiling_score",
        "offensive_grade", "baserunning_value", "defensive_value",
        "durability_score", "offensive_ceiling",
    ]
    for col in new_cols:
        if col not in existing:
            conn.execute(f"ALTER TABLE ratings_history ADD COLUMN {col} INTEGER")
    # prone is TEXT (H/N/L injury proneness), and is written by the history
    # snapshot. Missing on installs created before it was added to the schema.
    if "prone" not in existing:
        conn.execute("ALTER TABLE ratings_history ADD COLUMN prone TEXT")


def _migrate_ratings_components(conn: sqlite3.Connection) -> None:
    """Add component score columns to ratings and ratings_history."""
    new_cols = ["offensive_grade", "baserunning_value", "defensive_value",
                "durability_score", "offensive_ceiling"]
    for table in ("ratings", "ratings_history"):
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        for col in new_cols:
            if col not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} INTEGER")
