"""Percentile ranking for hitters and pitchers against the qualified pool."""

import os, sys, json

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "scripts"))
from web_league_context import get_db, get_cfg, has_extended_ratings


# ── year resolution helpers ──────────────────────────────────────────────

def _resolve_pctile_year(conn, year, table="batting_stats", fallback=True):
    """Find the best year for percentile calculation.

    If year is provided and has data, use it. If fallback=True (default),
    fall back to the most recent year with data when the requested year is
    empty (handles offseason). If fallback=False, return None when the
    requested year has no data.
    """
    split_filter = " AND split_id=1" if table in ("batting_stats", "pitching_stats") else ""
    if year is not None:
        has_data = conn.execute(
            f"SELECT 1 FROM {table} WHERE year=?{split_filter} LIMIT 1", (year,)
        ).fetchone()
        if has_data:
            return year
        if not fallback:
            return None
    # Fall back to most recent year with data
    row = conn.execute(
        f"SELECT MAX(year) FROM {table} WHERE 1=1{split_filter}"
    ).fetchone()
    return row[0] if row and row[0] else None


def available_pctile_years(pid, is_pitcher=False):
    """Return list of years (descending) where this player has stats."""
    conn = get_db()
    table = "pitching_stats" if is_pitcher else "batting_stats"
    rows = conn.execute(
        f"SELECT DISTINCT year FROM {table} WHERE player_id=? AND split_id=1 ORDER BY year DESC",
        (pid,)
    ).fetchall()
    conn.close()
    return [r[0] for r in rows]

# ── constants ────────────────────────────────────────────────────────────

HITTER_PCTILE_STATS = [
    ("avg", "AVG", False),
    ("obp", "OBP", False),
    ("slg", "SLG", False),
    ("ops", "OPS", False),
    ("iso", "ISO", False),
    ("bb_pct", "BB%", False),
    ("so_pct", "K%", True),
    ("babip", "BABIP", False),
    ("war", "WAR", False),
    ("war_rate", "WAR/600", False),
]

HITTER_TAG_MAP = {
    "AVG":  ("avg",    "cntct", False, False),
    "ISO":  ("iso",    "pow",   False, False),
    "BB%":  ("bb_pct", "eye",   False, False),
    "K%":   ("so_pct", "ks",    True,  False),
}

PITCHER_PCTILE_STATS = [
    ("era", "ERA", True),
    ("fip", "FIP", True),
    ("siera", "SIERA", True),
    ("era_plus", "ERA+", False),
    ("k9", "K/9", False),
    ("bb9", "BB/9", True),
    ("hr9", "HR/9", True),
    ("babip", "BABIP", True),
    ("war", "WAR", False),
    ("war_rate", "WAR/200", False),
]

PITCHER_TAG_MAP = {
    "K/9":  ("k9",  "stf",  False, False),
    "BB/9": ("bb9", "ctrl", True,  False),
}

# ── helpers ──────────────────────────────────────────────────────────────

def _tag_threshold(r_pctile):
    room = min(r_pctile, 100 - r_pctile)
    return max(15, min(25, round(room * 0.6)))

def _pctile(val, all_vals):
    below = sum(1 for v in all_vals if v < val)
    n = len(all_vals)
    return round(below / (n - 1) * 100) if n > 1 else 50


def _est_team_games(conn, year):
    """Estimate team games played from max team PA (≈38 PA/game)."""
    row = conn.execute(
        "SELECT MAX(pa) FROM team_batting_stats WHERE split_id=1 AND year=?", (year,)
    ).fetchone()
    return round((row[0] or 0) / 38) or 1


def _expected_range(expected, pa_or_ip, qualifier):
    """Half-width of expected range band, scaled by sample size.

    ±12 percentile points at the qualifier threshold, narrowing as
    sqrt(qualifier / sample).  Clamped so the band stays within 0-100.
    """
    if expected is None:
        return None
    import math
    half = 12 * math.sqrt(qualifier / max(pa_or_ip, 1))
    half = min(half, 25)  # cap so early-season doesn't blow out
    lo = max(0, round(expected - half))
    hi = min(100, round(expected + half))
    return (lo, hi)

# BABIP model: regression coefficients from 1311 player-seasons (2028-2032)
# BABIP ≈ 0.2276 + 0.001178 * cntct + 0.000228 * speed
_BABIP_B0, _BABIP_B1, _BABIP_B2 = 0.2276, 0.001178, 0.000228

def _babip_expected(pid, cntct, speed, conn, year, babip_rating=None):
    """Expected BABIP from ratings. Uses the BABIP rating directly when available,
    otherwise falls back to the contact+speed regression model."""
    if babip_rating is not None:
        # BABIP rating → expected stat-line BABIP.
        # League average BABIP ≈ .300, rating 50 = average on both scales.
        # On 1-100: each point ≈ 0.002 BABIP. On 20-80: each point ≈ 0.00333.
        from ratings import get_ratings_scale as _get_ratings_scale
        if _get_ratings_scale() == "20-80":
            model_babip = 0.200 + (babip_rating - 20) / 60 * 0.200
        else:
            model_babip = 0.200 + babip_rating * 0.002
    else:
        from ratings import get_ratings_scale as _get_ratings_scale
        c, s = cntct, speed
        if _get_ratings_scale() == "20-80":
            c = (cntct - 20) / 60 * 100
            s = (speed - 20) / 60 * 100
        model_babip = _BABIP_B0 + _BABIP_B1 * c + _BABIP_B2 * s
    # Historical residual: average (actual - model) over prior qualifying seasons
    rows = conn.execute("""
        SELECT b.h, b.hr, b.ab, b.k, b.sf, r.cntct, r.speed
        FROM batting_stats b JOIN latest_ratings r ON b.player_id = r.player_id
        WHERE b.player_id=? AND b.split_id=1 AND b.ab>=200 AND b.year<? AND b.year>=?
    """, (pid, year, year - 5)).fetchall()
    if len(rows) >= 2:
        resids = []
        for h, hr, ab, k, sf, rc, rs in rows:
            denom = ab - k - hr + (sf or 0)
            if denom <= 0:
                continue
            actual = (h - hr) / denom
            pred_c = (rc - 20) / 60 * 100 if _get_ratings_scale() == "20-80" else (rc or 0)
            pred_s = (rs - 20) / 60 * 100 if _get_ratings_scale() == "20-80" else (rs or 0)
            pred = _BABIP_B0 + _BABIP_B1 * pred_c + _BABIP_B2 * pred_s
            resids.append(actual - pred)
        if len(resids) >= 2:
            model_babip += sum(resids) / len(resids)
    return model_babip


# ── hitter percentiles ───────────────────────────────────────────────────

def get_hitter_percentiles(pid, split_id=1, year=None):
    conn = get_db()
    conn.row_factory = None
    current_year = get_cfg().year
    explicit_year = year is not None
    year = _resolve_pctile_year(conn, year or current_year, "batting_stats", fallback=not explicit_year)
    if year is None:
        conn.close()
        return None
    is_current = (year == current_year)
    games = _est_team_games(conn, year)
    min_pa = max(round(2.0 * games), 30) if split_id == 1 else 20

    # Use split-specific ratings for L/R expected percentiles
    if split_id == 2:    # vs L
        rcols = "r.cntct_l, r.pow_l, r.eye_l, r.ks_l"
    elif split_id == 3:  # vs R
        rcols = "r.cntct_r, r.pow_r, r.eye_r, r.ks_r"
    else:
        rcols = "r.cntct, r.pow, r.eye, r.ks"

    q = ("SELECT b.player_id, SUM(b.ab), SUM(b.h), SUM(b.d), SUM(b.t), SUM(b.hr), "
         "SUM(b.bb), SUM(b.k), SUM(b.pa), SUM(b.war), SUM(b.hbp), SUM(b.sf), "
         f"{rcols}, r.speed "
         "FROM batting_stats b JOIN latest_ratings r ON b.player_id=r.player_id "
         "WHERE b.year=? AND b.split_id=? "
         "GROUP BY b.player_id "
         "HAVING SUM(b.pa)>=?")
    rows = conn.execute(q, (year, split_id, min_pa)).fetchall()

    qualified = True
    player_row = None
    if pid not in {r[0] for r in rows}:
        player_row = conn.execute(
            q.replace("HAVING SUM(b.pa)>=?", "HAVING b.player_id=?"), (year, split_id, pid)
        ).fetchone()
        qualified = False

    # Compute expected BABIP before closing (needs conn for historical residual)
    _babip_col = ", babip" if has_extended_ratings() else ""
    _pc = conn.execute(
        f"SELECT cntct, speed{_babip_col} FROM ratings WHERE player_id=? ORDER BY snapshot_date DESC LIMIT 1",
        (pid,)).fetchone()
    exp_babip = _babip_expected(pid, _pc[0] or 0, _pc[1] or 0, conn, year, _pc[2] if len(_pc) > 2 else None) if _pc else None
    conn.close()

    if not rows:
        return None

    def _parse(r):
        _, ab, h, d, t, hr, bb, k, pa, war, hbp, sf, cntct, pw, eye, ks, speed = r
        hbp = hbp or 0; sf = sf or 0
        if not ab:
            return None, None, 0
        obp = (h + bb + hbp) / (ab + bb + hbp + sf) if (ab + bb + hbp + sf) else 0
        slg = (h + d + 2 * t + 3 * hr) / ab
        stats = {
            "avg": h / ab, "obp": obp, "slg": slg, "ops": obp + slg,
            "iso": slg - h / ab,
            "bb_pct": bb / pa * 100 if pa else 0,
            "so_pct": k / pa * 100 if pa else 0,
            "babip": (h - hr) / (ab - k - hr + sf) if (ab - k - hr + sf) > 0 else 0,
            "war": war or 0,
            "war_rate": (war or 0) * 600 / pa if pa else 0,
        }
        rats = {"cntct": cntct or 0, "pow": pw or 0, "eye": eye or 0, "ks": ks or 0, "speed": speed or 0}
        return stats, rats, pa

    pool, ratings_pool = {}, {}
    player_pa = 0
    for r in rows:
        s, rt, pa = _parse(r)
        if s:
            pool[r[0]] = s
            ratings_pool[r[0]] = rt
            if r[0] == pid:
                player_pa = pa

    if not qualified:
        if not player_row:
            return None
        s, rt, pa = _parse(player_row)
        if not s:
            return None
        pool[pid] = s
        ratings_pool[pid] = rt
        player_pa = pa

    if pid not in pool:
        return None

    player = pool[pid]
    player_rat = ratings_pool[pid]
    rat_vals = {k: [ratings_pool[p][k] for p in pool] for k in ("cntct", "pow", "eye", "ks")}
    cntct_pctile = _pctile(player_rat["cntct"], rat_vals["cntct"])

    result = []
    for key, label, inverted in HITTER_PCTILE_STATS:
        if key in ("war", "war_rate") and split_id != 1:
            continue
        val = player[key]
        pctile = _pctile(val, [pool[p][key] for p in pool])
        if inverted:
            pctile = 100 - pctile

        tag = None
        expected = None
        if qualified and is_current:
            if label in HITTER_TAG_MAP:
                sk, rk, s_inv, r_inv = HITTER_TAG_MAP[label]
                r_pctile = _pctile(player_rat[rk], rat_vals[rk])
                expected = r_pctile
                gap = pctile - r_pctile
                thresh = _tag_threshold(r_pctile)
                tag = "hot" if gap >= thresh else ("cold" if gap <= -thresh else None)
            elif label == "BABIP":
                if exp_babip is not None:
                    babip_vals = [pool[p]["babip"] for p in pool]
                    expected = _pctile(exp_babip, babip_vals)
                else:
                    expected = cntct_pctile
                gap = pctile - expected
                thresh = _tag_threshold(expected)
                tag = "lucky" if gap >= thresh else ("unlucky" if gap <= -thresh else None)

        result.append({"label": label, "value": val, "pctile": pctile, "tag": tag,
                       "qualified": qualified, "expected": expected,
                       "expected_range": _expected_range(expected, player_pa, min_pa),
                       "fmt": ".1f" if "pct" in key else (".1f" if key in ("war", "war_rate") else ".3f")})
    return result

# ── pitcher percentiles ──────────────────────────────────────────────────

def get_pitcher_percentiles(pid, split_id=1, year=None):
    conn = get_db()
    conn.row_factory = None
    current_year = get_cfg().year
    explicit_year = year is not None
    year = _resolve_pctile_year(conn, year or current_year, "pitching_stats", fallback=not explicit_year)
    if year is None:
        conn.close()
        return None
    is_current = (year == current_year)
    games = _est_team_games(conn, year)
    min_ip = max(round(0.7 * games), 5) if split_id == 1 else 5

    # RPs pitch far fewer innings — use a lower pool threshold so relievers
    # with a full workload aren't flagged as small sample.
    rp_min_ip = max(round(0.35 * games), 5) if split_id == 1 else 5

    from web_league_context import league_averages as _load_la
    lg_era = _load_la()["pitching"]["era"]

    tp = conn.execute(
        "SELECT SUM(era*ip)/SUM(ip), SUM(hra), SUM(bb), SUM(k), SUM(ip) "
        "FROM team_pitching_stats WHERE split_id=1"
    ).fetchone()
    fip_const = (tp[0] - ((13 * tp[1] + 3 * tp[2] - 2 * tp[3]) / tp[4])) if tp and tp[4] else 3.1

    # Use split-specific ratings for L/R expected percentiles
    _ext = has_extended_ratings()
    if split_id == 2:    # vs L batters
        rcols = "r.stf_l, r.mov_l, r.ctrl_l, r.ctrl_l" + (", r.hra_l, r.pbabip_l" if _ext else ", NULL, NULL")
    elif split_id == 3:  # vs R batters
        rcols = "r.stf_r, r.mov_r, r.ctrl_r, r.ctrl_r" + (", r.hra_r, r.pbabip_r" if _ext else ", NULL, NULL")
    else:
        rcols = "r.stf, r.mov, r.ctrl, r.ctrl" + (", r.hra, r.pbabip" if _ext else ", NULL, NULL")

    q = ("SELECT ps.player_id, SUM(ps.ip), SUM(ps.era*ps.ip)/NULLIF(SUM(ps.ip),0), "
         "SUM(ps.k), SUM(ps.bb), SUM(ps.ha), SUM(ps.war), SUM(ps.hra), SUM(ps.bf), SUM(ps.hp), "
         f"{rcols}, SUM(ps.gs), SUM(ps.g) "
         "FROM pitching_stats ps JOIN latest_ratings r ON ps.player_id=r.player_id "
         "WHERE ps.year=? AND ps.split_id=? "
         "GROUP BY ps.player_id "
         "HAVING SUM(ps.ip)>=?")
    rows = conn.execute(q, (year, split_id, rp_min_ip)).fetchall()

    # Detect if this player is an RP (few or no starts)
    player_gs_row = conn.execute(
        "SELECT SUM(gs), SUM(g) FROM pitching_stats WHERE player_id=? AND year=? AND split_id=?",
        (pid, year, split_id)).fetchone()
    is_rp = player_gs_row and player_gs_row[1] and player_gs_row[0] / player_gs_row[1] < 0.25

    qualified = True
    player_row = None
    is_in_pool = pid in {r[0] for r in rows}
    if not is_in_pool:
        player_row = conn.execute(
            q.replace("HAVING SUM(ps.ip)>=?", "HAVING ps.player_id=?"), (year, split_id, pid)
        ).fetchone()
        qualified = False
    elif not is_rp:
        # SP must meet the higher threshold to be qualified
        player_ip_check = next((r[1] for r in rows if r[0] == pid), 0)
        if player_ip_check < min_ip:
            qualified = False

    # Pre-fetch league-wide extended ratings before closing conn
    _ext_lgw = None
    conn.close()

    if not rows:
        return None

    def _parse(r):
        _, ip, era, k, bb, ha, war, hra, bf, hp, stf, mov, ctrl_r, ctrl_l, hra_rat, pbabip_rat, _gs, _g = r
        hra = hra or 0; bf = bf or 0; hp = hp or 0; ha = ha or 0
        if not ip:
            return None, None, 0
        k9 = k * 9 / ip
        bb9 = bb * 9 / ip
        hr9 = hra * 9 / ip
        fip = (13 * hra + 3 * (bb + hp) - 2 * k) / ip + fip_const
        era_plus = round(lg_era / era * 100) if era else 0
        babip_d = bf - k - hra - bb - hp
        babip = (ha - hra) / babip_d if babip_d > 0 else 0
        k_pct = k / bf if bf else 0
        bb_pct = bb / bf if bf else 0
        siera = (6.145 - 16.986 * k_pct + 11.434 * bb_pct
                 + 7.653 * k_pct**2 + 6.664 * bb_pct**2 + 0.9) if bf else 0
        stats = {
            "era": era or 99, "fip": fip, "siera": siera, "era_plus": era_plus,
            "k9": k9, "bb9": bb9, "hr9": hr9, "babip": babip, "war": war or 0,
            "war_rate": (war or 0) * 200 / ip if ip else 0,
        }
        ctrl = round(((ctrl_r or 0) + (ctrl_l or 0)) / 2)
        rats = {"stf": stf or 0, "mov": mov or 0, "ctrl": ctrl,
                "hra": hra_rat, "pbabip": pbabip_rat}
        return stats, rats, ip

    pool, ratings_pool = {}, {}
    player_ip = 0
    for r in rows:
        s, rt, ip = _parse(r)
        if s:
            pool[r[0]] = s
            ratings_pool[r[0]] = rt
            if r[0] == pid:
                player_ip = ip

    if not qualified and not is_in_pool:
        if not player_row:
            return None
        s, rt, ip = _parse(player_row)
        if not s:
            return None
        pool[pid] = s
        ratings_pool[pid] = rt
        player_ip = ip

    if pid not in pool:
        return None

    player = pool[pid]
    player_rat = ratings_pool[pid]
    rat_vals = {k: [ratings_pool[p][k] for p in pool] for k in ("stf", "mov", "ctrl")}
    # Extended ratings: use MLB pool for HRA (sufficient spread).
    # For BABIP, use a regression model (pbabip → expected BABIP) instead of
    # rating percentiles — the MLB pbabip distribution is too compressed for
    # percentile ranking to be meaningful.
    _has_hra = player_rat.get("hra") is not None
    _has_pbabip = player_rat.get("pbabip") is not None
    if _has_hra:
        rat_vals["hra"] = [ratings_pool[p].get("hra") or 0 for p in pool]
    if _has_pbabip:
        rat_vals["pbabip"] = [ratings_pool[p].get("pbabip") or 0 for p in pool]

    result = []
    _skip_splits = {"war", "war_rate", "era_plus", "siera"}
    for key, label, inverted in PITCHER_PCTILE_STATS:
        if key in _skip_splits and split_id != 1:
            continue
        val = player[key]
        pctile = _pctile(val, [pool[p][key] for p in pool])
        if inverted:
            pctile = 100 - pctile

        tag = None
        expected = None
        if qualified and is_current:
            if label == "HR/9":
                rk = "hra" if _has_hra else "mov"
                r_pctile = _pctile(player_rat.get(rk) or 0, rat_vals[rk])
                expected = r_pctile
                gap = pctile - r_pctile
                thresh = _tag_threshold(r_pctile)
                tag = "hot" if gap >= thresh else ("cold" if gap <= -thresh else None)
            elif label == "BABIP":
                # Pitcher BABIP expected: regression model from pbabip rating.
                # BABIP ≈ 0.439 - 0.0028 * pbabip (r=-0.18, from 362 qualifying seasons).
                # Rating percentiles don't work here — MLB pbabip distribution is too
                # compressed (stdev 3.3) for percentile ranking to be meaningful.
                if _has_pbabip:
                    exp_babip = 0.4387 - 0.002797 * (player_rat.get("pbabip") or 50)
                else:
                    exp_babip = 0.293  # league average fallback
                expected = _pctile(exp_babip, [pool[p]["babip"] for p in pool])
                expected = 100 - expected  # invert (lower BABIP = higher percentile)
                gap = pctile - expected
                thresh = _tag_threshold(expected)
                tag = "lucky" if gap >= thresh else ("unlucky" if gap <= -thresh else None)
            elif label in PITCHER_TAG_MAP:
                sk, rk, s_inv, r_inv = PITCHER_TAG_MAP[label]
                r_pctile = _pctile(player_rat[rk], rat_vals[rk])
                expected = r_pctile
                gap = pctile - r_pctile
                thresh = _tag_threshold(r_pctile)
                tag = "hot" if gap >= thresh else ("cold" if gap <= -thresh else None)

        fmt = "d" if key == "era_plus" else (".1f" if key in ("k9", "bb9", "hr9", "war", "war_rate") else ".2f" if key in ("era", "fip", "siera") else ".3f")
        result.append({"label": label, "value": val, "pctile": pctile, "tag": tag,
                       "qualified": qualified, "expected": expected,
                       "expected_range": _expected_range(expected, player_ip, rp_min_ip if is_rp else min_ip),
                       "fmt": fmt})
    return result


# ── fielding percentiles ─────────────────────────────────────────────────

_POS_NAMES = {2: "C", 3: "1B", 4: "2B", 5: "3B", 6: "SS", 7: "LF", 8: "CF", 9: "RF"}

def get_fielding_percentiles(pid, year=None):
    """Return fielding percentiles per position the player has played.

    Returns a list of dicts: [{pos, stats: [{label, value, pctile, ...}], qualified}]
    """
    conn = get_db()
    conn.row_factory = None
    current_year = get_cfg().year
    explicit_year = year is not None
    year = _resolve_pctile_year(conn, year or current_year, "fielding_stats", fallback=not explicit_year)
    if year is None:
        conn.close()
        return None
    games = _est_team_games(conn, year)
    min_ip = max(round(1.0 * games), 15)

    # Player's positions and ratings this year
    player_rows = conn.execute(
        "SELECT f.position, f.g, f.ip, f.tc, f.a, f.po, f.e, f.zr, f.framing, f.arm, "
        "       r.ifr, r.ofr, r.ife, r.ofe, r.c_arm, r.c_blk, r.c_frm, r.ifa "
        "FROM fielding_stats f "
        "JOIN latest_ratings r ON f.player_id = r.player_id "
        "WHERE f.player_id=? AND f.year=? AND f.position > 1",
        (pid, year)).fetchall()
    if not player_rows:
        conn.close()
        return None

    results = []
    for prow in player_rows:
        pos, g, ip, tc, a, po, e, zr, framing, arm, ifr, ofr, ife, ofe, c_arm, c_blk, c_frm, ifa = prow
        if g == 0:
            continue
        fpct = (po + a) / tc if tc else 0

        qualified = ip >= min_ip

        # Build pool with ratings for expected percentiles
        pool_rows = conn.execute(
            "SELECT f.player_id, f.ip, f.tc, f.a, f.po, f.e, f.zr, f.framing, f.arm, "
            "       r.ifr, r.ofr, r.ife, r.ofe, r.c_arm, r.c_blk, r.c_frm, r.ifa "
            "FROM fielding_stats f "
            "JOIN latest_ratings r ON f.player_id = r.player_id "
            "WHERE f.year=? AND f.position=? AND f.ip>=? "
            "GROUP BY f.player_id",
            (year, pos, min_ip)).fetchall()

        if not pool_rows:
            continue

        # Rating composites by position for ZR expected
        def _zr_composite(r_ifr, r_ofr, r_ife, r_ofe, r_c_arm, r_c_blk, r_ifa=0):
            if pos in (3, 4, 5, 6):  # infielders: range, error, arm
                return (r_ifr or 0) * 0.5 + (r_ife or 0) * 0.25 + (r_ifa or 0) * 0.25
            elif pos in (7, 8, 9):   # outfielders: range dominant
                return (r_ofr or 0)
            elif pos == 2:           # catchers: range + arm + blocking
                return ((r_ifr or 0) * 0.35 + (r_c_arm or 0) * 0.35 + (r_c_blk or 0) * 0.3)
            return 0

        def _framing_composite(r_c_frm, r_c_blk):
            return (r_c_frm or 0) * 0.7 + (r_c_blk or 0) * 0.3

        pool_fpct = [(r[3] + r[4]) / r[2] if r[2] else 0 for r in pool_rows]
        pool_zr = [r[6] for r in pool_rows if r[6] is not None]
        pool_zr_comp = [_zr_composite(r[9], r[10], r[11], r[12], r[13], r[14], r[16]) for r in pool_rows if r[6] is not None]
        pool_arm = [r[8] for r in pool_rows if r[8] is not None]
        pool_framing = [r[7] for r in pool_rows if r[7] is not None]
        pool_frm_comp = [_framing_composite(r[15], r[14]) for r in pool_rows if r[7] is not None]

        player_zr_comp = _zr_composite(ifr, ofr, ife, ofe, c_arm, c_blk, ifa)
        player_frm_comp = _framing_composite(c_frm, c_blk)

        def _stat_entry(label, value, pool_vals, fmt, rating_comp=None, pool_comps=None):
            if not pool_vals:
                return None
            all_vals = pool_vals + ([value] if not qualified else [])
            all_comps = (pool_comps or []) + ([rating_comp] if not qualified and pool_comps is not None else [])
            pctile = _pctile(value, all_vals)
            expected = None
            exp_range = None
            if qualified and rating_comp is not None and pool_comps:
                expected = _pctile(rating_comp, all_comps)
                exp_range = _expected_range(expected, ip, min_ip)
            return {"label": label, "value": value, "pctile": pctile, "fmt": fmt,
                    "qualified": qualified, "tag": None, "expected": expected, "expected_range": exp_range}

        stats = [_stat_entry("FPCT", fpct, pool_fpct, ".3f")]
        zr_entry = _stat_entry("ZR", zr or 0, pool_zr, ".1f", player_zr_comp, pool_zr_comp)
        if zr_entry:
            stats.append(zr_entry)
        if pos in (7, 8, 9):
            arm_entry = _stat_entry("Arm", arm or 0, pool_arm, ".1f")
            if arm_entry:
                stats.append(arm_entry)
        if pos == 2:
            frm_entry = _stat_entry("Framing", framing or 0, pool_framing, ".1f", player_frm_comp, pool_frm_comp)
            if frm_entry:
                stats.append(frm_entry)

        stats = [s for s in stats if s is not None]
        if stats:
            results.append({"pos": _POS_NAMES.get(pos, str(pos)), "stats": stats, "qualified": qualified})

    conn.close()
    return results if results else None


# ── multi-year percentile history ────────────────────────────────────────

def get_percentile_history(pid, is_pitcher=False, split_id=1):
    """Compute percentile rankings for all available years.

    Returns dict with:
      - years: [list of years descending]
      - stats: [{label, fmt, values: {year: {value, pctile, qualified}}}]
      - sample_sizes: {year: pa_or_ip}
      - sample_label: "PA" or "IP"
    """
    years = available_pctile_years(pid, is_pitcher=is_pitcher)
    if not years:
        return None

    # Get sample sizes per year
    conn = get_db()
    sample_sizes = {}
    if is_pitcher:
        for row in conn.execute(
            "SELECT year, SUM(ip) FROM pitching_stats WHERE player_id=? AND split_id=? GROUP BY year",
            (pid, split_id)).fetchall():
            sample_sizes[row[0]] = round(row[1], 1) if row[1] else 0
    else:
        for row in conn.execute(
            "SELECT year, SUM(pa) FROM batting_stats WHERE player_id=? AND split_id=? GROUP BY year",
            (pid, split_id)).fetchall():
            sample_sizes[row[0]] = row[1] or 0
    conn.close()

    stat_defs = PITCHER_PCTILE_STATS if is_pitcher else HITTER_PCTILE_STATS
    # Only include overall stats (skip splits-only stats)
    stat_defs = [(k, l, inv) for k, l, inv in stat_defs]

    history = {label: {} for _, label, _ in stat_defs}

    for yr in years:
        if is_pitcher:
            result = get_pitcher_percentiles(pid, split_id=split_id, year=yr)
        else:
            result = get_hitter_percentiles(pid, split_id=split_id, year=yr)
        if not result:
            continue
        for entry in result:
            label = entry["label"]
            if label in history:
                history[label][yr] = {
                    "value": entry["value"],
                    "pctile": entry["pctile"],
                    "fmt": entry["fmt"],
                    "qualified": entry["qualified"],
                }

    # Build structured output
    stats = []
    for key, label, inverted in stat_defs:
        if not history.get(label):
            continue
        # Get fmt from any year's data
        sample = next(iter(history[label].values()))
        # Compute PA-weighted career averages
        weighted_sum = 0.0
        weight_total = 0
        pctile_sum = 0
        n_qualified = 0
        for yr, entry in history[label].items():
            if entry.get("qualified"):
                w = sample_sizes.get(yr, 1) or 1
                weighted_sum += entry["value"] * w
                weight_total += w
                pctile_sum += entry["pctile"]
                n_qualified += 1
        career = None
        if n_qualified > 0:
            career = {
                "avg_value": weighted_sum / weight_total if weight_total else 0,
                "avg_pctile": round(pctile_sum / n_qualified),
                "fmt": sample["fmt"],
                "n_seasons": n_qualified,
            }
        stats.append({
            "label": label,
            "fmt": sample["fmt"],
            "values": history[label],
            "career": career,
        })

    # Filter to years that actually have data
    years_with_data = [yr for yr in years if any(
        yr in s["values"] for s in stats
    )]

    if not stats:
        return None

    return {
        "years": years_with_data,
        "stats": stats,
        "sample_sizes": sample_sizes,
        "sample_label": "IP" if is_pitcher else "PA",
    }


def get_fielding_percentile_history(pid):
    """Compute fielding percentile rankings for all available years.

    Returns dict with:
      - years: [list of years descending]
      - positions: [{pos, stats: [{label, values: {year: {value, pctile, qualified}}}]}]
    """
    conn = get_db()
    # Get years this player has fielding data
    years = [r[0] for r in conn.execute(
        "SELECT DISTINCT year FROM fielding_stats WHERE player_id=? ORDER BY year DESC",
        (pid,)).fetchall()]
    conn.close()
    if not years:
        return None

    # Collect data per year
    all_results = {}
    for yr in years:
        result = get_fielding_percentiles(pid, year=yr)
        if result:
            all_results[yr] = result

    if not all_results:
        return None

    # Determine all positions the player has played
    all_positions = []
    seen_pos = set()
    for yr, positions in all_results.items():
        for fp in positions:
            if fp["pos"] not in seen_pos:
                seen_pos.add(fp["pos"])
                all_positions.append(fp["pos"])

    # Build per-position history
    positions = []
    for pos in all_positions:
        # Collect stat labels from this position
        stat_labels = []
        seen_labels = set()
        for yr, yr_positions in all_results.items():
            for fp in yr_positions:
                if fp["pos"] == pos:
                    for s in fp["stats"]:
                        if s["label"] not in seen_labels:
                            seen_labels.add(s["label"])
                            stat_labels.append({"label": s["label"], "fmt": s["fmt"]})

        # Collect values per year
        for stat in stat_labels:
            stat["values"] = {}
            for yr, yr_positions in all_results.items():
                for fp in yr_positions:
                    if fp["pos"] == pos:
                        for s in fp["stats"]:
                            if s["label"] == stat["label"]:
                                stat["values"][yr] = {
                                    "value": s["value"],
                                    "pctile": s["pctile"],
                                    "qualified": s["qualified"],
                                    "fmt": s["fmt"],
                                }

        if stat_labels:
            positions.append({"pos": pos, "stats": stat_labels})

    years_with_data = [yr for yr in years if yr in all_results]
    return {"years": years_with_data, "positions": positions} if positions else None
