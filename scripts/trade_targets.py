#!/usr/bin/env python3
"""
trade_targets.py — Find trade targets by position with contract status and seller classification.

Usage:
  python3 scripts/trade_targets.py --bucket COF          # All OF trade targets
  python3 scripts/trade_targets.py --bucket SP --min-ovr 58
  python3 scripts/trade_targets.py --bucket 3B --sellers-only
  python3 scripts/trade_targets.py --bucket COF --include-controlled  # Include multi-year players
  python3 scripts/trade_targets.py --bucket SS --max-salary 10        # Max pro-rated salary ($M)

Contract status labels:
  RENTAL     — final contract year, no options (walks after season)
  CONTROLLED — multiple years remaining (higher prospect cost)
  OPTION     — final guaranteed year but team/player option exists

Seller classification: teams > 8 pythagorean GB from the last playoff spot.
"""

import argparse, os, sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "scripts"))

from statsplusplus.config.league_context import get_league_dir, get_active_league_slug
from statsplusplus.config.league_config import LeagueConfig
from statsplusplus.data.db import get_connection
from standings import _standings_from_db

league_dir = get_league_dir(get_active_league_slug())
_cfg = LeagueConfig(base_dir=league_dir)

def _get_conn():
    return get_connection(league_dir)


# ---------------------------------------------------------------------------
# Seller classification
# ---------------------------------------------------------------------------

PLAYOFF_SPOTS = 6   # default; overridden by league config if available

def _playoff_spots():
    """Number of playoff spots per league (AL/NL or equivalent)."""
    # Check league_settings for explicit playoff config
    playoff = _cfg.settings.get("playoff_spots_per_league")
    if playoff:
        return int(playoff)
    # Derive from number of teams: assume top ~33% make playoffs
    divs = _cfg.settings.get("divisions", {})
    if not divs:
        return PLAYOFF_SPOTS
    # Count teams per sub-league (AL/NL or first word of division name)
    league_counts = {}
    for div, teams in divs.items():
        key = div.split()[0] if div else "?"
        league_counts[key] = league_counts.get(key, 0) + len(teams)
    if not league_counts:
        return PLAYOFF_SPOTS
    avg_per_league = sum(league_counts.values()) / len(league_counts)
    return max(2, round(avg_per_league * 0.4))  # ~40% of each league makes playoffs

def _classify_sellers(year):
    """Return set of team_ids classified as sellers (> 8 GB from last playoff spot).

    Prefers real standings from the `standings` table (sourced from /lgdata).
    Falls back to pythagorean standings when real standings aren't available.
    """
    conn = _get_conn()

    # Try real standings first
    real = conn.execute(
        "SELECT team_id, w, l, gb FROM standings"
    ).fetchall()

    if real and len(real) >= 10:
        # Use real GB directly — much simpler than pythagorean approach
        spots = _playoff_spots()

        # Group by sub-league for proper GB comparison
        sub_leagues = {}
        for div, teams in _cfg.settings.get("divisions", {}).items():
            key = div.split()[0] if div else "ALL"
            for tid in teams:
                sub_leagues[tid] = key

        # Build team records
        records = {r["team_id"]: {"w": r["w"], "l": r["l"], "gb": r["gb"]} for r in real}

        if not sub_leagues:
            groups = {"ALL": list(records.keys())}
        else:
            groups = {}
            for tid, key in sub_leagues.items():
                groups.setdefault(key, []).append(tid)

        sellers = set()
        for league_key, league_tids in groups.items():
            league_ids = set(league_tids)
            league_rows = sorted(
                [(tid, records[tid]) for tid in league_ids if tid in records],
                key=lambda x: x[1]["w"], reverse=True
            )
            if len(league_rows) <= spots:
                continue
            cutoff_w = league_rows[spots - 1][1]["w"]
            for tid, rec in league_rows[spots:]:
                if (cutoff_w - rec["w"]) > 8:
                    sellers.add(tid)

        conn.close()
        return sellers

    conn.close()

    # Fallback: pythagorean standings
    rows = _standings_from_db(year)
    if not rows:
        return set()

    spots = _playoff_spots()

    sub_leagues = {}
    for div, teams in _cfg.settings.get("divisions", {}).items():
        key = div.split()[0] if div else "ALL"
        for tid in teams:
            sub_leagues[tid] = key

    if not sub_leagues:
        groups = {"ALL": [r["tid"] for r in rows]}
    else:
        groups = {}
        for tid, key in sub_leagues.items():
            groups.setdefault(key, []).append(tid)

    sellers = set()
    for league_key, league_tids in groups.items():
        league_ids = set(league_tids)
        league_rows = sorted(
            [r for r in rows if r["tid"] in league_ids],
            key=lambda x: x["pct"], reverse=True
        )
        if len(league_rows) <= spots:
            continue
        cutoff_w = league_rows[spots - 1]["w"]
        for r in league_rows[spots:]:
            if (cutoff_w - r["w"]) > 8:
                sellers.add(r["tid"])

    return sellers


# ---------------------------------------------------------------------------
# Contract status
# ---------------------------------------------------------------------------

def _contract_status(years, current_year, team_opt, player_opt, vesting_opt=False):
    yrs_left = years - current_year
    if yrs_left <= 1:
        if vesting_opt:
            return "VESTING"
        if team_opt or player_opt:
            return "OPTION"
        return "RENTAL"
    return "CONTROLLED"


# ---------------------------------------------------------------------------
# Main query
# ---------------------------------------------------------------------------

def find_targets(bucket, min_ovr=50, sellers_only=False, include_controlled=False,
                 max_salary_m=None, year=None, vs_hand=None, exclude_injured=False,
                 on_block_only=False):
    year = year or _cfg.year
    conn = _get_conn()

    sellers = _classify_sellers(year)

    # Load trade block player IDs
    trade_block_ids = set()
    try:
        trade_block_ids = {r[0] for r in conn.execute("SELECT player_id FROM trade_block").fetchall()}
    except Exception:
        pass  # Table may not exist in older DBs

    sellers = _classify_sellers(year)

    eval_date = conn.execute("SELECT MAX(eval_date) FROM player_surplus").fetchone()[0]

    # Map bucket to player positions
    pos_map = {
        "C": [2], "1B": [3], "2B": [4], "3B": [5], "SS": [6],
        "CF": [8], "COF": [7, 8, 9], "LF": [7], "RF": [9],
        "DH": [10], "SP": None, "RP": None,
    }
    positions = pos_map.get(bucket)
    is_pitcher = bucket in ("SP", "RP")

    # Build position filter
    if is_pitcher:
        role_val = 11 if bucket == "SP" else 12
        pos_filter = f"AND p.role = {role_val}"
    elif positions:
        pos_filter = f"AND p.pos IN ({','.join(str(x) for x in positions)})"
    else:
        pos_filter = ""

    rows = conn.execute(f"""
        SELECT p.player_id, p.name, p.age, p.team_id, p.pos, p.role,
               p.injury_is_injured, p.injury_left, p.is_on_dl, p.is_on_dl60,
               p.designated_for_assignment, p.is_on_waivers,
               r.ovr, r.pot,
               r.cntct, r.pow, r.eye, r.speed, r.cf,
               r.cntct_r, r.pow_r, r.eye_r,
               r.cntct_l, r.pow_l, r.eye_l,
               r.stf, r.mov, r.ctrl, r.vel,
               b.avg, b.obp, b.slg, b.hr, b.war, b.pa,
               pi.era, pi.ip, pi.war as pwar, pi.k, pi.bb,
               c.salary_0, c.years, c.current_year,
               c.last_year_team_option, c.last_year_player_option,
               c.last_year_vesting_option, c.last_year_option_buyout,
               s.surplus,
               ce.salary_0 as ext_salary, ce.years as ext_years
        FROM players p
        JOIN latest_ratings r ON p.player_id = r.player_id
        LEFT JOIN contracts c ON p.player_id = c.player_id
        LEFT JOIN contract_extensions ce ON p.player_id = ce.player_id
        LEFT JOIN player_surplus s ON s.player_id = p.player_id AND s.eval_date = ?
        LEFT JOIN mlb_batting_stats b ON p.player_id = b.player_id
            AND b.year = ? AND b.split_id = 1
        LEFT JOIN mlb_pitching_stats pi ON p.player_id = pi.player_id
            AND pi.year = ? AND pi.split_id = 1
        WHERE p.level = '1'
          AND r.ovr >= ?
          AND r.league_id > 0
          AND c.player_id IS NOT NULL
          AND c.salary_0 > ?
          {pos_filter}
        ORDER BY r.ovr DESC
    """, (eval_date, year, year, min_ovr, _cfg.minimum_salary)).fetchall()

    # Pull split stats if vs_hand requested (split_id 2=vsLHP, 3=vsRHP)
    split_stats = {}
    if vs_hand and not is_pitcher:
        split_id = 3 if vs_hand == 'R' else 2
        split_rows = conn.execute("""
            SELECT player_id, avg, obp, slg, hr, pa
            FROM mlb_batting_stats WHERE year=? AND split_id=?
        """, (year, split_id)).fetchall()
        split_stats = {r["player_id"]: r for r in split_rows}

    from datetime import datetime
    try:
        gd = datetime.strptime(_cfg.game_date, "%Y-%m-%d")
        season_start = datetime(gd.year, 4, 1)
        season_end = datetime(gd.year, 10, 1)
        elapsed = (gd - season_start).days / (season_end - season_start).days
        games_remaining = max(1, round(162 * (1 - elapsed)))
    except Exception:
        games_remaining = 53  # fallback
    conn.close()

    results = []
    seen = set()
    for r in rows:
        pid = r["player_id"]
        if pid in seen:
            continue
        seen.add(pid)

        # Skip DFA'd players — not tradeable
        if r["designated_for_assignment"]:
            continue

        if exclude_injured and r["injury_is_injured"]:
            continue

        # Skip my team
        if r["team_id"] == _cfg.my_team_id:
            continue

        status = _contract_status(
            r["years"] or 1, r["current_year"] or 0,
            r["last_year_team_option"], r["last_year_player_option"],
            vesting_opt=r["last_year_vesting_option"],
        )
        ext_salary_m = (r["ext_salary"] or 0) / 1e6
        # A "rental" with a signed extension is actually a commitment
        if status == "RENTAL" and ext_salary_m > 0:
            status = "RENTAL+EXT"

        if not include_controlled and status == "CONTROLLED":
            continue

        is_seller = r["team_id"] in sellers
        if sellers_only and not is_seller:
            continue

        sal = (r["salary_0"] or 0) / 1e6
        # Detect arb-eligible: salary above minimum but service time < 6 years
        # These are NOT true rentals — acquiring team doesn't control them post-season
        if status == "RENTAL" and sal > (_cfg.minimum_salary / 1e6):
            from statsplusplus.evaluation.arb import estimate_service_time as _est_svc
            conn2 = _get_conn()
            svc = _est_svc(conn2, pid)
            conn2.close()
            if svc is not None and svc < 6.0:
                status = "ARB"
        prorated = sal * (games_remaining / 162)
        if max_salary_m is not None and prorated > max_salary_m:
            continue

        surplus = (r["surplus"] or 0) / 1e6

        entry = {
            "pid": pid,
            "name": r["name"],
            "age": r["age"],
            "team_id": r["team_id"],
            "team": _cfg.team_abbr(r["team_id"]),
            "ovr": r["ovr"],
            "pot": r["pot"],
            "status": status,
            "seller": is_seller,
            "salary_m": sal,
            "prorated_m": round(prorated, 1),
            "surplus_m": round(surplus, 1),
            "ext_salary_m": round(ext_salary_m, 1),
            "is_pitcher": is_pitcher,
        }

        if is_pitcher:
            entry.update({
                "era": r["era"],
                "ip": round(r["ip"] or 0, 0),
                "war": r["pwar"],
                "vel": r["vel"],
                "stf": r["stf"], "mov": r["mov"], "ctrl": r["ctrl"],
            })
        else:
            split = split_stats.get(pid, {})
            pow_split = r["pow_r"] if vs_hand == 'R' else (r["pow_l"] if vs_hand == 'L' else None)
            entry.update({
                "avg": r["avg"], "obp": r["obp"], "slg": r["slg"],
                "hr": r["hr"], "war": r["war"], "pa": r["pa"],
                "hit": r["cntct"], "pow": r["pow"], "eye": r["eye"],
                "spd": r["speed"], "cf": r["cf"] or 0,
                "pow_r": r["pow_r"] or 0, "hit_r": r["cntct_r"] or 0, "eye_r": r["eye_r"] or 0,
                "pow_l": r["pow_l"] or 0, "hit_l": r["cntct_l"] or 0, "eye_l": r["eye_l"] or 0,
                "split_avg": split["avg"] if split else None,
                "split_obp": split["obp"] if split else None,
                "split_slg": split["slg"] if split else None,
                "split_hr": split["hr"] if split else None,
                "vs_hand": vs_hand,
                "_sort_key": pow_split or r["pow"] or 0,
            })

        # Injury flag
        if r["injury_is_injured"]:
            days = r["injury_left"] or 0
            entry["injury"] = f"DL:{days}d" if days else "DL"
            if r["is_on_dl60"]:
                entry["injury"] = f"60-DL:{days}d"
        else:
            entry["injury"] = None

        if r["is_on_waivers"]:
            entry["on_waivers"] = True
        else:
            entry["on_waivers"] = False

        # Trade block flag
        pid = r["player_id"]
        entry["on_block"] = pid in trade_block_ids

        # Filter: --on-block shows only trade block players
        if on_block_only and not entry["on_block"]:
            continue

        results.append(entry)

    sort_key = (lambda x: (0 if x["seller"] else 1, -x.get("_sort_key", x["ovr"]))) \
               if vs_hand else (lambda x: (0 if x["seller"] else 1, -x["ovr"]))
    results.sort(key=sort_key)
    return results


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def _fmt_line(r):
    status_marker = {
        "RENTAL": "🔴",
        "RENTAL+EXT": "🟠",
        "ARB": "🟣",
        "OPTION": "🟡",
        "CONTROLLED": "🔵",
    }.get(r["status"], " ")
    seller_marker = "SELL" if r["seller"] else "    "

    if r["is_pitcher"]:
        era = f"{r['era']:.2f}" if r["era"] else "-.--"
        ip = int(r["ip"] or 0)
        war = f"{r['war']:.1f}" if r["war"] else "-.-"
        ext_note = f" +EXT${r['ext_salary_m']:.1f}M/yr" if r.get("ext_salary_m", 0) > 0 else ""
        inj = f" 🏥{r['injury']}" if r.get('injury') else ""
        blk = " 📋" if r.get("on_block") else ""
        return (
            f"{status_marker} {seller_marker} {r['name']:<22} {r['age']:>2} "
            f"Ovr:{r['ovr']:>2}/{r['pot']:>2} {r['team']:<5} "
            f"${r['prorated_m']:.1f}M(pro) ${r['salary_m']:.1f}M(full){ext_note} "
            f"Surp:${r['surplus_m']:+.1f}M | "
            f"ERA:{era} {ip}IP WAR:{war}{inj}{blk}"
        )
    else:
        avg = f".{int((r['avg'] or 0)*1000):03}" if r["avg"] else "---"
        obp = f".{int((r['obp'] or 0)*1000):03}" if r["obp"] else "---"
        slg = f".{int((r['slg'] or 0)*1000):03}" if r["slg"] else "---"
        war = f"{r['war']:.1f}" if r["war"] else "-.-"
        hr = r["hr"] or 0
        cf = f" CF:{r['cf']:>2}" if r["cf"] else ""

        # Split line — show ratings always when vs_hand set, stats if available
        vh = r.get("vs_hand")
        if vh:
            pow_k = "pow_r" if vh == "R" else "pow_l"
            hit_k = "hit_r" if vh == "R" else "hit_l"
            eye_k = "eye_r" if vh == "R" else "eye_l"
            ratings_str = f"[Hit:{r[hit_k]} Pow:{r[pow_k]} Eye:{r[eye_k]}]"
            if r.get("split_avg") is not None:
                s_avg = f".{int(r['split_avg']*1000):03}"
                s_obp = f".{int(r['split_obp']*1000):03}"
                s_slg = f".{int(r['split_slg']*1000):03}"
                s_hr = r["split_hr"] or 0
                split_str = f" | vs{'R' if vh=='R' else 'L'}HP: {s_avg}/{s_obp}/{s_slg} {s_hr}HR {ratings_str}"
            else:
                split_str = f" | vs{'R' if vh=='R' else 'L'}HP ratings: {ratings_str}"
        else:
            split_str = ""

        ext_note = f" +EXT${r['ext_salary_m']:.1f}M/yr" if r.get("ext_salary_m", 0) > 0 else ""
        inj = f" 🏥{r['injury']}" if r.get('injury') else ""
        blk = " 📋" if r.get("on_block") else ""
        return (
            f"{status_marker} {seller_marker} {r['name']:<22} {r['age']:>2} "
            f"Ovr:{r['ovr']:>2}/{r['pot']:>2} {r['team']:<5} "
            f"${r['prorated_m']:.1f}M(pro) ${r['salary_m']:.1f}M(full){ext_note} "
            f"Surp:${r['surplus_m']:+.1f}M | "
            f"{avg}/{obp}/{slg} {hr}HR WAR:{war}{cf}{split_str}{inj}{blk}"
        )


def print_targets(results, bucket):
    rentals = [r for r in results if r["status"] == "RENTAL"]
    options = [r for r in results if r["status"] == "OPTION"]
    controlled = [r for r in results if r["status"] == "CONTROLLED"]

    print(f"\nTrade Targets — {bucket}  ({len(results)} players)\n")
    print("Legend: 🔴 RENTAL  🟠 RENTAL+EXT  🟣 ARB-ELIGIBLE  🟡 OPTION  🔵 CONTROLLED  |  SELL = seller team")
    print("-" * 110)

    for label, group in [("RENTALS (walk-year, true FA)", [r for r in results if r["status"] == "RENTAL"]),
                          ("RENTALS w/ SIGNED EXTENSION", [r for r in results if r["status"] == "RENTAL+EXT"]),
                          ("ARB-ELIGIBLE (not a rental — high prospect cost)", [r for r in results if r["status"] == "ARB"]),
                          ("OPTIONS", [r for r in results if r["status"] == "OPTION"]),
                          ("CONTROLLED", [r for r in results if r["status"] == "CONTROLLED"])]:
        if not group:
            continue
        print(f"\n── {label} ──")
        for r in group:
            print(_fmt_line(r))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Find trade targets by position")
    parser.add_argument("--bucket", required=True,
                        help="Position bucket: C 1B 2B 3B SS CF COF LF RF DH SP RP")
    parser.add_argument("--min-ovr", type=int, default=50,
                        help="Minimum OVR (default: 50)")
    parser.add_argument("--sellers-only", action="store_true",
                        help="Only show players on selling teams")
    parser.add_argument("--include-controlled", action="store_true",
                        help="Include multi-year controlled players (default: rentals/options only)")
    parser.add_argument("--max-salary", type=float, default=None,
                        help="Max pro-rated salary cost for remainder of season ($M). "
                             "E.g. --max-salary 4 means you can absorb up to $4M this year.")
    parser.add_argument("--vs-hand", choices=["R", "L"], default=None,
                        help="Show and sort by split ratings/stats vs RHP or LHP")
    parser.add_argument("--exclude-injured", action="store_true",
                        help="Exclude injured players from results")
    parser.add_argument("--on-block", action="store_true",
                        help="Show only players on the trade block")
    parser.add_argument("--year", type=int, default=None)
    args = parser.parse_args()

    results = find_targets(
        bucket=args.bucket,
        min_ovr=args.min_ovr,
        sellers_only=args.sellers_only,
        include_controlled=args.include_controlled,
        max_salary_m=args.max_salary,
        year=args.year,
        vs_hand=args.vs_hand,
        exclude_injured=args.exclude_injured,
        on_block_only=args.on_block,
    )
    print_targets(results, args.bucket)
