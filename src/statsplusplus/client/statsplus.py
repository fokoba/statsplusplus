"""StatsPlus API client.

HTTP client for the StatsPlus league data API. Handles authentication,
CSV parsing, ratings header repair, and rate limiting.

This module is self-contained: no dependency on DB, config, or evaluation layers.
Credentials are resolved lazily or set explicitly via configure().

Public API:
    configure(league_url, cookie) -> None
    get_players() -> list[dict]
    get_player_batting_stats(year, pid, split, lid) -> list[dict]
    get_player_pitching_stats(year, pid, split, lid) -> list[dict]
    get_player_fielding_stats(year, pid, split, lid) -> list[dict]
    get_contracts() -> list[dict]
    get_contract_extensions() -> list[dict]
    get_teams() -> list[dict]
    get_date() -> str
    get_exports() -> dict
    get_team_batting_stats(year, split) -> list[dict]
    get_team_pitching_stats(year, split) -> list[dict]
    get_draft(lid) -> list[dict]
    get_game_history(year) -> list[dict]
    get_lgdata() -> dict
    get_tradeblock() -> dict
    get_ballparks(lid) -> dict
    get_ratings(player_ids, poll_url, skip_initial_wait) -> list[dict]
    start_ratings_export() -> str
"""

from __future__ import annotations

import csv
import io
import json
import logging
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger("statspp.client")

# Deferred credential resolution — no module-level env reads.
_league_url: Optional[str] = None
_cookie: Optional[str] = None
_token: Optional[str] = None


class CookieExpiredError(Exception):
    """Raised when StatsPlus returns a login-required response."""
    pass


class TokenExpiredError(Exception):
    """Raised when StatsPlus reports an expired or invalid API token.

    Tokens are valid for 90 days; the user must log in on the StatsPlus site
    to refresh the token, then update it in Settings.
    """
    pass


class RateLimitedError(Exception):
    """Raised when a rate-limited endpoint refuses and the wait is too long to
    block on (e.g. the /ratings once-per-5-minutes-per-team cooldown)."""
    def __init__(self, seconds: int, message: str = ""):
        self.seconds = seconds
        super().__init__(message or f"Rate limited — try again in {seconds} seconds.")


def configure(league_url: str, cookie: str, token: str = "") -> None:
    """Set credentials explicitly (used by onboarding, tests, etc.)."""
    global _league_url, _cookie, _token
    _league_url = league_url
    _cookie = cookie
    _token = token


def _resolve_creds() -> tuple[str, str, str]:
    """Resolve credentials lazily.

    Priority: configure() > league_context > environment > .env file.
    Returns ``(league_url, cookie, token)``. The token is the sanctioned
    per-team API token (https://wiki.statsplus.net/web-tools/statsplus-api),
    preferred over the session cookie when set; the cookie remains the fallback.
    """
    global _league_url, _cookie, _token
    if _league_url and (_cookie or _token):
        return _league_url, _cookie or "", _token or ""

    # Try league_context (multi-league path)
    try:
        from statsplusplus.config.league_context import (
            get_league_dir, get_statsplus_cookie, get_statsplus_token,
        )
        cookie = get_statsplus_cookie()
        token = get_statsplus_token()
        league_dir = get_league_dir()
        settings_path = league_dir / "config" / "league_settings.json"
        if settings_path.exists():
            settings = json.loads(settings_path.read_text())
            slug = settings.get("statsplus_slug", "")
            if slug and (cookie or token):
                _league_url = str(slug)
                _cookie = str(cookie)
                _token = str(token)
                return _league_url, _cookie, _token
    except Exception:
        pass

    # Environment variables
    env_url = os.environ.get("STATSPLUS_LEAGUE_URL", "")
    env_cookie = os.environ.get("STATSPLUS_COOKIE", "")
    env_token = os.environ.get("STATSPLUS_TOKEN", "")
    if env_url and (env_cookie or env_token):
        _league_url = env_url
        _cookie = env_cookie
        _token = env_token
        return _league_url, _cookie, _token

    # Legacy .env file
    env_path = Path(__file__).parent.parent.parent.parent / "statsplus" / ".env"
    if env_path.exists():
        try:
            for line in env_path.read_text().splitlines():
                line_s = line.strip()
                if line_s and not line_s.startswith("#") and "=" in line_s:
                    k, _, v = line_s.partition("=")
                    os.environ.setdefault(k.strip(), v.strip())
        except OSError:
            pass
        _league_url = os.environ.get("STATSPLUS_LEAGUE_URL", "")
        _cookie = os.environ.get("STATSPLUS_COOKIE", "")
        _token = os.environ.get("STATSPLUS_TOKEN", "")
        if _league_url and (_cookie or _token):
            return _league_url, _cookie, _token

    raise RuntimeError(
        "StatsPlus credentials not configured. "
        "Set via configure(), app_config.json, or environment variables."
    )


def _base_url() -> str:
    slug, _, _ = _resolve_creds()
    return f"https://statsplus.net/{slug}/api"


# StatsPlus bot filtering serves a login page to requests with no User-Agent —
# identify the tool. https://wiki.statsplus.net/web-tools/statsplus-api#authentication
_USER_AGENT = "statsplusplus/1.0 (+https://github.com/statsplusplus)"
_WAIT_RE = re.compile(r"wait (\d+) seconds", re.IGNORECASE)
_RATE_LIMIT_MAX_RETRIES = 4

# HTTP-200 plain-text human messages the API documents (see client_reference).
_MSG_TOKEN_EXPIRED = "api token has expired"
_MSG_TOKEN_INVALID = "invalid or unknown api token"
_MSG_LOGIN_REQUIRED = "requires user to be logged in"
_MSG_RATINGS_UPDATING = "ratings are being updated"


def _classify_message(body: str) -> str:
    """Classify an API response body: auth_token / auth_cookie / wait /
    transient / data. Only documented human messages are recognized; the
    ratings-poll 'still in progress' body is left as data for the poll loop."""
    low = body.lower()
    if _MSG_TOKEN_EXPIRED in low or _MSG_TOKEN_INVALID in low:
        return "auth_token"
    if _MSG_LOGIN_REQUIRED in low:
        return "auth_cookie"
    if _WAIT_RE.search(body):
        return "wait"
    if _MSG_RATINGS_UPDATING in low:
        return "transient"
    return "data"


def _fetch(url: str, _retries: int = _RATE_LIMIT_MAX_RETRIES) -> str:
    _, cookie, token = _resolve_creds()
    headers = {"Accept": "application/json", "User-Agent": _USER_AGENT}
    if token:
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}token={token}"
    else:
        headers["Cookie"] = cookie
    body: str = ""
    for attempt in range(_retries + 1):
        req = urllib.request.Request(url, headers=headers)
        ctype = ""
        try:
            with urllib.request.urlopen(req) as r:
                ctype = (r.headers.get("Content-Type") or "").lower()
                body = r.read().decode()
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < _retries:
                retry_after = e.headers.get("Retry-After") if e.headers else None
                wait = int(retry_after) if retry_after and retry_after.isdigit() else 35
                log.info("Rate limited (429) — waiting %ds (attempt %d)", wait, attempt + 1)
                time.sleep(wait + 2)
                continue
            raise
        is_data_ctype = ("text/csv" in ctype) or ("json" in ctype)
        if not is_data_ctype:
            decision = _classify_message(body)
            if decision == "auth_token":
                raise TokenExpiredError(
                    "StatsPlus API token expired or invalid — log in on the "
                    "StatsPlus site to refresh it, then update it in Settings.")
            if decision == "auth_cookie":
                raise CookieExpiredError(
                    "StatsPlus session expired — update your cookie in Settings.")
            if decision == "wait" and attempt < _retries:
                m = _WAIT_RE.search(body)
                wait = int(m.group(1)) if m else 30
                log.info("Rate limited — waiting %ds (attempt %d)", wait, attempt + 1)
                time.sleep(wait + 2)
                continue
            if decision == "transient" and attempt < _retries:
                log.info("StatsPlus data not ready — retrying (attempt %d)", attempt + 1)
                time.sleep(15)
                continue
        return body
    return body


def _get(path: str, params: Optional[dict[str, Any]] = None) -> str:
    p = params or {}
    qs = "&".join(f"{k}={v}" for k, v in p.items() if v is not None)
    url = f"{_base_url()}{path}" + (f"?{qs}" if qs else "")
    result = _fetch(url)
    return str(result)


def _parse_csv(text: str) -> list[dict[str, Any]]:
    if not text.strip():
        return []
    rows: list[dict[str, Any]] = []
    for row in csv.DictReader(io.StringIO(text)):
        coerced: dict[str, Any] = {}
        for k, v in row.items():
            try:
                coerced[k] = int(v)
            except (ValueError, TypeError):
                try:
                    coerced[k] = float(v)
                except (ValueError, TypeError):
                    coerced[k] = v
        rows.append(coerced)
    return rows


def _csv(path: str, params: Optional[dict[str, Any]] = None) -> list[dict[str, Any]]:
    return _parse_csv(_get(path, params))


def _json(path: str, params: Optional[dict[str, Any]] = None) -> Any:
    return json.loads(_get(path, params))


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

def get_players() -> list[dict[str, Any]]:
    """Fetch all players in the league."""
    return _csv("/players/")


def get_player_batting_stats(
    year: Optional[int] = None,
    pid: Optional[int] = None,
    split: Optional[int] = None,
    lid: Optional[int] = None,
) -> list[dict[str, Any]]:
    """Fetch player batting stats."""
    return _csv("/playerbatstatsv2/", {"year": year, "pid": pid, "split": split, "lid": lid})


def get_player_pitching_stats(
    year: Optional[int] = None,
    pid: Optional[int] = None,
    split: Optional[int] = None,
    lid: Optional[int] = None,
) -> list[dict[str, Any]]:
    """Fetch player pitching stats."""
    return _csv("/playerpitchstatsv2/", {"year": year, "pid": pid, "split": split, "lid": lid})


def get_player_fielding_stats(
    year: Optional[int] = None,
    pid: Optional[int] = None,
    split: Optional[int] = None,
    lid: Optional[int] = None,
) -> list[dict[str, Any]]:
    """Fetch player fielding stats."""
    return _csv("/playerfieldstatsv2/", {"year": year, "pid": pid, "split": split, "lid": lid})


def get_contracts() -> list[dict[str, Any]]:
    """Fetch all active contracts."""
    return _csv("/contract/")


def get_contract_extensions() -> list[dict[str, Any]]:
    """Fetch pending contract extensions."""
    return _csv("/contractextension/")


def get_teams() -> list[dict[str, Any]]:
    """Fetch all teams."""
    return _csv("/teams/")


def get_date() -> str:
    """Fetch current in-game date."""
    return _get("/date/").strip()


def get_exports() -> Any:
    """Check available data exports."""
    return _json("/exports/")


def get_team_batting_stats(
    year: Optional[int] = None,
    split: Optional[int] = None,
) -> list[dict[str, Any]]:
    """Fetch team-level batting stats."""
    return _csv("/teambatstats/", {"year": year, "split": split})


def get_team_pitching_stats(
    year: Optional[int] = None,
    split: Optional[int] = None,
) -> list[dict[str, Any]]:
    """Fetch team-level pitching stats."""
    return _csv("/teampitchstats/", {"year": year, "split": split})


def get_draft(lid: Optional[int] = None) -> list[dict[str, Any]]:
    """Fetch draft data."""
    return _csv("/draftv2/", {"lid": lid})


def get_game_history(year: Optional[int] = None) -> list[dict[str, Any]]:
    """Fetch game results."""
    return _csv("/gamehistory/", {"year": year})


def get_lgdata() -> Any:
    """League structure: leagues, subleagues, divisions, teams, standings."""
    return _json("/lgdata/")


def get_tradeblock() -> Any:
    """Player IDs on the trade block."""
    return _json("/tradeblock/")


def get_ballparks(lid: Optional[int] = None) -> Any:
    """Park factors for all ballparks."""
    return _json("/ballparks/", {"lid": lid})


def start_ratings_export() -> str:
    """Kick off the ratings export and return the poll URL.

    /ratings is rate-limited to once per 5 minutes per team. Short waits are
    slept through; longer cooldowns raise RateLimitedError so callers can tell
    the user how long to wait rather than blocking the refresh.
    """
    _RATINGS_MAX_BLOCKING_WAIT = 45
    resp = ""
    for attempt in range(3):
        resp = _fetch(f"{_base_url()}/ratings/", _retries=0)
        m = _WAIT_RE.search(resp)
        if m:
            secs = int(m.group(1))
            if secs <= _RATINGS_MAX_BLOCKING_WAIT and attempt < 2:
                log.info("ratings: rate limited — waiting %ds...", secs)
                time.sleep(secs + 2)
                continue
            raise RateLimitedError(
                secs,
                f"StatsPlus limits ratings pulls to once per 5 minutes per team. "
                f"Try again in about {secs} seconds.")
        break
    match = re.search(r"https?://\S+", resp)
    if not match:
        raise ValueError(f"Unexpected /ratings/ response: {resp}")
    return match.group(0).rstrip(".)")


def get_ratings(
    player_ids: Optional[list[int]] = None,
    poll_url: Optional[str] = None,
    skip_initial_wait: bool = False,
) -> list[dict[str, Any]]:
    """Fetch and parse player ratings (triggers export if needed).

    This is a long-running operation (~30-90 seconds) due to API export time.

    Args:
        player_ids: Optional filter to specific player IDs.
        poll_url: Pre-existing export URL (skip triggering new export).
        skip_initial_wait: Skip the initial 30s wait (for retries).

    Returns:
        List of player rating dicts with corrected column names.
    """
    if poll_url is None:
        poll_url = start_ratings_export()

    if not skip_initial_wait:
        log.info("ratings: waiting 30s for export...")
        time.sleep(30)

    for attempt in range(20):
        text = _fetch(poll_url)
        if "still in progress" not in text and not text.startswith("Request received"):
            text = _fix_ratings_header(text)
            rows = _parse_csv(text)
            log.info("ratings: parsed %d rows from %d bytes", len(rows), len(text))
            if not rows:
                first_500 = text[:500].replace("\n", "\\n")
                log.warning("ratings: empty parse — first 500 chars: %s", first_500)
            if player_ids:
                id_set = set(player_ids)
                rows = [r for r in rows if r.get("ID") in id_set]
            return rows
        log.info("ratings: not ready, waiting 15s... (attempt %d)", attempt + 1)
        time.sleep(15)

    raise TimeoutError("Ratings export timed out after ~5 minutes.")


# ---------------------------------------------------------------------------
# Ratings header repair
# ---------------------------------------------------------------------------

_RATINGS_EXPECTED_113 = (
    "ID,Name,Pos,League,Team,Org,LgLvl,Age,Height,Bats,Throws,"
    "Cntct,Gap,Pow,Eye,Ks,Cntct_R,Gap_R,Pow_R,Eye_R,Ks_R,"
    "Cntct_L,Gap_L,Pow_L,Eye_L,Ks_L,PotCntct,PotGap,PotPow,PotEye,PotKs,"
    "IFR,IFE,IFA,TDP,OFR,OFE,OFA,CBlk,CArm,CFrm,"
    "P,C,1B,2B,3B,SS,LF,CF,RF,PotP,PotC,Pot1B,Pot2B,Pot3B,PotSS,PotLF,PotCF,PotRF,"
    "Speed,StlRt,Steal,Run,SacBunt,BuntHit,"
    "Stf,Mov,Ctrl,Stf_R,Mov_R,Ctrl_R,Stf_L,Mov_L,Ctrl_L,"
    "PotStf,PotMov,PotCtrl,Vel,GB,Stm,Hold,"
    "Fst,Snk,Cutt,Crv,Sld,Chg,Splt,Frk,CirChg,Scr,Kncrv,Knbl,"
    "PotFst,PotSnk,PotCutt,PotCrv,PotSld,PotChg,PotSplt,PotFrk,"
    "PotCirChg,PotScr,PotKncrv,PotKnbl,"
    "Int,WrkEthic,Greed,Loy,Lead,Acc,Ovr,Pot"
).split(",")

_RATINGS_EXPECTED_126 = (
    "ID,Name,Pos,League,Team,Org,LgLvl,Age,Height,Bats,Throws,"
    "Cntct,Gap,Pow,Eye,Ks,BABIP,Cntct_R,Gap_R,Pow_R,Eye_R,Ks_R,BABIP_R,"
    "Cntct_L,Gap_L,Pow_L,Eye_L,Ks_L,BABIP_L,PotCntct,PotGap,PotPow,PotEye,PotKs,PotBABIP,"
    "IFR,IFE,IFA,TDP,OFR,OFE,OFA,CBlk,CArm,CFrm,"
    "P,C,1B,2B,3B,SS,LF,CF,RF,PotP,PotC,Pot1B,Pot2B,Pot3B,PotSS,PotLF,PotCF,PotRF,"
    "Speed,StlRt,Steal,Run,SacBunt,BuntHit,"
    "Stf,Mov,HRA,PBABIP,Ctrl,Stf_R,Mov_R,HRA_R,PBABIP_R,"
    "Ctrl_R,Stf_L,Mov_L,HRA_L,PBABIP_L,Ctrl_L,"
    "PotStf,PotMov,PotHRA,PotPBABIP,PotCtrl,Vel,GB,Stm,Hold,"
    "Fst,Snk,Cutt,Crv,Sld,Chg,Splt,Frk,CirChg,Scr,Kncrv,Knbl,"
    "PotFst,PotSnk,PotCutt,PotCrv,PotSld,PotChg,PotSplt,PotFrk,"
    "PotCirChg,PotScr,PotKncrv,PotKnbl,"
    "Int,WrkEthic,Greed,Loy,Lead,Prone,Acc,Ovr,Pot"
).split(",")

# Newer OOTP export (127 cols). Drops Ovr, Pot, Prone — leagues that don't
# surface OVR/POT (e.g. PPL) never populated these anyway. Adds GBType, FBType,
# PotVel, ArmSlot. The three Ctrl columns are already correctly labeled.
_RATINGS_EXPECTED_127 = (
    "ID,Name,Pos,League,Team,Org,LgLvl,Age,Height,Bats,Throws,"
    "Cntct,Gap,Pow,Eye,Ks,BABIP,Cntct_R,Gap_R,Pow_R,Eye_R,Ks_R,BABIP_R,"
    "Cntct_L,Gap_L,Pow_L,Eye_L,Ks_L,BABIP_L,PotCntct,PotGap,PotPow,PotEye,PotKs,PotBABIP,"
    "IFR,IFE,IFA,TDP,OFR,OFE,OFA,CBlk,CArm,CFrm,"
    "P,C,1B,2B,3B,SS,LF,CF,RF,PotP,PotC,Pot1B,Pot2B,Pot3B,PotSS,PotLF,PotCF,PotRF,"
    "Speed,StlRt,Steal,Run,SacBunt,BuntHit,GBType,FBType,"
    "Stf,Mov,HRA,PBABIP,Ctrl,Stf_R,Mov_R,HRA_R,PBABIP_R,"
    "Ctrl_R,Stf_L,Mov_L,HRA_L,PBABIP_L,Ctrl_L,"
    "PotStf,PotMov,PotHRA,PotPBABIP,PotCtrl,Vel,PotVel,ArmSlot,GB,Stm,Hold,"
    "Fst,Snk,Cutt,Crv,Sld,Chg,Splt,Frk,CirChg,Scr,Kncrv,Knbl,"
    "PotFst,PotSnk,PotCutt,PotCrv,PotSld,PotChg,PotSplt,PotFrk,"
    "PotCirChg,PotScr,PotKncrv,PotKnbl,"
    "Int,WrkEthic,Greed,Loy,Lead,Acc"
).split(",")

_RATINGS_KNOWN_FORMATS: dict[int, list[str]] = {
    113: _RATINGS_EXPECTED_113,
    126: _RATINGS_EXPECTED_126,
    127: _RATINGS_EXPECTED_127,
}


def _fix_ratings_header(text: str) -> str:
    """Fix known API header issues and validate against expected columns."""
    lines = text.split("\n", 1)
    if len(lines) < 2:
        return text

    cols = lines[0].split(",")

    # Find the three Ctrl columns
    has_plain_ctrl = "Ctrl" in cols
    ctrl_r_idx: Optional[int] = None
    ctrl_l_indices: list[int] = []
    for i, c in enumerate(cols):
        if c == "Ctrl_R":
            ctrl_r_idx = i
        elif c == "Ctrl_L":
            ctrl_l_indices.append(i)

    # Only repair the mislabeled legacy header. Newer exports label the three
    # Ctrl columns correctly; if a plain "Ctrl" is already present the header is
    # well-formed and the rename would corrupt it.
    if has_plain_ctrl:
        pass  # already correct — no repair needed
    elif ctrl_r_idx is not None and len(ctrl_l_indices) >= 2:
        cols[ctrl_r_idx] = "Ctrl"
        cols[ctrl_l_indices[0]] = "Ctrl_R"
    elif ctrl_r_idx is not None and len(ctrl_l_indices) == 1:
        cols[ctrl_r_idx] = "Ctrl"
        cols[ctrl_l_indices[0]] = "Ctrl_R"
    elif len(ctrl_l_indices) >= 2:
        cols[ctrl_l_indices[1]] = "Ctrl"

    header = ",".join(cols)

    # Validate against known formats
    actual = header.split(",")
    expected = _RATINGS_KNOWN_FORMATS.get(len(actual))
    if expected and actual != expected:
        diff = [(i, e, a) for i, (e, a) in enumerate(zip(expected, actual)) if e != a]
        log.warning(
            "Ratings CSV header: %d columns match count but %d differ: %s",
            len(actual), len(diff), diff[:5],
        )
    elif not expected:
        closest = min(_RATINGS_KNOWN_FORMATS.values(), key=lambda f: abs(len(f) - len(actual)))
        added = set(actual) - set(closest)
        removed = set(closest) - set(actual)
        parts: list[str] = []
        if added:
            parts.append(f"new columns: {sorted(added)}")
        if removed:
            parts.append(f"removed columns: {sorted(removed)}")
        parts.append(f"column count {len(actual)} (known: {sorted(_RATINGS_KNOWN_FORMATS.keys())})")
        log.warning("Ratings CSV header changed — %s", "; ".join(parts))

    lines[0] = header
    return "\n".join(lines)
