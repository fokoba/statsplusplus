"""Home park fit — how well a player's skill profile matches this league's
home park factors.

Pure functions. No DB access — league_dir is only used to locate the static
park_factors.json config, never queried.

Public API:
    load_park_factors(league_dir) -> dict | None
    compute_batter_park_fit(tools, bats, weights, park) -> int
    compute_pitcher_park_fit_from_stats(gb_pct, k_pct, bb_pct, lg_gb_pct, lg_k_pct, lg_bb_pct, park) -> int
    compute_pitcher_park_fit_from_tools(tools, park) -> int
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional


def load_park_factors(league_dir: Path) -> Optional[dict]:
    """Load this league's static park_factors.json, or None if not configured."""
    path = Path(league_dir) / "config" / "park_factors.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def _clamp100(v: float) -> int:
    return int(max(-100, min(100, round(v))))


# ---------------------------------------------------------------------------
# Batters
# ---------------------------------------------------------------------------

def _batter_park_raw(
    tools: dict[str, float | int | None],
    bats: Optional[str],
    weights: dict[str, float],
    park: dict,
) -> Optional[tuple[float, float]]:
    """Shared setup for both batter park functions below: resolves
    handedness-split factors, matches them against available tools/weights,
    and returns (raw, max_possible) — see compute_batter_park_fit() for what
    each represents. None if there's not enough tool data to score.
    """
    if not park:
        return None

    bats = (bats or "").upper()
    if bats == "L":
        avg_pf, hr_pf = park.get("avg_l", park["avg"]), park.get("hr_l", park["hr"])
    elif bats == "R":
        avg_pf, hr_pf = park.get("avg_r", park["avg"]), park.get("hr_r", park["hr"])
    elif bats == "S":
        avg_pf = (park.get("avg_l", park["avg"]) + park.get("avg_r", park["avg"])) / 2
        hr_pf = (park.get("hr_l", park["hr"]) + park.get("hr_r", park["hr"])) / 2
    else:
        avg_pf, hr_pf = park["avg"], park["hr"]

    gap_pf = (park["doubles"] + park["triples"]) / 2

    categories = [
        ("contact", avg_pf),
        ("gap", gap_pf),
        ("power", hr_pf),
    ]
    available = [(tools.get(k), pf, weights.get(k, 0.0)) for k, pf in categories]
    available = [(v, pf, w) for v, pf, w in available if v is not None and w > 0]
    if not available:
        return None

    total_w = sum(w for _, _, w in available)
    raw = sum((v - 50.0) * (pf - 1.0) * (w / total_w) for v, pf, w in available)
    max_possible = sum(30.0 * abs(pf - 1.0) * (w / total_w) for _, pf, w in available)
    return raw, max_possible


def compute_batter_park_fit(
    tools: dict[str, float | int | None],
    bats: Optional[str],
    weights: dict[str, float],
    park: dict,
) -> Optional[int]:
    """-100 to 100: how well a batter's Contact/Gap/Power tools match this
    park's AVG/2B+3B/HR factors.

    AVG and HR factors are handedness-split in the source data (a lefty and
    a righty can see very different park effects in the same park) — uses
    the batter's own bats hand when known, averages the L/R splits for
    switch hitters, falls back to the park-wide figure otherwise.

    Eye (walks) has no matching category here — this league's park factor
    data has no walk factor, only AVG/HR/2B/3B/overall-runs, so forcing a
    walk-fit number would be inventing a signal that isn't in the data.
    Excluded rather than approximated.

    Args:
        tools: Hitter tool ratings on the 20-80 canonical scale (contact,
            gap, power, ...). Same dict already used for composite.
        bats: 'L', 'R', 'S' (switch), or None.
        weights: This bucket's hitter weight profile (contact/gap/power/...)
            — same weights already driving composite, so "how much this
            tool matters" stays consistent across the whole app.
        park: Park factors dict from load_park_factors().

    Returns:
        Integer -100..100, or None if there's not enough tool data to score.
        This is a RELATIVE/comparative score (100 = the best any player's
        profile could possibly do in this park) — not a dollar or % figure.
        See compute_batter_park_value_pct() for a quantified version.
    """
    result = _batter_park_raw(tools, bats, weights, park)
    if result is None:
        return None
    raw, max_possible = result
    if max_possible <= 0:
        return 0
    return _clamp100(100.0 * raw / max_possible)


def compute_batter_park_value_pct(
    tools: dict[str, float | int | None],
    bats: Optional[str],
    weights: dict[str, float],
    park: dict,
) -> Optional[float]:
    """Estimated marginal %% swing in this batter's total production from
    park fit, relative to a league-average hitter (tools at 50 in every
    category) — a quantified counterpart to compute_batter_park_fit()'s
    0-100 relative score, meant to convert directly into dollars.

    Same weighted category setup as compute_batter_park_fit() (so the two
    always agree in sign and roughly in ranking), but this one isn't
    normalized against a theoretical max-grade profile: dividing by 30 (the
    full range from average(50) to max(80) on the 20-80 scale) means a
    player who is fully maxed out and 100% weighted toward one category
    converges to that category's exact real park-factor deviation — e.g. a
    grade-80 pure power bat in a park with hr=1.10 nets +10.0% here, not an
    arbitrary comparative score. An average hitter (all tools at 50) nets
    0% — this measures the marginal park benefit of leaning into what the
    park rewards, not the park's absolute effect on a league-average player.

    Multiply the result by a projected value figure (e.g. Long-Term
    Surplus) for a rough dollar estimate. Deliberately kept out of surplus/
    bid figures themselves — this is a discovery signal on top of them, not
    a correction folded into the core valuation.
    """
    result = _batter_park_raw(tools, bats, weights, park)
    if result is None:
        return None
    raw, _ = result
    return raw / 30.0


# ---------------------------------------------------------------------------
# Pitchers
# ---------------------------------------------------------------------------

def compute_pitcher_park_fit_from_stats(
    gb_pct: float, k_pct: float, bb_pct: float,
    lg_gb_pct: float, lg_k_pct: float, lg_bb_pct: float,
    park: dict,
) -> Optional[int]:
    """-100 to 100, from real observed GB%/K%/BB% (preferred over scouting
    tools whenever a pitcher has a meaningful innings sample — see the
    BF/IP threshold in the caller).

    Two directional effects, plus a strikeout dampener:
      - Groundball tendency vs the park's HR factor: a groundball-heavy
        pitcher is specifically protected from a homer-friendly park (fly
        balls are what leave the yard, not grounders) — above-average GB%
        in an above-average HR park is a good fit.
      - Walk rate vs the park's overall run factor: more walks means more
        baserunners exposed to however much damage this park adds on balls
        in play — below-average BB% in a hitter-friendly park is a good
        fit; above-average BB% there is a bad one (this was the user's own
        stated intuition).
      - Strikeout rate doesn't get its own directional term — a strikeout
        bypasses the park entirely, so it doesn't specifically reward or
        punish a park's tendencies. Instead it dampens the other two terms:
        elite-K pitchers are less exposed to park effects generally (fewer
        balls in play means less at stake either way), so their score
        compresses toward neutral as K% rises above league average.
    """
    if not park:
        return None

    gb_dev = gb_pct - lg_gb_pct
    bb_dev = bb_pct - lg_bb_pct
    k_dev = k_pct - lg_k_pct

    hr_dev = park["hr"] - 1.0
    overall_dev = park["overall"] - 1.0

    gb_term = gb_dev * hr_dev
    bb_term = -bb_dev * overall_dev

    # Dampen up to 40% for a pitcher striking out well above league average;
    # no bonus for below-average K% (absence of whiffs doesn't add park
    # exposure beyond what GB/BB already capture).
    k_dampen = max(0.0, min(0.4, k_dev * 2.0))
    raw = (gb_term + bb_term) * (1.0 - k_dampen)

    # Scale so 100 = a pitcher one full standard "extreme" (20 points of
    # GB%/BB% above/below league average, expressed as a 0.20 fraction) in
    # both dimensions, fully aligned with this park's largest factor.
    max_possible = 0.20 * (abs(hr_dev) + abs(overall_dev))
    if max_possible <= 0:
        return 0
    return _clamp100(100.0 * raw / max_possible)


def compute_pitcher_park_fit_from_tools(
    tools: dict[str, float | int | None],
    park: dict,
) -> Optional[int]:
    """-100 to 100, scouting-tool fallback for pitchers with no real innings
    sample (a fresh upload, an amateur, a rookie) — Custom Upload always
    uses this path since an uploaded roster CSV has no game logs at all.

    Movement stands in for groundball tendency (this league's own model
    already ties higher Movement to more ground-ball contact), Control
    stands in for command/walk avoidance, matched against the same park
    factors compute_pitcher_park_fit_from_stats() uses. No Stuff/strikeout
    term — without a real K rate there's nothing to dampen with, so Stuff
    is left out rather than guessed at.
    """
    if not park:
        return None
    mov = tools.get("movement")
    ctrl = tools.get("control")
    if mov is None and ctrl is None:
        return None

    hr_dev = park["hr"] - 1.0
    overall_dev = park["overall"] - 1.0

    terms, max_terms = [], []
    if mov is not None:
        terms.append((mov - 50.0) * hr_dev)
        max_terms.append(30.0 * abs(hr_dev))
    if ctrl is not None:
        terms.append((ctrl - 50.0) * overall_dev)
        max_terms.append(30.0 * abs(overall_dev))

    raw = sum(terms) / len(terms)
    max_possible = sum(max_terms) / len(max_terms) if max_terms else 0
    if max_possible <= 0:
        return 0
    return _clamp100(100.0 * raw / max_possible)
