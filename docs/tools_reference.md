# Tools Reference

Quick-reference catalog of all CLI tools, importable libraries, and data sources available
in the EMLB analytics platform. Intended for agent context — keeps tool discovery out of
the conversation window.

**Maintenance rule:** Update this file whenever a script, query function, or data source
is added, removed, or has its interface changed. Add this to the end-of-session
documentation checklist in `.kiro/steering/emlb-agent.md`.

---

## CLI Tools

All scripts run from the project root: `cd ~/statsplusplus`

### `scripts/standings.py`

League-wide standings with pythagorean expected records.

```bash
python3 scripts/standings.py [--year 2033] [--actual]
python3 scripts/standings.py --team ANA              # Team-specific: actual record + playoff picture
```

Output: Ranked table of all teams — W, L, Pct, GB, RS, RA, Diff. User's team marked with `◄`.
`--actual` adds actual W-L from `games` table alongside pythagorean and shows the delta with
luck/regression interpretation (Pyth >> actual = bullpen drag; Pyth << actual = regression risk).

`--team <ABBR>` shows a single team's actual record, division leaders, wild card race with GB
from the last WC spot, and pythagorean comparison. Preferred over ad-hoc SQL for record lookups.

Importable: `actual_record(team_id, year)` returns `(w, l)` from the `games` table.
`all_actual_records(year)` returns `{team_id: (w, l)}` for all teams.
`league_standings_actual(year, league_name)` returns sorted standings for a league.
`playoff_picture(year, team_id)` returns formatted string with division leaders + WC race.

### `scripts/prospect_query.py`

League-wide prospect rankings and farm system comparisons.

```bash
# Top prospects league-wide (filterable)
python3 scripts/prospect_query.py top [--n 20] [--bucket SP] [--age-max 22] [--fv-min 50] [--level AA] [--sort {fv,surplus}]

# Farm system rankings (total surplus, FV tier counts)
python3 scripts/prospect_query.py systems [--n 10]

# Single team's prospect list
python3 scripts/prospect_query.py team ANA [--n 15] [--fv-min 40] [--sort {fv,surplus}]
```

Output: Ranked prospect tables with FV, surplus, level, position, age. Systems view
shows aggregate farm value with tier breakdowns.

### `scripts/contract_value.py`

Contract surplus/deficit breakdown for any MLB player.

```bash
python3 scripts/contract_value.py <player_id or "player name">
```

Output: Year-by-year projection — salary, projected WAR, dollar value, surplus/deficit.
Shows contract flags (NTC, options), total surplus, peak WAR estimate.

### `scripts/prospect_value.py`

Prospect trade surplus calculator.

```bash
# By player ID (looks up FV/level from DB)
python3 scripts/prospect_value.py --player <id>

# Manual parameters
python3 scripts/prospect_value.py --fv 50 --age 21 --level AA --bucket SP
```

Output: Surplus value in dollars with option value model breakdown.

### `scripts/trade_calculator.py`

Trade surplus balance calculator. Accepts player names or IDs.

```bash
# Simple interface
python3 scripts/trade_calculator.py --offer "Jeff Hudson,Pat Showalter" --receive "Greg Brewer"
python3 scripts/trade_calculator.py --offer 62201,201 --receive 59877

# Full JSON (for salary retention or prospect overrides)
python3 scripts/trade_calculator.py --trade '{"my_team_send": [...], "my_team_receive": [...]}'
```

Output: Per-player surplus breakdown (pessimistic/base/optimistic), net surplus for each side,
verdict. Team name derived from `config/state.json`. Legacy `angels_send`/`angels_receive` keys
still accepted.

Trade package surplus balance evaluator.

```bash
python3 scripts/trade_calculator.py --trade '{"side_a": [{"type":"mlb","id":232}], "side_b": [{"type":"prospect","id":12345}]}'
```

Output: Surplus balance between two trade packages with sensitivity ranges.

**Note on N-for-1 trades:** The calculator reports raw surplus balance but does not
account for the consolidation premium. When one side sends fewer, higher-value players,
the consolidated side should show a surplus advantage of ~15-25% (for 3-for-1) to be
truly balanced. A "even" result on the calculator in a 3-for-1 likely means the team
sending one player is being shortchanged.

### `scripts/team_needs.py`

Positional needs analysis — production vs league average, upgrade priorities.

```bash
python3 scripts/team_needs.py                  # My team
python3 scripts/team_needs.py --team MIN       # Any team
python3 scripts/team_needs.py --aaa-roster     # Needs report + full AAA roster
```

Output: Per-position OPS vs league average (flagged SEVERE/WEAK/OK/STRONG), rotation and
bullpen ERA vs league average, ranked upgrade priority list with platoon flags where applicable.
`--aaa-roster` appends full AAA roster sorted by Ovr — includes veterans below FV threshold
that `prospect_query.py` misses. Designed for trade analyst session initialization.

### `scripts/trade_assets.py`

Tradeable assets viewer — shows what a team can offer in a trade (MLB surplus players + farm prospects).

```bash
python3 scripts/trade_assets.py                    # My team's assets
python3 scripts/trade_assets.py --team MIN         # Another team's assets
python3 scripts/trade_assets.py --bucket SP        # Filter by position
python3 scripts/trade_assets.py --min-surplus 10   # Min surplus value ($M)
python3 scripts/trade_assets.py --prospects-only
python3 scripts/trade_assets.py --mlb-only
```

Output: MLB players ranked by surplus (with contract status, salary, stats) and farm prospects
ranked by surplus (with FV, level, Ovr/Pot). Complements `trade_targets.py` for package construction.

### `scripts/trade_targets.py`

Trade target finder — MLB players by position with contract status and seller classification.

```bash
python3 scripts/trade_targets.py --bucket COF           # All OF trade targets (rentals/options)
python3 scripts/trade_targets.py --bucket SP --min-ovr 58
python3 scripts/trade_targets.py --bucket 3B --sellers-only
python3 scripts/trade_targets.py --bucket COF --include-controlled  # Include multi-year players
python3 scripts/trade_targets.py --bucket SS --max-salary 10        # Max pro-rated salary ($M)
```

Output: Ranked target list grouped by contract status (RENTAL / OPTION / CONTROLLED). Shows
OVR/Pot, team, pro-rated and full salary, surplus value, production stats, CF grade for OF.
Seller teams (>8 GB from last playoff spot) flagged with `SELL`.



### `scripts/free_agents.py`

Upcoming free agent class analysis.

```bash
# My team's expiring contracts
python3 scripts/free_agents.py --my-team

# League-wide by position
python3 scripts/free_agents.py [--bucket SP] [--min-war 2.0] [--years 1]
```

Output: Players approaching free agency with age, position, salary, surplus, and status:
- **FA** — true walk-year, hits free agency after this season
- **ARB** — arb-eligible (service time < 6 years), another year of team control after this season
- **TO** — team option exists

FAs sorted before ARB players. Use status column to distinguish true rentals from arb-eligible
players who carry a future salary obligation.

### `scripts/farm_analysis.py`

Generates the Angels farm system scaffold for report writing.

```bash
python3 scripts/farm_analysis.py
```

Output: Writes `tmp/farm_scaffold_<date>.md` — ranked prospect cards with grade tables,
FV history, development signals, and rewrite flags. **Read the scaffold, don't read the
script source.**

### `scripts/roster_analysis.py`

Generates the Angels MLB roster scaffold for report writing.

```bash
python3 scripts/roster_analysis.py
```

Output: Writes `tmp/roster_scaffold_<date>.md` — player cards with stat lines, ratings,
contract info, surplus, and summary rewrite flags.

### `scripts/draft_board.py`

Draft board analysis, simulation, and auto-draft list generation. CLI commands
auto-load per-round-group settings from `config/draft_settings.json` (see `draft_settings.py`).

```bash
python3 scripts/draft_board.py board [--top 30]           # Full ranked board (FV sort)
python3 scripts/draft_board.py available [--top 30]       # Board minus taken players
python3 scripts/draft_board.py pick N [--no-balance]      # Two-list merge ranked list for N players
python3 scripts/draft_board.py upload [--top 500] [--no-balance]  # Generate auto-draft file
python3 scripts/draft_board.py compare "Name1" "Name2"    # Side-by-side comparison
python3 scripts/draft_board.py sim PICK [--rounds 7] [--seed S]  # Draft simulation
```

Draft value formula: `FV + (ceiling-55)×0.2 + RP_discount + Acc(L:-2,VL:-4) + risk(Extreme:-3,High:-1) + contact(-2 if cnt<50,pow≥80,eye<70) + ctl<45(-3) + arsenal(-2 to +1) + personality(±0.9) + needs(Rd3+)`.
RP discount: -2 if stamina ≥ 30 (tweener with SP upside), -5 if stamina < 30 (pure reliever).
ADP computed from POT rank. `pick` and `upload` commands use two-list merge: List A (our draft_value + position-scaled surplus weight `0.02+0.06/√pos`) vs List B (POT rank),
deferring sleepers beyond survival threshold (default: `30 + 6√pos`, configurable via `_threshold_sqrt` or `_threshold_fixed`).
Value gap override: if the best available player's draft_value exceeds the best survival-passing player by ≥3 points, take them regardless of ADP.
Balance adjustment (default on): tracks running pitcher/hitter ratio and applies a small bonus to the underrepresented type (target 45%, bonus 2.0). Disable with `--no-balance`.
Sim uses randomized other-team picks (window `8+pick×0.15`, exponent `max(1.0, 2.8 - pick×0.018)`).

Importable: `load_board()`, `draft_value()`, `compute_adp()`, `compute_org_needs()`,
`build_pick_list()`, `build_urgency_list()`, `simulate_draft()`.

`compute_org_needs(conn)` auto-detects league type: FA leagues use departure-based needs
(players leaving with thin farm depth); perpetual arb leagues use weakness-based needs
(starter composite below league median with no FV 50+ farm replacement).

### `scripts/draft_settings.py`

Draft board settings persistence and validation. Manages per-league, per-round-group
slider configurations that control `draft_value()` and `build_pick_list()` behavior.

```python
from pathlib import Path
from draft_settings import load_settings, save_settings, copy_settings, map_to_params, resolve_for_round

league_dir = Path("data/emlb")
settings = load_settings(league_dir)              # Load from config/draft_settings.json (or defaults)
save_settings(league_dir, settings)               # Validate and persist to disk
params = resolve_for_round(settings, round_num=3) # Resolve mapped internal parameters for round 3
raw_params = map_to_params({"upside": 0.75, "risk_tolerance": 0.5, ...})  # Manual slider→param mapping
copy_settings(Path("data/vmlb"), league_dir)      # Copy settings between leagues
```

**Settings structure:**
```json
{
  "version": 1,
  "round_groups": [
    {"start": 1, "end": 3, "settings": {"upside": 0.5, "risk_tolerance": 0.5, ...}},
    {"start": 4, "end": null, "settings": {"upside": 0.5, "risk_tolerance": 0.5, ...}}
  ],
  "active_preset": "balanced"
}
```

The last round group must have `"end": null` (catch-all). Validation enforces no overlaps,
proper ordering, and snaps slider values to the nearest discrete position.

**11 slider parameters** (all 0.0–1.0, midpoint 0.5 = original hardcoded behavior):

| Slider | Setting key | Internal param | 0.0 | 0.5 (default) | 1.0 |
|--------|-------------|----------------|-----|----------------|-----|
| Upside vs Safety | `upside` | `ceiling_weight` | 0.0× | 0.2× | 0.4× |
| Risk Tolerance | `risk_tolerance` | `risk_scale` | 2× penalties | 1× (standard) | 0× (ignore risk) |
| Pitcher/Hitter Mix | `balance` | `balance_target` | 25% pitcher target | 45% (standard) | 65% pitcher target |
| Organizational Need | `need` | `need_scale` | No needs bonus | 1.5× | 3× needs bonus |
| Reliever Penalty | `rp_discount` | `rp_discount_scale` | 2× RP penalty | 1× (standard) | 0× (no penalty) |
| Scouting Accuracy | `acc_penalty` | `acc_scale` | 2× Acc penalty | 1× (standard) | 0× (ignore Acc) |
| Survival Strategy | `survival` | `survival_base/scale` | Tight (grab early) | 30+6√pos | Wide (let them fall) |
| Personality Weight | `personality` | `personality_scale` | No personality | 1× (standard) | 2× personality |
| Arsenal Penalty | `arsenal` | `arsenal_scale` | No arsenal adj | 1× (standard) | 2× arsenal |
| Contact Floor | `contact_floor` | `contact_floor_scale` | No cnt penalty | 1× (standard) | 2× contact penalty |
| Balance Enforcement | `balance_strength` | `balance_bonus` | No balance | 2.0 (standard) | 4.0 (aggressive) |

**Presets:** `balanced` (all 0.5, need 0.25 early), `upside` (ceiling 1.0, risk 0.75, acc 0.25),
`conservative` (ceiling 0.0, risk 0.25, acc 0.75, personality 0.75, arsenal 0.75),
`org_needs` (need 1.0, everything else 0.5).

**Web API endpoints:**
- `GET /api/draft-settings` — returns current settings JSON + preset definitions
- `POST /api/draft-settings` — saves settings (body: `{settings: <full settings object>}`)
- `POST /api/draft-settings/copy` — copies settings from another league (body: `{from_league: "<slug>"}`)

Importable: `load_settings(league_dir)`, `save_settings(league_dir, settings)`,
`copy_settings(from_dir, to_dir)`, `map_to_params(normalized)`, `resolve_for_round(settings, round_num)`,
`PRESETS`, `SETTING_KEYS`, `DEFAULT_SETTINGS`, `DEFAULT_PARAMS`.

### `scripts/refresh.py`

Data pipeline — pulls all data from StatsPlus API into the DB. Runs calibration
and FV/surplus computation automatically after data pull.

```bash
python3 scripts/refresh.py [year]    # Full refresh, all teams
python3 scripts/refresh.py state <game_date> [year]  # Manual state override
```

**Do not run during article writing.** Only run when the game date has advanced.

### `scripts/benchmark.py`

Evaluation engine performance benchmark. Measures composite vs WAR correlation by
position bucket, prospect inflation by level/age, ceiling collapse for high-upside
young prospects, and cross-league weight stability.

```bash
python3 scripts/benchmark.py              # Active league
python3 scripts/benchmark.py --all        # All leagues side-by-side
python3 scripts/benchmark.py --json       # Machine-readable output
STATSPP_LEAGUE=emlb python3 scripts/benchmark.py  # Specific league
```

### `scripts/calibrate.py`

Derives league-specific valuation tables and tool weights from actual data. Produces
`config/model_weights.json` (OVR→WAR, COMPOSITE→WAR, FV→WAR, arb, scarcity) and
`config/tool_weights.json` (component-level regression weights per position). Runs
automatically during refresh.

```bash
python3 scripts/calibrate.py           # Write model_weights.json + tool_weights.json
python3 scripts/calibrate.py --dry-run # Show results without writing
```

Two-pass execution during refresh:
- **Pass 1** (before evaluation engine): tool weight regression (hitting→WAR, baserunning→SB metrics, fielding→ZR, pitching→FIP) + OVR_TO_WAR. Hitting regression uses WAR as target (not OPS+) and excludes `avoid_k` (collinear with contact) and `speed` (contributes via baserunning only). Min weight floor: 0.18 for hitters, 0.15 for pitchers. Calibrated weights are blended with defaults proportional to R² (`final = R² × calibrated + (1-R²) × default`).
- **Pass 2** (after evaluation engine): COMPOSITE_TO_WAR regression using freshly computed composite scores

---

## Importable Libraries

These scripts double as importable modules. Use from Python when you need structured
data rather than CLI text output.

### `evaluation_engine`

Custom player evaluation — computes Composite_Score, Ceiling_Score, and Tool_Only_Score
from individual tool ratings. All computation functions are pure (no side effects).
The `run()` entry point is the only function with DB access.

```python
from evaluation_engine import (
    compute_composite_hitter,   # Hitter score from tool ratings + positional weights
    compute_composite_pitcher,  # Pitcher score from tool ratings + arsenal + stamina
    compute_composite_mlb,      # Stat-blended score for MLB players
    compute_ceiling,            # Ceiling score from potential tool ratings
    compute_tool_only_score,    # Pre-stat-blend score (for divergence detection)
    compute_offensive_grade,    # Offensive component (contact/gap/power/eye only)
    compute_baserunning_value,  # Baserunning component (speed/steal/stl_rt)
    compute_defensive_value,    # Defensive component (positional defensive tools)
    compute_durability_score,   # Durability component (SP stamina)
    compute_component_ceilings, # Component-level ceilings from potential tools
    derive_composite_from_components,  # Verify decomposition is lossless (inverse of composite)
    stat_to_2080,               # Convert league-normalized stat (OPS+) to 20-80 scale
    pitcher_stat_to_2080,       # Asymmetric pitcher stat conversion (steeper above-avg slope)
    _tool_transform,            # Non-linear piecewise tool transformation (exported for tests)
    detect_divergence,          # Compare tool_only_score vs OVR → hidden_gem/landmine/agreement
    classify_archetype,         # Tool profile archetype (contact-first, power-over-hit, etc.)
    identify_carrying_tools,    # Tools rated 15+ above composite
    identify_red_flag_tools,    # Tools rated 15+ below composite
    compute_snapshot_deltas,    # Tool-level deltas between two rating snapshots (riser/reduced ceiling flags)
    is_two_way_player,          # Detect two-way players (both hitting + pitching tools)
    compute_two_way_scores,     # Dual scoring for two-way players
    compute_combined_value,     # Combined value from primary + secondary role scores
    derive_tool_weights,        # Per-feature r² regression for tool weight derivation
    normalize_coefficients,     # Clamp negatives, normalize to sum 1.0, optional min_weight floor
    recombine_component_weights,# Merge hitting/baserunning/fielding weights by position
    load_tool_weights,          # Load per-league tool_weights.json (or defaults)
    validate_tool_weights,      # Validate a tool-weights config dict (weight sums, types)
    run,                        # Batch pipeline: compute all scores, write to DB
)
```

Pipeline integration: runs after `calibrate.py` pass 1 and before `fv_calc.py`.
Scores are written to `ratings` table (`composite_score`, `ceiling_score`, `tool_only_score`,
`secondary_composite`, `offensive_grade`, `baserunning_value`, `defensive_value`,
`durability_score`, `offensive_ceiling`) and `ratings_history` for development tracking.

### `ratings`

Rating normalization — the foundation for all tool grade display and model inputs.

```python
from ratings import norm, norm_floor, get_ratings_scale, init_ratings_scale
norm(75)              # → 65 (1-100 scale) or 75 (20-80 scale)
norm_floor(0)         # → 20 (floor for numeric comparisons)
get_ratings_scale()   # → '1-100' or '20-80'
```

### `fv_model`

Prospect FV grade calculation. Empirical gap closure model with MLB-anchored grading.

```python
from fv_model import calc_fv, calc_fv_v2, defensive_score
# player_dict needs: Ovr (composite), Pot (true_ceiling), Age, _bucket,
#   _norm_age, _is_pitcher, _mlb_median, WrkEthic, Int, Acc
fv_base, fv_plus = calc_fv(player_dict)
# FV = 45 + (expected_peak - positional_MLB_median)
# expected_peak = composite + gap × closure × bust_discount × gap_scale
```

### `war_model`

WAR projection and stat history loading.

```python
from war_model import peak_war_from_score, aging_mult, load_stat_history, stat_peak_war
peak_war_from_score(60, 'SP')         # → float (uses COMPOSITE_TO_WAR when available, falls back to OVR_TO_WAR)
peak_war_from_ovr(60, 'SP')           # → float (backward-compatible alias)
aging_mult(33, 'SP')                  # → float (multiplier on peak WAR)
bat_hist, pit_hist, two_way = load_stat_history(conn, game_date)
war = stat_peak_war(pid, 'SP', bat_hist, pit_hist)
```

`load_stat_history` includes the current year's stats with a `season_pct` field
(games_played / 162). Offseason game dates (month ≥ 11) get season_pct = 1.0.
Mid-season dates get proportional completion.

`stat_peak_war` uses 4-year weighted average `[3, 3, 2, 1]` with the most recent
year's weight scaled by `season_pct`. Handles SP↔RP role converts by blending
prior-role history (discounted) with new-role data when < 2 full new-role seasons exist.

### `arb_model`

Arb salary estimation and service time calculation.

```python
from arb_model import arb_salary, arb_salary_perpetual, estimate_service_time, estimate_control

# FA leagues: exponential base + raise model
arb_salary(60, 'SS', arb_year=1, prior_salary=825000, min_sal=825000)  # → int

# Perpetual arb leagues: growth + ceiling model (calibrated from model_weights.json)
arb_salary_perpetual(age=26, projected_war=4.0, dpw=21743, min_sal=6600,
                     career_war=12.0, model=None)  # → int

svc = estimate_service_time(conn, player_id)                            # → float (years)
ctrl, sals, pre_arb = estimate_control(conn, player_id, age, salary)   # → (int, list, int) or (None,None,None)
```

### `contract_value`

MLB contract surplus/deficit breakdown.

```python
from contract_value import contract_value
result = contract_value(player_id)
# Returns dict: player_id, name, bucket, age, ovr, years_left, flags,
#               breakdown (list of year dicts), total_surplus {pessimistic, base, optimistic}
```

### `prospect_value`

Prospect trade surplus calculator with component-aware outcome model.

```python
from prospect_value import prospect_surplus, prospect_surplus_with_option, career_outcome_probs
result = prospect_surplus(fv=55, age=21, level='AA', bucket='SP', ovr=50, pot=70)
# Returns dict: total_surplus, dev_discount, certainty_mult, scarcity_mult, breakdown
opt = prospect_surplus_with_option(fv=55, age=21, level='AA', bucket='SP', ovr=50, pot=70,
    offensive_grade=50, offensive_ceiling=60, defensive_value=None, durability_score=65)
# Returns int: surplus including option value, adjusted for profile shape
probs = career_outcome_probs(fv=55, age=21, level='AA', bucket='SP', ovr=50, pot=70,
    offensive_grade=50, durability_score=65)
# Returns dict: tiers (WAR probability curve), thresholds, confidence
```

### `player_utils`

Shared utilities — bucketing, display helpers, league settings, PAP.
Also re-exports `norm`, `norm_floor`, `calc_fv`, `peak_war_from_ovr`, `aging_mult`,
`load_stat_history`, `stat_peak_war` for backward compatibility.

```python
from player_utils import (
    assign_bucket,    # Determine positional bucket from ratings dict
    display_pos,      # Convert bucket to display string (COF → OF)
    height_str,       # cm → feet/inches string
    fmt_table,        # Format markdown table row
    calc_pap,         # PAP score from WAR, salary, team games, $/WAR
    dollars_per_war,  # Current $/WAR from league_averages.json
    league_minimum,   # League minimum salary from league_settings.json
)
```

### `league_config`

Single source of truth for league settings.

```python
from league_config import config
config.my_team_id        # int
config.year              # int
config.team_abbr(44)     # str e.g. "ANA"
config.team_name(44)     # str e.g. "Anaheim Angels"
config.team_abbr_map     # {int: str}
config.level_map         # {"1": "MLB", "2": "AAA", ...}
config.minimum_salary    # int
config.ratings_scale     # "1-100" or "20-80"
config.settings          # full league_settings.json dict
```

### `constants`

Named constants for all model levers. Sections:
- **Shared identifiers**: `PITCH_FIELDS`, `ROLE_MAP`, `DEFAULT_DOLLARS_PER_WAR`, `DEFAULT_MINIMUM_SALARY`, `PEAK_AGE_PITCHER/HITTER`, `SERVICE_GAMES_*`
- **Prospect model**: `ARB_PCT`, `FV_TO_PEAK_WAR*`, `DEVELOPMENT_DISCOUNT`, `YEARS_TO_MLB`, `PROSPECT_DISCOUNT_RATE`, `LEVEL_AGE_DISCOUNT_RATE`, `PROSPECT_WAR_RAMP`, `NO_TRACK_RECORD_DISCOUNT`, `RP_POT_DISCOUNT`, `SCARCITY_MULT`, `MIN_REGRESSION_N`, `CALIBRATION_YEARS`
- **MLB contract model**: `MLB_SCARCITY`, `ARB_HITTER_BASE/EXP`, `ARB_RP_BASE/EXP`, `ARB_RAISE_*`, `ARB_DEEP_SALARY_THRESHOLD`
- **WAR tables**: `OVR_TO_WAR`, `OVR_TO_WAR_CALIBRATED`
- **Aging curves**: `AGING_HITTER`, `AGING_PITCHER`

---

## Web Query Functions

These are in `web/queries.py`, `web/team_queries.py`, `web/player_queries.py`, and `web/trade_queries.py`.
Import with `sys.path.insert(0, 'web')`. All are read-only against the DB.

### League-Level (`queries.py`)

| Function | Returns |
|---|---|
| `get_state()` | `{game_date, year, my_team_id}` |
| `get_my_team_id()` | int |
| `get_my_team_abbr()` | str (e.g. "ANA") |
| `get_top_prospects(n)` | List of top N prospects league-wide |
| `get_all_prospects()` | All FV≥40 prospects with ratings |
| `get_prospect_summary(pid)` | Full prospect detail for side panel |
| `get_batting_leaders(year, min_pa)` | Top 5 per stat, MLB/AL/NL |
| `get_pitching_leaders(year, min_ip)` | Top 5 per stat, MLB/AL/NL |
| `search_players(query)` | Up to 15 matching players (MLB + prospects) across all orgs |
| `get_prospect_comps(pid)` | 3-tier MLB comps for a prospect (Upside/Likely/Floor) |
| `get_player_card(pid)` | Side-panel-style data for any player (tools, pitches, defense, stats) |
| `get_draft_pool()` | Draft board: state detection, surplus-ranked pool (via `prospect_surplus_with_option`), outcome probabilities, `$Val` per player |

### Team-Level (`team_queries.py`)

| Function | Returns |
|---|---|
| `get_summary(team_id)` | KPI summary — game date, surplus, FV50 count |
| `get_standings()` | All teams — W/L, pct, RS/RA, pythagorean, division |
| `get_division_standings(team_id)` | Division-only standings for a team |
| `get_power_rankings()` | Composite rankings — pyth, L10, RD/G, surplus |
| `get_head_to_head_matrix(year)` | Full NxN team-vs-team W-L matrix for a season |
| `get_roster(team_id)` | Full MLB roster with ratings, contract, surplus |
| `get_roster_hitters(team_id)` | Hitters with full stat lines and ratings |
| `get_roster_pitchers(team_id)` | Pitchers with full stat lines and ratings |
| `get_farm(team_id)` | Minor league prospects with FV, level, surplus |
| `get_team_stats(team_id)` | Team batting/pitching aggregates with league ranks |
| `get_contracts(team_id)` | All contracts with year-by-year salaries |
| `get_payroll_summary(team_id)` | Payroll breakdown by category |
| `get_upcoming_fa(team_id)` | Players approaching free agency |
| `get_surplus_leaders(team_id)` | Top surplus contributors (MLB + farm) |
| `get_age_distribution(team_id)` | Roster age breakdown |
| `get_record_breakdown(team_id)` | W/L by month, home/away, vs division, etc. |
| `get_recent_games(team_id, n)` | Last N games with scores, pitchers, opponents |
| `get_stat_leaders(team_id)` | Team-internal stat leaders by category |
| `get_depth_chart(team_id)` | Positional depth with WAR projections |
| `get_draft_org_depth(team_id)` | Per-position positive surplus totals (MLB + farm split) for draft needs panel. Returns `{pos: {mlb, farm, total}}` in $M. |
| `get_org_overview(team_id)` | Cross-level org summary: position depth, surplus leaders, retention priorities, payroll shape |
| `get_farm_depth(team_id)` | Farm system depth by positional bucket |
| `get_affiliates(team_id)` | List of minor league affiliates for an MLB team (team_id, name, level) |
| `get_minor_league_team(team_id)` | Minor league team info: name, level, parent org, sibling affiliates |
| `get_minor_league_roster(team_id)` | Full roster sorted by composite with FV/risk/surplus |
| `get_minor_league_notables(team_id)` | Notable players: FV 45+, composite 50+, ceiling 55+, or young-for-level |

### Player-Level (`player_queries.py`)

| Function | Returns |
|---|---|
| `get_player(pid)` | Full player detail — bio, ratings, stats, contract, surplus, splits, percentiles, evaluation panel data (composite/ceiling with MLB context) |
| `get_player_popup(pid)` | Lightweight popup data — bio, key ratings, current stats, surplus |
| `_mlb_context(conn, bucket, composite, ceiling)` | MLB percentile + tier label for composite and ceiling vs all MLB players at the position |

### Trade-Level (`trade_queries.py`)

| Function | Returns |
|---|---|
| `get_org_players(team_id)` | Full org roster (MLB + farm) with Ovr/Pot/FV/surplus/WAR for trade tab |
| `get_trade_value(player_id, retention_pct)` | Single-player valuation — contract breakdown or prospect surplus + career outcomes |

---

## Data Files (Read-Only Context)

| File | Contents |
|---|---|
| `config/state.json` | Current game date, year, my_team_id |
| `config/league_settings.json` | Division structure, team maps, rating thresholds |
| `config/league_averages.json` | League-wide batting/pitching averages, $/WAR |
| `config/model_weights.json` | Calibrated valuation tables (OVR_TO_WAR, COMPOSITE_TO_WAR, FV, ARB, scarcity) |
| `config/tool_weights.json` | Per-league calibrated tool weights from component regression (hitting→WAR, baserunning→SB, fielding→ZR, pitching→FIP). Source: `calibrated` or `default`. Includes R² and sample size per bucket. Hitting regression excludes `avoid_k` and `speed`. |
| `history/prospects.json` | Scouting summaries + FV history by player_id |
| `history/roster_notes.json` | MLB player summaries by player_id |
| `reports/<year>/*.md` | Published farm reports, roster analyses, org overviews |

---

## DB Tables (league.db)

All accessed via the query functions above. Direct SQL is rarely needed.

| Table | Key Contents |
|---|---|
| `players` | All players — name, age, team, level, position, role |
| `teams` | Team ID → name mapping |
| `ratings` | Scouting ratings — all attributes, L/R splits, defensive tools |
| `contracts` | Active contracts — salary schedule, options, NTC |
| `batting_stats` | MLB batting by player/year/split |
| `pitching_stats` | MLB pitching by player/year/split |
| `team_batting_stats` | Team-level batting aggregates |
| `team_pitching_stats` | Team-level pitching aggregates |
| `fielding_stats` | Fielding by player/position |
| `games` | Game results — scores, WP/LP/SV, dates |
| `prospect_fv` | FV grades for all non-MLB prospects |
| `player_surplus` | Surplus value for all MLB players |

---

## Known Data Limitations

- **No minor league stats** — the StatsPlus API returns empty for minor league player IDs.
  Farm analysis relies on ratings + age-vs-level context only.
- **No play-by-play or box scores** — game results include final score and pitchers only.
- **No injury data** — StatsPlus doesn't expose DL/IL status.
- **No transaction log** — trades, callups, DFA are not tracked historically.
- **Ratings are scouted** — accuracy varies by scout quality. `Acc` field indicates
  reliability (VH/H/A/L). Only mention low accuracy when `Acc = L`.
