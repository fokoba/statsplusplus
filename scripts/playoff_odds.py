"""playoff_odds.py — real simulated postseason odds, scraped from StatsPlus.

The site's own Playoff Odds page (/playoffodds/) runs 5,000 season
simulations and publishes, per team: current W/L, MaxW/MaxL (the
mathematical best/worst-case final record if they won or lost every
remaining game), AvgW/AvgL, and simulated 1st%/Div%/PO%/Last% — all in one
authenticated GET, no POST/AJAX involved (unlike the draft room).

PO% ("postseason odds") is the one number that means the right thing in
every league format this app supports: it's the pennant probability in a
single-division no-wildcard league (PPL) and the real combined
division-winner-or-wildcard probability in a multi-division league with
wildcards (eMLB) — so callers can use it uniformly instead of needing
per-format logic.

Requires the user to be logged in via the Session panel (cookie), same
auth mechanism as draft_clock.py.

Public API:
    get_playoff_odds(league_dir=None) -> dict[int, dict] | None
"""

from __future__ import annotations

import re
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

_USER_AGENT = "Mozilla/5.0 (compatible; statsplusplus-dominator/1.0)"
_STATSPLUS_ENTRY = "https://statsplus.net"

# Order the page's plain (non data-name) numeric <td> cells appear in, per
# team row — W, L, MaxW, MaxL, AvgW, AvgL, then the two SoS columns at the
# very end. The four percentage cells (1st/Div/PO/Last %) are NOT plain
# <td>NUMBER</td> — they carry a data-name='NUMBER' attribute instead
# (confirmed: one of them even closes with a stray </th> typo in the site's
# own markup), so the two extraction patterns below never collide.
_PLAIN_TD_RE = re.compile(r"<td>(-?[\d.]+)</td>")
_PCT_RE = re.compile(r"data-name='(-?[\d.]+)'")
_ROW_RE = re.compile(r"<tr[^>]*>((?:(?!</tr>).)*?/team/(\d+)(?:(?!</tr>).)*?)</tr>", re.S)


def _resolve_host(slug: str) -> str:
    req = urllib.request.Request(
        f"{_STATSPLUS_ENTRY}/{slug}/playoffodds/", headers={"User-Agent": _USER_AGENT})
    resp = urllib.request.urlopen(req, timeout=15)
    return resp.geturl().split("/" + slug + "/")[0]


def _parse_playoff_odds_html(html: str) -> dict[int, dict]:
    out: dict[int, dict] = {}
    for block, tid_str in _ROW_RE.findall(html):
        plain = _PLAIN_TD_RE.findall(block)
        pct = _PCT_RE.findall(block)
        if len(plain) < 6 or len(pct) < 4:
            continue
        try:
            tid = int(tid_str)
            out[tid] = {
                "w": int(plain[0]), "l": int(plain[1]),
                "max_w": int(plain[2]), "max_l": int(plain[3]),
                "avg_w": float(plain[4]), "avg_l": float(plain[5]),
                "first_pct": float(pct[0]), "div_pct": float(pct[1]),
                "po_pct": float(pct[2]), "last_pct": float(pct[3]),
            }
        except (ValueError, IndexError):
            continue
    return out


_CACHE_MAX_AGE = 1800  # seconds — StatsPlus recomputes this from 5,000 sims
                        # server-side; no need to hit it on every page load.


def get_playoff_odds_cached(league_dir: Path) -> Optional[dict[int, dict]]:
    """Same as get_playoff_odds(), but reads/writes a per-league JSON cache
    (data/<league>/config/playoff_odds_cache.json) so a live /league page
    load doesn't do an authenticated network fetch every time. Serves a
    stale cache (however old) if a fresh fetch fails — a slightly-stale
    real percentage beats no percentage at all — and only returns None
    when there's neither a fresh fetch nor any cache to fall back on."""
    import json
    import time

    cache_path = Path(league_dir) / "config" / "playoff_odds_cache.json"
    cached = None
    if cache_path.exists():
        try:
            raw = json.loads(cache_path.read_text())
            cached = {int(k): v for k, v in raw["data"].items()}
            if time.time() - raw["fetched_at"] < _CACHE_MAX_AGE:
                return cached
        except Exception:
            cached = None

    fresh = get_playoff_odds(league_dir)
    if fresh:
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(json.dumps({"fetched_at": time.time(), "data": fresh}))
        except Exception:
            pass
        return fresh
    return cached


def get_playoff_odds(league_dir: Optional[Path] = None) -> Optional[dict[int, dict]]:
    """Fetch and parse the live Playoff Odds page for the active (or given)
    league. Returns {team_id: {...}} or None on any failure (expired
    cookie, network error, unparseable page) — callers should fall back to
    a locally-computed elimination check rather than break the page."""
    from statsplusplus.config.league_context import (
        get_statsplus_cookie, get_active_league_slug, get_league_dir)

    ld = league_dir or get_league_dir()
    slug = ld.name if hasattr(ld, "name") else get_active_league_slug()
    try:
        cookie = get_statsplus_cookie(ld)
        if not cookie:
            return None
        host = _resolve_host(slug)
        req = urllib.request.Request(
            f"{host}/{slug}/playoffodds/",
            headers={"User-Agent": _USER_AGENT, "Cookie": cookie,
                      "Referer": f"{host}/{slug}/"})
        html = urllib.request.urlopen(req, timeout=20).read().decode(errors="ignore")
        parsed = _parse_playoff_odds_html(html)
        return parsed or None
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError):
        return None
