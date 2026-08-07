"""
trade_calculator.py — Trade surplus balance calculator.

Usage (simple):
  python3 scripts/trade_calculator.py --offer 62201,201 --receive 59877
  python3 scripts/trade_calculator.py --offer "Jeff Hudson" --receive "Greg Brewer"

Usage (full JSON, for salary retention or manual prospect overrides):
  python3 scripts/trade_calculator.py --trade '<json>'

Trade JSON format:
  {
    "my_team_send": [
      {"player_id": 62201, "retention": 0.15},
      {"player_id": 201, "is_prospect": true, "fv": 50, "age": 23, "level": "AAA", "bucket": "2B"}
    ],
    "my_team_receive": [
      {"player_id": 59877}
    ]
  }

  Legacy keys "angels_send"/"angels_receive" are also accepted.
"""

import argparse, json, os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from statsplusplus.config.league_context import get_league_dir, get_active_league_slug
from statsplusplus.config.league_config import LeagueConfig, dollars_per_war
from statsplusplus.data.db import get_connection

from contract_value import contract_value, get_player_info
from prospect_value import prospect_surplus_with_option, find_player

league_dir = get_league_dir(get_active_league_slug())
_cfg = LeagueConfig(base_dir=league_dir)

def _get_conn():
    return get_connection(league_dir)

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ---------------------------------------------------------------------------
# Name / ID resolution
# ---------------------------------------------------------------------------

def resolve_player(token):
    """Accept player_id (int or str) or player name. Returns {"player_id": int}."""
    token = str(token).strip()
    if token.isdigit():
        return {"player_id": int(token)}
    # Name lookup
    conn = _get_conn()
    rows = conn.execute(
        "SELECT player_id, name FROM players WHERE name LIKE ? ORDER BY level LIMIT 5",
        (f"%{token}%",)
    ).fetchall()
    conn.close()
    if not rows:
        raise ValueError(f"Player not found: '{token}'")
    if len(rows) > 1:
        matches = ", ".join(f"{r['name']} (id:{r['player_id']})" for r in rows)
        raise ValueError(f"Ambiguous name '{token}': {matches}")
    return {"player_id": rows[0]["player_id"]}


def parse_player_list(arg):
    """Parse comma-separated player IDs or names into spec list."""
    specs = []
    for token in arg.split(","):
        token = token.strip()
        if not token:
            continue
        specs.append(resolve_player(token))
    return specs

def value_player(spec):
    """
    Given a trade spec entry, return a valuation dict.
    Detects whether the player is on an MLB contract or a minor league contract.
    """
    pid        = spec["player_id"]
    retention  = spec.get("retention", 0.0)
    is_prospect = spec.get("is_prospect", False)

    # Try MLB contract first (unless explicitly flagged as prospect)
    if not is_prospect:
        result = contract_value(pid, retention_pct=retention)
        if result:
            return {"type": "contract", "data": result}

    # Prospect path — pull from DB
    fv, level, bucket, db_age, fv_plus, fv_continuous = find_player(pid)

    fv_raw  = spec.get("fv") or fv
    fv_plus = str(fv_raw).endswith("+") if isinstance(fv_raw, str) else fv_plus
    fv_int  = int(str(fv_raw).rstrip("+")) if fv_raw else None
    level   = spec.get("level")  or level
    bucket  = spec.get("bucket") or bucket
    age     = spec.get("age")    or db_age

    if not all([fv_int, level, bucket, age]):
        return {"type": "unknown", "player_id": pid,
                "error": f"Insufficient data for player {pid}. Provide fv/age/level/bucket in trade spec."}

    # Use fv_continuous for surplus when available (pre-rounding, more accurate)
    # If user overrides fv via spec, use the integer override instead.
    fv_for_surplus = fv_continuous if (fv_continuous is not None and not spec.get("fv")) else fv_int
    fv_plus_for_surplus = False if fv_for_surplus != fv_int else fv_plus

    # Look up ovr/pot for certainty multiplier
    from statsplusplus.data import db as _db
    conn = _get_conn()
    row = conn.execute("SELECT name, age FROM players WHERE player_id=?", (pid,)).fetchone()
    name = row["name"] if row else str(pid)
    rr = conn.execute("SELECT ovr, pot, pot_cf, pot_ss, pot_c, pot_second_b, pot_third_b, "
                      "composite_score, true_ceiling, ceiling_score "
                      "FROM ratings WHERE player_id=? ORDER BY snapshot_date DESC LIMIT 1", (pid,)).fetchone()
    conn.close()
    # Use model scores for certainty/option value and scarcity
    ovr = (rr["composite_score"] if rr and rr["composite_score"] else None) or (rr["ovr"] if rr else None)
    pot = (rr["true_ceiling"] or rr["ceiling_score"] if rr else None) or (rr["pot"] if rr else None)
    _def_keys = {'CF':'pot_cf','SS':'pot_ss','C':'pot_c','2B':'pot_second_b','3B':'pot_third_b'}
    def_rating = rr[_def_keys[bucket]] if rr and bucket in _def_keys else None

    SENSITIVITY = {"pessimistic": 0.85, "base": 1.00, "optimistic": 1.15}
    base_surplus = prospect_surplus_with_option(fv_for_surplus, age, level, bucket,
                                                 ovr=ovr, pot=pot,
                                                 fv_plus=fv_plus_for_surplus,
                                                 def_rating=def_rating)
    total_surplus = {s: max(0, round(base_surplus * mult)) for s, mult in SENSITIVITY.items()}

    fv_display = f"{fv_int}+" if fv_plus else str(fv_int)

    return {"type": "prospect", "data": {
        "player_id": pid, "name": name, "bucket": bucket,
        "fv": fv_int, "fv_display": fv_display, "level": level, "age": age,
        "total_surplus": total_surplus,
    }}

# ---------------------------------------------------------------------------
# Trade balance
# ---------------------------------------------------------------------------

def evaluate_trade(trade_spec):
    # Support both new keys and legacy "angels_send"/"angels_receive"
    my_send    = trade_spec.get("my_team_send") or trade_spec.get("angels_send", [])
    my_receive = trade_spec.get("my_team_receive") or trade_spec.get("angels_receive", [])

    send_valuations    = [value_player(s) for s in my_send]
    receive_valuations = [value_player(s) for s in my_receive]

    def net_surplus(valuations):
        total = {"pessimistic": 0, "base": 0, "optimistic": 0}
        for v in valuations:
            if v["type"] in ("contract", "prospect"):
                t = v["data"]["total_surplus"]
                for s in total:
                    total[s] += t.get(s, t.get("base", 0))
        return total

    my_receive_surplus = net_surplus(receive_valuations)
    my_send_surplus    = net_surplus(send_valuations)

    my_net    = {s: my_receive_surplus[s] - my_send_surplus[s] for s in ("pessimistic", "base", "optimistic")}
    other_net = {s: -my_net[s] for s in my_net}

    return {
        "my_team_send":    send_valuations,
        "my_team_receive": receive_valuations,
        "my_net":          my_net,
        "other_team_net":  other_net,
    }

# ---------------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------------

def fmt_millions(n):
    return f"${n/1_000_000:.1f}M" if abs(n) >= 1_000_000 else f"${n:,}"

def player_summary_line(v):
    if v["type"] == "contract":
        d = v["data"]
        t = d["total_surplus"]
        flags = f"  [{', '.join(d['flags'])}]" if d["flags"] else ""
        ret   = f"  (Angels retain {d['retention_pct']*100:.0f}%)" if d["retention_pct"] else ""
        surplus_str = f"base {fmt_millions(t['base'])} | pessimistic {fmt_millions(t['pessimistic'])} | optimistic {fmt_millions(t['optimistic'])}"
        return f"  {d['name']:25s} | {d['bucket']:4s} | Age {d['age']} | {d['years_left']} yrs left{ret}{flags}\n    Surplus: {surplus_str}"
    elif v["type"] == "prospect":
        d = v["data"]
        t = d["total_surplus"]
        fv_str = d.get("fv_display", d["fv"])
        surplus_str = f"base {fmt_millions(t['base'])} | pessimistic {fmt_millions(t['pessimistic'])} | optimistic {fmt_millions(t['optimistic'])}"
        return f"  {d['name']:25s} | {d['bucket']:4s} | FV {fv_str} | {d['level']} | Age {d['age']}\n    Surplus: {surplus_str}"
    else:
        return f"  [ERROR] Player {v['player_id']}: {v.get('error', 'unknown error')}"

def verdict(my_net, my_team):
    b = my_net["base"]
    p = my_net["pessimistic"]
    o = my_net["optimistic"]
    if b > 0 and p > 0:
        return f"{my_team} win in all scenarios (base: {fmt_millions(b)})"
    elif b > 0 and p < 0:
        return f"{my_team} win in base/optimistic, lose in pessimistic (base: {fmt_millions(b)})"
    elif b < 0 and o > 0:
        return f"{my_team} lose in base/pessimistic, win only in optimistic (base: {fmt_millions(b)})"
    else:
        return f"{my_team} lose in all scenarios (base: {fmt_millions(b)})"

def print_trade(result):
    my_team = _cfg.team_names_map.get(_cfg.my_team_id, "My Team")
    print("\n" + "="*60)
    print("TRADE SUMMARY")
    print("="*60)

    print(f"\n{my_team.upper()} SEND:")
    for v in result["my_team_send"]:
        print(player_summary_line(v))

    print(f"\n{my_team.upper()} RECEIVE:")
    for v in result["my_team_receive"]:
        print(player_summary_line(v))

    mn = result["my_net"]
    on = result["other_team_net"]
    print(f"\n{my_team.upper()} NET:  pessimistic {fmt_millions(mn['pessimistic'])} | base {fmt_millions(mn['base'])} | optimistic {fmt_millions(mn['optimistic'])}")
    print(f"OTHER TEAM NET:  pessimistic {fmt_millions(on['pessimistic'])} | base {fmt_millions(on['base'])} | optimistic {fmt_millions(on['optimistic'])}")

    print(f"\nVERDICT: {verdict(mn, my_team)}")
    print("="*60)

# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Trade surplus balance calculator")
    parser.add_argument("--offer", default=None,
                        help="Players my team sends: comma-separated IDs or names. "
                             "E.g. --offer 'Jeff Hudson,Pat Showalter'")
    parser.add_argument("--receive", default=None,
                        help="Players my team receives: comma-separated IDs or names.")
    parser.add_argument("--trade", default=None,
                        help="Full JSON trade spec (for salary retention or prospect overrides).")
    args = parser.parse_args()

    if args.trade:
        try:
            trade_spec = json.loads(args.trade)
        except json.JSONDecodeError as e:
            print(f"Invalid trade JSON: {e}"); sys.exit(1)
    elif args.offer or args.receive:
        try:
            trade_spec = {
                "my_team_send":    parse_player_list(args.offer or ""),
                "my_team_receive": parse_player_list(args.receive or ""),
            }
        except ValueError as e:
            print(f"Error: {e}"); sys.exit(1)
    else:
        parser.print_help(); sys.exit(1)

    result = evaluate_trade(trade_spec)
    print_trade(result)

if __name__ == "__main__":
    main()
