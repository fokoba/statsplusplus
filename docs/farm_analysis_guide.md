# Farm System Analysis Guide

## Purpose

This document defines the repeatable process for evaluating and presenting farm
system prospects for the Anaheim Angels org.

---

## Eligibility

- Include all players in `farm/` directories at any level
- **Exclude any player age 26 or older** — they are no longer prospects
- Exclude players in `farm/intl/` unless Pot ≥ 40 (too raw to rank meaningfully)

---

## Level Age Norms

| Level | Norm | Young (≤) | Old (≥) |
|---|---|---|---|
| MLB | 27 | 24 | 30 |
| AAA | 26 | 23 | 29 |
| AA | 24 | 21 | 26 |
| A | 22 | 20 | 24 |
| A-Short | 21 | 19 | 23 |
| USL | 19 | 17 | 21 |
| DSL | 18 | 17 | 21 |
| Intl | 17 | 15 | 20 |

---

## Step 1 — Assign Positional Bucket

Do **not** use the player's listed `Pos` field as their bucket. Use their **positional
grade ratings** to determine where they are viable at the MLB level.

Positional grades are on the **1-100 scale**. Thresholds below use that scale.

### Positional bucketing logic (uses 1-100 scale thresholds)

Pitchers are bucketed as **RP** if either condition is true:
- Fewer than 3 pitches with **potential grade ≥ 45** (raw scale) — viability is based on projection, not current grade. Check all 12 pitch fields: `Fst`, `Snk`, `Crv`, `Sld`, `Chg`, `Splt`, `Cutt`, `CirChg`, `Scr`, `Frk`, `Kncrv`, `Knbl`
- `Stm` < 40

**Exception — knuckleball pitchers:** A pitcher with `PotKnbl ≥ 45` or `PotKncrv ≥ 45` and `Stm ≥ 40` is bucketed as **SP** regardless of how many other pitches they have. A knuckleball is a complete arsenal on its own — knuckleballers do not need supporting pitches to project as starters.

Otherwise they are bucketed as **SP**. The 45 pot threshold represents a fringe-average future offering — a pitch the pitcher can realistically deploy at the MLB level. A pitcher with two plus pitches and nothing else projecting to 45+ is a reliever by arsenal, regardless of stamina.

For position players:

Evaluate in this exact order — assign the first bucket the player qualifies for:

1. **C** — `C` ≥ 45
2. **SS** — `SS` ≥ 50
3. **2B** — `2B` or `SS` ≥ 50
4. **CF** — `CF` ≥ 55 (below 55 is not viable at CF)
5. **COF** — `LF` or `RF` ≥ 45 ← check before 3B and 1B
6. **3B** — `3B` ≥ 45
7. **1B** — `1B` ≥ 45
8. **COF** — fallback if no other bucket qualifies

**Why COF before 3B/1B:** A player with plus OF grades and a 1B fallback is a corner outfielder, not a first baseman. The 1B bucket is for players with no viable OF or IF grade elsewhere.

Use **potential grades** (`PotSS`, `PotCF`, etc.) for players age ≤ 23.

---

## Step 2 — Normalize Individual Ratings to 20-80 Scale

Individual attribute ratings (Cntct, Pow, Stf, etc.) are stored on a **1-100 scale**.
Convert to 20-80 before presenting:

```
normalized = round(20 + (raw / 100) * 60)
```

**Quick reference:**

| Raw | Normalized | Label |
|---|---|---|
| 100 | 80 | Elite |
| 83 | 70 | Plus-plus |
| 67 | 60 | Plus |
| 50 | 50 | Average |
| 33 | 40 | Below average |
| 17 | 30 | Poor |
| 1 | 20 | Org player |

`Ovr` and `Pot` are **already on the 20-80 scale** — do not normalize them.

### Batter Tool Grade Mapping

| Tool | Present | Future |
|---|---|---|
| Hit | `Cntct` | `PotCntct` |
| Raw Power | `Pow` | `PotPow` |
| Game Power | `min(Pow, avg(Pow, Gap) × (0.6 + 0.4 × Cntct/100))` | same formula using `PotPow`, `PotGap`, `PotCntct` |
| Run | `Speed` | `Speed` (rarely changes) |
| Fielding | best positional grade for bucket | corresponding `Pot*` grade |
| Throw | `OFA` (OF) / `IFA` (IF) / `CArm` (C) | same field |

**Catchers only:** Also show `CBlk` (blocking) and `CFrm` (framing) — normalize these to the 20-80 scale the same as other attributes. Framing directly affects called strikes for pitchers; blocking affects passed balls. Neither has a `Pot*` counterpart, so show as present-only grades.

Catcher grade table format:
```
| Hit   | Raw Power | Game Power | Run | Eye   | K-Rate | Fielding (C) | Arm | Blocking | Framing |
| ----- | --------- | ---------- | --- | ----- | ------ | ------------ | --- | -------- | ------- |
| XX/XX | XX/XX     | XX/XX      | XX  | XX/XX | XX/XX  | XX/XX        | XX  | XX       | XX      |
```

### Pitcher Tool Grade Mapping

Show the pitcher's **top 4 viable pitches** by present grade. A pitch is considered viable if its present grade ≥ 25 **or** its potential grade ≥ 45 — include projected pitches even if they are not yet showing up in games. Do not default to showing a fastball — some pitchers work primarily off a cutter, sinker, or breaking ball. Available pitch fields:

| Field | Pitch |
|---|---|
| `Fst` | Fastball |
| `Snk` | Sinker |
| `Crv` | Curveball |
| `Sld` | Slider |
| `Chg` | Changeup |
| `Splt` | Splitter |
| `Cutt` | Cutter |
| `CirChg` | Circle Change |
| `Scr` | Screwball |
| `Frk` | Forkball |
| `Kncrv` | Knuckle Curve |
| `Knbl` | Knuckleball |

Each has a corresponding `Pot*` field for future grade. Also show `Vel` (velocity string), `Stf`/`PotStf`, `Mov`/`PotMov` (Movement), and control (see below).

**Control:** No top-level `Ctrl` field exists. `Ctrl_R` and `Ctrl_L` represent control vs. right-handed and left-handed batters respectively — average them: `Ctrl = (Ctrl_R + Ctrl_L) / 2`. Use `PotCtrl` for the future grade.

### Height Conversion

Height stored in cm: `feet = int(cm / 30.48)`, `inches = round((cm % 30.48) / 2.54)`

---

## Step 3 — Assign FV with Half-Grade (+) Support

FV is on the 20-80 scale representing expected MLB contribution:

| FV | MLB Role | WAR/season |
|---|---|---|
| 20 | Org player | — |
| 30 | Fringe MLB / AAA depth | < 0.0 |
| 40 | Bench bat / spot starter | 0.0 – 0.8 |
| 45 | Low-end regular / platoon | 0.9 – 1.6 |
| 50 | Average everyday player | 1.7 – 2.4 |
| 55 | Above-average regular | 2.5 – 3.5 |
| 60 | All-Star caliber | 3.6 – 4.8 |
| 65 | Perennial All-Star | 4.9 – 6.2 |
| 70 | Top-10 overall player | 6.3 – 8.5 |
| 80 | Top-5 overall / generational | > 8.5 |

Use **half-grades** (e.g. `45+`, `50+`) for a player who has established a floor
at the lower grade with meaningful probability of reaching the next. A `45+` ranks
above `45` and below `50`. Use sparingly.

### Deriving FV

```
FV ≈ Ovr + (Pot - Ovr) × development_weight
```

**Development weight by age vs. level norm:**

| Age vs. norm | Weight |
|---|---|
| 3+ years young | 0.65 |
| 1-2 years young | 0.50 |
| At norm (±1 year) | 0.35 |
| 1-2 years old | 0.20 |
| 3+ years old | 0.10 |

**Adjustments (all bonuses require Pot ≥ 45 — no bonuses apply to below-average ceiling players):**
- Reliever profile (RP bucket): FV hard cap at 50 — a reliever's peak value (≤2.0 WAR/season) does not support a higher grade regardless of raw talent
- `WrkEthic` H/VH: +1
- `WrkEthic` L: -1
- `Acc` = L: apply -2 FV penalty before rounding, and shift 5% from middle outcomes to **bust only** (not star). Low accuracy means we know less, which increases downside risk but does not increase upside. Note in the scouting summary that the range of outcomes is wider than normal

**Unified defensive bonus** — a single scaled system for all defensive positions (C, SS, CF, 2B, 3B, COF). Uses the positional composite as the base (rewards positional difficulty and experience) with the position-weighted defensive score as a modifier (rewards underlying tool quality). 1B is excluded — first base defense is not a meaningful differentiator.

| Composite | Weighted Score ≥ 65 | Weighted Score 55-64 | Weighted Score < 55 |
|---|---|---|---|
| ≥ 70 | +3 | +2 | +1 |
| 60-69 | +2 | +1 | +0 |

Composite uses potential grades for age ≤ 23, current grades for 24+. The weighted score uses position-specific tool importance:

| Bucket | Weights |
|---|---|
| C | Framing 45%, Blocking 35%, Arm 20% |
| SS | Range 40%, Error 20%, Arm 20%, Turn DP 20% |
| 2B | Range 35%, Turn DP 30%, Error 20%, Arm 15% |
| 3B | Arm 35%, Error 30%, Range 25%, Turn DP 10% |
| CF | Range 55%, Error 25%, Arm 20% |
| COF | Best of LF (Range 50%, Error 30%, Arm 20%) or RF (Range 40%, Arm 35%, Error 25%) |

The composite captures positional difficulty — an average-tool SS with comp=65 gets +1 because playing shortstop at all is valuable, while the same tools at 2B might yield +0. The weighted score differentiates within a composite tier — an elite-glove SS (comp=70, wt=70) gets +3 while a comp=70 SS with average tools (wt=58) gets +2. Positional value (SS > COF at the same skill level) is already priced into Ovr/Pot by the game engine, so the defensive bonus rewards tool quality relative to the position rather than duplicating the positional adjustment.

**Platoon split penalty** — a prospect with a severe weak side is a platoon player (hitter) or gets exposed against one handedness (pitcher). Only applies when split data exists and the weak side is exploitable:
- **Hitters:** weak-side Contact < 30 raw AND gap ≥ 25 → -3 FV; gap ≥ 15 → -2 FV
- **Pitchers:** weak-side Stuff < 30 raw AND gap ≥ 20 → -3 FV; gap ≥ 12 → -2 FV

**Positional versatility bonus** — a player viable at multiple positions has roster flexibility that adds real value beyond their primary bucket. Evaluate using the same age-gated thresholds as bucketing (Pot grades for age ≤ 23, current grades for age 24+):
- **+2 FV** if viable at 2 or more positions outside the primary bucket
- **+1 FV** if viable at 1 additional position outside the primary bucket
- Uses the same viability thresholds as the bucketing logic (SS ≥ 50, 3B ≥ 45, CF ≥ 55, etc.)
- Pitchers are ineligible. A position player's pitcher grade does not count.
- Requires **Pot ≥ 45** — roster flexibility only adds value if the player has a viable MLB role to begin with.
- Stacks with the defensive bonus but cannot push FV above `Pot + 5`

**Pitcher arsenal ceiling override** — the composite Pot can understate a pitcher's ceiling when individual pitch projections are elite. Use an effective Pot for FV calculation only (does not change the displayed Pot):
- **3+ pitches with Pot ≥ 80**: effective Pot = max(Pot, 55)
- **2+ pitches with Pot ≥ 80**: effective Pot = max(Pot, 50)

**Critical:** Do not penalize young, high-ceiling prospects for low current Ovr.
A 19-year-old with Ovr 28 / Pot 63 in A-Short has more FV than a 25-year-old
with Ovr 44 / Pot 46 in AAA. The development weight accounts for this — trust it.

Round FV to nearest 5, then apply `+` if the player shows a meaningful floor at
that grade with upside for the next.

---

---

## Output Format

### Section 1 — Top 15 Prospects

Rank farm-system-only players by FV (ties broken by age, younger first). For each:

```
[Rank]. [Full Name] | [Bucket] | [Level] | Age [X] | FV [XX or XX+]
[X'X"] | [Bats]/[Throws]

Tool Grades (Present/Future)
[Batter]:
| Hit   | Raw Power | Game Power | Run | Eye   | K-Rate | Fielding | Arm |
| ----- | --------- | ---------- | --- | ----- | ------ | -------- | --- |
| XX/XX | XX/XX     | XX/XX      | XX  | XX/XX | XX/XX  | XX/XX    | XX  |

[Pitcher]:
| Pitch1 | Pitch2 | Pitch3 | Pitch4 | Velocity | Control | Stuff | GB/Contact |
| ------ | ------ | ------ | ------ | -------- | ------- | ----- | ---------- |
| XX/XX  | XX/XX  | XX/XX  | XX/XX  | XX-XX    | XX/XX   | XX/XX | XX/XX      |
```

**Pitcher field definitions:**
- **Control** (`Ctrl_R`/`Ctrl_L` averaged) — ability to throw strikes and avoid walks
- **Movement** (`Mov`) — composite of ground ball rate and pitcher BABIP; reflects ability to limit hard contact and home runs, not pitch location per se
- **Stuff** (`Stf`) — overall quality of pitch arsenal; drives strikeouts. **Note: relievers receive an engine-level stuff bonus** tied to their top two pitches — RP stuff grades are already inflated relative to what the same pitcher would show as a starter.

Grade labels (80=Elite, 70=Plus-plus, 60=Plus, 55=Above-average, 50=Average, 45=Fringe-average, 40=Below-average, 30=Well below-average, 20=Poor) are for use in scouting prose only — do not display them in the grade table.

[2-4 sentence scouting summary. Do NOT use generic templates — every summary must be specific to this player's actual grades. Use varied scouting language — avoid repeating the same descriptors across players. Do not reference any game engine field names in prose — this includes `Ovr`, `Pot`, `Stm`, `Ctrl_R`, `PotCntct`, `PotPow`, or any other raw data field. Describe the player's tools and projection as a scout would. Address:
- **Best tool:** Name it with a grade label (e.g. "a plus-plus fastball", "an above-average glove at shortstop", "plus raw power")
- **Second-best tool or key supporting grade** if it meaningfully shapes the profile
- **Biggest risk:** The specific weakness — name the tool and the concern (e.g. "control that hasn't found the strike zone consistently", "a swing with too many holes to project as an everyday hitter", "raw power that hasn't translated to game power yet")
- **Ceiling:** The MLB role if things go right
- **Floor:** What the player is if the projection doesn't materialize

For pitchers: lead with the best pitch and velocity, then address the supporting arsenal, then control. Describe stamina in prose terms only — never cite the raw number. Use the following thresholds:
- `Stm ≥ 50`: no stamina comment needed — projects as a full starter
- `Stm 40–49`: note that the pitcher profiles as a shorter-outing starter who will hand the ball to the bullpen early (e.g. "a five-inning starter", "won't go deep into games") — do **not** say he lacks the durability to start or project him as a reliever
- `Stm < 40`: note that the pitcher lacks the stamina to project as a starter and is better suited to shorter stints out of the bullpen

For hitters: lead with the best offensive tool, then defense, then the risk.
Vary your language — use synonyms for grade labels where natural (e.g. "a tick above average", "borderline plus", "well short of average", "legitimate plus-plus projection"). Avoid using "fringe-average" more than once per report.
If Acc = L, close with: "Additional scouting is needed for a complete picture."]

### Section 2 — Players to Watch

3-5 players who missed the top 15 but are worth monitoring. Favor young, high-ceiling prospects — players with a large gap between current grades and projection, especially those age 22 or younger. Players in the international complex are excluded — they lack the established baseline needed to evaluate meaningfully. Older players with a higher floor but lower ceiling should be deprioritized. These are names to revisit at the next evaluation — not ready to rank yet, but the upside is real.

For each, one short paragraph specific to that player — not a template. Cover: age, level, the one tool that makes them interesting, the specific thing holding them out of the top 15, and what needs to improve for them to crack it.

### Section 3 — System Snapshot

A brief descriptive summary of the farm system's current state. No recommendations — those belong in the org overview report. Cover:

- **Pipeline depth by position:** Which buckets have multiple ranked prospects vs. none at FV 45+
- **Level distribution:** Where the top-15 prospects are concentrated, and which levels are thin
- **Age distribution:** Average age of the top 15, count of high-ceiling prospects age ≤ 20
- **Old-for-level flags:** Levels with meaningful concentrations of age-inappropriate players

Keep it factual and concise — 3-4 short paragraphs. This section feeds the org overview report; it does not replace it.

---

## FV History Tracking

After each evaluation, append each ranked prospect's FV to `config/prospect_history.json`:

```json
[
  {"player_id": 1234, "name": "Chad Marshall", "date": "2033-04-22", "fv": 45, "level": "A-Short"},
  ...
]
```

On subsequent evaluations, look up prior entries by `player_id` and note movement:
- FV increased: developing as expected or ahead of schedule
- FV flat: on track, no new information
- FV decreased: regression, injury concern, or scouting accuracy correction

Surface FV movement in the scouting summary when it's meaningful (e.g. "+5 since last eval"). If a player is at AAA, age-appropriate or old for their level, and Ovr is within 5 of Pot, note in the scouting summary that they are knocking on the door of the big league club.

---

## Repeatable Process Checklist

Run in this order each evaluation cycle:

1. Read `config/state.json` — note the `game_date`
2. Check whether `prospect_fv` already has rows for that `game_date`. If it does, skip to step 5.
3. Only if data is missing or stale: `python3 -m statsplusplus.data.refresh [year]`, then `python3 -m statsplusplus.data.refresh state <game_date> [year]`
4. Run `python3 scripts/fv_calc.py` — computes FV for all prospects and surplus for all MLB players league-wide, writes to `prospect_fv` and `player_surplus`.
5. Run `python3 scripts/farm_analysis.py` — reads `prospect_fv` and produces `tmp/farm_scaffold_<game_date>.md` containing:
   - All 15 ranked prospect cards with computed FV, normalized grade tables, and FV movement flags
   - Players to Watch cards
   - Raw org data (bucket counts, level distribution, old-for-level counts) for use in Section 3
   - Existing summaries from `history/prospect_notes.json` pre-filled where available, with `[NEW PLAYER]` placeholders for first-time entries
5. Read the scaffold. For each prospect:
   - If marked `[EXISTING SUMMARY]` or `[FV STABLE]`: review the summary against the current grade table. If FV moved ±5 or more, rewrite it. Otherwise use as-is.
   - If marked with a rewrite flag (FV moved, developing, stagnant, or new season): rewrite the summary to reflect current grades, level, age, and any FV movement.
   - If marked `[NEW PLAYER]`: write a new scouting summary per the Output Format standards below.
6. Write Section 3 (System Snapshot) using the org data block at the bottom of the scaffold.
7. Assemble the final report and write it to `reports/<year>/farm_report_<game_date>.md`. Do not print the report to the terminal — confirm the file path and provide a brief bullet summary of key findings only.
8. For any player who received a new or updated summary, update their entry in `history/prospects.json`.

---

## Scouting Summaries — `history/prospects.json`

Hand-written scouting summaries and FV history are stored together in `history/prospects.json`, keyed by `player_id` (as a string). Each entry contains both the snapshot history and the scouting prose.

Each entry has the following shape:

```json
{
  "12345": {
    "name": "Player Name",
    "summary": "Full scouting paragraph for top-15 use.",
    "watch_summary": "Shorter paragraph for Players to Watch section.",
    "summary_date": "2033-04-22",
    "archived": false,
    "history": [
      {"date": "2033-04-22", "fv": 45, "fv_str": "45", "level": "A-Short", "bucket": "COF", "ovr": 28},
      {"date": "2033-10-15", "fv": 45, "fv_str": "45+", "level": "A",      "bucket": "COF", "ovr": 33}
    ]
  }
}
```

- `summary` is used for top-15 prospect cards
- `watch_summary` is used for the Players to Watch section; falls back to `summary` if absent
- When a player moves from the watch list into the top 15, write a full `summary` entry
- When a player drops off the list entirely, their entry remains in the file and will be reused if they return
- On FV movement of ±5 or more, review and update the summary to reflect the change



## Grade Labels

Use standard scouting vernacular in all prose. Never describe grades as "normalized."

| Grade | Label |
|---|---|
| 80 | Elite / Double-plus |
| 70 | Plus-plus |
| 60 | Plus |
| 55 | Above-average |
| 50 | Average |
| 45 | Fringe-average |
| 40 | Below-average |
| 30 | Well below-average |
| 20 | Poor |

Half-grade `+` (e.g. `55+`) = established floor at that grade with upside for the next.

Examples of correct usage:
- "A plus-plus fastball projecting to double-plus" → present 70, future 80
- "Fringe-average control that projects to average" → present 45, future 50
- "A plus glove behind the plate" → 60 defensive grade
- "Plus-plus raw power" → 70 power grade
- "An above-average hit tool" → 55 hit grade

---

## Notes

- Individual attributes (Cntct, Pow, Stf, etc.) are on **1-100 scale** — always normalize to 20-80, then round to nearest 5
- `Ovr` and `Pot` are already on the **20-80 scale** — do not normalize
- All grades in reports must be multiples of 5 — no exceptions, including Players to Watch blurbs
- Never describe grades as "normalized" in prose — use grade labels (plus, above-average, etc.)
- `Acc` is scouting accuracy (VH/H/A/L) — only mention it in prose when Acc = L, and frame it as "additional scouting is needed for a complete picture" rather than referencing the field directly. For all other accuracy levels, treat the grades as reliable without comment.
- Surplus value is shown on each prospect card for trade context. Do not reference it in scouting prose — it is a GM data point, not a scouting descriptor.
- Character fields on VL/L/N/H/VH scale: `Int`, `WrkEthic`, `Greed`, `Loy`, `Lead`
- Stats are unavailable for minor leaguers via the API — ratings + age-vs-level is the primary signal
- Roster `Pos` and `Role` fields are **numeric** — use `league_settings.json` `pos_map` and `role_map` to decode them (e.g. `Pos=1` → P, `Role=11` → SP, `Role=12/13` → RP)
- When building prospect history, capture `player_id` from the roster `ID` field at ranking time — do not leave it null. FV movement tracking relies on ID, not name
