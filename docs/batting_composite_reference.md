# Batting Composite (OVR / vR / vL) — Reference for Claude

This document explains a metric called **Batting Composite** in the Stats++ app (OOTP Baseball
assistant-GM tool for two leagues: **ppl**, 20-80 ratings scale, and **emlb**, 1-100 ratings
scale). Paste this into another Claude session to give it full, precise context on what the
number means and how it's computed — no guessing required.

## What it is, in one sentence

**Batting Composite = a weighted average of ONLY Contact, Gap, Power, and Eye** (the four core
hitting tools), using each league's own calibrated weights for those four tools, renormalized so
they sum to 1. Nothing else goes into it — no defense, no speed, no baserunning, no transform
curves, no positional adjustment.

## What it's for (and what it's NOT for)

- **Use it to answer: "who is the better hitter?"** — a pure bat-to-bat comparison, independent
  of position or defensive value.
- **Do NOT use it for overall roster value or WAR ranking.** The app's main composite score
  (`Ovr`/`Pot`, and the "Comp vL"/"Comp vR" columns) is the right tool for that — it also factors
  in defense, speed/baserunning, tool-imbalance penalties, and per-tool transform curves, all of
  which matter for actual WAR. Two players can have identical Batting Composite scores and very
  different real value if one plays a premium defensive position well and the other doesn't.
- It also ignores **position-relative replacement level** — a 50 Batting Composite is worth far
  more at catcher or shortstop than at first base or a corner outfield spot, and that gap only
  shows up in the full composite and in surplus-value ($) calculations, never in this metric.

## The exact formula

For any of the three variants (see below), given four tool ratings and a weight dict:

```
available = [(value, weight) for each of contact/gap/power/eye where value is not None
             and weight > 0]
total_weight = sum(weight for _, weight in available)
result = round( sum(value * weight for value, weight in available) / total_weight )
```

- If a tool's rating is missing, it's dropped and the remaining weights are implicitly
  renormalized (the division by `total_weight` handles this automatically).
- If **all four** are missing, the result is `None` (blank in the UI).
- Result is rounded to the nearest integer, on the league's native ratings scale (20-80 for ppl,
  1-100 for emlb).

Implementation: `compute_batting_composite(contact, gap, power, eye, weights)` in
`src/statsplusplus/evaluation/composite.py`.

## The three variants

| Metric | Inputs used |
|---|---|
| **Batting Composite OVR** | Current Contact/Gap/Power/Eye → one number. A separate call with the four **Potential** ratings produces the paired "current/potential" display (e.g. `45/60`) shown in the UI — these are two independent invocations of the same formula, not a blend. |
| **Batting Composite vR** | The player's **vs-RHP split** ratings: `Cntct_R`, `Gap_R`, `Pow_R`, `Eye_R` |
| **Batting Composite vL** | The player's **vs-LHP split** ratings: `Cntct_L`, `Gap_L`, `Pow_L`, `Eye_L` |

All three use the *same* weight set (see below) — only the input ratings change.

## Where the weights come from

Each league has its own calibrated `hitter` weight table in
`data/<league>/config/tool_weights.json`, broken out **per position bucket** (C, SS, 2B, 3B, CF,
COF, 1B). Each bucket's full weight set includes `contact`, `gap`, `power`, `eye`, `speed`,
`steal`, `stl_rt`, and `defense` (all summing to 1 across the whole bucket).

Batting Composite takes the bucket's own `contact`/`gap`/`power`/`eye` weights and rescales them
to sum to 1 by themselves (dropping `speed`/`steal`/`stl_rt`/`defense` from the denominator).

**Important simplification, confirmed by direct calculation**: because `speed`/`steal`/`stl_rt`
are nearly constant across buckets within a league, and `defense` scales in a way that preserves
the *ratio* between contact/gap/power/eye, the renormalized 4-tool weights come out **essentially
identical across every position bucket** in both leagues. In practice, Batting Composite uses one
fixed weight profile per league, regardless of the player's position:

### PPL (calibrated 1955-01-01, n=166 hitters) — 20-80 scale

| Contact | Gap | Power | Eye |
|---|---|---|---|
| 16.8% | 38.6% | 26.9% | 17.7% |

Gap (doubles/triples power) dominates — consistent with a lower-power, extra-base-hit-driven
1955 offensive environment.

### eMLB (calibrated 2034-01-01, n=400 hitters) — 1-100 scale

| Contact | Gap | Power | Eye |
|---|---|---|---|
| 38.5% | 5.0% | 25.7% | 30.8% |

Contact and plate discipline (Eye) dominate, Gap is nearly irrelevant — consistent with a modern,
on-base-driven 2034 offensive environment.

(Full per-bucket raw weight tables, including defense/speed shares, are in
`docs/composite_calculation_reference.md` if you need the un-renormalized source values.)

## Worked example (hand-verified)

PPL, COF-bucket player with Contact 35 / Gap 45 / Power 35 / Eye 35:

```
35×0.1577 + 45×0.363 + 35×0.2525 + 35×0.1668 = 36.53
36.53 / (0.1577+0.363+0.2525+0.1668) = 36.53 / 0.94 = 38.86 → rounds to 39
```

This matches the app's live output exactly (verified against Travis Walker, a real PPL prospect,
whose Batting Composite OVR shows `39`).

## Where it's computed in code

- **Core function**: `src/statsplusplus/evaluation/composite.py` → `compute_batting_composite()`
- **Draft page**: `web/queries.py` → `get_draft_pool()`
- **Player page**: `web/player_queries.py` → `get_player()`
- **Team roster (Waivers/Free Agents tabs)**: `web/team_queries.py` →
  `get_waiver_candidates()`, `get_free_agent_candidates()`
- **Minor league rosters (single team + org-wide "All Minor Leaguers")**:
  `web/team_queries.py` → `get_minor_league_roster()`, `get_org_minor_league_roster()`,
  `_org_vr_vl_composites()`
- **Custom Upload**: `scripts/custom_upload.py` → `evaluate_row()`

Every call site loads weights via `load_tool_weights(league_dir)` (falling back to
`DEFAULT_TOOL_WEIGHTS` if a league's file is missing/incomplete), and resolves `league_dir` the
session-safe way — `get_cfg().league_dir` in Flask request context — never the global
`get_league_dir()` fallback, which reads a shared `app_config.json` file that a *different*
browser tab/session can silently overwrite. (This distinction matters: an earlier version of an
unrelated surplus-value calculation had exactly this bug, causing one session's numbers to use
another session's active league. Batting Composite was built to avoid that class of bug from day
one.)

## Where it's surfaced in the UI

| Page | Columns |
|---|---|
| Draft (Hitters/All views) | `Bat OVR` (current/pot pair), `Bat vR`, `Bat vL` |
| Player detail page | "Bat Composite" row (current/pot) under Hit/Gap/Power/Eye, plus a vL/vR split row |
| Team → Waivers / Free Agents | `Bat OVR`, `Bat vR`, `Bat vL` |
| Team → Minor League roster (single affiliate) | `Bat OVR` (current/pot) — no split columns here |
| Team → All Minor Leaguers (org-wide) | `Bat OVR` (current/pot), `Bat vR`, `Bat vL` |
| Custom Upload | `Bat OVR` (current/pot), `Bat vR`, `Bat vL` |

Hitters only — pitchers show `-`/blank for all Batting Composite fields.

## How it differs from the existing "Comp vL" / "Comp vR" columns

The app already has a fuller weighted composite (`compute_composite_hitter`) that also appears as
"Comp vL"/"Comp vR" on some pages. That one includes defense, speed/steal, tool-imbalance
penalties, per-tool transform curves, and baserunning-boost logic. Batting Composite is
deliberately a **simpler sibling** — same underlying weight file, but restricted to exactly four
tools with no extra logic layered on. Treat "Comp vL/vR" as the fuller value signal and "Bat
OVR/vR/vL" as the pure hitting-skill comparator.
