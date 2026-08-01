# MiLB Stats Integration — Design Spec

## Objective

Integrate minor league statistics into the player evaluation pipeline so that
MiLB production informs composite scores, FV grades, and risk labels —
creating a seamless transition from prospect evaluation to MLB evaluation
with no cliff or hard cutoff.

## Current State

**Two hard-separated evaluation paths:**

1. **Prospects (level ≠ 1):** Pure tool evaluation. `composite_score` comes
   from weighted tool ratings only. `calc_fv()` uses composite + ceiling +
   age + gap closure curves to produce FV grade and risk label. Zero stat
   influence.

2. **MLB (level = 1):** Tool score blended with stat performance.
   `compute_composite_mlb()` blends `tool_only_score` with OPS+ or ERA-
   from qualifying MLB seasons (130+ AB / 40+ IP). Blend weight: 20% (1
   season) → 35% (2) → 60% (3). Young players get a reduced blend factor.

**The cliff:** A player called up with 0 qualifying MLB PA gets pure-tool
evaluation (same as when he was in the minors). Once he hits 130 AB, a
single season gets 20% weight. All prior MiLB production is discarded.

## Empirical Findings (VMLB 2034)

Correlation between MiLB level-relative OPS+ and tool-based composite:

| Level | r | n | Notes |
|-------|---|---|-------|
| AAA | 0.46 | 400 | Moderate — stats add ~21% explanatory power |
| AA | 0.51 | 410 | Strongest signal — development level, less noise |
| A | 0.40 | 767 | Weaker — younger, more noise, tools dominating |

**Age-for-level at AAA:**
- Young performers (≤23, OPS+ > 115): avg composite 49.5, avg ceiling 51.4
- Young underperformers (≤23, OPS+ < 85): avg composite 44.7, avg ceiling 47.5
- Correlation is similar for young vs old (0.46 vs 0.44), but young outperformers
  tend to have higher ceilings — their stats confirm development trajectory

**Key insight:** MiLB stats at AAA/AA have a meaningful but moderate correlation
with tool grades. They're strong enough to add value (confirming or contradicting
tools) but not strong enough to replace tools as the primary signal.

## Design Philosophy

**Mirror real-life scouting:**
- Tools are the primary projection — "what can this player become?"
- Stats are confirming evidence — "is the development trajectory matching?"
- Higher levels = more reliable signal (AAA closer to MLB, less noise)
- Larger samples = more reliable (400 PA > 100 PA)
- Young-for-level dominance is a strong positive signal
- Old-for-level poor performance is a negative signal
- Stats should refine confidence in tools, not override them

**Continuous transition:**
- MiLB stats contribute at a discounted rate relative to MLB
- As MLB PA accumulate, MiLB contribution naturally fades (older, recency-weighted out)
- A prospect with 0 MLB PA but 500 AAA PA should get some stat influence
- A player with 300 MLB PA should have MiLB contribution nearly irrelevant

## Architecture

### Where It Lives

**`evaluation_engine.py`** — extends the existing stat blending pathway.

Currently `_load_qualifying_stat_seasons()` only pulls from `mlb_batting_stats` /
`mlb_pitching_stats`. The change adds a parallel `_load_milb_stat_seasons()` that
pulls MiLB data and normalizes it to a common "MLB-equivalent" scale.

Both feed into a unified stat signal that gets blended via the existing
`compute_composite_mlb()` function (which will be generalized to work for any
player with stat data, not just level=1 players).

**The `level == 1` gate in `_run_impl()` gets removed.** Any player with
sufficient stat history (MLB or MiLB) gets stat blending. The blend weight
is determined by the total accumulated "effective sample size" across all levels.

### Data Flow

```
Player's stat history across levels
    ↓
Per-level normalization (OPS+/ERA- relative to level peers)
    ↓
Level discount applied (MLB=1.0, AAA=0.70, AA=0.50, A=0.30, Rk=0.10)
    ↓
Recency weighting (3×/2×/1× for most recent 3 seasons)
    ↓
Combined into single "stat signal" on 20-80 scale
    ↓
Blend weight from effective sample size (0% at 0 eff. PA → 60% at 600+ eff. PA)
    ↓
composite_score = tool_only × (1 - blend) + stat_signal × blend
```

### Level Discount Factors

These represent how much a level's stats translate to MLB-equivalent production.
Initial estimates (to be calibrated per-league):

| Level | Hitter Discount | Pitcher Discount | Rationale |
|-------|----------------|------------------|-----------|
| MLB (1) | 1.00 | 1.00 | Ground truth |
| AAA (2) | 0.65–0.75 | 0.60–0.70 | Strong translation, ~r=0.65 to MLB |
| AA (3) | 0.45–0.55 | 0.40–0.50 | Development league, reliable signal |
| A (4) | 0.25–0.35 | 0.20–0.30 | High noise, tool development focus |
| Rookie (6) | 0.10–0.15 | 0.05–0.10 | Mostly noise, only extreme outliers signal |

**Calibration method:** For each league, use players who have stats at level X
AND subsequent MLB stats. Regress MiLB OPS+ (relative to level peers) against
MLB composite score and/or MLB OPS+. The regression coefficient approximates
the discount factor. Requires multi-season MiLB data for robust calibration —
initial run uses reasonable defaults, refined as seasons accumulate.

### Effective Sample Size

The "effective PA" determines blend weight:

```python
effective_pa = Σ (PA_at_level × level_discount × season_weight)
```

Where `season_weight` decays with recency (current year = 1.0, prior year = 0.7,
2 years ago = 0.4).

Blend weight schedule:

| Effective PA | Blend Weight | Typical scenario |
|-------------|-------------|------------------|
| 0 | 0% | No stats (pure tools) |
| 50 | 5% | Partial season low minors |
| 100 | 10% | Half-season A-ball |
| 200 | 20% | Full AAA season or equiv |
| 350 | 35% | Multiple MiLB seasons |
| 500 | 50% | Extensive MiLB + some MLB |
| 650+ | 60% | Established MLB (≈ current max) |

The curve is smooth (e.g., `blend = min(0.60, effective_pa / 1100)`).

### Normalization

**Hitters:** Level-relative OPS+ using per-league averages computed during
refresh (stored in `league_settings.json` or a new `milb_averages` table).

```
level_ops_plus = 100 × (OBP/lg_OBP + SLG/lg_SLG - 1)
```

Then converted to 20-80 scale using existing `stat_to_2080()`.

**Pitchers:** Level-relative ERA- (or FIP- if derivable from K/BB/HR/IP).

```
level_era_minus = ERA / lg_ERA × 100
inverted_signal = 200 - era_minus
```

Then converted via `pitcher_stat_to_2080()`.

### Young-Player Blend Discount (Preserved)

The existing mechanism in `compute_composite_mlb()` that reduces blend weight
for young players below peak age still applies:

```python
if player_age < peak_age and tool_score > stat_signal:
    age_factor = max(0.3, 1.0 - (peak_age - player_age) * 0.1)
    blend_weight *= age_factor
```

This means a 20-year-old whose tools are better than his stats (expected for
a developing prospect) gets the blend naturally dampened — tools still dominate.
But a 20-year-old whose stats EXCEED his tools (rare, high-signal) gets less
dampening — the stats carry weight because they confirm development.

### Risk Label Interaction

MiLB stats should modulate risk when the signal is strong:

**Risk reduction (stats confirm development):**
- Player is young for level (≤ norm_age - 1) AND
- OPS+/ERA- is in top quartile at level AND
- Sample is sufficient (100+ effective PA)
- Effect: reduce `dev_confidence` multiplier by one band (e.g., High → Medium)

**Risk increase (stats contradict tools):**
- Player is old for level (≥ norm_age + 2) AND
- OPS+/ERA- is in bottom quartile at level AND
- Sample is sufficient (150+ effective PA, higher threshold for negative signal)
- Effect: increase risk by one band (e.g., Medium → High)

This is applied in `calc_fv_v2()` as a modifier to `dev_confidence` before
the risk classification thresholds are applied.

### FV Impact

FV is computed from composite + ceiling. If the composite changes due to stat
blending, FV naturally adjusts:

- Player outperforming tools → higher composite → smaller gap to ceiling →
  higher FV (tools are being realized)
- Player underperforming tools → lower composite → larger gap to ceiling →
  lower FV (tools aren't translating yet)

Combined with risk adjustment, a young AAA dominator could see:
- Composite bumped +2-3 points (stat blend)
- Risk reduced from High to Medium (confirming signal)
- Net FV: potentially one tier higher

An old A-ball underperformer could see:
- Composite pulled -1-2 points
- Risk increased from Medium to High
- Net FV: potentially one tier lower

### What DOESN'T Change

- **Calibration pipeline** — `calibrate.py` still uses MLB data only for tool
  weight regression. MiLB stats are not mixed into tool weight calibration.
- **Percentile rankings** — Already level-aware (Session 73). No changes needed.
- **MLB surplus model** — Players with contracts still use MLB WAR projections.
  The composite change may slightly affect their stat-blended projection, but
  the surplus machinery is unchanged.

## Performance-Adjusted Ceiling

### The Problem with Static Potential Ratings

OOTP's potential ratings are scout reports — probabilistic estimates that can
be wrong. The game even tells you this via the Accuracy attribute (VH/H/N/L/VL).
But even "accurate" scouting misses context that only performance reveals:

- A 20-year-old dominating AA is **demonstrating** that his tools translate to
  production against advanced competition. The scout report might have his
  contact at 55 potential, but his .320/.400/.560 line against older pitchers
  suggests it could be higher.
  
- A 23-year-old "elite prospect" struggling at A-ball is revealing that his
  tools aren't translating. The 70 raw power means nothing if he's whiffing
  40% of the time against 21-year-old pitchers.

### Performance-Adjusted Ceiling (PAC)

Introduce a **performance-adjusted ceiling** alongside the raw `true_ceiling`
(scouting report). This parallels how MLB players already show `composite_score`
(stat-blended) alongside `tool_only_score` (pure tools):

| Concept | MLB Equivalent | Prospect Equivalent |
|---------|---------------|-------------------|
| Pure scouting | `tool_only_score` | `true_ceiling` (raw) |
| Performance-adjusted | `composite_score` | `performance_adjusted_ceiling` |

### How It Works

**Inputs:**
- Player's level-relative OPS+/ERA- (from stat signal computation)
- Player's age relative to level norm age
- Sample size (PA/IP at level)
- Current `true_ceiling` from scouting report

**Age-for-level factor:**
```
age_context = (norm_age_at_level - player_age)
```
- Positive = young for level (amplifies positive signal, dampens negative)
- Negative = old for level (amplifies negative signal, dampens positive)
- Zero = age-appropriate (neutral multiplier)

**Signal strength:**
```
signal = (level_ops_plus - 100) / 100  # normalized distance from average
                                        # e.g., OPS+ 140 → +0.40, OPS+ 70 → -0.30
```

**Age-scaled signal:**
```
# Young outperformers get amplified boost
# Old underperformers get amplified penalty
if signal > 0:
    age_mult = 1.0 + max(0, age_context) * 0.15   # +15% per year young
else:
    age_mult = 1.0 + max(0, -age_context) * 0.15  # +15% per year old
    
# But: young underperformers get dampened penalty (they're still developing)
if signal < 0 and age_context > 0:
    age_mult = max(0.3, 1.0 - age_context * 0.25)  # reduce negative by 25% per year young
    
# And: old outperformers get dampened boost (could just be AAAA player)
if signal > 0 and age_context < 0:
    age_mult = max(0.4, 1.0 + age_context * 0.20)  # reduce positive by 20% per year old
```

**Ceiling adjustment:**
```
# Scale factor based on sample size (more PA = more confidence)
sample_confidence = min(1.0, effective_pa / 300)

# Maximum ceiling adjustment: ±6 points on 20-80 scale
# This is ~1 FV tier worth of change at maximum
max_adjustment = 6

adjustment = signal * age_mult * sample_confidence * max_adjustment
adjustment = clamp(adjustment, -max_adjustment, +max_adjustment)

performance_adjusted_ceiling = true_ceiling + round(adjustment)
```

**Example scenarios:**

| Player | Level | Age | Norm | OPS+ | Signal | Age Mult | Sample | Adj | Result |
|--------|-------|-----|------|------|--------|----------|--------|-----|--------|
| A (young dominator) | AA | 20 | 23 | 145 | +0.45 | 1.45 | 0.8 | +3 | Ceil 58 → 61 |
| B (age-appropriate) | AAA | 24 | 24 | 125 | +0.25 | 1.00 | 1.0 | +2 | Ceil 55 → 57 |
| C (old struggler) | A | 24 | 21 | 65 | -0.35 | 1.45 | 0.9 | -3 | Ceil 60 → 57 |
| D (young struggler) | AA | 20 | 23 | 75 | -0.25 | 0.25 | 0.7 | -0 | Ceil 58 → 58 |
| E (AAAA type) | AAA | 27 | 24 | 150 | +0.50 | 0.40 | 1.0 | +1 | Ceil 50 → 51 |

**Key properties:**
- Young players who dominate get meaningful ceiling boosts (the scout might be wrong)
- Young players who struggle get almost no penalty (they're developing — expected)
- Old players who struggle get meaningful ceiling reductions (scout was wrong)
- Old players who dominate get minimal boost (AAAA player signal)
- Maximum adjustment is ±6 points — never more than ~1 FV tier of impact
- Sample-gated: 50 PA barely moves anything; 300+ PA is full confidence

### Display

On the player page, show both values:

```
Ceiling: 61 (scout: 58, performance: +3)
```

Or in the evaluation panel:
```
Scouting Ceiling:     58
Performance-Adjusted: 61  ▲ young-for-level, dominant stats at AA
```

This gives users the same dual-view that MLB players already have (tool_only vs
stat-blended composite), extended to the prospect ceiling dimension.

### Integration with FV

`calc_fv_v2()` currently uses `p["Pot"]` (which is `true_ceiling`) as the
ceiling input. With PAC:

- `fv_calc.py` passes `performance_adjusted_ceiling` as `p["Pot"]` when
  available, falling back to `true_ceiling` for players without sufficient
  stat data.
- The raw `true_ceiling` is preserved in the ratings table (unchanged).
- PAC is stored as a new column in `ratings` or computed on-the-fly during
  fv_calc.

This means FV naturally incorporates the performance signal through the
adjusted ceiling — a young dominator with PAC boosted from 58→61 will grade
higher in FV because the system sees a higher ceiling to project toward.

## Implementation Plan

### Phase A: Infrastructure (Low Risk)

1. **Compute and store MiLB league averages** during refresh
   - Per-league OBP/SLG averages for hitters, ERA for pitchers
   - Store in `league_settings.json` under `minor_leagues[].batting_avg` / `.pitching_avg`
   - Or new `milb_league_averages` table
   - Computed from current-year data during each refresh
   
2. **Add `_load_milb_stat_seasons()` to evaluation_engine.py**
   - Queries MiLB stat tables grouped by level
   - Returns list of {year, level, ops_plus, pa} dicts
   - Computes level-relative OPS+/ERA- using stored league averages

3. **Store level discount factors** in `model_weights.json`
   - Initial hardcoded defaults (from table above)
   - Key: `MILB_LEVEL_DISCOUNTS` → {2: 0.70, 3: 0.50, 4: 0.30, 6: 0.10}
   - Will be calibrated per-league in Phase C

4. **Store level norm ages** (already exist in `fv_model.py` as `LEVEL_NORM_AGE`)
   - Verify alignment: {aaa: 24, aa: 23, a: 21, rookie: 19}
   - These drive the age-for-level context in PAC

### Phase B: Core Integration (Medium Risk)

5. **Generalize stat blending in `evaluation_engine.py`**
   - Rename internal logic: `compute_composite_mlb()` becomes the unified blender
   - New `_compute_unified_stat_signal()` that merges MLB + MiLB stat histories
   - Each stat season carries: {value_2080, effective_pa, year, level}
   - Blend weight determined by total effective_pa (continuous schedule)
   - Remove the `is_mlb` gate: any player with effective_pa > threshold gets blending

6. **Compute Performance-Adjusted Ceiling (PAC)**
   - New function: `compute_performance_adjusted_ceiling()`
   - Inputs: true_ceiling, level_ops_plus, age, norm_age, effective_pa
   - Outputs: adjusted_ceiling (integer 20-80)
   - Age-for-level amplification/dampening as specified
   - Maximum ±6 adjustment, sample-gated
   - Store in ratings table: new `perf_adjusted_ceiling` column

7. **Wire PAC into `fv_calc.py`**
   - When `perf_adjusted_ceiling` is populated, use it as `p["Pot"]`
   - Fall back to `true_ceiling` when no stat data exists
   - Preserves raw `true_ceiling` in DB (scouting report unchanged)

8. **Add stat-based risk modifier to `calc_fv_v2()`**
   - New parameter: `stat_context` (optional dict with performance/age data)
   - Adjusts `dev_confidence` before risk thresholds
   - Strong confirming signal (young + OPS+ > 115 + 100+ eff PA) → boost confidence
   - Strong contradicting signal (old + OPS+ < 80 + 150+ eff PA) → reduce confidence
   - Fires independently of PAC (complementary mechanisms)

### Phase C: Calibration (Lower Risk)

9. **Add MiLB discount calibration to `calibrate.py`**
   - Uses cross-level progression data (players at level X with MLB stats)
   - Regresses level OPS+ against MLB composite/WAR
   - Requires multi-season data (accumulates over refreshes)
   - Falls back to hardcoded defaults until sufficient data exists
   - Stores in `model_weights.json` under `MILB_LEVEL_DISCOUNTS`

10. **Validate ceiling adjustment bounds**
    - Run on VMLB: check that PAC adjustments are reasonable (±1-4 typically)
    - Ensure no player gets more than ±6 ceiling adjustment
    - Spot-check: young AAA dominator, old A-ball struggler, AAAA vet

### Phase D: Display & Polish (Lowest Risk)

11. **Surface on player pages**
    - Show "Scouting Ceiling: 58 / Performance-Adjusted: 61 (▲3)"
    - Color-code: green for boost, red for reduction, gray if within ±1
    - Show contributing factors: "Young for AA, dominant production"
    - Add to the Overview tab next to existing composite/ceiling display

12. **Surface in prospect lists**
    - Optional column: "Adj" showing ceiling delta from performance
    - Sortable — helps identify players whose stats disagree with scouting

## Data Limitations

- **Single-year MiLB data only** — cannot do multi-year recency weighting yet.
  Phase B will use current-year MiLB with a single weight. Multi-year weighting
  activates once historical MiLB data is stored.
- **No MiLB splits** — cannot compute vL/vR performance at MiLB level.
- **No MiLB fielding** — defensive component remains tool-only for prospects.
- **Level-specific park factors unknown** — a factor that affects A-ball but
  should wash out across league-level averages.
- **League composition varies** — some MiLB leagues may be stronger than others
  at the same nominal level. Per-league averages handle this.

## Risk Assessment

| Risk | Mitigation |
|------|-----------|
| Over-weighting MiLB stats for toolsy young players | Young-player blend discount preserved; A/Rookie levels get 0.25-0.10 discount |
| Under-weighting for MLBers with thin track record | Effective sample math naturally counts MiLB PA at discount; 500 AAA PA = 350 effective |
| Calibration instability with single-year data | Start with reasonable defaults; flag as "uncalibrated" until data accumulates |
| Breaking existing MLB evaluations | MLB path unchanged — effective_pa calculation includes MLB at 1.0×, so established players are dominated by their MLB stats as before |
| FV inflation for old minor leaguers | Old-for-level penalty in risk; stats at A-ball for a 26-year-old are heavily discounted (0.30×) and don't move the needle |

## Test & Validation Plan

The system must be defensible both quantitatively (data-driven) and qualitatively
(does it pass the sniff test for someone who understands baseball?). We validate
in stages, capturing before/after snapshots at each step.

### Baseline Capture (Before Any Changes)

Before implementing anything, freeze the current state for comparison:

```
python3 scripts/fv_calc.py  # ensure current FV/surplus is computed
```

**Snapshot the following for all prospects (≤24, non-MLB):**
- player_id, name, age, level, bucket
- composite_score, true_ceiling, tool_only_score
- fv, fv_continuous, risk, prospect_surplus
- MiLB OPS+ / ERA- (computed offline for reference, not yet in the system)

Store as `data/<league>/tmp/milb_baseline_snapshot.json`.

This gives us the "before" for every comparison below.

### Test Cohorts

Define specific player groups that stress-test the system:

| Cohort | Definition | Expected behavior |
|--------|-----------|-------------------|
| **A. Young dominators** | Age ≤ norm_age - 2, OPS+/ERA- top 20% at level, 150+ PA | Composite +2-4, ceiling +2-4, risk ↓ one band |
| **B. Young strugglers** | Age ≤ norm_age - 1, OPS+/ERA- bottom 25% at level, 150+ PA | Composite -0-1 (dampened), ceiling unchanged (±0), risk unchanged |
| **C. Age-appropriate performers** | Age ≈ norm_age ±1, OPS+/ERA- 90-110, 200+ PA | Composite ±1, ceiling ±0-1, risk unchanged |
| **D. Old underperformers** | Age ≥ norm_age + 2, OPS+/ERA- bottom 25%, 200+ PA | Composite -2-3, ceiling -2-3, risk ↑ one band |
| **E. AAAA veterans** | Age ≥ 26, OPS+/ERA- top 20% at AAA, 300+ PA | Composite +1-2, ceiling +0-1, risk unchanged |
| **F. Small-sample noise** | Any player with <80 PA at level | Composite ±0-1, ceiling unchanged, risk unchanged |
| **G. Fresh callups** | Level=1, <130 MLB AB, 200+ AAA PA this year | Should still reflect MiLB track record (not pure tools) |
| **H. Established MLB** | Level=1, 400+ career MLB PA, any prior MiLB | Change <0.5 from baseline (MiLB nearly irrelevant) |

### Quantitative Validation

**V1. Distribution stability**
After implementation, the overall FV distribution should not shift dramatically:
- Mean FV across all prospects: within ±1 of baseline
- Proportion at each FV tier (40/45/50/55/60/65): within ±5% of baseline
- If the feature systematically inflates or deflates FV, the thresholds are wrong

**V2. Correlation improvement**
The stat-blended composite should correlate more strongly with *future* MLB
production than the tool-only composite. Test with the 240 hitters who have
both MiLB and MLB stats in our DB:
- Baseline: corr(tool_only_score, MLB_WAR)
- After: corr(stat_blended_composite, MLB_WAR)
- The after correlation should be ≥ baseline (even slightly higher = success)
- If it's lower, the stat signal is adding noise, not signal

**V3. Ceiling accuracy**
For players who subsequently developed to peak (age 26+, current composite ≈ ceiling):
- Compare `true_ceiling` vs `perf_adjusted_ceiling` from when they were younger
- Which was closer to their actual peak composite?
- Limitation: requires longitudinal data we don't have yet. This validates over
  time as seasons accumulate.

**V4. Handoff continuity**
For Cohort G (fresh callups), compute:
- Composite the day before callup (using MiLB stats)
- Composite the day after callup (now level=1, below MLB PA threshold)
- Delta should be ≤ 1 point (proving no cliff)

**V5. Risk calibration**
After implementation, risk labels should still be predictive:
- "Low risk" players should have higher realization rates than "High risk"
- The stat-modified risk should be *more* predictive than the raw risk
- Test: what % of "Low" risk players in current snapshot are performing above
  their composite at their current level? (should be >60%)

### Qualitative Validation (Sniff Tests)

For each test cohort, manually review 5-10 players and ask:

1. **Does this make sense?** Would a real scout agree with the adjustment?
2. **Is the magnitude right?** A +3 composite boost for a 20-year-old AA
   dominator feels reasonable. A +8 would not.
3. **Are there pathological cases?** Any player getting an absurd result that
   would embarrass the system?
4. **Is the dual display clear?** Can a user look at "Ceiling: 61 (scout: 58)"
   and understand what it means?

**Specific sniff test scenarios:**

| Scenario | Expected | Red flag if... |
|----------|----------|----------------|
| Jeff Collins: age 22, AAA OPS+ 163, comp 46, ceil 51 | Comp → 49-51, PAC → 54-56, risk Med→Low | PAC > 58 (overcorrection) |
| Pat Romine: age 24, AAA OPS+ 43, comp 51, ceil 54 | Comp → 48-49, PAC → 51-52, risk stays | PAC < 48 (too harsh for 1yr data) |
| A 19-year-old in Rookie ball with 60 PA, OPS+ 200 | Nearly unchanged (sample too small, level too low) | Any change > ±1 |
| Jim Dunn: age 23, AAA OPS+ 190, comp 56, ceil 60 | Comp → 58-59, PAC → 62-63 | PAC > 66 |

### Threshold Tuning Process

The key tunable parameters are:

| Parameter | Initial Value | Sensitivity | Tune How |
|-----------|--------------|-------------|----------|
| Level discounts (AAA/AA/A/Rk) | 0.70/0.50/0.30/0.10 | High — drives effective PA | Regression of MiLB OPS+ vs MLB production |
| Blend schedule slope | PA/1100 | Medium — how fast stats matter | Run V1 (distribution stability) at different slopes |
| PAC max adjustment | ±6 | Medium — ceiling movement cap | Review Cohort A/D, adjust if too aggressive or timid |
| Age-for-level multiplier | 0.15/yr | Medium — amplification rate | Review Cohort A vs E (should separate clearly) |
| Young-struggler dampening | 0.25/yr | High — protects developing players | Review Cohort B (should show near-zero change) |
| Risk threshold adjustment | TBD | Medium — when to shift a band | Run V5, tune until predictive power improves |
| Minimum PA for any effect | 80 effective PA | Low — floor before system engages | Set conservatively, reduce only if V2 improves |

**Tuning methodology:**
1. Implement with initial values
2. Run against VMLB, capture results for all cohorts
3. For each cohort, check if expected behavior matches actual
4. If a cohort deviates, adjust the relevant parameter(s)
5. Re-run and verify no other cohort regressed
6. Repeat until all 8 cohorts behave as expected

### Regression Testing

After tuning is complete, lock the parameters and create automated checks:

```python
def test_milb_integration_cohorts():
    """Ensure MiLB stat integration produces expected ranges for each cohort."""
    
    # Cohort A: young dominators should see composite boost 2-4
    for player in cohort_a:
        delta = player.new_composite - player.baseline_composite
        assert 1 <= delta <= 5, f"{player.name}: composite delta {delta} out of range"
        
    # Cohort B: young strugglers should see minimal change
    for player in cohort_b:
        delta = abs(player.new_composite - player.baseline_composite)
        assert delta <= 2, f"{player.name}: young struggler moved {delta} points"
        
    # Cohort F: small sample should be nearly unchanged
    for player in cohort_f:
        delta = abs(player.new_composite - player.baseline_composite)
        assert delta <= 1, f"{player.name}: small sample moved {delta} points"
        
    # Cohort H: established MLB unchanged
    for player in cohort_h:
        delta = abs(player.new_composite - player.baseline_composite)
        assert delta < 1, f"{player.name}: established MLB moved {delta} points"
```

### Timeline and Gating

| Phase | Gate to proceed |
|-------|----------------|
| A (Infrastructure) | League averages computed, stat loader returns correct values |
| B (Core) | V1 passes (distribution stable), all cohort sniff tests pass |
| B → tuning | V2 shows improvement (or at minimum no regression) |
| C (Calibration) | Only after multi-season MiLB data exists |
| D (Display) | Only after B is fully validated and committed |

**We do NOT ship Phase B until:**
- All 8 cohort expected behaviors are confirmed
- Overall FV distribution is within ±1 of baseline mean
- At least 5 manual sniff tests pass per cohort
- No pathological cases identified in the full player set
