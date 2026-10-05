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
