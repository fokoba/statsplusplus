"""fetch_site_draft_value.py — pulls StatsPlus's own draft-value data.

Two pages, not exposed by the sanctioned CSV/API client, only as rendered
HTML on the website (same auth pattern as draft_clock.py — the stored
session cookie, not the token):

  /ppl/draftvalue/          — career WAR by exact overall pick (1949-2020,
                               unless start/end year params are passed),
                               including the site's own exponential-curve
                               "Expected WAR" fit at each pick.
  /ppl/surplusdraftvalue/   — per-team total/expected/surplus draft WAR,
                               built on the same Expected WAR curve.

This is a manual, one-time-per-refresh pull (not a background sync) — the
league's own draft history only grows once a year, per completed draft.
Re-run this script by hand after each draft to update the snapshot.

Output: two JSON files under <league_dir>/config/:
  site_draft_value.json  — list of {pick, min_war, max_war, total_war,
                            avg_war, majors_count, expected_war}
  site_draft_skill.json  — list of {team_id, team_name, total_war,
                            expected_war, surplus_war, surplus_war_plus,
                            surplus_wo_surprises, lottery_tickets, lottery_war}

Usage:
    python3 scripts/fetch_site_draft_value.py [--league-dir data/ppl]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.request
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE / "src"))

_USER_AGENT = "Mozilla/5.0 (compatible; statsplusplus-dominator/1.0)"
_STATSPLUS_ENTRY = "https://statsplus.net"


def _resolve_host(slug: str) -> str:
    req = urllib.request.Request(
        f"{_STATSPLUS_ENTRY}/{slug}/draft/", headers={"User-Agent": _USER_AGENT})
    resp = urllib.request.urlopen(req, timeout=15)
    return resp.geturl().split("/" + slug + "/")[0]


def _fetch(url: str, cookie: str) -> str:
    req = urllib.request.Request(url, headers={"Cookie": cookie, "User-Agent": _USER_AGENT})
    return urllib.request.urlopen(req, timeout=30).read().decode("utf-8", errors="ignore")


def _num(s: str):
    s = s.strip()
    if s in ("", "-"):
        return None
    try:
        return float(s) if "." in s else int(s)
    except ValueError:
        return None


def _parse_draft_value(html: str) -> list[dict]:
    m = re.search(r'class="[^"]*draftvalue-table[^"]*".*?<tbody>(.*?)</tbody>', html, re.S)
    if not m:
        return []
    rows = []
    for row_html in re.findall(r"<tr[^>]*>(.*?)</tr>", m.group(1), re.S):
        cells = re.findall(r"<td[^>]*>(.*?)</td>", row_html, re.S)
        if len(cells) < 9:
            continue
        pick_m = re.search(r">(\d+)<", cells[0])
        if not pick_m:
            continue
        strip = lambda c: re.sub(r"<[^>]+>", "", c).strip()
        rows.append({
            "pick": int(pick_m.group(1)),
            "min_war": _num(strip(cells[1])),
            "max_war": _num(strip(cells[2])),
            "total_war": _num(strip(cells[3])),
            "avg_war": _num(strip(cells[4])),
            "majors_count": _num(strip(cells[5])),
            "expected_war": _num(strip(cells[6])),
        })
    return rows


def _parse_draft_skill(html: str) -> list[dict]:
    m = re.search(r'class="[^"]*surplusdraftvalue-table[^"]*".*?<tbody>(.*?)</tbody>', html, re.S)
    if not m:
        return []
    rows = []
    for row_html in re.findall(r"<tr[^>]*>(.*?)</tr>", m.group(1), re.S):
        cells = re.findall(r"<td[^>]*>(.*?)</td>", row_html, re.S)
        if len(cells) < 9:
            continue
        tid_m = re.search(r"/ppl/team/(\d+)", cells[1])
        name_m = re.search(r'class="wide oneline"><a[^>]*>(.*?)</a>', cells[1])
        if not tid_m:
            continue
        strip = lambda c: re.sub(r"<[^>]+>", "", c).strip()
        rows.append({
            "team_id": int(tid_m.group(1)),
            "team_name": name_m.group(1).strip() if name_m else None,
            "total_war": _num(strip(cells[2])),
            "expected_war": _num(strip(cells[3])),
            "surplus_war": _num(strip(cells[4])),
            "surplus_war_plus": _num(strip(cells[5])),
            "surplus_wo_surprises": _num(strip(cells[6])),
            "lottery_tickets": _num(strip(cells[7])),
            "lottery_war": _num(strip(cells[8])),
        })
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--league-dir", default="data/ppl")
    ap.add_argument("--start-year", type=int, default=None)
    ap.add_argument("--end-year", type=int, default=None)
    args = ap.parse_args()

    from statsplusplus.config.league_context import get_statsplus_cookie
    from statsplusplus.config.league_config import LeagueConfig

    league_dir = Path(args.league_dir)
    cfg = LeagueConfig(base_dir=league_dir)
    slug = cfg.settings.get("statsplus_slug", "")
    cookie = get_statsplus_cookie(league_dir)
    if not slug or not cookie:
        print("No statsplus_slug or session cookie configured for this league.", file=sys.stderr)
        sys.exit(1)

    host = _resolve_host(slug)
    yr_q = ""
    if args.start_year or args.end_year:
        yr_q = f"?startyear={args.start_year or ''}&endyear={args.end_year or ''}"

    dv_html = _fetch(f"{host}/{slug}/draftvalue/{yr_q}", cookie)
    dv_rows = _parse_draft_value(dv_html)
    sv_html = _fetch(f"{host}/{slug}/surplusdraftvalue/{yr_q}", cookie)
    sv_rows = _parse_draft_skill(sv_html)

    out_dir = league_dir / "config"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "site_draft_value.json").write_text(json.dumps(dv_rows, indent=2))
    (out_dir / "site_draft_skill.json").write_text(json.dumps(sv_rows, indent=2))

    print(f"Wrote {len(dv_rows)} pick rows to {out_dir / 'site_draft_value.json'}")
    print(f"Wrote {len(sv_rows)} team rows to {out_dir / 'site_draft_skill.json'}")


if __name__ == "__main__":
    main()
