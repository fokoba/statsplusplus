"""Tests for the phase-aware Offseason page (web/offseason_queries.py + routes).

Covers the logic that had real bugs during development:
  - market board = actual unsigned FAs only (team_id=0, free_agent=1)
  - foreign-league players (no stats in THIS league, e.g. NPB) excluded
  - "fills a need" requires need-position AND an upgrade over the incumbent
  - Proj WAR matches the player valuation page (shared compute_player_value)
  - phase gating (which panels show for a given sub-phase)
  - toggle / set-phase endpoints persist to state.json
  - /offseason renders (route smoke) on both fixture league types
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "web"))
sys.path.insert(0, str(_ROOT / "scripts"))

from _fixture_league import build_fixture, remove_fixture, EVAL_DATE, TEAM_ID, YEAR
from statsplusplus.data.db import get_connection

_SLUG = "_web_offseason"


def _add_offseason_entities(league_dir):
    """Add free agents (signable + a foreign-league one) and an arb-eligible
    player to the base fixture so the offseason panels have data to work on."""
    conn = get_connection(league_dir)

    def _ins(table, **cols):
        keys = ",".join(cols)
        conn.execute(f"INSERT OR REPLACE INTO {table} ({keys}) "
                     f"VALUES ({','.join('?' * len(cols))})", list(cols.values()))

    # 300: signable FA — unsigned, has prior stats in this league, strong 1B.
    # 301: foreign-league FA — unsigned, NO stats in this league (should be
    #      excluded from the market board even though free_agent=1).
    # 302: weak unsigned FA at SS — real FA but below any incumbent.
    _ins("players", player_id=300, name="Frank Freeagent", age=29, team_id=0,
         parent_team_id=0, level="1", pos=3, role=0, free_agent=1)
    _ins("players", player_id=301, name="Kenji Foreign", age=28, team_id=0,
         parent_team_id=0, level="0", pos=3, role=0, free_agent=1)
    _ins("players", player_id=302, name="Wally Weak", age=33, team_id=0,
         parent_team_id=0, level="1", pos=6, role=0, free_agent=1)

    # composite_score/ceiling_score drive get_market_board's composite/ceiling
    # (sourced from latest_ratings — this fork doesn't use player_evaluation).
    for pid, comp, ceil in ((300, 58, 58), (301, 62, 62), (302, 48, 48)):
        _ins("ratings", player_id=pid, snapshot_date=EVAL_DATE, ovr=comp, pot=ceil,
             composite_score=comp, ceiling_score=ceil, league_id=1, bats="R", throws="R")

    # bucket comes from prospect_fv (fv_calc.py writes free agents there too,
    # with level_label="FA") — fv/surplus values are unused by the market board.
    for pid, bkt in ((300, "1B"), (301, "1B"), (302, "SS")):
        _ins("prospect_fv", player_id=pid, eval_date=EVAL_DATE, fv=50, fv_str="50",
             level="FA", bucket=bkt, prospect_surplus=0, risk="Medium", fv_continuous=50.0)

    # Prior stats in THIS league only for the signable FAs (300, 302) — 301 has none.
    _ins("batting_stats", player_id=300, year=2033, team_id=0, split_id=1,
         pa=500, ab=450, h=135, hr=20, rbi=75, bb=45, k=90, avg=0.300, obp=0.365, slg=0.520, war=3.0)
    _ins("batting_stats", player_id=302, year=2033, team_id=0, split_id=1,
         pa=300, ab=280, h=64, hr=4, rbi=25, bb=18, k=70, avg=0.229, obp=0.280, slg=0.320, war=-0.4)

    # 400: on-roster player (team 1) with a TEAM OPTION in his final contract
    # year — exercises the Contract Options panel. get_option_decisions reads
    # composite/ceiling/bucket from player_surplus (this fork's table, not
    # upstream's player_evaluation) — same as get_arbitration/get_extension_candidates.
    _ins("players", player_id=400, name="Opt Guy", age=30, team_id=TEAM_ID,
         parent_team_id=0, level="1", pos=8, role=0)
    _ins("ratings", player_id=400, snapshot_date=EVAL_DATE, ovr=55, pot=55,
         composite_score=56, ceiling_score=60, league_id=1, bats="R", throws="R")
    _ins("player_surplus", player_id=400, eval_date=EVAL_DATE, name="Opt Guy",
         bucket="CF", age=30, ovr=56, fv=55, fv_str="55",
         surplus=0, surplus_yr1=0, level="MLB",
         team_id=TEAM_ID, parent_team_id=0)
    _ins("batting_stats", player_id=400, year=2033, team_id=TEAM_ID, split_id=1,
         pa=550, ab=500, h=145, hr=22, rbi=80, bb=45, k=100, avg=0.290, obp=0.360, slg=0.500, war=3.0)
    # 2-year deal, currently in year 0; final year (yr 1) is a team option @ 8M, buyout 1M.
    _ins("contracts", player_id=400, team_id=TEAM_ID, contract_team_id=TEAM_ID,
         is_major=1, season_year=YEAR, years=2, current_year=0,
         salary_0=6_000_000, salary_1=8_000_000,
         last_year_team_option=1, last_year_option_buyout=1_000_000)
    conn.commit()
    conn.close()


@pytest.fixture(scope="module")
def league():
    build_fixture(_SLUG, with_ovr=True)
    from statsplusplus.config.league_context import get_league_dir
    _add_offseason_entities(get_league_dir(_SLUG))
    prev = os.environ.get("STATSPP_LEAGUE")
    os.environ["STATSPP_LEAGUE"] = _SLUG
    try:
        yield _SLUG
    finally:
        if prev is None:
            os.environ.pop("STATSPP_LEAGUE", None)
        else:
            os.environ["STATSPP_LEAGUE"] = prev
        remove_fixture(_SLUG)


@pytest.fixture()
def q(league, monkeypatch):
    """offseason_queries wired to the fixture league via patched accessors —
    avoids the live-app request context and its fixture-ordering fragility."""
    import offseason_queries as osq
    from statsplusplus.config.league_config import LeagueConfig
    from statsplusplus.config.league_context import get_league_dir
    from statsplusplus.data.db import get_connection
    ld = get_league_dir(_SLUG)
    cfg = LeagueConfig(base_dir=ld)
    conn = get_connection(ld)
    monkeypatch.setattr(osq, "get_db", lambda: conn)
    monkeypatch.setattr(osq, "get_cfg", lambda: cfg)
    # _team_need_positions imports get_draft_org_depth from team_queries, which
    # also uses get_db/get_cfg from web_league_context — patch there too.
    import web_league_context as wlc
    monkeypatch.setattr(wlc, "get_db", lambda: conn)
    monkeypatch.setattr(wlc, "get_cfg", lambda: cfg)
    yield osq
    conn.close()


# ---------------------------------------------------------------------------
# Market board
# ---------------------------------------------------------------------------

def test_market_board_only_unsigned(q):
    board = q.get_market_board(TEAM_ID, limit=100)
    names = {p["name"] for p in board}
    assert "Frank Freeagent" in names  # unsigned + has league stats
    assert "Joe Hitter" not in names and "Rival Bat" not in names


def test_foreign_league_player_excluded(q):
    """A free_agent=1 player with no stats in THIS league (NPB-style) must not
    appear on the market board."""
    board = q.get_market_board(TEAM_ID, limit=100)
    assert "Kenji Foreign" not in {p["name"] for p in board}


def test_proj_war_matches_player_value(q):
    """Board Proj WAR == the shared compute_player_value breakdown year-1 WAR
    (single source of truth, not a re-derived approximation)."""
    from statsplusplus.evaluation.player_value import compute_player_value
    from statsplusplus.evaluation.constants import load_model_weights
    from statsplusplus.config.league_config import dollars_per_war, league_minimum
    from statsplusplus.config.league_context import get_league_dir
    ld = get_league_dir(_SLUG)

    board = {p["name"]: p for p in q.get_market_board(TEAM_ID, limit=100)}
    frank = board["Frank Freeagent"]
    res = compute_player_value(
        fv_continuous=0.0, bucket="1B", age=29, level="MLB",
        composite=58, ceiling=58, career_pa=500, career_ip=0.0, stat_war=3.0,
        years_control=1, salaries=None,
        dpw=dollars_per_war(ld), min_sal=league_minimum(ld),
        weights=load_model_weights(ld))
    assert frank["proj_war"] == round(res["breakdown"][0]["war"], 1)


def test_need_flag_requires_upgrade(q):
    """A weak FA at a need position is NOT flagged (wouldn't move the needle)."""
    board = {p["name"]: p for p in q.get_market_board(TEAM_ID, limit=100)}
    if "Wally Weak" in board:
        assert board["Wally Weak"]["fills_need"] is False


def test_market_board_carries_recommended_contract(q):
    """Each FA row has a value-based recommended contract for the cart draw-down
    (aav/years/total), aav == max(proj_war,0) × $/WAR."""
    from statsplusplus.config.league_config import dollars_per_war
    from statsplusplus.config.league_context import get_league_dir
    dpw = dollars_per_war(get_league_dir(_SLUG))
    frank = {p["name"]: p for p in q.get_market_board(TEAM_ID, limit=100)}["Frank Freeagent"]
    assert "rec_aav" in frank and "rec_years" in frank and "rec_total" in frank
    expected_aav = round(max(frank["proj_war"] or 0, 0) * dpw)
    assert frank["rec_aav"] == expected_aav
    assert frank["rec_total"] == frank["rec_aav"] * frank["rec_years"]
    assert frank["rec_aav"] >= 0


# ---------------------------------------------------------------------------
# Phase gating
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("phase,expect", [
    ("", {"arbitration": True, "options": True, "free_agency": True, "extensions": True}),
    ("arbitration", {"arbitration": True, "options": False, "free_agency": False, "extensions": False}),
    ("options", {"arbitration": False, "options": True, "free_agency": False, "extensions": True}),
    ("free_agency", {"arbitration": False, "options": False, "free_agency": True, "extensions": True}),
    ("rule5", {"arbitration": False, "options": False, "free_agency": False, "extensions": False}),
])
def test_phase_panel_gating(phase, expect):
    """panels_for_phase surfaces only the panels relevant to the phase."""
    import offseason_queries as osq
    assert osq.panels_for_phase(phase) == expect


# ---------------------------------------------------------------------------
# Contract options
# ---------------------------------------------------------------------------

def test_option_decisions_team_option_recommendation(q):
    """A team option is surfaced with an exercise/decline recommendation from
    the shared valuation model, in the correctly-timed bucket."""
    res = q.get_option_decisions(TEAM_ID)
    # Fixture player 400: signed 2033, 2yr → option year 2034 == game_year+1 →
    # "this offseason".
    opt = {o["name"]: o for o in res["this_offseason"]}
    assert "Opt Guy" in opt
    g = opt["Opt Guy"]
    assert g["type"] == "Team"
    assert g["year"] == YEAR + 1          # 2034, derived from season_year+offset
    assert g["option_salary"] == 8_000_000
    assert g["buyout"] == 1_000_000
    assert g["rec"] in ("Exercise", "Exercise (marginal)", "Decline")
    assert g["proj_value"] is not None


def test_option_year_derived_from_contract_not_game_year(q):
    """The option year comes from season_year + offset, not the game year —
    guards the off-by-one that mislabeled decision timing."""
    res = q.get_option_decisions(TEAM_ID)
    allopts = res["this_offseason"] + res["upcoming"]
    g = {o["name"]: o for o in allopts}["Opt Guy"]
    assert g["year"] == YEAR + 1  # season_year(2033) + (years-1=1) = 2034


def test_option_decisions_empty_when_none(q):
    """A team with no option contracts returns empty buckets (team 2 has none)."""
    res = q.get_option_decisions(2)
    assert res == {"this_offseason": [], "upcoming": []}


def test_set_phase_persists(league, monkeypatch):
    """The set-offseason-phase endpoint writes the phase to state.json and
    rejects unknown phases. Exercised via the blueprint function with a patched
    config, avoiding a shared-app import (keeps test isolation)."""
    import api_routes
    from statsplusplus.config.league_config import LeagueConfig
    from statsplusplus.config.league_context import get_league_dir
    ld = get_league_dir(_SLUG)
    cfg = LeagueConfig(base_dir=ld)
    monkeypatch.setattr(api_routes, "_get_cfg", lambda: cfg)

    from flask import Flask
    probe = Flask(__name__)
    probe.register_blueprint(api_routes.api_bp)
    with probe.test_client() as c:
        r = c.post("/api/set-offseason-phase", json={"phase": "free_agency"})
        assert r.get_json()["ok"] is True
        assert json.loads((ld / "config" / "state.json").read_text())["offseason_phase"] == "free_agency"
        assert c.post("/api/set-offseason-phase", json={"phase": "bogus"}).status_code == 400
        c.post("/api/set-offseason-phase", json={"phase": ""})


def test_toggle_offseason_persists(league, monkeypatch):
    import api_routes
    from statsplusplus.config.league_config import LeagueConfig
    from statsplusplus.config.league_context import get_league_dir
    ld = get_league_dir(_SLUG)
    cfg = LeagueConfig(base_dir=ld)
    monkeypatch.setattr(api_routes, "_get_cfg", lambda: cfg)

    from flask import Flask
    probe = Flask(__name__)
    probe.register_blueprint(api_routes.api_bp)
    with probe.test_client() as c:
        first = c.post("/api/toggle-offseason").get_json()["offseason_mode"]
        assert json.loads((ld / "config" / "state.json").read_text())["offseason_mode"] == first
        assert c.post("/api/toggle-offseason").get_json()["offseason_mode"] is not first


# ---------------------------------------------------------------------------
# Finance settings (offseason budget) — routes + derived available figure
# ---------------------------------------------------------------------------

def _finance_client(monkeypatch):
    """A probe app + patched accessors so the finance routes resolve the
    fixture league's team and config (no shared live app)."""
    import api_routes
    import queries
    from statsplusplus.config.league_config import LeagueConfig
    from statsplusplus.config.league_context import get_league_dir

    ld = get_league_dir(_SLUG)
    cfg = LeagueConfig(base_dir=ld)
    # get_my_team_id() -> queries.get_cfg().my_team_id
    monkeypatch.setattr(queries, "get_cfg", lambda: cfg)

    from flask import Flask
    probe = Flask(__name__)
    probe.register_blueprint(api_routes.api_bp)
    return probe.test_client(), ld


def test_finance_get_defaults(league, monkeypatch):
    """GET returns v2 defaults for a league with no finance_settings.json yet."""
    c, ld = _finance_client(monkeypatch)
    try:
        d = c.get("/api/finance-settings").get_json()
        assert d["ok"] is True
        assert d["settings"]["fa_budget"] is None
        assert d["available"] is None  # no budget entered → nothing to derive
    finally:
        (ld / "config" / "finance_settings.json").unlink(missing_ok=True)


def test_finance_post_then_derive(league, monkeypatch):
    """POST persists the game's FA/extension figures and echoes available."""
    c, ld = _finance_client(monkeypatch)
    try:
        r = c.post("/api/finance-settings", json={"settings": {
            "fa_budget": 1_232_320, "ext_budget": 1_401_880}})
        d = r.get_json()
        assert d["ok"] is True
        assert d["available"] == 1_232_320  # no cart spend yet
        saved = json.loads((ld / "config" / "finance_settings.json").read_text())
        assert saved["fa_budget"] == 1_232_320
        assert saved["ext_budget"] == 1_401_880
    finally:
        (ld / "config" / "finance_settings.json").unlink(missing_ok=True)


def test_finance_post_missing_settings_400(league, monkeypatch):
    c, ld = _finance_client(monkeypatch)
    assert c.post("/api/finance-settings", json={}).status_code == 400
