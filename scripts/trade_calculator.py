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
from statsplusplus.config.league_config import LeagueConfig, dollars_per_war, league_minimum
from statsplusplus.data.db import get_connection
from statsplusplus.evaluation.unified import unified_surplus, stat_confidence
from statsplusplus.evaluation.war import stat_peak_war
from statsplusplus.evaluation.constants import load_model_weights

league_dir = get_league_dir(get_active_league_slug())
_cfg = LeagueConfig(base_dir=league_dir)
_weights = load_model_weights(league_dir)
_dpw = dollars_per_war(league_dir)
_min_sal = league_minimum(league_dir)

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
    Given a trade spec entry, return a valuation dict using the unified model.
    Supports overrides for fv, age, level, bucket (for hypothetical scenarios).
    """
    pid = spec["player_id"]
    retention = spec.get("retention", 0.0)

    conn = _get_conn()

    # Get player info
    player = conn.execute(
        "SELECT name, age, level FROM players WHERE player_id=?", (pid,)
    ).fetchone()
    if not player:
        conn.close()
        return {"type": "unknown", "player_id": pid, "error": f"Player {pid} not found"}
    name = player["name"]
    age = spec.get("age") or player["age"]

    # Get ratings
    rr = conn.execute(
        "SELECT ovr, pot, composite_score, true_ceiling, ceiling_score, "
        "pot_cf, pot_ss, pot_c, pot_second_b, pot_third_b "
        "FROM ratings WHERE player_id=? ORDER BY snapshot_date DESC LIMIT 1",
        (pid,)
    ).fetchone()
    if not rr:
        conn.close()
        return {"type": "unknown", "player_id": pid, "error": f"No ratings for {name}"}

    composite = rr["composite_score"] or rr["ovr"] or 0
    ceiling = rr["true_ceiling"] or rr["ceiling_score"] or rr["pot"] or 0

    # Get pre-computed FV from player_evaluation (most accurate — includes MiLB context)
    pe = conn.execute(
        "SELECT fv, fv_str, fv_continuous, level, bucket, risk, surplus, "
        "stat_confidence, peak_war, years_control, ctrl_type "
        "FROM player_evaluation WHERE player_id=? ORDER BY eval_date DESC LIMIT 1",
        (pid,)
    ).fetchone()

    # Determine values — use pre-computed if no overrides, recompute if overrides given
    has_overrides = any(spec.get(k) for k in ("fv", "level", "bucket", "age"))

    if pe and not has_overrides:
        # Fast path: use pre-computed unified values
        fv_int = pe["fv"]
        fv_continuous = pe["fv_continuous"] or float(fv_int)
        level_str = pe["level"]
        bucket = pe["bucket"]
        risk = pe["risk"]

        SENSITIVITY = {"pessimistic": 0.85, "base": 1.00, "optimistic": 1.15}
        base_surplus = pe["surplus"]

        # Apply retention (reduces salary cost — increases surplus)
        if retention > 0 and pe["stat_confidence"] and pe["stat_confidence"] >= 0.5:
            # Retention adds value proportional to remaining salary cost
            # Rough: retention_pct × annual_salary × years_remaining
            c = conn.execute("SELECT salary_0, years, current_year FROM contracts WHERE player_id=?", (pid,)).fetchone()
            if c and c["salary_0"]:
                remaining_yrs = max(1, (c["years"] or 1) - (c["current_year"] or 0))
                retention_value = int(retention * c["salary_0"] * remaining_yrs)
                base_surplus += retention_value

        total_surplus = {s: max(0, round(base_surplus * mult)) for s, mult in SENSITIVITY.items()}
        years_left = pe["years_control"] or 1

        conn.close()

        val_type = "contract" if (pe["stat_confidence"] or 0) >= 0.75 else "prospect"
        return {"type": val_type, "data": {
            "player_id": pid, "name": name, "bucket": bucket,
            "age": age, "ovr": composite,
            "fv": fv_int, "fv_display": pe["fv_str"] or str(fv_int),
            "level": level_str, "risk": risk,
            "years_left": years_left,
            "total_surplus": total_surplus,
            "retention_pct": retention,
            "flags": [],
        }}

    # Slow path: recompute with overrides or when pre-computed not available
    # Resolve bucket
    from statsplusplus.utils.positions import assign_bucket
    role_map = {str(k): v for k, v in _cfg.role_map.items()}
    p = {"_role": role_map.get(str(rr.get("role") if hasattr(rr, "keys") else 0), "position_player"),
         "Pos": str(player.get("pos") if hasattr(player, "keys") else ""),
         "Ovr": composite, "Pot": ceiling}
    p["_is_pitcher"] = (p["Pos"] == "P" or p["_role"] in ("starter", "reliever", "closer"))
    bucket = spec.get("bucket") or (pe["bucket"] if pe else None) or assign_bucket(p)
    level_str = spec.get("level") or (pe["level"] if pe else ("MLB" if player["level"] == 1 else "AAA"))
    fv_continuous = float(spec.get("fv") or (pe["fv_continuous"] if pe else None) or
                          min(ceiling - 3, composite + (ceiling - composite) * 0.4))
    fv_int = int(spec.get("fv") or (pe["fv"] if pe else round(fv_continuous / 5) * 5))

    # Career stats for stat_confidence
    career_pa = conn.execute(
        "SELECT COALESCE(SUM(ab + COALESCE(bb,0) + COALESCE(hbp,0) + COALESCE(sf,0)), 0) "
        "FROM mlb_batting_stats WHERE player_id=? AND split_id=1", (pid,)
    ).fetchone()[0]
    career_ip = conn.execute(
        "SELECT COALESCE(SUM(ip), 0) FROM mlb_pitching_stats WHERE player_id=? AND split_id=1", (pid,)
    ).fetchone()[0]

    # Stat WAR
    import json as _json
    state = _json.loads((league_dir / "config" / "state.json").read_text())
    from statsplusplus.evaluation.war import load_stat_history as _lsh
    bat_hist, pit_hist, two_way = _lsh(conn, state["game_date"],
                                        dh_rule=_cfg.settings.get("dh_rule", "Universal DH"))
    sw = stat_peak_war(pid, bucket, bat_hist, pit_hist, two_way=two_way)

    # Control estimation
    career_ab = conn.execute(
        "SELECT COALESCE(SUM(ab), 0) FROM mlb_batting_stats WHERE player_id=? AND split_id=1", (pid,)
    ).fetchone()[0]
    if career_ab < 130 and career_ip < 50:
        years_ctrl = 6
    else:
        c = conn.execute("SELECT years, current_year, salary_0 FROM contracts WHERE player_id=?", (pid,)).fetchone()
        if c and c[0]:
            yrs_left = max(1, c[0] - (c[1] or 0))
            if yrs_left == 1 and (c[2] or 0) <= _min_sal * 1.1:
                svc = conn.execute("SELECT mlb_service_years FROM players WHERE player_id=?", (pid,)).fetchone()
                svc_years = (svc[0] or 0) if svc else 0
                years_ctrl = max(1, 6 - svc_years)
            else:
                years_ctrl = yrs_left
        else:
            years_ctrl = 3

    # Get salary schedule
    salaries = None
    c = conn.execute("SELECT * FROM contracts WHERE player_id=?", (pid,)).fetchone()
    if c and c["years"]:
        sals = []
        for i in range(c["current_year"] or 0, c["years"]):
            sals.append(c[f"salary_{i}"] or _min_sal)
        if len(sals) >= years_ctrl:
            salaries = sals[:years_ctrl]
        # Apply retention
        if retention > 0 and salaries:
            salaries = [max(0, int(s * (1 - retention))) for s in salaries]

    conn.close()

    result = unified_surplus(
        fv_continuous=fv_continuous, bucket=bucket, age=age, level=level_str,
        composite=composite, ceiling=ceiling,
        career_pa=career_pa, career_ip=career_ip, stat_war=sw,
        years_control=years_ctrl, salaries=salaries,
        dpw=_dpw, min_sal=_min_sal,
        perpetual_arb=_cfg.perpetual_arb,
        weights=_weights,
    )

    SENSITIVITY = {"pessimistic": 0.85, "base": 1.00, "optimistic": 1.15}
    total_surplus = {s: max(0, round(result["surplus"] * mult)) for s, mult in SENSITIVITY.items()}

    val_type = "contract" if result["stat_confidence"] >= 0.75 else "prospect"
    return {"type": val_type, "data": {
        "player_id": pid, "name": name, "bucket": bucket,
        "age": age, "ovr": composite,
        "fv": fv_int, "fv_display": str(fv_int),
        "level": level_str, "risk": pe["risk"] if pe else None,
        "years_left": years_ctrl,
        "total_surplus": total_surplus,
        "retention_pct": retention,
        "flags": [],
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
