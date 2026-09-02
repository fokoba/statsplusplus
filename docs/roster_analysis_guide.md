# MLB Roster Analysis Guide

## Purpose

This document defines the repeatable process for evaluating and presenting the
Anaheim Angels MLB roster. It produces a structured report covering current
performance, positional depth, contract health, and roster construction concerns.

---

## Eligibility

- Include Angels MLB players from the DB: `players` table, `level=1`, `team_id=44` or `parent_team_id=44`, `league_id >= 0` (excludes international complex players)
- Include players on the active 26-man roster only — do not include players
  currently on the IL unless explicitly noted
- Contract data comes from the `contracts` table filtered to `contract_team_id == 44` and `is_major = 1`

---

## DH Rule

This league uses a **universal DH** — all teams carry a designated hitter. The DH
is a legitimate everyday lineup slot, not a bench role or fallback.

When assessing roster construction:
- Account for the DH slot explicitly in the lineup depth section
- A player deployed primarily at DH should be evaluated on offense only — their
  defensive grades are irrelevant to their value
- A player with a poor defensive profile but strong offensive grades is a DH
  candidate, not a liability — do not flag their defense as a concern if they
  are playing DH
- Positional overcrowding (e.g. two quality outfielders competing for one OF spot)
  can be resolved by slotting one player at DH — factor this into roster
  construction analysis before flagging a logjam as a problem

---

## Ratings Normalization

Individual attribute ratings (`Cntct`, `Pow`, `Stf`, etc.) are stored on a **1-100 scale**.
Convert to 20-80 before presenting:

```
normalized = round(20 + (raw / 100) * 60)
```

Round to the nearest 5 for all prose and tables.

`Ovr` and `Pot` are **already on the 20-80 scale** — do not normalize them.

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

### Batter Tool Grades

| Tool | Field |
|---|---|
| Hit | `Cntct` |
| Power | `Pow` |
| Eye | `Eye` |
| K-Rate | `Ks` (higher = fewer strikeouts) |
| Gap | `Gap` |
| Speed | `Speed` |

**Catchers only:** Also show `CBlk` (blocking) and `CFrm` (framing) — normalize
to 20-80. Neither has a `Pot*` counterpart.

### Pitcher Tool Grades

Show the pitcher's **top 4 viable pitches** by present grade. A pitch is viable
if its present grade ≥ 25. Available pitch fields:

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

Also show `Vel` (velocity string), `Stf` (Stuff), `Mov` (Movement), and
`Ctrl` — averaged from `Ctrl_R` and `Ctrl_L`: `Ctrl = (Ctrl_R + Ctrl_L) / 2`.

**Stamina prose thresholds (never cite the raw number):**
- `Stm ≥ 50`: no comment needed — projects as a full starter
- `Stm 40–49`: note the pitcher profiles as a shorter-outing starter
- `Stm < 40`: note the pitcher lacks the stamina to start; better suited to the bullpen

### Height Conversion

Height stored in cm: `feet = int(cm / 30.48)`, `inches = round((cm % 30.48) / 2.54)`

---

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

---

## Performance Context

Stats are available for MLB players. Use them to contextualize ratings — a player
performing well above or below their ratings is worth flagging. Reference league
averages from `config/league_averages.json` when assessing performance.

**Key league average benchmarks (2033):**
- Batting: .255 AVG / .327 OBP / .426 SLG / .752 OPS
- Pitching: 4.53 ERA / 1.32 WHIP / 21.3% K rate / 8.7% BB rate

Early-season stats (< 50 AB / < 20 IP) should be treated as directional, not
definitive. Flag outliers but anchor the assessment in ratings.

---

## Contract Decoding

Contract fields in the `contracts` table:

- `season_year` = the first year of the contract (e.g. 2030)
- `years` = total contract length in years
- `current_year` = 0-indexed offset into the contract (0 = first year, 1 = second year, etc.)
- `salary0`–`salary{years-1}` = per-year salaries in order from year 0 to year N-1
- Current season salary = `salary[current_year]`
- Remaining years = `years - current_year`
- Remaining salaries = `salary[current_year]` through `salary[years-1]`
- Total remaining = sum of `salary[current_year]` through `salary[years-1]`
- AAV = total contract value (sum of all salary slots) / `years`

**Example:** `season_year=2030, years=4, current_year=2` means the contract runs
2030–2033. We are in year index 2 (2032). Remaining = 4-2 = 2 years (2032, 2033).
Current salary = `salary2`. AAV = (salary0+salary1+salary2+salary3) / 4.

Flag contracts as concerns when:
- A player's `Ovr` is more than 10 points below what the salary implies for their role
- A player is age 33+ with 3+ years remaining, or age 35+ with 2+ years remaining
- Total remaining exceeds $100M for a player with `Pot` at or near current `Ovr`
  (no development upside to justify the commitment)

**Option fields:** `last_year_team_option`, `last_year_player_option`,
`last_year_vesting_option` apply to the final contract year. The `next_last_year_*`
variants apply to the penultimate year. Buyout costs are in
`last_year_option_buyout` and `next_last_year_option_buyout`. The script maps
each option to its actual game year automatically.

---

## Output Format

### Section 1 — Lineup & Positional Depth

Group position players by position bucket (C, 1B, 2B, SS, 3B, LF/CF/RF). For each:

```
[Position]
[Starter Name] | Age [X] | Ovr [XX] | Pot [XX] | [Contract: Xyr $XM AAV, $XM remaining]
[Backup Name]  | Age [X] | Ovr [XX] | Pot [XX] | [Contract: Xyr $XM AAV, $XM remaining]

Tool Grades:
| Hit | Power | Eye | K-Rate | Gap | Speed | Fielding | Arm |
| --- | ----- | --- | ------ | --- | ----- | -------- | --- |
| XX  | XX    | XX  | XX     | XX  | XX    | XX       | XX  |

2033 Stats: [G / AB / AVG / OBP / SLG / HR / RBI / BB / K]

[2-3 sentence assessment. Cover: current performance vs. ratings, role security,
contract health, and one specific concern or strength. Do not use generic templates.
If a player is performing significantly above or below their ratings, say so and
note whether it is likely to persist. Flag any contract that represents a
meaningful roster construction risk.]
```

For the Fielding grade: use the positional composite rating (`C`, `SS`, `2B`, etc.)
normalized to 20-80. For Arm: use `OFA` (OF), `IFA` (IF), or `CArm` (C), normalized.

### Section 2 — Starting Rotation

For each starter, ordered by Ovr descending:

```
[Name] | Age [X] | Ovr [XX] | Pot [XX] | [Contract: Xyr $XM AAV, $XM remaining]

| Pitch1 | Pitch2 | Pitch3 | Pitch4 | Velocity | Stuff | Movement | Control |
| ------ | ------ | ------ | ------ | -------- | ----- | -------- | ------- |
| XX     | XX     | XX     | XX     | XX-XX    | XX    | XX       | XX      |

2033 Stats: [GS / IP / ERA / WHIP / K / BB]

[2-3 sentence assessment. Lead with the best pitch and velocity. Address the
supporting arsenal, then control. Note stamina in prose terms only. Flag any
meaningful gap between current ERA and underlying ratings — early-season noise
vs. a real concern. Address contract health for any player with 3+ years remaining.]
```

**Pitcher field definitions (for prose use):**
- **Stuff** (`Stf`) — overall arsenal quality; drives strikeouts. Relievers receive an engine-level stuff bonus — RP stuff grades are already inflated relative to what the same pitcher would show as a starter.
- **Movement** (`Mov`) — composite of ground ball rate and pitcher BABIP; reflects ability to limit hard contact and home runs
- **Control** (`Ctrl`) — ability to throw strikes and avoid walks

### Section 3 — Bullpen

Same format as the rotation. Order: closer first, then by Ovr descending.

Note the reliever stuff bonus in any assessment where a reliever's stuff grade
appears inflated relative to their results — the bonus is real but it means
a reliever's 60 Stuff is not equivalent to a starter's 60 Stuff.

### Section 4 — Contract Health Summary

A concise table of all multi-year commitments:

```
| Player | Pos | Age | Ovr | Pot | AAV | Yrs Remaining | Total Remaining | Flag |
| ------ | --- | --- | --- | --- | --- | ------------- | --------------- | ---- |
```

Flag column values:
- `OK` — contract is reasonable for the player's current and projected value
- `WATCH` — player is aging, declining, or underperforming relative to salary
- `CONCERN` — contract is a meaningful roster construction problem now
- `EXTENSION` — on a 1-year deal with Ovr ≥ 50. The contract data does not expose
  service time or arb eligibility — a player may be pre-arb, in arbitration, or on
  a negotiated 1-year deal. Treat all of these the same: if the player is worth
  keeping, pursue an extension before they reach free agency or costs escalate
  further through arb. The league minimum is $825K — players at that salary are
  likely pre-arb, but that does not reduce the urgency of locking up good ones early.

The Options column surfaces any team, player, or vesting options with the year,
salary, and buyout cost. Format: `TEAM 2034 $10M (bo $1.5M)`. A `—` means no
options. Options affect how to interpret the remaining commitment:
- **Team option:** The guaranteed exposure ends one year earlier than `Yrs Rem`
  suggests — the team can walk away for the buyout cost.
- **Player option:** The player controls the final year. If they're productive,
  they opt in; if not, the team is off the hook — but there is no buyout protection
  for the Angels.
- **Vesting option:** Automatically triggers if the player meets a performance
  threshold. Treat as likely guaranteed unless the player is clearly declining.

Sort by Total Remaining descending.

### Section 5 — Roster Construction Assessment

3-4 paragraphs covering:

- **Strengths:** The 2-3 positions or players that represent genuine roster advantages
- **Weaknesses:** The 2-3 positions or roles where the roster is thin or below average
- **Contract risks:** The 1-2 contracts that most constrain future flexibility
- **Extension priorities:** Players on short deals who should be locked up before hitting free agency

Keep it analytical and specific — every claim should be traceable to a grade,
a stat, or a contract figure from the data. No generic observations.

---

## Repeatable Process Checklist

Run in this order each evaluation cycle:

1. Read `config/state.json` — note the `game_date`
2. Check whether `angels/` data files already exist on disk for that game date.
   If they do, **skip the refresh entirely** and proceed to step 4
3. Only if data is missing or stale: `python3 -m statsplusplus.data.refresh [year]`, then
   `python3 -m statsplusplus.data.refresh state <game_date> [year]`
4. Run `python3 scripts/roster_analysis.py` — this produces
   `tmp/roster_scaffold_<game_date>.md` containing:
   - All position players and pitchers with normalized grade tables pre-computed
   - Grade callout blocks identifying best/worst tools and explicit warnings against misreading any grade ≥ 60 or ≤ 40
   - 2033 stat lines with league-average deltas
   - Contract table with flags pre-populated (AAV = total value / total years — not current-year salary)
   - `[NEW PLAYER]` placeholders for any player not in `history/roster_notes.json`
   - `[EXISTING SUMMARY]` with prior text pre-filled for returning players
   - `[EXISTING SUMMARY — Ovr moved ±X, consider rewrite]` when Ovr changed ±5 or more
   - `[EXISTING SUMMARY — new season (YYYY → YYYY), consider rewrite]` at season boundaries
5. Read the scaffold. For each player:
   - **The grade callout block is authoritative.** If it says a grade is plus, the prose must not describe it as average or below — and vice versa. Early-season stats that contradict a grade should be noted as small-sample noise, not used to override the grade.
   - **AAV in prose must match the contract line in the scaffold**, not the current-year salary. Many contracts are backloaded.
   - If marked `[EXISTING SUMMARY]`: review against current grades and stats.
     Rewrite if Ovr moved ±5 or more, or if the stat line tells a materially
     different story than the prior summary.
   - If marked with a rewrite flag (Ovr moved or new season): rewrite the summary
     to reflect current age, contract status, stats, and any grade changes.
   - If marked `[NEW PLAYER]`: write a new assessment per the Output Format above.
6. Write Section 5 (Roster Construction Assessment) using the contract table
   and positional depth picture from the scaffold.
7. Assemble the final report and write it to `reports/roster_report_<game_date>.md`.
   Do not print the report to the terminal — confirm the file path and provide
   a brief bullet summary of key findings only.
8. For any player who received a new or updated summary, update their entry in
   `history/roster_notes.json` — set `summary`, `summary_date`, `last_ovr`, and
   `last_year` to the current values.

---

## Player Notes — `history/roster_notes.json`

Hand-written assessments are stored in `history/roster_notes.json`, keyed by
`player_id` (as a string). The analysis script uses these in preference to auto-generated prose.

Each entry:

```json
{
  "12345": {
    "name": "Player Name",
    "summary": "Assessment paragraph for the roster report.",
    "summary_date": "2033-04-22",
    "last_ovr": 55,
    "last_year": 2033
  }
}
```

The scaffold script uses `last_ovr` and `last_year` to detect when a summary
needs refreshing:
- **Ovr moved ±5:** player's ratings changed meaningfully — rewrite
- **New season:** age, contract, and stats have all reset — rewrite
- **New player:** no existing entry — write from scratch

On subsequent evaluations, look up prior entries by `player_id` and note movement:
- Ovr increased: developing or returning from injury
- Ovr flat: on track, no new information
- Ovr decreased: regression or aging

Surface Ovr movement in the assessment when it is meaningful (e.g. "down 5 since
last eval"). If a player's stats are significantly outperforming or underperforming
their ratings over a meaningful sample, note it.

---

## Notes

- **Never print the full report to the terminal.** Write it to `reports/roster_report_<game_date>.md` and confirm the file path. Provide only a brief bullet summary of key findings in the terminal.
- `Ovr` and `Pot` are already on the 20-80 scale — do not normalize
- All grades in reports must be multiples of 5 — no exceptions
- `Acc` is scouting accuracy — only mention it in prose when `Acc = L`, framed
  as "additional scouting is needed for a complete picture"
- Stats are available for MLB players — use them. They are the primary signal
  for in-season performance; ratings are the primary signal for projection
- Roster `Pos` and `Role` fields are numeric — use `league_settings.json`
  `pos_map` and `role_map` to decode them
- Never read raw data files directly during a roster analysis run. The scaffold script processes all raw data — the agent's
  input is `tmp/roster_scaffold_<date>.md` only.
- If `scripts/roster_analysis.py` is not found, stop immediately. Do not attempt
  to replicate its logic by reading raw data files. Report the missing script and
  wait for instructions.
