"""Trade Center: pennant race simulation, the deadline value of a win, and a
trade-partner finder for human-GM leagues.

Pennant race
    Leagues are recovered from who plays whom this season. Each club's
    true-talent win% is its Pythagorean record (current-season runs) regressed
    toward .500. The remaining schedule is exact for a balanced schedule: each
    pair plays games_per_season / (league size - 1) games, minus what they have
    already played. Every remaining game is simulated (log5), then a best-of-7
    World Series between the two pennant winners.

Win value
    A deal that adds X WAR per full season raises the team's per-game win
    probability by X / games_per_season for the remaining games (and in the
    World Series). Pennant and title odds are re-simulated with the same random
    numbers, so the differences are smooth, then priced with the user's
    postseason dollar values.

Partner finder
    Built for human GMs, so it uses only what can be measured: each club's
    buyer/seller status from the simulation, its weakest lineup/rotation/bullpen
    slots against the league, the upgrades it could supply at your weak slots,
    and the non-untouchable assets you have at its weak slots.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np

from web_league_context import get_db, get_cfg

N_SIMS = 20000
REGRESS_GAMES = 70          # pull Pythagorean win% toward .500 by this many games
PYTH_EXP = 1.83
WAR_STEPS = [-3, -2, -1, 0, 1, 2, 3, 4, 5]

LINEUP_SLOTS = ["C", "1B", "2B", "3B", "SS", "LF", "CF", "RF"]
POS_TO_SLOT = {2: "C", 3: "1B", 4: "2B", 5: "3B", 6: "SS", 7: "LF", 8: "CF", 9: "RF"}
SLOT_BUCKET = {"C": "C", "1B": "1B", "2B": "2B", "3B": "3B", "SS": "SS",
               "LF": "COF", "CF": "CF", "RF": "COF", "SP": "SP", "RP": "RP"}
ROTATION_DEPTH = 4          # the SP slot is the 4th-best starter
PEN_DEPTH = 5               # the RP slot is the 5th-best reliever


# ── settings ────────────────────────────────────────────────────────────────

def _settings_path(league_dir) -> Path:
    return Path(league_dir) / "config" / "trade_center.json"


def load_settings(league_dir) -> dict:
    try:
        d = json.loads(_settings_path(league_dir).read_text())
    except (OSError, ValueError):
        d = {}
    d.setdefault("untouchables", [])
    d.setdefault("pennant_value", None)
    d.setdefault("title_value", None)
    return d


def save_settings(league_dir, d: dict) -> None:
    p = _settings_path(league_dir)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(d))
    tmp.replace(p)


# ── finance (optional StatsPlus finances CSV in ~/Downloads) ─────────────────

def _finance_row(team_abbr: str):
    """Newest StatsPlus finances export with this team's row, or None."""
    import csv
    files = sorted(Path.home().joinpath("Downloads").glob("statsplus*.csv"),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    for f in files[:10]:
        try:
            rows = list(csv.DictReader(f.open(encoding="utf-8-sig")))
        except (OSError, UnicodeDecodeError):
            continue
        if not rows or "GateRevenue" not in rows[0]:
            continue
        for r in rows:
            if (r.get("Team") or "").strip().upper() == team_abbr.upper():
                return r, f.name
    return None, None


def _money(v):
    try:
        return float(str(v).replace("$", "").replace(",", ""))
    except (TypeError, ValueError):
        return None


def default_postseason_values(conn, team_id, year):
    """Pennant value ~ playoff gate: this club's gate revenue per home game x
    3.5 expected World Series home games. Title value defaults to twice that.
    Both are editable on the page; returns (pennant, title, note)."""
    abbr = (get_cfg().team_abbr_map or {}).get(team_id, "")
    row, fname = _finance_row(abbr)
    if row:
        gate = _money(row.get("GateRevenue"))
        home = conn.execute(
            "SELECT COUNT(*) FROM games WHERE home_team=? AND played=1 AND game_type=0 AND date>=?",
            (team_id, f"{year}-01-01")).fetchone()[0]
        if gate and home:
            per = gate / home
            pennant = round(per * 3.5)
            return pennant, pennant * 2, (f"From {fname}: gate ${gate:,.0f} over {home} home games "
                                          f"= ${per:,.0f}/game x 3.5 World Series home games")
    return 50_000, 100_000, "No StatsPlus finances export found in Downloads; placeholder values"


# ── league structure and strengths ──────────────────────────────────────────

def _season_games(conn, year):
    return conn.execute(
        "SELECT home_team, away_team, runs0, runs1 FROM games "
        "WHERE played=1 AND game_type=0 AND date>=? AND date<?",
        (f"{year}-01-01", f"{year + 1}-01-01")).fetchall()


def _leagues(games, teams):
    """Connected components of the 'played each other' graph."""
    adj = {t: set() for t in teams}
    for g in games:
        h, a = g[0], g[1]
        if h in adj and a in adj:
            adj[h].add(a)
            adj[a].add(h)
    seen, out = set(), []
    for t in sorted(teams):
        if t in seen:
            continue
        comp, stack = [], [t]
        while stack:
            x = stack.pop()
            if x in seen:
                continue
            seen.add(x)
            comp.append(x)
            stack.extend(adj[x] - seen)
        out.append(sorted(comp))
    return out


def _race_inputs(conn, year, gps):
    mlb = set(get_cfg().mlb_team_ids or [])
    stand = {r[0]: {"w": r[1], "l": r[2]} for r in conn.execute(
        "SELECT team_id, w, l FROM standings").fetchall() if not mlb or r[0] in mlb}
    teams = sorted(stand)
    games = _season_games(conn, year)
    rs = {t: 0 for t in teams}
    ra = {t: 0 for t in teams}
    played = {}
    for h, a, r_away, r_home in games:
        if h not in rs or a not in rs:
            continue
        rs[h] += r_home or 0; ra[h] += r_away or 0
        rs[a] += r_away or 0; ra[a] += r_home or 0
        key = (min(h, a), max(h, a))
        played[key] = played.get(key, 0) + 1
    strength = {}
    for t in teams:
        g = stand[t]["w"] + stand[t]["l"]
        if rs[t] + ra[t] > 0:
            p = rs[t] ** PYTH_EXP / (rs[t] ** PYTH_EXP + ra[t] ** PYTH_EXP)
        else:
            p = 0.5
        strength[t] = (p * g + 0.5 * REGRESS_GAMES) / (g + REGRESS_GAMES)
    leagues = _leagues(games, teams)
    remaining = []
    for lg in leagues:
        per_opp = gps / max(1, len(lg) - 1)
        for i, a in enumerate(lg):
            for b in lg[i + 1:]:
                n = int(round(per_opp)) - played.get((a, b), 0)
                if n > 0:
                    remaining.append((a, b, n))
    return stand, strength, leagues, remaining, rs, ra


def _log5(pa, pb):
    d = pa + pb - 2 * pa * pb
    return 0.5 if d <= 0 else (pa - pa * pb) / d


def _series_win_prob(p, games=7):
    """P(win a best-of-`games` series) with per-game win prob p."""
    need = games // 2 + 1
    return sum(math.comb(need - 1 + k, k) * p ** need * (1 - p) ** k for k in range(need))


def simulate(conn, year, gps, my_team, war_steps=WAR_STEPS, seed=7):
    """Monte-Carlo the rest of the season + World Series for each WAR step
    added to `my_team` (common random numbers across steps)."""
    stand, strength, leagues, remaining, rs, ra = _race_inputs(conn, year, gps)
    teams = sorted(stand)
    idx = {t: i for i, t in enumerate(teams)}
    rng = np.random.default_rng(seed)
    games = [(a, b) for a, b, n in remaining for _ in range(n)]
    u = rng.random((N_SIMS, len(games))) if games else np.zeros((N_SIMS, 0))
    tie = rng.random((N_SIMS, len(teams))) * 1e-3
    ws_u = rng.random(N_SIMS)
    base_w = np.array([stand[t]["w"] for t in teams], dtype=float)
    my_league = next((lg for lg in leagues if my_team in lg), [])
    my_games_left = sum(n for a, b, n in remaining if my_team in (a, b))

    results = {}
    for step in war_steps:
        s = dict(strength)
        if my_team in s:
            s[my_team] = min(0.95, max(0.05, s[my_team] + step / gps))
        wins = np.tile(base_w, (N_SIMS, 1))
        if games:
            pa = np.array([_log5(s[a], s[b]) for a, b in games])
            a_win = u < pa
            ai = np.array([idx[a] for a, _ in games])
            bi = np.array([idx[b] for _, b in games])
            np.add.at(wins.T, ai, a_win.T)
            np.add.at(wins.T, bi, (~a_win).T)
        final = wins + tie
        pennant = {}
        winners = []
        for lg in leagues:
            cols = [idx[t] for t in lg]
            w_idx = np.array(cols)[np.argmax(final[:, cols], axis=1)]
            winners.append(w_idx)
            for t in lg:
                pennant[t] = float(np.mean(w_idx == idx[t]))
        title = {t: 0.0 for t in teams}
        if len(winners) == 2:
            l1, l2 = winners
            p1 = np.array([_series_win_prob(_log5(s[teams[i]], s[teams[j]]))
                           for i, j in zip(l1, l2)])
            w1 = ws_u < p1
            champ = np.where(w1, l1, l2)
            for t in teams:
                title[t] = float(np.mean(champ == idx[t]))
        exp_w = {t: float(wins[:, idx[t]].mean()) for t in teams}
        results[step] = {"pennant": pennant, "title": title, "exp_w": exp_w}
    return {"results": results, "stand": stand, "strength": strength, "leagues": leagues,
            "my_league": my_league, "my_games_left": my_games_left, "rs": rs, "ra": ra}


# ── rosters, slots, player value ────────────────────────────────────────────

def _mlb_players(conn):
    return [dict(r) for r in conn.execute("""
        SELECT p.player_id, p.name, p.age, p.pos, p.role, p.team_id,
               COALESCE(NULLIF(p.parent_team_id,0), p.team_id) AS org,
               p.injury_is_injured, p.injury_left,
               r.composite_score AS comp, ps.surplus
        FROM players p
        JOIN latest_ratings r ON r.player_id = p.player_id
        LEFT JOIN player_surplus ps ON ps.player_id = p.player_id
             AND ps.eval_date = (SELECT MAX(eval_date) FROM player_surplus)
        WHERE p.level = '1' AND r.composite_score IS NOT NULL
    """).fetchall()]


def _slot_of(p):
    if p["role"] == 11:
        return "SP"
    if p["role"] in (12, 13):
        return "RP"
    return POS_TO_SLOT.get(p["pos"])


def _war_rate(comp, slot, weights):
    from statsplusplus.evaluation.war import peak_war_from_score
    try:
        return float(peak_war_from_score(comp, SLOT_BUCKET[slot], weights))
    except Exception:
        return 0.0


def _team_slots(players, weights):
    """{team: {slot: {"comp", "war", "pid", "name"}}} — starter quality per slot
    (best healthy player at a listed position; 4th SP; 5th RP)."""
    by = {}
    for p in players:
        if p["injury_is_injured"] and (p["injury_left"] or 0) > 20:
            continue                       # long-term injured can't fill a slot now
        slot = _slot_of(p)
        if slot:
            by.setdefault(p["team_id"], {}).setdefault(slot, []).append(p)
    # Starters beyond the rotation pitch out of the bullpen (many clubs list
    # relievers as starters), so the pen is RP-role arms plus SP overflow.
    for t, slots in by.items():
        sps = sorted(slots.get("SP", []), key=lambda x: -x["comp"])
        if len(sps) > ROTATION_DEPTH:
            slots["SP"] = sps[:ROTATION_DEPTH]
            slots.setdefault("RP", []).extend(sps[ROTATION_DEPTH:])
    out = {}
    for t, slots in by.items():
        out[t] = {}
        for slot in LINEUP_SLOTS + ["SP", "RP"]:
            lst = sorted(slots.get(slot, []), key=lambda x: -x["comp"])
            depth = ROTATION_DEPTH if slot == "SP" else PEN_DEPTH if slot == "RP" else 1
            if len(lst) >= depth:
                q = lst[depth - 1]
                out[t][slot] = {"comp": q["comp"], "war": _war_rate(q["comp"], slot, weights),
                                "pid": q["player_id"], "name": q["name"]}
            else:
                out[t][slot] = {"comp": 20, "war": _war_rate(20, slot, weights), "pid": None, "name": "—"}
    return out


# ── main payload ────────────────────────────────────────────────────────────

def _status(odds):
    if odds is None:
        return "—", ""
    if odds >= 0.30:
        return "Buyer", "buyer"
    if odds >= 0.02:
        return "Bubble", "bubble"
    return "Seller", "seller"


_build_cache: dict = {}


def build(team_id, league_dir):
    import os
    st = Path(league_dir) / "config" / "state.json"
    key = (team_id, str(league_dir), os.path.getmtime(st) if st.exists() else 0,
           os.path.getmtime(_settings_path(league_dir)) if _settings_path(league_dir).exists() else 0,
           os.path.getmtime(Path(league_dir) / "league.db"))
    if key not in _build_cache:
        _build_cache.clear()
        _build_cache[key] = _build(team_id, league_dir)
    return _build_cache[key]


def _build(team_id, league_dir):
    from statsplusplus.config.league_config import games_per_season
    from statsplusplus.evaluation.constants import load_model_weights
    conn = get_db()
    cfg = get_cfg()
    gps = games_per_season(league_dir)
    year = conn.execute("SELECT MAX(year) FROM mlb_batting_stats").fetchone()[0]
    weights = load_model_weights(Path(league_dir))
    settings = load_settings(league_dir)
    d_pen, d_title, value_note = default_postseason_values(conn, team_id, year)
    pennant_value = settings["pennant_value"] if settings["pennant_value"] is not None else d_pen
    title_value = settings["title_value"] if settings["title_value"] is not None else d_title

    sim = simulate(conn, year, gps, team_id)
    base = sim["results"][0]
    names = cfg.team_names_map or {}
    abbr = cfg.team_abbr_map or {}

    def dollars(res):
        return res["pennant"].get(team_id, 0) * pennant_value + res["title"].get(team_id, 0) * title_value

    curve = [{"war": s, "pennant": sim["results"][s]["pennant"].get(team_id, 0),
              "title": sim["results"][s]["title"].get(team_id, 0),
              "dollars": dollars(sim["results"][s]) - dollars(base)} for s in WAR_STEPS]
    up, down = sim["results"][1], sim["results"][-1]
    marginal = {
        "pennant": (up["pennant"].get(team_id, 0) - down["pennant"].get(team_id, 0)) / 2,
        "title": (up["title"].get(team_id, 0) - down["title"].get(team_id, 0)) / 2,
        "dollars": (dollars(up) - dollars(down)) / 2,
    }

    race = []
    for lg in sim["leagues"]:
        rows = []
        for t in lg:
            st = sim["stand"][t]
            rows.append({"tid": t, "name": names.get(t, str(t)), "abbr": abbr.get(t, ""),
                         "w": st["w"], "l": st["l"], "rs": sim["rs"][t], "ra": sim["ra"][t],
                         "true_pct": sim["strength"][t], "exp_w": base["exp_w"][t],
                         "pennant": base["pennant"][t], "title": base["title"][t],
                         "mine": t == team_id})
        rows.sort(key=lambda r: (-r["w"] + r["l"], -r["pennant"]))
        race.append({"mine": team_id in lg, "rows": rows})
    race.sort(key=lambda g: not g["mine"])
    magic = conn.execute("SELECT magic_number FROM standings WHERE team_id=?", (team_id,)).fetchone()
    magic = magic[0] if magic and magic[0] is not None and magic[0] < 1000 else None

    partners = _partner_finder(conn, team_id, weights, base, marginal, settings, names, abbr)
    return {"gps": gps, "year": year, "games_left": sim["my_games_left"], "magic": magic,
            "curve": curve, "marginal": marginal, "race": race,
            "pennant_value": pennant_value, "title_value": title_value, "value_note": value_note,
            "now": {"pennant": base["pennant"].get(team_id, 0), "title": base["title"].get(team_id, 0)},
            **partners}


def _partner_finder(conn, team_id, weights, base, marginal, settings, names, abbr):
    players = _mlb_players(conn)
    slots = _team_slots(players, weights)
    mlb = set(get_cfg().mlb_team_ids or [])
    teams = sorted(t for t in slots if not mlb or t in mlb)
    league_avg = {s: sum(slots[t][s]["comp"] for t in teams) / len(teams) for s in LINEUP_SLOTS + ["SP", "RP"]}
    mine = slots.get(team_id, {})
    my_needs = sorted(LINEUP_SLOTS + ["SP", "RP"],
                      key=lambda s: mine.get(s, {"comp": 20})["comp"] - league_avg[s])
    my_needs = [s for s in my_needs if mine.get(s, {"comp": 20})["comp"] < league_avg[s] + 3][:5]
    untouch = set(settings.get("untouchables") or [])

    # My tradeable assets: org players (MLB + farm) not untouchable, with surplus.
    assets = [dict(r) for r in conn.execute("""
        SELECT p.player_id, p.name, p.age, p.pos, p.role, p.level, r.composite_score AS comp,
               COALESCE(ps.surplus, pf.prospect_surplus) AS surplus, pf.fv, pf.bucket
        FROM players p
        JOIN latest_ratings r ON r.player_id = p.player_id
        LEFT JOIN player_surplus ps ON ps.player_id=p.player_id AND ps.eval_date=(SELECT MAX(eval_date) FROM player_surplus)
        LEFT JOIN prospect_fv pf ON pf.player_id=p.player_id AND pf.eval_date=(SELECT MAX(eval_date) FROM prospect_fv)
        WHERE (p.team_id=? OR p.parent_team_id=?) AND COALESCE(p.retired,0)=0
    """, (team_id, team_id)).fetchall()]
    my_starter_pids = {v["pid"] for v in mine.values() if v.get("pid")}
    for a in assets:
        a["slot"] = _slot_of(a) if str(a["level"]) == "1" else (
            {"COF": "LF", "SP": "SP", "RP": "RP"}.get(a["bucket"], a["bucket"]))
        a["untouchable"] = a["player_id"] in untouch
        a["starter"] = a["player_id"] in my_starter_pids
    offer_pool = [a for a in assets if not a["untouchable"] and (a["surplus"] or 0) > 0]

    out_teams = []
    for t in teams:
        if t == team_id:
            continue
        status, status_cls = _status(base["pennant"].get(t))
        their = slots[t]
        their_needs = [s for s in LINEUP_SLOTS + ["SP", "RP"] if their[s]["comp"] < league_avg[s] - 2]
        # Upgrades they could give me at my weak slots
        targets = []
        for p in players:
            if p["team_id"] != t or (p["injury_is_injured"] and (p["injury_left"] or 0) > 20):
                continue
            slot = _slot_of(p)
            if slot not in my_needs:
                continue
            gain = _war_rate(p["comp"], slot, weights) - mine.get(slot, {"war": 0})["war"]
            if gain < 0.5:
                continue
            targets.append({"pid": p["player_id"], "name": p["name"], "slot": slot, "comp": p["comp"],
                            "age": p["age"], "gain": round(gain, 1),
                            "season_dollars": gain * marginal["dollars"],
                            "surplus": p["surplus"]})
        targets.sort(key=lambda x: -x["gain"])
        # My assets that fill their weak slots (prefer non-starters so this year isn't hurt)
        fits = [a for a in offer_pool if a["slot"] in their_needs]
        fits.sort(key=lambda a: (a["starter"], -(a["surplus"] or 0)))
        fit_score = (sum(x["gain"] for x in targets[:3])
                     * {"seller": 1.5, "bubble": 1.0, "buyer": 0.5}.get(status_cls, 1.0)
                     + 0.3 * min(3, len(fits)))
        out_teams.append({"tid": t, "name": names.get(t, str(t)), "abbr": abbr.get(t, ""),
                          "status": status, "status_cls": status_cls,
                          "pennant": base["pennant"].get(t, 0), "needs": their_needs,
                          "targets": targets[:4], "fits": [
                              {"pid": a["player_id"], "name": a["name"], "slot": a["slot"],
                               "comp": a["comp"], "fv": a["fv"], "surplus": a["surplus"],
                               "level": a["level"], "starter": a["starter"]} for a in fits[:4]],
                          "fit_score": round(fit_score, 2)})
    out_teams.sort(key=lambda x: -x["fit_score"])
    untouch_list = sorted(assets, key=lambda a: -(a["surplus"] or 0))[:40]
    return {"my_needs": [{"slot": s, "comp": mine.get(s, {}).get("comp"), "avg": round(league_avg[s], 1),
                          "who": mine.get(s, {}).get("name")} for s in my_needs],
            "partners": out_teams,
            "untouch_list": [{"pid": a["player_id"], "name": a["name"], "slot": a["slot"],
                              "level": a["level"], "comp": a["comp"], "surplus": a["surplus"],
                              "untouchable": a["untouchable"]} for a in untouch_list]}


def what_if(team_id, league_dir, incoming, outgoing):
    """WAR change from a proposed deal and its pennant / title / dollar effect."""
    from statsplusplus.config.league_config import games_per_season
    from statsplusplus.evaluation.constants import load_model_weights
    conn = get_db()
    gps = games_per_season(league_dir)
    year = conn.execute("SELECT MAX(year) FROM mlb_batting_stats").fetchone()[0]
    weights = load_model_weights(Path(league_dir))
    players = {p["player_id"]: p for p in _mlb_players(conn)}
    # Compare the team's slot quality before and after the roster change, so an
    # incoming CF who replaces an outgoing CF is counted once, not twice.
    before = _team_slots(list(players.values()), weights).get(team_id, {})
    moved = []
    for p in players.values():
        if p["player_id"] in outgoing:
            continue
        q = dict(p)
        if p["player_id"] in incoming:
            q["team_id"] = team_id
        moved.append(q)
    after = _team_slots(moved, weights).get(team_id, {})
    lines, delta = [], 0.0
    for slot in LINEUP_SLOTS + ["SP", "RP"]:
        b_, a_ = before.get(slot), after.get(slot)
        if not b_ or not a_ or b_.get("pid") == a_.get("pid"):
            continue
        d = a_["war"] - b_["war"]
        delta += d
        lines.append({"name": f"{b_['name']} → {a_['name']}", "slot": slot,
                      "dir": "in" if d >= 0 else "out", "war": round(d, 2)})
    settings = load_settings(league_dir)
    d_pen, d_title, _ = default_postseason_values(conn, team_id, year)
    pv = settings["pennant_value"] if settings["pennant_value"] is not None else d_pen
    tv = settings["title_value"] if settings["title_value"] is not None else d_title
    sim = simulate(conn, year, gps, team_id, war_steps=[0, round(delta, 2)])
    b, a = sim["results"][0], sim["results"][round(delta, 2)]
    surplus_in = sum((players[p]["surplus"] or 0) for p in incoming if p in players)
    surplus_out = sum((players[p]["surplus"] or 0) for p in outgoing if p in players)
    return {"delta_war": round(delta, 2), "lines": lines,
            "pennant_before": b["pennant"].get(team_id, 0), "pennant_after": a["pennant"].get(team_id, 0),
            "title_before": b["title"].get(team_id, 0), "title_after": a["title"].get(team_id, 0),
            "dollars": (a["pennant"].get(team_id, 0) - b["pennant"].get(team_id, 0)) * pv
                       + (a["title"].get(team_id, 0) - b["title"].get(team_id, 0)) * tv,
            "surplus_in": surplus_in, "surplus_out": surplus_out}
