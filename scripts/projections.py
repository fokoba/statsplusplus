"""
projections.py — Player projection utilities for depth chart and roster planning.

Pure functions — no DB access. Takes player dicts as input, returns projections.
Used by web/team_queries.py::get_depth_chart().
"""

from statsplusplus.evaluation.war import peak_war_from_score as peak_war_from_ovr, aging_mult
from statsplusplus.evaluation.constants import PEAK_AGE_PITCHER, PEAK_AGE_HITTER

# ---------------------------------------------------------------------------
# OPS+ model — calibrated from 2,573 qualified hitter-seasons (PA >= 200)
# R² = 0.45, RMSE = 11.5 OPS+ points
# Inputs on 1-100 raw scale
# ---------------------------------------------------------------------------
_OPS_B0 = 48.55
_OPS_B  = {"cntct": 0.287, "gap": 0.074, "pow": 0.442, "eye": 0.198}

# ---------------------------------------------------------------------------
# Pitcher ERA/FIP — WAR-based derivation
# ERA: repl_era = lg_era + 0.40, RMSE = 1.00
# FIP: repl_fip = lg_fip + 0.53, RMSE = 0.73
# Both: projected = repl - peak_war * 81 / full_season_ip
# ---------------------------------------------------------------------------
_ERA_REPL_OFFSET = 0.40
_FIP_REPL_OFFSET = 0.53
_SP_FULL_IP = 200
_RP_FULL_IP = 65


def project_ovr(ovr, pot, age, bucket, year_offset):
    """Project Ovr for a future year using development ramp."""
    peak_age = PEAK_AGE_PITCHER if bucket in ("SP", "RP") else PEAK_AGE_HITTER
    future_age = age + year_offset
    ovr = ovr or 0
    pot = pot or ovr
    if pot <= ovr or age >= peak_age:
        return ovr
    years_to_peak = max(1, peak_age - age)
    progress = min(year_offset / years_to_peak, 1.0)
    return ovr + (pot - ovr) * progress


def project_war(ovr, pot, age, bucket, year_offset=0, stat_war=None):
    """Full-season WAR projection.

    year_offset=0 with stat_war: uses stat_war (actual performance).
    year_offset>0 with stat_war: blends stat_war into ratings projection
      with exponential decay (stat influence halves each year).
    Otherwise: Ovr-based with development ramp and aging curve.
    """
    proj_ovr = project_ovr(ovr, pot, age, bucket, year_offset)
    future_age = age + year_offset
    ratings_war = peak_war_from_ovr(proj_ovr, bucket) * aging_mult(future_age, bucket)
    ratings_war = max(ratings_war, 0.0)

    if stat_war is not None and stat_war > 0:
        if year_offset == 0:
            return stat_war
        # Blend: stat influence decays by half each year
        stat_weight = 0.5 ** year_offset
        blended = stat_weight * stat_war + (1 - stat_weight) * ratings_war
        # Apply aging from current year forward
        age_ratio = aging_mult(future_age, bucket) / max(aging_mult(age, bucket), 0.01)
        return max(blended * age_ratio, 0.0)

    return ratings_war


def _to_model_scale(val):
    """Convert a tool rating to the 1-100 scale used by projection model coefficients.
    On 1-100 leagues this is a no-op. On 20-80 leagues, maps 20→0, 50→50, 80→100."""
    from statsplusplus.config.league_config import LeagueConfig; _get_ratings_scale = lambda: LeagueConfig().ratings_scale
    if _get_ratings_scale() == "20-80":
        return (val - 20) / 60 * 100
    return val


def project_ops_plus(cntct, gap, pow_, eye):
    """Ratings -> OPS+ projection. Inputs are auto-converted to model scale."""
    c, g, p, e = _to_model_scale(cntct), _to_model_scale(gap), _to_model_scale(pow_), _to_model_scale(eye)
    return _OPS_B0 + _OPS_B["cntct"] * c + _OPS_B["gap"] * g \
        + _OPS_B["pow"] * p + _OPS_B["eye"] * e


def _int_or(val, default=50):
    """Coerce to int, returning default for None or non-numeric values."""
    if val is None:
        return default
    try:
        return int(val)
    except (ValueError, TypeError):
        return default


def project_ops_plus_splits(ratings):
    """Weighted OPS+ from L/R splits. ~60% of PA come vs RHP.

    ratings: dict with cntct_l, cntct_r, pow_l, pow_r, eye_l, eye_r, gap_l, gap_r
    Returns (overall_ops_plus, ops_vs_l, ops_vs_r).
    """
    vl = project_ops_plus(_int_or(ratings.get("cntct_l")) or _int_or(ratings.get("cntct")),
                          _int_or(ratings.get("gap_l")) or _int_or(ratings.get("gap")),
                          _int_or(ratings.get("pow_l")) or _int_or(ratings.get("pow")),
                          _int_or(ratings.get("eye_l")) or _int_or(ratings.get("eye")))
    vr = project_ops_plus(_int_or(ratings.get("cntct_r")) or _int_or(ratings.get("cntct")),
                          _int_or(ratings.get("gap_r")) or _int_or(ratings.get("gap")),
                          _int_or(ratings.get("pow_r")) or _int_or(ratings.get("pow")),
                          _int_or(ratings.get("eye_r")) or _int_or(ratings.get("eye")))
    weighted = vr * 0.60 + vl * 0.40  # 60% of PA vs RHP
    return weighted, vl, vr


def project_era(ovr, pot, age, bucket, year_offset=0, lg_era=4.55, stat_war=None):
    """Projected ERA from WAR -> run prevention."""
    war = project_war(ovr, pot, age, bucket, year_offset, stat_war)
    ip_full = _SP_FULL_IP if bucket == "SP" else _RP_FULL_IP
    repl = lg_era + _ERA_REPL_OFFSET
    return repl - war * 81 / ip_full


def project_fip(ovr, pot, age, bucket, year_offset=0, lg_fip=4.55, stat_war=None):
    """Projected FIP from WAR -> run prevention."""
    war = project_war(ovr, pot, age, bucket, year_offset, stat_war)
    ip_full = _SP_FULL_IP if bucket == "SP" else _RP_FULL_IP
    repl = lg_fip + _FIP_REPL_OFFSET
    return repl - war * 81 / ip_full


def project_ratings(ratings, year_offset, age, bucket):
    """Interpolate ratings toward potential for pre-peak players.

    Returns a new dict with projected values for cntct, gap, pow, eye,
    stf, mov, ctrl (averaged from ctrl_r/ctrl_l).
    """
    peak_age = PEAK_AGE_PITCHER if bucket in ("SP", "RP") else PEAK_AGE_HITTER
    if year_offset == 0 or age >= peak_age:
        progress = 0.0
    else:
        years_to_peak = max(1, peak_age - age)
        progress = min(year_offset / years_to_peak, 1.0)

    def _interp(cur, pot):
        if cur is None:
            return pot or 50
        if pot is None or pot <= cur:
            return cur
        return cur + (pot - cur) * progress

    return {
        "cntct": _interp(ratings.get("cntct"), ratings.get("pot_cntct")),
        "gap":   _interp(ratings.get("gap"),   ratings.get("pot_gap")),
        "pow":   _interp(ratings.get("pow"),   ratings.get("pot_pow")),
        "eye":   _interp(ratings.get("eye"),   ratings.get("pot_eye")),
        "stf":   _interp(ratings.get("stf"),   ratings.get("pot_stf")),
        "mov":   _interp(ratings.get("mov"),   ratings.get("pot_mov")),
        "ctrl":  _interp(
            ratings.get("ctrl") or ((ratings.get("ctrl_r") or 50) + (ratings.get("ctrl_l") or 50)) / 2,
            ratings.get("pot_ctrl")
        ),
    }


# ---------------------------------------------------------------------------
# Position viability thresholds (same as bucketing in player_utils / farm guide)
# ---------------------------------------------------------------------------
POS_THRESHOLDS = {
    "C": ("c", 45), "SS": ("ss", 50), "2B": ("second_b", 50),
    "3B": ("third_b", 45), "1B": ("first_b", 45),
    "LF": ("lf", 45), "CF": ("cf", 55), "RF": ("rf", 45),
}

# Level discount on playing time (how likely a non-MLB player contributes)
LEVEL_DISCOUNT = {"MLB": 1.0, "AAA": 0.5, "AA": 0.25, "A": 0.1, "A-Short": 0.05,
                  "Rookie": 0.02, "Intl": 0.0}

# Full-season baselines
DEFAULT_TEAM_PA = 6200
DEFAULT_TEAM_IP = 1450
SP_IP_SHARES = [0.20, 0.19, 0.18, 0.17, 0.16, 0.10]  # top 6 SP


def viable_positions(ratings, use_pot=False):
    """Return list of diamond positions a player is viable at, given ratings dict."""
    positions = []
    for pos, (field, thresh) in POS_THRESHOLDS.items():
        key = f"pot_{field}" if use_pot else field
        val = ratings.get(key) or ratings.get(field) or 0
        if val >= thresh:
            positions.append(pos)
    return positions


def assign_diamond_positions(player, fielding_games=None, batting_games=0, use_pot=False):
    """Determine which diamond positions a player appears at and their weight.

    player: dict with ratings fields + 'role'. May also include:
        'dh_primary': True if player was DH-primary in year 1 (persists to future years)
        'primary_pos': str position from year 1 (e.g. 'CF') for premium lock in future years
        'war_proj': projected WAR (used for premium position lock)
    fielding_games: dict of {pos_num: games} from fielding_stats (year 1 only)
    batting_games: total batting games (to detect full-time DH)
    use_pot: use potential ratings for viability (future years / young prospects)

    Returns list of (position_str, weight) tuples. Weights sum to 1.0.
    """
    role = player.get("role", 0)
    # A NULL games value (player with a stat row but no games logged) coalesces
    # to 0 rather than crashing the >= comparison below.
    batting_games = batting_games or 0
    # Pitchers don't appear on the diamond
    if role in (11, 12, 13):
        return []

    POS_NUM_MAP = {2:"C", 3:"1B", 4:"2B", 5:"3B", 6:"SS", 7:"LF", 8:"CF", 9:"RF", 10:"DH"}

    # Year 1: use fielding data if available, with ratings fallback
    if fielding_games:
        field_pos = {POS_NUM_MAP[p]: g for p, g in fielding_games.items()
                     if p in POS_NUM_MAP and p != 10 and g >= 3}

        # Detect DH-primary: if player has many more batting games than fielding games,
        # they're DHing most of the time (e.g. Vlad Jr, Devers, Yordan Alvarez).
        total_fld = sum(g for p, g in fielding_games.items() if p in POS_NUM_MAP and p != 10)
        dh_games = max(batting_games - total_fld, 0)
        if batting_games >= 5 and dh_games / batting_games >= 0.50:
            # DH-primary player who also plays the field sometimes
            dh_weight = dh_games / batting_games
            field_weight = 1.0 - dh_weight
            result = [("DH", dh_weight)]
            if field_pos:
                ftotal = sum(field_pos.values())
                for pos, g in field_pos.items():
                    result.append((pos, field_weight * g / ftotal))
            else:
                # DH-primary with viable field positions from ratings
                viable = viable_positions(player, use_pot=use_pot)
                if viable:
                    for vp in viable:
                        result.append((vp, field_weight / len(viable)))
            return result

        # Ratings fallback: if a player has only 1 fielding position and few
        # total games (bench player), add viable positions from ratings.
        # Skip for everyday players (15+ fielding games) and elite premium positions.
        if field_pos and len(field_pos) <= 1 and total_fld < 15:
            primary_pos = next(iter(field_pos))
            war = player.get("war_proj", 0)
            premium_lock = primary_pos in ("CF", "SS", "C") and war >= 5.0
            if not premium_lock:
                viable = viable_positions(player, use_pot=use_pot)
                for vp in viable:
                    if vp not in field_pos:
                        field_pos[vp] = max(1, min(g for g in field_pos.values()) * 0.10)
        if field_pos:
            total = sum(field_pos.values())
            return [(pos, g / total) for pos, g in field_pos.items()]

    # DH-primary flag persists across years — a player who was DH in year 1
    # stays DH in future years (they don't suddenly become a fielder).
    # Also catches year-1 DH detection (batting games but no fielding).
    if player.get("dh_primary") or (batting_games >= 5 and not fielding_games):
        field_pos = viable_positions(player, use_pot=use_pot)
        if field_pos:
            result = [("DH", 0.90)]
            weights = {}
            for pos in field_pos:
                fld, _ = POS_THRESHOLDS[pos]
                key = f"pot_{fld}" if use_pot else fld
                weights[pos] = player.get(key) or player.get(fld) or 0
            wt_total = sum(weights.values()) or 1
            for pos, w in weights.items():
                result.append((pos, 0.10 * w / wt_total))
            return result
        return [("DH", 1.0)]

    # Fallback: use ratings
    positions = viable_positions(player, use_pot=use_pot)
    if not positions:
        return [("DH", 1.0)]

    # Premium position lock: elite players at CF/SS/C stay at their position
    primary = player.get("primary_pos")
    war = player.get("war_proj", 0)
    if primary in ("CF", "SS", "C") and war >= 3.0 and primary in positions:
        return [(primary, 1.0)]

    # Weight toward highest-rated position, with primary position inertia.
    # A player who started at a position in year 1 should keep most of their
    # weight there in future years — they don't abandon their starting job
    # just because they *could* play elsewhere.
    weights = {}
    for pos in positions:
        field, _ = POS_THRESHOLDS[pos]
        key = f"pot_{field}" if use_pot else field
        val = player.get(key) or player.get(field) or 0
        weights[pos] = val
    if primary and primary in weights:
        # Strong inertia at primary position — premium positions get locked harder
        boost = 4.0 if primary in ("CF", "SS", "C") else 2.0
        weights[primary] *= boost
    total = sum(weights.values()) or 1
    return [(pos, w / total) for pos, w in weights.items()]


def identify_dh_candidates(players, position_assignments):
    """Find players who should be DH — significant hitters with no/minimal field position.

    players: list of player dicts (with 'player_id', 'war_proj', 'role')
    position_assignments: dict of player_id -> [(pos, weight), ...]

    Returns list of (player_dict, weight) for DH slot.
    """
    candidates = []
    for p in players:
        if p.get("role", 0) != 0:
            continue  # pitchers
        assignments = position_assignments.get(p["player_id"], [])
        field_weight = sum(w for pos, w in assignments if pos != "DH")
        # DH candidate if: no field position, or very low field weight
        if field_weight < 0.1 and p.get("war_proj", 0) > 0:
            candidates.append(p)
    # Sort by WAR, take top 3
    candidates.sort(key=lambda x: x.get("war_proj", 0), reverse=True)
    return candidates[:5]


def allocate_playing_time(players_by_pos, team_pa=None, team_ip=None):
    """Allocate playing time across positions.

    players_by_pos: dict of position -> list of player dicts, each with:
        'player_id', 'name', 'war_proj', 'level_discount', 'pos_weight',
        'split_ops_plus', 'ovr_ops_plus', 'ops_vs_l', 'ops_vs_r'
    team_pa: total team PA for the season
    team_ip: total team IP for the season

    Two-pass algorithm:
    1. Allocate per-position (85/15 starter/backup split)
    2. Enforce per-player PA cap — redistribute excess to backups

    Returns dict of position -> list of player dicts with 'pt_pct' and 'pa' added.
    """
    team_pa = team_pa or DEFAULT_TEAM_PA
    team_ip = team_ip or DEFAULT_TEAM_IP

    # Per-player PA cap: no player should exceed what an elite starter gets.
    # Top starters get 95% of a slot = ~654 PA. Catchers cap lower (~75%)
    # because they need more rest — but the position total stays the same.
    pos_pa_base = team_pa / 9
    MAX_PA = round(pos_pa_base * 0.95)       # ~654
    MAX_PA_C = round(pos_pa_base * 0.75)     # ~517

    # Position PA budget: every position gets the same total PA per season.
    # Catchers don't get fewer total PA — the position is filled every game.
    # The reduced workload is reflected in the starter/backup split, not the total.
    pos_pa_base = team_pa / 9
    pos_pa_map = {p: pos_pa_base for p in
                  ["C","1B","2B","3B","SS","LF","CF","RF","DH"]}

    FIELD_POSITIONS = ["C", "1B", "2B", "3B", "SS", "LF", "CF", "RF", "DH"]

    # Catcher starter share is lower — 65-75% due to physical demands
    CATCHER_STARTER_MAX = 0.75

    # Pass 1: initial allocation per position
    raw = {}  # pos -> [(player_dict, share), ...]
    for pos in FIELD_POSITIONS:
        players = players_by_pos.get(pos, [])
        if not players:
            raw[pos] = []
            continue

        # Compute effective WAR for ranking at this position.
        # Level discount and platoon splits affect ranking.
        # pos_weight does NOT affect ranking — a 2.1 WAR utility player
        # is still a 2.1 WAR option at any position he plays.
        # The per-player PA cap (pass 2) prevents him from exceeding
        # a full-time starter's total across all positions.
        for p in players:
            base = p["war_proj"] * p.get("level_discount", 1.0)
            s_ops = p.get("split_ops_plus")
            o_ops = p.get("ovr_ops_plus")
            if s_ops and o_ops and o_ops > 0:
                p["_eff_war"] = base * (s_ops / o_ops)
            else:
                p["_eff_war"] = base
        players.sort(key=lambda x: x["_eff_war"], reverse=True)

        # Find the starter: highest WAR player with meaningful presence at this position.
        # Prefer players with 40%+ of their games here, but if the best player
        # at the position has vastly higher WAR (3x+), they win regardless.
        entries = []
        starter_idx = 0  # default: best WAR
        for i, p in enumerate(players[:5]):
            if p.get("pos_weight", 1.0) >= 0.40:
                if i == 0 or players[0]["_eff_war"] < p["_eff_war"] * 3:
                    starter_idx = i
                break

        starter = players[starter_idx]
        w = starter["_eff_war"]
        if pos == "C":
            starter_share = min(CATCHER_STARTER_MAX, max(0.65, 0.65 + 0.025 * w))
        elif pos == "DH":
            starter_share = min(0.98, max(0.92, 0.92 + 0.015 * w))
        else:
            starter_share = min(0.95, max(0.85, 0.85 + 0.025 * w))
        entries.append((starter, starter_share))

        # Backups get the remainder, split by effective WAR with platoon bonus.
        # If a backup beats the starter by 5+ OPS+ vs a handedness, they get
        # a minimum floor of playing time from those games (~40% vs LHP, ~60% vs RHP).
        backup_pct = 1.0 - starter_share
        backups = [p for j, p in enumerate(players[:5]) if j != starter_idx]

        s_vl = starter.get("ops_vs_l", 0)
        s_vr = starter.get("ops_vs_r", 0)
        backup_weights = []
        platoon_floors = []  # minimum share for platoon backups
        for p in backups:
            w = max(p["_eff_war"], 0.01)
            backup_weights.append(w)
            # Platoon floor: if backup is 5+ OPS+ better vs a hand,
            # they should start a meaningful share of those games
            p_vl = p.get("ops_vs_l", 0)
            p_vr = p.get("ops_vs_r", 0)
            floor = 0.0
            if s_vl and p_vl > s_vl + 5:
                floor += 0.40 * min((p_vl - s_vl) / 30, 1.0)  # up to 40% of games vs LHP
            if s_vr and p_vr > s_vr + 5:
                floor += 0.60 * min((p_vr - s_vr) / 30, 1.0)  # up to 60% of games vs RHP
            platoon_floors.append(floor * backup_pct)

        # Distribute: first honor platoon floors (capped to backup_pct), then split remainder by WAR
        total_floor = sum(platoon_floors)
        if total_floor > backup_pct and total_floor > 0:
            scale = backup_pct / total_floor
            platoon_floors = [f * scale for f in platoon_floors]
            total_floor = backup_pct
        remaining_pct = max(backup_pct - total_floor, 0)
        total_bk = sum(backup_weights) or 1
        for i, p in enumerate(backups):
            war_share = remaining_pct * (backup_weights[i] / total_bk)
            share = platoon_floors[i] + war_share
            entries.append((p, share))
        raw[pos] = entries

    # Pass 2: enforce per-player PA cap across all positions
    # Sum each player's total PA across positions, then scale down if over cap
    player_total_pa = {}  # pid -> total PA
    for pos, entries in raw.items():
        pos_pa = pos_pa_map.get(pos, team_pa / 9)
        for p, share in entries:
            pid = p["player_id"]
            pa = pos_pa * share
            player_total_pa[pid] = player_total_pa.get(pid, 0) + pa

    # Compute scale factors for over-cap players
    player_scale = {}
    # Track which players are DH-primary (>50% of their raw PA from DH)
    player_dh_pa = {}
    for pos, entries in raw.items():
        pos_pa = pos_pa_map.get(pos, team_pa / 9)
        for p, share in entries:
            pid = p["player_id"]
            if pos == "DH":
                player_dh_pa[pid] = player_dh_pa.get(pid, 0) + pos_pa * share

    for pid, total in player_total_pa.items():
        # Check if this player is a catcher at any position
        is_catcher = any(pos == "C" and any(pp["player_id"] == pid for pp, _ in entries)
                         for pos, entries in raw.items())
        # DH-primary players can play every game — higher cap (~98%)
        is_dh_primary = player_dh_pa.get(pid, 0) > total * 0.50
        if is_catcher:
            cap = MAX_PA_C
        elif is_dh_primary:
            cap = round(pos_pa_base * 0.98)
        else:
            cap = MAX_PA
        if total > cap:
            player_scale[pid] = cap / total

    # Pass 3: build final output, redistributing excess PA to backups
    result = {}
    for pos in FIELD_POSITIONS:
        entries = raw.get(pos, [])
        if not entries:
            result[pos] = []
            continue

        pos_pa = pos_pa_map.get(pos, team_pa / 9)
        excess = 0.0
        allocated = []

        for i, (p, share) in enumerate(entries):
            pid = p["player_id"]
            scale = player_scale.get(pid, 1.0)
            adj_share = share * scale
            excess += share - adj_share  # accumulate what this player gave up

            p_out = {k: v for k, v in p.items() if not k.startswith("_eff")}
            p_out["pa"] = round(pos_pa * adj_share)
            p_out["pt_pct"] = round(adj_share * 100, 1)
            allocated.append(p_out)

        # Redistribute excess to backups who aren't themselves capped
        if excess > 0 and len(allocated) > 1:
            backups = [b for b in allocated[1:] if b["player_id"] not in player_scale]
            if not backups:
                backups = allocated[1:]  # fallback: spread among all backups
            backup_war = sum(max(b.get("war_proj", 0) * b.get("level_discount", 1.0), 0.01)
                            for b in backups)
            for b in backups:
                bw = max(b.get("war_proj", 0) * b.get("level_discount", 1.0), 0.01)
                extra_share = excess * (bw / backup_war)
                b["pa"] += round(pos_pa * extra_share)
                b["pt_pct"] = round(b["pt_pct"] + extra_share * 100, 1)

        result[pos] = allocated

    return result


def allocate_pitcher_time(sp_list, rp_list, team_ip=None):
    """Allocate innings to SP and RP lists.

    Each pitcher dict needs: 'player_id', 'name', 'war_proj', 'level_discount'
    Returns (sp_result, rp_result) with 'pt_pct' and 'ip' added.
    """
    team_ip = team_ip or DEFAULT_TEAM_IP
    sp_ip_total = team_ip * 0.62  # ~62% of innings to starters
    rp_ip_total = team_ip - sp_ip_total

    # SP: rank by WAR, assign shares — redistribute if fewer than 6 SP
    sp_list.sort(key=lambda x: x["war_proj"] * x.get("level_discount", 1.0), reverse=True)
    sp_count = min(len(sp_list), 6)
    sp_shares = SP_IP_SHARES[:sp_count]
    if sp_shares:
        # Normalize so shares sum to 1.0
        share_total = sum(sp_shares)
        sp_shares = [s / share_total for s in sp_shares]
    sp_result = []
    for i, p in enumerate(sp_list[:sp_count]):
        share = sp_shares[i]
        ip = round(sp_ip_total * share, 1)
        p_out = {k: v for k, v in p.items()}
        p_out["pt_pct"] = round(share * 100, 1)
        p_out["ip"] = ip
        sp_result.append(p_out)

    # RP: rank by WAR, distribute remaining IP proportionally
    rp_list.sort(key=lambda x: x["war_proj"] * x.get("level_discount", 1.0), reverse=True)
    n_rp = min(len(rp_list), 8)
    rp_result = []
    if n_rp > 0:
        # Weighted distribution: top RP gets more IP, declining
        rp_weights = [max(1.0 - i * 0.12, 0.3) for i in range(n_rp)]
        wt_total = sum(rp_weights)
        for i, p in enumerate(rp_list[:n_rp]):
            ip = round(rp_ip_total * rp_weights[i] / wt_total, 1)
            p_out = {k: v for k, v in p.items()}
            p_out["pt_pct"] = round((ip / team_ip) * 100, 1)
            p_out["ip"] = ip
            if i == 0:
                p_out["rp_role"] = "CL"
            elif i <= 2:
                p_out["rp_role"] = "SU"
            else:
                p_out["rp_role"] = "MR"
            rp_result.append(p_out)

    return sp_result, rp_result


# ---------------------------------------------------------------------------
# Roster availability — determine which players are under team control
# for each year in a multi-year projection window.
# ---------------------------------------------------------------------------

def roster_availability(players, year_offsets=(0, 1, 2)):
    """Determine which players are available in each projected year.

    Each player dict must include:
        player_id, name, age, level,
        contract: {years, current_year, salaries: [sal0..sal14],
                   team_option, player_option},
        control: {ctrl_years, pre_arb_left} or None (for FA/unknown),
        war_proj (full-season WAR at current age),
        ovr, pot, bucket

    Returns dict of {year_offset: [player_dict, ...]} with players available
    that year. Players gain 'salary' and 'ctrl_type' fields.
    """
    from statsplusplus.config.league_config import dollars_per_war, league_minimum
    from statsplusplus.config.league_context import get_league_dir, get_active_league_slug
    _ld = get_league_dir(get_active_league_slug())
    dpw = dollars_per_war(_ld)
    min_sal = league_minimum(_ld)
    import math

    result = {off: [] for off in year_offsets}

    for p in players:
        c = p.get("contract")
        ctrl = p.get("control")
        age = p["age"]
        ovr = p.get("ovr", 40)
        pot = p.get("pot", ovr)
        bucket = p.get("bucket", "CF")
        level = p.get("level", "MLB")

        # Prospects without contracts are always available
        if not c or level != "MLB":
            for off in year_offsets:
                result[off].append(p)
            continue

        yrs_total = c["years"]
        cur_yr = c["current_year"] or 0
        yrs_left = yrs_total - cur_yr  # years remaining including current
        has_to = c.get("team_option", False)
        has_po = c.get("player_option", False)

        for off in year_offsets:
            if off == 0:
                # Current year — everyone on the roster is available
                result[off].append(p)
                continue

            # Multi-year contract: check if it extends to this year
            if yrs_total > 1:
                last_yr_off = yrs_left - 1  # offset of the final contract year

                if off < yrs_left:
                    # Check if this is the option year (last year of contract)
                    if off == last_yr_off and has_to:
                        # Team option — exercise if surplus > 0
                        future_war = project_war(ovr, pot, age, bucket, off)
                        opt_idx = cur_yr + off
                        opt_sal = c["salaries"][opt_idx] if opt_idx < 15 else 0
                        if future_war * dpw > (opt_sal or 0):
                            result[off].append(p)
                        # else: option declined, player departs
                    elif off == last_yr_off and has_po:
                        # Player option on last year — assume exercised
                        result[off].append(p)
                    else:
                        # Guaranteed year
                        result[off].append(p)
                # else: contract expired, player is a free agent
                continue

            # 1-year contract: use estimated control
            if ctrl and ctrl.get("ctrl_years", 0) > off:
                # Check non-tender gate for arb-eligible years
                pre_arb = ctrl.get("pre_arb_left", 0)
                if off >= pre_arb:
                    from statsplusplus.evaluation.arb import arb_salary as _arb_salary
                    arb_yr = off - pre_arb + 1  # 1-indexed
                    base_sal = c["salaries"][0] if c["salaries"] else min_sal
                    arb_sal = _arb_salary(ovr, bucket, arb_yr, base_sal, min_sal)
                    future_war = project_war(ovr, pot, age, bucket, off)
                    if arb_sal > max(future_war * dpw, min_sal):
                        continue  # non-tendered
                result[off].append(p)
            elif ctrl is None:
                # Unknown control (likely 1yr FA deal) — gone after this year
                pass
            # else: control exhausted

    return result
