"""Prospect development-speed metric — pure computation.

Describes observed raw-tool movement over a trailing window, relative to
same-role / same-age peers. This is not a validated failure forecast. Raw
batting/pitching tool means keep both pace and potential-gap arithmetic on one
fixed scale, independent of positional changes or evaluation-model revisions.
Legacy output column names are retained for database compatibility.

Design + validation: `.kiro/specs/development-speed-metric/design.md`.

This module is pure: it operates on pre-loaded snapshot rows and a pre-built
baseline. The DB loading, bucketing (via ``assign_bucket``), and persistence live
in the data pipeline (``fv_calc``).

Public API:
    component_delta(snaps, key) -> (annual_delta|None, first, last)
    build_baseline(records) -> {(bucket, band): (mean, sd, n)}
    dev_speed_z(annual_move, bucket, band, baseline) -> (z|None, group|None)
    classify(z, gap, signal) -> (label, css_class, note)
    confidence_tier(window_years, n_snaps, acc, playing_time, is_pitcher) -> str
    age_band(age) -> str
    compute_dev_speed(...) -> dict   # the top-level per-player result
"""

from __future__ import annotations

import statistics as _st
from datetime import date
from typing import Any, Optional

from statsplusplus.evaluation.constants import PEAK_AGE_HITTER, PEAK_AGE_PITCHER

# --- Descriptive reporting choices; not calibrated failure thresholds ---
WINDOW_MONTHS = 12          # trailing window for "current" dev pace
MIN_SNAPSHOTS = 3           # minimum points in the window to compute
MIN_WINDOW_DAYS = 150       # ~5 months minimum span (also the reporting gate)
MIN_GROUP_N = 20            # minimum peers to form a baseline cell
AGE_BAND_WIDTH = 2
MAX_BASELINE_AGE = 25       # prospect age ceiling for the baseline population
WIDE_GAP = 12               # POT-gap threshold for the "upside" qualifier

# Young players can show tool changes, but not a firm pace assessment.
MIN_AGE_REPORT = 18
HITTER_TOOLS = ("cntct", "gap", "pow", "eye")
PITCHER_TOOLS = ("stf", "mov", "ctrl")
STAGNANT_EPS = 1.0
GAP_SMOOTH_POINTS = 3
# Compatibility constants used only by the retired legacy peer-cut helper.
BEHIND_GAP_CLOSED_PCT = .05
GAP_NOT_CLOSING = -.5


DRIFT_MIN_N = 15


def neutralize_scale_drift(by_player: dict, group_of: dict, keys=("composite_score", "ceiling_score", "offensive_grade")) -> None:
    """Legacy compatibility helper; no longer used by DEV.

    Put composite history on the latest model scale, in place.

    ratings_history is not on one scale: when the evaluation model changes, or
    the engine re-scores only a player's newest row, composites step by several
    points for everybody at once while the underlying tools haven't moved. Raw
    composite deltas then read as development (or decline) that never happened
    and poison gap-closure / gap-trend. A step is measured, per dev group and
    per snapshot, as the median change in `key` among players whose tools were
    IDENTICAL between consecutive snapshots (their true change is ~0), then
    accumulated and subtracted so older rows are expressed on today's scale.
    """
    from collections import defaultdict
    tool_keys = ("cntct", "gap", "pow", "eye", "stf", "mov", "ctrl")
    for key in keys:
        steps = defaultdict(list)   # (group, date_next) -> [delta among unchanged-tool players]
        for pid, snaps in by_player.items():
            g = group_of.get(pid)
            for a, b in zip(snaps, snaps[1:]):
                if a.get(key) is None or b.get(key) is None:
                    continue
                if all(a.get(t) == b.get(t) for t in tool_keys):
                    steps[(g, b["snapshot_date"])].append(b[key] - a[key])
        by_group_dates = defaultdict(dict)
        for (g, d), v in steps.items():
            if len(v) >= DRIFT_MIN_N:
                by_group_dates[g][d] = _st.median(v)
        for g, dd in by_group_dates.items():
            dates = sorted(dd)
            cum, run = {}, 0.0
            for d in dates:
                run += dd[d]
                cum[d] = run
            total = run
            def cum_at(d):
                c = 0.0
                for dt in dates:
                    if dt <= d:
                        c = cum[dt]
                    else:
                        break
                return c
            for pid, snaps in by_player.items():
                if group_of.get(pid) != g:
                    continue
                for sn in snaps:
                    if sn.get(key) is not None:
                        sn[key] = sn[key] + (total - cum_at(sn["snapshot_date"]))


def peer_gap_cuts(results: list[dict]) -> dict:
    """Retired legacy peer cuts, retained for old analysis clients only.

    {(dev_group, age_band): {behind_closed_cut, not_closing_cut}} from the
    population itself. The absolute gap-closure thresholds (5%/yr, trend >= -0.5)
    were calibrated on a scale-contaminated history and flagged ~half of ALL
    prospects; "behind" should mean slower than same-group, same-age peers:
    gap closure in the bottom quartile, "not closing" = gap trend in the top
    quartile (widening fastest). Cells under MIN_GROUP_N fall back to the
    absolute defaults.
    """
    from collections import defaultdict
    closed, trend = defaultdict(list), defaultdict(list)
    for r in results:
        k = (dev_group(r["bucket"]), r["band"])
        if r.get("gap_closed_pct_yr") is not None:
            closed[k].append(r["gap_closed_pct_yr"])
        if r.get("gap_trend") is not None:
            trend[k].append(r["gap_trend"])
    def q(v, f):
        v = sorted(v); return v[min(len(v) - 1, int(f * len(v)))]
    out = {}
    for k in set(closed) | set(trend):
        cut = {}
        if len(closed[k]) >= MIN_GROUP_N:
            cut["behind_closed_cut"] = min(BEHIND_GAP_CLOSED_PCT, q(closed[k], 0.25))
        if len(trend[k]) >= MIN_GROUP_N:
            cut["not_closing_cut"] = max(GAP_NOT_CLOSING, q(trend[k], 0.75))
        out[k] = cut
    return out


def age_band(age: int) -> str:
    lo = (int(age) // AGE_BAND_WIDTH) * AGE_BAND_WIDTH
    return f"{lo}-{lo + AGE_BAND_WIDTH - 1}"


def _years_between(d1: str, d2: str) -> float:
    y1, m1, day1 = map(int, d1.split("-"))
    y2, m2, day2 = map(int, d2.split("-"))
    return (date(y2, m2, day2) - date(y1, m1, day1)).days / 365.25


def component_delta(snaps: list[dict], key: str) -> tuple[Optional[float], Optional[float], Optional[float]]:
    """Annualized delta of a component over the sub-window where it's populated.

    Components (offensive_grade, defensive_value) are only written on snapshots
    where the eval engine ran, which can start later than the composite history.
    Returns (annual_delta or None, first_val, last_val).
    """
    pts = [s for s in snaps if s.get(key) is not None and s[key] > 0]
    if len(pts) < 2:
        return None, (pts[0][key] if pts else None), (pts[-1][key] if pts else None)
    f, l = pts[0], pts[-1]
    yrs = _years_between(f["snapshot_date"], l["snapshot_date"])
    if yrs * 365.25 < MIN_WINDOW_DAYS:
        return None, f[key], l[key]
    return (l[key] - f[key]) / yrs, f[key], l[key]


def dev_group(bucket: str) -> str:
    """Coarser grouping for the dev-speed *baseline*.

    Offensive development rate is essentially position-independent for hitters
    (validated: 1B/2B/3B/SS/CF/COF offensive-dev means cluster within ~0.5/yr),
    so we do NOT slice hitters by fielding position — that only thins the sample.
    The one meaningful exception is catcher, whose bat develops slower (workload/
    defensive-demand drag). Pitchers keep the SP/RP split (real, validated).

    Groups: "SP", "RP", "C", "HIT" (all non-catcher hitters).
    The player's true position bucket is still used for display, not the baseline.
    """
    if bucket in ("SP", "RP"):
        return bucket
    if bucket == "C":
        return "C"
    return "HIT"


def build_baseline(records: list[dict]) -> dict[str, dict[tuple, tuple]]:
    """Per (dev_group, age-band) mean/SD for each dev signal.

    Returns {signal: {(group, band): (mean, sd, n)}} for signal in {"comp","off"}
    — annualized movement of composite / offensive grade. `group` is the coarse
    dev_group (SP/RP/C/HIT), not the fine fielding bucket — see dev_group().
    A (group, "ALL") all-ages fallback cell is also emitted for tiny leagues.
    """
    field = {"comp": "annual_comp", "off": "annual_off"}
    out: dict[str, dict[tuple, tuple]] = {}
    from collections import defaultdict
    for sig, fld in field.items():
        fine: dict[tuple, list] = defaultdict(list)
        grp_all: dict[str, list] = defaultdict(list)
        for r in records:
            if r["age"] <= MAX_BASELINE_AGE and r.get(fld) is not None:
                g = dev_group(r["bucket"])
                fine[(g, r["band"])].append(r[fld])
                grp_all[g].append(r[fld])
        b = {}
        for key, vals in fine.items():
            if len(vals) >= MIN_GROUP_N:
                b[key] = (round(_st.mean(vals), 3), round(_st.pstdev(vals) or 1.0, 3), len(vals))
        for g, vals in grp_all.items():
            if len(vals) >= MIN_GROUP_N:
                b[(g, "ALL")] = (round(_st.mean(vals), 3), round(_st.pstdev(vals) or 1.0, 3), len(vals))
        out[sig] = b
    return out


def primary_signal(bucket: str, has_off: bool) -> str:
    """Legacy signal names: off = batting tools, comp = pitching tools."""
    is_hitter = bucket not in ("SP", "RP")
    return "off" if (is_hitter and has_off) else "comp"


def dev_speed_z(annual_move: Optional[float], bucket: str, band: str,
                sig: str, baseline: dict) -> tuple[Optional[float], Optional[tuple]]:
    if annual_move is None:
        return None, None
    b = baseline.get(sig, {})
    g = dev_group(bucket)
    # (group, band) → (group, all ages) fallback for tiny leagues
    for key in ((g, band), (g, "ALL")):
        if key in b:
            mean, sd, n = b[key]
            if sd <= 0:
                return None, (mean, sd, n)
            return round((annual_move - mean) / sd, 2), (mean, sd, n)
    return None, None


def classify(z: Optional[float], gap: float, sig: str) -> tuple[str, str, str]:
    """Relative pace only; below-peer growth does not imply failure."""
    if z is None:
        return "Limited evidence", "none", "Not enough comparable peers."
    if z >= 1:
        return "Faster pace", "rising", "Observed tool growth exceeds the peer mean."
    if z <= -0.5:
        return "Slower pace", "onpace", "Observed tool growth is below the peer mean; future outcome is unknown."
    return "Similar pace", "onpace", "Observed tool growth is near the peer mean."


def confidence_tier(window_years: float, n_snaps: int, acc: Optional[str],
                    playing_time: float, is_pitcher: bool) -> str:
    """Confidence in observed ratings, not certainty about future outcomes.

    Playing time is deliberately unused: a two-season PA sum is not evidence
    about the reliability of a dated raw-rating change.
    """
    a = (acc or "").upper()
    if window_years >= .9 and n_snaps >= 6 and a in ("VH", "H"):
        return "High"
    if window_years >= .6 and n_snaps >= 4 and a in ("VH", "H", "A", "M"):
        return "Medium"
    return "Low"


def prepare_tool_window(window: list[dict], bucket: str, ratings_scale="20-80") -> list[dict]:
    """One fixed tool basis for current ability, potential, and both endpoints.

    Movement is the aggregate Movement grade; HRA/PBABIP are not double-counted.
    Missing tools never become zeros or phantom gains.
    """
    from statsplusplus.config.ratings import norm_continuous
    keys = PITCHER_TOOLS if bucket in ("SP", "RP") else HITTER_TOOLS
    result = []
    for original in sorted(window, key=lambda r: r["snapshot_date"]):
        r = dict(original)
        if "_dev_current" in r:
            result.append(r)
            continue
        vals = [norm_continuous(r.get(k), ratings_scale) for k in keys]
        pots = [norm_continuous(r.get("pot_" + k), ratings_scale) for k in keys]
        if any(v is None for v in vals):
            continue
        for k, v in zip(keys, vals):
            r[k] = v
        for k, v in zip(keys, pots):
            r["pot_" + k] = v
        r["_dev_current"] = _st.mean(vals)
        r["_dev_potential"] = _st.mean(pots) if all(v is not None for v in pots) else None
        r["_dev_gap"] = (max(0, r["_dev_potential"] - r["_dev_current"])
                         if r["_dev_potential"] is not None else None)
        result.append(r)
    return result


def stagnant_tools(window: list[dict], bucket: str) -> list[str]:
    """Tool keys (from HITTER_TOOLS or PITCHER_TOOLS) whose annualized
    movement across the window is within STAGNANT_EPS of zero.

    A tool-blended signal like offensive_grade can rise on one hot tool
    while others sit completely flat — the aggregate reads as broad-based
    progress when it isn't. Ignores tools without 2+ populated points in
    the window (no data ≠ stagnant); requires the same MIN_WINDOW_DAYS span
    component_delta() already enforces.
    """
    tools = PITCHER_TOOLS if bucket in ("SP", "RP") else HITTER_TOOLS
    out = []
    for t in tools:
        delta, first, last = component_delta(window, t)
        if delta is not None and abs(delta) < STAGNANT_EPS:
            out.append(t)
    return out


def _gap_series(window: list[dict]) -> list[tuple[str, float]]:
    out = []
    for r in window:
        if "_dev_gap" in r:
            gap = r["_dev_gap"]
        else:
            cur, ceiling = r.get("composite_score"), r.get("ceiling_score")
            gap = max(0, ceiling-cur) if cur is not None and ceiling is not None else None
        if gap is not None:
            out.append((r["snapshot_date"], gap))
    return out


def gap_smoothed_and_trend(window: list[dict]) -> tuple[Optional[float], Optional[float]]:
    """Non-overlapping endpoint averages timed at their mean dates."""
    series = _gap_series(window)
    if len(series) < 2:
        return None, None
    n = min(GAP_SMOOTH_POINTS, len(series)//2)
    first, last = series[:n], series[-n:]
    first_date = _st.mean(date.fromisoformat(d).toordinal() for d, _ in first)
    last_date = _st.mean(date.fromisoformat(d).toordinal() for d, _ in last)
    years = (last_date-first_date)/365.25
    last_mean = _st.mean(v for _, v in last)
    trend = (last_mean-_st.mean(v for _, v in first))/years if years > 0 else None
    return round(last_mean, 2), round(trend, 2) if trend is not None else None


def schedule_tag(
    *, z=None, gap=None, gap_first=None, gap_closed_pct_yr=None,
    gap_trend=None, stagnant=(), age=None, bucket=None, confidence="Low",
    improving=(), declining=(), early=False, **unused,
):
    """Observed changes only. No unvalidated schedule or failure verdicts."""
    if early:
        return "early", "Early view", "Too little established history for a development assessment."
    if confidence == "Low":
        return "limited", "Limited evidence", "The observed changes are tentative; more reliable history is needed."
    if declining and improving:
        return "mixed", "Mixed changes", "Some tools improved and others declined."
    if declining:
        return "declining", "Declines observed", "Some recorded tool grades declined over this window; this is not a future-failure forecast."
    if improving:
        if z is not None and z >= 1 and confidence == "High":
            return "ahead", "Faster gains", "Observed tool gains exceed the peer mean; future development is not guaranteed."
        return "improving", "Gains observed", "One or more tools improved; flat tools do not negate those gains."
    return "unchanged", "Little change", "No material core-tool movement was recorded in this window; future development remains uncertain."


def compute_dev_speed(
    *, bucket, age, window, baseline, acc=None, playing_time=0,
    peak_age_hitter=PEAK_AGE_HITTER, peak_age_pitcher=PEAK_AGE_PITCHER,
    rookie=False, peer_cuts=None, ratings_scale="20-80",
) -> Optional[dict[str, Any]]:
    """Describe raw-tool changes, with evidence-gated labels and real dates.

    comp_first/last, off_first/last and d_ovr/d_pot are legacy storage names;
    here they contain mean core-tool values and changes, never model composite.
    """
    window = prepare_tool_window(window, bucket, ratings_scale)
    if not window:
        return None
    first, last = window[0], window[-1]
    yrs = _years_between(first["snapshot_date"], last["snapshot_date"])
    is_pitcher = bucket in ("SP", "RP")
    tools = PITCHER_TOOLS if is_pitcher else HITTER_TOOLS
    sig = "comp" if is_pitcher else "off"
    gain = last["_dev_current"]-first["_dev_current"]
    annual = gain/yrs if yrs*365.25 >= MIN_WINDOW_DAYS else None
    z, grp = dev_speed_z(annual, bucket, age_band(age), sig, baseline)
    # Many daily reports in one month do not substitute for longitudinal coverage.
    observed_months = len({r["snapshot_date"][:7] for r in window})
    conf = confidence_tier(yrs, observed_months, acc, playing_time, is_pitcher)
    early = age < MIN_AGE_REPORT or rookie or yrs < .6 or len(window) < 4
    changes = {k: last[k]-first[k] for k in tools}
    stagnant = [k for k,v in changes.items() if abs(v) < STAGNANT_EPS]
    improving = [k for k,v in changes.items() if v >= STAGNANT_EPS]
    declining = [k for k,v in changes.items() if v <= -STAGNANT_EPS]
    status, label, summary = schedule_tag(z=z, confidence=conf, improving=improving,
                                        declining=declining, early=early)
    available = (not early and conf != "Low" and z is not None)
    css = ("rising" if status == "ahead" else "regressing" if status == "declining" else "onpace") if available else "none"
    gap_first, gap_last = first["_dev_gap"], last["_dev_gap"]
    closure = gain/gap_first/yrs if gap_first is not None and gap_first > 0 and yrs > 0 else None
    pot_first, pot_last = first["_dev_potential"], last["_dev_potential"]
    d_pot = pot_last-pot_first if pot_first is not None and pot_last is not None else None
    smooth, trend = gap_smoothed_and_trend(window)
    names = {"cntct":"Contact", "gap":"Gap", "pow":"Power", "eye":"Eye",
             "stf":"Stuff", "mov":"Movement", "ctrl":"Control"}
    detail = "; ".join(f"{names[k]} {v:+g}" for k,v in changes.items())
    basis = "pitching" if is_pitcher else "batting"
    note = (f"{summary} {basis.title()} tools, {first['snapshot_date']} to {last['snapshot_date']}: "
            f"{detail}. Changes use normalized 20–80 grades. Historical observation, not a bust probability.")
    if grp is None:
        note += " A comparable peer baseline is not available."
    if d_pot and d_pot > 0:
        note += " Potential increased too; a wider potential gap is not itself a setback."
    return {
        "available":available,"z":z,"signal":sig,"label":label,"css_class":css,"note":note,
        "gap":round(gap_last,2) if gap_last is not None else None,
        "d_ovr":round(gain,2),"d_pot":round(d_pot,2) if d_pot is not None else None,
        "confidence":conf,"annual_move":round(annual,2) if annual is not None else None,
        "peer_mean":grp[0] if grp else None,"peer_sd":grp[1] if grp else None,"peer_n":grp[2] if grp else None,
        "comp_first":round(first['_dev_current'],2),"comp_last":round(last['_dev_current'],2),
        "off_first":round(first['_dev_current'],2) if not is_pitcher else None,
        "off_last":round(last['_dev_current'],2) if not is_pitcher else None,
        "def_first":None,"def_last":None,"window_years":round(yrs,2),"n_snaps":len(window),
        "band":age_band(age),"schedule_status":status,"schedule_label":label,"schedule_note":note,
        "stagnant_tools":stagnant,"gap_closed_pct_yr":round(closure,3) if closure is not None else None,
        "bucket":bucket,"gap_trend":trend,"gap_smoothed":smooth,"years_to_peak":None,
    }
