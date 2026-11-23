"""Philadelphia's agreed 1956 coaching checklist and league-local progress."""

import json
import re
from pathlib import Path
from threading import Lock

from statsplusplus.config.league_context import atomic_write_text

_MANIFEST = Path(__file__).parent / "data" / "coaching_signings_ppl_1956.json"
_SAVE_LOCK = Lock()


def _plan(league_dir, team_id):
    if Path(league_dir).name.lower() != "ppl" or team_id != 6:
        return None
    return json.loads(_MANIFEST.read_text())


def _state_path(league_dir, plan):
    # Each revision starts with independent checks; preserve earlier offers.
    revision = plan.get("progress_revision", "")
    if revision and not re.fullmatch(r"[a-z0-9-]+", revision):
        raise ValueError("Invalid coaching plan revision")
    suffix = f"_{revision}" if revision else ""
    return Path(league_dir) / "config" / f"coaching_signing_progress_1956_6{suffix}.json"


def _load_checked(league_dir, plan):
    path = _state_path(league_dir, plan)
    if not path.exists():
        return {}
    saved = json.loads(path.read_text())
    if not isinstance(saved, dict) or saved.get("plan_id") != plan["id"]:
        raise ValueError("Saved coaching checklist does not match this plan")
    checked = saved.get("checked", {})
    if not isinstance(checked, dict) or any(type(v) is not bool for v in checked.values()):
        raise ValueError("Saved coaching checklist has invalid progress")
    return checked


def get_checklist(league_dir, team_id):
    plan = _plan(league_dir, team_id)
    if plan is None:
        return None
    checked = _load_checked(league_dir, plan)
    plan["entries"] = [dict(entry, checked=checked.get(entry["id"], False))
                       for entry in plan["entries"]]
    return plan


def save_check(league_dir, team_id, body):
    plan = _plan(league_dir, team_id)
    if plan is None:
        raise ValueError("Coaching checklist unavailable for this team")
    if not isinstance(body, dict) or body.get("plan_id") != plan["id"]:
        raise ValueError("Reload the page to use the current coaching plan")
    entry_id = body.get("entry_id")
    valid_ids = {entry["id"] for entry in plan["entries"]}
    if not isinstance(entry_id, str) or entry_id not in valid_ids:
        raise ValueError("Unknown coaching checklist entry")
    if type(body.get("checked")) is not bool:
        raise ValueError("Checkbox value must be true or false")
    # Merge one checkbox at a time so offers entered in separate browser tabs
    # cannot overwrite progress for unrelated coaches.
    with _SAVE_LOCK:
        checked = _load_checked(league_dir, plan)
        checked[entry_id] = body["checked"]
        path = _state_path(league_dir, plan)
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(path, json.dumps({"plan_id": plan["id"], "checked": checked}, indent=2) + "\n")
    return {"entry_id": entry_id, "checked": body["checked"]}
