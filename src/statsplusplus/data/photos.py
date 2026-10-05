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
import time
import urllib.error
import urllib.request
from pathlib import Path

log = logging.getLogger(__name__)

PHOTO_URL = "https://statsplus.net/{slug}/reports/news/html/images/person_pictures/player_{pid}.png"
USER_AGENT = "statsplusplus/1.0 (+https://github.com/statsplusplus)"
STALE_SECS = 7 * 24 * 3600
DEFAULT_DELAY = 0.5       # seconds between requests (StatsPlus 429s fast clients)
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


def sync_photos(league_dir: Path, slug: str, *, delay: float = DEFAULT_DELAY,
                stale_secs: int = STALE_SECS, max_requests: int | None = None,
                stop=None) -> dict:
    """Download missing/stale player photos. Returns counts.

    ``stop`` is an optional callable returning True to abort early.
    Stops the pass if StatsPlus rate-limits us repeatedly; the next pass
    resumes where this one left off (it only touches stale entries).
    """
    d = photo_dir(league_dir)
    d.mkdir(parents=True, exist_ok=True)
    idx = _load_index(d)
    now = time.time()
    counts = {"downloaded": 0, "unchanged": 0, "missing": 0, "errors": 0, "rate_limited": 0}
    todo = [pid for pid in _candidate_ids(league_dir)
            if now - idx.get(str(pid), {}).get("t", 0) > stale_secs]
    requests = 0
    consecutive_429 = 0
    for pid in todo:
        if (stop and stop()) or (max_requests is not None and requests >= max_requests):
            break
        entry = idx.get(str(pid), {})
        headers = {"User-Agent": USER_AGENT}
        if entry.get("etag") and photo_path(league_dir, pid).exists():
            headers["If-None-Match"] = entry["etag"]
        req = urllib.request.Request(PHOTO_URL.format(slug=slug, pid=pid), headers=headers)
        requests += 1
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                data = r.read()
                etag = r.headers.get("ETag")
            tmp = d / f"{pid}.png.tmp"
            tmp.write_bytes(data)
            os.replace(tmp, photo_path(league_dir, pid))
            idx[str(pid)] = {"t": now, "s": 200, "etag": etag}
            counts["downloaded"] += 1
            consecutive_429 = 0
        except urllib.error.HTTPError as e:
            if e.code == 304:
                idx[str(pid)] = {**entry, "t": now, "s": 200}
                counts["unchanged"] += 1
                consecutive_429 = 0
            elif e.code == 404:
                idx[str(pid)] = {"t": now, "s": 404}
                counts["missing"] += 1
                consecutive_429 = 0
            elif e.code == 429:
                counts["rate_limited"] += 1
                consecutive_429 += 1
                if consecutive_429 >= 3:
                    log.warning("photos: rate limited 3x in a row, pausing sync")
                    break
                try:
                    wait = min(int(e.headers.get("Retry-After", "30")), 120)
                except ValueError:
                    wait = 30
                time.sleep(wait)
                continue
            else:
                counts["errors"] += 1
        except Exception:
            counts["errors"] += 1
        if requests % 50 == 0:
            _save_index(d, idx)
        time.sleep(delay)
    _save_index(d, idx)
    counts["remaining"] = max(0, len(todo) - requests)
    return counts
