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
    # Per-player latest snapshot, NOT a single global MAX(snapshot_date) —
    # confirmed bug (2026-09-28): a single stray row written at a newer date
    # than the rest of the table (e.g. one player picked up individually by
    # an incremental sync after the last full-batch import) made the old
    # global-MAX version of this view return just that one row, silently
    # emptying every page built on latest_ratings (Free Agent Adds showed
    # 0 candidates across every category despite a real 1,045-player
    # signable pool). Drop+recreate on every connection since the view
    # needs updating even where CREATE VIEW IF NOT EXISTS already fired
    # with the old (buggy) definition on a previous connection.
    conn.execute("DROP VIEW IF EXISTS latest_ratings")
    conn.execute(
        "CREATE VIEW latest_ratings AS "
        "SELECT r.* FROM ratings r "
        "WHERE r.snapshot_date = (SELECT MAX(r2.snapshot_date) FROM ratings r2 "
        "WHERE r2.player_id = r.player_id)"
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
    organization_id INTEGER,
    player_league_id INTEGER,
    level          TEXT,
    pos            INTEGER,
    role           INTEGER,
    retired        INTEGER,
    free_agent     INTEGER,
    is_on_waivers  INTEGER,
    nation_id      INTEGER,
    draft_eligible INTEGER,
    draft_team_id  INTEGER
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

CREATE TABLE IF NOT EXISTS prospect_fv (
    player_id       INTEGER,
    eval_date       TEXT,
    fv              INTEGER,
    fv_str          TEXT,
    level           TEXT,
    bucket          TEXT,
    prospect_surplus INTEGER,
    risk            TEXT,
    fv_continuous   REAL,
    PRIMARY KEY (player_id, eval_date)
);

-- Development-speed metric (ported from upstream tfalsone/statsplusplus 886ee50).
-- A separate axis from FV/surplus by design — displayed adjacent, not blended.
-- Rebuilt each fv_calc run. `available=0` = below the history/reporting gate.
CREATE TABLE IF NOT EXISTS dev_speed (
    player_id   INTEGER,
    eval_date   TEXT,
    available   INTEGER,
    z           REAL,
    signal      TEXT,      -- 'off' (bat, hitters) | 'comp' (pitchers)
    label       TEXT,
    css_class   TEXT,      -- rising|onpace|watch|stalled|regressing|none
    note        TEXT,
    gap         INTEGER,
    d_ovr       INTEGER,
    d_pot       INTEGER,
    confidence  TEXT,      -- High|Medium|Low
    annual_move REAL,
    peer_mean   REAL,
    peer_sd     REAL,
    peer_n      INTEGER,
    comp_first  INTEGER, comp_last INTEGER,
    off_first   INTEGER, off_last INTEGER,
    def_first   INTEGER, def_last INTEGER,
    window_years REAL,
    n_snaps     INTEGER,
    -- Schedule/risk tag (2026-09-29) — a SECOND, independent signal
    -- alongside the z-score label above, not a replacement: label/css_class
    -- answer "is he outpacing peers right now"; schedule_status answers "is
    -- he actually closing his own gap to ceiling before his runway to peak
    -- age runs out, across multiple tools, not just one." See dev_speed.py.
    schedule_status TEXT,   -- ahead|behind|at_risk|on_track|NULL(not reportable)
    schedule_label  TEXT,
    schedule_note   TEXT,
    stagnant_tools  TEXT,   -- comma-joined tool keys with ~0 movement in-window
    gap_closed_pct_yr REAL, -- fraction of the window-start gap closed per year
    years_to_peak   REAL,
    gap_smoothed    REAL,   -- trailing-average gap (vs point-in-time `gap`)
    gap_trend       REAL,   -- annualized change in smoothed gap; negative = closing
    PRIMARY KEY (player_id, eval_date)
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

CREATE TABLE IF NOT EXISTS org_reports (
    team_id     INTEGER,
    report_date TEXT,
    report_md   TEXT,
    PRIMARY KEY (team_id, report_date)
);

-- Free agent salary demands ("asking price"), imported from a manually
-- uploaded OOTP "All Free Agents" export — the live statsplus.net sync has
-- no such field at all, so this is the only source for it.
CREATE TABLE IF NOT EXISTS fa_asking_prices (
    player_id   INTEGER PRIMARY KEY,
    ask_raw     TEXT,
    uploaded_at TEXT,
    changed_at  TEXT
);

-- Signing bonus demands for draft-eligible amateurs, imported from an
-- uploaded OOTP draft pool "All Columns" export's DEM column (e.g. "$10k",
-- "Slot", "Impos."). Distinct from fa_asking_prices, which is a current
-- free agent's ongoing salary ask — this is a one-time up-front bonus cost
-- to sign an amateur, used to compare against a prospect's multi-year
-- surplus value on the Draft board.
CREATE TABLE IF NOT EXISTS draft_bonus_asks (
    player_id    INTEGER PRIMARY KEY,
    ask_raw      TEXT,
    ask_dollars  REAL,
    uploaded_at  TEXT
);

-- Real per-year salary/arbitration figures, imported from a manually
-- uploaded OOTP "Team Salary" export. The game computes its own arbitration
-- renewal estimate (mostly driven by current-year performance) that we can
-- only approximate with our own formula — this table lets a real, exact
-- figure override that formula guess wherever the export covers it.
CREATE TABLE IF NOT EXISTS salary_estimates (
    player_id   INTEGER,
    year        INTEGER,
    amount      INTEGER,
    marker      TEXT,
    uploaded_at TEXT,
    PRIMARY KEY (player_id, year)
);

-- Personality archetype + adaptability, imported from a manually uploaded
-- OOTP "All Columns" export via Custom Upload. The sanctioned StatsPlus API
-- refresh does NOT expose these two fields at all (confirmed against the
-- documented ratings CSV formats and the API client) — every other
-- personality trait (Int/WrkEthic/Greed/Loy/Lead) comes through the API
-- refresh live and needs no persistence here, but personality_type ("Type")
-- and adaptability ("AD") would otherwise revert to NULL on every single
-- refresh, since a fresh API-driven ratings row never sets them. Keyed by
-- player_id only (not snapshot_date) so it survives every refresh; a
-- post-refresh backfill step (_upsert_ratings) copies these onto each new
-- snapshot row. uploaded_at also powers the "last valid upload" reminder
-- on the Custom Upload page.
CREATE TABLE IF NOT EXISTS personality_overrides (
    player_id        INTEGER PRIMARY KEY,
    personality_type TEXT,
    adaptability     TEXT,
    uploaded_at      TEXT
);

-- Rule 5 draft eligibility, imported from a manually uploaded OOTP
-- "Rule 5 Draft Eligible" export. Not derivable from synced data — real
-- Rule 5 status depends on internal roster-protection history the live
-- sync doesn't expose, so this is the only reliable source for it.
CREATE TABLE IF NOT EXISTS rule5_eligible (
    player_id   INTEGER PRIMARY KEY,
    uploaded_at TEXT
);

-- Manual depth-chart role designations (Everyday Starter / Platoon vR /
-- Platoon vL / Bench), set by the user on the team page's Depth Chart Roles
-- tab. Only applied to the current year of the 3-year depth chart projection
-- (future years keep using the automatic WAR-ranked allocation, since roles
-- may change as players age/depart). A position with no rows here falls
-- back entirely to the automatic algorithm in allocate_playing_time().
-- share: optional explicit playing-time fraction (0-1) for pitcher roles
-- (position 'SP'/'RP'), used verbatim instead of the automatic split when
-- set. NULL for batting-position rows, and NULL for pitcher rows where the
-- user only specified a role tier (e.g. "long_relief") and wants the
-- automatic within-tier weighting.
CREATE TABLE IF NOT EXISTS depth_chart_roles (
    team_id     INTEGER NOT NULL,
    position    TEXT NOT NULL,
    player_id   INTEGER NOT NULL,
    role        TEXT NOT NULL,
    share       REAL,
    updated_at  TEXT,
    PRIMARY KEY (team_id, position, player_id)
);

-- League-wide park factors (every team's home park), imported from a
-- manually uploaded OOTP park-info export. Only the active team's own
-- park is available any other way (config/park_factors.json) — this
-- table is what makes an AWAY-game park selectable in Lineup Optimizer.
CREATE TABLE IF NOT EXISTS league_park_factors (
    team_id     INTEGER PRIMARY KEY,
    park        TEXT,
    avg         REAL, avg_l  REAL, avg_r  REAL,
    hr          REAL, hr_l   REAL, hr_r   REAL,
    doubles     REAL, triples REAL,
    overall     REAL,
    uploaded_at TEXT
);

CREATE TABLE IF NOT EXISTS league_meta (
    id                INTEGER PRIMARY KEY CHECK (id = 1),
    primary_league_id INTEGER
);

-- MLB views: primary league only (excludes co-resident leagues such as NPB).
CREATE VIEW IF NOT EXISTS mlb_batting_stats AS
    SELECT b.* FROM batting_stats b
    WHERE b.league_id IS NULL
      AND (
        NOT EXISTS (SELECT 1 FROM league_meta WHERE primary_league_id IS NOT NULL)
        OR b.player_id IN (
            SELECT p.player_id FROM players p
            WHERE p.player_league_id IS NULL
               OR p.player_league_id = (SELECT primary_league_id FROM league_meta WHERE id = 1)
        )
      );

CREATE VIEW IF NOT EXISTS mlb_pitching_stats AS
    SELECT ps.* FROM pitching_stats ps
    WHERE ps.league_id IS NULL
      AND (
        NOT EXISTS (SELECT 1 FROM league_meta WHERE primary_league_id IS NOT NULL)
        OR ps.player_id IN (
            SELECT p.player_id FROM players p
            WHERE p.player_league_id IS NULL
               OR p.player_league_id = (SELECT primary_league_id FROM league_meta WHERE id = 1)
        )
      );

CREATE VIEW IF NOT EXISTS mlb_fielding_stats AS
    SELECT fs.* FROM fielding_stats fs
    WHERE fs.league_id IS NULL
      AND (
        NOT EXISTS (SELECT 1 FROM league_meta WHERE primary_league_id IS NOT NULL)
        OR fs.player_id IN (
            SELECT p.player_id FROM players p
            WHERE p.player_league_id IS NULL
               OR p.player_league_id = (SELECT primary_league_id FROM league_meta WHERE id = 1)
        )
      );
"""


# ---------------------------------------------------------------------------
# Org attribution
# ---------------------------------------------------------------------------
#
# The organization a player belongs to. StatsPlus added `Organization ID` in
# April 2026 as the reliable org key — OOTP sometimes leaves `Parent Team ID`
# unset (0) for major-league players, but `Organization ID` is still populated.
# Resolution order: Organization ID → Parent Team ID → Team ID (the club he is
# on right now). All three fall through 0 (OOTP writes 0, not NULL, for "none").
#
# `p` is the required table alias for the `players` row in the query.
ORG_ID_SQL = "COALESCE(NULLIF(p.organization_id,0), NULLIF(p.parent_team_id,0), p.team_id)"


def primary_league_predicate(primary_league_id, alias: str = "p"):
    """SQL predicate restricting a `players` row (aliased ``alias``) to the
    primary MLB league — excludes co-resident top-level leagues like NPB.

    Returns ``(clause, params)`` for splicing into a WHERE (parameterized, no
    string interpolation of the id).

    - ``primary_league_id is None`` (single-top-league universe / older DB) →
      ``("1=1", [])`` — an always-true no-op, so non-multi-league leagues
      (eMLB/vMLB) are completely unaffected.
    - Otherwise → keep players whose league id IS the primary OR IS NULL. The
      NULL allowance preserves backward compat (older data / single-league DBs
      where the field isn't populated). See task_list: revisit the NULL
      allowance (a handful of level-1 players with NULL league id slip through).
    """
    if primary_league_id is None:
        return "1=1", []
    return (f"({alias}.player_league_id = ? OR {alias}.player_league_id IS NULL)",
            [primary_league_id])


# ---------------------------------------------------------------------------
# Migrations (idempotent column additions)
# ---------------------------------------------------------------------------

def get_conn(league_dir: Path | None = None) -> sqlite3.Connection:
    """Legacy alias for get_connection() — several call sites (onboarding,
    settings routes, fv_calc) still use this name."""
    return get_connection(league_dir)


def _migrate_players(conn: sqlite3.Connection) -> None:
    """Add expanded player fields from StatsPlus API."""
    existing = {row[1] for row in conn.execute("PRAGMA table_info(players)").fetchall()}
    new_cols = [
        ("organization_id", "INTEGER"),
        ("player_league_id", "INTEGER"),
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
        ("years_protected_from_rule_5", "INTEGER"),
        ("draft_eligible", "INTEGER"),
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
    # player_surplus.surplus_yr1
    ps_cols = {r[1] for r in conn.execute("PRAGMA table_info(player_surplus)").fetchall()}
    if "surplus_yr1" not in ps_cols:
        conn.execute("ALTER TABLE player_surplus ADD COLUMN surplus_yr1 INTEGER")

    # prospect_fv.fv_continuous and risk
    pf_cols = {r[1] for r in conn.execute("PRAGMA table_info(prospect_fv)").fetchall()}
    if "fv_continuous" not in pf_cols:
        conn.execute("ALTER TABLE prospect_fv ADD COLUMN fv_continuous REAL")
    if "risk" not in pf_cols:
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

    # fa_asking_prices.changed_at — separate from uploaded_at (bumped on every
    # upload regardless) so the "last valid upload" reminder reflects when an
    # ask actually moved, not just when the file was re-uploaded unchanged.
    fa_cols = {r[1] for r in conn.execute("PRAGMA table_info(fa_asking_prices)").fetchall()}
    if "changed_at" not in fa_cols:
        conn.execute("ALTER TABLE fa_asking_prices ADD COLUMN changed_at TEXT")
        # Backfill: rows written before this column existed have no real
        # change history to recover, so uploaded_at is the best available
        # proxy — without this the "last valid upload" reminder would show
        # nothing at all until the next actual price change.
        conn.execute("UPDATE fa_asking_prices SET changed_at = uploaded_at WHERE changed_at IS NULL")

    # players.retired — not part of the base schema or _migrate_players'
    # StatsPlus-API column set, but used by our own free-agent tracking.
    p_cols = {r[1] for r in conn.execute("PRAGMA table_info(players)").fetchall()}
    if "retired" not in p_cols:
        conn.execute("ALTER TABLE players ADD COLUMN retired INTEGER")

    # depth_chart_roles.share — added for manual pitcher role overrides
    # (explicit IP-share pins for SP/RP), see the table's own comment.
    dcr_cols = {r[1] for r in conn.execute("PRAGMA table_info(depth_chart_roles)").fetchall()}
    if "share" not in dcr_cols:
        conn.execute("ALTER TABLE depth_chart_roles ADD COLUMN share REAL")


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
    for _v in ("mlb_batting_stats", "mlb_pitching_stats", "mlb_fielding_stats"):
        conn.execute(f"DROP VIEW IF EXISTS {_v}")
    conn.executescript(SCHEMA)
    _migrate_ratings(conn)
    _migrate_ratings_history(conn)
    _migrate_ratings_components(conn)
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
        "personality_type", "adaptability",
    ]
    for col in new_cols:
        if col not in existing:
            typ = "REAL" if col == "positional_percentile" else (
                "TEXT" if col in ("prone", "personality_type", "adaptability") else "INTEGER"
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
    # true_ceiling was already computed every evaluation cycle and written to
    # `ratings`, but never to `ratings_history` — dev_speed.py had no
    # historical ceiling series to work from other than the more
    # conservative ceiling_score, an inconsistency with the rest of the app
    # (contract_value._resolve prefers true_ceiling). Rows written before
    # this migration stay NULL here; history accumulates from now on.
    existing = {row[1] for row in conn.execute("PRAGMA table_info(ratings_history)").fetchall()}
    if "true_ceiling" not in existing:
        conn.execute("ALTER TABLE ratings_history ADD COLUMN true_ceiling INTEGER")

    # dev_speed's schedule/risk tag columns (2026-09-29) — table is fully
    # derived/recomputed on every fv_calc run, so a plain ALTER (not a
    # rebuild) is safe; old rows just carry NULL until the next recompute.
    # Guard on the table actually existing — some callers (tests) run this
    # migration function against a minimal schema without dev_speed at all.
    has_table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='dev_speed'"
    ).fetchone()
    if has_table:
        existing = {row[1] for row in conn.execute("PRAGMA table_info(dev_speed)").fetchall()}
        for col, typ in [("schedule_status", "TEXT"), ("schedule_label", "TEXT"),
                          ("schedule_note", "TEXT"), ("stagnant_tools", "TEXT"),
                          ("gap_closed_pct_yr", "REAL"), ("years_to_peak", "REAL"),
                          ("gap_smoothed", "REAL"), ("gap_trend", "REAL")]:
            if col not in existing:
                conn.execute(f"ALTER TABLE dev_speed ADD COLUMN {col} {typ}")
