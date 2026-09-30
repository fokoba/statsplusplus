"""draft_clock.py — live "on the clock" status for the draft page.

Scrapes the StatsPlus website's live draft room (NOT the sanctioned CSV/API
client in statsplus/client.py — that only exposes completed picks via
/draftv2/, never the in-progress "who's up next / time remaining" state,
which only exists as dynamically-loaded HTML on the draft room page itself).

Confirmed 2026-09-18 by inspecting the draft room's own page source: the
header ("Last pick" / "On the Clock" / "On deck") is populated client-side
by a POST to /draftajax/ with {'info': 'pickinfo', 'lid': <league id>},
using the same session cookie (sessionid + csrftoken) this app already
stores for the website (get_statsplus_cookie) — a different auth mechanism
than the token-based CSV API. Requires the user to be logged in via the
Session panel (cookie), not just an API token.

Public API:
    get_draft_clock(league_dir=None) -> dict | None
"""

from __future__ import annotations

import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Optional

_USER_AGENT = "Mozilla/5.0 (compatible; statsplusplus-dominator/1.0)"

# StatsPlus shards its leagues across hosts (e.g. atl-01, atl-02) — rather
# than hardcode a mapping, follow statsplus.net's own redirect once per call
# to find the real host, exactly as a browser would.
_STATSPLUS_ENTRY = "https://statsplus.net"


def _text(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def _resolve_host(slug: str) -> str:
    req = urllib.request.Request(
        f"{_STATSPLUS_ENTRY}/{slug}/draft/", headers={"User-Agent": _USER_AGENT})
    resp = urllib.request.urlopen(req, timeout=15)
    final_url = resp.geturl()
    return final_url.split("/" + slug + "/")[0]


def _parse_pickinfo_html(html: str) -> dict:
    out = {"last_pick": None, "on_clock": None, "on_deck": None}

    m = re.search(r"id='draft-header-last-pick'>(.*?)</div>\s*</div>", html, re.S)
    if m:
        pm = re.match(r"\s*([\d]+-[\d]+)\s*-\s*(.*?)\s*-\s*<a[^>]*>(.*?)</a>", m.group(1), re.S)
        if pm:
            out["last_pick"] = {
                "pick": pm.group(1), "team": _text(pm.group(2)), "player": _text(pm.group(3)),
            }

    m = re.search(r"id='draft-header-on-clock' data-due-time='([^']*)'>(.*?)</div>\s*</div>", html, re.S)
    if m:
        due_parts = [int(x.strip()) for x in m.group(1).split(",")]
        nowraps = [_text(x) for x in re.findall(r"<div class='nowrap[^']*'>(.*?)</div>", m.group(2), re.S)]
        team = nowraps[0] if len(nowraps) > 0 else None
        record = nowraps[1] if len(nowraps) > 1 else None
        due_text = nowraps[2] if len(nowraps) > 2 else ""

        due_iso = None
        if len(due_parts) >= 6:
            try:
                from datetime import datetime
                from zoneinfo import ZoneInfo
                # [year, month(0-idx), day, hour, minute, second] — same
                # shape as JS `new Date(y,m,d,h,mi,s)`, meant as Pacific
                # wall-clock time (StatsPlus's own display is always
                # "... PDT/PST"). Resolved here (not left for the browser)
                # so the countdown is correct regardless of the viewer's
                # own timezone/clock.
                y, mo, d, h, mi, s = due_parts
                due_dt = datetime(y, mo + 1, d, h, mi, s, tzinfo=ZoneInfo("America/Los_Angeles"))
                due_iso = due_dt.astimezone(ZoneInfo("UTC")).isoformat()
            except Exception:
                due_iso = None

        out["on_clock"] = {
            "team": team, "record": record,
            "due_iso": due_iso,
            "due_text": due_text.lstrip("- ").strip(),
        }

    m = re.search(r"id='draft-header-on-deck'>(.*?)</div>", html, re.S)
    if m:
        text = _text(m.group(1))
        out["on_deck"] = text or None

    return out


def get_draft_clock(league_dir=None) -> Optional[dict]:
    """Fetch and parse the live draft-room header for the active/given
    league. Returns None (not raises) on any failure — auth expiry, no
    active draft, network error — so callers can show "unavailable"
    without crashing a page render.
    """
    try:
        from statsplusplus.config.league_context import get_statsplus_cookie
        from statsplusplus.config.league_config import LeagueConfig

        cfg = LeagueConfig(base_dir=Path(league_dir)) if league_dir else LeagueConfig()
        slug = cfg.settings.get("statsplus_slug", "")
        lid = cfg.settings.get("primary_league_id")
        cookie = get_statsplus_cookie(Path(league_dir) if league_dir else None)
        if not slug or not cookie:
            return None

        host = _resolve_host(slug)
        csrf = None
        for part in cookie.split(";"):
            part = part.strip()
            if part.startswith("csrftoken="):
                csrf = part[len("csrftoken="):]
        if not csrf:
            return None

        data = urllib.parse.urlencode({"info": "pickinfo", "lid": lid or ""}).encode()
        req = urllib.request.Request(
            f"{host}/{slug}/draftajax/", data=data,
            headers={
                "Cookie": cookie, "User-Agent": _USER_AGENT,
                "X-CSRFToken": csrf, "X-Requested-With": "XMLHttpRequest",
                "Referer": f"{host}/{slug}/draft/",
                "Content-Type": "application/x-www-form-urlencoded",
            })
        resp = urllib.request.urlopen(req, timeout=15)
        html = resp.read().decode("utf-8", errors="ignore")
        return _parse_pickinfo_html(html)
    except Exception:
        return None
