"""fetch_missing_batting_splits.py — backfills missing vs-L/vs-R batting
splits (batting_stats.split_id 2/3) from StatsPlus's own player pages.

Why this exists: the sanctioned CSV/API export (statsplus/client.py) doesn't
carry handedness splits at all — they only exist as an AJAX call on each
player's own website page (POST /playerajax/ with
{'info': 'hitstats', 'pid': ..., 'gameOpt': 'reg', 'splitOpt': 'r'|'l'}),
same auth pattern as draft_clock.py / fetch_site_draft_value.py (the
session cookie, not the token). Most players already have full split
coverage from the normal sync; this fills in the specific players/years
where that coverage is missing (see the diagnostic this was built from:
comparing SUM(pa) at split_id=1 vs split_id IN (2,3) per player-year).

Usage:
    python3 scripts/fetch_missing_batting_splits.py --league-dir data/ppl \
        --pids 24233,25323,25189,26164,26612,25159,24723,27457,27407,27460,27358 \
        --year 1954
"""

from __future__ import annotations

import argparse
import re
import sqlite3
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE / "src"))

_USER_AGENT = "Mozilla/5.0 (compatible; statsplusplus-dominator/1.0)"
_STATSPLUS_ENTRY = "https://statsplus.net"

# Fixed column order in the "Career Major League Stats" table — see
# fetch_site_draft_value.py's docstring for why positional parsing beats
# relying on the data-category attributes (those numeric ids aren't
# self-explanatory and aren't worth reverse-engineering when the header
# row already gives an unambiguous, stable column order).
_COLS = ["team_href", "year", "age", "g", "pa", "ab", "h", "d", "t", "hr",
         "rbi", "r", "bb", "k", "sb", "cs", "avg", "obp", "slg", "ops",
         "babip", "woba", "wrcplus", "war"]


def _resolve_host(slug: str) -> str:
    req = urllib.request.Request(
        f"{_STATSPLUS_ENTRY}/{slug}/draft/", headers={"User-Agent": _USER_AGENT})
    resp = urllib.request.urlopen(req, timeout=15)
    return resp.geturl().split("/" + slug + "/")[0]


def _fetch_split(host, slug, cookie, pid, split_opt):
    data = urllib.parse.urlencode({
        "info": "hitstats", "pid": pid, "gameOpt": "reg", "splitOpt": split_opt,
    }).encode()
    req = urllib.request.Request(
        f"{host}/{slug}/playerajax/", data=data,
        headers={
            "Cookie": cookie, "User-Agent": _USER_AGENT,
            "X-Requested-With": "XMLHttpRequest",
            "Referer": f"{host}/{slug}/player/{pid}?page=hit",
        })
    return urllib.request.urlopen(req, timeout=20).read().decode("utf-8", errors="ignore")


def _num(s):
    s = (s or "").strip()
    if s in ("", "-", "—"):
        return None
    try:
        return float(s) if "." in s else int(s)
    except ValueError:
        return None


def _parse_season_rows(html):
    """Yields dicts for each per-team-per-year row in the FIRST table only
    (the "Career Major League Stats" table) — stops before the 'Total'
    grid-break rows, which aren't per-team-year data."""
    m = re.search(r"<tbody>(.*?)</tbody>", html, re.S)
    if not m:
        return
    body = m.group(1)
    for row_html in re.findall(r"<tr[^>]*>(.*?)</tr>", body, re.S):
        if "grid-break" in row_html:
            break
        team_m = re.search(r'/ppl/team/(\d+)"[^>]*>(\w+)</a>\s*-\s*MLB', row_html)
        if not team_m:
            continue
        team_id = int(team_m.group(1))
        cells = re.findall(r"<td[^>]*>(.*?)</td>", row_html, re.S)
        if len(cells) < len(_COLS) - 1:
            continue
        strip = lambda c: re.sub(r"<[^>]+>", "", c).strip()
        vals = [strip(c) for c in cells]
        # cells[0] is the team cell (already extracted above); the rest
        # line up with _COLS[1:].
        row = {"team_id": team_id}
        for name, v in zip(_COLS[1:], vals[1:]):
            row[name] = _num(v) if name != "year" else int(v)
        yield row


def backfill(league_dir: Path, pids: list[int], years: set[int] | None, dry_run: bool):
    from statsplusplus.config.league_context import get_statsplus_cookie
    from statsplusplus.config.league_config import LeagueConfig

    cfg = LeagueConfig(base_dir=league_dir)
    slug = cfg.settings.get("statsplus_slug", "")
    cookie = get_statsplus_cookie(league_dir)
    if not slug or not cookie:
        print("No statsplus_slug or session cookie configured.", file=sys.stderr)
        sys.exit(1)
    host = _resolve_host(slug)

    conn = sqlite3.connect(league_dir / "league.db")
    conn.row_factory = sqlite3.Row
    inserted, skipped, errors = 0, 0, 0

    for pid in pids:
        for split_opt, split_id in (("l", 2), ("r", 3)):
            try:
                html = _fetch_split(host, slug, cookie, pid, split_opt)
            except urllib.error.HTTPError as e:
                print(f"pid={pid} split={split_opt}: HTTP {e.code}")
                errors += 1
                continue
            for row in _parse_season_rows(html):
                if years and row["year"] not in years:
                    continue
                existing = conn.execute(
                    "SELECT 1 FROM batting_stats WHERE player_id=? AND year=? AND team_id=? AND split_id=?",
                    (pid, row["year"], row["team_id"], split_id),
                ).fetchone()
                if existing:
                    skipped += 1
                    continue
                if dry_run:
                    print(f"[dry-run] would insert pid={pid} year={row['year']} team={row['team_id']} "
                          f"split={split_id} pa={row['pa']} avg={row['avg']} obp={row['obp']} slg={row['slg']}")
                    inserted += 1
                    continue
                conn.execute("""
                    INSERT INTO batting_stats
                        (player_id, year, team_id, split_id, ab, h, d, t, hr, r, rbi, sb,
                         bb, k, avg, obp, slg, war, pa, cs)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (pid, row["year"], row["team_id"], split_id,
                      row["ab"], row["h"], row["d"], row["t"], row["hr"], row["r"], row["rbi"], row["sb"],
                      row["bb"], row["k"], row["avg"], row["obp"], row["slg"], row["war"], row["pa"], row["cs"]))
                inserted += 1
    if not dry_run:
        conn.commit()
    print(f"Inserted {inserted}, already-present {skipped}, errors {errors}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--league-dir", default="data/ppl")
    ap.add_argument("--pids", required=True, help="comma-separated player ids")
    ap.add_argument("--year", type=int, action="append", help="restrict to this year (repeatable)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    pids = [int(p) for p in args.pids.split(",") if p.strip()]
    years = set(args.year) if args.year else None
    backfill(Path(args.league_dir), pids, years, args.dry_run)
