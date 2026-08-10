# Unified Evaluation Engine — Implementation Plan

## Overview

Phased migration from the current dual-model system (prospect FV + MLB contract surplus)
to a single unified evaluation pipeline. Each phase is independently shippable and
non-destructive — the previous system continues working until explicitly replaced.

**Design reference:** `docs/unified_evaluation_design.md`

**Guiding principle:** Build alongside, validate, then switch. Never break existing
evaluations during the transition.

---

## Current Architecture (What We're Replacing)

```
┌─────────────────────────────────┐    ┌─────────────────────────────────┐
│      PROSPECT MODEL             │    │         MLB MODEL               │
│                                 │    │                                 │
│  fv_calc.py                     │    │  contract_value.py              │
│  ├─ calc_fv() → FV grade       │    │  ├─ stat_peak_war() → WAR      │
│  ├─ prospect_surplus() → $     │    │  ├─ peak_war_from_ovr() → WAR  │
│  └─ Writes: prospect_fv table  │    │  ├─ estimate_control() → years │
│                                 │    │  └─ Writes: player_surplus tbl │
│  Applies to: level != 1        │    │  Applies to: level == 1        │
│  OR rookie-eligible (both)     │    │                                 │
└─────────────────────────────────┘    └─────────────────────────────────┘

Decision boundary: level == 1 → MLB model (hard cutoff)
Exception: age ≤ 24 AND career_ab < 130 AND career_ip < 50 → also run prospect model
```

**Problems:**
- Spring training invites get MLB model with flat 50% discount (undervalued)
- First-callup players with 20 PA get stat-based projection from noise
- Dual-listed players (in both tables) show different surplus numbers
- Trade calculator has two code paths with different interfaces
- Web UI has conditional logic for "which surplus to display"

---

## Target Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                    UNIFIED EVALUATION                            │
│                                                                 │
│  unified_eval.py                                                │
│  ├─ compute_tool_projection() → tool_war (from FV)             │
│  ├─ compute_stat_projection() → stat_war (from MLB history)    │
│  ├─ stat_confidence() → blend weight (0.0–1.0)                 │
│  ├─ blend_projections() → peak_war                             │
│  ├─ project_surplus() → unified surplus                        │
│  └─ Writes: player_evaluation table                            │
│                                                                 │
│  Applies to: ALL players with ratings                           │
│  Gradient: stat_confidence determines tool vs stat weight       │
└─────────────────────────────────────────────────────────────────┘
```

---

## Phase 1: Core Function + Validation Framework

**Goal:** Implement the unified surplus calculation as a standalone function.
Run it on all players side-by-side with existing models. Validate outputs.

**No user-facing changes. No schema changes. No consumer changes.**

### 1a. Implement `stat_confidence()`

New function in `src/statsplusplus/evaluation/surplus.py` (or a new module):

```python
def stat_confidence(career_pa: int, career_ip: float) -> float:
    """Compute confidence in MLB stat-based projection.

    Returns 0.0 (pure tools) to 1.0 (pure stats) based on
    accumulated MLB playing time.
    """
```

Inputs:
- `career_pa`: Total career MLB plate appearances
- `career_ip`: Total career MLB innings pitched

Initial implementation: simple ramp. Refine curve shape in Phase 4.

Tasks:
- [ ] Decide on curve shape (linear, sqrt, sigmoid) — start with linear for simplicity
- [ ] Decide on thresholds (400 PA / 120 IP for 1.0 — validate against player examples)
- [ ] Implement function with unit tests
- [ ] Consider whether pitcher and hitter ramps should differ

### 1b. Implement `unified_surplus()`

New function that computes surplus for any player using the blended approach:

```python
def unified_surplus(
    # Tool-based inputs
    fv_continuous: float,
    bucket: str,
    age: int,
    level: str,
    composite: int,
    ceiling: int,
    # Stat-based inputs
    career_pa: int,
    career_ip: float,
    stat_war: Optional[float],  # from stat_peak_war(), None if no MLB stats
    # Control/contract inputs
    contract: Optional[dict],
    service_years: int,
    service_days: int,
    # League context
    dpw: int,
    min_sal: int,
    perpetual_arb: bool,
    perp_model: Optional[dict],
    weights: Optional[ModelWeights],
    # Existing discount inputs
    def_rating: Optional[int],
    scarcity_table: Optional[dict],
    ...
) -> dict:
    """Compute unified surplus for any player."""
```

Tasks:
- [ ] Implement the blended WAR projection (tool_war × (1-sc) + stat_war × sc)
- [ ] Implement discount fading (dev_discount, cert_mult fade with stat_confidence)
- [ ] Implement unified year-by-year surplus projection
- [ ] Handle control period estimation (merge logic from estimate_control + prospect 6yr model)
- [ ] Unit tests for pure prospect case (should match prospect_surplus output)
- [ ] Unit tests for established MLB case (should match contract_value output)
- [ ] Unit tests for crossover cases (new behavior — validate by inspection)

### 1c. Validation Harness

Build a comparison script that runs all three models on every player and reports
discrepancies:

```python
# scripts/validate_unified.py
# For each player:
#   - Run existing prospect model (if applicable)
#   - Run existing MLB model (if applicable)
#   - Run unified model
#   - Report: name, age, level, PA, existing_surplus, unified_surplus, delta, delta%
```

Tasks:
- [ ] Write validation script
- [ ] Run on EMLB — categorize results by player type
- [ ] Run on VMLB — verify cross-league behavior
- [ ] Identify any cases where unified model is clearly worse
- [ ] Iterate on parameters until validation passes

### 1d. Acceptance Criteria for Phase 1

- Pure prospects (0 MLB PA): unified surplus within ±10% of current prospect_surplus
- Established MLB (600+ PA): unified surplus within ±10% of current contract_value base
- Crossover players (1-400 PA): unified surplus is between the two existing values
  (no longer at either extreme) and passes smell test
- No NaN, negative, or absurdly large values for any player
- Performance: unified function runs in <50ms per player (batch of 2000 under 10s)

---

## Phase 2: Integrate Into Pipeline

**Goal:** Wire the unified function into `fv_calc.py` and write to a new table.
Old tables continue to be written for backward compat. No consumer changes yet.

### 2a. Schema Migration

Add `player_evaluation` table to `db.py` migrations:

Tasks:
- [ ] Define table schema (see design doc)
- [ ] Add migration to `_migrate()` function
- [ ] Verify idempotent (safe to run on existing DBs)

### 2b. Integrate Into fv_calc.py

Modify the batch pipeline to also compute and write unified evaluations:

```python
# In fv_calc.run():
# After existing prospect_rows and surplus_rows computation...
# Also compute unified evaluations for ALL players
unified_rows = []
for p in all_rated_players:
    result = unified_surplus(...)
    unified_rows.append(...)
conn.executemany("INSERT INTO player_evaluation VALUES (...)", unified_rows)
```

Tasks:
- [ ] Wire unified_surplus into the player loop in fv_calc.py
- [ ] Gather stat history (career PA/IP) for all players
- [ ] Write results to player_evaluation table
- [ ] Keep existing prospect_fv and player_surplus writes (dual-write period)
- [ ] Verify refresh time increase is acceptable (<5s additional)
- [ ] Run full refresh on EMLB and VMLB, validate stored values

### 2c. Backward-Compatible Views

Create views that replicate the old table interfaces from the new unified data:

Tasks:
- [ ] Create `prospect_fv_v2` view (from player_evaluation WHERE stat_confidence < threshold)
- [ ] Create `player_surplus_v2` view (from player_evaluation)
- [ ] Verify views produce same row counts as actual tables
- [ ] Do NOT replace actual tables yet — views are for testing only

### 2d. Acceptance Criteria for Phase 2

- `player_evaluation` table populated for all rated players on refresh
- Refresh time increase < 5 seconds
- All existing tests still pass (old tables still written)
- Web UI still works identically (still reads from old tables)

---

## Phase 3: Switch Consumers

**Goal:** Update all code that reads from `prospect_fv` / `player_surplus` to read
from `player_evaluation` instead. One consumer at a time, with fallback.

### 3a. Web UI — Player Page

The player page currently has conditional logic: "if prospect_row AND surplus_row..."
Replace with a single query against `player_evaluation`.

Tasks:
- [ ] Update `player_queries.py` valuation section to read from `player_evaluation`
- [ ] Display stat_confidence and blended peak_war on the player page
- [ ] Remove the "prospect vs MLB" conditional display logic
- [ ] Show FV/risk for all players where stat_confidence < 0.75 (not just non-MLB)
- [ ] Verify player pages render correctly for all player types
- [ ] QA: spring training invite, rookie, established vet, AAA prospect

### 3b. Web UI — Team Pages and Prospect Lists

- [ ] Update team_queries.py (farm section, org overview) to use player_evaluation
- [ ] Update queries.py (league prospect list, positional rankings)
- [ ] Verify prospect rankings still sort correctly (FV primary, surplus secondary)
- [ ] Verify power rankings use correct surplus values

### 3c. Trade Calculator

The trade calculator currently has two paths (contract_value vs prospect_value).
Unify to one.

Tasks:
- [ ] Rewrite `value_player()` in trade_calculator.py to read from player_evaluation
- [ ] Remove the `is_prospect` branch — all players valued the same way
- [ ] Update trade_targets.py, trade_assets.py to use unified surplus
- [ ] Verify trade calculator outputs are reasonable for mixed packages (prospect + vet)

### 3d. CLI Tools

- [ ] Update prospect_query.py to read from player_evaluation
- [ ] Update free_agents.py surplus display
- [ ] Update contract_value.py CLI output (show unified breakdown)
- [ ] Update prospect_value.py CLI output (show stat_confidence if applicable)

### 3e. Acceptance Criteria for Phase 3

- All web pages render correctly with no visual regression
- Trade calculator produces sensible results for all trade types
- CLI tools work identically (or better) for common use cases
- Zero references to `prospect_fv` or `player_surplus` tables in application code
  (they may still exist as views or for external tools)

---

## Phase 4: Remove Legacy + Tune

**Goal:** Delete dead code, remove dual-write, tune parameters based on user feedback.

### 4a. Remove Dual-Write

- [ ] Stop writing to `prospect_fv` and `player_surplus` in fv_calc.py
- [ ] Replace tables with views (for any external tools that might reference them)
- [ ] Update tests that reference old table schemas

### 4b. Remove Legacy Code

- [ ] Remove `prospect_surplus()` function (replaced by unified_surplus)
- [ ] Remove `contract_value()` function (replaced by unified_surplus)
- [ ] Remove `NO_TRACK_RECORD_DISCOUNT` constant
- [ ] Remove the dual-path logic from all CLI scripts
- [ ] Clean up imports across the codebase

### 4c. Tune Parameters

Based on real-world validation and user feedback:

- [ ] Refine stat_confidence curve shape (linear → sigmoid if needed)
- [ ] Validate discount fading rates — are discounts phasing out too fast/slow?
- [ ] Check: do crossover players trade at values that "feel right" to users?
- [ ] Run completed trades through the calculator — does the unified model
      produce more consistent evaluations for both sides?
- [ ] Consider recency adjustment to stat_confidence (stale stats = lower confidence)
- [ ] Consider separate pitcher/hitter ramp speeds

### 4d. Development Ramp Refinement

The current system has two separate ramp mechanisms:
- MLB model: linear OVR growth toward Pot over years-to-peak
- Prospect model: PROSPECT_WAR_RAMP (fixed per control year)

Unify into a single development projection:

- [ ] Design: project composite growth year-by-year using calibrated closure rates
- [ ] Convert each year's projected composite to WAR via COMPOSITE_TO_WAR tables
- [ ] Replace both PROSPECT_WAR_RAMP and the MLB model's dev_ramp with this
- [ ] Validate that development projections are sensible for different player profiles

### 4e. Acceptance Criteria for Phase 4

- No references to old table names or legacy functions in main codebase
- Test suite updated and passing
- At least 2 leagues validated (EMLB, VMLB)
- User feedback incorporated (trade calculator feels right, prospect rankings unchanged)

---

## Phase 5: Extended Features (Future)

Once the unified model is stable, it enables features that were impossible or awkward
with the dual system:

- **Unified leaderboard:** Sort ALL players by surplus regardless of level
- **Trade value trends:** Track a player's unified surplus over time as they develop
- **Projection confidence intervals:** Use stat_confidence to show uncertainty bands
  on surplus estimates
- **"What-if" projections:** "What's this player worth if he puts up 3.0 WAR next year?"
  — trivial with the unified model, awkward with the dual system
- **Promotion impact modeling:** "How does calling up Prospect X change their value?"
  — smooth transition instead of cliff

---

## Risk Mitigation

| Risk | Mitigation |
|------|-----------|
| Unified model produces worse evaluations for some player type | Phase 1 validation catches this before any consumer sees it. Iterate on parameters. |
| Performance regression (slower refresh) | Benchmark in Phase 2. The unified function is simpler than running two separate models, so should be comparable or faster. |
| Breaking existing features | Dual-write period (Phase 2) ensures old tables exist. Phase 3 is consumer-by-consumer with rollback capability. |
| User confusion during transition | Phase 2 is invisible to users. Phase 3 changes display but should produce better (not different) results. |
| stat_confidence curve is wrong | Start conservative (linear ramp). Tune in Phase 4 based on validation. The curve is one line of code to change. |

---

## Dependency Map

```
Phase 1 (core function)
    │
    ├── 1a: stat_confidence()
    ├── 1b: unified_surplus()  ← depends on 1a
    └── 1c: validation script  ← depends on 1b
            │
Phase 2 (pipeline integration)  ← depends on Phase 1 passing validation
    │
    ├── 2a: schema migration
    ├── 2b: fv_calc integration  ← depends on 2a
    └── 2c: compat views         ← depends on 2a
            │
Phase 3 (consumer switch)  ← depends on Phase 2 passing refresh test
    │
    ├── 3a: player page     (independent)
    ├── 3b: team/league pages (independent)
    ├── 3c: trade calculator (independent)
    └── 3d: CLI tools        (independent)
            │
Phase 4 (cleanup + tune)  ← depends on all of Phase 3
    │
    ├── 4a: remove dual-write
    ├── 4b: remove legacy code  ← depends on 4a
    ├── 4c: parameter tuning    (independent, ongoing)
    └── 4d: development ramp    (independent)
```

---

## Estimated Level of Effort

| Phase | LOE | Notes |
|-------|-----|-------|
| 1a | Low | One function, tests |
| 1b | Medium | Core complexity — control estimation, discount fading, year-by-year projection |
| 1c | Low | Script that calls existing + new functions, compares |
| 2a | Low | One migration |
| 2b | Medium | Wiring into the batch loop, gathering inputs |
| 2c | Low | SQL views |
| 3a | Medium | Player page has complex conditional logic to simplify |
| 3b | Low-Medium | Mostly query changes |
| 3c | Medium | Trade calculator has two-path logic to unify |
| 3d | Low | CLI scripts are thin wrappers |
| 4a-4b | Low | Deletion + cleanup |
| 4c | Medium | Empirical work, user validation |
| 4d | Medium | Design + implement unified development projection |

**Total estimated LOE:** Medium-High across all phases. Individually shippable pieces
are each Low-Medium. Can be done incrementally across multiple sessions.

---

## Success Metrics

1. **Continuity:** Established player evaluations do not change by more than ±10%
2. **Crossover quality:** Rookie/callup players get sensible middle-ground values
   (no more $0 or $76M for 26-IP rookies)
3. **Simplicity:** Trade calculator has one code path. Player page has one display path.
4. **User trust:** Beta testers confirm that trade values "feel right" for
   cross-category deals (prospect for MLB player)
5. **Code reduction:** Net deletion of code after Phase 4 (unified is simpler than dual)
