# Unified Player Evaluation Model — Design Document

## Purpose

This document describes the proposed unified evaluation model for Stats++. The goal
is to replace the current dual-system approach (prospect FV model + MLB contract surplus
model) with a single evaluation framework that smoothly transitions from tool-based
projection to stat-based evidence as a player accumulates professional track record.

The unified model should:
- Eliminate the hard cutoff at the prospect/MLB boundary
- Produce one surplus number per player that reflects their true value at any point in their career
- Handle all edge cases (spring training invites, 20-PA rookies, AAA veterans, established stars)
  without special-casing
- Not degrade the quality of evaluations for any player type compared to the current system

---

## Philosophy

### Hierarchy of Evidence

Player evaluation draws on multiple information sources. Their reliability and weight
shift as the player progresses through professional baseball:

| Evidence Type | Reliability at Low-A | Reliability at AAA | Reliability at MLB (2+ yr) |
|---------------|---------------------|-------------------|---------------------------|
| Raw tools (scouting grades) | ★★★★★ | ★★★★ | ★★★ |
| MiLB stats (rate-based, level-adjusted) | ★★ | ★★★★ | N/A |
| MLB stats (production-based) | N/A | N/A | ★★★★★ |
| Development trajectory (ratings history) | ★★★ | ★★★★ | ★★ |
| Age/level context | ★★★★★ | ★★★★ | ★★ |

The unified model respects this hierarchy: tools are the foundation, MiLB stats
calibrate our confidence in the tools, and MLB stats progressively become the
primary projection as sample size grows.

### Two Roles for Statistics

**Role A — Tool Calibration (MiLB stats)**

MiLB statistics answer: "Are the tools playing in games?" They don't directly
project MLB production — the level gap is too large and the context too different.
Instead, they cause us to adjust our belief in what the tools will become:

- A 70-power prospect slugging .350 in AA → maybe the power is theoretical, not game-usable
- A 50-tool prospect posting elite K/BB in AA → maybe the tools are undergraded, or he's figured
  something out that ratings haven't caught yet

MiLB stats modify:
1. Our ceiling belief (Performance-Adjusted Ceiling)
2. Our risk assessment (stat risk modifier on development confidence)
3. Our current ability estimate (composite score blend, capped at 25%)

They never directly produce a WAR projection.

**Role B — Direct Production Evidence (MLB stats)**

MLB statistics answer: "What has this player actually produced at the highest level?"
Once a player has meaningful MLB time, his stat line IS the projection — weighted by
recency, adjusted for context (park, era, role), and fed through aging curves.

MLB stats:
1. Directly project future WAR via weighted seasonal averages
2. Override tool-based projections as sample size grows
3. Provide the basis for contract valuation and trade decisions

### The Transition Gradient

The core innovation of the unified model: instead of switching from Role A to Role B
at a single moment, we define a **stat confidence** function that smoothly blends between
the tool-based projection and the stat-based projection:

```
stat_confidence = 0.0 → Pure tool-based (prospect with no MLB time)
stat_confidence = 0.3 → Mostly tools, stats informing
stat_confidence = 0.7 → Mostly stats, tools as safety net
stat_confidence = 1.0 → Pure stat-based (established veteran)
```

---

## Information Sources

### 1. Tool-Based Assessment (Scouting Ratings)

**What it provides:** A snapshot of the player's physical abilities and projected
development as assessed by in-game scouting.

**Components:**
- Current ratings (the 20-80 scale tools: contact, power, eye, speed, stuff, movement, control, etc.)
- Potential ratings (projected ceiling for each tool)
- Defensive ratings (positional grades)
- Pitch repertoire (individual pitch quality, current and potential)
- Character traits (work ethic, intelligence, leadership)
- Scouting accuracy (confidence in the grades themselves)

**How it informs evaluation:**
- **Composite score**: Tool-weighted current ability on 20-80 scale. Position-specific
  weights derived per-league via OLS regression against WAR. Includes sub-MLB floor
  penalties, defensive value, and positional adjustments.
- **Ceiling score**: Same methodology applied to potential ratings. Represents the
  maximum production this player achieves if all tools develop fully.
- **FV grade**: Probability-weighted expected peak outcome. Incorporates development
  probability (age-specific closure rates, bust discounts), positional value, and
  ceiling quality relative to MLB positional medians.

**When it dominates:** Always the foundation. For pure prospects (0 MLB PA), this is
100% of the projection. For established veterans, it still informs aging projections
and role-change scenarios.

### 2. MiLB Performance Data

**What it provides:** Evidence of whether tools translate to in-game performance
against professional competition at various levels.

**Key signals (hitters):** K%, BB%, ISO, wRC+ vs level, BABIP context
**Key signals (pitchers):** K/9, BB/9, HR/9, ERA vs FIP (contact management), SwStr% proxy

**How it informs evaluation:**

These operate as **calibrators** on the tool-based projection, not as independent
production projections:

1. **Performance-Adjusted Ceiling (PAC):** Adjusts ceiling ±6 points based on
   MiLB production vs tools. Level-discounted, age-context-aware. Young players
   outperforming get more ceiling credit; old players underperforming get penalized.

2. **Risk modifier:** Adjusts development confidence ±0.12. Outperforming tools
   in MiLB = lower risk (development is occurring). Underperforming = higher risk.

3. **Composite blend (evaluation engine):** MiLB stat signal blended into composite
   at up to 25% weight. Provides "what is this player doing right now" context beyond
   what tool grades capture. Fades as MLB stats accumulate.

**Level discounting:** MiLB stats are weighted by the predictive power of that level
for MLB outcomes. AAA stats get ~70% discount relative to MLB; Rookie ball gets ~15%.
These discounts are calibrated per-league from cross-level progression data.

**Temporal weighting:** Recent MiLB seasons weighted higher (1.0 current, 0.7 prior, 0.4 two years ago).

**When it dominates:** Never dominates independently. Maximum influence for upper-level
prospects (AA/AAA) with large samples and no MLB time. Completely superseded once
MLB stats provide direct evidence.

### 3. MLB Performance Data

**What it provides:** Direct evidence of production at the highest level. The most
predictive single input for future MLB production.

**Key signals:** WAR (or WAR components), blended across a recency-weighted window.
For pitchers: blended WAR and RA9-WAR (captures both talent and outcomes).

**How it informs evaluation:**

Directly produces a **peak WAR projection** via `stat_peak_war()`:
- 4-year window with weights [3, 3, 2, 1] (most recent first)
- Partial-season scaling (prevents a 50-IP half-season from anchoring the projection)
- Role-change blending (SP↔RP transitions handled via discount factors)
- Rate normalization (WAR per season equivalent)

**Interaction with tools:** When stats and tools diverge:
- Player pre-peak with tools > stats: blend favoring tools (player likely still developing)
- Player post-peak with stats > tools: blend favoring stats with increasing tool weight
  as age advances (tools predict upcoming decline before stats show it)

**When it dominates:** Once stat_confidence approaches 1.0 (~400+ PA, 120+ IP across
2+ seasons), the stat-based projection IS the projection. Tools only contribute to
aging curve shape and role-change scenarios.

### 4. Development Context

**What it provides:** Information about the player's trajectory and timeline that
affects how we weight other evidence.

**Components:**
- **Age relative to level:** Young-for-level players get more development credit.
  Old-for-level players face steeper discounts.
- **Level progression speed:** Fast movers (skipping levels, promoting early) signal
  that the organization sees something the stats may not fully capture.
- **Ratings history trajectory:** Improving ratings over time signal development;
  stagnant or declining ratings signal a ceiling being reached.
- **Service time:** Exact MLB/pro service days inform team control calculations.

**How it informs evaluation:**
- `dev_discount`: Level and age-adjusted discount on surplus value. Captures time value
  of money AND residual development risk beyond what FV/risk already encode.
- `certainty_mult`: Ratio of current ability to ceiling. Higher realization = higher certainty.
- `years_to_MLB`: Derived from current level. Determines when projected WAR begins accruing.
- Aging curves: Position-specific decline rates applied to WAR projections year-by-year.

### 5. Contract and Control Context

**What it provides:** The cost side of the surplus equation — how much the player
will be paid over their remaining team control.

**Components:**
- Current contract (guaranteed years, salary, options, incentives)
- Service time → estimated arb/FA timeline
- Extension agreements
- League financial structure ($/WAR, minimum salary, arb model)

**How it informs evaluation:**
- Surplus = Value - Cost. The evaluation model determines Value (WAR × $/WAR);
  the contract model determines Cost (salary over remaining control).
- For pre-arb players: cost is near zero (league minimum), maximizing surplus
- For arb players: cost rises with production, partially offsetting surplus gains
- For signed players: cost is known (contract guarantees)

---

## The Unified Evaluation Pipeline

### Step 1: Compute Tool-Based Projection

For every player, regardless of level or MLB experience:

```
composite_score = tool_weighted_composite(current_ratings, position, MiLB_calibration)
ceiling_score = tool_weighted_composite(potential_ratings, position)
fv_grade = calc_fv(composite, ceiling, age, level, risk_factors)
tool_war = peak_war_from_fv(fv_continuous, bucket)
```

MiLB stats contribute here through their calibration role (PAC, risk modifier,
composite blend). The output is a probability-weighted peak WAR projection
based on what the tools say this player should become.

### Step 2: Compute Stat-Based Projection (If Available)

For players with MLB statistics:

```
stat_war = stat_peak_war(pid, bucket, batting_history, pitching_history)
```

This is a recency-weighted average of actual MLB WAR production, rate-normalized
and role-adjusted. Returns None if no qualifying MLB seasons exist.

### Step 3: Determine Stat Confidence

```
stat_confidence = f(career_mlb_pa, career_mlb_ip)
```

This function determines how much weight to give the stat-based projection
relative to the tool-based projection:

| Career MLB PA | Career MLB IP | stat_confidence | Interpretation |
|--------------|---------------|-----------------|----------------|
| 0 | 0 | 0.00 | Pure prospect — tools only |
| 50 | 15 | ~0.05 | Cup of coffee — tools dominant, stats as minor signal |
| 130 | 50 | ~0.25 | Rookie threshold — stats meaningful but tools still primary |
| 250 | 80 | ~0.50 | Half season — balanced blend |
| 400 | 120 | ~0.75 | Full season+ — stats primary |
| 600+ | 180+ | 1.00 | Established — stats dominate |

**Recency adjustment:** stat_confidence should consider how recent the MLB time is.
A player with 300 PA two years ago who's been in AAA since may warrant lower
confidence than a player with 300 PA last month.

### Step 4: Blend WAR Projections

```
if stat_war is not None:
    peak_war = (1 - stat_confidence) × tool_war + stat_confidence × stat_war
else:
    peak_war = tool_war
```

**Pre-peak development case:** When tools project higher WAR than stats and the
player is pre-peak, this is expected (they haven't fully developed yet). The tool
projection already accounts for this via FV's development probability. No special
handling needed — the blend naturally weights toward tools when stat_confidence
is low (young player, limited sample).

**Post-peak decline case:** When stats project higher WAR than tools and the player
is aging, tools are predicting upcoming decline. stat_confidence stays high (veteran)
but the tool-based component (even at small weight) pulls the projection toward
a steeper decline curve than stats alone suggest.

### Step 5: Project Year-by-Year WAR

Using `peak_war` from Step 4, project annual WAR over the remaining control period:

```
for each year in control_period:
    war[year] = peak_war × aging_mult(age + year, bucket) × development_ramp(year)
```

**Development ramp:** For players not yet at peak (pre-peak age, stat_confidence < 1.0):
- Project composite growth toward ceiling at the league's calibrated closure rate
- Convert each year's projected composite to WAR
- This replaces both the current `dev_ramp` (MLB model) and `PROSPECT_WAR_RAMP` (prospect model)
  with a single principled mechanism

**Aging curve:** Position-specific multipliers applied uniformly regardless of
player type. A 23-year-old and a 33-year-old use the same curve — the difference
is where they sit on it.

### Step 6: Compute Surplus

```
for each year in control_period:
    market_value[year] = war[year] × $/WAR
    salary[year] = estimated_salary(service_time, arb_model, contract)
    surplus[year] = market_value[year] - salary[year]

raw_surplus = sum(surplus[year] × time_discount[year])
```

### Step 7: Apply Evaluation Discounts (Fading)

The prospect-style discounts (dev_discount, certainty_mult) **fade out** as
stat_confidence increases:

```
effective_dev_discount = lerp(dev_discount, 1.0, stat_confidence)
effective_cert_mult = lerp(cert_mult, 1.0, stat_confidence)
scarcity_mult = scarcity_mult  # Positional value doesn't fade

final_surplus = raw_surplus × effective_dev_discount × effective_cert_mult × scarcity_mult
```

Rationale:
- **Dev discount fading:** Once a player has proven himself at the MLB level (high
  stat_confidence), the "will he make it?" discount is no longer relevant. He's already there.
- **Certainty mult fading:** The certainty multiplier penalizes unrealized potential.
  But if a player has MLB stats, his production IS his certainty — the gap between
  composite and ceiling matters less when you have direct evidence of output.
- **Scarcity staying:** Positional value is intrinsic — a SS is more valuable than a 1B
  regardless of how much track record they have.

---

## Edge Cases and Their Resolution

### Spring Training Invite (Level 1, 0 PA)

- stat_confidence = 0.0
- Evaluation: pure tool-based (FV grade, prospect surplus model)
- No 50% flat discount — the FV already encodes development probability
- Result: valued identically to a AAA prospect of same tools/age

### First Callup (20 PA)

- stat_confidence ≈ 0.05
- Blend: 95% tools, 5% stats
- The tiny stat sample barely moves the needle — a .400 or .100 line in 20 PA
  gets almost no weight
- Result: essentially still a prospect valuation, but the stat signal exists

### Promising Rookie (150 PA, 2.5 WAR pace)

- stat_confidence ≈ 0.35
- Blend: 65% tools, 35% stats
- If tools and stats agree: projection is confident
- If tools say 4.0 WAR but stats say 2.5: blend is ~3.5 (tools tempered by reality)
- Dev discount partially faded (×0.65 of original discount value)
- Result: smooth transition, no cliff

### Established Second-Year Player (600 PA, 3.0 WAR)

- stat_confidence ≈ 1.0
- Blend: essentially pure stat-based
- Dev discount fully faded (×1.0 = no discount)
- Result: identical to current MLB model

### 28-Year-Old Veteran (3000 PA)

- stat_confidence = 1.0
- Pure stat_peak_war projection with aging curve
- Result: identical to current MLB model (no regression)

### AAA Veteran (Age 27, 0 MLB PA, FV below threshold)

- stat_confidence = 0.0
- Tool-based projection using composite/ceiling
- FV grade may be below 40 (no surplus value)
- Result: correctly valued as a fringe player with minimal surplus

### Player Returning from Extended Absence

- stat_confidence based on career PA, but recency factor reduces it
- A player with 400 PA three years ago might get stat_confidence of 0.5 instead of 0.75
- Allows tools to inform the projection more heavily when the stat history is stale
- Result: reasonable blend that accounts for uncertainty after time away

---

## FV Grade in the Unified System

FV grades continue to exist and serve their current purpose:

- **Communication tool:** "This is an FV 55 prospect" is understood by baseball people
- **Risk labeling:** Low/Medium/High/Extreme captures development probability distribution
- **Input to WAR projection:** `peak_war_from_fv()` remains the mapping from FV to expected peak WAR

FV is NOT removed or decomposed. It remains a probability-weighted expected outcome
that already handles the difficulty of projecting young, high-variance players. The
unified model simply uses it as the tool-based WAR projection source and lets MLB
stats gradually supplement (and eventually replace) it as evidence accumulates.

---

## Output Schema

One unified table replaces both `prospect_fv` and `player_surplus`:

```sql
CREATE TABLE player_evaluation (
    player_id INTEGER,
    eval_date TEXT,

    -- Identity
    name TEXT,
    bucket TEXT,
    age INTEGER,
    level TEXT,
    team_id INTEGER,
    parent_team_id INTEGER,

    -- Tool Assessment
    composite INTEGER,         -- Current tool-weighted composite (20-80)
    ceiling INTEGER,           -- Tool-weighted ceiling score (20-80)
    fv INTEGER,                -- FV grade (rounded to 5s)
    fv_str TEXT,               -- Display string ("55", "50+")
    fv_continuous REAL,        -- Pre-rounding FV for interpolation
    risk TEXT,                 -- "Low" / "Medium" / "High" / "Extreme"

    -- WAR Projection
    tool_war REAL,             -- peak WAR from tool-based projection
    stat_war REAL,             -- peak WAR from stat history (NULL if no MLB stats)
    stat_confidence REAL,      -- 0.0–1.0 blend weight
    peak_war REAL,             -- final blended peak WAR projection

    -- Surplus
    surplus INTEGER,           -- unified surplus value (dollars)
    surplus_yr1 INTEGER,       -- year-1 surplus only (trade deadline value)

    -- Control
    years_control INTEGER,     -- estimated remaining team control
    ctrl_type TEXT,            -- 'pre-arb' / 'arb' / 'contract' / 'extension'

    PRIMARY KEY (player_id, eval_date)
);
```

### Backward Compatibility

During migration, views can replicate the old tables:

```sql
CREATE VIEW prospect_fv AS
SELECT player_id, eval_date, fv, fv_str, level, bucket,
       surplus AS prospect_surplus, risk, fv_continuous
FROM player_evaluation
WHERE stat_confidence < 0.5 AND level != 'MLB';

CREATE VIEW player_surplus AS
SELECT player_id, eval_date, name, bucket, age, composite AS ovr,
       fv, fv_str, surplus, surplus_yr1, level, team_id, parent_team_id
FROM player_evaluation;
```

---

## Calibration Requirements

The unified model introduces one new parameter to calibrate:

- **stat_confidence curve:** The mapping from career PA/IP to confidence weight.
  Initially use a simple ramp (PA/400, IP/120). Validate by comparing:
  - Does a 200-PA player's unified surplus match what a human GM would pay?
  - Do the crossover players (currently in both tables) produce sensible unified values?
  - Is the phaseout of prospect discounts smooth and non-jarring?

Existing calibrated parameters remain unchanged:
- Tool weights per position (from OLS regression)
- FV_TO_PEAK_WAR tables (per position)
- COMPOSITE_TO_WAR tables (per position)
- Gap closure rates and expected gap tables (per league)
- Aging curves (pitcher vs hitter)
- Level discounts for MiLB stats
- Arb salary model parameters
- Scarcity multipliers

---

## Implementation Phases

### Phase 1: Build and Validate (Non-Destructive)

- Implement `unified_surplus()` as a new function alongside existing models
- Run on all players in EMLB and VMLB
- Compare outputs to existing models for:
  - Pure prospects (should match prospect_surplus closely)
  - Established MLB (should match contract_value closely)
  - Crossover players (should produce sensible intermediate values)
- Identify cases where the unified model is clearly worse and iterate

### Phase 2: Replace Storage

- Switch `fv_calc.py` to write to `player_evaluation` table
- Create backward-compatible views for `prospect_fv` and `player_surplus`
- Verify all consumers (web UI, CLI tools, trade calculator) work via views

### Phase 3: Update Consumers

- Simplify `trade_calculator.py` — one `value_player()` path instead of two
- Update player page display logic — one valuation section, not conditional prospect/MLB
- Update trade_targets, trade_assets, free_agents to use unified table
- Remove dead code paths for the dual-model approach

### Phase 4: Tune and Polish

- Profile edge cases reported by users
- Adjust stat_confidence curve based on real-world trade validation
- Consider recency adjustment to stat_confidence
- Add development ramp refinements (project composite growth year-by-year)

---

## Open Questions

1. **stat_confidence curve shape:** Linear ramp (PA/400)? Square root (√PA/√400)?
   Sigmoid (slow start, fast middle, slow end)? Needs empirical validation against
   "what feels right" for players at various sample sizes.

2. **Recency in stat_confidence:** Should a player with 400 PA two years ago and
   0 PA since get stat_confidence = 0.75 or lower? If lower, how much does staleness
   reduce confidence?

3. **Development ramp for partially-established players:** How to project composite
   growth toward ceiling for a player with stat_confidence = 0.4? Use the tool-based
   closure rate? Or let the stat trend inform the growth rate?

4. **Spring training / option year edge cases:** A player optioned to AAA mid-season
   is level != 1 but has MLB stats. stat_confidence should be based on career MLB
   stats, not current level. Verify the implementation handles this correctly.

5. **Two-way players:** How does stat_confidence work when a player has PA AND IP?
   Separate confidence per role? Combined?

6. **Extension / trade context:** Does the unified model need to handle "what if this
   player is traded to a team that uses him differently?" Or is that out of scope
   (trade calculator handles it separately)?
