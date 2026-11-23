"""Durable offer tracking without leaking Philadelphia's plan across leagues."""

import json
from types import SimpleNamespace

import pytest
from flask import Flask

import api_routes
from coaching_signings import get_checklist, save_check


@pytest.fixture
def league_dir(tmp_path):
    path = tmp_path / "ppl"
    path.mkdir()
    return path


def test_offers_survive_reload_without_overwriting_other_rows(league_dir):
    plan = get_checklist(league_dir, 6)
    offers = [row for row in plan["entries"] if row["action"] == "offer"]
    assert len(offers) == 3
    assert len({row["id"] for row in plan["entries"]}) == 3
    for row in offers:
        save_check(league_dir, 6, {"plan_id": plan["id"], "entry_id": row["id"], "checked": True})
    save_check(league_dir, 6, {"plan_id": plan["id"], "entry_id": offers[0]["id"], "checked": False})
    checked = {row["id"] for row in get_checklist(league_dir, 6)["entries"] if row["checked"]}
    assert checked == {offers[1]["id"], offers[2]["id"]}


def test_new_revision_preserves_previous_offer_progress(league_dir):
    old_path = league_dir / "config" / "coaching_signing_progress_1956_6.json"
    old_path.parent.mkdir()
    previous = json.dumps({"plan_id": "ppl-pha-coaching-1956", "checked": {"joe-smith-10291904": True}})
    old_path.write_text(previous)
    plan = get_checklist(league_dir, 6)
    assert not any(row["checked"] for row in plan["entries"])
    payload = {"plan_id": plan["id"], "entry_id": plan["entries"][0]["id"], "checked": True}
    with pytest.raises(ValueError, match="current coaching plan"):
        save_check(league_dir, 6, dict(payload, plan_id="ppl-pha-coaching-1956"))
    save_check(league_dir, 6, payload)
    assert old_path.read_text() == previous
    assert get_checklist(league_dir, 6)["entries"][0]["checked"] is True


def test_order_and_retained_staff_match_current_plan(league_dir):
    plan = get_checklist(league_dir, 6)
    rows = plan["entries"]
    assert [row["order"] for row in rows] == [1, 2, 3]
    assert [row["name"] for row in rows] == ["Ryan Baran", "Mike Reszka", "Ben Ruthledge"]
    assert all(row["action"] == "offer" for row in rows)
    seen = set()
    for row in rows:
        assert set(row["depends_on"]) <= seen
        seen.add(row["id"])
    assert rows[0]["team"] == "St. Petersburg (C)"
    assert rows[0]["replaces"] == "Vacant"
    assert rows[1]["team"] == "Dallas (AA)"
    assert rows[1]["replaces"] == "Alex Pastrana"
    assert all(row["depends_on"] == [] for row in rows)
    assert sum(row["annual_demand"] or 0 for row in rows) == 755
    assert rows[0]["annual_demand"] is None and rows[1]["annual_demand"] is None
    assert len(plan["retained_staff"]) == 30
    assert not {"Omar Padilla", "Kevin Knox"} & {r["name"] for r in rows}
    assert {r["name"] for r in plan["retained_staff"]} >= {
        "Brandon Greene", "Steve Eddy", "Joe Bryant", "Dylan Webb", "Jonathan Kemp",
        "Angel Jimenez", "Ryan Harrower", "Steve Kaercher", "Matt Oroark", "Kyle Books",
        "Jason Lee", "Ernie Higbee", "Omar Padilla", "Bryan Benson", "Ben Gardner",
        "Haruhiko Kubo", "Ryan Hayes", "Tim Lovig", "Matt Phillips", "Brian Pedraza"}
    assert not {r["name"] for r in rows} & {r["name"] for r in plan["retained_staff"]}


def test_api_scope_validation_and_persistent_state(league_dir, monkeypatch):
    cfg = SimpleNamespace(league_dir=league_dir)
    monkeypatch.setattr(api_routes, "_get_cfg", lambda: cfg)
    app = Flask(__name__)
    app.register_blueprint(api_routes.api_bp)
    client = app.test_client()
    url = "/api/team/6/coaching-signings"
    response = client.get(url)
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    plan = response.json["checklist"]
    payload = {"plan_id": plan["id"], "entry_id": plan["entries"][0]["id"], "checked": True}
    assert client.post(url, json=payload).status_code == 200
    assert client.get(url).json["checklist"]["entries"][0]["checked"] is True
    for invalid in ([], {}, dict(payload, checked="false"),
                    dict(payload, entry_id="not-a-coach"), dict(payload, plan_id="old-plan")):
        assert client.post(url, json=invalid).status_code == 400
    assert client.get(url).json["checklist"]["entries"][0]["checked"] is True
    assert client.post("/api/team/3/coaching-signings", json=payload).status_code == 404
    cfg.league_dir = league_dir.parent / "emlb"
    assert client.get(url).status_code == 404
    assert client.post(url, json=payload).status_code == 404
    assert not cfg.league_dir.exists()
    assert get_checklist(cfg.league_dir, 6) is None


def test_checklist_still_renders_when_personnel_export_is_empty(league_dir):
    from jinja2 import ChoiceLoader, DictLoader, FileSystemLoader
    from pathlib import Path

    app = Flask(__name__)
    app.jinja_loader = ChoiceLoader([
        DictLoader({"base.html": "{% block content %}{% endblock %}{% block scripts %}{% endblock %}"}),
        FileSystemLoader(Path(__file__).resolve().parents[1] / "web" / "templates"),
    ])
    app.register_blueprint(api_routes.api_bp)
    with app.test_request_context():
        page = app.jinja_env.get_template("coaching.html").render(
            tid=6, team_name="Philadelphia", data={"n_total": 0},
            signing_checklist=get_checklist(league_dir, 6))
    assert page.count('data-entry-id="') == 3
    assert "No personnel data found" in page
    assert "Brooklyn Dodgers: Columbia (Class A), Manager" in page
    assert "Pittsburgh Pirates: Bristol (Rookie League), Hitting Coach" in page
    assert page.index('data-step="1"') < page.index('data-step="2"') < page.index('data-step="3"')
    assert "Restore your current saved staff first" in page
    assert "No new promotions are recommended" in page
    assert "coaching_signings.js" in page
