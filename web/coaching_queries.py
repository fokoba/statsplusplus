"""Coaching staff page — skills and personalities for a whole org.

Reads the `personnel` table (populated by scripts/local_ingest.py /
custom_upload.import_personnel_sync from a manually-exported OOTP "All
Personnel" CSV — there is no live StatsPlus API for this, it's local-file
only). Organizes every coach/executive across the org (MLB + every
affiliate) by role group, with the rating tier -> color mapping the
templates use to highlight strengths green and weaknesses orange/red.
"""

import os, sys
from collections import defaultdict

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "scripts"))
from web_league_context import get_db, get_cfg, mlb_team_ids, team_names_map

# OOTP's 8-tier rating scale -> the app's existing 5-bucket gr-pill color
# scale (elite/good/mid/poor/bad, already used for Farm Rankings/Power
# Rankings/etc — see web/static/style.css's "Generic bright green -> red
# tier scale" block). "Inexperienced"/"Unproven" (reputation-only values,
# no track record yet) read as neutral, same bucket as "OK"/"Average".
TIER_TO_GRADE = {
    "LEGENDARY": "elite", "Outstanding": "elite",
    "Excellent": "good", "Good": "good",
    "Average": "mid", "OK": "mid", "Inexperienced": "mid", "Unproven": "mid",
    "Fair": "poor",
    "Poor": "bad",
}


def tier_grade(value):
    """Rating tier text -> gr-pill-<grade> class suffix, or None (unrated/NA)."""
    if not value:
        return None
    return TIER_TO_GRADE.get(value)


# Ordinal ranking for sortable-table columns (higher = better).
TIER_SORT = {
    "Poor": 0, "Fair": 1, "OK": 2, "Average": 3, "Inexperienced": 3, "Unproven": 3,
    "Good": 4, "Excellent": 5, "Outstanding": 6, "LEGENDARY": 7,
}

# Per-job composite formula: primary_weight on `primary` field + the
# remaining weight split across whichever 1-2 fields in `secondary_pool`
# actually grade highest for THIS coach (not a fixed secondary field) — so
# a coach with a standout secondary skill (e.g. a Hitting Coach who also
# grades LEGENDARY on Teach Infield) gets real credit for it, matching the
# "secondary up to two categories... with two legendary ratings" idea.
# GM/Owner/Scouting Director don't have a clean single "skill" rating in
# this export (GM/Owner are almost entirely reputation + stylistic
# preference; Scouting Director's 4 scout categories are all comparably
# important), so those three use their own explicit formula instead of
# primary+secondary. These weights and pools are a judgment call, not
# something OOTP defines — flagged clearly so they're easy to tell me to
# adjust rather than treated as authoritative.
COMPOSITE_RULES = {
    "Manager": {"primary": "development", "secondary_pool": ["mechanics", "veteran_handling", "handle_running"]},
    "Bench Coach": {"primary": "handle_running", "secondary_pool": ["teach_running", "development"]},
    "Hitting Coach": {"primary": "teach_hitting", "secondary_pool": [
        "development", "mechanics", "teach_catching", "teach_infield", "teach_outfield", "teach_running"]},
    "Pitching Coach": {"primary": "teach_pitching", "secondary_pool": ["development", "mechanics"]},
    "First Base Coach": {"primary": "handle_running", "secondary_pool": ["teach_running"]},
    "Third Base Coach": {"primary": "handle_running", "secondary_pool": ["teach_running"]},
    "Team Trainer": {"primary": "fatigue_recovery", "secondary_pool": [
        "recover_legs", "recover_arms", "recover_back", "recover_other",
        "prevent_legs", "prevent_arms", "prevent_back", "prevent_other"]},
}
PRIMARY_WEIGHT = 0.6
SECONDARY_WEIGHT = 0.4

# Scouting Director: no single dominant category — MLB/amateur scouting is
# what most directly feeds prospect grading and the draft, so weighted
# higher than MiLB/international, but all four count.
SCOUT_WEIGHTS = {"scout_major": 0.40, "scout_amateur": 0.30, "scout_minor": 0.20, "scout_intl": 0.10}


def _tier_score(value):
    """Tier text -> 0-100 (LEGENDARY=100, Poor=0), or None if unrated."""
    s = TIER_SORT.get(value)
    return None if s is None else round(s / 7 * 100)


def compute_composite(d):
    """0-100 composite for one coach dict, or None if their job has no
    defined formula (Owner, General Manager) or no usable rating data."""
    job = d.get("job")
    if job == "Scouting Director":
        vals = [(d.get(f), w) for f, w in SCOUT_WEIGHTS.items()]
        scored = [(_tier_score(v), w) for v, w in vals if _tier_score(v) is not None]
        if not scored:
            return None
        total_w = sum(w for _, w in scored)
        return round(sum(s * w for s, w in scored) / total_w)

    rule = COMPOSITE_RULES.get(job)
    if not rule:
        return None
    primary_score = _tier_score(d.get(rule["primary"]))
    if primary_score is None:
        return None
    sec_scores = sorted(
        (s for s in (_tier_score(d.get(f)) for f in rule["secondary_pool"]) if s is not None),
        reverse=True,
    )[:2]
    if not sec_scores:
        return primary_score
    sec_avg = sum(sec_scores) / len(sec_scores)
    return round(primary_score * PRIMARY_WEIGHT + sec_avg * SECONDARY_WEIGHT)


LEVEL_ORDER = ["MLB", "AAA", "AA", "A", "A-Short", "Rookie", "Indy", "Intl"]

# Role -> which rating fields matter for that job, driving both which
# columns a role-group table shows and which fields get the tier coloring.
# (Every coach's DB row has every field populated regardless of role — OOTP
# generates a full profile for everyone, same as players having ratings for
# tools they'll never use — so this is purely about not cluttering the UI
# with irrelevant columns, not a data limitation.)
ROLE_GROUPS = {
    "Front Office": {
        "jobs": ("Owner", "General Manager"),
        "rating_cols": [
            ("scout_major", "Scout MLB"), ("scout_minor", "Scout MiLB"),
            ("scout_intl", "Scout Intl"), ("scout_amateur", "Scout Amateur"),
        ],
    },
    "Manager & Bench": {
        "jobs": ("Manager", "Bench Coach"),
        "rating_cols": [
            ("development", "Development"), ("mechanics", "Mechanics"),
            ("veteran_handling", "Vet Handling"), ("handle_running", "In-Game Running"),
        ],
    },
    "Hitting & Pitching Coaches": {
        "jobs": ("Hitting Coach", "Pitching Coach"),
        "rating_cols": [
            ("teach_hitting", "Teach Hitting"), ("teach_pitching", "Teach Pitching"),
            ("development", "Development"), ("mechanics", "Mechanics"),
        ],
    },
    "Base Coaches": {
        "jobs": ("First Base Coach", "Third Base Coach"),
        "rating_cols": [
            ("teach_running", "Teach Running"), ("handle_running", "In-Game Running"),
        ],
    },
    "Scouting": {
        "jobs": ("Scouting Director",),
        "rating_cols": [
            ("scout_major", "Scout MLB"), ("scout_minor", "Scout MiLB"),
            ("scout_intl", "Scout Intl"), ("scout_amateur", "Scout Amateur"),
        ],
    },
    "Trainers": {
        "jobs": ("Team Trainer",),
        "rating_cols": [
            ("fatigue_recovery", "Fatigue Recovery"),
            ("recover_legs", "Recover Legs"), ("recover_arms", "Recover Arms"),
            ("recover_back", "Recover Back"), ("recover_other", "Recover Other"),
            ("prevent_legs", "Prevent Legs"), ("prevent_arms", "Prevent Arms"),
            ("prevent_back", "Prevent Back"), ("prevent_other", "Prevent Other"),
        ],
    },
}


def _level_sort_key(level):
    try:
        return LEVEL_ORDER.index(level)
    except ValueError:
        return len(LEVEL_ORDER)


def composite_grade(composite):
    """0-100 composite -> gr-pill grade bucket, or None if uncomputed."""
    if composite is None:
        return None
    if composite >= 86:
        return "elite"
    if composite >= 64:
        return "good"
    if composite >= 43:
        return "mid"
    if composite >= 21:
        return "poor"
    return "bad"


def _annotate(d, team_names):
    d["team_name"] = team_names.get(d["team_id"], "?")
    d["reputation_grade"] = tier_grade(d.get("reputation"))
    d["reputation_sort"] = TIER_SORT.get(d.get("reputation"), -1)
    d["composite"] = compute_composite(d)
    d["composite_grade"] = composite_grade(d["composite"])
    return d


def get_all_team_names():
    """{team_id: name} for every real MLB team in this league (not the
    `teams` table's own foreign-league/all-star placeholder rows), for the
    compare-team picker dropdown."""
    tids = mlb_team_ids()
    names = team_names_map()
    return dict(sorted(((t, names.get(t, f"Team {t}")) for t in tids), key=lambda x: x[1]))


def get_role_league_ranking(job):
    """Every team's current holder of `job` (MLB level only — league-wide
    role comparison is about big-league staff, not every affiliate's),
    ranked by composite score descending. Coaches with no computable
    composite (Owner/GM, or missing ratings) sort last.

    Returns list of coach dicts (same shape as get_org_coaching's, plus
    `rank`).
    """
    conn = get_db()
    tids = mlb_team_ids()
    team_names = team_names_map()
    qs = ",".join("?" * len(tids)) if tids else "NULL"
    rows = conn.execute(
        f"SELECT * FROM personnel WHERE job=? AND level='MLB' AND team_id IN ({qs})",
        [job] + list(tids),
    ).fetchall()
    cols = [d[0] for d in conn.execute("SELECT * FROM personnel LIMIT 0").description]

    out = []
    for r in rows:
        d = dict(zip(cols, r))
        _annotate(d, team_names)
        out.append(d)

    out.sort(key=lambda d: (d["composite"] is None, -(d["composite"] or 0)))
    for i, d in enumerate(out):
        d["rank"] = i + 1 if d["composite"] is not None else None
    return out


def get_staff_chemistry(team_id):
    """Pairwise personality chemistry across the MLB-level pro staff only
    (not the whole org — chemistry is a clubhouse/staff-room dynamic, and
    affiliate staffs work in separate buildings).

    Each coach has a Type (their own personality), a "works well with"
    personality type, and a "struggles with" one. For every ordered pair
    (A, B) of pro-staff coaches where A != B: if B's Type matches A's
    "works well with" value, that's a buff (from A's side); if it matches
    A's "struggles with" value, that's a nerf. Summed across the whole
    staff — more buffs and fewer nerfs means a healthier coaching room,
    which is the whole point of surfacing this (per-coach detail lets you
    see WHO to target when reshuffling assignments or making a hire).

    Returns {"buffs": int, "nerfs": int, "net": int, "pairs": [
        {"a_name", "a_job", "b_name", "b_job", "kind": "buff"|"nerf"}, ...
    ], "per_coach": {coach_key: {"buffs": int, "nerfs": int}}}.
    """
    conn = get_db()
    staff = conn.execute(
        "SELECT coach_key, name, job, personality_type, personality_pos, personality_neg "
        "FROM personnel WHERE team_id=? AND level='MLB'",
        (team_id,),
    ).fetchall()

    pairs = []
    per_coach = {r[0]: {"buffs": 0, "nerfs": 0} for r in staff}
    for a_key, a_name, a_job, a_type, a_pos, a_neg in staff:
        if not a_type:
            continue
        for b_key, b_name, b_job, b_type, _, _ in staff:
            if b_key == a_key or not b_type:
                continue
            if a_pos and b_type == a_pos:
                pairs.append({"a_name": a_name, "a_job": a_job, "b_name": b_name,
                              "b_job": b_job, "kind": "buff"})
                per_coach[a_key]["buffs"] += 1
            elif a_neg and b_type == a_neg:
                pairs.append({"a_name": a_name, "a_job": a_job, "b_name": b_name,
                              "b_job": b_job, "kind": "nerf"})
                per_coach[a_key]["nerfs"] += 1

    buffs = sum(1 for p in pairs if p["kind"] == "buff")
    nerfs = sum(1 for p in pairs if p["kind"] == "nerf")
    return {"buffs": buffs, "nerfs": nerfs, "net": buffs - nerfs,
            "pairs": pairs, "per_coach": per_coach}


def get_league_chemistry():
    """get_staff_chemistry() for every MLB team in the league, so you can
    see where your own staff's buff/nerf balance actually stacks up rather
    than just knowing it's "positive" in isolation.

    Returns list of {team_id, team_name, buffs, nerfs, net} sorted by net
    descending.
    """
    tids = mlb_team_ids()
    names = team_names_map()
    out = []
    for t in tids:
        chem = get_staff_chemistry(t)
        out.append({
            "team_id": t, "team_name": names.get(t, f"Team {t}"),
            "buffs": chem["buffs"], "nerfs": chem["nerfs"], "net": chem["net"],
        })
    out.sort(key=lambda d: -d["net"])
    for i, d in enumerate(out):
        d["rank"] = i + 1
    return out


def get_org_coaching(team_id):
    """Every coach/executive across team_id's whole org (MLB + every
    affiliate), grouped by role group then level.

    Returns {"groups": {group_name: [coach_dict, ...]}, "n_total": int,
             "n_unrated_personality": int}. Each coach_dict has every
    `personnel` column plus `team_name`, `level_rank` (sort key), and a
    `grades` dict of {field: gr-pill grade} for that group's rating_cols.
    """
    conn = get_db()

    team_names = {r[0]: r[1] for r in conn.execute("SELECT team_id, name FROM teams").fetchall()}
    my_name = team_names.get(team_id, f"Team {team_id}")

    affiliate_ids = [r[0] for r in conn.execute(
        "SELECT team_id FROM teams WHERE parent_team_id=?", (team_id,)
    ).fetchall()]
    org_ids = set(affiliate_ids) | {team_id}

    qs = ",".join("?" * len(org_ids))
    rows = conn.execute(
        f"SELECT * FROM personnel WHERE team_id IN ({qs})", list(org_ids)
    ).fetchall()
    cols = [d[0] for d in conn.execute("SELECT * FROM personnel LIMIT 0").description]

    groups = {g: [] for g in ROLE_GROUPS}
    job_to_group = {}
    for gname, gdef in ROLE_GROUPS.items():
        for j in gdef["jobs"]:
            job_to_group[j] = gname

    for r in rows:
        d = dict(zip(cols, r))
        gname = job_to_group.get(d["job"])
        if not gname:
            continue
        d["level_rank"] = _level_sort_key(d.get("level"))
        rating_cols = ROLE_GROUPS[gname]["rating_cols"]
        d["grades"] = {field: tier_grade(d.get(field)) for field, _ in rating_cols}
        d["sorts"] = {field: TIER_SORT.get(d.get(field), -1) for field, _ in rating_cols}
        _annotate(d, team_names)
        groups[gname].append(d)

    for gname in groups:
        groups[gname].sort(key=lambda d: (d["level_rank"], d["team_name"], d["name"]))

    return {
        "team_id": team_id, "team_name": my_name,
        "groups": groups,
        "role_groups": ROLE_GROUPS,
        "n_total": sum(len(v) for v in groups.values()),
    }
