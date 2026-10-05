"""Player photo cache.

StatsPlus serves each player's portrait at a public, per-league path
(``<slug>/reports/news/html/images/person_pictures/player_<id>.png``) — no
login needed. We mirror them into ``<league_dir>/photos/`` so pages load fast
and keep working offline.

Photos show the player in their *current* team's uniform, so cached copies go
stale after trades; ``sync_photos`` re-checks anything older than
``STALE_SECS`` (a week) using conditional requests, so an unchanged photo costs
a 304 rather than a download.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

log = logging.getLogger(__name__)

PHOTO_URL = "https://statsplus.net/{slug}/reports/news/html/images/person_pictures/player_{pid}.png"
USER_AGENT = "statsplusplus/1.0 (+https://github.com/statsplusplus)"
STALE_SECS = 7 * 24 * 3600
MISSING_RETRY_SECS = 24 * 3600  # StatsPlus only renders a portrait once a player is in its reports; re-check 404s daily
DEFAULT_WORKERS = 8       # parallel downloads; backs off automatically on 429
INDEX_NAME = "index.json"


def photo_dir(league_dir: Path) -> Path:
    return Path(league_dir) / "photos"


def photo_path(league_dir: Path, pid: int) -> Path:
    return photo_dir(league_dir) / f"{int(pid)}.png"


def _load_index(d: Path) -> dict:
    try:
        return json.loads((d / INDEX_NAME).read_text())
    except (OSError, ValueError):
        return {}


def _save_index(d: Path, idx: dict) -> None:
    tmp = d / (INDEX_NAME + ".tmp")
    tmp.write_text(json.dumps(idx))
    os.replace(tmp, d / INDEX_NAME)


def _candidate_ids(league_dir: Path) -> list[int]:
    """Active players, most relevant first: our own org, then everyone else
    on a team, then free agents / draft prospects. Retired players are skipped."""
    conn = sqlite3.connect(str(Path(league_dir) / "league.db"))
    try:
        my_team = None
        try:
            from statsplusplus.config.league_config import LeagueConfig
            my_team = LeagueConfig(base_dir=Path(league_dir)).my_team_id or None
        except Exception:
            pass
        rows = conn.execute(
            "SELECT player_id, team_id, parent_team_id FROM players "
            "WHERE COALESCE(retired, 0) = 0"
        ).fetchall()
    finally:
        conn.close()

    def rank(r):
        pid, tid, parent = r
        if my_team is not None and my_team in (tid, parent):
            return 0
        return 1 if tid else 2

    return [r[0] for r in sorted(rows, key=lambda r: (rank(r), r[0]))]


def _fetch_one(league_dir: Path, slug: str, pid: int, entry: dict, pause: threading.Event):
    """Fetch one photo. Returns (pid, kind, payload) with kind in
    ok/unchanged/missing/rate_limited/error."""
    headers = {"User-Agent": USER_AGENT}
    if entry.get("etag") and photo_path(league_dir, pid).exists():
        headers["If-None-Match"] = entry["etag"]
    req = urllib.request.Request(PHOTO_URL.format(slug=slug, pid=pid), headers=headers)
    while pause.is_set():
        time.sleep(0.5)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            data = r.read()
            etag = r.headers.get("ETag")
        tmp = photo_dir(league_dir) / f"{pid}.png.{threading.get_ident()}.tmp"
        tmp.write_bytes(data)
        os.replace(tmp, photo_path(league_dir, pid))
        return pid, "ok", etag
    except urllib.error.HTTPError as e:
        if e.code == 304:
            return pid, "unchanged", None
        if e.code == 404:
            return pid, "missing", None
        if e.code == 429:
            try:
                wait = min(int(e.headers.get("Retry-After", "30")), 120)
            except ValueError:
                wait = 30
            return pid, "rate_limited", wait
        return pid, "error", None
    except Exception:
        return pid, "error", None


def sync_photos(league_dir: Path, slug: str, *, workers: int = DEFAULT_WORKERS,
                stale_secs: int = STALE_SECS, max_requests: int | None = None,
                stop=None) -> dict:
    """Download missing/stale player photos in parallel. Returns counts.

    On a 429 all workers pause for Retry-After; three rate limits in a row
    end the pass, and the next pass resumes (it only touches stale entries).
    ``stop`` is an optional callable returning True to abort early.
    """
    d = photo_dir(league_dir)
    d.mkdir(parents=True, exist_ok=True)
    idx = _load_index(d)
    now = time.time()
    counts = {"downloaded": 0, "unchanged": 0, "missing": 0, "errors": 0, "rate_limited": 0}
    def _is_stale(pid):
        e = idx.get(str(pid), {})
        limit = MISSING_RETRY_SECS if e.get("s") == 404 else stale_secs
        return now - e.get("t", 0) > limit

    todo = [pid for pid in _candidate_ids(league_dir) if _is_stale(pid)]
    if max_requests is not None:
        todo = todo[:max_requests]
    pause = threading.Event()
    consecutive_429 = 0
    done = 0
    BATCH = workers * 25
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for start in range(0, len(todo), BATCH):
            if stop and stop():
                break
            batch = todo[start:start + BATCH]
            results = list(pool.map(
                lambda pid: _fetch_one(league_dir, slug, pid, idx.get(str(pid), {}), pause), batch))
            retry_wait = 0
            for pid, kind, payload in results:
                done += 1
                if kind == "ok":
                    idx[str(pid)] = {"t": now, "s": 200, "etag": payload}
                    counts["downloaded"] += 1
                elif kind == "unchanged":
                    idx[str(pid)] = {**idx.get(str(pid), {}), "t": now, "s": 200}
                    counts["unchanged"] += 1
                elif kind == "missing":
                    idx[str(pid)] = {"t": now, "s": 404}
                    counts["missing"] += 1
                elif kind == "rate_limited":
                    counts["rate_limited"] += 1
                    done -= 1  # will be retried next pass
                    retry_wait = max(retry_wait, payload)
                else:
                    counts["errors"] += 1
            _save_index(d, idx)
            if retry_wait:
                consecutive_429 += 1
                if consecutive_429 >= 3:
                    log.warning("photos: rate limited 3 batches in a row, pausing sync")
                    break
                pause.set(); time.sleep(retry_wait); pause.clear()
            else:
                consecutive_429 = 0
    counts["remaining"] = max(0, len(todo) - done)
    return counts
