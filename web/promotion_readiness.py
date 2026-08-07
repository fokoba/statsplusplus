"""Promotion readiness indicator for minor league players.

Assesses whether a player is ready for promotion based on:
1. Performance at current level vs league average
2. Ratings readiness for the next level
3. Age context (young/normal/old for the level)

All thresholds are calibrated per-league from the actual player population.
"""

import json
from pathlib import Path


# Level hierarchy — higher number = lower level
LEVEL_ORDER = [1, 2, 3, 4, 6, 8]  # MLB, AAA, AA, A, Rookie, Intl
LEVEL_NAMES = {1: "MLB", 2: "AAA", 3: "AA", 4: "A", 6: "Rookie", 8: "Intl"}


def _next_level(current_level):
    """Return the next level up (lower number) in the hierarchy."""
    try:
        idx = LEVEL_ORDER.index(current_level)
        if idx == 0:
            return None  # Already MLB
        return LEVEL_ORDER[idx - 1]
    except ValueError:
        return None


def _get_level_averages(league_dir):
    """Load per-level batting and pitching averages from league_settings.json."""
    settings_path = league_dir / "config" / "league_settings.json"
    if not settings_path.exists():
        return {}
    settings = json.loads(settings_path.read_text())
    minor_leagues = settings.get("minor_leagues", [])

    # Aggregate by level (multiple leagues may share a level)
    by_level = {}
    for lg in minor_leagues:
        lv = lg["level"]
        if lv not in by_level:
            by_level[lv] = {"bat_obp": [], "bat_slg": [], "pit_era": []}
        bat = lg.get("batting_avg", {})
        pit = lg.get("pitching_avg", {})
        if bat.get("obp"):
            by_level[lv]["bat_obp"].append(bat["obp"])
        if bat.get("slg"):
            by_level[lv]["bat_slg"].append(bat["slg"])
        if pit.get("era"):
            by_level[lv]["pit_era"].append(pit["era"])

    result = {}
    for lv, data in by_level.items():
        result[lv] = {
            "obp": sum(data["bat_obp"]) / len(data["bat_obp"]) if data["bat_obp"] else 0.320,
            "slg": sum(data["bat_slg"]) / len(data["bat_slg"]) if data["bat_slg"] else 0.400,
            "era": sum(data["pit_era"]) / len(data["pit_era"]) if data["pit_era"] else 4.00,
        }
        result[lv]["ops"] = result[lv]["obp"] + result[lv]["slg"]
    return result


def _calibrate_thresholds(conn):
    """Derive OVR and age thresholds from the league's actual population.

    Returns dict keyed by level with:
      ovr_p75: OVR at the 75th percentile (strong for the level)
      ovr_p50: OVR at the 50th percentile (typical for the level)
      age_p50: median age (normal age for level)
      age_p75: 75th percentile age (old for level)
    """
    thresholds = {}
    for level in [1, 2, 3, 4, 6, 8]:
        ovr_rows = conn.execute(
            "SELECT r.ovr FROM players p "
            "JOIN latest_ratings r ON r.player_id = p.player_id "
            "WHERE p.level = ? AND r.ovr > 0 ORDER BY r.ovr",
            (level,)
        ).fetchall()
        age_rows = conn.execute(
            "SELECT p.age FROM players p WHERE p.level = ? AND p.age > 0 ORDER BY p.age",
            (level,)
        ).fetchall()

        if ovr_rows:
            ovrs = [r[0] for r in ovr_rows]
            n = len(ovrs)
            thresholds.setdefault(level, {})
            thresholds[level]["ovr_p50"] = ovrs[int(n * 0.50)]
            thresholds[level]["ovr_p75"] = ovrs[int(n * 0.75)]
            thresholds[level]["ovr_p90"] = ovrs[int(n * 0.90)]
        if age_rows:
            ages = [r[0] for r in age_rows]
            n = len(ages)
            thresholds.setdefault(level, {})
            thresholds[level]["age_p25"] = ages[int(n * 0.25)]
            thresholds[level]["age_p50"] = ages[int(n * 0.50)]
            thresholds[level]["age_p75"] = ages[int(n * 0.75)]

    return thresholds


def compute_promotion_readiness(player_id, conn, league_dir):
    """Compute promotion readiness for a single player.

    Returns dict with:
      tier: "ready" | "knocking" | "developing" | "too_early" | None
      label: human-readable label
      reasons: list of supporting evidence strings
      next_level: what they'd promote to
      performance_pct: how far above/below league avg (0-based, >0 = above)
    Or None if the player is MLB or has no level info.
    """
    # Get player info
    player = conn.execute(
        "SELECT p.level, p.age, r.ovr, r.pot "
        "FROM players p "
        "JOIN latest_ratings r ON r.player_id = p.player_id "
        "WHERE p.player_id = ?", (player_id,)
    ).fetchone()

    if not player or not player[0]:
        return None  # No level info

    level = int(player[0]) if player[0] else 0
    if level <= 1:
        return None  # MLB or unknown

    age, ovr, pot = player[1], player[2], player[3]
    next_lv = _next_level(level)
    if next_lv is None:
        return None

    # Get current-level stats
    # Find league_ids for this level
    settings_path = league_dir / "config" / "league_settings.json"
    settings = json.loads(settings_path.read_text()) if settings_path.exists() else {}
    minor_leagues = settings.get("minor_leagues", [])
    level_lids = [lg["lid"] for lg in minor_leagues if lg["level"] == level]

    # Determine if hitter or pitcher
    role_row = conn.execute(
        "SELECT role FROM players WHERE player_id = ?", (player_id,)
    ).fetchone()
    is_pitcher = role_row and role_row[0] in (11, 12, 13)

    # Get performance at current level (current year)
    state_path = league_dir / "config" / "state.json"
    year = json.loads(state_path.read_text()).get("year", 2033) if state_path.exists() else 2033

    performance_pct = None
    sample_size = 0

    if not is_pitcher:
        if level_lids:
            placeholders = ",".join("?" * len(level_lids))
            stat_row = conn.execute(
                f"SELECT SUM(pa), SUM(h), SUM(ab), SUM(bb), SUM(hbp), SUM(sf), "
                f"SUM(h)+SUM(d)+2*SUM(t)+3*SUM(hr) as tb "
                f"FROM batting_stats "
                f"WHERE player_id=? AND split_id=1 AND year=? "
                f"AND league_id IN ({placeholders})",
                (player_id, year, *level_lids)
            ).fetchone()
            if stat_row and stat_row[0] and stat_row[0] > 0:
                pa, h, ab, bb, hbp, sf, tb = stat_row
                hbp = hbp or 0
                sf = sf or 0
                sample_size = pa
                obp = (h + bb + hbp) / (ab + bb + hbp + sf) if (ab + bb + hbp + sf) else 0
                slg = tb / ab if ab else 0
                ops = obp + slg
                # Compare to level average
                level_avgs = _get_level_averages(league_dir)
                lg_ops = level_avgs.get(level, {}).get("ops", 0.720)
                if lg_ops > 0:
                    performance_pct = (ops - lg_ops) / lg_ops
    else:
        if level_lids:
            placeholders = ",".join("?" * len(level_lids))
            stat_row = conn.execute(
                f"SELECT SUM(ip), SUM(er) "
                f"FROM pitching_stats "
                f"WHERE player_id=? AND split_id=1 AND year=? "
                f"AND league_id IN ({placeholders})",
                (player_id, year, *level_lids)
            ).fetchone()
            if stat_row and stat_row[0] and stat_row[0] > 0:
                ip, er = stat_row
                sample_size = round(ip)  # Use IP as sample proxy
                era = er * 27 / (ip * 3) if ip else 99
                # Compare to level average (for ERA, lower = better)
                level_avgs = _get_level_averages(league_dir)
                lg_era = level_avgs.get(level, {}).get("era", 4.00)
                if lg_era > 0:
                    performance_pct = (lg_era - era) / lg_era  # Positive = better than avg

    # Minimum sample thresholds
    min_sample = 80 if not is_pitcher else 30
    if sample_size < min_sample:
        return {
            "tier": "too_early",
            "label": "Too Early",
            "reasons": [f"Only {sample_size} {'PA' if not is_pitcher else 'IP'} at level"],
            "next_level": LEVEL_NAMES.get(next_lv, "?"),
            "performance_pct": performance_pct,
        }

    # Calibrate thresholds from league population
    thresholds = _calibrate_thresholds(conn)
    level_thresh = thresholds.get(level, {})
    next_thresh = thresholds.get(next_lv, {})

    # Signal 1: Performance
    # >10% above league avg = strong, >0% = solid, below = still developing
    perf_strong = performance_pct is not None and performance_pct > 0.10
    perf_solid = performance_pct is not None and performance_pct > 0.0

    # Signal 2: Ratings readiness for next level
    # OVR at or above the next level's median = ratings-ready
    next_level_median = next_thresh.get("ovr_p50", 40)
    ratings_ready = ovr >= next_level_median
    # Strong ratings: at/above next level's 75th percentile
    next_level_p75 = next_thresh.get("ovr_p75", 45)
    ratings_strong = ovr >= next_level_p75

    # Signal 3: Age context
    level_age_median = level_thresh.get("age_p50", 23)
    level_age_p75 = level_thresh.get("age_p75", 25)
    level_age_p25 = level_thresh.get("age_p25", 21)
    age_old = age >= level_age_p75
    age_young = age <= level_age_p25

    # Determine tier
    reasons = []

    if perf_strong and ratings_ready:
        tier = "ready"
        label = "Ready"
        reasons.append(f"Performing {performance_pct*100:+.0f}% vs level avg")
        reasons.append(f"OVR {ovr} ≥ {LEVEL_NAMES.get(next_lv, '?')} median ({next_level_median})")
        if age_old:
            reasons.append(f"Age {age} — old for level, nothing more to prove")
    elif (perf_strong and not ratings_ready) or (ratings_strong and perf_solid):
        tier = "knocking"
        label = "Knocking"
        if perf_strong:
            reasons.append(f"Performing {performance_pct*100:+.0f}% vs level avg")
            reasons.append(f"OVR {ovr} still below {LEVEL_NAMES.get(next_lv, '?')} median ({next_level_median})")
        else:
            reasons.append(f"OVR {ovr} — ratings ready for {LEVEL_NAMES.get(next_lv, '?')}")
            reasons.append(f"Performance {performance_pct*100:+.0f}% vs level avg — solid but not dominant")
        if age_old:
            reasons.append(f"Age {age} — should be challenged at a higher level")
    elif perf_solid and age_old:
        # Old for level + performing above average = knocking even without elite ratings
        tier = "knocking"
        label = "Knocking"
        reasons.append(f"Performing {performance_pct*100:+.0f}% vs level avg")
        reasons.append(f"Age {age} — old for {LEVEL_NAMES.get(level, '?')}, deserves a look")
    else:
        tier = "developing"
        label = "Developing"
        if performance_pct is not None:
            reasons.append(f"Performance {performance_pct*100:+.0f}% vs level avg")
        reasons.append(f"OVR {ovr} — {'growing toward' if ovr < next_level_median else 'approaching'} {LEVEL_NAMES.get(next_lv, '?')} level")
        if age_young:
            reasons.append(f"Age {age} — young for level, time to develop")

    return {
        "tier": tier,
        "label": label,
        "reasons": reasons,
        "next_level": LEVEL_NAMES.get(next_lv, "?"),
        "performance_pct": performance_pct,
    }


def compute_team_promotion_readiness(team_id, conn, league_dir):
    """Compute promotion readiness for all players on a minor league team.

    Returns list of (player_id, name, pos, readiness_dict) sorted by tier priority.
    """
    players = conn.execute(
        "SELECT player_id, name, pos FROM players WHERE team_id = ? AND level > 1",
        (team_id,)
    ).fetchall()

    results = []
    for row in players:
        pid, name, pos = row[0], row[1], row[2]
        readiness = compute_promotion_readiness(pid, conn, league_dir)
        if readiness:
            results.append((pid, name, pos, readiness))

    # Sort: ready first, then knocking, then developing, then too_early
    tier_order = {"ready": 0, "knocking": 1, "developing": 2, "too_early": 3}
    results.sort(key=lambda x: tier_order.get(x[3]["tier"], 9))
    return results


def _prev_level(current_level):
    """Return the next level down (higher number) in the hierarchy."""
    try:
        idx = LEVEL_ORDER.index(current_level)
        if idx >= len(LEVEL_ORDER) - 1:
            return None  # Already at lowest level
        return LEVEL_ORDER[idx + 1]
    except ValueError:
        return None


def compute_demotion_risk(player_id, conn, league_dir):
    """Compute demotion risk for a player at any level including MLB.

    Returns dict with:
      tier: "overmatched" | "struggling" | None
      label: human-readable label
      reasons: list of supporting evidence strings
      prev_level: what they'd be demoted to
      performance_pct: how far below league avg (negative = below)
    Or None if no demotion signal.
    """
    # Get player info
    player = conn.execute(
        "SELECT p.level, p.age, r.ovr, r.pot "
        "FROM players p "
        "JOIN latest_ratings r ON r.player_id = p.player_id "
        "WHERE p.player_id = ?", (player_id,)
    ).fetchone()

    if not player or not player[0]:
        return None

    level = int(player[0]) if player[0] else 0
    if level == 0:
        return None

    age, ovr, pot = player[1], player[2], player[3]
    prev_lv = _prev_level(level)
    if prev_lv is None:
        return None  # Can't demote from lowest level

    # Determine if hitter or pitcher
    role_row = conn.execute(
        "SELECT role FROM players WHERE player_id = ?", (player_id,)
    ).fetchone()
    is_pitcher = role_row and role_row[0] in (11, 12, 13)

    # Get performance at current level (current year)
    settings_path = league_dir / "config" / "league_settings.json"
    settings = json.loads(settings_path.read_text()) if settings_path.exists() else {}
    state_path = league_dir / "config" / "state.json"
    year = json.loads(state_path.read_text()).get("year", 2033) if state_path.exists() else 2033

    performance_pct = None
    sample_size = 0

    if level == 1:
        # MLB — use mlb views and league averages
        la_path = league_dir / "config" / "league_averages.json"
        la = json.loads(la_path.read_text()) if la_path.exists() else {}

        if not is_pitcher:
            stat_row = conn.execute(
                "SELECT SUM(pa), SUM(h), SUM(ab), SUM(bb), SUM(hbp), SUM(sf), "
                "SUM(h)+SUM(d)+2*SUM(t)+3*SUM(hr) as tb "
                "FROM mlb_batting_stats "
                "WHERE player_id=? AND split_id=1 AND year=?",
                (player_id, year)
            ).fetchone()
            if stat_row and stat_row[0] and stat_row[0] > 0:
                pa, h, ab, bb, hbp, sf, tb = stat_row
                hbp = hbp or 0
                sf = sf or 0
                sample_size = pa
                obp = (h + bb + hbp) / (ab + bb + hbp + sf) if (ab + bb + hbp + sf) else 0
                slg = tb / ab if ab else 0
                ops = obp + slg
                lg_ops = la.get("batting", {}).get("obp", 0.320) + la.get("batting", {}).get("slg", 0.400)
                if lg_ops > 0:
                    performance_pct = (ops - lg_ops) / lg_ops
        else:
            stat_row = conn.execute(
                "SELECT SUM(ip), SUM(er) "
                "FROM mlb_pitching_stats "
                "WHERE player_id=? AND split_id=1 AND year=?",
                (player_id, year)
            ).fetchone()
            if stat_row and stat_row[0] and stat_row[0] > 0:
                ip, er = stat_row
                sample_size = round(ip)
                era = er * 27 / (ip * 3) if ip else 99
                lg_era = la.get("pitching", {}).get("era", 4.00)
                if lg_era > 0:
                    performance_pct = (lg_era - era) / lg_era
    else:
        # Minor league — same approach as promotion readiness
        minor_leagues = settings.get("minor_leagues", [])
        level_lids = [lg["lid"] for lg in minor_leagues if lg["level"] == level]

        if not is_pitcher:
            if level_lids:
                placeholders = ",".join("?" * len(level_lids))
                stat_row = conn.execute(
                    f"SELECT SUM(pa), SUM(h), SUM(ab), SUM(bb), SUM(hbp), SUM(sf), "
                    f"SUM(h)+SUM(d)+2*SUM(t)+3*SUM(hr) as tb "
                    f"FROM batting_stats "
                    f"WHERE player_id=? AND split_id=1 AND year=? "
                    f"AND league_id IN ({placeholders})",
                    (player_id, year, *level_lids)
                ).fetchone()
                if stat_row and stat_row[0] and stat_row[0] > 0:
                    pa, h, ab, bb, hbp, sf, tb = stat_row
                    hbp = hbp or 0
                    sf = sf or 0
                    sample_size = pa
                    obp = (h + bb + hbp) / (ab + bb + hbp + sf) if (ab + bb + hbp + sf) else 0
                    slg = tb / ab if ab else 0
                    ops = obp + slg
                    level_avgs = _get_level_averages(league_dir)
                    lg_ops = level_avgs.get(level, {}).get("ops", 0.720)
                    if lg_ops > 0:
                        performance_pct = (ops - lg_ops) / lg_ops
        else:
            if level_lids:
                placeholders = ",".join("?" * len(level_lids))
                stat_row = conn.execute(
                    f"SELECT SUM(ip), SUM(er) "
                    f"FROM pitching_stats "
                    f"WHERE player_id=? AND split_id=1 AND year=? "
                    f"AND league_id IN ({placeholders})",
                    (player_id, year, *level_lids)
                ).fetchone()
                if stat_row and stat_row[0] and stat_row[0] > 0:
                    ip, er = stat_row
                    sample_size = round(ip)
                    era = er * 27 / (ip * 3) if ip else 99
                    level_avgs = _get_level_averages(league_dir)
                    lg_era = level_avgs.get(level, {}).get("era", 4.00)
                    if lg_era > 0:
                        performance_pct = (lg_era - era) / lg_era

    # Minimum sample
    min_sample = 80 if not is_pitcher else 30
    if sample_size < min_sample:
        return None  # Not enough data to flag

    # No demotion signal if performing near or above average
    if performance_pct is None or performance_pct >= -0.05:
        return None

    # Calibrate thresholds
    thresholds = _calibrate_thresholds(conn)
    level_thresh = thresholds.get(level, {})

    # Age context — suppress demotion flag for young players (aggressive assignment)
    level_age_p25 = level_thresh.get("age_p25", 20)
    age_young = age <= level_age_p25

    if age_young:
        return None  # Young for level — aggressive assignment, give time

    # OVR context
    level_ovr_p25 = level_thresh.get("ovr_p50", 35)  # Below median = weak for level
    level_ovr_floor = level_thresh.get("ovr_p50", 35) - 5  # Well below median
    ratings_weak = ovr < level_ovr_p25
    ratings_very_weak = ovr < level_ovr_floor

    # Determine tier
    perf_poor = performance_pct < -0.10  # More than 10% below average
    perf_very_poor = performance_pct < -0.20  # More than 20% below average

    reasons = []

    if perf_poor and ratings_weak:
        tier = "overmatched"
        label = "Overmatched"
        reasons.append(f"Performing {performance_pct*100:+.0f}% vs level avg")
        reasons.append(f"OVR {ovr} below {LEVEL_NAMES.get(level, '?')} median ({level_thresh.get('ovr_p50', '?')})")
    elif perf_very_poor:
        tier = "overmatched"
        label = "Overmatched"
        reasons.append(f"Performing {performance_pct*100:+.0f}% vs level avg")
        if not ratings_weak:
            reasons.append(f"OVR {ovr} suggests ability — may be slumping or adjusting")
    elif perf_poor or ratings_very_weak:
        tier = "struggling"
        label = "Struggling"
        if perf_poor:
            reasons.append(f"Performing {performance_pct*100:+.0f}% vs level avg")
        if ratings_very_weak:
            reasons.append(f"OVR {ovr} well below {LEVEL_NAMES.get(level, '?')} level")
    else:
        return None  # Below average but not enough to flag

    reasons.append(f"Consider {LEVEL_NAMES.get(prev_lv, '?')} assignment")

    return {
        "tier": tier,
        "label": label,
        "reasons": reasons,
        "prev_level": LEVEL_NAMES.get(prev_lv, "?"),
        "performance_pct": performance_pct,
    }
