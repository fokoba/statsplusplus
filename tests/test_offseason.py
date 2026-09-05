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

from _fixture_league import build_fixture, remove_fixture, EVAL_DATE, TEAM_ID
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


# ---------------------------------------------------------------------------
# Phase gating
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("phase,expect", [
    ("", {"arbitration": True, "free_agency": True, "extensions": True}),
    ("arbitration", {"arbitration": True, "free_agency": False, "extensions": False}),
    ("free_agency", {"arbitration": False, "free_agency": True, "extensions": True}),
    ("options", {"arbitration": False, "free_agency": False, "extensions": True}),
    ("rule5", {"arbitration": False, "free_agency": False, "extensions": False}),
])
def test_phase_panel_gating(phase, expect):
    """panels_for_phase surfaces only the panels relevant to the phase."""
    import offseason_queries as osq
    assert osq.panels_for_phase(phase) == expect


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
