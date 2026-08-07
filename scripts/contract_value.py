"""
contract_value.py — Contract surplus/deficit breakdown for any player in the league.
Usage:
  python3 scripts/contract_value.py <player_id>
  python3 scripts/contract_value.py <name search>
"""

import json, os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from statsplusplus.config.league_context import get_league_dir, get_active_league_slug
from statsplusplus.config.league_config import LeagueConfig
from statsplusplus.config.league_config import dollars_per_war as _dollars_per_war_pkg
from statsplusplus.config.league_config import league_minimum as _league_minimum_pkg
from statsplusplus.data.db import get_connection
from statsplusplus.utils.positions import assign_bucket
from statsplusplus.evaluation.war import peak_war_from_score as peak_war_from_ovr, aging_mult, stat_peak_war
from statsplusplus.evaluation.arb import estimate_control as _estimate_control_pkg, arb_salary as _arb_salary, arb_salary_perpetual as _arb_salary_perp
from statsplusplus.evaluation.constants import \
    PEAK_AGE_PITCHER, PEAK_AGE_HITTER, \
    NO_TRACK_RECORD_DISCOUNT, MLB_SCARCITY
from constants import ARB_PCT

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Resolve league context once at module level
_league_dir = get_league_dir(get_active_league_slug())
_cfg = LeagueConfig(base_dir=_league_dir)

# Local wrappers to maintain no-arg calling convention used throughout this file
def dollars_per_war():
    return _dollars_per_war_pkg(_league_dir)

def league_minimum():
    return _league_minimum_pkg(_league_dir)

def estimate_control(conn, player_id, age, salary, bucket=None):
    return _estimate_control_pkg(conn, player_id, age, salary,
                                  min_sal=league_minimum(),
                                  perpetual_arb=_cfg.perpetual_arb,
                                  bucket=bucket)

# Override load_stat_history to pass league's DH rule
from statsplusplus.evaluation.war import load_stat_history as _load_stat_history_pkg

def load_stat_history(conn, game_date):
    return _load_stat_history_pkg(conn, game_date, dh_rule=_cfg.settings.get("dh_rule", "Universal DH"))

_state_cache = {}
_perp_arb_model_cache = {}


def _load_perp_arb_model():
    """Load calibrated perpetual arb salary model from model_weights.json.

    Returns the model dict (with "curve" key) or None if not calibrated.
    Cached after first load.
    """
    if "model" not in _perp_arb_model_cache:
        try:
            from league_context import get_league_dir
            mw_path = get_league_dir() / "config" / "model_weights.json"
            if mw_path.exists():
                mw = json.load(open(mw_path))
                _perp_arb_model_cache["model"] = mw.get("ARB_SALARY_MODEL")
            else:
                _perp_arb_model_cache["model"] = None
        except Exception:
            _perp_arb_model_cache["model"] = None
    return _perp_arb_model_cache["model"]

def _get_state():
    if not _state_cache:
        from league_context import get_league_dir
        state_path = get_league_dir() / "config" / "state.json"
        with open(state_path) as f:
            _state_cache.update(json.load(f))
    return _state_cache

KEY_MAP = {
    "pot_fst":"PotFst","pot_snk":"PotSnk","pot_crv":"PotCrv","pot_sld":"PotSld",
    "pot_chg":"PotChg","pot_splt":"PotSplt","pot_cutt":"PotCutt","pot_circhg":"PotCirChg",
    "pot_scr":"PotScr","pot_frk":"PotFrk","pot_kncrv":"PotKncrv","pot_knbl":"PotKnbl",
    "pot_c":"PotC","pot_ss":"PotSS","pot_second_b":"Pot2B","pot_third_b":"Pot3B",
    "pot_first_b":"Pot1B","pot_lf":"PotLF","pot_cf":"PotCF","pot_rf":"PotRF",
    "stm":"Stm","ovr":"Ovr","pot":"Pot",
    "c":"C","ss":"SS","second_b":"2B","third_b":"3B","first_b":"1B",
    "lf":"LF","cf":"CF","rf":"RF",
}


def _resolve(conn, query):
    try:
        pid = int(query)
        row = conn.execute("SELECT player_id, name, age FROM players WHERE player_id=?", (pid,)).fetchone()
    except ValueError:
        row = conn.execute(
            "SELECT player_id, name, age FROM players WHERE name LIKE ? AND level=1 LIMIT 1",
            (f"%{query}%",)
        ).fetchone()
    if not row:
        return None

    pid, name, age = row["player_id"], row["name"], row["age"]
    r = conn.execute(
        "SELECT * FROM ratings WHERE player_id=? ORDER BY snapshot_date DESC LIMIT 1", (pid,)
    ).fetchone()
    if not r:
        return None

    rat = dict(r)
    for db_key, api_key in KEY_MAP.items():
        if db_key in rat:
            rat[api_key] = rat[db_key]

    # Prefer composite_score/ceiling_score over OVR/POT when available
    ovr = rat.get("composite_score") or rat.get("ovr") or 0
    pot = rat.get("true_ceiling") or rat.get("ceiling_score") or rat.get("pot") or 0

    p = conn.execute("SELECT role, pos FROM players WHERE player_id=?", (pid,)).fetchone()
    rat["_role"] = "starter"  if p and str(p["role"]) == "11" else \
                   "reliever" if p and str(p["role"]) in ("12","13") else "position_player"
    rat["Pos"] = str(p["pos"]) if p else "0"

    bucket = assign_bucket(rat, use_pot=False)
    return pid, name, age, ovr, pot, bucket


def contract_value(player_id, retention_pct=0.0, _conn=None, _hist=None):
    """
    Programmatic interface for trade_calculator. Returns surplus dict or None.
    retention_pct: fraction of salary the sending team retains.

    For 1yr contracts, estimates remaining team control (pre-arb + arb years)
    and projects salary schedule accordingly.

    Optional _conn/_hist for batch mode (avoids repeated DB/stat loading).
    _hist = (bat_hist, pit_hist, two_way) tuple from load_stat_history().
    """
    conn = _conn or get_connection(_league_dir)
    result = _resolve(conn, str(player_id))
    if not result:
        return None

    pid, name, age, ovr, pot, bucket = result
    c = conn.execute("SELECT * FROM contracts WHERE player_id=?", (pid,)).fetchone()
    if not c:
        return None

    state     = _get_state()
    game_date = state["game_date"]
    game_year = int(game_date[:4])

    if _hist:
        bat_hist, pit_hist = _hist[0], _hist[1]
        two_way = _hist[2] if len(_hist) > 2 else set()
    else:
        bat_hist, pit_hist, two_way = load_stat_history(conn, game_date)
    stat_war = stat_peak_war(pid, bucket, bat_hist, pit_hist, two_way=two_way)
    ratings_war = peak_war_from_ovr(ovr, bucket)
    no_track_record = stat_war is None
    if stat_war is None:
        pw = ratings_war * NO_TRACK_RECORD_DISCOUNT
    elif ratings_war > stat_war and age < (PEAK_AGE_PITCHER if bucket in ("SP", "RP") else PEAK_AGE_HITTER):
        peak_age = PEAK_AGE_PITCHER if bucket in ("SP", "RP") else PEAK_AGE_HITTER
        rtg_w = min(0.5, max(0, (peak_age - age) / (peak_age - 21)))
        pw = rtg_w * ratings_war + (1 - rtg_w) * stat_war
    elif ratings_war < stat_war and age > (PEAK_AGE_PITCHER if bucket in ("SP", "RP") else PEAK_AGE_HITTER):
        decline_start = PEAK_AGE_PITCHER if bucket in ("SP", "RP") else PEAK_AGE_HITTER
        age_w = min(1.0, (age - decline_start) / 5)
        gap_ratio = (stat_war - ratings_war) / stat_war if stat_war > 0 else 0
        rtg_w = min(0.75, age_w * gap_ratio)
        pw = rtg_w * ratings_war + (1 - rtg_w) * stat_war
    else:
        pw = stat_war

    dpw     = dollars_per_war()
    min_sal = league_minimum()
    from constants import MLB_SCARCITY
    scarcity = MLB_SCARCITY.get(bucket, 1.0)

    years_total  = c["years"]
    current_year = c["current_year"] or 0
    remaining    = years_total - current_year

    # Check for pending contract extension
    try:
        ext = conn.execute("SELECT * FROM contract_extensions WHERE player_id=?", (pid,)).fetchone()
    except Exception:
        ext = None
    ext_start = None  # index in breakdown where extension kicks in

    # For 1yr contracts, estimate full team control period
    ctrl_type = "contract"
    pre_arb_left = 0
    if ext and ext["years"] > 0:
        # Extension exists — use current contract remaining + extension years
        ext_start = remaining
        remaining += ext["years"]
        ctrl_type = "extension"
    elif years_total == 1:
        est_ctrl, est_sals, est_pre_arb = estimate_control(conn, pid, age, c["salary_0"] or 0, bucket=bucket)
        if est_ctrl and est_ctrl > 1:
            remaining = est_ctrl
            pre_arb_left = est_pre_arb or 0
            ctrl_type = "estimated"

    peak_age        = PEAK_AGE_PITCHER if bucket in ("SP", "RP") else PEAK_AGE_HITTER
    use_incremental = stat_war is not None and age > peak_age
    base_mult       = aging_mult(age, bucket) if use_incremental else 1.0

    # Development ramp: for players below peak age with Pot > Ovr,
    # project Ovr growth toward Pot over remaining years to peak.
    dev_ramp = (not use_incremental and pot > ovr and age < peak_age)
    years_to_peak = max(1, peak_age - age)

    SENSITIVITY = {"pessimistic": 0.85, "base": 1.00, "optimistic": 1.15}
    totals   = {s: 0.0 for s in SENSITIVITY}
    breakdown = []

    # For perpetual arb: track cumulative career WAR (used by salary model)
    _career_war_accum = 0.0
    try:
        from league_config import config as _cfg_init
        if _cfg_init.perpetual_arb:
            # Sum prior career WAR from stats
            _cw_bat = conn.execute(
                "SELECT COALESCE(SUM(war), 0) FROM mlb_batting_stats WHERE player_id=? AND split_id=1",
                (pid,)).fetchone()[0]
            _cw_pit = conn.execute(
                "SELECT COALESCE(SUM((war + COALESCE(ra9war, war))/2.0), 0) FROM mlb_pitching_stats WHERE player_id=? AND split_id=1",
                (pid,)).fetchone()[0]
            _career_war_accum = (_cw_bat or 0) + (_cw_pit or 0)
    except Exception:
        pass

    for i in range(remaining):
        a = age + i

        if use_incremental:
            curr_mult = aging_mult(a, bucket)
            war_base  = pw * (curr_mult / base_mult) if base_mult > 0 else 0
        elif dev_ramp and i > 0:
            # Linearly ramp Ovr toward Pot, capping at peak age
            progress = min(i / years_to_peak, 1.0)
            proj_ovr = ovr + (pot - ovr) * progress
            ratings_war = peak_war_from_ovr(proj_ovr, bucket) * aging_mult(a, bucket)
            # Blend stat history with ratings projection — decays at 0.5^year
            if stat_war is not None:
                stat_weight = 0.5 ** i
                war_base = stat_weight * stat_war + (1 - stat_weight) * ratings_war
            else:
                # No track record: discount ratings projection
                war_base = ratings_war * 0.5
        else:
            war_base = pw * aging_mult(a, bucket)
        # Floor WAR at 0 — a team can always release/DFA a player
        war_base = max(war_base, 0.0)

        # Determine salary for this year
        if ctrl_type == "estimated" and i > 0:
            # Year 0 uses actual contract salary; future years are projected
            from league_config import config as _cfg_cv
            if _cfg_cv.perpetual_arb:
                # Perpetual arb: salary based on career WAR accumulation + ceiling
                # Salary can decrease if WAR declines. No pre-arb concept.
                _perp_model = _load_perp_arb_model()
                _career_war_accum += war_base
                sal_full = _arb_salary_perp(a, war_base, dpw, min_sal,
                                            career_war=_career_war_accum, model=_perp_model)
            elif i < pre_arb_left:
                sal_full = min_sal
            else:
                arb_yr = i - pre_arb_left + 1  # 1-indexed
                prior = breakdown[-1]["salary_full"] if breakdown else min_sal
                sal_full = _arb_salary(ovr, bucket, arb_yr, prior, min_sal)
            # Non-tender / diminishing surplus gate:
            # FA leagues: truncate when salary > 2× market value (non-tender)
            # Perpetual arb: truncate when surplus per year drops below 30% of
            #   year-1 surplus (diminishing returns — remaining years add little
            #   value). Only fires after year 2 to avoid early truncation for
            #   players still developing.
            mkt_val = war_base * dpw * scarcity
            if _cfg_cv.perpetual_arb:
                yr_surplus = mkt_val - sal_full
                if (i >= 3 and breakdown and breakdown[0].get("surplus", 0) > 0
                        and yr_surplus < breakdown[0]["surplus"] * 0.30):
                    break
            else:
                if i >= pre_arb_left and sal_full > max(mkt_val * 2, min_sal):
                    break
        elif ext_start is not None and i >= ext_start:
            ext_idx = i - ext_start
            sal_full = ext[f"salary_{ext_idx}"] if ext_idx < 15 else min_sal
            sal_full = sal_full or min_sal
        else:
            idx = current_year + i
            sal_full = c[f"salary_{idx}"] if idx < 15 else min_sal
            sal_full = sal_full or min_sal

        sal_net = sal_full * (1 - retention_pct)

        mkt = war_base * dpw * scarcity
        for scenario, mult in SENSITIVITY.items():
            totals[scenario] += war_base * mult * dpw * scarcity - sal_net

        breakdown.append({
            "year": game_year + i, "age": a,
            "war_base": round(war_base, 2),
            "market_value": round(mkt),
            "salary_full": sal_full,
            "salary_net": round(sal_net),
            "surplus": round(mkt - sal_net),
        })

    actual_years = len(breakdown)
    flags = []
    if c["no_trade"]:                flags.append("NTC")
    if c["last_year_team_option"]:   flags.append(f"team option yr {years_total}")
    if c["last_year_player_option"]: flags.append(f"player option yr {years_total}")
    if ctrl_type == "estimated":     flags.append(f"~{actual_years}yr control (estimated)")
    if ctrl_type == "extension":     flags.append(f"+{ext['years']}yr extension")

    return {
        "player_id": pid, "name": name, "bucket": bucket, "age": age, "ovr": ovr,
        "years_left": actual_years, "retention_pct": retention_pct, "flags": flags,
        "breakdown": breakdown,
        "total_surplus": {s: round(v) for s, v in totals.items()},
    }


def get_player_info(player_id):
    """Legacy shim for trade_calculator compatibility."""
    conn = get_connection(_league_dir)
    return _resolve(conn, str(player_id))


def contract_breakdown(query):
    conn = get_connection(_league_dir)
    result = _resolve(conn, query)
    if not result:
        print(f"Player not found: {query}")
        return

    pid, name, age, ovr, pot, bucket = result

    c = conn.execute("SELECT * FROM contracts WHERE player_id=?", (pid,)).fetchone()
    if not c:
        print(f"{name} has no active contract.")
        return

    state     = _get_state()
    game_date = state["game_date"]
    game_year = int(game_date[:4])

    bat_hist, pit_hist, two_way = load_stat_history(conn, game_date)
    stat_war = stat_peak_war(pid, bucket, bat_hist, pit_hist, two_way=two_way)
    ovr_war  = peak_war_from_ovr(ovr, bucket)
    pw       = stat_war if stat_war is not None else ovr_war
    source   = "stat-weighted" if stat_war is not None else "Ovr-based"
    if pid in two_way:
        source += " (two-way)"

    dpw     = dollars_per_war()
    min_sal = league_minimum()
    from constants import MLB_SCARCITY
    scarcity = MLB_SCARCITY.get(bucket, 1.0)

    years_total  = c["years"]
    current_year = c["current_year"] or 0
    remaining    = years_total - current_year

    peak_age        = PEAK_AGE_PITCHER if bucket in ("SP", "RP") else PEAK_AGE_HITTER
    use_incremental = stat_war is not None and age > peak_age
    base_mult       = aging_mult(age, bucket) if use_incremental else 1.0

    flags = []
    if c["no_trade"]:                flags.append("NTC")
    if c["last_year_team_option"]:   flags.append(f"team option yr {years_total}")
    if c["last_year_player_option"]: flags.append(f"player option yr {years_total}")

    stored = conn.execute(
        "SELECT surplus FROM player_surplus WHERE player_id=? AND eval_date=?",
        (pid, game_date)
    ).fetchone()

    div_flag = ""
    if stat_war is not None and abs(stat_war - ovr_war) > 1.5:
        dir_ = "above" if stat_war > ovr_war else "below"
        div_flag = f"  ⚠  Stat WAR ({stat_war:.1f}) is {dir_} Ovr-based estimate ({ovr_war:.1f}) — review manually\n"

    print(f"\n{name} | {bucket} | Age {age} | Ovr {ovr} Pot {pot}")
    print(f"Peak WAR: {pw:.2f}/yr ({source}) | $/WAR: ${dpw/1e6:.2f}M" +
          (f" × {scarcity:.2f} scarcity" if scarcity != 1.0 else ""))
    if div_flag:
        print(div_flag, end="")
    if flags:
        print(f"Contract flags: {', '.join(flags)}")
    print(f"{years_total}yr contract, year {current_year} of {years_total} — {remaining} years remaining\n")

    print(f"{'Year':<6} {'Age':<5} {'Salary':>10} {'Proj WAR':>9} {'Value':>10} {'Surplus':>10}")
    print("-" * 56)

    total_sal = total_val = total_sur = 0
    for i in range(remaining):
        idx = current_year + i
        if idx >= 15:
            break
        sal = c[f"salary_{idx}"] or min_sal
        yr  = game_year + i
        a   = age + i

        if use_incremental:
            curr_mult = aging_mult(a, bucket)
            war = pw * (curr_mult / base_mult) if base_mult > 0 else 0
        else:
            war = pw * aging_mult(a, bucket)

        val = war * dpw * scarcity
        sur = val - sal
        total_sal += sal; total_val += val; total_sur += sur

        opt = " <- player opt" if (c["last_year_player_option"] and i == remaining - 1) else \
              " <- team opt"   if (c["last_year_team_option"]   and i == remaining - 1) else ""
        print(f"{yr:<6} {a:<5} {sal/1e6:>9.1f}M {war:>8.1f}  {val/1e6:>9.1f}M {sur/1e6:>+10.1f}M{opt}")

    print("-" * 56)
    print(f"{'TOTAL':<11} {total_sal/1e6:>9.1f}M {'':>9} {total_val/1e6:>9.1f}M {total_sur/1e6:>+10.1f}M")
    if stored:
        print(f"\nStored surplus (fv_calc): ${stored['surplus']/1e6:.1f}M")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python3 scripts/contract_value.py <player_id or name>")
        sys.exit(1)
    contract_breakdown(" ".join(sys.argv[1:]))
