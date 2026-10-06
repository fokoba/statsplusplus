"""Team logo cache — full-size StatsPlus logos mirrored to ``<league_dir>/logos/``.

Only teams with a full name in ``league_settings.json`` ``team_names`` have a
logo on StatsPlus (minor-league affiliates don't); the web route falls back to
the parent club's logo for those.
"""
from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import time
import urllib.error
import urllib.request
from pathlib import Path

log = logging.getLogger(__name__)

LOGO_URL = "https://statsplus.net/{slug}/reports/news/html/images/team_logos/{name}.png"
USER_AGENT = "statsplusplus/1.0 (+https://github.com/statsplusplus)"


def logo_dir(league_dir: Path) -> Path:
    return Path(league_dir) / "logos"


def logo_path(league_dir: Path, team_id: int) -> Path:
    return logo_dir(league_dir) / f"{int(team_id)}.png"


def _file_stem(team_name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", team_name.lower()).strip("_")


def sync_logos(league_dir: Path, slug: str, *, refresh: bool = False) -> dict:
    """Download any missing team logos (all of them when ``refresh``)."""
    league_dir = Path(league_dir)
    try:
        settings = json.loads((league_dir / "config" / "league_settings.json").read_text())
        names = settings.get("team_names", {})
        overrides = settings.get("logo_names", {})  # {team_id: file stem} when StatsPlus' file differs from the display name
    except (OSError, ValueError):
        return {"downloaded": 0, "missing": 0, "errors": 0}
    logo_dir(league_dir).mkdir(parents=True, exist_ok=True)
    counts = {"downloaded": 0, "missing": 0, "errors": 0}
    for tid, name in names.items():
        dest = logo_path(league_dir, int(tid))
        if dest.exists() and not refresh:
            continue
        req = urllib.request.Request(LOGO_URL.format(slug=slug, name=overrides.get(str(tid)) or _file_stem(name)),
                                     headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                data = r.read()
            tmp = dest.with_suffix(".tmp")
            tmp.write_bytes(data)
            os.replace(tmp, dest)
            counts["downloaded"] += 1
        except urllib.error.HTTPError as e:
            counts["missing" if e.code == 404 else "errors"] += 1
        except Exception:
            counts["errors"] += 1
    return counts


TEAM_PAGE_URL = "https://statsplus.net/{slug}/team/{tid}"
_TEAMPIC_RE = re.compile(r"class=['\"]teampic['\"][^>]*src=['\"][^'\"]*/team_logos/([^'\"/]+?)(?:_\d+)?\.png", re.I)
MISSING_RETRY_SECS = 7 * 24 * 3600


def _index(league_dir: Path) -> dict:
    try:
        return json.loads((logo_dir(league_dir) / "index.json").read_text())
    except (OSError, ValueError):
        return {}


def sync_team_page_logos(league_dir: Path, slug: str, cookie: str = "", delay: float = 0.6) -> dict:
    """Fetch the logo every team actually uses from its StatsPlus team page.

    Minor-league affiliates have nicknames (e.g. "Atlanta Firebirds") that the
    teams table doesn't store, so their logo file can't be guessed from the
    name — the team page's ``teampic`` image gives the real file name. Teams
    that already have a cached logo are skipped; teams whose page had no
    logo are retried weekly.
    """
    league_dir = Path(league_dir)
    d = logo_dir(league_dir)
    d.mkdir(parents=True, exist_ok=True)
    idx = _index(league_dir)
    try:
        conn = sqlite3.connect(str(league_dir / "league.db"))
        tids = [r[0] for r in conn.execute("SELECT team_id FROM teams ORDER BY team_id")]
        conn.close()
    except sqlite3.Error:
        return {"downloaded": 0, "missing": 0, "errors": 0}
    counts = {"downloaded": 0, "missing": 0, "errors": 0}
    now = time.time()
    for tid in tids:
        entry = idx.get(str(tid), {})
        if logo_path(league_dir, tid).exists() and entry.get("src") == "page":
            continue
        if entry.get("missing") and now - entry["missing"] < MISSING_RETRY_SECS:
            continue
        # A name-guessed logo (MLB clubs) is fine until proven otherwise, but
        # always re-resolve from the page once so it's the authoritative file.
        headers = {"User-Agent": USER_AGENT}
        if cookie:
            headers["Cookie"] = cookie
        try:
            req = urllib.request.Request(TEAM_PAGE_URL.format(slug=slug, tid=tid), headers=headers)
            with urllib.request.urlopen(req, timeout=20) as r:
                html = r.read().decode("utf8", "ignore")
            m = _TEAMPIC_RE.search(html)
            if not m:
                idx[str(tid)] = {**entry, "missing": now}
                counts["missing"] += 1
            else:
                req = urllib.request.Request(LOGO_URL.format(slug=slug, name=m.group(1)),
                                             headers={"User-Agent": USER_AGENT})
                with urllib.request.urlopen(req, timeout=20) as r:
                    data = r.read()
                dest = logo_path(league_dir, tid)
                tmp = dest.with_suffix(".tmp")
                tmp.write_bytes(data)
                os.replace(tmp, dest)
                idx[str(tid)] = {"src": "page", "file": m.group(1)}
                counts["downloaded"] += 1
        except urllib.error.HTTPError as e:
            if e.code == 429:
                break  # rate limited: resume next pass
            idx[str(tid)] = {**entry, "missing": now}
            counts["missing" if e.code == 404 else "errors"] += 1
        except Exception:
            counts["errors"] += 1
        time.sleep(delay)
    (d / "index.json").write_text(json.dumps(idx))
    return counts
