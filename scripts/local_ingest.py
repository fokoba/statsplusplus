"""
local_ingest.py — auto-import OOTP grid/HTML exports without a browser upload.

Forrest manually exports CSV/HTML reports from his local OOTP install (via
OOTP's own "Export to CSV" grid button). The game drops each file into a
per-league import_export folder (or, for the Team Salary HTML report,
~/Downloads). This module finds the newest matching file per category and
feeds it straight into the *existing* import_* functions that the web
upload forms already call (import_ratings_sync, import_fa_asking_prices,
import_team_salary, import_league_park_factors, import_rule5_eligible) —
no new parsers, just automatic discovery instead of a manual file picker.

A small per-league state file (config/local_ingest_state.json) tracks the
mtime last processed per category, so unchanged files are skipped cheaply
on every poll.

Public API:
    ingest_once(league_slug, league_dir) -> dict[str, str]
    ingest_all_leagues(data_root) -> dict[str, dict[str, str]]
"""

from __future__ import annotations

import csv
import io
import json
import os
import sys
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _SCRIPTS_DIR.parent
_WEB_DIR = _PROJECT_ROOT / "web"
for _p in (str(_SCRIPTS_DIR), str(_WEB_DIR), str(_PROJECT_ROOT / "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

HOME = Path.home()
DOWNLOADS = HOME / "Downloads"

# Per-league OOTP import_export folders — confirmed on Forrest's machine
# 2026-09-16. PPL runs in OOTP26 (unsandboxed path); eMLB runs in OOTP27
# (App Sandbox path under Containers). Add more leagues here if/when they
# get their own OOTP install.
LEAGUE_FOLDERS: dict[str, list[Path]] = {
    "ppl": [
        HOME / "Application Support" / "Out of the Park Developments"
        / "OOTP Baseball 26" / "saved_games" / "PPL.lg" / "import_export",
        HOME / "Library" / "Application Support" / "Out of the Park Developments"
        / "OOTP Baseball 26" / "saved_games" / "PPL.lg" / "import_export",
    ],
    "emlb": [
        HOME / "Library" / "Containers" / "com.ootpdevelopments.ootp27macqlm"
        / "Data" / "Application Support" / "Out of the Park Developments"
        / "OOTP Baseball 27" / "saved_games" / "eMLB.lg" / "import_export",
    ],
}


def _find_latest(folders: list[Path], must_contain: list[str]) -> Path | None:
    """Newest file across `folders` whose lowercased name contains every
    substring in `must_contain`."""
    best: Path | None = None
    for folder in folders:
        if not folder.exists():
            continue
        for p in folder.iterdir():
            if not p.is_file():
                continue
            name = p.name.lower()
            if all(s in name for s in must_contain):
                if best is None or p.stat().st_mtime > best.stat().st_mtime:
                    best = p
    return best


def _state_path(league_dir) -> Path:
    return Path(league_dir) / "config" / "local_ingest_state.json"


def _load_state(league_dir) -> dict:
    p = _state_path(league_dir)
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:
            return {}
    return {}


def _save_state(league_dir, state: dict) -> None:
    p = _state_path(league_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(state, indent=2))


def _r5_filter_csv(file_bytes: bytes) -> bytes:
    """Return CSV bytes containing only rows where R5 == 'Yes'.

    import_rule5_eligible() trusts every row in the file it's given as
    eligible (see its docstring), so filtering happens here rather than
    there — reuses the same allcolumns export already pulled for ratings
    sync instead of requiring a separate in-game pre-filtered export.
    """
    text = file_bytes.decode("utf-8-sig", errors="ignore")
    reader = csv.DictReader(io.StringIO(text))
    fieldnames = reader.fieldnames or []
    rows = [row for row in reader if (row.get("R5") or "").strip().lower() == "yes"]
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fieldnames)
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue().encode("utf-8")


def _ingest_draft_pool(file_bytes: bytes, league_dir) -> int:
    """Same ID-column detection as /api/draft-pool-upload in
    web/api_routes.py — kept in sync manually since that route's logic is
    inline rather than a shared function."""
    text = file_bytes.decode("utf-8-sig", errors="ignore")
    reader = csv.DictReader(io.StringIO(text))
    headers = reader.fieldnames or []
    id_col = None
    for h in headers:
        if h.strip().lower().replace(" ", "").replace("_", "") in ("id", "playerid", "pid"):
            id_col = h
            break
    if not id_col:
        return 0
    pids = []
    for row in reader:
        val = (row.get(id_col) or "").strip()
        if val.isdigit():
            pids.append(int(val))
    if not pids:
        return 0
    pool_path = Path(league_dir) / "config" / "draft_pool.json"
    pool_path.write_text(json.dumps({"player_ids": pids}, indent=2))
    return len(pids)


def _best_salary_match(candidates: list[Path], league_dir) -> tuple[Path | None, float]:
    """Among Team Salary HTML candidates, return whichever has the highest
    fraction of parsed player_ids present in this league's own players
    table, and that fraction. (Path, 0.0) if candidates exist but none
    parse any player_ids; (None, 0.0) if there are no candidates at all.
    """
    import re
    import sqlite3

    if not candidates:
        return None, 0.0

    db_path = Path(league_dir) / "league.db"
    if not db_path.exists():
        return None, 0.0
    conn = sqlite3.connect(str(db_path))

    best_file, best_ratio = None, -1.0
    for f in candidates:
        try:
            html = f.read_bytes().decode("utf-8", errors="ignore")
        except Exception:
            continue
        pids = set(int(m) for m in re.findall(r"player_(\d+)\.html", html))
        if not pids:
            ratio = 0.0
        else:
            qs = ",".join("?" * len(pids))
            matched = conn.execute(
                f"SELECT COUNT(*) FROM players WHERE player_id IN ({qs})", list(pids)
            ).fetchone()[0]
            ratio = matched / len(pids)
        if ratio > best_ratio:
            best_file, best_ratio = f, ratio
    conn.close()
    return best_file, max(best_ratio, 0.0)


def ingest_once(league_slug: str, league_dir) -> dict[str, str]:
    """Check every category for a fresher local export than what's already
    been imported, and import any that changed. Returns a summary dict of
    category -> human-readable outcome string."""
    folders = LEAGUE_FOLDERS.get(league_slug, [])
    state = _load_state(league_dir)
    summary: dict[str, str] = {}

    def _maybe(category, must_contain, importer):
        f = _find_latest(folders, must_contain)
        if f is None:
            summary[category] = "not found"
            return
        mtime = f.stat().st_mtime
        if state.get(category) == mtime:
            summary[category] = "unchanged"
            return
        try:
            result = importer(f.read_bytes())
            state[category] = mtime
            summary[category] = f"imported ({f.name}): {result}"
        except Exception as e:
            summary[category] = f"error on {f.name}: {e}"

    from custom_upload import import_ratings_sync, import_fa_asking_prices, import_team_salary
    from lineup_optimizer_queries import import_league_park_factors
    from scouting_queries import import_rule5_eligible

    _maybe("ratings", ["player_list", "allcolumns"],
           lambda data: import_ratings_sync(data, league_dir=league_dir))
    _maybe("fa_asks", ["free_agents", "allcolumns"],
           lambda data: import_fa_asking_prices(data, league_dir=league_dir))
    _maybe("park_factors", ["team_statistics"],
           lambda data: import_league_park_factors(data, league_dir=league_dir))
    _maybe("draft_pool", ["draft_pool", "allcolumns"],
           lambda data: _ingest_draft_pool(data, league_dir))

    # Rule 5 reuses the same ratings file, filtered to R5 == Yes.
    ratings_file = _find_latest(folders, ["player_list", "allcolumns"])
    if ratings_file is None:
        summary["rule5"] = "not found"
    else:
        mtime = ratings_file.stat().st_mtime
        if state.get("rule5") == mtime:
            summary["rule5"] = "unchanged"
        else:
            try:
                filtered = _r5_filter_csv(ratings_file.read_bytes())
                n = import_rule5_eligible(filtered, league_dir=league_dir)
                state["rule5"] = mtime
                summary["rule5"] = f"imported ({ratings_file.name}): {n} eligible"
            except Exception as e:
                summary["rule5"] = f"error: {e}"

    # Team Salary — OOTP always writes "Team Salary.html" to ~/Downloads
    # regardless of which league it's for, so filename/recency alone can't
    # tell leagues apart (confirmed: PPL and eMLB exports landed with the
    # same name minutes apart, and "newest wins" picked the wrong file for
    # one league — its parsed player_ids simply don't belong there). Instead
    # pick, among all candidates, whichever has the highest player_id
    # overlap with THIS league's own players table, and require a strong
    # majority match before trusting it at all.
    salary_candidates = list(DOWNLOADS.glob("Team Salary*.html")) if DOWNLOADS.exists() else []
    for folder in folders:
        if folder.exists():
            salary_candidates += list(folder.glob("Team Salary*.html"))
    salary_file, salary_ratio = _best_salary_match(salary_candidates, league_dir)
    if salary_file is None:
        summary["team_salary"] = "not found"
    elif salary_ratio < 0.8:
        summary["team_salary"] = (
            f"skipped ({salary_file.name}): only {salary_ratio:.0%} of its "
            "player IDs belong to this league — likely the other league's export"
        )
    else:
        mtime = salary_file.stat().st_mtime
        if state.get("team_salary_mtime") == mtime and state.get("team_salary_path") == str(salary_file):
            summary["team_salary"] = "unchanged"
        else:
            try:
                n = import_team_salary(salary_file.read_bytes(), league_dir=league_dir)
                state["team_salary_mtime"] = mtime
                state["team_salary_path"] = str(salary_file)
                summary["team_salary"] = f"imported ({salary_file.name}): {n} cells"
            except Exception as e:
                summary["team_salary"] = f"error: {e}"

    _save_state(league_dir, state)
    return summary


def get_freshness(league_dir) -> dict:
    """Newest mtime across every locally-ingested category for this league —
    feeds a 'local export last updated' UI indicator so Forrest knows when
    it's worth re-exporting from OOTP, without having to track it himself.

    Returns {"newest_mtime": float | None, "categories": {name: mtime}}.
    `newest_mtime` is None if nothing has ever been ingested for this league.
    """
    state = _load_state(league_dir)
    mtimes: dict[str, float] = {}
    for k in ("ratings", "fa_asks", "park_factors", "draft_pool", "rule5"):
        if k in state:
            mtimes[k] = state[k]
    if "team_salary_mtime" in state:
        mtimes["team_salary"] = state["team_salary_mtime"]
    newest = max(mtimes.values()) if mtimes else None
    return {"newest_mtime": newest, "categories": mtimes}


def ingest_all_leagues(data_root) -> dict[str, dict[str, str]]:
    """Loop every league directory under data_root and ingest_once() each."""
    results: dict[str, dict[str, str]] = {}
    for d in sorted(Path(data_root).iterdir()):
        if not d.is_dir():
            continue
        if not (d / "config" / "league_settings.json").exists():
            continue
        slug = d.name
        try:
            results[slug] = ingest_once(slug, d)
        except Exception as e:
            results[slug] = {"error": str(e)}
    return results


def main():
    data_root = _PROJECT_ROOT / "data"
    results = ingest_all_leagues(data_root)
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
