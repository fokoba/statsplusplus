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
import urllib.request
from pathlib import Path

log = logging.getLogger("statspp.client")

# Deferred credential resolution — no module-level env reads.
_league_url = None
_cookie = None


def configure(league_url: str, cookie: str):
    """Set credentials explicitly (used by onboarding, tests, etc.)."""
    global _league_url, _cookie
    _league_url = league_url
    _cookie = cookie


def _resolve_creds():
    """Resolve credentials lazily. Priority: configure() > league_context > .env."""
    global _league_url, _cookie
    if _league_url and _cookie:
        return _league_url, _cookie
    # Try league_context (new multi-league path)
    try:
        from statsplusplus.config.league_context import get_league_dir, get_statsplus_cookie
        cookie = get_statsplus_cookie()
        league_dir = get_league_dir()
        settings_path = league_dir / "config" / "league_settings.json"
        if settings_path.exists():
            settings = json.loads(settings_path.read_text())
            slug = settings.get("statsplus_slug", "")
            if slug and cookie:
                _league_url = slug
                _cookie = cookie
                return _league_url, _cookie
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
    if not _league_url or not _cookie:
        raise RuntimeError("StatsPlus credentials not configured. Set via configure(), app_config.json, or statsplus/.env")
    return _league_url, _cookie


def _base_url():
    slug, _ = _resolve_creds()
    return f"https://statsplus.net/{slug}/api"


class CookieExpiredError(Exception):
    """Raised when StatsPlus returns a login-required response."""
    pass


def _fetch(url: str) -> str:
    _, cookie = _resolve_creds()
    req = urllib.request.Request(url, headers={"Cookie": cookie, "Accept": "application/json"})
    with urllib.request.urlopen(req) as r:
        body = r.read().decode()
    if "requires user to be logged in" in body:
        raise CookieExpiredError(
            "StatsPlus session expired — update your cookie in Settings.")
    return body


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

_RATINGS_KNOWN_FORMATS = {113: _RATINGS_EXPECTED_113, 126: _RATINGS_EXPECTED_126}


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
    ctrl_r_idx = None
    ctrl_l_indices = []
    for i, c in enumerate(cols):
        if c == "Ctrl_R":
            ctrl_r_idx = i
        elif c == "Ctrl_L":
            ctrl_l_indices.append(i)

    if ctrl_r_idx is not None and len(ctrl_l_indices) >= 2:
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
    """Kick off the ratings export and return the poll URL without waiting."""
    for attempt in range(5):
        resp = _get("/ratings/")
        wait = re.search(r'wait (\d+) seconds', resp)
        if wait:
            secs = int(wait.group(1))
            log.info(f"Rate limited — waiting {secs}s...")
            time.sleep(secs + 2)
            continue
        if "being updated" in resp.lower():
            log.info("Ratings still being updated league-side — waiting 60s... (attempt %d)", attempt + 1)
            time.sleep(60)
            continue
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
