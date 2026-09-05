"""
StatsPlus API client. Credentials resolved at call time from league context.
All methods return parsed JSON (lists of dicts) or raise on error.
"""

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

log = logging.getLogger("statspp.client")

# Deferred credential resolution — no module-level env reads.
_league_url = None
_cookie = None
_token = None


def configure(league_url: str, cookie: str, token: str = ""):
    """Set credentials explicitly (used by onboarding, tests, etc.)."""
    global _league_url, _cookie, _token
    _league_url = league_url
    _cookie = cookie
    _token = token


def _resolve_creds():
    """Resolve credentials lazily. Priority: configure() > league_context > .env.

    Returns ``(league_url, cookie, token)``. The token is the sanctioned
    per-team API token (https://wiki.statsplus.net/web-tools/statsplus-api),
    preferred over the session cookie when configured; the cookie remains the
    fallback for leagues that haven't set a token yet.
    """
    global _league_url, _cookie, _token
    if _league_url and (_cookie or _token):
        return _league_url, _cookie, _token
    # Try league_context (new multi-league path)
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
                _league_url = slug
                _cookie = cookie
                _token = token
                return _league_url, _cookie, _token
    except Exception:
        pass
    # Legacy fallback: .env file
    env_path = Path(__file__).parent / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip())
    _league_url = os.environ.get("STATSPLUS_LEAGUE_URL", "")
    _cookie = os.environ.get("STATSPLUS_COOKIE", "")
    _token = os.environ.get("STATSPLUS_TOKEN", "")
    if not _league_url or not (_cookie or _token):
        raise RuntimeError("StatsPlus credentials not configured. Set via configure(), app_config.json, or statsplus/.env")
    return _league_url, _cookie, _token


def _base_url():
    slug, _, _ = _resolve_creds()
    return f"https://statsplus.net/{slug}/api"


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
    block on (e.g. the /ratings once-per-5-minutes-per-team cooldown). Carries
    the number of seconds to wait so callers can surface it to the user."""
    def __init__(self, seconds: int, message: str = ""):
        self.seconds = seconds
        super().__init__(message or f"Rate limited — try again in {seconds} seconds.")


# StatsPlus runs bot filtering that serves a login page to requests with no
# User-Agent. Identify the tool so the request isn't filtered. See:
# https://wiki.statsplus.net/web-tools/statsplus-api#authentication
_USER_AGENT = "statsplusplus/1.0 (+https://github.com/statsplusplus)"

# Per the StatsPlus API docs, several errors are returned as HTTP 200 with a
# plain-text body (rate-limit waits, expired credentials, "ratings updating",
# etc.). A tool that only checks the status code saves the message into the
# file where it expected data. We detect these human-message bodies and branch:
# rate-limit → retry; auth → raise; transient → retry.
# https://wiki.statsplus.net/web-tools/statsplus-api  (Status codes and response types)
_WAIT_RE = re.compile(r"wait (\d+) seconds", re.IGNORECASE)
_RATE_LIMIT_MAX_RETRIES = 4

# Human-message markers (matched case-insensitively against the response body).
_MSG_TOKEN_EXPIRED = "api token has expired"
_MSG_TOKEN_INVALID = "invalid or unknown api token"
_MSG_LOGIN_REQUIRED = "requires user to be logged in"
# Transient "try again shortly" messages that are not a fixed wait-N-seconds.
_MSG_RATINGS_UPDATING = "ratings are being updated"
_MSG_IN_PROGRESS = "still in progress"


def _fetch(url: str, _retries: int = _RATE_LIMIT_MAX_RETRIES) -> str:
    _, cookie, token = _resolve_creds()
    headers = {"Accept": "application/json", "User-Agent": _USER_AGENT}
    if token and "token=" not in url:
        # Sanctioned auth: per-team token as a query param, no session cookie.
        # https://wiki.statsplus.net/web-tools/statsplus-api
        # Skip if the URL already carries a token= param — export poll URLs
        # (e.g. /api/mycsv/?request=...&token=...) embed their own per-job
        # token, and appending ours a second time breaks the poll.
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}token={token}"
    elif not token:
        headers["Cookie"] = cookie

    for attempt in range(_retries + 1):
        req = urllib.request.Request(url, headers=headers)
        ctype = ""
        try:
            with urllib.request.urlopen(req) as r:
                ctype = (r.headers.get("Content-Type") or "").lower()
                body = r.read().decode()
        except urllib.error.HTTPError as e:
            # HTTP 429: team-stats render limit. Honor Retry-After, else back off.
            if e.code == 429 and attempt < _retries:
                retry_after = e.headers.get("Retry-After") if e.headers else None
                wait = int(retry_after) if retry_after and retry_after.isdigit() else 35
                log.info("Rate limited (429) — waiting %ds (attempt %d)", wait, attempt + 1)
                time.sleep(wait + 2)
                continue
            raise
        # Content-type / human-message guard: StatsPlus returns several errors as
        # HTTP 200 with a text/plain body. Only inspect the body as a "human
        # message" when it isn't the expected data content-type (CSV/JSON), so
        # legitimate data that happens to contain a keyword isn't misclassified.
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
                log.info("StatsPlus data not ready (%s) — retrying (attempt %d)",
                         body.strip()[:60], attempt + 1)
                time.sleep(15)
                continue
            # decision == "data" (or retries exhausted): fall through and return.
        return body
    return body


def _classify_message(body: str) -> str:
    """Classify an API response body into a handling decision.

    Returns one of: ``"auth_token"``, ``"auth_cookie"``, ``"wait"``,
    ``"transient"``, or ``"data"``. Only the plain-text human messages the
    StatsPlus API documents are recognized; anything else is treated as data.
    Note: the ratings-export poll expects ``still in progress`` bodies and
    handles them itself, so those are classified as data here (the poll loop
    inspects them), not as a generic transient error.
    """
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


def _get(path: str, params: dict = {}) -> str:
    qs = "&".join(f"{k}={v}" for k, v in params.items() if v is not None)
    url = f"{_base_url()}{path}" + (f"?{qs}" if qs else "")
    return _fetch(url)


def _parse_csv(text: str) -> list[dict]:
    if not text.strip():
        return []
    rows = []
    for row in csv.DictReader(io.StringIO(text)):
        coerced = {}
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


def _csv(path: str, params: dict = {}) -> list[dict]:
    return _parse_csv(_get(path, params))


def _json(path: str, params: dict = {}) -> dict | list:
    return json.loads(_get(path, params))


# --- Endpoints ---

def get_players() -> list[dict]:
    return _csv("/players/")

def get_player_batting_stats(year: int = None, pid: int = None, split: int = None, lid: int = None) -> list[dict]:
    return _csv("/playerbatstatsv2/", {"year": year, "pid": pid, "split": split, "lid": lid})

def get_player_pitching_stats(year: int = None, pid: int = None, split: int = None, lid: int = None) -> list[dict]:
    return _csv("/playerpitchstatsv2/", {"year": year, "pid": pid, "split": split, "lid": lid})

def get_player_fielding_stats(year: int = None, pid: int = None, split: int = None, lid: int = None) -> list[dict]:
    return _csv("/playerfieldstatsv2/", {"year": year, "pid": pid, "split": split, "lid": lid})

def get_contracts() -> list[dict]:
    return _csv("/contract/")

def get_contract_extensions() -> list[dict]:
    return _csv("/contractextension/")

def get_teams() -> list[dict]:
    return _csv("/teams/")

def get_date() -> str:
    return _get("/date/").strip()


def tokencheck(slug: str, token: str) -> tuple[bool, str]:
    """Validate a StatsPlus API token against a league.

    Uses the documented /tokencheck endpoint
    (https://wiki.statsplus.net/web-tools/statsplus-api): returns the team ID
    (plain text) on success, or HTTP 400 with "Invalid Token" / "Token expired".

    Args:
        slug: League URL slug (e.g. "emlb").
        token: The per-team API token to validate.

    Returns:
        (ok, detail). On success ``ok`` is True and ``detail`` is the team ID.
        On failure ``ok`` is False and ``detail`` is a human-readable reason.
    """
    if not token:
        return False, "No token provided"
    url = f"https://statsplus.net/{slug}/api/tokencheck/?token={token}"
    req = urllib.request.Request(
        url, headers={"Accept": "text/plain", "User-Agent": _USER_AGENT})
    try:
        with urllib.request.urlopen(req) as r:
            body = r.read().decode().strip()
        # Success is a team ID; but guard against HTTP-200 message bodies too.
        decision = _classify_message(body)
        if decision == "auth_token":
            return False, "Token expired or invalid"
        return True, body
    except urllib.error.HTTPError as e:
        try:
            detail = e.read().decode().strip() or f"HTTP {e.code}"
        except Exception:
            detail = f"HTTP {e.code}"
        return False, detail
    except Exception as e:
        return False, str(e)


def get_exports() -> dict:
    return _json("/exports/")

def get_team_batting_stats(year: int = None, split: int = None) -> list[dict]:
    return _csv("/teambatstats/", {"year": year, "split": split})

def get_team_pitching_stats(year: int = None, split: int = None) -> list[dict]:
    return _csv("/teampitchstats/", {"year": year, "split": split})

def get_draft(lid: int = None) -> list[dict]:
    return _csv("/draftv2/", {"lid": lid})

def get_game_history(year: int = None) -> list[dict]:
    return _csv("/gamehistory/", {"year": year})

def get_lgdata() -> dict:
    """League structure: leagues, subleagues, divisions, teams, standings."""
    return _json("/lgdata/")

def get_tradeblock() -> dict:
    """Player IDs on the trade block. Returns {"player_ids": [...]}."""
    return _json("/tradeblock/")

def get_ballparks(lid: int = None) -> dict:
    """Park factors for all ballparks. Optional lid for specific league."""
    return _json("/ballparks/", {"lid": lid})

# --- Ratings header repair and validation ---

# Expected column names from the ratings CSV (in order). The API sends a
# duplicate "Ctrl_L" at position 73/83 which is actually overall Ctrl — we
# rename it to "Ctrl" before parsing. This list reflects the CORRECTED header.
# Two known formats exist: 113-col (older OOTP) and 126-col (adds BABIP, HRA,
# PBABIP splits + PotBABIP/PotHRA/PotPBABIP + Prone).
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
# surface OVR/POT (e.g. PPL) never populated these anyway. Adds pitcher
# batted-ball type descriptors (GBType, FBType), potential velocity (PotVel),
# and arm slot (ArmSlot). The three Ctrl columns are already correctly labeled.
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

_RATINGS_KNOWN_FORMATS = {
    113: _RATINGS_EXPECTED_113,
    126: _RATINGS_EXPECTED_126,
    127: _RATINGS_EXPECTED_127,
}


def _fix_ratings_header(text: str) -> str:
    """Fix known API header issues and validate against expected columns.

    Known issue: the API mislabels all three Ctrl columns. The data order is
    correct (overall, vs_R, vs_L) but the labels are wrong:
      - Position with overall Ctrl is labeled "Ctrl_R"
      - Position with Ctrl vs R is labeled "Ctrl_L"
      - Position with Ctrl vs L is labeled "Ctrl_L" (duplicate)
    We fix by renaming based on the pattern: Ctrl_R → Ctrl, first Ctrl_L → Ctrl_R,
    second Ctrl_L → Ctrl_L (no-op, but the first rename frees the name).
    """
    lines = text.split("\n", 1)
    if len(lines) < 2:
        return text

    cols = lines[0].split(",")

    # Find the three Ctrl columns by scanning for the pattern
    has_plain_ctrl = "Ctrl" in cols
    ctrl_r_idx = None
    ctrl_l_indices = []
    for i, c in enumerate(cols):
        if c == "Ctrl_R":
            ctrl_r_idx = i
        elif c == "Ctrl_L":
            ctrl_l_indices.append(i)

    # Only repair the mislabeled legacy header. Newer exports label the three
    # Ctrl columns correctly (Ctrl / Ctrl_R / Ctrl_L). If a plain "Ctrl" column
    # is already present, the header is well-formed — running the rename would
    # corrupt it (turning Ctrl_R → Ctrl duplicate, Ctrl_L → Ctrl_R).
    if has_plain_ctrl:
        pass  # already correct — no repair needed
    elif ctrl_r_idx is not None and len(ctrl_l_indices) >= 2:
        # API sends: Ctrl_R (=overall), Ctrl_L (=vs_R), Ctrl_L (=vs_L)
        # Fix to:    Ctrl,              Ctrl_R,          Ctrl_L
        cols[ctrl_r_idx] = "Ctrl"
        cols[ctrl_l_indices[0]] = "Ctrl_R"
        # ctrl_l_indices[1] stays as Ctrl_L
    elif ctrl_r_idx is not None and len(ctrl_l_indices) == 1:
        # Only one Ctrl_L — just need to add overall Ctrl
        cols[ctrl_r_idx] = "Ctrl"
        cols[ctrl_l_indices[0]] = "Ctrl_R"
        # No second Ctrl_L to keep — insert would shift columns, skip
    elif len(ctrl_l_indices) >= 2:
        # No Ctrl_R but two Ctrl_L — old assumption: second is overall
        cols[ctrl_l_indices[1]] = "Ctrl"

    header = ",".join(cols)

    # Validate: compare corrected header against known column formats
    actual = header.split(",")
    expected = _RATINGS_KNOWN_FORMATS.get(len(actual))
    if expected and actual == expected:
        pass  # Known format, no warning needed
    elif expected:
        # Right count but different columns — something shifted
        diff = [(i, e, a) for i, (e, a) in enumerate(zip(expected, actual)) if e != a]
        log.warning("Ratings CSV header: %d columns match count but %d differ: %s",
                     len(actual), len(diff), diff[:5])
    else:
        # Unknown column count — log full diff against closest known format
        closest = min(_RATINGS_KNOWN_FORMATS.values(), key=lambda f: abs(len(f) - len(actual)))
        added = set(actual) - set(closest)
        removed = set(closest) - set(actual)
        parts = []
        if added:
            parts.append(f"new columns: {sorted(added)}")
        if removed:
            parts.append(f"removed columns: {sorted(removed)}")
        parts.append(f"column count {len(actual)} (known: {sorted(_RATINGS_KNOWN_FORMATS.keys())})")
        log.warning("Ratings CSV header changed — %s", '; '.join(parts))

    lines[0] = header
    return "\n".join(lines)


def start_ratings_export() -> str:
    """Kick off the ratings export and return the poll URL.

    /ratings is rate-limited to once per 5 minutes per team. Rather than block
    a refresh (and its lock) for a multi-minute cooldown, short waits are slept
    through and longer ones are surfaced as RateLimitedError so the caller can
    tell the user exactly how long to wait.
    """
    _RATINGS_MAX_BLOCKING_WAIT = 45  # sleep through brief waits; surface longer ones
    for attempt in range(3):
        # _retries=0: inspect the wait ourselves instead of _fetch auto-sleeping.
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
    match = re.search(r'https?://\S+', resp)
    if not match:
        raise ValueError(f"Unexpected /ratings/ response: {resp}")
    return match.group(0).rstrip(".)")


def get_ratings(player_ids: list[int] = None, poll_url: str = None, skip_initial_wait: bool = False) -> list[dict]:
    if poll_url is None:
        poll_url = start_ratings_export()
    # Minimum 30s before first poll — export is never ready before then
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
                first_500 = text[:500].replace('\n', '\\n')
                log.warning("ratings: empty parse — first 500 chars: %s", first_500)
            if player_ids:
                id_set = set(player_ids)
                rows = [r for r in rows if r.get("ID") in id_set]
            return rows
        log.info("ratings: not ready, waiting 15s... (attempt %d)", attempt + 1)
        time.sleep(15)
    raise TimeoutError("Ratings export timed out after ~5 minutes.")
