"""wOBA (weighted On-Base Average) — pure computation.

wOBA is a plate-production-only offensive metric: it run-weights each batting
event (walk, HBP, single, double, triple, HR) and, by construction, excludes
baserunning (SB/CS are handled separately via UBR/wSB). This makes it the
correct regression target for the *offensive* hitter tools (contact/gap/power/
eye), isolating offensive value from the defensive/baserunning/positional value
that total WAR bundles in.

OOTP exposes wOBA only at the team level, not per player. So we derive the
per-league/year linear weights from the run environment by scaling the
canonical FanGraphs weight *shape* so that our computed league wOBA matches
OOTP's stored league wOBA for that season (`woba_weights_from_run_env`), then
apply those weights per player (`player_woba`). When the anchor season is too
small (early in a league's life / an in-progress season) we fall back to the
canonical weights.

No DB access, no global state — callers pass in aggregated totals and the OOTP
league-wOBA anchor.

Public API:
    woba_weights_from_run_env(totals, ootp_league_woba, ...) -> (weights, source)
    player_woba(counts, weights) -> float | None
"""

from __future__ import annotations

from typing import Any, Optional

from statsplusplus.evaluation.constants import (
    CANONICAL_WOBA_WEIGHTS,
    WOBA_MIN_TEAM_SEASONS,
    WOBA_MIN_PA_PER_TEAM,
)


def _woba_denominator(ab: float, bb: float, ibb: float, sf: float, hbp: float) -> float:
    """Standard wOBA denominator: AB + BB - IBB + SF + HBP."""
    return (ab or 0) + (bb or 0) - (ibb or 0) + (sf or 0) + (hbp or 0)


def _event_counts(t: dict[str, Any]) -> dict[str, float]:
    """Extract wOBA event counts from a raw counting-stat dict.

    Expects keys: ab, h, d, t, hr, bb, ibb, hbp, sf (missing -> 0).
    Singles are derived as h - d - t - hr; unintentional walks as bb - ibb.
    """
    h = t.get("h", 0) or 0
    d = t.get("d", 0) or 0
    tr = t.get("t", 0) or 0
    hr = t.get("hr", 0) or 0
    bb = t.get("bb", 0) or 0
    ibb = t.get("ibb", 0) or 0
    return {
        "ubb": bb - ibb,
        "hbp": t.get("hbp", 0) or 0,
        "b1": h - d - tr - hr,
        "b2": d,
        "b3": tr,
        "hr": hr,
    }


def player_woba(counts: dict[str, Any], weights: dict[str, float]) -> Optional[float]:
    """Compute wOBA for a single player-season from raw counting stats.

    Args:
        counts: dict with keys ab, h, d, t, hr, bb, ibb, hbp, sf (missing -> 0).
        weights: linear weights dict (keys ubb, hbp, b1, b2, b3, hr).

    Returns:
        wOBA as a float, or None if the denominator is non-positive
        (no meaningful plate appearances).
    """
    denom = _woba_denominator(
        counts.get("ab", 0), counts.get("bb", 0), counts.get("ibb", 0),
        counts.get("sf", 0), counts.get("hbp", 0),
    )
    if denom <= 0:
        return None
    ev = _event_counts(counts)
    num = sum(weights[k] * ev[k] for k in weights)
    return num / denom


def woba_weights_from_run_env(
    totals: dict[str, Any],
    ootp_league_woba: Optional[float],
    n_team_seasons: int,
    total_pa: int,
    min_team_seasons: int = WOBA_MIN_TEAM_SEASONS,
    min_pa_per_team: int = WOBA_MIN_PA_PER_TEAM,
) -> tuple[dict[str, float], str]:
    """Derive per-league/year wOBA linear weights (Method A).

    Scales the canonical FanGraphs weight *shape* by a single factor so that
    league wOBA (computed from the league's own event totals) equals OOTP's
    stored league wOBA for the season. This adapts the weights to the league's
    run environment while staying numerically robust (one free parameter,
    anchored to OOTP's engine) and self-validating (per-player wOBA under the
    derived weights aggregates back to OOTP's league wOBA).

    Falls back to canonical weights when the anchor season is too small — the
    sample guard the caller supplies via n_team_seasons / total_pa.

    Args:
        totals: league event totals for the season, keys ab, h, d, t, hr, bb,
            ibb, hbp, sf.
        ootp_league_woba: OOTP's PA-weighted league wOBA for the season, the
            anchor. None -> fall back.
        n_team_seasons: number of teams with an OOTP wOBA for the season.
        total_pa: total plate appearances across those teams.
        min_team_seasons / min_pa_per_team: configurable sample guards.

    Returns:
        (weights, source) where source is "derived" or "canonical-fallback".
    """
    canonical = dict(CANONICAL_WOBA_WEIGHTS)

    # Sample guard: need a real anchor and a large-enough, complete-enough season.
    if ootp_league_woba is None or ootp_league_woba <= 0:
        return canonical, "canonical-fallback"
    if n_team_seasons < min_team_seasons:
        return canonical, "canonical-fallback"
    if n_team_seasons > 0 and (total_pa / n_team_seasons) < min_pa_per_team:
        return canonical, "canonical-fallback"

    denom = _woba_denominator(
        totals.get("ab", 0), totals.get("bb", 0), totals.get("ibb", 0),
        totals.get("sf", 0), totals.get("hbp", 0),
    )
    if denom <= 0:
        return canonical, "canonical-fallback"

    ev = _event_counts(totals)
    canon_league_woba = sum(canonical[k] * ev[k] for k in canonical) / denom
    if canon_league_woba <= 0:
        return canonical, "canonical-fallback"

    scale = ootp_league_woba / canon_league_woba
    derived = {k: round(v * scale, 4) for k, v in canonical.items()}
    return derived, "derived"
