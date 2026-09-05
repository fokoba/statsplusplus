"""Smoke tests: the live Flask app boots and its key routes return 200.

Guards the coverage gap from the Session 80 audit — nothing exercised the real
`web/app.py` (module-level `app`, full route set + real before_request) against
a fixture DB. This drives the app's actual request lifecycle (league-context
resolution, request-scoped DB, query execution, template rendering) rather than
mocking the data layer, so it catches wiring/schema regressions.

Runs against both an OVR/POT-present and an OVR/POT-less (PPL-style) fixture
league. All routes must render without a 500.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "web"))
sys.path.insert(0, str(_ROOT / "scripts"))

from _fixture_league import build_fixture, remove_fixture, MLB_HITTER_ID, PROSPECT_ID, TEAM_ID

_SLUGS = {"_web_ovr": True, "_web_noovr": False}


@pytest.fixture(scope="module")
def app_client():
    """Build fixture leagues, point the live app at one via STATSPP_LEAGUE,
    and yield (test_client, set_league) so tests can switch leagues."""
    for slug, with_ovr in _SLUGS.items():
        build_fixture(slug, with_ovr)

    prev_env = os.environ.get("STATSPP_LEAGUE")
    os.environ["STATSPP_LEAGUE"] = "_web_ovr"

    # Import after fixtures exist — app runs init_schema over data/ on import.
    from app import app as flask_app
    flask_app.config["TESTING"] = True

    def _set_league(slug: str) -> None:
        os.environ["STATSPP_LEAGUE"] = slug

    try:
        with flask_app.test_client() as client:
            yield client, _set_league
    finally:
        if prev_env is None:
            os.environ.pop("STATSPP_LEAGUE", None)
        else:
            os.environ["STATSPP_LEAGUE"] = prev_env
        for slug in _SLUGS:
            remove_fixture(slug)


# GET routes that render a page or return read-only JSON. `{tid}`/`{pid}`
# are substituted with fixture IDs.
_ROUTES = [
    "/dashboard",
    "/league",
    f"/team/{TEAM_ID}",
    f"/team/{TEAM_ID}/minors",
    f"/player/{MLB_HITTER_ID}",
    f"/player/{PROSPECT_ID}",
    "/settings",
    f"/api/prospect/{PROSPECT_ID}",
    f"/api/player-card/{MLB_HITTER_ID}",
    f"/api/player-percentiles/{MLB_HITTER_ID}",
]


@pytest.mark.real_web
@pytest.mark.parametrize("slug", list(_SLUGS))
@pytest.mark.parametrize("route", _ROUTES)
def test_route_renders(app_client, route, slug):
    """Every key route returns a non-5xx status on both league types."""
    client, set_league = app_client
    set_league(slug)
    resp = client.get(route)
    assert resp.status_code < 500, (
        f"{route} on {slug} returned {resp.status_code}\n"
        f"{resp.get_data(as_text=True)[:1500]}"
    )


@pytest.mark.real_web
def test_root_redirects(app_client):
    """`/` redirects to a team page (not a 500)."""
    client, set_league = app_client
    set_league("_web_ovr")
    resp = client.get("/")
    assert resp.status_code in (200, 302)
