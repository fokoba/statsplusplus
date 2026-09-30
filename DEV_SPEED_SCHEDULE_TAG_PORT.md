# Porting the "Schedule/Risk" dev-speed tag into a local falsone/statsplusplus instance

## Context (read this first)

This fork (fokoba/statsplusplus, a Flask app for OOTP Baseball GM assistance)
already has a `dev_speed` metric — a z-score of how fast a prospect's
offensive grade / composite score is moving vs. same-bucket/age-band peers,
labeled Rising/On pace/Watch/Stalled/Regressing (`classify()` in
`src/statsplusplus/evaluation/dev_speed.py`). That metric itself was
originally **ported from upstream tfalsone/statsplusplus commit 886ee50**
(see the docstring in `_compute_dev_speed_pass()` in
`src/statsplusplus/data/fv_calc.py`), so if the target local instance is
running an older/base version of falsone's tool, it may or may not already
have that base z-score system at all — check first (see Step 0).

On 2026-09-29 we added a **second, independent tag** on top of the existing
z-score label: a "schedule/risk" system that answers a different question.
The z-score label answers "is this player outpacing his peers right now."
The new schedule tag answers "is he actually closing his OWN gap to his OWN
ceiling, across multiple tools (not just one hot tool skewing the average),
before his runway to peak age runs out." A player can be "Rising" (good
peer-relative pace) and "Behind Schedule" (narrow, one-tool progress) at the
same time — that combination is the whole point, not a bug to resolve.

This was built and validated against a real case: Ethan Wilson, a 24-year-old
PPL hitter whose z-score correctly showed him out-developing a **declining**
peer baseline, which masked that his own HR-power tool hadn't moved and his
gap to ceiling wasn't meaningfully closing with only ~3 years of runway left
to peak age (28 for hitters).

New tag values: `ahead` / `behind` / `at_risk` / `on_track` / `None` (not
enough signal — treat same as not reportable, never coerce to `on_track`).

**IMPORTANT — a real threshold bug was found and fixed during validation,
do not skip it:** the first implementation used "2+ stagnant tools" as a
standalone trigger for "behind." Checked against real league data, this
flagged **84% of the reportable pool** as behind — meaningless, because most
tools move <1 grade point in any given year even for healthy, normally
developing prospects. It was corrected so tool stagnation only counts when
it *contradicts* an otherwise-decent z-score (`z >= 0.5 and n_stagnant >= 2`)
— see `schedule_tag()` below, and its inline comment, which you should port
verbatim, including the comment, so nobody "fixes" it back to the naive
version later.

---

## Step 0 — Confirm target state before touching anything

The target repo is falsone's `tfalsone/statsplusplus` (or a local clone /
fork of it), NOT this repo. Before porting anything:

1. Confirm `src/statsplusplus/evaluation/dev_speed.py` (or equivalent path)
   already exists there with the *base* z-score system (`classify()`,
   `confidence_tier()`, `compute_dev_speed()`, a `dev_speed` DB table, and a
   `_compute_dev_speed_pass()` in the fv_calc/evaluation pipeline). If it
   does NOT exist yet, the base dev-speed metric itself needs porting first
   (out of scope for this document — that's the 886ee50 upstream commit
   referenced above; check if the target is already past that commit).
2. Confirm the target's `dev_speed.py` module structure/function names match
   closely enough that the diffs below apply with minimal adjustment. If
   function/variable names differ, adapt names but preserve all logic,
   constants, and comments (the comments encode validated reasoning, not
   decoration — keep them).
3. Take a DB backup before running any migration:
   `cp data/<league>/league.db /tmp/dominator_dbbackup_pre_schedule_tag_<league>`
   for each league directory in the target instance.

If the target's dev_speed implementation differs meaningfully (different
column names, different pipeline shape), treat everything below as a
reference implementation to adapt rather than a literal patch — the
important thing to preserve is the *logic and thresholds*, not exact code
shape.

---

## Step 1 — `src/statsplusplus/evaluation/dev_speed.py`: add the schedule-tag logic

### 1a. New imports/constants (add near the top, after existing tunables)

```python
from statsplusplus.evaluation.constants import PEAK_AGE_HITTER, PEAK_AGE_PITCHER
```

(If `PEAK_AGE_HITTER`/`PEAK_AGE_PITCHER` don't exist in the target's
`constants` module, add them — values used here: hitter peak age 28,
pitcher peak age 27. Adjust if the target's league configuration defines
peak age differently; these should match whatever the target already uses
elsewhere for aging curves, if anything.)

```python
# --- Schedule/risk tag (2026-09-29) — SECOND, independent signal from the
# z-score label above. Built from a real case (Ethan Wilson, PPL): the
# z-score correctly showed him out-developing a DECLINING peer baseline, but
# that alone doesn't say whether he's actually closing his own gap to
# ceiling, across all his tools, before his runway to peak age runs out. See
# schedule_tag() below for how these combine; each threshold here is
# independently tunable. ---
HITTER_TOOLS = ("cntct", "gap", "pow", "eye")
PITCHER_TOOLS = ("stf", "mov", "ctrl")
STAGNANT_EPS = 1.0          # abs(annual tool movement) below this = stagnant
GAP_SMOOTH_POINTS = 3       # trailing snapshots averaged for gap_smoothed
NEAR_PEAK_YEARS = 1.5       # "near/past peak" runway threshold
SHORT_RUNWAY_YEARS = 3.0    # "limited runway" threshold (softer than near-peak)
GAP_NOT_CLOSING = -0.5      # annualized gap_trend at/above this = "not closing"
AHEAD_GAP_CLOSED_PCT = 0.15 # >15%/yr of the start-of-window gap closed
BEHIND_GAP_CLOSED_PCT = 0.05
AT_RISK_SCORE = 4           # composite risk-score threshold for "at_risk"
```

Adjust `HITTER_TOOLS`/`PITCHER_TOOLS` to match whatever the target's
`ratings_history` table actually calls its tool-grade columns (contact,
gap-power, raw-power, eye/plate-discipline for hitters; stuff, movement,
control for pitchers). These MUST be columns that exist with history in
`ratings_history`, not just the current `ratings` snapshot.

### 1b. New functions — add these three functions to the module, in this order

```python
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
    """[(snapshot_date, ceiling_score - composite_score), ...] for snapshots
    where both are populated. Uses ceiling_score (not true_ceiling) even
    though true_ceiling is the app's preferred *current* ceiling estimate
    (see compute_dev_speed's headline `gap`) — true_ceiling only exists
    going forward from the migration that added it to ratings_history, so
    mixing it into a historical series would silently jump-discontinuity
    the trend the day that column started populating. ceiling_score has
    been tracked the whole time and is internally consistent across it.
    """
    out = []
    for s in window:
        c, g = s.get("composite_score"), s.get("ceiling_score")
        if c is not None and g is not None and c > 0:
            out.append((s["snapshot_date"], max(0, g - c)))
    return out


def gap_smoothed_and_trend(window: list[dict]) -> tuple[Optional[float], Optional[float]]:
    """(gap_smoothed, gap_trend) — trailing-average gap (last
    GAP_SMOOTH_POINTS snapshots) and its annualized change across the window
    (first GAP_SMOOTH_POINTS avg -> last GAP_SMOOTH_POINTS avg). Negative
    trend = gap closing (good); positive = widening.

    Smoothing matters because ceiling_score itself is noisy month to month
    (a scouting-report wobble, not real ceiling movement) — a point-in-time
    gap reading can flip the WIDE_GAP qualifier on pure noise.
    """
    series = _gap_series(window)
    if len(series) < 2:
        return None, None
    vals = [v for _, v in series]
    smoothed = _st.mean(vals[-GAP_SMOOTH_POINTS:])
    first_avg = _st.mean(vals[:GAP_SMOOTH_POINTS])
    last_avg = _st.mean(vals[-GAP_SMOOTH_POINTS:])
    yrs = _years_between(series[0][0], series[-1][0])
    if yrs <= 0:
        return round(smoothed, 2), None
    return round(smoothed, 2), round((last_avg - first_avg) / yrs, 2)


def schedule_tag(
    *, z: Optional[float], gap: int, gap_first: Optional[float], gap_closed_pct_yr: Optional[float],
    gap_trend: Optional[float], stagnant: list[str], age: int, bucket: str,
) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """(status, label, note) — the schedule/risk tag. Independent of
    classify()'s peer-relative label; combines age runway to typical peak,
    whether the gap to ceiling is actually closing (not just out-relative-
    to-a-declining-peer-baseline), and tool-breadth.

    status in {"ahead", "behind", "at_risk", "on_track", None}. None means
    not enough signal to tag at all (no gap history, etc.) — callers should
    treat that the same as not reportable, not as "on_track".
    """
    if z is None:
        return None, None, None
    peak = PEAK_AGE_PITCHER if bucket in ("SP", "RP") else PEAK_AGE_HITTER
    years_to_peak = peak - age
    near_peak = years_to_peak <= NEAR_PEAK_YEARS
    short_runway = years_to_peak <= SHORT_RUNWAY_YEARS
    wide_gap = gap >= WIDE_GAP
    not_closing = gap_trend is not None and gap_trend >= GAP_NOT_CLOSING
    n_stagnant = len(stagnant)

    risk = 0
    reasons = []
    if near_peak and wide_gap:
        risk += 2; reasons.append("near peak age with a wide unclosed gap")
    elif short_runway and wide_gap:
        risk += 1; reasons.append("limited runway to peak with a wide gap")
    if wide_gap and not_closing:
        risk += 2; reasons.append("gap isn't closing")
    if n_stagnant >= 3:
        risk += 2; reasons.append(f"{n_stagnant} tools stagnant")
    elif n_stagnant == 2:
        risk += 1; reasons.append("2 tools stagnant")

    if risk >= AT_RISK_SCORE:
        return "at_risk", "At Risk", "; ".join(reasons) + " — real chance this doesn't come together"

    ahead = (z >= 1.0 and n_stagnant <= 1
             and gap_closed_pct_yr is not None and gap_closed_pct_yr > AHEAD_GAP_CLOSED_PCT)
    if ahead:
        return "ahead", "Ahead of Schedule", "outpacing peers with the gap genuinely closing, broadly across tools"

    # NOTE: tool stagnation alone is NOT a standalone trigger — validated
    # against real data (2026-09-29): most tools move less than 1 grade
    # point in any given year even for healthy, normally-developing
    # prospects, so a blanket "2+ flat tools" rule flagged ~84% of the
    # reportable pool "behind", which is meaningless as a discriminator.
    # Stagnant tools only matter here when they CONTRADICT an otherwise
    # decent z-score — the actual pattern this tag exists to catch (a
    # player whose aggregate/peer-relative read looks fine because of one
    # hot tool, while the rest of his profile hasn't moved) — matching the
    # Ethan Wilson case this whole feature was built from.
    stagnant_despite_ok_z = z is not None and z >= 0.5 and n_stagnant >= 2
    behind = (
        (z is not None and z <= -0.5)
        or (wide_gap and (gap_closed_pct_yr is None or gap_closed_pct_yr <= BEHIND_GAP_CLOSED_PCT))
        or stagnant_despite_ok_z
    )
    if behind:
        note_bits = []
        if n_stagnant >= 2:
            note_bits.append(f"{n_stagnant} tools ({', '.join(stagnant)}) flat over the window")
        if wide_gap and (gap_closed_pct_yr is None or gap_closed_pct_yr <= BEHIND_GAP_CLOSED_PCT):
            note_bits.append("gap to ceiling not meaningfully closing")
        if z is not None and z <= -0.5:
            note_bits.append("trailing same-age/bucket peers")
        return "behind", "Behind Schedule", "; ".join(note_bits) or "not tracking toward ceiling"

    return "on_track", "On Schedule", ""
```

`WIDE_GAP` is an existing constant in the base dev_speed.py (POT-gap
threshold, value 12 in this fork) — reuse it, don't redefine it.

### 1c. Extend `compute_dev_speed()`

Inside the existing `compute_dev_speed()` function, find where `gap`,
`d_ovr`, `d_pot` are computed (this logic should already exist from the
base port) and:

1. If the target doesn't already have a `true_ceiling` preference for the
   headline gap, add it (optional — depends whether target has a
   `true_ceiling` concept at all; if not, just use `ceiling_score` and skip
   this sub-step):
   ```python
   ceil_last_display = last.get("true_ceiling") or ceil_last
   gap = max(0, ceil_last_display - cur_last)
   gap_first = max(0, ceil_first - cur_first)
   ```
2. After `label, css, note = classify(...)` is computed, add:
   ```python
   stagnant = stagnant_tools(window, bucket) if available else []
   gap_smoothed, gap_trend = gap_smoothed_and_trend(window)
   gap_closed_pct_yr = round(d_ovr / gap_first, 3) if gap_first > 0 else None
   if available:
       sched_status, sched_label, sched_note = schedule_tag(
           z=z, gap=gap, gap_first=gap_first, gap_closed_pct_yr=gap_closed_pct_yr,
           gap_trend=gap_trend, stagnant=stagnant, age=age, bucket=bucket,
       )
   else:
       sched_status = sched_label = sched_note = None
   ```
3. Add these keys to the function's returned dict:
   ```python
   "schedule_status": sched_status,
   "schedule_label": sched_label,
   "schedule_note": sched_note,
   "stagnant_tools": stagnant,
   "gap_closed_pct_yr": gap_closed_pct_yr,
   "years_to_peak": round((PEAK_AGE_PITCHER if bucket in ("SP","RP") else PEAK_AGE_HITTER) - age, 1),
   "gap_smoothed": gap_smoothed,
   "gap_trend": gap_trend,
   ```

---

## Step 2 — DB schema: `dev_speed` table + migration

Find the target's `dev_speed` table `CREATE TABLE` (in its `db.py` or
equivalent schema file) and add 8 columns at the end, before the
`PRIMARY KEY` line:

```sql
    schedule_status TEXT,   -- ahead|behind|at_risk|on_track|NULL(not reportable)
    schedule_label  TEXT,
    schedule_note   TEXT,
    stagnant_tools  TEXT,   -- comma-joined tool keys with ~0 movement in-window
    gap_closed_pct_yr REAL, -- fraction of the window-start gap closed per year
    years_to_peak   REAL,
    gap_smoothed    REAL,   -- trailing-average gap (vs point-in-time `gap`)
    gap_trend       REAL,   -- annualized change in smoothed gap; negative = closing
```

Then add an idempotent migration (find wherever the target does its
`ALTER TABLE ... ADD COLUMN` style migrations — usually a function like
`_migrate_ratings_components` or similar run on every connection/startup):

```python
# dev_speed's schedule/risk tag columns (2026-09-29) — table is fully
# derived/recomputed on every fv_calc run, so a plain ALTER (not a
# rebuild) is safe; old rows just carry NULL until the next recompute.
# Guard on the table actually existing — some callers (tests) run this
# migration function against a minimal schema without dev_speed at all.
has_table = conn.execute(
    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='dev_speed'"
).fetchone()
if has_table:
    existing = {row[1] for row in conn.execute("PRAGMA table_info(dev_speed)").fetchall()}
    for col, typ in [("schedule_status", "TEXT"), ("schedule_label", "TEXT"),
                      ("schedule_note", "TEXT"), ("stagnant_tools", "TEXT"),
                      ("gap_closed_pct_yr", "REAL"), ("years_to_peak", "REAL"),
                      ("gap_smoothed", "REAL"), ("gap_trend", "REAL")]:
        if col not in existing:
            conn.execute(f"ALTER TABLE dev_speed ADD COLUMN {col} {typ}")
```

**Gotcha to watch for**: if this migration function runs unconditionally
against a bare/test schema that doesn't have `dev_speed` at all, the
`has_table` guard above is required — without it you'll hit
`sqlite3.OperationalError: no such table: dev_speed` in any test fixture or
fresh-DB code path. This was an actual bug hit and fixed during this port.

If the target's test suite has a hand-rolled schema for `dev_speed` in a
conftest/fixture file (separate from the real schema), remember to add the
same 8 columns there too, or tests referencing `ds.schedule_status` etc.
will fail with "no such column."

---

## Step 3 — Compute pipeline: wherever `dev_speed` rows get written

Find the function that calls `compute_dev_speed()` in a loop and writes to
the `dev_speed` table (in this fork, `_compute_dev_speed_pass()` in
`src/statsplusplus/data/fv_calc.py`). Two changes:

1. **The SELECT that loads `ratings_history`** needs the tool-grade columns
   added (whatever `HITTER_TOOLS`/`PITCHER_TOOLS` map to — contact, gap,
   power, eye for hitters; stuff, movement, control for pitchers) plus
   `true_ceiling` if the target has that column. Example (adapt column
   names to target):
   ```sql
   SELECT h.player_id, h.snapshot_date, h.composite_score, h.ceiling_score,
          h.true_ceiling, h.ovr, h.pot, h.offensive_grade, h.defensive_value,
          h.cntct, h.gap, h.pow, h.eye, h.stf, h.mov, h.ctrl, p.age
   FROM ratings_history h JOIN players p ON p.player_id = h.player_id
   WHERE h.composite_score IS NOT NULL AND h.composite_score > 0
   ORDER BY h.player_id, h.snapshot_date
   ```
   And when building the per-snapshot dict passed into `compute_dev_speed`'s
   `window` list, include those same keys:
   ```python
   d = {"snapshot_date": r["snapshot_date"], "composite_score": r["composite_score"],
        "ceiling_score": r["ceiling_score"], "true_ceiling": r["true_ceiling"],
        "ovr": r["ovr"], "pot": r["pot"],
        "offensive_grade": r["offensive_grade"], "defensive_value": r["defensive_value"],
        "cntct": r["cntct"], "gap": r["gap"], "pow": r["pow"], "eye": r["eye"],
        "stf": r["stf"], "mov": r["mov"], "ctrl": r["ctrl"]}
   ```
   (Naming collision to be aware of: `r["gap"]` here is the raw
   Gap-Power **hit tool** column, totally unrelated to `compute_dev_speed`'s
   own `gap` variable, which means ceiling-minus-composite. Keep them in
   separate dicts as shown, don't let the names collide in one scope.)

2. **The INSERT tuple** needs the 8 new result fields appended, and the
   placeholder count in the `INSERT OR REPLACE` bumped accordingly (this
   fork went from 24 to 32 `?` placeholders):
   ```python
   out_rows.append((
       # ...existing fields...
       res["schedule_status"], res["schedule_label"], res["schedule_note"],
       ",".join(res["stagnant_tools"]) if res["stagnant_tools"] else None,
       res["gap_closed_pct_yr"], res["years_to_peak"],
       res["gap_smoothed"], res["gap_trend"],
   ))
   ...
   conn.executemany(
       "INSERT OR REPLACE INTO dev_speed VALUES (" + ",".join("?" * 32) + ")", out_rows)
   ```
   **Count the `?` placeholders yourself against the target's actual column
   count** — 32 is only correct if the target's base `dev_speed` table had
   24 columns before this port; don't copy the literal number blindly.

3. **If the target doesn't yet have `true_ceiling` written to
   `ratings_history`** (check: does the evaluation engine's history-update
   INSERT/UPDATE include a `true_ceiling` column?), this is optional but
   recommended — without it, `schedule_tag`'s headline `gap` falls back to
   `ceiling_score`, which is fine, just slightly more conservative. If you
   do want to add it, the pattern used in this fork (in
   `src/statsplusplus/data/evaluation_engine.py`, wherever `ratings_history`
   gets its per-cycle UPDATE) was to build the SET clause dynamically and
   guard on column existence, to avoid this exact SQL bug:
   ```python
   # BUG THAT WAS HIT: f"...offensive_ceiling = ?, {true_ceiling_col} WHERE..."
   # produced a trailing comma before WHERE when true_ceiling_col was empty
   # (column didn't exist yet on an unmigrated DB), causing a silent SQL
   # syntax error swallowed by a bare `except Exception: pass` around the
   # whole write. Build the SET clause from a list instead:
   set_cols = ["composite_score", "ceiling_score", "offensive_grade",
               "baserunning_value", "defensive_value", "durability_score",
               "offensive_ceiling"]
   if "true_ceiling" in hist_cols:
       set_cols.append("true_ceiling")
   set_clause = ", ".join(f"{c} = ?" for c in set_cols)
   conn.executemany(f"UPDATE ratings_history SET {set_clause} WHERE player_id = ? AND snapshot_date = ?",
                     history_updates)
   ```

---

## Step 4 — Query/display layer

### 4a. Shared badge-cell helper

Find (or create) the shared helper that turns a `dev_speed` row slice into
a display dict for templates — in this fork it's `dev_cell()` in
`web/web_league_context.py`. Add a schedule-badge lookup dict and extend the
row-unpacking to read 3 more fields (the row slice grows from 5 fields to
8):

```python
# Schedule/risk tag (2026-09-29) — a second, independent badge alongside the
# peer-relative icon above; see statsplusplus.evaluation.dev_speed.schedule_tag.
# "on_track" intentionally renders no badge (kept quiet — only the notable
# cases get flagged) and "ahead"/"ok" aren't warnings, unlike the other two.
_SCHEDULE_BADGE = {
    "ahead": {"text": "Ahead", "css": "sched-ahead"},
    "behind": {"text": "Behind", "css": "sched-behind"},
    "at_risk": {"text": "At Risk", "css": "sched-risk"},
}


def dev_cell(row, i):
    """Build a compact dev-speed cell from a row slice starting at index i:
    (available, css_class, label, confidence, z, schedule_status,
    schedule_label, schedule_note). Returns None when unavailable so list
    templates render an empty cell.
    """
    try:
        available, css, label, conf, z = row[i], row[i + 1], row[i + 2], row[i + 3], row[i + 4]
        sched_status, sched_label, sched_note = row[i + 5], row[i + 6], row[i + 7]
    except (IndexError, TypeError):
        return None
    if not available:
        return None
    badge = _SCHEDULE_BADGE.get(sched_status)
    return {"icon": _DEV_ICON.get(css, ""), "css_class": css, "label": label,
            "confidence": conf, "z": z, "dim": conf == "Low",
            "schedule_status": sched_status, "schedule_label": sched_label,
            "schedule_note": sched_note,
            "schedule_badge_text": badge["text"] if badge else None,
            "schedule_badge_css": badge["css"] if badge else None}
```

### 4b. SQL SELECTs that feed the row-unpacking above

Every SELECT that currently does something like:
```sql
ds.available, ds.css_class, ds.label, ds.confidence, ds.z
```
needs three more columns appended right after `ds.z`:
```sql
ds.available, ds.css_class, ds.label, ds.confidence, ds.z,
ds.schedule_status, ds.schedule_label, ds.schedule_note
```

**⚠️ Critical gotcha — positional index renumbering:** if this 5-column
block sits in the MIDDLE of a larger SELECT (not at the end), inserting 3
new columns shifts every downstream `row[N]` / `r[N]` reference by +3 for
everything after it. In this fork, `get_farm()` in `web/team_queries.py` had
`ds.z` followed later by `r.acc` and personality fields — adding the 3
columns between them required renumbering roughly 8 downstream indices
(e.g. `_personality_fields(r[21]...r[27])` was `r[18]...r[24]` before this
change; `confidence_tier(r[11], r[20], ...)` was `r[17]` before). **Trace
the exact original column order and every positional reference before
editing, don't guess-and-test.** Prefer appending the 3 columns at the very
END of the SELECT instead, if the query has one — that avoids renumbering
entirely (this is what was done for the two prospect-list queries in
`web/queries.py`, since `ds.z` was already the last dev_speed column there).

### 4c. CSS — badge styling

Add to the target's stylesheet:

```css
/* Development schedule/risk badge (2026-09-29) — a second, independent tag
   alongside the existing peer-relative Dev icon (⚡/↗/⚠/↘). See
   statsplusplus.evaluation.dev_speed.schedule_tag for what drives it. */
.sched-badge {
  display: inline-block; font-size: 10px; font-weight: 700; padding: 1px 6px;
  border-radius: 3px; white-space: nowrap; vertical-align: middle;
}
.sched-ahead { background: rgba(76, 175, 80, 0.2); color: #4caf50; border: 1px solid rgba(76, 175, 80, 0.4); }
.sched-behind { background: rgba(255, 152, 0, 0.2); color: #ff9800; border: 1px solid rgba(255, 152, 0, 0.4); }
.sched-risk { background: rgba(244, 67, 54, 0.2); color: #f44336; border: 1px solid rgba(244, 67, 54, 0.4); }
.sched-panel {
  margin-top: 8px; padding: 8px 10px; border-radius: 4px; font-size: 12px;
  display: flex; flex-wrap: wrap; gap: 6px; align-items: baseline;
  background: var(--surface); border-left: 3px solid transparent;
}
.sched-panel.sched-ahead { border-left-color: #4caf50; }
.sched-panel.sched-behind { border-left-color: #ff9800; }
.sched-panel.sched-risk { border-left-color: #f44336; }
.sched-panel-note { color: var(--text); }
.sched-panel-meta { color: var(--text-dim); font-size: 11px; }
```

If the target's stylesheet doesn't have `--surface`/`--text`/`--text-dim`
CSS variables, substitute whatever theme variables (or literal colors) it
already uses elsewhere for similar note/panel components.

### 4d. Templates — where the Dev badge already appears

Wherever the existing dev-speed icon/label is rendered (farm table, player
detail page, any prospect list), add the schedule badge alongside it:

- **Compact table cell** (e.g. farm table `<td>` for Dev):
  ```html
  {% if p.dev.schedule_badge_text %}
    <span class="sched-badge {{ p.dev.schedule_badge_css }}" title="{{ p.dev.schedule_note }}">{{ p.dev.schedule_badge_text }}</span>
  {% endif %}
  ```
- **Player detail page summary panel** — a small inline badge next to the
  existing dev-speed line, using a Jinja map from status→CSS class (mirrors
  the Python `_SCHEDULE_BADGE` dict so template + backend stay in sync):
  ```html
  {% set _sched_css = {"ahead": "sched-ahead", "behind": "sched-behind", "at_risk": "sched-risk"} %}
  {% if p.dev_speed.schedule_status in _sched_css %}
    <div><span class="sched-badge {{ _sched_css[p.dev_speed.schedule_status] }}" title="{{ p.dev_speed.schedule_note }}">{{ p.dev_speed.schedule_label }}</span></div>
  {% endif %}
  ```
- **Player detail page, fuller "Development" tab/section** — a more verbose
  panel with the note and supporting metrics, shown only for the 3 notable
  statuses (never for `on_track`/`None` — keep those quiet, only flag
  exceptions):
  ```html
  {% if p.dev_speed.schedule_status in ("ahead", "behind", "at_risk") %}
    <div class="sched-panel {{ _sched_css[p.dev_speed.schedule_status] }}">
      <strong>{{ p.dev_speed.schedule_label }}</strong>
      <span class="sched-panel-note">{{ p.dev_speed.schedule_note }}</span>
      <span class="sched-panel-meta">
        {% if p.dev_speed.stagnant_tools %}stagnant: {{ p.dev_speed.stagnant_tools }} · {% endif %}
        {% if p.dev_speed.gap_closed_pct_yr is not none %}{{ (p.dev_speed.gap_closed_pct_yr * 100)|round(1) }}%/yr gap closed · {% endif %}
        {{ p.dev_speed.years_to_peak }} yrs to peak
      </span>
    </div>
  {% elif p.dev_speed.schedule_status == "on_track" %}
    <p class="note">Tracking normally toward ceiling.</p>
  {% endif %}
  ```

Confirm however the target's player-detail route loads `dev_speed` for a
single player (likely a `SELECT * FROM dev_speed WHERE player_id = ?` +
`dict(zip(cols, row))`, or equivalent) — if it's a `SELECT *` already, the 8
new columns show up automatically with **zero code changes** on that path;
only explicit column-list SELECTs (Step 4b) need edits.

---

## Step 5 — Verify

1. Run the target's fv_calc / evaluation pipeline once (or trigger a
   refresh) so `dev_speed` gets recomputed with the new columns populated.
2. Direct DB spot-check:
   ```sql
   SELECT player_id, schedule_status, schedule_label, gap_closed_pct_yr, years_to_peak
   FROM dev_speed WHERE schedule_status IS NOT NULL LIMIT 20;
   ```
3. **Recalibration sanity check — do not skip:** query the "behind" rate
   across the reportable pool and confirm it's NOT anywhere near 80%+:
   ```sql
   SELECT
     SUM(CASE WHEN schedule_status='behind' THEN 1 ELSE 0 END) * 1.0 /
     SUM(CASE WHEN schedule_status IS NOT NULL THEN 1 ELSE 0 END) AS behind_rate
   FROM dev_speed;
   ```
   Reasonable range validated on this fork's leagues was ~40-60%. If it's
   80%+, the `stagnant_despite_ok_z` gating in `schedule_tag()` didn't port
   correctly — re-check Step 1b against the exact code above, don't
   re-introduce standalone `n_stagnant >= 2` as a trigger.
4. Load the farm/prospects page and a player detail page in a browser,
   confirm the new badges render (not blank, not broken layout — watch for
   CSS class collisions with any existing `.badge`/absolute-positioned
   classes the target may already use for something else, the way
   `.grade-cur` collided with a similarly-named badge class in this fork).
5. Run the target's test suite; compare failure count against its own
   pre-port baseline (don't assume 0 failures is the baseline — note
   whatever pre-existing failures exist before you start, and confirm the
   count doesn't grow).
