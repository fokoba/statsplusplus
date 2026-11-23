"""How a player joined his current organization, from OOTP's transaction logs.

Source: the league's monthly HTML transaction pages that OOTP writes into
<league>.lg/news/html/leagues/league_200_all_transactions_<M>_<YYYY>.html.
Those cover trades, waiver claims, Rule 5 picks and all signings (with
bonus when OOTP logged one). Amateur-draft slot comes from the players table
(draft_year / round / pick / draft_team_id), which StatsPlus keeps.

Result for a player:
    {"main": "...", "sub": "..." | None, "tip": "..." | None}
  main – how he joined the current club (latest arrival)
  sub  – his origin when that differs (e.g. drafted by another team)
  tip  – hover detail (trade package each way)
"""

from __future__ import annotations

import html as _html
import re
from datetime import date
from pathlib import Path

HOME = Path.home()
_LEAGUE_HTML = {
    "ppl": [
        HOME / "Application Support/Out of the Park Developments/OOTP Baseball 26/saved_games/PPL.lg/news/html/leagues",
        HOME / "Library/Application Support/Out of the Park Developments/OOTP Baseball 26/saved_games/PPL.lg/news/html/leagues",
    ],
}

_MONTHS = {m: i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July",
     "August", "September", "October", "November", "December"], 1)}
_DATE_RE = re.compile(r"<th class=\"dl\">\s*\w+day, (\w+) (\d+)\w*\s*,\s*(\d{4})\s*</th>")
_ENTRY_RE = re.compile(r"<td class=\"dl wrap\">(.*?)</td>", re.S)
_P = r'<a href="[^"]*player_(\d+)\.html">'
_T = r'<a href="[^"]*team_(\d+)\.html">'
_MONEY = r"\$([\d,]+)"

_cache: dict = {"key": None, "events": {}}


def _folder(slug: str) -> Path | None:
    for f in _LEAGUE_HTML.get(slug, []):
        try:
            if f.is_dir():
                return f
        except OSError:
            pass
    return None


def _text(s: str) -> str:
    return re.sub(r"\s+", " ", _html.unescape(re.sub(r"<[^>]+>", "", s))).strip()


def _money(s: str | None) -> int | None:
    return int(s.replace(",", "")) if s else None


def _side(fragment: str) -> tuple[list[int], int]:
    """Player ids and cash in one side of a trade sentence."""
    pids = [int(x) for x in re.findall(_P, fragment)]
    cash = sum(_money(m) for m in re.findall(r"\$([\d,]+) in cash", fragment))
    return pids, cash


def _parse_entry(d: date, raw: str, ev: dict) -> None:
    def add(pid, **kw):
        ev.setdefault(int(pid), []).append({"date": d, **kw})

    # Trade: The [A] traded <X> to the [B], getting <Y> in return.
    m = re.search(r"^\s*The " + _T + r".*?</a> traded (.*?) to the " + _T + r".*?</a>, getting (.*?) in return", raw, re.S)
    if m:
        a, x, b, y = int(m.group(1)), m.group(2), int(m.group(3)), m.group(4)
        xp, xc = _side(x)
        yp, yc = _side(y)
        names = {int(p): _text(n) for p, n in re.findall(_P + r"(.*?)</a>", raw)}
        ev.setdefault("_trades", []).append({
            "date": d, "a": a, "b": b, "a_sent": xp, "a_cash": xc, "b_sent": yp, "b_cash": yc,
            "names": names, "text": _text(raw)})
        for pid in yp:   # moved B -> A
            add(pid, kind="trade", to=a, frm=b, gave=[names.get(p) for p in xp], gave_cash=xc,
                got=[names.get(p) for p in yp], got_cash=yc)
        for pid in xp:   # moved A -> B
            add(pid, kind="trade", to=b, frm=a, gave=[names.get(p) for p in yp], gave_cash=yc,
                got=[names.get(p) for p in xp], got_cash=xc)
        return

    mt = re.match(r"\s*" + _T, raw)
    if not mt:
        return
    team = int(mt.group(1))
    body = raw[mt.end():]
    body = body[body.find("</a>") + 4:] if "</a>" in body else body
    pm = re.search(_P, body)
    if not pm:
        return
    pid = pm.group(1)
    t = _text(body)
    bonus = re.search(r"signing bonus of " + _MONEY + r"|with a " + _MONEY + r" signing bonus", t)
    bonus = _money(bonus.group(1) or bonus.group(2)) if bonus else None

    if t.startswith(": Signed international amateur"):
        c = re.search(r" out of (.+?) to a ", t)
        add(pid, kind="intl", to=team, bonus=bonus, country=c.group(1) if c else None)
    elif t.startswith(": Signed free agent"):
        add(pid, kind="fa", to=team, bonus=bonus)
    elif re.match(r": Signed \S.* to a minor league contract with a signing bonus", t):
        add(pid, kind="draft_signing", to=team, bonus=bonus)
    elif "off waivers from" in t:
        f = re.search(r"off waivers from " + _T, body)
        add(pid, kind="waivers", to=team, frm=int(f.group(1)) if f else None)
    elif "Rule 5 Draft" in t and t.startswith(": Drafted"):
        f = re.search(r"from the " + _T, body)
        add(pid, kind="rule5", to=team, frm=int(f.group(1)) if f else None)


def _load(slug: str) -> dict:
    folder = _folder(slug)
    if folder is None:
        return {}
    files = sorted(folder.glob("league_200_all_transactions_*.html"))
    key = (len(files), max((f.stat().st_mtime for f in files), default=0))
    if _cache["key"] == key:
        return _cache["events"]
    ev: dict = {}
    seen = set()
    for f in files:
        try:
            raw = f.read_text(errors="ignore")
        except OSError:
            continue
        # walk dates and entries in document order
        pos, cur = 0, None
        tokens = sorted([(m.start(), "d", m) for m in _DATE_RE.finditer(raw)] +
                        [(m.start(), "e", m) for m in _ENTRY_RE.finditer(raw)], key=lambda x: x[0])
        for _, kind, m in tokens:
            if kind == "d":
                mo = _MONTHS.get(m.group(1))
                cur = date(int(m.group(3)), mo, int(m.group(2))) if mo else None
            elif cur:
                sig = (cur, m.group(1))
                if sig in seen:
                    continue
                seen.add(sig)
                _parse_entry(cur, m.group(1), ev)
    ev.setdefault("_trades", [])
    for lst in ev.values():
        lst.sort(key=lambda e: e["date"])
    _cache.update(key=key, events=ev)
    return ev


def trades(slug: str) -> list[dict]:
    """Every logged trade, newest first."""
    return list(reversed(_load(slug).get("_trades", [])))


def _ord(n: int) -> str:
    return f"{n}{'th' if 10 <= n % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


def _usd(n: int | None) -> str:
    return f"${n:,}" if n else ""


def _pkg(names, cash) -> str:
    parts = [n for n in names if n]
    if cash:
        parts.append(f"${cash:,} cash")
    return ", ".join(parts) or "nothing"


def describe(pid: int, org_id: int, draft: dict, slug: str, abbr: dict) -> dict | None:
    """draft: players-table row with draft_year/round/pick/overall/team_id."""
    if not org_id:
        return None
    events = _load(slug).get(int(pid), [])
    A = lambda t: abbr.get(t, str(t)) if t else "?"
    mon = lambda d: d.strftime("%b %Y")

    def draft_text(with_team: bool) -> str:
        s = f"{draft['draft_year']} draft, Rd {draft['draft_round']} Pick {draft['draft_pick']}"
        if draft.get("draft_overall_pick"):
            s += f" (#{draft['draft_overall_pick']} overall)"
        if with_team:
            s += f" by {A(draft['draft_team_id'])}"
        sig = next((e for e in events if e["kind"] == "draft_signing" and e["to"] == draft["draft_team_id"]), None)
        if sig and sig.get("bonus"):
            s += f", {_usd(sig['bonus'])} bonus"
        return s

    def sign_text(e, with_team: bool) -> str:
        what = "Intl amateur" if e["kind"] == "intl" else "Free agent"
        s = f"{what}, {mon(e['date'])}"
        if e.get("bonus"):
            s += f", {_usd(e['bonus'])}"
        elif e["kind"] == "intl":
            s += ", no bonus"
        if e["kind"] == "intl" and e.get("country"):
            s += f" ({e['country']})"
        if with_team:
            s += f" — signed by {A(e['to'])}"
        return s

    arrivals = [e for e in events if e.get("to") == org_id and e["kind"] != "draft_signing"]
    latest = arrivals[-1] if arrivals else None
    # Release + re-sign by the same club: report the first signing of that run.
    if latest is not None and latest["kind"] in ("fa", "intl"):
        moves = [e for e in events if e["kind"] != "draft_signing"]
        i = moves.index(latest)
        while i > 0 and moves[i - 1].get("to") == org_id and moves[i - 1]["kind"] in ("fa", "intl"):
            i -= 1
        latest = moves[i]
    drafted = bool(draft.get("draft_year")) and draft.get("draft_team_id")
    # A draft by the current org that is newer than any logged arrival wins.
    if drafted and draft["draft_team_id"] == org_id and (
            latest is None or latest["date"].year < draft["draft_year"]):
        latest = None
        main, tip = "Draft: " + draft_text(False), None
    elif latest is None:
        if drafted and draft["draft_team_id"] == org_id:
            main, tip = "Draft: " + draft_text(False), None
        elif drafted:
            return {"main": "Arrival not in logs (pre-1953)", "sub": "Orig: " + draft_text(True),
                    "tip": "Transaction logs are sparse before 1953."}
        else:
            return {"main": "Original roster (1951)", "sub": None,
                    "tip": "No signing or trade found in the transaction logs (dense from 1953)."}
    else:
        k = latest["kind"]
        tip = None
        if k == "trade":
            only_cash = not any(latest["gave"]) and latest["gave_cash"]
            if only_cash:
                main = f"Purchased from {A(latest['frm'])}, {mon(latest['date'])} ({_usd(latest['gave_cash'])})"
            else:
                main = f"Trade w/ {A(latest['frm'])}, {mon(latest['date'])}"
            tip = (f"{A(org_id)} received: {_pkg(latest['got'], latest['got_cash'])}\n"
                   f"{A(org_id)} sent: {_pkg(latest['gave'], latest['gave_cash'])}")
        elif k == "waivers":
            main = f"Waiver claim from {A(latest['frm'])}, {mon(latest['date'])}"
        elif k == "rule5":
            main = f"Rule 5 pick from {A(latest['frm'])}, {mon(latest['date'])}"
        else:
            main = sign_text(latest, False)

    # Origin line, when he started somewhere else / some other way.
    sub = None
    if latest is not None:
        if drafted:
            sub = "Orig: " + draft_text(True)
        else:
            first = next((e for e in events if e["kind"] in ("intl", "fa")), None)
            if first and first is not latest:
                sub = "Orig: " + sign_text(first, True)
            elif first is None and latest["kind"] not in ("intl", "fa"):
                sub = None
    return {"main": main, "sub": sub, "tip": tip}
