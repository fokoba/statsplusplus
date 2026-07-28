# Changelog

Completed and deferred work items, organized by session. Moved from `task_list.md` to keep the task list focused on pending work.

---

## Session 69 (2026-07-27)

### Features

- **StatsPlus API integration — Phase 1 complete** (`db.py`, `refresh.py`, `arb_model.py`, `free_agents.py`, `trade_targets.py`, `team_queries.py`, `team.html`) — Expanded player data from the StatsPlus API. Schema migration adds 30+ columns to the `players` table covering injury status, exact MLB service time, roster status flags, draft history, and demographics. All fields stored on refresh.

- **Injury & DL status badges** (`team_queries.py`, `team.html`, `style.css`) — Team roster pages (Hitters/Pitchers tabs) display inline DL/DFA/WVR badges next to player names with tooltip showing days remaining. Trade targets tool shows 🏥 annotations for injured players and auto-skips DFA'd players. New `--exclude-injured` CLI flag on `trade_targets.py`.

- **Exact service time replaces estimation** (`arb_model.py`, `free_agents.py`) — `estimate_service_time()` now reads exact `mlb_service_days` from the DB (172 days = 1 year), falling back to the games-based heuristic only when the data isn't available. `free_agents.py` arb/FA classification uses exact service time directly. Enables precise Super Two detection and deterministic control period calculation.

- **Minor league stats pipeline** (`client.py`, `refresh.py`, `db.py`, `player_queries.py`) — Full MiLB batting and pitching stats now ingested during refresh. Discovers all minor league IDs via `/lgdata` (13 leagues for eMLB), fetches current-year stats for each, stores with `league_id` column in existing stat tables (NULL = MLB for backward compatibility). ~5,900 batting + 4,700 pitching rows. Player page query code returns `milb_bat_stats`/`milb_pit_stats` (template rendering pending).

- **New client methods** (`client.py`) — `get_lgdata()` (league structure/standings), `get_tradeblock()` (players on trade block), `get_ballparks()` (park factors). Storage/integration for tradeblock and ballparks deferred to Phase 3.

### Documentation

- **API impact analysis** (`docs/api_impact_analysis.md`) — Comprehensive mapping of all newly available StatsPlus API data and how it integrates with existing subsystems. Covers service time, injury, roster flags, standings, trade block, MiLB stats, expanded contracts, park factors, draft history, and OSA ratings. Includes dependency graph and implementation priority matrix.

- **API integration roadmap** (`docs/task_list.md`) — Phased implementation plan added to task list. Phase 1 (player fields) and Phase 2a-c (MiLB pipeline) marked complete.

- **Client reference updated** (`docs/client_reference.md`) — Full documentation of expanded `/players` fields, new endpoints (`/lgdata`, `/tradeblock`, `/ballparks`), and MiLB stat fetching via `lid` parameter.

- **Trade analyst steering updated** (`.kiro/steering/trade-analyst.md`) — Noted that injury data and roster status flags are now available in the DB, reducing the number of questions the agent needs to ask the user.

---

## Session 68 (2026-07-27)

### Bug Fixes

- **Stat history excluded completed season in offseason** (`war_model.py`) — `load_stat_history()` used `year < game_year` to exclude the "current partial season," but when the game date was in the offseason (November+), the just-completed season was incorrectly excluded. Every player's stat-weighted WAR projection ignored their most recent full season. Example: Josh Corr's 2033 season (126 IP, 3.15 ERA, 1.51 blended WAR) was completely invisible, causing his surplus to show as -$1.2M instead of ~+$10M. Affected all player valuations in offseason mode across all leagues.

- **Role-convert pitchers valued only on new-role data** (`war_model.py`) — Pitchers who changed roles (SP→RP or RP→SP) were evaluated only on their new-role seasons. If they had just one bad year in the new role (e.g., Josh Moran: 3 years of solid SP work then one bad RP season), the entire SP history was ignored. Now blends prior-role history (with appropriate discount) when fewer than 2 full seasons exist in the new role. Moran's projection went from -0.42 WAR (one bad RP year only) to 0.13 WAR (blended with discounted SP history).

### Improvements

- **Stat projection model overhaul** (`war_model.py`) — Replaced the 3-year `[3, 2, 1]` weighting scheme with a 4-year `[3, 3, 2, 1]` window. More stable projections — one outlier year doesn't dominate, and equal weight on the two most recent seasons reflects that both are highly relevant. Older data tapers off but still contributes context.

- **Partial-season inclusion** (`war_model.py`) — Current year stats are now always loaded (no blanket exclusion). A `season_pct` field tracks season completeness (games played / 162). The most recent year's weight is scaled by this fraction, so mid-season data influences projections proportionally to sample size. Offseason (month ≥ 11) gets full weight automatically. April data barely registers (~0.12 weight); mid-season (~0.5) is meaningful but not dominant.

- **Standings `--team` flag** (`standings.py`) — Added `--team <ABBR>` option showing a team's actual record, division leaders, wild card race with GB, and pythagorean comparison. Prevents needing ad-hoc SQL against the games table (which has counterintuitive column naming: `runs0` = away, `runs1` = home). Added helper functions `all_actual_records()`, `league_standings_actual()`, `playoff_picture()` as importable utilities.

- **Games table documentation** (`db.py`) — Added inline comments clarifying `runs0 = AWAY team runs` and `runs1 = HOME team runs` to prevent future confusion.

### Documentation

- **Consolidation premium** (`.kiro/steering/trade-analyst.md`, `docs/tools_reference.md`) — Documented that the trade calculator's raw surplus balance doesn't account for consolidation value. In N-for-1 trades, the consolidated side should show a surplus advantage (10-15% for 2-for-1, 15-25% for 3-for-1) before the deal is considered balanced.

- **`actual_record()` docstring** (`standings.py`) — Added explicit documentation of the `runs0`/`runs1` column semantics to prevent recurring W-L reversal bugs.

### Bug Fixes (continued — Jul 27)

- **Traded player stats showing wrong team** (`team_queries.py`) — `get_roster()`, `get_roster_hitters()`, `get_roster_pitchers()` loaded stats without `team_id` filter, causing traded players' prior-team stats to overwrite current-team stats.

- **SP rankings missing role=12 starters** (`queries.py`) — Positional rankings hardcoded `role=11` as SP. Leagues like PPL use role=12 for some full-time starters. Now classifies from actual GS/G ratio with role-code fallback.

- **Draft board crash on missing pool** (`draft_board.py`) — `sys.exit()` raised `SystemExit` (not caught by `except Exception`) crashing Flask. Replaced with `FileNotFoundError`. Sim/Auto-Draft buttons disabled in UI when no pool uploaded.

- **Onboarding crash on brand-new leagues** (`db.py`, `app.py`) — Merged PR #2 from fokoba. `get_conn()` crashed when `data/<slug>/` didn't exist; template context queried DB before league was configured.

- **Blank valuation tab for older non-MLB players** (`player_queries.py`) — Merged PR #4 from fokoba. Players over age 24 at non-MLB levels (e.g. 26yo A-ball catcher) had no `prospect_fv` row and fell through all fallback paths. Now computes real valuation using actual level/age.

### Features (continued — Jul 27)

- **WAR display limited to 1 decimal** (`percentiles.py`, `player.html`) — Pitcher percentile history showed 3 decimals (e.g. 10.023). Fixed format across all WAR displays.

- **WAR rate stats** (`percentiles.py`) — WAR/600 PA (hitters) and WAR/200 IP (pitchers) in the Advanced tab percentile history. Contextualizes raw WAR by playing time.

- **GB% context label on pitcher ratings** (`player_queries.py`, `refresh.py`) — Shows "Extreme GB / High GB / Average / Fly ball / Extreme FB" based on z-score against league distribution. League-normalized (not hardcoded).

- **GB% in pitcher percentile rankings** (`percentiles.py`, `refresh.py`) — New stat with expected value from league-calibrated regression (gb_rating → actual GB%). Hot/Cold tags. Gracefully hidden for leagues without GB data.

- **Minor league roster redesign** (`team_queries.py`, `team_minor.html`) — Tabbed Hitters/Pitchers view with position first, cur/pot tool format, B/T handedness, position-specific defense, positional sorting with data-sort-value. 40-man badges on notable cards.

- **README screenshots** — Added 6 screenshots (league overview, team page, depth chart, player page, prospects, draft board) and updated project structure + CLI tools section.

### Known Issues (for next session)

- **IP display as 3.3 instead of 3.1** — Advanced tab career row uses float formatting instead of baseball fractional notation. Also causes floating point drift (581.3000000000001).
- **CSV export for minor league rosters** — Feature request from beta tester. Extend export to MiLB team pages or add "all minor leagues" org view.

---

## Session 67 (2026-07-22)

### Bug Fixes

- **Pitcher stat-blending completely broken** (`evaluation_engine.py`) — MLB pitchers never got stat-blended composite scores. The SQL query in `_load_qualifying_stat_seasons()` was missing `era` in the SELECT clause, causing `_compute_stat_signal()` to skip every pitcher season. All pitchers showed tool-only composites with no performance adjustment. One-line fix: added `era` to the SELECT.

- **Depth chart rankings broken for leagues without OVR** (`team_queries.py`) — Leagues like PPL where the API doesn't provide OVR ratings got meaningless depth charts (all players at ~0 WAR). Added `_resolve_depth_score()` helper with a 3-tier fallback: composite_score → OVR → tool-derived estimate. Applied consistently across MLB players, prospects, and league-wide position rankings. Also removed orphaned dead code block.

- **Depth chart position colors always blue for SP/RP** (`team.html`) — Color system used absolute WAR thresholds (≥5 = elite) which are trivially exceeded when summing 5+ pitchers. Replaced with `rankColorClass()` that colors based on league-wide position rank percentile: top 25% = blue, 25-50% = green, 50-75% = neutral, bottom 25% = red. Now works correctly for all positions.

### Tests

- **Pitcher stat-blending regression test** (`test_evaluation_pipeline.py`) — Seeds an MLB pitcher with strong ERA, verifies composite ≠ tool_only after engine runs. Would have caught the missing-ERA bug.

- **Depth chart fallback tests** (`test_team_queries.py`) — 8-test `TestResolveDepthScore` class covering the full fallback chain for both hitters and pitchers, including all-NULL graceful degradation.

### Backlog

- Added comprehensive regression testing item to task_list.md covering systematic gaps in test coverage (pitcher evaluation, depth chart edge cases, cross-league scenarios).

---

## Session 66 (2026-07-12)

### Bug Fixes

- **Career outcome probabilities wildly inconsistent** (`prospect_value.py`, `player_queries.py`) — Player page showed 20% Contributor for an FV 70 SP while draft board showed 46%. Root causes: (1) case-sensitivity bug in `DEVELOPMENT_DISCOUNT` lookup — "aaa" didn't match "AAA" keys, causing all non-MLB levels to use 0.45 default; (2) "Draft"/"College"/"HS" levels got absurd dev discounts because norm_age=18 penalized college players; (3) player page wasn't passing composite/ceiling to the outcome function. Fix: `_age_adjusted_discount` now uses composite score to estimate effective minor league level for amateur players, added case-normalization alias map, player page falls back to composite_score/true_ceiling when OVR/POT are null.

- **Prospect surplus values too low** (`prospect_value.py`) — Same root cause as above: `YEARS_TO_MLB` lookup used literal "Draft"/"College"/"HS" labels (not in table, defaulting to 3.5 years). Now uses composite-based level estimation. Davidson went from ETA 3.5yr/$96K to ETA 0.5yr/$503K.

- **OVR/POT vs COMP/CEIL priority** (`player_queries.py`) — Valuation now uses composite_score/ceiling_score first, falling back to game OVR/POT only when composite doesn't exist. Our calibrated evaluation is more predictive.

- **Draft board ExpRd sorting broken** (`league.html`) — Clicking ExpRd column did nothing. Root cause: `applyDraftFilters()` rebuilds DOM from scratch on every filter change, destroying any DOM-level sort. Fix: implemented `draftSortState` + `initDraftSort()` that sorts the JS data array before rendering. Supports toggle (asc/desc) and persists across filter changes.

- **Draft board sort toggle only worked once** — Sort direction was read from DOM class which got cleared on re-render. Now tracked in `draftSortState` variable; clicking same column flips direction.

- **$Val column uninformative** (`league.html`, `queries.py`) — Values showed "$0.2M" or "$0.1M" with no differentiation. Fixed: (1) `fmtSurplus()` now shows "$139K", "$503K", "$1.2M" etc.; (2) surplus stored with 3 decimal places; (3) uses canonical `prospect_fv.prospect_surplus` instead of recalculating.

- **Jack Harris bucketed as SS** (`player_utils.py`) — Listed as SS (pos=6) with pot_ss=35. Old fallback trusted game position if IFR was decent. Now uses calibrated positional models.

- **Draft upload list included already-drafted players** (`app.py`, `league.html`) — Upload list now excludes players already picked (passed from localStorage-persisted draft picks).

- **Open Folder button silently failed** (`app.py`, `league.html`) — `xdg-open` not installed. Now tries multiple file managers, returns useful error, JS shows path in alert with clipboard copy fallback.

### Features

- **Positional rating estimation model** (`calibrate.py`, `player_utils.py`) — OLS regression models predict positional ratings from defensive tools (IFR, IFA, IFE, TDP, OFR, OFA, OFE, Height, CArm/Blk/Frm). R² 0.92-0.96 across 8 positions. Calibrated per league, stored in `model_weights.json`. Used in `assign_bucket` fallback when no positional grade meets thresholds.

- **Draft pick persistence** (`league.html`) — Picked players stored in localStorage by league slug. Survives page navigation. Server picks overlay on load.

- **Auto-fetch picks on tab open** (`league.html`) — First click on Draft tab triggers update automatically.

- **Raw/ceiling surplus display** (`league.html`, `queries.py`) — Draft board shows adjusted surplus (risk-weighted) with tooltip showing ceiling value (best-case undiscounted). Detail panel shows both. Ceiling always ≥ adjusted.

- **Realization-aware profile pills** (`league.html`) — Profile labels (Safe Star, Projection, Boom/Bust, etc.) now factor in tool realization (comp/pot ratio). Raw prospects get "Projection" or "Boom/Bust" instead of "Safe Star" regardless of outcome probabilities.

- **Realization-scaled outcome variance** (`prospect_value.py`) — Spread parameter in `_p_above()` narrows from 0.40 (raw prospect) to 0.20 (fully realized) based on comp/pot ratio. Near-MLB players get tighter, more confident outcome distributions.

---

## Session 65 (2026-07-11)

### Bug Fixes

- **Pitcher W-L record in recent games** (`team_queries.py`) — Running W-L counter showed incomplete records (wins defaulting to 0 on loss dates). Fixed to update all running totals per appearance. Optimized query: ~26 rows instead of ~555.

- **Foreign league players in rankings** (`league_config.py`, `fv_calc.py`, `queries.py`) — Japanese league players appeared in prospect lists and positional rankings. Fixed with org filter at multiple layers.

- **Minor league roster position display** (`team_queries.py`, `team_minor.html`) — Non-prospect players showed blank position. Now falls back to game position. Pos column left-aligned.

- **Team roster vs L/vs R toggle empty** (`refresh.py`) — Split stats (split_id 2/3) were never fetched. Now pulled for current + prior year, with backfill for historical years.

- **Year selector showing same data** (`app.py`) — Missing return statement in `/api/player-percentiles` endpoint after adding split history endpoint.

- **Advanced tab: split toggle reset** (`player.html`) — Show Percentiles/Values mode now persists across Overall/vs L/vs R switches. Career row restored in JS-rendered tables.

- **Career averages incorrect** (`percentiles.py`, `player.html`) — Career row now uses PA-weighted averages (not simple mean of yearly rates). Proper format (`.266` not `0.3`). Toggles between stat values and percentiles.

- **Draft simulation showing no players** (`fv_calc.py`) — Level 10 (college) and 11 (HS) players were missing from `LEVEL_INT_KEY` mapping, causing silent skip during FV evaluation. All 2604 draft pool players now evaluated.

### Features

- **CSV export** (`sort.js`, `league.html`, `team.html`) — Generic `exportTableCSV()` function. Export buttons on draft board, team roster (hitters/pitchers), and prospect list. Respects active filters and views.

- **L/R split toggle on Advanced tab** (`percentiles.py`, `player.html`, `app.py`) — Overall/vs L/vs R buttons on percentile history panel. New API endpoint `/api/player-percentile-history/<pid>?split=1|2|3`.

- **Historical L/R splits** (`refresh.py`) — Split stats now included in the 15-year historical loop with a one-time backfill pass.

- **Discord webhook integration** (`scripts/discord_post.py`) — Patch notes posting via webhook. Subcommands: latest, preview, message. `--title` flag for custom naming. Config in gitignored `data/discord_config.json`. Added to session workflow in steering file.

- **Discord widget on settings page** (`settings.html`) — Community panel with embedded server widget.

- **Settings page layout** (`style.css`) — Two-column grid replacing narrow single column.

---

## Session 64 (2026-07-10)

### Bug Fixes

- **Pitcher W-L record in recent games** (`team_queries.py`) — Running W-L counter only stored a pitcher's win count on dates they won (and vice versa). When displaying a loss date, the win count defaulted to 0. Fixed to update all running totals whenever a pitcher appears in any role. Also optimized query from scanning all season games (~555 rows) to only games involving relevant pitchers (~26 rows).

- **Foreign league players in prospect/positional rankings** (`league_config.py`, `fv_calc.py`, `queries.py`) — Japanese league players (Chunichi, Yomiuri, etc.) appeared in prospect lists and positional rankings. Fixed: `mlb_team_ids()` now intersects with configured league teams, `fv_calc.py` filters out foreign orgs before evaluation, positional rankings MLB section applies the same org filter.

### Features

- **Discord webhook integration** (`scripts/discord_post.py`) — New script posts formatted changelog entries to Discord via webhook. Subcommands: `latest` (post), `preview` (dry run), `message` (custom text). Config stored in `data/discord_config.json` (gitignored). Added to end-of-session workflow in dev-agent steering.

- **Discord widget on settings page** (`settings.html`) — Community section with embedded Discord server widget (dark theme).

- **Settings page layout** (`style.css`, `settings.html`) — Two-column grid layout replacing single narrow column. League Structure spans full width. Responsive collapse on narrow screens.

---

## Session 63 (2026-07-09)

### Features

- **Percentile rankings: offseason display** (`percentiles.py`, `player_queries.py`, `player.html`) — Percentile panels now auto-fall back to the most recent year with stats during offseason (was blank). Year selector dropdown on batting/pitching and fielding percentile panels allows browsing any historical season. Tags (hot/cold/lucky/unlucky) disabled for non-current years. New API endpoint `GET /api/player-percentiles/<pid>?year=YYYY&type=main|fielding` for dynamic year switching.

- **Advanced tab: percentile history** (`player.html`, `style.css`, `percentiles.py`) — New "Advanced" tab on player pages with:
  - Color-coded percentile history table (blue→white→red, Savant-inspired palette)
  - PA/IP sample size column per year
  - Career averages row (qualified seasons only)
  - "Show Percentiles" / "Show Values" toggle with bidirectional hover tooltips
  - Unqualified seasons visually dimmed
  - Per-position fielding percentile history table
  - Compact WAR diverging bar chart in panel header

- **Historical fielding stats** (`refresh.py`) — Fielding stats now included in the historical stats loop (up to 15 years back). Backfill pass detects leagues with batting but missing fielding data and fills the gap on next refresh. Enables multi-year fielding percentiles and player page fielding history.

### Backlog Added

- Discord integration (long-term — outbound patch notes, inbound feedback)
- Split-based composite ratings on team roster (Koba request — vR/vL toggle)

---

## Session 62 (2026-07-09)

### Bug Fixes

- **Org page position depth blank** (`team_queries.py`, `refresh.py`) — Org overview showed no position players (only pitchers and prospects). Root cause: `fielding_stats` table was empty for leagues onboarded mid-season — refresh only fetched current-year fielding, which is empty during spring training. Fix: (1) refresh now always re-fetches prior-year fielding stats (matching the batting/pitching fix from Session 61), (2) `get_org_overview()` falls back to `batting_stats` + `players.pos` when fielding data is unavailable. Reported by Koba.

### Investigation

- **$/WAR discrepancy across environments** — PPL showed $153K/WAR on one machine vs $22K on another. Same 48 contracts, same $3.5M salary sum, but WAR denominator differed (22.8 vs 158.4). Root cause was the incomplete prior-year stats bug (fixed Session 61) — one machine had partial 1953 data inflating the rate. $22K/WAR is correct for PPL's financial scale ($6,600 minimum salary).

---

## Session 61 (2026-07-08)

### Bug Fixes

- **RP tweener classification** (`draft_board.py`) — The RP discount logic classified any RP with STM ≥ 30 as a "tweener" (−2 penalty). Fixed to require both STM ≥ 35 AND 3+ pitches with pot ≥ 45 for tweener status. Pure relievers (e.g., 2 pitches, STM 30) now correctly get the full −5 penalty.
- **ADP ceiling-based sort for RPs** (`draft_board.py`) — In leagues without POT (using true_ceiling as ADP proxy), RPs sorted at the top (ceiling 72 → ExpRd 1). Added −15 penalty to RP ceilings in the ADP sort since other GMs also devalue relievers.
- **Incomplete prior-year stats** (`refresh.py`) — The refresh pipeline only fetched historical years not already in the DB. A mid-season refresh stored partial prior-year data, and subsequent refreshes skipped it (year already exists). This caused incomplete WAR totals that inflated $/WAR by 7× ($153K vs correct $21K in PPL). Fix: always re-fetch year−1 stats regardless of DB state.

### UI Changes

- **Acc column on draft board** (`league.html`) — Added scouting accuracy as a charPill in the base columns of the draft board table (visible in All/Hitters/Pitchers views). Removed duplicate Acc column from hit/pit detail sections.

---

## Session 60 (2026-07-05)

### Draft Board Settings Feature

**New module: `scripts/draft_settings.py`** — Per-league, per-round-group settings persistence and validation. Manages 11 configurable slider parameters (4 core, 3 Tier 2, 4 Tier 3) stored at `data/<league>/config/draft_settings.json`. Features:

- 5 discrete slider positions (0.0, 0.25, 0.5, 0.75, 1.0) mapping to labeled values
- Midpoint (0.5) reproduces exact original hardcoded behavior — full backwards compatibility
- User-defined round groups with independent parameter sets
- Presets (balanced, upside, conservative, org_needs) apply uniformly then allow per-group customization
- Validation, copy-between-groups, and default resolution

**Core sliders (always visible):**
1. Ceiling weight — how much ceiling bonus influences draft value
2. Risk tolerance — penalty magnitude for High/Extreme risk and Acc=L/VL
3. Needs weight — org need bonus strength (Rd3+ only)
4. Surplus weight — position-scaled surplus influence on list building

**Advanced sliders (collapsible):**
5. Balance strength — pitcher/hitter ratio enforcement
6. Arsenal weight — SP depth bonus/penalty magnitude
7. Personality weight — WE/INT/Lead influence
8. RP discount — pure reliever vs tweener penalty
9. Control penalty — SP with pot_ctrl < 45
10. Contact penalty — hitter with cnt<50/pow≥80/eye<70
11. Survival threshold — ADP survival window width

**`scripts/draft_board.py` refactoring:**
- `draft_value()` accepts optional `params` dict for all 11 slider parameters
- `build_pick_list()` accepts `settings` dict, resolves per-round parameters
- `simulate_draft()` passes settings through
- `compute_org_needs()` disabled for perpetual arb leagues (no FA departures = false positives)
- `compute_adp()` falls back to `true_ceiling` when POT unavailable
- CLI commands (`cmd_pick`, `cmd_upload`) auto-load settings from disk

**Web UI — 3 new API endpoints:**
- `GET /api/draft-settings` — load current settings
- `POST /api/draft-settings` — save settings (full settings object)
- `POST /api/draft-settings/copy` — copy one round group's params to another

**Web UI — settings modal on draft tab:**
- Gear button (⚙️) opens full settings modal with round group tabs
- 4 core sliders always visible, 7 advanced sliders behind collapsible toggle
- Preset buttons (Balanced, Upside, Conservative, Org Needs)
- Save/Reset/Copy functionality
- Auto-draft list and draft sim endpoints now load and apply saved settings

**Bug fixes (PPL/non-OVR leagues):**
- OVR/POT null display — falls back to composite_score/true_ceiling throughout draft UI
- Outcome probabilities (C%, R%, AS%, Bust%, Profile) now compute when OVR/POT null
- ADP/ExpRd no longer shows nonsensical rounds (e.g., Rd57 for #1 prospect) when POT unavailable
- Draft depth indicators now use league-relative thresholds (ratio to league avg: >1.2× = green, 0.6-1.2× = orange, <0.6× = red) instead of fixed dollar amounts that didn't scale across leagues
- `compute_org_needs()` no longer produces false positives in perpetual arbitration leagues

**Design decisions:**
- ADP falls back to `true_ceiling` when POT unavailable (for leagues without OVR/POT)
- Depth indicators use league-relative ratios rather than absolute dollar thresholds
- Settings are per-league (different leagues may need different draft strategies)

### Post-commit fixes (Session 60b)

- **JS slider state bugs** — `addDraftRoundGroup()`, `applyDraftPreset()`, and `resetDraftSettings()` only set 4 of 11 slider keys; advanced keys were dropped on group creation, preset application, and reset. Extracted `_defaultSliderSettings()` helper returning all 11 keys.
- **Documentation accuracy** — Fixed `docs/tools_reference.md` (wrong function names, wrong JSON structure, wrong preset names), `docs/system_overview.md` (copy endpoint description), and `docs/changelog.md` (preset names).
- **Test coverage** — Added 12 unit tests for `draft_settings.py` in `test_scripts.py` (validation, snapping, param mapping, round resolution, persistence roundtrip).
- **Draft agent steering** — Added `config/draft_settings.json` to data sources and documented settings-aware mode in `.kiro/steering/draft-agent.md`.
- **Org needs for perpetual arb leagues** — Implemented `_compute_org_needs_weakness()`: compares team's positional starter composite vs league median (3rd-best SP for rotation depth), gated by farm FV 50+ depth. Thresholds: +2 if ≥5 below median with no farm help, +1 if ≥2 below with no farm help, +1 if ≥8 below even with farm help. FA leagues remain on departure-based logic (`_compute_org_needs_departures()`). Added 6 unit tests covering both paths.
- **Arb salary model for perpetual arb leagues** — New `arb_salary_perpetual()` in `arb_model.py` uses a growth+ceiling formula calibrated from actual 1-year contract data. Growth: `min_sal + k × max(0, career_WAR - discount)^exp` (salary ramps with accumulated track record, "discount" captures proving-it threshold). Ceiling: `ceiling_pct × current_WAR × $/WAR` (caps salary at fraction of market value, allows decrease on decline). Calibration step added to `calibrate.py` — fits k, exp, discount, ceiling_pct from cross-sectional salary/career-WAR data. `contract_value.py` dispatches to the new model for perpetual arb leagues; FA leagues unchanged.
- **Prospect surplus uses perpetual arb salary model** — `prospect_value.py` now dispatches to `arb_salary_perpetual()` for perpetual arb leagues. Before: arb years projected $97K-$187K (scaled exponential) → negative surplus → total collapsed. After: salary grows with career WAR → positive surplus throughout control.
- **League structure detection fix** — Added connected-component fallback for leagues with no inter-league play (PPL: 2×8, balanced schedule, zero cross-league games). Previously each team became its own "division." Also added `manual_structure` flag to prevent overwrite on refresh.
- **Expanded standings tab** — New top-level "Standings" tab on league page with two sub-views: Expanded (actual W-L, pythagorean, luck delta, RS/RA/RD) and Head-to-Head (full NxN team-vs-team matrix with color coding).
- **Division display fix** — Single-division leagues (where div name = league name) now render correctly on the overview tab. Route falls back to raw div name when `"{short} {div_name}"` pattern doesn't match.
- **Player breadcrumb fix** — Prospect pages now show the minor league affiliate in the breadcrumb trail (PPL → Cubs → AAA Indianapolis → Player).

---

## Session 59 (2026-06-22)

### Draft Board: Balance, RP Discount, and Value Gap Override

**Pitcher/hitter balance adjustment** in `build_pick_list`:
- Tracks running pitcher/hitter ratio as the list is built.
- Applies a score bonus to the underrepresented type, scaling with picks made
  (minimal early where talent gaps are large, stronger in mid/late rounds).
- Parameters: `balance_target=0.45`, `balance_bonus=2.0` (keyword args with defaults).
- Result: list maintains 38-46% pitchers throughout 500 picks. Eliminates feast-or-famine
  runs (previously 0% pitchers in some 10-pick windows, 80% in others).
- `--no-balance` flag added to `pick` and `upload` CLI commands.

**RP tweener discount** in `draft_value`:
- Previously: flat -5 for all RP-bucketed players.
- Now: stamina ≥ 30 gets -2 (SP-upside tweener), stamina < 30 gets -5 (pure reliever).
- Added `r.stm` to `_BOARD_SQL` query.
- Prevents punishing fringe starters who happen to be bucketed as RP.

**Value gap override** in `build_pick_list`:
- If the best available player's `draft_value` exceeds the best survival-passing player
  by ≥3 points, take them regardless of ADP survival threshold.
- Prevents deferring elite prospects on speculative survival estimates (e.g., a board #7
  player available at pick #31 should never be deferred).

**Context:** Analysis of the eMLB 2033 draft revealed three issues:
1. The upload list produced 10+ consecutive hitter runs in mid-rounds, forcing manual
   pitcher intervention that resulted in weaker picks (Hams FV 35 RP, Stern FV 40 SP).
2. RP-bucketed pitchers with SP-level stamina (30-45) were over-penalized.
3. The survival model could theoretically defer a top-10 board player at pick 31
   if their POT-rank fell within the (permissive) threshold window.

### Draft Board: Added `r.stm` to board SQL

Minor schema addition to support the RP tweener logic.

---

## Session 58 (2026-05-12)

### Draft Board: Two-List Merge Algorithm + Valuation Enhancements

**Algorithm overhaul:** Replaced the `pick` and `upload` commands' ranking logic with a
two-list merge approach that maximizes total draft value across all rounds.

- **List A** (our evaluation): `draft_value` + position-scaled surplus weight.
- **List B** (OOTP evaluation): POT rank — what other managers see.
- **Merge:** At each slot, take the best from List A within the survival threshold
  (`30 + 6√pos`). Sleepers deferred to the slot where they're at risk.
- **Surplus weight:** `0.02 + 0.06/√pos` — heavier early (favors youth/upside), fades later.
- **Sim updated:** Other teams pick with position-scaled randomness (`exp = max(1.0, 2.8 - pick×0.012)`).
  Our picks use the pre-built pick list.
- **Web UI and CLI upload** now use `build_pick_list` (same as `pick` command).

**New draft_value penalties/bonuses:**

| Component | Value | Condition |
|-----------|-------|-----------|
| Control penalty | -3 | SP with pot_ctrl < 45 |
| Contact penalty | -2 | Hitter: cnt<50, pow≥80, eye<70 |
| Arsenal (bonus) | +0.5 | SP with 4+ pitches pot≥60; or 4+ cur≥35 at age 21+ |
| Arsenal (thin) | -1 to -2 | SP at/above norm age, <3 pitches cur≥35 (age-scaled) |
| Personality | ±0.9 max | WE: ±0.5, INT: ±0.25, Lead: ±0.15 |

**Young pitchers (< 21) never penalized for thin arsenal** — rawness expected.

**Threshold functions:** `_threshold_sqrt(base, scale)` and `_threshold_fixed(breakpoints)`
provide configurable survival logic. Default is sqrt.

**`build_urgency_list` preserved** as legacy; all active paths now use `build_pick_list`.

**Performance:** `build_pick_list` optimized from O(n²) to O(n log n) via cached scores
and periodic re-sort (5.15s → 0.03s for full 862-player pool). Enables 100-sim analysis
in seconds.

---

## Session 57 (2026-05-10)

### Evaluation Model Audit

Comprehensive review of the non-linear model changes from Session 56. Identified overfitting risks, redundancies, and dead code. Two changes made:

**Interaction terms disabled (dead code removal):**
- `contact_eye`, `power_eye` (hitters) and `stuff_mov` (pitchers) were never active — `tool_weights.json` lacked these keys, so the engine skipped them (weight=0 guard).
- Proper multivariate OLS confirms they add no explanatory power beyond the linear model: residual correlation with WAR is ~0.01 across all positions (N=520 hitters, N=871 pitchers).
- The product features are highly collinear with their components (r=0.85-0.93) — they're proxies for "both tools are good," not true synergies.
- The tool transform (1.3× above 60, 1.5× below 40) already captures the non-linearity these were meant to address.
- Disabled in both `evaluation_engine.py` (application) and `calibrate.py` (regression features).

**Carrying tool bonus on ceiling disabled:**
- Was adding +5 to +31 to `ceiling_score` for prospects with elite potential tools.
- Purely cosmetic — `ceiling_score` doesn't feed into FV grades, surplus, or draft rankings (all use `true_ceiling`).
- `true_ceiling` (without bonus) actually predicts developed composite better than `ceiling_score` (r=0.912 vs 0.897).
- Redundant with tool transform (1.3× above 60) + peak tool bonus (capped +10) already in `compute_ceiling`.
- Was already disabled for composite in Session 56; now consistent across both.

**No impact on model outputs:** Composite scores, FV grades, and surplus values are identical before and after. The interaction terms were never executing, and the ceiling bonus only affected display.

### Low-Hanging Fruit

- **Continuous FV for surplus interpolation** — `calc_fv_v2` now exposes `_fv_continuous` on the player dict (pre-rounding value). `fv_calc.py` passes this to `prospect_surplus` instead of the rounded 5-point tier. `peak_war()` already interpolates, so surplus now differentiates within FV tiers (e.g., FV 66.4 gets ~$2-3M more surplus than FV 65.0).
- **Draft board flag badges** — Acc=L (⚠ orange) and Extreme risk (☠ red) badges now display inline next to player names in the draft board table. Visible at a glance without reading the Acc column.
- **Snapshot test fragility** — `test_prospect_value.py` now stubs `dollars_per_war()` and `league_minimum()` to fixed values via `unittest.mock.patch`. Tests are deterministic regardless of `league_averages.json` changes. Added $/WAR scaling test.
- **1-20 ratings scale** — `norm()` and `norm_continuous()` now handle `"1-20"` scale (linear: 1→20, 20→80). Auto-detection: max rating ≤20 → 1-20. Added to settings and onboarding dropdowns.
- **Minor league notable filter tuning** — "Young for level" criterion now requires ceiling ≥ 45 in addition to age. Prevents every teenager at Intl/Rookie from qualifying. Intl: 9.7→5.9/team, Rookie: 6.2→4.7/team.

### Draft Board Fixes

- **College/HS filter** — Level filter was returning zero results because all amateur players were at level 0 (not 10/11). Now uses age-based detection: ≤18 = HS, 19+ = College.
- **COF surplus in needs panel** — Corner outfielders (bucket "COF") weren't mapping to the "LF/RF" display key because `display_pos("COF")` returns "OF". Now uses raw bucket for the mapping.
- **Position mismatch arrow** — Was using a separate defensive threshold check that disagreed with `assign_bucket`. Now compares listed position against bucket directly. Shows "LF/RF" instead of "OF" for corner outfielders. Suppresses arrow when listed pos matches bucket (no more "SS → SS").
- **Detail pane crash** — `/api/draft-detail/` was returning 500 for all players due to `_cfg()` typo (should be `_cfg`). Also: players with pitcher role but hitter tools (e.g., role=RP bucketed as 3B) now correctly show fielding/positions instead of pitcher template.
- **Defense display on player card** — Position ratings were filtered on raw 1-100 values before normalizing, hiding valid positions. Now normalizes first. Added `def_tools` (IF Range/Error/Arm, OF Range/Error/Arm, C tools) filtered to only show tools relevant to the player's eligible positions.

### Other Findings (no action needed)

- **Triple coverage (transform + floor + compensation):** Small overlap at 30-35 range, conceptually justified, <1 pt impact. Leave as-is.
- **Dynamic share reallocation:** <1 composite point impact, self-correcting via calibration. Leave as-is.
- **Arsenal bonus:** Redundant with stuff (r≈0 after controlling for core tools), but low impact (~0-2.5 pts). Flag for future cleanup.
- **Platoon penalty:** Never triggers for MLB pitchers (0 of 482). Harmless dead code.
- **RP model:** Stable (CV<0.05 across years). Movement dominance (0.55) is a real OOTP effect, not noise. Min-weight floor appropriate.

---

## Session 56 (2026-05-09)

### FV Distribution Calibration

Addressed EMLB FV inflation (was 6.9x FG at FV 50+, now 3.0x):

- **Closure-normalized bust discount** — `bust = target_product / closure` ensures leagues with higher closure rates (more survivorship bias) get proportionally lower bust credit. EMLB compressed significantly; VMLB unchanged.
- **Floor rounding for developing prospects** — Developing players (gap > 3) use floor rounding: must project to 50.0+ to get FV 50. Maxed players (gap ≤ 3) keep standard rounding since they've proven their level.

### Tool Interaction Terms

Added nonlinear interaction features to the tool weight calibration, capturing synergies the linear model misses:

- **contact × eye** (hitters): On-base synergy (+1.29 empirical). Players need both to get on base consistently.
- **power × eye** (hitters): Plate discipline enables power (+0.70). Eye gets into hitter's counts where power plays.
- **stuff × movement** (pitchers): Movement makes stuff unhittable (+1.09). Stuff alone can be squared up.

R² improvements: SS hitting 0.455→0.481, CF 0.476→0.552. Interaction terms applied as additive adjustments in `_offensive_grade_raw()` and `compute_composite_pitcher()`.

### Empirical Validation

- **Gelof comp analysis**: Found MLB players with similar profiles (contact ≤50, avoidK ≤45, speed ≥50, defense ≥55 at 2B/SS). Mean WAR = 1.6, confirming FV 50 is borderline correct for this profile in OOTP.
- **Negative compounding investigated**: Data shows threshold effect (one weak tool = big penalty, already captured by sub-MLB floor), not smooth compounding. No additional negative interactions needed.
- **Interaction term data quality**: Validated that findings are robust (large N, intuitive mechanisms, meaningful R² improvement). Stopped at three terms to avoid overfitting.

### Distribution Results

| Tier | EMLB Before | EMLB After | VMLB Before | VMLB After | FG |
|------|------------|-----------|------------|-----------|-----|
| 60+ | 1.6 | 1.1 | 0.3 | 0.1 | ~0.6 |
| 55+ | 6.9 | 3.3 | 1.7 | 1.1 | ~1.3 |
| 50+ | 24.9 | 10.8 | 11.1 | 6.7 | ~3.6 |
| 45+ | 62.4 | 31.0 | 42.4 | 29.8 | ~8.6 |

### Carrying Tool System

- Percentile-based calibration threshold (P85) replaces fixed 65 — adapts to league distributions
- VMLB now calibrates all 7 positions (was only COF and 1B)
- Default merge fills missing positions from hardcoded defaults
- Dynamic application threshold matches calibration threshold
- **Carrying tool bonus disabled** — redundant with tool transform (1.2× above 60) + interaction terms + position-specific weights. Was producing +15-18 composite from single elite tools.

### Comp-Based FV Validation Tool

- New CLI: `scripts/comp_validate.py` — finds all MLB player-seasons matching a tool profile, shows WAR distribution
- Supports hitters and pitchers, current or ceiling tools, `--year`/`--recent` filters
- WAR rate-normalized (per 600PA / 180IP) to handle partial seasons
- Web UI: "Ceiling Profile" summary on prospect pages using potential ratings
- Tooltip distinguishes from Career Outcomes (ceiling IF realized vs probability OF realizing)
- Data limitation documented: uses current ratings vs historical stats; will improve as `ratings_history` accumulates

### Model Audit

- Confirmed no double-counting between tool transform, interaction terms, and position weights
- Sub-MLB floor penalty + tool transform overlap at low end is small (0.3-4 pts) and doesn't affect FV grades
- Negative tool interactions investigated — data shows threshold effect, not smooth compounding
- Composite→WAR R² = 0.334 (hitters), 0.185 (pitchers) — reasonable for tool-only model

### Surplus Model Fixes

- **Arb calibration**: Added WAR ≥ 1.0 floor, outlier cap (pct < 1.5), N ≥ 10 minimum, monotonic enforcement. EMLB: {1:0.71, 2:0.34, 3:0.45} → {1:0.24, 2:0.24, 3:0.32}.
- **Arb salary model**: Switched from flat-percentage (`ARB_PCT × WAR × $/WAR`) to raise-based (`arb_model.arb_salary`). Arb salaries now properly escalate year over year.
- **Discount mismatch**: Salary now discounted to present value same as market value. Was comparing discounted value against undiscounted salary.
- **$/WAR calculation**: Now uses FA market rate (multi-year contracts, age 28+) instead of all-contract average. EMLB: $6.2M → $7.75M. VMLB: $8.7M → $9.8M.
- **contract_value.py**: Uses `true_ceiling` instead of `ceiling_score` for development projection. Fixes inflated WAR projections for maxed players.
- **UI**: Surplus range now formatted with money filter. Adjusted surplus tooltip explains risk discounting.

---

## Session 55

**Minor League Team Pages:**
- `/team/<id>` now serves minor league teams with dedicated `team_minor.html` template
- Notables section: prospect cards (FV 45+) + "worth tracking" players (composite ≥ 50, ceiling ≥ 55, young-for-level)
- Full roster table sorted by composite with FV/risk/surplus
- Unified `org-nav` component: shared [MLB] [AAA] [AA] [A] [Rookie] navigation bar on both MLB and minor league pages
- Configurable filter thresholds (`NOTABLE_MIN_*` constants in team_queries.py)

**Player Page Fixes:**
- `true_ceiling` displayed everywhere instead of raw `ceiling_score` (header, eval panel, league hover)
- MLB positional context (rank #X/Y) now shown for all players with composite scores, not just MLB roster players
- Ceiling tier label uses `true_ceiling` for consistency

**Evaluation Engine:**
- ERA- replaces FIP for pitcher stat blending (OOTP WAR is RA9-based; FIP penalized contact managers)
- Arb salary scaling only applies when league min < 50% of default (fixes vMLB 22% reduction)

**Draft Board — Major Overhaul:**
- ADP (Average Draft Position): ranks class by POT, compares to FV rank, labels Sleeper/Value/Goes Early/Reach
- Draft simulation (`sim` command): other teams pick by POT with randomness, we pick by "now or never" logic
- Urgency-greedy list building: at each position, prefer players who'll be gone soon unless a sleeper is significantly better
- Threshold fades by round: Rd1-2 strongly prefer urgent (threshold 10), Rd3-4 moderate (5), Rd5+ pure BPA
- Org needs: computed from MLB departures vs farm depth, applied as tiebreaker in Rd3+
- Draft value formula: FV + ceiling bonus + RP discount (-5) + Acc penalty (L: -2, VL: -4) + risk penalty (Extreme: -3, High: -1) + needs
- RP excluded from org needs (SP prospects convert naturally)
- Web UI: 🎲 Sim button + 📋 Auto-Draft List button + 📂 Open Folder button in draft tab
- API endpoints: `/api/draft-sim`, `/api/draft-upload-list`, `/api/open-file-location`
- Draft board FV values now use canonical `prospect_fv` table (was recalculating with inflated dev_weight)
- Refactored `draft_board.py` into SOLID layers: Data, Valuation, Strategy, Display, CLI Commands
- Public API for web: `load_board()`, `draft_value()`, `compute_adp()`, `compute_org_needs()`, `build_urgency_list()`, `simulate_draft()`

---

## Session 54

**Multi-League Compatibility (PPL — 1950s historical league):**
- Full support for leagues without OVR/POT ratings (evaluation engine does all heavy lifting)
- Reordered refresh pipeline: eval_engine → calibrate → fv_calc (single pass, no second refresh needed)
- Removed hardcoded 2005 floor from historical stats pull (supports any era)
- Year derived from API game_date, not stale state.json
- League structure detection falls back to prior year team stats during spring training
- Added `manual_structure` flag to prevent auto-detection from overwriting manual config
- League averages and stat percentiles handle NULL gracefully (spring training)
- Team stats pulled for historical years too (needed for standings)

**Preseason Support:**
- Standings show all teams with 0-0 during preseason
- Roster pages show players without current-year stats
- `stats_year` helper in team_queries uses most recent year with data
- Phase detection: "Spring Training" when no games played
- Division standings fall back to full league when divisions have 1 team

**Financial Model — Low-Salary Leagues:**
- `perpetual_arb` league setting: no free agency, all players under team control until age 38
- Arb salary scaling for drastically different salary environments (ratio < 0.5 only)
- `dollars_per_war` scales default by league salary level when uncalibrated
- `$/WAR` calculation threshold scales with league minimum (was hardcoded $5M)
- `|money` Jinja filter: auto-selects $K vs $M format based on value
- All salary displays in team.html and player.html use `|money` filter
- Surplus projection stores raw values, formatted at display time

**Evaluation Engine Fixes:**
- ERA- replaces FIP for pitcher stat blending (OOTP WAR is RA9-based)
- Two-way player detection: 250 AB threshold for pitchers in no-DH leagues
- SP bucketing trusts game starter role when stamina ≥ 35
- `calc_fv_v2` guards against None Ovr/Pot
- `project_ovr` handles None inputs gracefully
- `true_ceiling` displayed everywhere instead of raw `ceiling_score`

**Draft Board (continued from Session 53):**
- Draft value sort: `FV + (ceiling-55) × 0.2 + ctl_penalty`
- Removed Acc penalty from sort (scouting informs manual adjustments)
- SP control < 45 penalty (-3) for reliever risk
- Updated draft-agent.md with final design

**UI Fixes:**
- Player page: hide Ovr/Pot when NULL, show true_ceiling consistently
- Career outcomes fix for MLB players with prospect_fv entry (_comp_kw NameError)
- pos_avg_war marker clamped to chart max when off-scale
- stat_row macro guards against None values
- Team page: team_names_map/team_abbr_map fall back to DB when settings empty
- Depth chart uses composite_score as OVR fallback

**Infrastructure:**
- Detailed logging throughout refresh pipeline (row counts, timing, skip reasons)
- `perpetual_arb` toggle in Settings UI
- `ratings_scale: "20-80"` support confirmed working

---

## Session 53

**FV Model Improvements:**
- Bust discount recalibrated: 0.55-0.85 (was 0.30-0.60). Empirical validation shows MLB players realize 92% of ceiling on average.
- FV ceiling cap: grade cannot exceed `true_ceiling - 3`. Low-ceiling organizational players correctly grade FV 45 instead of 50.
- Offensive ceiling cap: hitters with offensive ceiling < 45 capped at FV 50. Prevents defense/speed from inflating grades for players who can't hit (Victor Scott II: FV 55 → 50).

**Evaluation Engine:**
- SP true_ceiling now uses potential arsenal (was using current pitch ratings). SP prospect ceilings increase 1-4 points. Harry Nathan correctly grades FV 65.
- `assign_bucket` fallback validates athleticism: SS/2B listed players with IFR < 50 downgraded to 1B (Juan Pereira fix).

**Draft System (new):**
- `scripts/draft_board.py` CLI tool with 5 modes: board, available, pick, upload, compare.
- Draft pool players (level 0) now included in `prospect_fv` via fv_calc.
- `LEVEL_NORM_AGE["draft"] = 18` and `LEVEL_INT_KEY[0] = "draft"` added.
- `.kiro/steering/draft-agent.md` — full agent definition with evaluation framework, scouting priority system, and output conventions.
- Draft value sort: `FV + (ceiling-55) × 0.2 + ctl_penalty`. No Acc penalty — scouting informs manual adjustments.
- Commissioner list output uses game positions (not evaluation buckets).

**Bug Fixes:**
- `prospect_fv.risk` column missing on eMLB — ran eval_engine + fv_calc to populate.
- `ratings.true_ceiling` missing on eMLB — ran eval_engine to add column.

---

## Session 52 (2026-05-06)

### Evaluation Engine — Compensation, Baserunning, Defense, and FV Model Overhaul

Major rework of the tool compensation mechanism, baserunning weighting, defensive value, and FV grading. Addresses score compression, prospect evaluation accuracy, and FV grade inflation.

**Post-transform compensation (replaces pre-transform approach):**
- Compensation now operates on the penalty/deficit from 50 (average) after `_tool_transform`, not on the raw value before transform
- Smooth curve with no cliff at 40 — compensation applies to any tool below 50
- Formula: `effective = transformed + (50 - transformed) * pull_fraction`
- Pull fraction = `min(0.75, sum(surplus_i * strength_i))` where surplus = compensator - 50
- Hitter: power/eye compensated by contact (0.020/pt) and eye (0.012/pt for power)
- Pitcher: stuff compensated by movement/control; control by stuff/movement

**Contact-scaled baserunning weight:**
- High-contact players extract more WAR from speed (r=0.320 for Cnt≥60 vs r=0.145 for Cnt<50)
- Baserunning share increases by up to 100% of base when contact > 50
- Formula: `boost_factor = min(1.0, (contact - 50) / 30)`
- Taken from offense_share to keep total at 1.0

**Elite defense boost in composite:**
- When primary defensive rating > 50, defense share increases (up to 2× base)
- Formula mirrors baserunning boost: `def_addition = base_def_weight * min(1.0, (primary_def - 50) / 30)`
- Improves WAR correlation for C (+0.042), CF (+0.020), SS (+0.024)

**Above-60 tool transform bonus restored (1.2×):**
- Each point above 60 counts as 1.2 points in the weighted average
- Decompresses the top end of the composite scale
- Elite prospects with 70-80 tools properly separate from average players
- No effect on tools ≤ 60 (average and below unchanged)

**Stat blending improvements:**
- ERA- replaces FIP- for pitcher stat signal (OOTP WAR is RA9-based)
- League-calibrated P95 slopes for stat-to-2080 conversion
- Blend weights increased: 0.20/0.35/0.60 for 1/2/3 seasons
- Pitcher asymmetric blend removed (was over-protecting pitchers)
- `_refresh_stat_percentiles()` added to refresh.py

**FV model fixes:**
- Floor-based FV grading: `base = int(fv / 5) * 5` — must earn the grade (no rounding up from 52.65 to 55)
- Plus modifier at remainder ≥ 2.0
- Versatility bonus disabled (CF→corner OF is not real extra value; inflated 4th outfielders)
- Positional access premium redesigned: scales with BOTH defense level and offensive adequacy
  - Elite defenders (≥65): full premium, low offensive floor (38)
  - Average defenders (50-64): reduced premium, higher floor (46)
  - Only fires when offensive ceiling > current + 5 (prevents double-counting defense already in composite)
- Premium uses projected offense (blended with offensive_ceiling via dev_weight)
- `_offensive_ceiling` passed through fv_calc for premium calculation

**dev_weight granularity:**
- More granular for diff 1-3: diff=2 now gives 0.60 (was 0.50, same as diff=1)
- Properly rewards being 2 years young for level vs 1 year young
- Old players (diff ≤ -1) get lower weights (0.25/0.15 vs 0.35/0.20)

**FV bust discount recalibration:**
- Raised from 0.30-0.60 to 0.55-0.85 by age bracket
- Empirical validation: MLB players realize 92% of ceiling on average
- Old discount conflated bust probability with development rate
- Risk label now solely handles bust probability; FV reflects expected outcome

**FV ceiling cap:**
- FV cannot exceed true_ceiling - 3
- Players whose best-case is average (ceiling=50) correctly grade FV 45
- Eliminates inflation of low-ceiling organizational players into FV 50

**Results:**
- WAR correlation: 0.644 (VMLB hitters), 0.756 (eMLB hitters)
- RP rate quality: r(Composite, ERA) = -0.728 (VMLB), -0.530 (eMLB)
- FV 55 per org: 1.3 (VMLB), 5.9 (eMLB)
- FV 50 reduced from 24.8/org to 13.0/org (VMLB) via ceiling cap
- Validated on both VMLB and eMLB — model is league-agnostic

---

## Session 51 (2026-04-26)

### Per-League Development Curve Calibration

New `_calibrate_development_curves()` in `calibrate.py` derives gap closure rates, age runway tables, and expected gap tables from cross-sectional OVR/POT data per league. Stored in `model_weights.json`, loaded by `fv_model.py` via `_dev_curve()` with hardcoded VMLB-derived fallbacks.

EMLB vs VMLB show meaningfully different development profiles:
- Hitter closure at 22: EMLB 0.91 vs VMLB 0.67
- Pitcher closure at 22: EMLB 0.96 vs VMLB 0.79

All six tables now league-aware: `gap_closure_hitter/pitcher`, `age_runway_hitter/pitcher`, `expected_gap_hitter/pitcher`.

### Survivorship Bias Investigation

Cross-sectional OVR/POT data has significant survivorship bias from OOTP's POT revision mechanic: only 22% of POT 50+ players at age 17 retain POT 50+ at age 26. Busts have POT revised downward, appearing as "low-POT, high-realization" players in the snapshot.

Key findings:
- Ages 17-21: ~100% player coverage, no survivorship bias
- Ages 22-24: 95-99% coverage, minimal bias
- Ages 25-26: 72-88% coverage, moderate bias from missing unsigned players
- Unsigned players (level 0) already included in calibration queries
- The POT revision effect is the dominant bias source, not missing players
- FV grade formula is immune (uses current snapshot only)
- Gap closure rates (→ risk labels) are slightly optimistic; bust_discount mitigates
- Expected gap tables are correct for their cross-sectional use case
- True fix requires longitudinal tracking across multiple seasons

---

## Session 50 (2026-04-26)

### Three-Score Evaluation Model

Replaced the two-score model (composite/ceiling) with three scores:
- **Composite**: Current tools, honestly evaluated. No prospect discount, no character traits.
- **Projected** (ceiling_score): Age-blended likely outcome (~50th percentile). Peak tool bonus + age-weighted blend with current composite. Character traits removed (now in FV).
- **True Ceiling** (true_ceiling): Pure potential tools, no age blend, no peak bonus. Theoretical maximum if everything develops.

New `compute_true_ceiling()` in `evaluation_engine.py`. Column `true_ceiling` added to ratings table.

### FV Rearchitecture — Ceiling-Credit with Risk Labels

Replaced the `dev_weight` point-estimate system with a ceiling-credit model that separates FV grade (what could this player be?) from risk (how likely is that?). Evolved through several iterations during the session: gap-closure expected value → MLB-anchored offset → dynamic positional median → ceiling-credit with realization scaling.

**Final FV formula:** `FV = 45 + (true_ceiling - positional_MLB_median) × ceiling_credit`
- `ceiling_credit = 0.20 + 0.55 × (composite / ceiling)` — players closer to their ceiling get more credit
- Maxed-out players (gap < 3): `FV = 45 + (ceiling - median)` — no credit scaling needed
- Ceiling quality gate: ceiling must be 6+ above positional median for FV 45+ (uses raw ceiling before RP discount)
- Dynamic positional MLB median computed per league from current MLB composite distribution
- Character traits adjust development confidence (work ethic, intelligence)
- Accuracy L: -2 FV. Platoon splits: -2/-3 FV. RP cap: FV ≤ 55.

**Risk labels** (Low/Medium/High/Extreme) derived from development confidence:
- `dev_confidence = closure_rate × age_discount × gap_scale + character_adj`
- Closure rates: empirical forward-looking by age and player type (hitter/pitcher)
- Age discount: 0.30 (17-19) to 0.60 (24-25)
- Gap scale: penalizes excess gap above age-expected norm (empirical mean POT-OVR by age)
- Stored in `prospect_fv.risk` column, displayed on player page with color coding

**Dropped "+" grades** — clean FV tiers (40/45/50/55/60/65/70). Risk label provides the granularity that "+" was capturing.

### Prospect Discount Removed

The flat -5/-3 prospect composite discount was removed. It double-counted with the development model, suppressed honest tool evaluations, and dragged ceilings down. The composite now reflects pure tool value; the FV grade (via ceiling-credit and MLB anchoring) handles the translation to prospect value.

### FV Distribution (Final)

| Metric | EMLB | VMLB | Fangraphs ref |
|--------|------|------|---------------|
| FV 55+/org | 0.6 | 0.2 | ~1.0 |
| FV 50+/org | 7.1 | 2.9 | ~3.3 |
| FV 45+/org | 10.7 | 10.3 | ~8.3 |

VMLB matches Fangraphs closely at all tiers. EMLB FV 50+ runs higher (league composition — more high-ceiling prospects).

### Key Player Outcomes (Final)
- **Schwarzenberg** (24yo AAA SP, POT 65): Comp 45 / Ceil 59 / FV 50 Medium
- **Chad Marshall** (20yo A COF, POT 64): Comp 35 / Ceil 61 / FV 50 Medium
- **Eric Kiefer** (22yo A COF, POT 61): Comp 39 / Ceil 67 / FV 55 High
- **Mike French** (22yo AAA SS, POT 42): Comp 48 / Ceil 48 / FV 45 Low (maxed)
- **Hoadley** (24yo AA 2B, POT 42): Comp 47 / Ceil 51 / FV 40 High
- **J.C. Swishman** (23yo AAA RP, POT 76): Comp 50 / Ceil 65 / FV 50 Medium

### SP Ceiling Compression — Accepted Limitation

Pitcher true_ceiling runs ~5-6 below game POT. Root cause: stuff rating already incorporates individual pitch quality, so the arsenal bonus partially double-counts. The gap is stable and predictable. The FV system compensates through ceiling-credit. Increasing arsenal weight would risk further double-counting.

### Bug Fixes

- **Ratings scale not updating on league switch** (carried from S49 docs): Fixed `app.py` to set `ratings._ratings_scale` directly.
- **MLB context qualification thresholds**: Now scale by season progress (June: ~30 IP SP threshold vs 80 IP full-season).

### Key Player Outcomes
- **Schwarzenberg** (24yo AAA SP, POT 65): Comp 45 / Proj 53 / Ceil 59 / FV 45. Limited by SP ceiling compression (true_ceiling 59 vs POT 65).
- **Hoadley** (24yo AA 2B, POT 42): Comp 47 / Ceil 51 / FV 40. Ceiling at positional median = FV 40. Correct.

Files changed: `scripts/fv_model.py`, `scripts/fv_calc.py`, `scripts/evaluation_engine.py`, `web/app.py`, `web/player_queries.py`

---

## Session 49 (2026-04-25)

### Player Page — Evaluation Panel

Replaced the scattered evaluation display (Tool Profile panel, Development Tracking panel, header component scores) with a unified **Player Evaluation** panel.

- **Header simplified:** Added Comp/Ceil scores, kept OVR/POT as game reference. Removed inline component scores, carrying tool bonus, divergence badges. Position label shows `(eval: XX)` when evaluation bucket differs from listed position.
- **Evaluation panel:** Two-box layout (Now/Ceiling) with MLB percentile and tier label. Component bars with current/potential overlay. Carrying/red-flag tools. "vs. Game Rating" divergence section. Development tracking deltas.
- **`_mlb_context()` query** in `player_queries.py` — computes positional percentile and tier for composite/ceiling vs MLB population at that position.

### FV Pipeline Migration to Composite/Ceiling

Migrated legacy FV components from OVR/POT assumptions to composite/ceiling:

- **`dev_weight()` curve:** diff≥2 now gets 0.60 (was 0.50). Fixes undervaluation of young-for-level high-ceiling prospects (Joe Read: 20yo A-ball SS, FV 50+ → 55+).
- **`age_development_mult()`:** New empirical age decay function derived from cross-sectional OVR/POT gap analysis (N=50+ per age bucket). At age 22, 69% of development runway remains; at 24, 28%; at 26, 12%. Replaces arbitrary hard cutoffs.
- **Removed redundant low-upside discount:** The extra +1/+3 composite penalty for age 23+ prospects with small ceiling gaps is now handled by `age_development_mult()`.
- **`effective_pot()` removed:** Dead code — column name mismatch meant it never fired.
- **`versatility_bonus()` removed:** No longer called by `calc_fv`.
- **`RP_POT_DISCOUNT`:** 0.80 → 0.85. Old value double-counted RP devaluation already in pitcher composite weights.

### Pitcher Composite — Extended Ratings

- **HRA and PBABIP** added as optional weighted tools in pitcher composite. Calibration produces weights when data exists (VMLB: hra=0.024, pbabip=0.020). Leagues without extended ratings (EMLB) degrade gracefully.
- HRA correlates with SP WAR at r=0.449 (partial r=0.265 controlling for composite), adding 0.040 to R².
- **COMPOSITE_TO_WAR tables** regenerated via calibration on both leagues. Surplus calculations now properly reflect positional value differences.

### Benchmark (Session 49 Final vs Session 48 Baseline)

| Metric | Baseline | Final | Target |
|--------|----------|-------|--------|
| VMLB All Prospect Comp-OVR | +0.7 | +1.2 | ±2.0 ✅ |
| VMLB FV40+ Comp-OVR | +2.6 | +3.5 | ±3.0 ⚠️ |
| VMLB Ceiling collapse | -0.2 | -0.2 | > -3.0 ✅ |
| VMLB Crushed >10pts | 10% | 10% | < 15% ✅ |
| EMLB All Prospect Comp-OVR | +0.1 | +0.7 | ±2.0 ✅ |
| EMLB FV40+ Comp-OVR | +2.3 | +3.2 | ±3.0 ⚠️ |
| Weight cosine similarity | 0.98+ | 0.98+ | > 0.85 ✅ |

FV40+ slight overshoot is a deliberate tradeoff from removing the ad-hoc low-upside discount in favor of the empirical age decay model.

### Stamina Calibration

Reduced SP stamina overweighting. Volume bonus: 0.20/pt cap 7 → 0.12/pt cap 4 (calibrated from quartile WAR analysis). Peak bonus stamina contribution capped at +5. Stamina correlates with WAR at only r=0.168.

### Player Evaluation UI — Iteration

- **Pitcher percentiles fixed**: SP/RP now matched by role directly instead of `assign_bucket` (which failed on minimal player dicts).
- **Divergence labels**: Pitchers show "pitching-driven" instead of "offense-driven". Added "durability-driven".
- **MLB context: qualified regulars only**: Filters to 200+ PA (hitters), 80+ IP (SP), 30+ IP (RP). Excludes bench/callup players.
- **Replaced percentiles with rank + distance from median**: Percentiles on small integer distributions were unreliable (1-point = 18 pctile swing). New display: "#27/45 MLB 2B, -1 vs median (51) · Average".
- **Ceiling context**: Shows distance + tier only (no rank). Rank implies prediction; distance communicates possibility.
- **Layout by player type**: Prospects get evaluation panel top-right (most prominent). MLB players keep it left column under ratings, freeing right column for stats/percentiles.

### Component-Aware Career Outcomes

Career outcome probabilities and surplus now adjust based on player profile shape:
- Premium defense (SS/C/CF, def ≥ 60): higher development probability. Glove-first SS: 65% vs 59% Contributor at same FV.
- Low-durability SP (dur < 45): higher bust risk. Low-stm SP: 45% vs 57% Contributor.
- Offensive ceiling ≥ 60: shifts toward ceiling scenario.
- Balanced profiles: tighter distributions. Extreme profiles: wider distributions.

New `_adjust_scenario_probs()` in `prospect_value.py`. Component scores threaded from `fv_calc.py` through `prospect_surplus_with_option()` and `career_outcome_probs()`.

### Bug Fixes

- **Ratings scale not updating on league switch**: `app.py` was setting `player_utils._ratings_scale` (no effect) instead of `ratings._ratings_scale`. Switching from VMLB (20-80) to EMLB (1-100) displayed inflated potential grades (e.g. 75 instead of 65 for raw 73).
- **MLB context qualification thresholds**: Hardcoded full-season thresholds (200 PA, 80 IP SP, 30 IP RP) produced tiny comparison pools early in the season (14 SP by June 10). Now scales by season progress with minimum floors.

### Hitter/Pitcher Age Decay Split

Cross-sectional OVR/POT gap analysis across both leagues shows pitchers develop later:
- Age 24: hitter runway 0.43, pitcher runway 0.48 (was 0.28 combined)
- Age 23: hitter 0.55, pitcher 0.62 (was 0.40 combined)

New `_AGE_RUNWAY_HITTER` and `_AGE_RUNWAY_PITCHER` tables in `fv_model.py`. Both curves are also less aggressive than the old combined table at ages 22-26. `dev_weight()` and `age_development_mult()` now accept `is_pitcher` parameter.

Files changed: `scripts/fv_model.py`, `scripts/evaluation_engine.py`, `scripts/calibrate.py`, `scripts/constants.py`, `scripts/player_utils.py`, `scripts/farm_analysis.py`, `scripts/prospect_value.py`, `scripts/fv_calc.py`, `web/player_queries.py`, `web/templates/player.html`

---
## Session 48 (2026-04-25)

### Evaluation Engine — Cross-League Calibration & Model Independence

First calibration run on both VMLB (20-80 scale) and EMLB (1-100 scale). Identified and fixed systematic prospect inflation, ceiling collapse, and calibration weight instability. Removed all POT/OVR dependencies from the evaluation engine — scores are now derived purely from individual tool ratings.

**New: `scripts/benchmark.py`** — Evaluation engine performance benchmark. Measures composite vs WAR correlation, prospect inflation, ceiling collapse, and cross-league weight stability. Supports `--all` (all leagues) and `--json` output. Used for before/after comparison when tuning model parameters.

**Calibration weight regularization (calibrate.py):**
- Raised `min_weight` floor: hitter hitting 0.10→0.18, pitcher 0.05→0.15. Prevents single-tool dominance (max single weight dropped from 67% to 31%).
- R²-proportional default blending: `final = R² × calibrated + (1-R²) × default`. Low-R² buckets (most hitter positions) now stay close to balanced defaults. High-R² buckets (EMLB C at 0.61) get more calibration influence.
- Cross-league weight stability improved dramatically: minimum cosine similarity 0.65→0.98.

**Prospect composite discount (evaluation_engine.py):**
- Age-based discount for non-MLB players: 0 (age ≤17), 3 (18-19), 5 (20+) points.
- Extra +3 discount for age 23+ prospects with low upside (ceiling within 5 points of composite).
- Addresses the structural gap between OVR (which incorporates "proven-ness") and the tool-weighted composite (which doesn't). Prospect Comp-OVR offset reduced from +3.4 to +0.7 (VMLB) and +3.7 to +0.1 (EMLB).

**Peak tool bonus for ceiling (evaluation_engine.py):**
- Adds +1 point per potential tool point above 60, capped at +15 (20-80 scale) or +10 (1-100 scale).
- SP stamina included as a 4th tool when stamina ≥ 55, giving SP parity with hitters' 4 offensive tools.
- Addresses ceiling collapse for prospects with uneven tool profiles. Purely tool-derived.
- VMLB ceiling collapse: -8.7→-0.2. Crushed >10pts: 38%→10%.

**Removed POT dependency from ceiling formula:**
- Removed the POT+8 soft cap from `compute_ceiling()` (was added Session 46).
- Removed `pot` parameter from `compute_ceiling()` signature and all call sites.
- The ceiling is now fully independent of the game's POT rating.

**Positional reclassification (player_utils.py):**
- Borderline SS (PotSS ≤ 55) with significantly better 3B/2B defense (≥10 point gap) are reclassified to the alternative position.
- Borderline CF (PotCF ≤ 55) with significantly better corner OF defense (≥10 point gap) are reclassified to COF.
- Reflects real scouting practice: a college SS who projects as a 3B gets evaluated as a 3B prospect.
- SP representation in top 100 improved: 18→22 (VMLB), 8→14 (EMLB) via stamina bonus.

**COMPOSITE_TO_WAR calibration:**
- Second calibration pass now produces COMPOSITE_TO_WAR tables on both leagues.
- EMLB shows strong R² at several positions (C=0.83, 3B=0.72, CF=0.65), confirming the composite is a useful WAR predictor when fed through position-specific regression tables.

**Benchmark results (final, POT-free):**

| Metric | Before | After | Target |
|---|---|---|---|
| VMLB Prospect Comp-OVR | +3.4 | +0.7 | ±2.0 ✅ |
| VMLB FV40 Comp-OVR | +8.0 | +2.7 | ±3.0 ✅ |
| VMLB Ceiling collapse | -8.7 | -0.2 | > -3.0 ✅ |
| VMLB Crushed >10pts | 38% | 10% | < 15% ✅ |
| EMLB Prospect Comp-OVR | +3.7 | +0.1 | ±2.0 ✅ |
| EMLB FV40 Comp-OVR | +7.5 | +2.3 | ±3.0 ✅ |
| EMLB Ceiling collapse | -3.9 | +2.7 | > -3.0 ✅ |
| Weight cosine similarity | 0.65 | 0.98 | > 0.85 ✅ |

Files changed: `scripts/calibrate.py`, `scripts/evaluation_engine.py`, `scripts/player_utils.py`, `scripts/fv_model.py`, `scripts/benchmark.py` (new), `web/player_queries.py`, `web/templates/player.html`

---

### Player Page — Evaluation Panel Redesign

Replaced the scattered evaluation data (header component scores, Tool Profile panel, Development Tracking panel) with a unified **Player Evaluation** panel and a cleaner header.

**Header simplified:**
- Kept: Team/Level, Name, Position, Age, Ht/B-T, Comp/Ceil (new), OVR/POT, FV, Surplus, PAP, Performance
- Removed: Component score grade bars, carrying tool bonus, positional percentile, divergence badges — all moved to evaluation panel

**New Player Evaluation panel:**
- Two-box layout: **Now** (composite + MLB percentile + tier) and **Ceiling** (ceiling + MLB percentile + tier)
- Percentiles computed against all MLB players at the same position bucket, giving instant context ("51 composite = 45th pctile MLB 2B = Average")
- Tier labels: Fringe / Below Avg / Average / Plus / Elite
- Component bars (Offense/Baserunning/Defense or Pitching/Durability) with color-coded fill
- Carrying tools and red flag tools on one line
- "vs. Game Rating" section showing Comp vs OVR and Ceil vs POT divergence with Hidden Gem / Landmine badges
- Development tracking (composite/ceiling deltas + Riser/Reduced Ceiling badges)
- Works for both prospects and MLB players

**New query: `_mlb_context()`** in `player_queries.py` — computes positional percentile and tier label for any composite/ceiling score against the MLB population at that position.

---

### FV Formula Simplification

Revamped `calc_fv()` to trust the evaluation engine's composite and ceiling scores, removing adjustments that were double-counting what the composite already captures.

**Removed from FV formula:**
- Defensive bonus (+1 to +3 for good defense) — already in composite via recombination weights
- Versatility bonus (+1 to +2 for multi-position) — was inflating borderline prospects by a full FV tier through rounding (e.g., Tim Klann: 55→50 after fix)
- Positional access premium (SS/C/CF bonus) — already in composite via position-specific tool weights
- Critical tool floor penalty — already in composite via piecewise tool transform
- RP hard cap at FV 50 — replaced with softer cap at FV 55 (allows elite RP to reach 55, was too restrictive at 50)

**Retained in FV formula:**
- `dev_weight` blend of composite and ceiling (core FV logic)
- RP ceiling discount (0.8×) — RP innings volume justifies lower ceiling
- Work ethic modifier (+1/-1)
- Accuracy penalty (-2 for Acc=L)
- Platoon split penalty (-2/-3) — not captured by composite

**Result:** FV formula went from ~50 lines with 6 adjustment mechanisms to ~25 lines with 3. Top 25 prospect list nearly identical. Tim Klann (49th percentile 2B) correctly grades as FV 50 instead of inflated FV 55.

---

## Session 47 (2026-04-19)

### Evaluation Engine — Calibration & Composite Overhaul

Major rework of the hitter and pitcher composite pipelines. Composite now beats OVR as a WAR predictor overall (r=0.679 vs 0.646, +0.034). Prospect inflation dramatically reduced. MiLB offset near zero.

**Dropped `avoid_k` from hitting regression:**
- Contact is a composite of BABIP + K-avoidance in the OOTP engine — including both Contact and Avoid_K double-counts the K-avoidance signal (r=0.78 collinearity)
- Removed from regression features, default weights, tool extraction, confidence checks, and tests

**Switched hitting regression target from OPS+ to WAR:**
- WAR regression produces weights that better predict actual value
- Fixes 2B bucket where OPS+ regression overweighted power and underweighted eye
- Overall neutral-to-positive impact across all positions

**Removed speed from hitting regression:**
- Speed contributes to WAR through baserunning, not hitting
- Including it in the hitting regression double-counted its value since it also appears in the baserunning regression
- Speed still flows through the baserunning component via recombination

**Enabled min_weight floor for hitter calibration:**
- Hitter hitting regression: `min_weight=0.10` (prevents degenerate single-variable solutions)
- Pitcher regression: `min_weight=0.05` (already existed)

**Non-linear piecewise tool transformation (`_tool_transform`):**
- Below 40: each point penalized 1.5× (a 30 contact is effectively 25)
- 40-60: linear (1:1) — preserves MLB sensitivity
- Above 60: each point rewarded 1.3× (a 70 power is effectively 73)
- Replaces the old elite tool bonus (+0.5 per point above 60)
- Applied to hitting tools (contact, gap, power, eye) and pitcher tools (stuff, movement, control)

**Pitcher-specific stat-to-2080 conversion (`pitcher_stat_to_2080`):**
- Asymmetric: steeper slope (0.45/pt) for above-average FIP, standard slope (0.30/pt) for below-average
- Prevents stat blend from over-penalizing average SP while rewarding elite pitching

**Increased SP innings-volume adjustment:**
- Changed from +1/10pts above 50 (cap +3) to +1/5pts above 45 (cap +7)
- Addresses the rate-stat vs counting-stat gap (Comp×IP correlates with WAR at r=0.70)

**WAR-derived recombination shares:**
- Defense shares dramatically reduced based on WAR regression data:
  - C: 35%→15%, SS: 35%→5%, 2B: 25%→5%, 3B: 25%→0%, CF: 35%→10%, COF: 20%→0%, 1B: 15%→0%
- Offense dominates WAR at every position; WAR already includes positional adjustment

**API changes:**
- `_tool_transform(val, midpoint, steepness)` — new function (piecewise non-linear transform)
- `pitcher_stat_to_2080(stat_plus)` — new function (asymmetric pitcher stat conversion)
- `compute_composite_hitter` — removed `bat_floor_threshold` parameter
- `BAT_FLOOR_THRESHOLDS` — retained as reference data, no longer used in composite formula
- Elite tool bonus removed from both hitter and pitcher composites (replaced by `_tool_transform`)

**Results:**
- EMLB: Composite beats OVR overall (r=0.679 vs 0.646, +0.034)
- Prospect inflation dramatically reduced (Pauldo from +14 to -3 gap vs OVR)
- MiLB offset near zero (EMLB: -0.9, VMLB: +3.7)
- MLB distribution aligned (EMLB: +1.2, VMLB: +0.8)

Files changed: `scripts/evaluation_engine.py`, `scripts/calibrate.py`, `tests/test_evaluation_engine.py`

---

## Session 46 (2026-04-19)

### Custom Player Evaluation — Post-Implementation Tuning

Ran the evaluation engine against the VMLB league (15,012 players, 20-80 scale) and identified three issues. All three fixed and documented in the design spec.

**Two-way player detection overhaul:**
- Original tool-based threshold (contact ≥ 35, power ≥ 30) flagged 9,649 of 15,012 players (64%) as two-way
- Root cause: on 20-80 scale leagues, every player has all tools populated at 20+; no `is_pitcher` precondition meant hitters with default pitcher ratings qualified
- Fix: tiered detection — (1) stat-based ground truth from `war_model.load_stat_history()`, (2) stat-based from season lists, (3) tool-based for prospects requiring `is_pitcher=True` + contact ≥ 45, power ≥ 40
- Result: 56 two-way players (realistic)

**Elite tool bonus for score compression:**
- Composite scores were compressed to ~30-62 while OVR ranged 20-80
- Root cause: weighted average of tools can't reach 80 unless every tool is 80; elite players have a mix of 55-70 tools
- Fix: +0.5 per tool point above 60 (weighted by tool importance) in both `compute_composite_hitter()` and `compute_composite_pitcher()`
- Result: top-end expanded by ~3-5 points for elite players

**Systematic offset documented (not a bug):**
- Composite averages +13 points above OVR for minor leaguers, but only +0.6 at MLB level
- OVR factors in development/experience; composite measures raw tool quality
- Documented as expected behavior in design spec with future tuning options

Files changed: `scripts/evaluation_engine.py`, `tests/test_evaluation_engine.py`, `.kiro/specs/custom-player-evaluation/design.md`

---

## Session 45 (2026-04-06)

### Trade Analyst Agent — Refinements
- `trade-analyst.json` Kiro agent added to `~/.kiro/agents/`
- `standings.py`: added `actual_record(team_id, year)` function and `--actual` CLI flag — shows actual W-L from `games` table alongside pythagorean, computes delta with luck/regression interpretation
- Steering file updated: Phase 1 now runs `standings.py --actual` instead of asking user for W-L; ARB vs FA distinction clarified throughout; `trade_targets.py` vs `free_agents.py` scope clarified; `--aaa-roster` added to Phase 1 farm step

### `free_agents.py` — ARB/FA Detection
- Arb-eligible players (1yr contract, service time < 6 years) now labeled **ARB** instead of appearing as walk-year FAs
- True pending FAs labeled **FA**, team options labeled **TO**
- Output sorted: FAs first, then ARB players
- Fixes false positives where arb-eligible players were listed as "key walk-year players"

### `team_needs.py` — Platoon Detection + AAA Roster
- Added platoon split detection: flags players with large split gap (20+ combined contact/power/eye) AND genuine weakness on weaker side (contact or power < 45). Uses eye instead of gap as third tool.
- Added `--aaa-roster` flag: prints full needs report + full AAA roster sorted by Ovr, including veterans below FV threshold that `prospect_query.py` misses
- `aaa_roster()` uses `level_map` to find AAA level (not hardcoded `'2'`)
- `--aaa-roster` now prints both the main report and AAA roster (previously either/or)

### Player Page — Multi-Stint Stats (Traded Players)
- Batting and pitching stats now aggregate multi-team stints into one combined row per year
- Per-team breakdown shown indented below the totals row in the Stats tab
- Totals row shown in bold with "(N teams)" label
- Percentile rankings now use aggregated stats across all stints (previously used only the most recent team's stint)
- Popup stats also aggregated via SQL `SUM`

### Contracts Page — Rule 5 / Off-Roster Players
- Players whose current org differs from `contract_team_id` (Rule 5 draft, etc.) are now excluded from all contract/payroll queries
- Consolidated into `_CONTRACT_ORG_SQL` constant + `_contract_org_params()` helper in `team_queries.py` — single definition used by all 4 contract queries

### Bug Fixes
- `calibrate.py`: SQL injection bug — `{DEFAULT_MINIMUM_SALARY}` literal in query replaced with `?` parameter binding
- `calibrate._bucket_player` + `fv_calc`: crash on empty string ratings from API (player ID 23 has `''` for all defensive grades) — `assign_bucket.pgrade()` now coerces non-numeric to 0; `fv_calc` skips players with non-numeric `Ovr`
- `fv_calc`: `int(level)` crash on string level values — changed to `str(level) in ("7", "8")`
- Multi-stint aggregation: `_bat_row`/`_pit_row` now store raw counting stats (`_d`, `_t`, `_hbp`, `_sf`, `_er`, `_hra`, `_bf`, etc.) needed for correct rate stat recomputation across stints

### Tests
- `tests/test_scripts.py` added: `assign_bucket` edge cases (empty string, None, string-numeric grades), `_bucket_player` with malformed DB rows, `fv_calc` skip logic for non-numeric Ovr
- Snapshot test values updated after partial refresh ($/WAR shifted from $8.9M → $8.78M due to empty ratings export)

---

## Session 44 (2026-04-05)

### Trade Analyst Agent
- Created `.kiro/steering/trade-analyst.md` — full agent definition for trade analysis:
  - Multi-league aware (derives playoff spots from division config, scales GB thresholds)
  - Two-phase session init: auto-pull standings/needs/farm/expiring contracts, then ask user for payroll/untouchables/recent transactions
  - Explicit "skip Phase 1 if user already provided context" instruction
  - Other team's needs workflow for package construction
  - Contract status classification table (PRE-ARB / ARB / RENTAL / RENTAL+EXT / OPTION / CONTROLLED)
  - Known data limitations prominently flagged, especially transaction log gap

### New CLI Tools
- `scripts/trade_targets.py` — find trade targets by position with full contract classification:
  - RENTAL / ARB-ELIGIBLE / RENTAL+EXT / OPTION / CONTROLLED status detection
  - Arb detection via `arb_model.estimate_service_time` (service time < 6 years = ARB, not rental)
  - Signed extension detection from `contract_extensions` table (RENTAL+EXT)
  - Seller classification derived from league config divisions (~40% playoff rate per sub-league)
  - `--vs-hand R|L` flag: shows split ratings (and stats if available), sorts by split power
  - `--max-salary` filters on pro-rated cost derived from game date
  - Pro-rated salary calculated from actual game date vs season start/end
- `scripts/trade_assets.py` — tradeable assets for any team:
  - MLB surplus players ranked by value with contract status and stats
  - Farm prospects ranked by surplus with FV, level, Ovr/Pot
  - `--team <abbr>` works for any team in the league
- `scripts/team_needs.py` — positional needs vs league average:
  - Per-position OPS vs league avg flagged SEVERE/WEAK/OK/STRONG
  - Rotation and bullpen ERA vs league avg
  - Ranked upgrade priority list
  - Works for any team via `--team` flag

### Improved CLI Tools
- `scripts/trade_calculator.py` — team-agnostic rewrite:
  - Hardcoded "Angels" references replaced with team name from config
  - `--offer`/`--receive` flags accept player names or IDs (name lookup with ambiguity detection)
  - Legacy `angels_send`/`angels_receive` JSON keys still accepted
- `scripts/free_agents.py` — fixed minimum salary filter using `config.minimum_salary` instead of hardcoded `DEFAULT_MINIMUM_SALARY` constant (was returning empty results in vMLB)

### Tests
- Updated `test_prospect_value.py` expected values (league_averages.json drift from refresh)

---

## Session 43 (2026-04-03 – 2026-04-04)

### Draft Tab — Bug Fixes
- Fixed stale API picks showing in pre-draft/uploaded states — `get_draft_pool()` now returns `picks: []` for `uploaded` and `pre_draft` states; only `active` state uses live API picks. Eliminates prior-year drafted players appearing as drafted in the new pool.
- Fixed My Picks panel growing unbounded — capped at 156px (`~6 rows`) with `overflow-y: auto`.
- Fixed draft prospect position filter using listed position instead of projected bucket — filter now uses `p.bucket` so a player listed as SS but projected as 2B appears under the 2B filter.

### Draft Tab — New Features
- **Position needs panel** — replaced `All / Hitters / Pitchers` filter bar with a combined needs + filter bar. Each position (C, 1B, 2B, 3B, SS, LF/RF, CF, SP, RP) shows a color-coded signal: green (≥$30M combined surplus), orange (≥$10M), red (<$10M). Clicking filters the board. Tooltip shows MLB/farm surplus breakdown. Backed by new `get_draft_org_depth(team_id)` query in `team_queries.py`.
- **B/T split into separate columns** — `Bat` and `Thr` are now individually sortable in All and Hitter views. Pitcher view shows `Thr` only.
- **Surplus-based board ranking** — board now sorts by `prospect_surplus_with_option` (descending) with FV as tiebreaker. RPs naturally fall down the board due to WAR ceiling. `$Val` column added to all views. Surplus computed in `_build_prospect` via `prospect_surplus_with_option`.
- **All 12 pitch types in pitcher view** — added FRK, CC, SCR, KC, KN columns to pitcher board thead and row rendering (was only 7).
- **Prospect comparison bar chart** — replaced side-by-side table compare with mirrored bar chart. Left player's bars grow right-to-left, right player's grow left-to-right, meeting at a center label column. Winner's bar is full opacity, loser's dimmed to 25%. Delta badge (`+N`) shown at 25% from center on winning bar. Player names as column headers with links.
- **Single player detail panel rewrite** — draft detail panel now calls `/api/prospect/{pid}` and reuses `renderPanel` (same as prospects tab). Falls back to `/api/draft-detail/{pid}` for amateur players not in `prospect_fv`, adapting the response shape to match `renderPanel`. `renderPanel` refactored to return HTML string instead of writing to DOM directly.
- **Draft tab layout reshuffle** — changed from stacked (detail panel on top, board below) to side-by-side (board left, 420px sidebar right with detail panel + My Picks stacked). Sidebar is `position: sticky`. Board and sidebar each scroll independently.
- **Sleeper/value flags — shelved** — investigated and documented. Requires independent ratings model to be meaningful; FV is too correlated with Pot to produce actionable signals. See task list for full rationale.

### Bug Fixes
- Fixed `_league_pos_rankings` accidentally deleted from `team_queries.py` — was overwritten when `get_draft_org_depth` was inserted. Restored from git. Caused `NameError` on all team page loads.
- Fixed `assign_bucket` in `player_utils.py` — `p.get("PotKnbl", 0)` returns `None` (not `0`) when column is present but NULL. Changed to `(p.get("PotKnbl") or 0)` pattern throughout. Caused `TypeError` in `contract_value` for pitchers with NULL pitch ratings.
- Fixed `get_depth_chart` — `val` from `row[field]` could be `None` when ratings column is NULL, causing `TypeError` on team page depth chart tab.
- Fixed defense ratings not showing on draft prospect player pages — `player_queries.py` defense list filtered `if c and c >= 20` (current rating), excluding prospects with low current but meaningful potential ratings. Changed to `if (c and c >= 20) or (f and f >= 20)`.

### Test Suite Expansion
- Added 8 new tests to `test_team_queries.py`: `get_depth_chart` (3 tests), `get_draft_org_depth` (3 tests), `get_payroll_summary` (2 tests). Total: 42 tests in `test_team_queries.py`.
- Updated `test_prospect_value.py` — 8 hardcoded expected values updated to match current model output (pre-existing drift from Session 41 constants refactor, not caught until now).

---

## Session 42 (2026-04-03)

### Web Layer Integration Tests
- Added `tests/conftest.py` — shared fixture infrastructure for web query tests:
  - `_ConnProxy` — wraps the shared in-memory connection, silently drops `row_factory` writes and no-ops `close()` so query functions can't corrupt shared test state.
  - `db_conn` (session-scoped) — in-memory SQLite seeded with 3 players (hitter, pitcher, prospect), contracts, batting/pitching/fielding stats, games, `player_surplus`, and `prospect_fv` rows.
  - `mock_cfg` (session-scoped) — minimal `LeagueConfig`-like mock with all required properties.
  - `patch_web_context` (autouse) — patches `get_db`, `get_cfg`, `_get_state`, and all module-level accessor aliases in `team_queries`, `player_queries`, `queries`, and `percentiles`.
- Added `tests/test_queries.py` — 15 tests covering `get_top_prospects`, `get_all_prospects`, `get_batting_leaders`, `get_pitching_leaders`, `search_players`.
- Added `tests/test_team_queries.py` — 34 tests covering all team-level query functions: `get_summary`, `get_standings`, `get_roster`, `get_roster_hitters`, `get_roster_pitchers`, `get_farm`, `get_contracts`, `get_upcoming_fa`, `get_surplus_leaders`, `get_roster_summary`, `get_recent_games`, `get_stat_leaders`, `get_farm_depth`, `get_age_distribution`, `get_record_breakdown`, `get_power_rankings`.
- Added `tests/test_player_queries.py` — 16 tests covering `get_player` for hitter, pitcher, prospect, and missing player cases.
- 65 new tests, all passing. Full suite (106 tests) green.

---

## Session 41 (2026-04-03)

### Fresh Install Testing & Bug Fixes
- Fixed `data/` directory not created on first run — `onboard_step1` now calls `APP_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)` before writing.
- Fixed closed DB connection in `onboard_step3` — `has_100` ratings scale detection query moved before `conn.close()`.
- Fixed pitch potential ratings not showing in player hover panel — `player_queries.py` `get_player_popup` was missing `pot_fst`, `pot_snk`, etc. from its SELECT; added all 12 pitch potential columns.
- Fixed pitch potential ratings not showing in league prospect side panel — `league.html` was using `p.fut` but `queries.py` builds pitches with `"pot"` key; updated template to use `p.pot`.
- Fixed `norm(0)` returning `None` causing `TypeError` in `_build_prospect` — introduced `norm_floor()` for call sites requiring a numeric result; replaced all `norm(x or 0)` patterns.
- Fixed `DEFAULT_DOLLARS_PER_WAR` `NameError` in `player_queries.py` hover popup — stale inline `league_averages` imports cleaned up; all dpw lookups now use `_dollars_per_war()`.
- Fixed `_estimate_control` `ImportError` in `team_queries.py` — three inline imports updated to use `arb_model.estimate_control` after Phase C extraction.
- Fixed circular import `player_utils` ↔ `fv_model` — extracted `norm`, `norm_floor`, `get_ratings_scale` into `scripts/ratings.py`.
- Fixed `_n80` `NameError` in `queries.py` `get_draft_pool` — two remaining `n = _n80` references updated to `n = _norm`.
- Fixed duplicate `from contract_value import contract_value` in `fv_calc.py`.
- Fixed `emlb.web`, `emlb.onboard`, `emlb.client` hardcoded logger names — updated to `statspp.*` via `get_logger()`.
- Fixed `"emlb"` hardcoded slug fallbacks in `app.py` and `queries.py` — changed to `""`.
- Removed unused `math` import from `contract_value.py`.

### README & Setup Improvements
- Fixed `pip` not available on Ubuntu 24.04 — README now uses `python3 -m venv .venv` + `.venv/bin/pip install`.
- Fixed PEP 668 externally-managed environment error — README documents venv requirement with Ubuntu callout.
- Updated launch command to `.venv/bin/python3 web/app.py` (no activation needed).
- Added `.venv/` to `.gitignore`.
- Added `pytest>=8.0` to `requirements.txt`.

### Web Logging
- Added `get_logger("web")` to `web/app.py` — all unhandled exceptions now logged to `data/logs/web.log` with full traceback.
- `_teardown_request` logs teardown exceptions.
- `_handle_exception` error handler logs and re-raises (Flask debugger still works in dev mode).

### Settings: Auto-recalculate on Ratings Scale Change
- Changing `ratings_scale` in Settings now triggers `fv_calc.run()` in a background thread automatically.

### Code Quality Refactor (Phases A–E)

**Phase A — Constants consolidation**
- `constants.py` rewritten with 6 clearly labeled sections.
- All magic numbers named: `ROLE_MAP`, `DEFAULT_DOLLARS_PER_WAR`, `DEFAULT_MINIMUM_SALARY`, `PEAK_AGE_PITCHER/HITTER`, `SERVICE_GAMES_*`, `ARB_*` coefficients, `NO_TRACK_RECORD_DISCOUNT`, `RP_POT_DISCOUNT`, `LEVEL_AGE_DISCOUNT_RATE`, `PROSPECT_WAR_RAMP`, `MIN_REGRESSION_N`, `CALIBRATION_YEARS`.
- All 8 consuming files updated to import named constants.

**Phase B — Single source of truth**
- `_n80()` removed from `queries.py`; replaced with `_norm` from `player_utils`.
- `norm_floor()` added to `ratings.py` — explicit named function for "normalize with numeric floor".
- `DEFAULT_MINIMUM_SALARY` replaces all `825000` literals (5 files).
- `dollars_per_war()` fallback unified to `DEFAULT_DOLLARS_PER_WAR` (was `9_500_000` in function, `8_976_775` elsewhere). Default updated to `9_000_000` (round number).
- Web layer `player_queries.py` and `team_queries.py` now call `dollars_per_war()` instead of duplicating `_load_la().get(...)`.
- Local `ROLE_MAP` definitions removed from `queries.py`, `player_queries.py`, `team_queries.py` — all import from `constants.py`.

**Phase C — File restructuring**
- `scripts/ratings.py` — new: `norm`, `norm_floor`, `get_ratings_scale`, `init_ratings_scale`.
- `scripts/fv_model.py` — new: `calc_fv`, `dev_weight`, `effective_pot`, `versatility_bonus`, `defensive_score`, `DEFENSIVE_WEIGHTS`, `LEVEL_NORM_AGE`.
- `scripts/war_model.py` — new: `peak_war_from_ovr`, `aging_mult`, `load_stat_history`, `stat_peak_war`.
- `scripts/arb_model.py` — new: `estimate_service_time`, `estimate_control` (extracted from `contract_value.py`).
- `player_utils.py` reduced to bucketing, display helpers, league settings, PAP; re-exports new modules for backward compat.

**Phase D — Arb formula consolidation**
- `arb_salary(ovr, bucket, arb_year, prior_salary, min_sal)` added to `arb_model.py` — single canonical implementation using RP-specific exponential model for RPs.
- `contract_value.py`, `team_queries.py`, `projections.py` all updated to call `arb_salary()`. `team_queries.py` previously used a `rp_mult=0.80` approximation; now uses the correct RP model.

**Phase E — Docs**
- `STRUCTURE.md` updated with new script files.
- `docs/tools_reference.md` rewritten to match actual interfaces.
- `.kiro/steering/dev-agent.md` gate table updated with `fv_model`, `war_model`, `arb_model`, `ratings` entries.
- `docs/code_cleanup.md` updated with completed items.

### Test Suite
- `tests/test_player_utils.py` — 21 tests: `norm`, `calc_fv`, `peak_war_from_ovr`, `aging_mult` (including monotonicity invariants).
- `tests/test_prospect_value.py` — 12 tests: `prospect_surplus`, `prospect_surplus_with_option` (known values + structural invariants).
- `tests/test_arb_model.py` — 11 tests: `arb_salary` known values and invariants.
- 63 tests total, all passing.

---

## Session 40 (2026-03-24)

### Global Player Search
- Added site-wide search bar in the nav header (between Settings and Refresh).
- Uses existing `/api/player-search` endpoint (built for trade tab). 200ms debounced fetch, up to 15 results.
- Dropdown shows name, position, team, level, Ovr/FV. Keyboard navigation (↑/↓/Enter/Escape).
- Selecting a result navigates to `/player/<pid>`.

### Prospect Side Panel Ratings Alignment
- Fixed tool ordering in prospect side panel (`get_prospect_summary`) to match player page:
  - Pitchers: Stuff → Movement → HR Allow → BABIP Allow → Control → Stamina
  - Hitters: Hit → BABIP → Avoid K's → Gap → Power → Eye → Speed
- Added `sub` flag to extended ratings (BABIP, Avoid K's, HR Allow, BABIP Allow) so they render with `grade-sub` styling (indented, dimmed) in the side panel.
- Fixed defensive position sort order: now uses diamond order (C→1B→2B→3B→SS→LF→CF→RF) instead of grade descending.

### Prospect MLB Comps
- New feature: prospect player pages show 3 MLB player comps at different outcome tiers.
- **Tiers**: Upside (p75 outcome), Likely (p50), Floor (p25) — derived from the existing career outcome probability model.
- **Matching**: 70% tool shape similarity (Euclidean distance on normalized tool vector) + 30% WAR proximity. Tool vectors:
  - Hitters: contact, gap, power, eye, K-avoidance, speed, primary position defense (7 dimensions)
  - Pitchers: stuff, movement, control (3 dimensions)
- Prospect's potential ratings are blended toward current at each tier (100%/70%/40% pot) to create different target profiles.
- **Comp pool filters**: same bucket (OF buckets pooled), mature players only (age ≥26 or Ovr-Pot gap ≤5), no repeat players across tiers.
- Each comp shows team, age, Ovr, and current-year stat line (slash line or ERA/IP/WAR).
- Click-to-expand inline detail shows full grade bars (tools, pitches, defense) via new `/api/player-card/<pid>` endpoint.
- Accordion behavior: only one comp expanded at a time.
- New `get_player_card()` query function returns side-panel-style data for any player (MLB or prospect). Reuses `_build_tools()` helper extracted from `get_prospect_summary()`.

### Prospect Page Tabs
- Broke prospect player page into two tabs:
  - **Overview**: Ratings + Character (left), Scouting Report + MLB Comps (right)
  - **Valuation**: Surplus Projection (left), Career Outcome Probabilities (right)

### Ratings Display Consistency
- Avoid K's now always renders as a sub-rating of Hit (grade-sub class) on the player page, regardless of whether BABIP extended rating is present.
- Replaced `<h2>` section dividers (Pitches, Running, Positions, Defense) with `panel-section-title` divs on the player page, matching the side panel and comp expand style.
- Removed redundant `<thead>` column headers from tool tables on the player page.
- Moved Velocity from a tools table row to the Pitches section title (matching side panel).
- Changed grade bar divider lines from dark (`rgba(0,0,0,0.35)`) to light (`rgba(255,255,255,0.15)`) for consistent visibility across all contexts.

### Prospect Comp Refinements
- **Upside overshoot**: Upside tier now uses 110% blend toward potential (was 100%), allowing comps slightly beyond scouted ceiling. Proportional to development gap — raw prospects get more overshoot than near-mature ones.
- **Self-match prevention**: Comp pool now excludes the prospect themselves (`player_id != ?`) and all other players in `prospect_fv`. Prevents MLB-level prospects from matching themselves or other developing players. Fixed 19 self-matches and 23 prospect-as-comp cases across 50 MLB prospects.
- **MLB prospect comps**: Rookie-eligible MLB players (in both `player_surplus` and `prospect_fv`) now get comps fetched and displayed. Previously only minor league prospects got comps.

### MLB Rookie Outlook Tab
- MLB-level prospects now get an **Outlook** tab (Overview | Stats | Outlook | Contract) containing career outcome probabilities and MLB comps. Previously these were crammed into the Overview tab.
- Tab only appears for players who have prospect comps (i.e., rookie-eligible MLB players in `prospect_fv`). Regular MLB players still get Overview | Stats | Contract.

### Career Outcome Confidence Fix
- Confidence meter now accounts for Ovr/Pot gap. Changed from additive (`conf + 0.15 × ovr/pot`) to multiplicative (`conf × (0.5 + 0.5 × ovr/pot)`). A player with Ovr 60 / Pot 78 at MLB now gets 84% confidence instead of ~100%. Near-mature players (75/78) still get 93%.

### PAP Score (Payroll Adjusted Performance)
- New metric: maps current-season production efficiency to a 1–10 scale. 5.0 = market neutral, 7+ = above average (green), <4 = below market (red).
- **Production-based**: uses actual WAR pace (annualized from team games played) vs. salary, not ratings-based expected production. A player outperforming their ratings gets a high PAP regardless of Ovr.
- **League-calibrated**: scale = 2 × stdev(surplus_yr1), computed by `calibrate.py` and stored in `model_weights.json`. Runs automatically on refresh.
- **Formula**: `PAP = 5 + 5 × tanh(annualized_surplus / scale)` where `annualized_surplus = (WAR × 162/team_games) × $/WAR − salary`.
- **Displayed on**: player page summary bar (with tooltip), team page Hitters/Pitchers tabs (replaces surplus_yr1 column), player hover popup.

### Contract Extensions
- **Discovery**: StatsPlus API exposes pending contract extensions via `/contractextension/` endpoint. 23 active extensions found in eMLB.
- **Data pipeline**: `refresh.py` now pulls extensions and stores in new `contract_extensions` table. Only rows with `years > 0` are stored.
- **Surplus model integration**: `contract_value.py` checks for pending extensions before estimating control. If found, appends extension years and salary schedule after current contract ends. No more guessing arb salaries for players with known future deals.
- **Impact**: Players like Jack Trainor went from 1yr/$31.6M surplus to 6yr/$90.7M — the model now sees the full commitment.
- **Player page**: Contract tab shows extension salaries below current contract with "Pending Extension (Xyr)" divider. All contract years now show actual game years (2033, 2034...) instead of Y1, Y2.

### Draft Tab
- New tab on the league page (Overview | Prospects | Trade | Draft) for amateur draft scouting and tracking.
- **Draft pool**: CSV upload from OOTP's draft-eligible player export. Stores player IDs in `config/draft_pool.json`. Auto-detects amateur levels (10/11 for college/HS, 0 for combined) with age filters.
- **FV calculation**: Pot-weighted for draft prospects (`dev_weight` ≈ 0.75) — a 30 Ovr / 50 Pot player grades higher than a 40 Ovr / 45 Pot player, reflecting that pre-pro Ovr doesn't indicate talent ceiling.
- **Three table views**: All (compact), Hitters (tools + individual position defense + fielding ratings), Pitchers (tools + individual pitch potentials). All columns sortable, color-graded on the 20-80 scale.
- **Prospect detail panel**: StatsPlus-style compact grids showing tools (Pot/Cur), pitches (Cur/Pot), Run/Bunt, Fielding (C/IF/OF), Position grades, Character traits. All color-graded.
- **Compare mode**: Checkbox column (⚖) to select up to 2 prospects for side-by-side full detail comparison.
- **Draft pick tracking**: "Update Picks" button fetches from `/draftv2/` API, marks drafted players in the table, populates My Picks panel with rank/FV.
- **Position mismatch detection**: Flags players whose defensive ratings suggest a different (more/less valuable) position than their listed one (e.g., SS listed but only 1B-viable defense).
- **State detection**: Uploaded pool → Active draft (API picks match pool) → Pre-draft (stale API, approximate pool) → No data.
- **Filters**: All/Hitters/Pitchers views, name search, College/HS level dropdown, Hide Drafted toggle.
- **Outcome probabilities**: C% (Contributor), R% (Regular), AS% (All-Star), Bust% columns in All view. Ovr-based level mapping for outcome model (Ovr≥45→AAA, ≥35→AA, ≥28→A, else A-Short).
- **Profile classification**: Auto-labels prospects as Safe Star / Upside+ / Boom-Bust / Safe / Upside / Steady / Lottery based on outcome thresholds and bust probability. Displayed as colored pills.
- **Pill displays**: FV (color-graded), Level (College/HS), character traits (Acc/W/E/Lead/Int) shown as pill badges for scannability.
- **Career outcome chart**: Draft prospect player pages now show the full career outcome probability chart on the Valuation tab.
- **Hitter table expanded**: Individual fielding ratings (IFR/IFA/OFR/OFA/CBlk/CFrm), B/T, Lead, Int columns.
- **Pitcher table expanded**: Individual pitch potential columns (FB/SI/CB/SL/CH/CUT/SPL), throws hand, Lead, Int.
- **Normalization fix**: `_n80()` and draft-detail API now use `player_utils.norm()` respecting league ratings scale (no double-conversion on 20-80 leagues).
- **Greed coloring inverted**: Low greed = good (green), high greed = bad (red).
- **FV+ sort fix**: 65+ now ranks above 65 in the draft board.
- Spec: `.kiro/specs/draft-page.md`.

### Refresh Fix
- `state.json` now updated after successful refresh, not before. Previously a failed refresh (e.g., expired cookie) left the game date ahead of actual data, causing the sync badge to show "up to date" incorrectly.

---

## Session 39 (2026-03-24)

### MLB Service Time Model
- **Problem**: `_estimate_control()` counted qualifying seasons (AB≥100 or IP≥40/20) as service years. This treated partial seasons as full years — a player with 30 games in a year got 1 full service year. Result: young players with brief callups had inflated service time and fewer estimated control years than they actually have.
- **Fix**: Replaced qualifying-season counting with games-based fractional service time (`_estimate_service_time()`). Uses role-adjusted denominators per year: hitters g/162, SP gs/32, RP g/65. Takes max of hitter/pitcher fractions per year (two-way), caps at 1.0, sums across career.
- **Pre-arb players** (salary == min): use `floor(svc)` for service years. Age gates tightened: age ≥30 → veteran minor league deal; age ≥28 with svc ≥3 → same.
- **Arb players** (salary > min, 1yr deal): use `ceil(svc)` to account for unobservable roster days (bench time, IL stints). Floored at 3 (arb minimum) or 4 (salary > $5.5M).
- **Validation**: Tested against 10 Angels players with known OOTP control years. Service time estimation: 9/10 correct. One miss (Gentry, off by 1yr) due to bench-player underestimation — 130 games played counted as 0.80 service years when OOTP counted a full year on the roster.
- **Known limitation**: Games played underestimates roster time for bench players who are on the 26-man but don't appear in games. The StatsPlus API does not expose roster days or service time directly.

### Pitcher Role-Change Stat Fallback
- **Problem**: Pitchers who converted between SP and RP (e.g., Josh Moran SP→RP, Garrett Crochet RP→SP) had their stat history invisible to `stat_peak_war()` because it filtered to only matching-role seasons. These players were treated as "unproven" (0.5× WAR discount) despite having multiple years of MLB production.
- **Fix**: When current role yields no qualifying seasons but the opposite role has them, fall back to those stats scaled by IP ratio: SP→RP × 0.46 (~65ip/140ip), RP→SP × 2.15 (inverse). Applied in `stat_peak_war()` in `player_utils.py`.
- **Impact**: 38 pitchers affected. Moran went from 0.23 WAR (unproven RP) to 0.61 WAR (SP stats scaled to RP). Crochet's RP stats properly scaled up for SP projection.

### Non-Tender Gate Softened
- **Problem**: The non-tender gate truncated control when projected arb salary exceeded market value. This was too aggressive — 256 players truncated, including Ovr 50-60+ players. The unproven discount (0.5×) and recalibrated RP WAR curve made many borderline players appear non-tenderable years in advance.
- **Fix**: Raised threshold from 1× to 2× market value. Only truncates when arb salary is double the player's projected value.
- **Impact**: Truncated players reduced from 256 to 148. Elvis Fautsch: 3yr → 6yr control (matches OOTP). Amari Wanza: 5yr → 6yr (matches OOTP).

---

## Session 38 (2026-03-24)

### Contract Surplus Model — Low-End MLB Player Fix
- **Problem**: 317 of 916 MLB players had no qualifying stat history (`stat_peak_war` returned None), falling back to pure ratings-based WAR projections. Combined with 4-6 years of cheap estimated control, this massively inflated surplus for replacement-level players (e.g., Elvis Fautsch: 41 Ovr RP with −0.6 career WAR → $24.3M surplus).
- **Fix 1 — 1-season stat_peak_war**: Lowered minimum qualifying seasons from 2 to 1 in `stat_peak_war()` and `_two_way_peak_war()`. 150 players now get stat-based projections instead of pure ratings fallback.
- **Fix 2 — Negative stat blend in dev_ramp**: The development ramp in `contract_value()` previously ignored negative stat history (`stat_war > 0` gate). Now blends negative stat_war into future year projections with 0.5^year decay, same as positive outperformance.
- **Fix 3 — Unproven player discount**: When `stat_war` is None (0 qualifying seasons), ratings-based WAR is discounted by 0.5×. Data showed Ovr 35-44 players with 1 qualifying season produce only 5% of ratings-based projection on average. Applied to both base `pw` and dev_ramp future years.
- **Impact**: Fautsch $24.3M → −$1.6M. Jeff Roe (38 Ovr, pot 53) $84.7M → $35.4M. Cristopher Sanchez (43 Ovr, pot 66, legit prospect) $132.8M → $57.8M. Top established players unchanged.

### RP OVR_TO_WAR Recalibration
- **Problem**: RP regression in `calibrate.py` targeted P75 of residuals (rationale: "value the closer/setup role"). This inflated the entire curve by 2-3× at the low end. A 43 Ovr RP projected 0.82 WAR when actual mean production is ~0.30 WAR.
- **Fix**: Removed P75 shift — RP regression now targets the mean, same as all other positions.
- **New curve**: Ovr 43 = 0.30 WAR (was 0.82), Ovr 50 = 0.62 (was 1.21), Ovr 55 = 0.85 (was 1.38). A "solid 0.7 WAR middle reliever" now maps to ~Ovr 53.
- **High-end RPs**: Minimally affected — elite RPs are driven by stat history, not the regression. Kerkering (Ovr 80) projects 1.50 WAR, Bednar (Ovr 75) 1.18 WAR — both reasonable.
- **RP prospect impact**: FV_TO_PEAK_WAR_RP recalibrated downstream. FV 50 RP prospect: 0.8 peak WAR (was ~1.2).
- **RP surplus distribution**: Average RP surplus dropped to +$1.4M. Split 151 positive / 138 negative — much more realistic.

### Ctrl Rating API Bug Fix
- **Bug**: StatsPlus API mislabels all three Ctrl columns. Data order is correct (overall, vs_R, vs_L) but labels are `Ctrl_R`, `Ctrl_L`, `Ctrl_L` (duplicate). Old fix only renamed the duplicate `Ctrl_L` → `Ctrl` but didn't fix the `Ctrl_R` ↔ `Ctrl` swap.
- **Result**: Every pitcher's `ctrl` (overall) was actually their vs_L value, `ctrl_l` was vs_R, `ctrl_r` was overall. All historical ratings snapshots affected.
- **Fix**: `_fix_ratings_header()` in `client.py` now remaps all three: `Ctrl_R` → `Ctrl`, first `Ctrl_L` → `Ctrl_R`, second `Ctrl_L` stays `Ctrl_L`. Updated both 113-col and 126-col expected headers.
- **Validation**: Tested against live API data (Rasmussen: Ctrl=55, Ctrl_R=58, Ctrl_L=52 — matches game). Verified 637/637 pitchers with different splits have overall between L/R after fix.
- **Impact**: Display-only for valuation (Ovr/Pot come from API directly, not computed from tools). Affects farm_analysis tool grades, projections ctrl component, and player page display. Requires DB wipe + re-import to fix historical snapshots.

### Ratings Scale Bug Fix
- **Bug**: `player_utils._ratings_scale` was reset to None per request, but re-read went through the module-level `league_config.config` singleton which cached the first league's scale. Switching leagues (e.g., emlb 1-100 → vmlb 20-80) kept the old scale, causing `norm()` to incorrectly convert 20-80 values (e.g., Stf 70 displayed as 60).
- **Fix**: `before_request` in `app.py` now sets `player_utils._ratings_scale` directly from the per-request `LeagueConfig` instead of relying on the module singleton.

### Onboarding UX
- **Baseball spinner**: Onboarding data pull spinner changed from generic CSS border spinner to spinning ⚾ emoji (matches refresh button behavior).
- **Ratings scale labels**: Removed "(raw)" and "(scouting)" parentheticals from ratings scale dropdown options — just "1-100" and "20-80".
- **Auto-detect ratings scale**: Step 3 (configure) now checks if any Ovr/Pot exceeds 80 in the DB. If so, pre-selects "1-100"; otherwise defaults to "20-80". User can still override.

---

## Session 37 (2026-03-24)

### Trade Review Tab — Complete
- **Spec**: `.kiro/specs/trade-review-tab.md` — full design for interactive trade builder as a new tab on the league page (Overview | Prospects | Trade).
- **`search_players()`** in `web/queries.py` — league-wide player search (15 results, MLB first). Reusable for future global search bar.
- **`get_org_players()`** in `web/trade_queries.py` — full org roster (MLB + farm) with Ovr/Pot/FV/surplus/WAR. Rookie-eligible deduplicated into prospect section.
- **`get_trade_value()`** in `web/trade_queries.py` — single-player valuation adapter. Prospect path: `prospect_surplus_with_option()` + `career_outcome_probs()` with 0.85/1.00/1.15 sensitivity. Contract path: `contract_value()` with retention support and year-by-year breakdown.
- **3 API routes** in `web/app.py`: `GET /api/player-search?q=`, `GET /api/org-players/<tid>`, `POST /api/trade-value`.
- **Trade tab UI** in `league.html`: two-column layout with org pickers (Side A defaults to user's team), level filter tabs, name search, MLB + prospect roster tables with click-to-add, player cards (contract: surplus/flags/retention slider/expandable breakdown; prospect: FV badge/surplus/expandable career outcome summary), cash consideration inputs, live trade balance panel with pessimistic/base/optimistic scenarios and verdict.
- **CSS** in `style.css`: `.trade-page` grid, `.trade-card`, `.trade-table`, `.trade-balance`, `.trade-verdict`, retention slider, level filter tabs, surplus coloring.

### Trade Balance Logic
- **Direction fix**: each side's players are what that side *sends*. Net = received surplus − sent surplus.
- **Scenario crossing**: pessimistic for Side A = sent players hit optimistic (gave up more) + received players hit pessimistic (got less). Optimistic is the reverse.
- **Mid-season pro-rating**: first year of MLB contract surplus is scaled by `(162 − avg_GP) / 162` in the balance calculation. Prospect surplus unaffected (control period hasn't started).

### UI Polish
- Full team names in org picker dropdowns (sorted alphabetically) and balance panel rows. Abbreviations in verdict.
- Selected team hidden from opposite side's dropdown.
- "Side A/B" labels removed from UI.
- Level filter tabs sorted in baseball hierarchy (MLB → AAA → AA → A → Rookie).
- Level column uses colored pills (`lvl-badge`), only shown when "All" filter is active.
- Table columns properly aligned (Pos/Name left, numeric right).
- Fixed-height scrollable card area (`.trade-package`) keeps roster tables aligned across sides.
- Balance panel centered with `max-width: 560px`.

### Task List Cleanup
- **3.8 Data wipe** marked done — was implemented in Session 28 (`/api/wipe-league` endpoint, settings UI, redirect to `/onboard`).
- **Transaction log** marked shelved — research done, StatsPlus API does not expose transaction data.
- **Playing time model edge case #1** (two-way players) updated — detection fixed in Session 23, PT allocation may still be off.
- **Global player search** added to Navigation backlog — reuses `/api/player-search` endpoint from trade tab.

### Aging Curve Recalibration
- Both hitter and pitcher aging curves steepened from age 31+, calibrated from league data (same-pitcher panel tracking with 3-year rolling baseline).
- OOTP aging is more aggressive than IRL — old curves were based on MLB research, new curves fit actual league production decline.
- SP: age 31 0.91→0.85, age 32 0.84→0.76, age 33 0.77→0.66, age 34 0.65→0.54, age 35 0.55→0.43.
- Hitter: age 31 0.91→0.84, age 32 0.85→0.76, age 33 0.79→0.68, age 34 0.76→0.60, age 35 0.67→0.51.
- Impact: long-term contracts for aging players become significantly more negative. Back-loaded deals properly penalized.

### MLB Scarcity Premium
- New `MLB_SCARCITY` constant in `constants.py`: positional multiplier on market value for MLB contract players.
- SS: 1.10, CF/SP: 1.06, C/2B/3B: 1.03, COF/RP: 0.94, 1B: 0.91.
- Applied in `contract_value()` to market value calculation — affects surplus in `player_surplus` table, player page breakdown, trade tab valuations, CLI trade calculator.
- Makes contract model consistent with prospect model (which already had scarcity via `_scarcity_mult`).
- Example: Cassie Thurman (SP, 74 Ovr, age 29) went from −$7.5M (old aging, no scarcity) → −$28.6M (new aging) → −$15.4M (new aging + scarcity).

---

## Session 36 (2026-03-24)

### RP-Specific Career Outcome Model
- Compression center shifted from 3.0 → 1.8 WAR for RPs — stops the curve from crushing probabilities in the RP ceiling range.
- RP thresholds: Contributor @ 0.5 WAR, Quality @ 1.0, Elite @ 1.5 (vs 1.0/2.0/3.0 for hitters/SP).
- WAR cap: 3.0 for RPs (24 bars) vs 5.0 for everyone else (40 bars).
- Template renders threshold names dynamically — no template changes needed.

### Current-Season Surplus on Team Stats
- Added `surplus_yr1` column to `player_surplus` table — stores first-year surplus from contract breakdown.
- Hitter and pitcher stats tables on team page now show current-season surplus instead of total remaining contract surplus.
- `fv_calc.py` drops and recreates `player_surplus` table on each run to ensure correct column order.
- Tooltip updated: "Current-season surplus (market value minus salary)".

### RP WAR Calibration
- **IP threshold fix**: Calibration regression now uses IP≥20 for RPs (was IP≥40, the SP threshold). Old threshold excluded most closers/setup men (median RP IP is 34), biasing the regression toward high-IP mop-up arms.
- **P75 regression shift**: RP regression intercept shifted up to target P75 instead of mean. A team's primary RP at a given OVR is a closer/setup arm, not a mop-up reliever. The shift is computed from the top-quartile residual of the actual data.
- Combined effect: RP OVR→WAR increased ~0.3 WAR across the board (e.g., OVR 60: 1.22 → 1.61, OVR 70: 1.59 → 2.00). Now matches actual P75 WAR production closely.
- FV_TO_PEAK_WAR_RP updated proportionally (e.g., FV 60: 1.4 → 1.8, FV 70: 1.8 → 2.2).

### Bug Fixes
- **Career outcome chart missing for rookie-eligible MLB players** — players in both `player_surplus` and `prospect_fv` took the MLB code path, skipping outcome probability computation. Added fallback: if MLB player also has a `prospect_fv` row, compute outcome probs from the prospect data.
- **Ratings scale cache** — (continued from Session 35) `app.py` `before_request` now resets `player_utils._ratings_scale = None` each request.

---

## Session 35 (2026-03-23)

### Career Outcome Probability Chart
- New panel on prospect pages showing cumulative probability of reaching each WAR/season tier.
- **Model**: Reuses FV option value scenarios (base/mid/ceiling) with logistic CDF for within-scenario variance. Smooth elite compression via sigmoid centered at 3 WAR — no hard cutoff. Development discount scales all probabilities by bust risk.
- **Chart**: 0.125 WAR increments (40 bars) as a smooth waterfall. Mid-50% zone highlighted in brighter blue, tails in darker blue. Position average WAR bar highlighted in gold. WAR labels at whole numbers only.
- **Threshold summary**: Contributor (1 WAR), Regular (2 WAR), All-Star (3 WAR) probabilities shown above chart.
- **Most likely outcome**: Text callout showing the WAR range of the middle 50% by area.
- **Confidence meter**: Green fill bar based on level proximity and Ovr/Pot realization.
- Shows on prospect pages (below surplus panel) and in the Contract tab for rookie-eligible MLB players.
- **Files**: `scripts/prospect_value.py` (`career_outcome_probs()`), `web/player_queries.py`, `web/templates/player.html` (`outcome_panel` macro), `web/static/style.css`.

### Bug Fixes
- **Ratings scale cache not cleared on settings change** — `player_utils._ratings_scale` was cached at module level and never reset. Changing the ratings scale in settings (e.g., from unset to "1-100") had no effect until server restart. Fix: `app.py` `before_request` now resets `_ratings_scale = None` each request so `norm()` always reads the current league config.

### Session 34 Documentation
- Added Session 34 changelog entry covering all prior session changes (scarcity fix, position-adjusted scarcity, rookie-eligible prospects, ratings_history, visual polish, bug fixes).
- Updated `task_list.md` — marked scarcity curve refinement and rookie-eligible as done.
- Updated `docs/system_overview.md` — ratings_history table, updated ratings/prospect_fv/player_surplus descriptions, ratings storage design decision.
- Updated `docs/valuation_model.md` — position-adjusted scarcity documentation.
- Updated `STRUCTURE.md` — added ratings_history table.

---

## Session 34 (2026-03-23)

### Scarcity Calibration Fix
- **Cross-league scarcity fix** — Sigmoid midpoint changed from 0.35 to 0.65 in `calibrate.py` `_ratio_to_scarcity()`. Both leagues now produce reasonable curves (Pot 46: 0.22-0.29, Pot 50: 0.60-0.72, Pot 70: 0.94). VMLB scarcity also corrected — Pot 46 went from 0.98 to 0.22.

### Position-Adjusted Scarcity
- **Positional Pot shift** — `prospect_value.py` `_scarcity_mult()` now applies a position-based shift to the effective Pot before scarcity lookup. SS: +4, CF: +2, SP: +2, C: +1, 2B: +1, 3B: +1, COF: -2, RP: -2, 1B: -3.
- **Defense-scaled shifts** — For CF/SS/C/2B/3B, the shift scales with defensive potential rating. Full shift at PotDef ≥ 70, linear 50-70, zero below 50. An elite defensive CF (PotCF 87) gets the full +2 bump; a bat-first CF (PotCF 40) gets none.
- `fv_calc.py`, `trade_calculator.py`, and `player_queries.py` all pass `def_rating` through to the surplus calculation.
- Pot 45 positional ordering: SS ($12.2M) > CF ($8.4M) > SP ($8.2M) > 3B ($5.7M) > 2B ($3.2M) > C ($2.5M) > 1B ($1.2M) > COF ($1.1M) > RP ($0.8M).

### Rookie-Eligible MLB Players in Prospect Rankings
- MLB players with <130 career AB AND <50 career IP AND age ≤ 24 now appear in `prospect_fv` with level "MLB". Uses MLB rookie eligibility thresholds.
- Players appear in both `prospect_fv` (for rankings) and `player_surplus` (for trade calc).
- FV computed using AAA norm age, dev discount 1.0, years_to_MLB 0.
- Web queries updated: `queries.py` and `team_queries.py` use `COALESCE(NULLIF(parent_team_id,0), team_id)` to resolve org for MLB-level prospects.
- eMLB: 89 rookie-eligible added (e.g., Loki Swayman FV 75 #1 overall, Yamamoto FV 70 #11). VMLB: 86 added.

### Ratings History Table
- New `ratings_history` table (53 columns vs 121 in `ratings`) for monthly in-game snapshots. Stores ovr/pot, hitter tools (current+potential), pitcher tools (current+potential), all 12 pitch types (current+potential), extended ratings (babip/hra/pbabip) when available.
- ~1.3MB per snapshot vs ~3.5MB for full ratings (~38% the size).
- `refresh.py` appends snapshot on first refresh of each in-game month. `ratings` table now keeps only the latest snapshot (old snapshots pruned on refresh).
- Seeded both leagues from current data. VMLB reclaimed 4.2MB after vacuum.

### Data Integrity Fixes
- **Duplicate prospect_fv/player_surplus rows** — `fv_calc.py` now does `DELETE FROM prospect_fv` and `DELETE FROM player_surplus` (was date-scoped, leaving stale rows from prior eval dates).
- **STATSPP_LEAGUE env var** — `league_context.py` now checks `os.environ["STATSPP_LEAGUE"]` before `app_config.json`. Enables `STATSPP_LEAGUE=vmlb python3 scripts/calibrate.py` without editing config.

### Player Page Visual Refresh
- **Segmented rating bars** — `::after` overlay on grade tracks with dividers aligned to 20-80 scouting scale (every 16.667%). Bold current rating, faded potential in label chips.
- **Character pill badges** — colored pills instead of plain text (green=Very High, blue=High, orange=Low, red=Very Low).
- **WAR heat-map** in surplus projection table — 4.0+ bright blue, 2.0-3.0 white, below dim.
- **Panel headers** — accent-colored (gold) bottom border.
- **Surplus projection improvements** — Raw Total + Adjusted breakdown for prospects, plain Total for MLB. Discount formula moved to ⓘ tooltip. Projected calendar years instead of "Ctrl 1/2/3". Option value included in total (matches card value).
- **Section spacing** — 16px gaps between Running, Positions, Pitches sections.

### Bug Fixes
- **Prospect highlight on even rows** — `tr.highlight` was overridden by `tr:nth-child(even)` background. Fixed with `tr.highlight:nth-child(even)` selector.
- **Highlight suppressed for single-team views** — no highlighting when filtered to one team in top 100 or by-team view.
- **1B/3B missing TDP rating** — turn double play fielding rating now shown for all infield positions (was only 2B/SS).
- **Infielder ZR composite** — added arm rating (25% weight) alongside range (50%) and error (25%). Fixes low expected ZR for strong-armed infielders like Tom Shuey (IFA 97).
- **MLB level pill/dots** — added gold/amber `lvl-mlb` CSS class and 5 filled dots for MLB-level prospects on league page.

---

## Session 33 (2026-03-23)

### Top 100 Prospect Model Tuning
Session 32 audit found four issues: COF flood (34/100), AAA concentration (81/100), inflated surplus values, and safe-over-ceiling bias. This session addressed all four with six changes.

- **Position-specific FV→WAR for hitters** — `calibrate.py` now derives per-bucket hitter tables (`FV_TO_PEAK_WAR_BY_POS`) instead of averaging all hitter positions. COF FV 50 → 3.0 WAR (was 3.3), SS → 3.6, CF → 3.9, C → 2.9. Stored in `model_weights.json`, loaded by `constants.py`, used by `prospect_value.py` `peak_war()`. Directly addresses COF flood and inflated surplus values.
- **Flattened development discount** — AAA: 0.88 (was 0.90), AA: 0.78 (was 0.75), A: 0.68 (was 0.60), Rookie: 0.45 (was 0.38), Intl: 0.35 (was 0.25). Old curve dropped too steeply below AAA, causing 81/100 to be AAA. The AA and A increases are the biggest drivers — high-ceiling A-ball arms were being penalized 33% vs AAA.
- **Certainty multiplier capped at 1.0** — Was up to 1.15x for maxed prospects (Ovr/Pot near 1.0). This double-counted AAA proximity since those players already benefit from higher dev discount and lower time-value discount.
- **Steeper age adjustment** — 4%/yr (was 3%) for young-for-level bonus and old-for-level penalty. Targeted lift for young A-ball prospects without changing AAA values (already at 0.95 cap).
- **Gap-scaled option value** — Upside probabilities now scale with the Pot-FV gap in addition to youth. `gap_factor = min(1.0, (pot - fv) / 25)` boosts p_mid and p_ceil for high-ceiling prospects. Ceiling FV cap removed (was 70, now uncapped). A Pot 80 / FV 50 player gets ~30% base / 45% mid / 25% ceiling probabilities (was 42/40/18).
- **Prospect age cutoff lowered to 24** — 25yo minor leaguers are MLB-bubble players, not prospects. `fv_calc.py` now excludes age 25+. Added `DELETE` before `INSERT` to clear stale rows.

**Results** (VMLB top 100):
| Metric | Before | After |
|---|---|---|
| COF count | 34 | 25 |
| AAA count | 81 | 60 |
| A-ball count | 4 | 19 |
| Age ≤ 20 | 7 | 17 |
| #50 surplus | $94.7M | $81.8M |
| #100 surplus | $75.8M | $65.1M |

Key prospect movement: Ricky Sanchez (A 1B, 18, Pot 80) #29→#4, Honor Lara (A COF, 17, Pot 80) #44→#9, Alex Rodriguez (A SP, 19, Pot 68) >100→#59, Angelo Rivera (AA SP, 20, Pot 55) >100→#87.

### Bug Fixes
- **Refresh button race condition** — Navigating during a refresh showed the green ✓ badge instead of the spinner. Root cause: `/api/game-date` and `/refresh/status` fired in parallel on page load; the game-date response overwrote the spinner because `state.json` updates early in the pipeline. Fix: added `refreshRunning` flag, sequenced the checks so sync badge only renders when no refresh is active.
- **Cross-league stale date indicator** — After switching leagues, the refresh button showed stale (!) because `/api/game-date` was still querying the previous league's StatsPlus API. The client retained the old league's slug. Fix: `api_game_date` now configures the client with the current league's `statsplus_slug` before each call.
- **Scarcity calibration broken for eMLB** — Old scarcity model counted unsigned amateur pool (team_id=0, level=0) as "free agents." VMLB worked by accident (few high-Pot unsigned players); eMLB had 30K+ unsigned players inflating FA rates at every Pot level, zeroing out scarcity for everything below Pot 62. Fix: switched to measuring non-MLB rate among rostered players (team_id > 0) relative to baseline (Pot 38-42). Sigmoid midpoint at 0.65 ratio. Both leagues now produce similar, reasonable curves (Pot 46: 0.22-0.29, Pot 50: 0.60-0.72, Pot 70: 0.94). VMLB scarcity also changed — Pot 46 went from 0.98 to 0.22, which is more realistic.

---

## Session 32 (2026-03-23)

### League-Calibrated Valuation Model
- Built `scripts/calibrate.py` — derives league-specific valuation tables from actual data instead of hand-tuned constants. Produces `config/model_weights.json` with position-specific `OVR_TO_WAR`, `FV_TO_PEAK_WAR` (hitter/SP/RP), `ARB_PCT`, and `SCARCITY_MULT`.
- **OVR_TO_WAR**: Position-specific Ovr→WAR regression from 3 years of data (2030-2032). 9 buckets (C, SS, 2B, 3B, CF, COF, 1B, SP, RP) with 53-355 seasons each. Falls back to grouped hitter regression when N < 40. Key findings: SS produces 4.44 WAR at Ovr 60 vs C 3.69 vs COF 3.70 — old generic table said 3.2 for all. SP produces 2.96 at Ovr 60 (old: 2.8). RP close to old values.
- **FV_TO_PEAK_WAR**: Derived from OVR_TO_WAR by mapping FV+5 to expected peak Ovr. Now has separate hitter, SP, and RP tables. FV 45 hitter = 2.6 WAR (was 1.2), FV 50 SP = 2.5 (was 2.0). Old table was calibrated for a different league and significantly undervalued mid-tier prospects.
- **ARB_PCT**: Calibrated from 104 arb-eligible players. Arb 1: 21% (was 20%), Arb 2: 18% (was 22%), Arb 3: 34% (was 33%). Minor changes.
- **SCARCITY_MULT**: Sigmoid-based mapping from FA availability rate to scarcity. Uses 2-point Pot bands for smoothing, monotonic enforcement. Mid-season only (offseason FA pool is flooded). Pot 40: 0.0, Pot 42: 0.03, Pot 44: 0.44, Pot 46: 0.97, Pot 50: 1.0.
- `constants.py` now loads from `model_weights.json` when present, falling back to hardcoded defaults. New exports: `FV_TO_PEAK_WAR_SP`, `OVR_TO_WAR_CALIBRATED`.
- `player_utils.py` `peak_war_from_ovr()` uses position-specific calibrated tables when available.
- `prospect_value.py` `peak_war()` uses SP-specific FV→WAR table for starting pitchers.
- `refresh.py` runs calibration before fv_calc in the refresh pipeline. Calibration failure is non-fatal (logs warning, uses defaults).
- Key value changes: Mead $68.2M → $100.5M, Teschler $67.5M → $83.8M, Showalter $49.2M → $86.9M, Jobe $32.6M → $71.6M. Increases driven by calibrated FV→WAR tables showing mid-tier players produce more WAR than old hand-tuned constants assumed.

### Veteran Decline Ratings Blend
- Victor Robles (Ovr 44, age 36, COF) showed $25.9M surplus despite being a clearly declining player. Root cause: his `stat_peak_war` of 3.31 was propped up by a 5.3 WAR season from 2030, and his worst recent season (2032: -0.1 WAR, 125 AB) was excluded for falling below the 130 AB qualifying threshold. The model had no downward blend for veterans — it only blended ratings upward for young players.
- **Fix**: Added declining veteran ratings blend in `contract_value.py`. For players past age 31 (30 for pitchers) where stat WAR exceeds ratings WAR, blends toward ratings. Weight scales with both age past peak and gap size (`age_w × gap_ratio`, capped at 0.75). Small gaps at age 32 get minimal correction; large gaps at age 36+ get aggressive correction.
- Robles: $25.9M → $16.4M (projected WAR: 3.31 → 2.25). Other affected players: Acuna ($2.0M), Julio Rodriguez ($11.2M), Adames ($7.2M) — all now more reasonable.

### Top 100 Prospect Audit (findings only — no code changes)
- **COF flood**: 34 of top 100 are COF (42 total OF). Model doesn't penalize positional replaceability — a Pot 53 COF ranks alongside scarcer positions.
- **AAA concentration**: 81 of 100 in AAA, only 4 in A-ball, 0 in Rookie/Intl. Development discount may be too steep for lower levels, or model over-rewards MLB proximity.
- **Surplus values feel high**: #100 at $75.8M, #50 at $94.7M. Driven by calibrated FV→WAR tables — FV 45 hitters now map to 2.6 WAR peak. Needs validation: do FV 45 prospects actually reach 2.6 WAR?
- **FV 50 clustering**: 48 of 100 are FV 50 with huge surplus spread ($121M to $76M). FV system may not differentiate enough in the middle tier.
- **No RPs**: RP FV discount working as intended — 0 RPs in top 100.
- Added findings to task list for next session.

---

## Session 31 (2026-03-23)

### Surplus Model Validation — Scarcity Recalibration
- Systematic validation revealed the original scarcity curve (from Session 30) was too gradual — didn't reach 1.0 until Pot 65, while FA availability data shows 0% availability at Pot 49+.
- **Fix (iteration 1)**: Steeper ramp: `{40: 0.0, 43: 0.10, 45: 0.30, 47: 0.60, 49: 0.85, 50: 1.0}`. Fixed the bottom end but created a cliff at Pot 48-50 (28% penalty for 2-point Pot difference).
- **Fix (iteration 2)**: Smoothed S-curve: `{40: 0.0, 42: 0.05, 44: 0.20, 45: 0.35, 46: 0.55, 47: 0.75, 48: 0.92, 49: 1.0}`. Reaches 1.0 at Pot 49 to reflect scouting fog of war — 1-2 point Pot differences are within noise. No single-point cliffs anywhere.
- Validated with fog-of-war test: Pot 48 vs 49 is only 8% swing (was 15%+ before). Pot 48 vs 50 is 8% (was 28%).
- Key prospect impact: Jobe (Pot 49) +18%, Donovan (Pot 48) +27%, Neely (Pot 47) +25%. High-ceiling unchanged.

### Surplus Model Validation — Realization Blend
- Crossover analysis showed maxed-out prospects (Ovr ≈ Pot) had a discontinuity: prospect surplus was 0.36x of MLB contract value for hitters and 1.31x for RPs at the same grade.
- Root cause: `FV_TO_PEAK_WAR` assumes further development, but maxed players have already reached their ceiling. Their peak WAR should equal their current production (from `OVR_TO_WAR`).
- **Fix**: When `ovr/pot` realization exceeds 0.7, blend `peak_war(fv)` with `peak_war_from_ovr(ovr)` using a squared weight curve. At realization 1.0 (fully maxed), uses 100% OVR→WAR. At 0.7 (still developing), uses 100% FV→WAR. Only applies when OVR→WAR < FV→WAR (downward adjustment only).
- Crossover ratios now 0.79-0.86x across all positions at Pot 50+ (was 0.36-1.31x). Developing players (realization < 0.7) completely unaffected.

### Trade Scenario Analysis — 3B Upgrade
- Evaluated 3B trade targets for the Rays (team 57, 40-33) from seller/fringe teams.
- Identified key targets: Eric Elwood (CLE, Ovr 64, 3B:65, $130M+ with extension), Andy Tatum (STL fire sale, Ovr 53, 3B:65, $65.5M), Kris Williamson (BAL fringe, Ovr 56, 3B:55, $80.3M), Bobby Butler (LAA, Ovr 58, 3B:45, $47.6M), Pat Clark (MIN rental, Ovr 70, $31.5M).
- Elwood has a 10yr/$136M extension not captured by the API — manually modeled at $261.5M surplus (vs $130.4M without extension). Effectively untradeable.
- Tatum best package: Teschler straight up ($67.5M, 1-for-1 avoids consolidation tax) or Woods + Showalter + filler (~$65-66M, needs ~10-15% overpay for 3-for-1).

### Data Gap — Pending Contract Extensions
- Discovered that the StatsPlus API returns only the current active contract, not pending extensions signed during the season. Elwood showed as 1yr/$610K pre-arb when he actually has a 10yr/$136M extension. Added to task list.

### RP FV Positional Discount
- FV grades were too generous to RPs — 102 RPs earned FV 45+ (23.8% of all ranked prospects), vs ~3-5% in real baseball prospect lists. A Pot 55 RP got the same FV as a Pot 55 COF despite producing far less WAR.
- **Fix**: Before FV calculation, RP Pot is scaled to 80% of raw value. A Pot 55 RP now has effective Pot 44, dropping from FV 45+ to FV 40. Only elite RPs (Pot 70+) reach FV 50.
- League-wide RPs at FV 45+: 102 → 26 (7.4%). Top 100 by FV: 4 RPs → 3 RPs.
- Surplus uses the raw (undiscounted) FV to avoid double-counting with the RP-specific WAR table. `fv_calc.py` computes raw FV separately for RPs and passes it to `prospect_surplus_with_option`.

### Valuation Model Documentation
- Created `docs/valuation_model.md` — plain-language explanation of how FV grades, prospect surplus, and MLB contract surplus work. Covers the full pipeline from ratings to trade value without requiring code reading.

### Positional WAR Regression (investigation only — no code changes)
- Ran Ovr→WAR regression by position bucket using 2031-2033 data (61-491 seasons per bucket).
- Generic hitter table is a poor fit: at Ovr 65, SS produces 5.47 WAR vs COF 3.51 vs C 2.97 — model says 4.5 for all. SS slope (0.217) is 2x COF slope (0.119).
- SP table overestimates at high OVR: actual 3.0 at Ovr 65 (model 4.0), actual 3.5 at Ovr 70 (model 5.5). Likely calibrated against a different league.
- RP table is close (0.86-0.95x across Ovr 50-70).
- Conclusion: constants were likely tuned for a different league. Added league-calibrated valuation model to task list as the proper fix rather than hand-tuning for VMLB.

### RP Service Time Fix
- `_estimate_control()` in `contract_value.py` used IP >= 40 for all pitchers to count qualifying seasons. RPs were being undercounted by 1-4 service years because they rarely reach 40 IP in a season.
- **Fix**: RPs (detected by bucket) now use IP >= 20 threshold. Passed `bucket` parameter to `_estimate_control()`.

### Pitcher Percentile Qualification Thresholds
- Percentile pool used a single IP threshold (0.5 × team_games) for all pitchers. RPs with a full workload (e.g. 34 IP mid-season) were flagged as "small sample."
- **Fix**: Split thresholds — SP uses 0.7 × team_games (55 IP), RP uses 0.35 × team_games (27 IP). RP detected by GS/G ratio < 0.25. SPs below the higher threshold correctly show as small sample even if they're in the broader pool.

### Pitcher BABIP Expected — Regression Model
- Pitcher BABIP expected percentile was using rating percentiles from the MLB pool. The pbabip rating distribution is extremely compressed (stdev 3.3, range 45-70) — a 50 rating showed as 4th percentile because 96% of MLB pitchers have pbabip ≥ 50 (survivorship bias).
- **Fix**: Replaced rating percentile with a regression model: `expected_BABIP = 0.439 - 0.0028 × pbabip` (r=-0.18, from 362 qualifying seasons). Maps the rating to an expected BABIP value, then percentile-ranks that against the stat pool. pbabip 50 → .299 expected (≈44th percentile). McKeever (.333 actual) now correctly tagged "unlucky."

---

## Session 30 (2026-03-23)

### Ratings Scale Support (20-80 / 1-100)
- Leagues using OOTP's 20-80 scouting scale (tools in 5-point increments, OVR/POT in single increments) were being double-normalized by `norm()`, which assumed 1-100 raw input. This compressed tool grades toward the center (80→70, 70→60, 35→40) and caused incorrect defensive bonuses, critical tool floor penalties, and OPS+/BABIP projections.
- **Fix**: `norm()` in `player_utils.py` is now scale-aware via lazy config detection. On 20-80 leagues, it passes values through unchanged (clamp + round to 5). On 1-100 leagues, behavior is unchanged.
- **Fix**: `projections.py` `project_ops_plus()` converts 20-80 inputs to 1-100 equivalent before applying regression coefficients calibrated on 1-100 data.
- **Fix**: `percentiles.py` BABIP expected model handles both scales for direct rating conversion and regression fallback.
- **Fix**: Platoon split thresholds in `calc_fv` now use `norm()` for scale-independent comparison.
- Added `ratings_scale` setting to `league_settings.json`, `league_config.py`, settings page, and onboarding wizard. Defaults to `"1-100"` for backward compatibility.
- FV grades themselves are minimally affected (driven by Ovr/Pot which were already correct) but display grades, OPS+ projections, and BABIP models are now accurate.

### Prospect Scarcity Multiplier
- FV 40-45 prospects (replacement-level depth) were valued at $16-29M surplus despite being freely available on waivers. A FV 45 RP prospect (Fisher, Ovr 43) was valued at $28.9M — on par with Chris Brown (Ovr 75 MLB RP, $28.1M). No GM would make that swap.
- **Fix**: New `SCARCITY_MULT` table in `constants.py` applies a non-linear multiplier to prospect surplus based on Pot (ceiling). Derived from MLB talent distribution data: Ovr 45-49 players comprise 35% of MLB rosters (abundant), while Ovr 65+ are <3% (scarce).
- Table: `{40: 0.0, 45: 0.15, 50: 0.45, 55: 0.70, 60: 0.90, 65: 1.0}` — interpolated for intermediate values.
- Uses Pot rather than FV so developing players (e.g. 40 Ovr / 65 Pot) are valued for their ceiling, not current ability. A maxed-out 43/43 gets 0.09x; a raw 40/65 gets 1.0x.
- Applied in `prospect_value.prospect_surplus()` alongside dev_discount and certainty_mult.
- Prospect surplus breakdown now applies combined multiplier to per-year surplus so rows sum to the total. Market value and salary stay raw. Discount math shown below the table on player pages.
- Fisher: $28.9M → $2.6M. Mead (40/65): $42.5M → $68.2M (Pot-based scarcity + option value).

### Extended Ratings Bug Fix
- Player pages crashed with `no such column: babip` on leagues without extended rating columns (BABIP, HRA, PBABIP, Prone). Root cause: `player_queries.py` hardcoded extended column names in SELECT; `percentiles.py` did the same for BABIP expected model and pitcher percentile ratings.
- **Fix**: `player_queries.py` switched from explicit column list + tuple unpack to `SELECT *` + dict access. Missing columns return `None` via `.get()`. All downstream code already guarded with `if X is not None`.
- **Fix**: `percentiles.py` conditionally includes extended columns using `has_extended_ratings()` helper, falls back to `NULL` when absent.
- **Fix**: `web_league_context.py` added `has_extended_ratings()` — checks `PRAGMA table_info(ratings)` for `babip` column, cached per request.
- **Fix**: `refresh.py` added backfill step in `_upsert_ratings()` — updates extended columns on existing rows when incoming API data has them but DB values are NULL. Fixes leagues where rows were inserted before 126-col format support was added.

### RP Surplus Model Calibration
- Regression analysis on 1,582 qualifying RP seasons (IP≥20, GS≤3) revealed three model problems:
  1. `RP_WAR_CAP=2.0` flattened FV 50–80 RPs to identical surplus ($63.3M each)
  2. `REPLACEMENT_WAR=1.0` threshold created an $8.4M cliff, zeroing out sub-1.0 WAR control years
  3. FV 45 peak WAR (1.2) was too high vs actual data (median 0.5 WAR for Ovr 48 RPs)
- **Data findings**: 80 Ovr RPs average 1.9 WAR vs 0.6 for 50 Ovr (3x ratio). Only 10 RPs league-wide are 70+ Ovr (0.2%). Linear fit: WAR = -1.37 + 0.040 × Ovr (R²=0.15).
- **Fix 1**: New `FV_TO_PEAK_WAR_RP` table in `constants.py` — scales from 0.5 (FV 40) to 3.2 (FV 80). Replaces flat `RP_WAR_CAP`. `prospect_value.peak_war()` selects RP table when `bucket=="RP"`.
- **Fix 2**: Smooth market value ramp in `prospect_value._market_value()` — linear interpolation from league minimum at 0 WAR to full `war × $/WAR` at 1.0 WAR. Replaces binary cliff at `REPLACEMENT_WAR=1.0`.
- **Fix 3**: Removed `RP_WAR_CAP` from `contract_value.py`, `projections.py`, `fv_calc.py`, `player_utils.py`. RP WAR for MLB players was already handled by `OVR_TO_WAR` table's RP column.
- **Result**: FV 50 RP $37M, FV 60 $63M, FV 70 $83M (was all $63M). FV 40 now $16M (was $0). FV 45 stable at $25M.
- Removed `RP_WAR_CAP` and `REPLACEMENT_WAR` constants.

### Young Player Ratings Blend
- `contract_value.py` previously used `stat_peak_war` as the sole WAR projection when stats were available, ignoring ratings entirely. For young players whose Ovr-based WAR significantly exceeds their stat WAR (e.g. Chris Brown: Ovr 75 → 2.0 WAR ratings, 1.28 WAR stats), this undervalued them and triggered premature non-tender via the arb salary gate.
- **Fix**: When `ratings_war > stat_war` and the player is below peak age (27 pitchers, 28 hitters), blend the two projections. Ratings weight fades linearly from 50% at age 21 to 0% at peak age. Only applies upward (ratings > stats) — stat underperformance relative to ratings is treated as unrealized potential, not the other way around.
- Chris Brown: $9.3M → $16.8M (2yr → 3yr control, WAR 1.28 → 1.64).

### RP Arb Salary Model
- The generic arb salary model (exponential in Ovr with 0.80x RP discount) dramatically overprojected RP arb salaries — e.g. Ovr 61 RP 3rd arb: model $12M vs game $3.8M. This triggered premature non-tender, cutting control years short.
- **Fix**: RP-specific arb model calibrated from 35 actual RP arb contracts. Uses separate exponential (`566K × e^(0.0294 × Ovr)`) with 25% annual raises instead of the generic additive raise model. Non-RP arb model unchanged.
- Gaytan (Ovr 61): $22.3M → $35.2M (4yr → 5yr control, 3rd arb $4.3M vs game $3.8M).

---

## Session 29 (2026-03-23)

### Task List Cleanup
- Pruned `task_list.md` to open items only. Removed all completed multi-league tasks (Layers 1–5, hardening 5.1–5.6), all resolved bugs (B.1–B.7), and all shipped web UI features (roster rework, depth chart, league overview, prospects tab, stat leaders, player popup, prospect side panel, two-way player support, ETA gap fix). Removed stale "remaining: 5.5, 5.6" note from long-term multi-league entry. All completed items were already recorded in the changelog from their respective sessions.
- Confirmed depth chart SVG alignment is resolved — card positions and SVG coordinates are well-tuned. Removed from task list.

### Organization Tab
- New **Organization** tab on team page — cross-level org summary in one view.
- **Position Depth table** — rows for C/1B/2B/3B/SS/LF/CF/RF plus SP 1-5 and RP top 3. Each row shows: league rank (color-coded pill on first row per position group), MLB player (name, age, color-coded Ovr, WAR, surplus), and top prospect (pos, name, age, FV badge, level badge, surplus). One prospect per position; SP shows 5, RP shows 3. OF prospects labeled with specific field position (LF/CF/RF) instead of generic OF. Prospects deduplicated across positions (each player appears once). SP/RP sorted by Ovr; prospects sorted by FV then surplus. SP/RP section separated by top border. Level badges reuse league prospects tab styling (AAA blue, AA green, A yellow, lower gray). Reuses `_league_pos_rankings()` from depth chart for rank data.
- **Surplus Leaders** — top 20 combined MLB + Farm players sorted by surplus. Level badges (MLB/AAA/AA/A/etc.) using existing `lvl-badge` styles plus new `lvl-mlb` class.
- **Retention Priorities** — players with ≤2 years of team control and positive surplus. Multi-year contracts use contract years remaining; 1-year contracts use `_estimate_control()` for arb/pre-arb estimation. Shows control years remaining. Positioned alongside position depth in a two-column grid layout.
- **Committed Payroll** — 4-year horizon bar chart showing total committed dollars per year. Reuses `get_payroll_summary()`.
- **Layout**: top row is position depth (wide) + retention priorities (narrow) side by side. Bottom row is surplus leaders + payroll bars in two columns.
- New `get_org_overview(team_id)` query function in `team_queries.py`. Re-exported via `queries.py`.
- **Ovr tier coloring consolidated** — moved duplicated Ovr/Pot tier coloring JS from `player.html` and `team.html` into `base.html`. Now runs globally on all pages. Single source of truth for the color palette.
- CSS: `.org-top` grid, `.org-depth-panel`/`.org-retention-panel`, `.org-depth-table`, `.org-group-top` separator, `.lvl-mlb` badge, `.payroll-bars`/`.payroll-bar-*` bar chart styles. Removed unused `.src-badge` styles.

---

## Session 28 (2026-03-22)

### Multi-League — Layer 5 Hardening + Onboarding Polish

**Layer 5 — Hardening (Tasks 5.1–5.3)**
- `before_request` checks for `league.db` + `league_averages.json`; data routes redirect to `/settings` if missing. When no leagues exist at all, redirects to `/onboard`.
- Settings page shows orange warning banner for incomplete leagues.
- All `league_averages.json` reads replaced with safe `web_league_context.league_averages()` loader (returns zeros when missing) — `app.py`, `player_queries.py`, `team_queries.py`, `percentiles.py`.
- Record count query guarded against missing DB.
- `/api/test-connection` endpoint — tests both public (`/date/`) and protected (`/ratings/`) endpoints. Catches `CookieExpiredError` specifically.
- `statsplus/client.py` — new `CookieExpiredError` exception. `_fetch()` detects "requires user to be logged in" response (StatsPlus returns 200, not 401).
- Refresh error handler detects cookie expiration in stderr and surfaces clear message.
- Post-refresh validation checks table counts (players ≥100, ratings ≥100, teams ≥10, contracts ≥50).

**Settings page improvements**
- DH rule: free text input replaced with constrained dropdown (No DH / Universal DH / AL Only DH) + server-side validation.
- Cookie fields split into separate Session ID and CSRF Token inputs (both settings and onboarding).
- Collapsible "Where do I find these?" instructions with DevTools walkthrough.
- Project root added to `sys.path` in `app.py` so `from statsplus import client` works when Flask runs from `web/`.

**Onboarding wizard improvements**
- Step 2 (Pull Data) is now async: background thread with `Popen` captures stdout stage markers. JS polls `/onboard/refresh-status` every 1.5s. Spinner, progress bar, stage text ("── teams", "── ratings", etc.). Error state shows retry + back buttons.
- Back buttons on steps 2 and 3.
- Step 3 loads team names from API (city + nickname) instead of empty settings file. Filters to MLB teams via `players.level = '1'`.
- Step 3 POST populates `team_names` and `team_abbr` in settings from API data.
- `fv_calc` deferred from refresh to step 3 save (`--no-fv` flag added to `refresh.py`).

**Refresh button staleness indicator**
- `/api/game-date` endpoint returns local and remote game dates.
- JS on every page load checks staleness and shows badge: green ✓ (up to date), yellow ! (stale), gray ? (API unreachable).
- Badge re-checks after refresh completes.

**Bug fixes**
- `db.py` `_resolve_db_path()` — when `league_dir` is explicitly passed, always use it (no legacy fallback). Fixed new leagues writing to `emlb.db` instead of their own `league.db`.
- `refresh.py` — intermediate commit before ratings so roster/stats data survives if ratings fails (cookie expiration).
- `league_config.py` — `_load()` handles missing `league_settings.json` and `state.json` (returns empty dicts). `pos_map`, `role_map`, `level_map` use `.get()` with empty dict fallback.
- `contract_value.py` — `_get_state()` uses `league_context.get_league_dir()` instead of hardcoded `BASE / "meta"` path.
- `player_utils.py` — `dollars_per_war()` uses `league_context.get_league_dir()` instead of hardcoded path.
- `projections.py` — `_int_or()` helper coerces non-numeric rating values (from misaligned CSV) instead of crashing.
- `league.html` — handles missing `dollar_per_war` in league averages (shows "—").
- Removed dangling symlinks at project root (`emlb.db`, `config/`, `history/`, `reports/`, `tmp/`).

---

## Session 27 (2026-03-22)

### Multi-League Implementation — Layers 1-4

Implemented the multi-league spec (`docs/multi_league_spec.md`) across 4 layers, 18 tasks.

**Layer 1 — Data Layer Refactor (Tasks 1.1–1.5)**
- Created `data/emlb/` directory structure with migration script (`scripts/migrate_to_multi_league.py`). Symlinks at old locations for backward compat.
- `data/app_config.json` — global config with `active_league` and `statsplus_cookie`.
- `scripts/league_context.py` — shared resolver for active league directory, cookie, slug.
- `scripts/db.py` — dynamic DB path from `get_league_dir()`, falls back to legacy `emlb.db`.
- `scripts/league_config.py` — accepts `base_dir` parameter, resolves paths dynamically. Added `leagues` property, `league_for_team()`, `state_path`, `league_dir` properties.
- `statsplus/client.py` — lazy credential resolution (no module-level env reads). `configure()` > `league_context` > `.env` fallback.
- `web/web_league_context.py` — request-scoped accessors (`get_db()`, `get_cfg()`, `team_abbr_map()`, etc.).
- `web/app.py` — `@app.before_request` populates Flask `g` with league config. Context processor for template globals.
- `web/queries.py`, `web/team_queries.py`, `web/player_queries.py`, `web/percentiles.py` — all module-level globals (`_cfg`, `_db`, `TEAM_ABBR`, `TEAM_NAMES`, `LEVEL_MAP`, `POS_MAP`, etc.) replaced with `web_league_context` accessors. `conn.close()` calls removed from `queries.py` (shared connection lifecycle). Each `get_db()` call creates a fresh connection scoped to the active league.

**Layer 2 — League Structure Generalization (Tasks 2.1–2.4)**
- `league_settings.json`: Added `leagues` array with explicit league objects (name, short, color, divisions). Old `divisions` dict kept for backward compat.
- `league_config.py`: `leagues` property synthesizes from old format if `leagues` key missing.
- `app.py` league route: Builds `league_groups` from `config.leagues`. Wild card computation per-league.
- `queries.py`: `_build_league_team_sets()` returns `{lg_short: set(tids)}`. Leader functions return `{"All": ..., "AL": ..., "NL": ...}`.
- `league.html`: Division cards use inline `border-top` color. Leader toggle buttons generated dynamically. Grid columns from division count.
- `style.css`: Removed `div-al`/`div-nl` classes.

**Layer 3 — Settings & Onboarding UI (Tasks 3.1–3.5)**
- `settings.html`: Full rebuild — 6 sections (My Team, League Identity, League Structure with JSON editor, Financial, Connection, Data) + "Add Another League" link.
- `app.py`: Expanded settings route with `save_identity`, `save_financial`, `save_cookie`, `save_structure` POST actions. Structure editor validates JSON and rebuilds flat `divisions`.
- `onboard.html` + routes: 4-step wizard (Connect → Pull Data → Configure → Done). Creates league directory, runs refresh, configures team.
- `base.html`: Dynamic `<title>` and `<h1>` from `league_name`. League switcher dropdown (hidden for single league).
- `/switch-league/<slug>` route updates `app_config.json`.
- Backlog item 3.6 added for settings page hardening (data safety, validation, visual editor, etc.).

**Layer 4 — Refresh Pipeline Updates (Tasks 4.1–4.2)**
- `refresh.py`: All paths resolve through `get_league_dir()`. Year defaults to `config.year`. `ORG_ID` resolved dynamically.
- `fv_calc.py`: DB and state path from `get_league_dir()`.
- `farm_analysis.py`: All data paths (prospects, state, scaffold output, tmp) from `get_league_dir()`.
- `roster_analysis.py`: Notes, league averages, scaffold output, tmp from `get_league_dir()`.

---

## Session 26 (2026-03-22)

### ETA Gap Fix
- **Root cause**: `_ETA` map in `queries.py` used `.5` values with Python's `round()` (banker's rounding). `round(0.5)=0` collapsed AAA to 2033 (same as MLB), `round(1.5)=2` pushed AA to 2035, skipping 2034 entirely.
- **Fix**: Replaced with integer values (AAA=1, AA=2, A=3, A-Short=4, USL/DSL/Intl=5) across all 3 occurrences. Removed `round()` calls.
- **ETA pull-forward**: Added `_calc_eta()` helper — prospects with Ovr ≥ 45 (MLB-viable contributor) get ETA pulled forward by 1 year. A AAA prospect who can contribute today shows 2033, not 2034.
- **Result**: Clean distribution — 2033 (10 MLB-ready AAA), 2034 (32), 2035 (13), 2036 (41), 2037 (6).
- `constants.py` `YEARS_TO_MLB` unchanged — `.5` values are correct for NPV discounting in surplus model.

### Multi-League Support Spec
- **`docs/multi_league_spec.md`** — comprehensive spec for transforming the app from single-league to multi-league. Covers:
  - Full audit of hardcoded assumptions (§1) — 25+ items across league identity, structure, team/org identity, financial model, ratings, API, file layout
  - League structure generalization (§2) — `leagues` array model replacing AL/NL hardcoding
  - Data isolation (§3) — `data/<league>/` directory structure (separate DB per league)
  - Onboarding flow (§4) — 6-step browser wizard
  - Settings page expansion (§5) — 6 sections covering full configuration surface
  - Code changes required (§6) — file-by-file breakdown
  - Migration path (§7) — existing EMLB data migration
  - Decisions log (§9) — 6 architectural decisions with rationale
  - Implementation plan (§10) — 5 layers, 18 tasks, ordered by dependency

### Architectural Decisions
- D1: Request-scoped league context (not singleton reload) — scales correctly
- D2: Full `leagues` array model (not naming convention) — explicit over inferred
- D3: UI onboarding wizard (not CLI) — target users are OOTP players
- D4: Multi-league directory structure from day one — avoid double migration
- D5: Full settings page expansion — build real config surface once
- D6: StatsPlus cookie is global, only slug is per-league

---

## Session 24 (2026-03-21/22)

### UI Visual Overhaul — Team Page
- **KPI cards**: Summary bar items restyled as individual cards with borders, lighter background (#1e2530), green left accent on surplus cards, conditional pos/neg coloring. Streak card with win/loss accent border+tint.
- **Rank pills**: 5-tier colored pill badges (elite/good/mid/poor/bad) replacing plain text rank. Blue/orange/red palette for color-deficiency accessibility. Contextual progress bars behind pills — bar width proportional to rank, pill rides at the end.
- **Recent games**: W/L solid color square badges in own column, bold scores, dimmed pitcher names, muted vs/@ indicator. Abbreviated player names via `|short` Jinja filter (handles Jr/Sr/II/III suffixes).
- **Leaders section**: Vertical card layout with category label left + players right. #1 leader bold with gold value. All values soft blue (#7ec8ff). Abbreviated names.
- **Two-column layout**: Main tab restructured into independent left (Standings, Record, Recent Games) and right (Team Stats, Leaders) columns — eliminates dead space gap.
- **Standings highlight**: Team row gets green left border via `td:first-child` border + background tint.
- **Section dividers**: Panel h2 headings get bottom border.
- **Zebra striping**: `tr:nth-child(even)` on all tables.
- **Active tab**: Underline changed from red to green.

### UI Visual Overhaul — Depth Chart
- **SVG cleanup**: Removed filled outfield wedge and thick infield diamond. Replaced with faint arc, subtle dirt circle, very faint basepaths.
- **Control bar**: Year tabs + stat selectors grouped in structured panel bar (years left, stats right).
- **Header contrast**: Forced white text on all colored position headers (elite/good/weak).
- **Sidebar**: DH/SP/RP wrapped in distinct container with dark background and left border.
- **Player grid alignment**: CSS grid (`1fr auto auto auto`) for consistent column alignment. Name+level tag wrapped in single grid cell.
- **Card sizing**: Fixed 210px width, edge cards repositioned to prevent overlap/clipping.
- **Heatmap legend**: Three colored swatches (Elite/Above Avg/Below Avg) in control bar.
- **Departed banner**: Lightened text to soft pink, chips get border for readability.
- **Level badges**: Yellow border + padding upgrade to proper status badge.

### UI Visual Overhaul — League Prospects Page
- **FV badges**: Color-coded pills — gold (65+), blue (55-64), green (50-54), gray (<50).
- **Level badges**: Colored pills by level — AAA blue, AA green, A yellow, lower gray.
- **ETA highlighting**: Current year bold white, 2035+ muted gray.
- **Surplus data bars**: Inline flex track bars scaled relative to #1 prospect.
- **Filter bar**: Search + team filter consolidated into mode tabs bar (pushed right).
- **Compact rows**: Reduced padding for more visible prospects without scrolling.
- **Height column**: Right-aligned with tabular nums.

### Infrastructure
- **`|short` Jinja filter** (`app.py`): `_short_name()` handles Jr/Sr/II/III/IV suffixes. Used on team page (recent games, leaders).
- **Global CSS fix**: `select { width: 100% }` scoped to `form select` only — was breaking all non-form selects site-wide.
- **Duplicate CSS cleanup**: Removed duplicate `.split-btn`, `.prospect-mode-select`, `.prospect-filters` rules.

### UI Visual Overhaul — League Overview Page
- **League vitals KPI cards**: Added Phase, Lg AVG, Lg ERA, Lg OPS, $/WAR cards to summary bar. KPI divider (`<hr>`) separates header from content.
- **Standings 2×3 division grid**: Replaced vertical stack of full tables with compact division cards in a 3-column grid. AL cards get blue top border, NL cards get red. Division leaders get gold "1st" badge, wild card teams get blue "WC" badge. Fixed-width badges + spacer for name alignment. PCT progress bars behind win percentage. Teams 10+ GB get muted opacity. Responsive: collapses to auto-fit below 1100px.
- **Wild card logic**: Computed from standings using `wild_cards_per_league` setting (3). Ties for last WC spot both marked.
- **Two-column layout**: Left column (standings grid + scrollable power rankings), right column (leaders starting at top). `league-main` grid.
- **Power rankings**: Fixed 480px height with internal scroll. Score column has heatmap bar (green, scaled to #1). User team highlighted with green left border.
- **Leader hero cards**: #1 leader featured with bold name + large gold value. 2-5 listed below with muted gold values. 2-column grid layout. Team abbreviations muted to `#8b949e`. User team players get green highlight (border + tint + green value).
- **Batting leaders**: Removed "R" (runs) category — now 6 categories (AVG, HR, RBI, SB, OPS, WAR) matching 6 pitching categories.
- **Prospect tab**: Mode tabs + filters moved inside `prospect-table-wrap` so right edge aligns with table.
- **ETA styling**: Current year bold white (`eta-now`), 2035+ muted gray (`eta-far`).

### Beat Reporter Agent — T.R. Falcone
- **Agent definition**: `.kiro/steering/beat-reporter.md` — standalone steering file with project context, persona, tone/style rules, 8 article type templates, research process, output format (short for Discord, long for Google Docs), and guardrails.
- **Agent config**: `~/.kiro/agents/beat-reporter.json` — registered as Kiro CLI agent with resources pointing to steering file, tools reference, and key project files. Accessible via `/agent swap` or `kiro-cli chat --agent beat-reporter`.
- **Tools reference**: `docs/tools_reference.md` — comprehensive catalog of all CLI tools (8), importable libraries (5), web query functions (20+), data files, DB tables, and known data limitations. Added to end-of-session documentation checklist.
- **Reporter identity**: T.R. Falcone, analytical tone by default (The Athletic style), user-overridable. No OOTP field names in prose — scout language only.

---

## Session 23 (2026-03-21)

### Two-Way Player Support
- **Detection**: `load_stat_history()` already returned a `two_way` set (players with qualifying batting AB≥130 and pitching GS≥10 in the same year). Now consumed by all callers.
- **Surplus fix**: New `_two_way_peak_war()` combines batting + pitching WAR per year (no incomplete adjustment). `stat_peak_war()` gains `two_way` kwarg, flows through `contract_value()` and `fv_calc.py`. Cowgill surplus: $170.8M → $210.1M (+$39.3M).
- **Roster hitters tab**: Two-way pitchers (PA≥30) now appear on the Hitters tab with their fielding position (e.g. Cowgill shows as 1B). `is_two_way` flag and "TW" badge on both tabs.
- **Player page — Pitcher/Hitter toggle**: Two-way players get a "Pitcher | Hitter" button pair in the header. Clicking swaps the entire page view between pitcher mode (pitcher ratings, pitching stats snapshot, pitching percentiles) and hitter mode (hitter ratings with L/R splits + defense + running, batting stats snapshot, batting percentiles). Reuses existing pitcher/hitter macros — no TW-specific template sections. Stats tab always shows both batting and pitching history tables regardless of toggle.
- **Player popup**: Two-way pitchers show batting tools (Con/Pow/Eye/Spd) below pitching tools, plus a batting stat line (slash line + HR + bWAR) below the pitching stat line.
- **Backend**: `get_player()` returns `hit_ratings` dict (full hitter ratings structure) for two-way pitchers, plus `bat_percentiles`/`bat_pctile_splits`. `get_player_popup()` returns `bat_stats` and `ratings.bat` for two-way pitchers.
- **CSS**: `.tw-badge`, `.tw-toggle`, `.tw-btn` styles. `tw-pit`/`tw-hit` CSS classes for view toggling.
- 12 two-way players detected league-wide; 7 with meaningful current-year playing time.

---

## Session 22 (2026-03-21)

### Data Integrity — Intl Complex Level Fix
- **Root cause**: API reports international complex players as `Level=1` (MLB). Only distinguishable by negative `league_id` in ratings. `fv_calc.py` treated them as MLB players → inflated surplus.
- **Fix at source** (`refresh.py`): After ratings ingest, reclassify any player with negative `League` from `level=1` to `level=8` (International) in the `players` table. 1,321 players reclassified.
- **Simplified downstream**: `fv_calc.py` no longer needs `is_intl_complex` special-casing — level=8 flows through normal prospect path via `LEVEL_INT_KEY`. `farm_analysis.py` old workaround (query level=1 then filter by league_id) replaced with standard `get_ratings(org, level=8)`.
- Deleted stale eval_date rows from `player_surplus` (1,844 rows).

### Roster Rework — Hitters & Pitchers Tabs
- Replaced single "Roster" tab with separate **Hitters** and **Pitchers** tabs.
- **Split toggle** (Overall / vs L / vs R) — all 3 splits loaded as JSON, JS swaps displayed values instantly. Split label ("Showing: vs LHP") appears when viewing a split.
- **Hitters columns**: Pos, Name, Age, Ovr | WAR | G, PA, AVG, OBP, SLG, OPS | HR, R, RBI, SB, CS | BB%, K% | Surplus
- **Pitchers columns**: Role, Name, Age, Ovr | WAR | IP, ERA, WHIP | K, BB, K%, BB%, K-BB% | HR, W-L, QS, SV+H, IRS% | Surplus
- Players with missing split data (e.g. no AB vs RHP) still shown with dashes instead of hidden.
- **Column header tooltips** — every stat header has a `title` attribute explaining the abbreviation on hover.
- **Conditional formatting** — rate stats (AVG, OBP, SLG, OPS, BB%, K%, ERA, K%, BB%) colored green/red when >5% above/below league average.
- **Column separators** — subtle left borders between logical stat groups for visual tracking.

### Player Hover Popup
- Hover any player name link for 300ms → tooltip appears with key data.
- **Content**: Name, age, height, bats/throws, position, team, level, Ovr/Pot, FV (prospects), stats (slash line or ERA/IP/K), surplus.
- **Ratings snapshot**: Hitters show Con/Pow/Eye (present/future) + Spd. Pitchers show Stf/Mov/Ctl (present/future) + Stm + Vel + top 4 pitches with grades.
- **Grade coloring**: blue (70+), green (60+), white (50+), orange (40+), red (<40).
- AJAX endpoint `/api/player-popup/<pid>` with client-side caching. Works on all pages (base.html).
- Added `player-link` class to depth chart links that were missing it.

---

## Session 21 (2026-03-21)

### Depth Chart Visual Improvements
- **Inline stats** — position player rows now show OPS+ (or selected stat) prominently with PT% always visible but dimmed; pitchers show ERA by default. Stat selectors default to OPS+ and ERA instead of PT%.
- **Age added** to all player rows (dimmed, after name). Ages increment correctly across projection years.
- **Legend row** — dynamic legend above diamond showing "Player Age [Stat] PT%", updates when stat selector changes.
- **Year tabs** — replaced ◀/▶ arrows with three clickable year buttons (2033/2034/2035) with active underline.
- **Backup trimming** — cards show players until 95% cumulative PT coverage instead of hard cutoff, reducing noise while preserving meaningful depth (e.g. DH rotation).
- **RP role hierarchy** — CL/SU/MR labels now have tiered prominence (CL: larger/bolder, SU: medium, MR: subtle).
- **Departed banner** — reformatted as individual chips sorted by WAR descending, with bold position labels. Much more scannable than the old dot-separated text.
- **DH placement** — moved from diamond to right sidebar above SP/RP, styled as matching section card.

### WAR Projection Model Fix — Stat/Ratings Blending
- **Problem**: Players outperforming their ratings (e.g. Rohnson: 5.2 stat WAR vs 2.6 ratings WAR) had a cliff in projections — year 1 used actual stats, year 2+ dropped to ratings-only.
- **Fix in `projections.py`**: `project_war()` now blends stat_war into future years with 50% exponential decay per year. Year 1 = stat_war, Year 2 = 50/50 blend, Year 3 = 25/75, converging to ratings-only.
- **Fix in `contract_value.py`**: Same blending applied to the `dev_ramp` branch of the surplus model, which had the same cliff for pre-peak players.
- **ERA/FIP alignment**: `project_era()` and `project_fip()` now delegate to `project_war()` instead of computing WAR independently from ratings, ensuring ERA/FIP track with the blended WAR projection.
- Rohnson's curve: 5.2 → 4.0 → 3.4 (was 5.2 → 2.8 → 2.8). ERA: 2.84 → 3.32 → 3.57 (was 3.88 flat).

### Data Integrity Fix — player_surplus Table
- **Problem**: `fv_calc.py` was writing international complex players (negative `league_id`, level=1) into `player_surplus` as MLB players. Inflated team surplus totals with cheap prospects not on the active roster. Sacramento showed $1164M surplus with 67 "MLB" players.
- **Fix**: Added `league_id > 0` guard to the MLB branch in `fv_calc.py`.
- **Cleanup**: Deleted 3,949 bad rows (intl complex) and 1,844 stale eval_date rows. All teams now show 26-28 players (actual MLB rosters).

---

## Session 20 (2026-03-21)

### Depth Chart Fixes
- **Level display bug** — `allocate_playing_time()` was stripping `_level` field; changed filter to only strip `_eff_war`
- **Premium position lock** — lowered WAR threshold from 5.0 to 3.0 for SS/CF/C; increased inertia boost to 4x. Kazansky stays locked at SS all 3 years.

### League Page — Stat Leaders Overhaul
- Replaced batting/pitching leader tables with per-stat panel cards (top 5 per category)
- Added MLB/AL/NL toggle — single query, client-side filtering
- Batting: AVG, HR, RBI, R, SB, OPS, WAR. Pitching: ERA, W, K, SV, WHIP, WAR

### League Page — Prospects Tab
- New tab bar on league page (Overview | Prospects)
- **Top 100** — default view with player search and All Teams filter dropdown; preserves original rank when filtering
- **Top 30 by Team** — dropdown-as-tab pattern, defaults to user's team, shows full team names
- **Top 10 by Position** — dropdown-as-tab, merged OF bucket (CF/LF/RF/COF), ordered C/1B/2B/3B/SS/OF/SP/RP
- Level dots indicator (5-dot scale: Rookie=1, A=2, AA=3, AAA=4, MLB=5)
- FV color coding (65+ blue, 55+ green, 50 white, 45 dim)
- Conditional columns — hides Position on position view, hides Team on team view
- Table capped at 960px max-width

---

## Session 19 (2026-03-20)

### Finances Tab
- **Finances tab on team page** — new tab with committed payroll table showing 6-year horizon (current year + 5 future). Per-player salary by year with TO/PO option markers, NTC badges, and total committed row.
- **Arb/pre-arb salary projections** — 1-year contract players get projected future salaries using the existing `contract_value` arb model. Projected cells shown in italics with `est` superscript. Pre-arb years at league minimum, arb years using OOTP-calibrated exponential + raise model with RP discount.

### Surplus Model Fixes
- **Service year threshold fix** — lowered qualifying thresholds from 300 AB / 100 IP to 100 AB / 40 IP to correctly count relievers and part-time players. Fixes Grimaldo (was missing 4 of 5 qualifying seasons) and other relievers.
- **Pre-arb age gate fix** — changed from blanket `age >= 28` rejection to `age >= 28 AND svc >= 4`. Fixes McClanahan (age 28, 1 qualifying season due to injuries) who was incorrectly treated as a veteran FA.
- **RP bucketing for MLB players** — `assign_bucket` now respects actual deployment role when `use_pot=False`. A reliever is valued as RP regardless of SP-viable ratings. Fixes Franklin ($11.4M → $5.4M surplus) and all other misclassified relievers league-wide.
- **Non-tender gate fix** — compare arb salary against `max(market_value, min_salary)` instead of requiring `market_value > 0`. Zero-WAR players now correctly get non-tendered at arb entry.
- **Removed pw > 0 control gate** — the non-tender gate handles this more precisely. Players with estimated control now always get the full projection.
- **Development ramp for pre-peak players** — `contract_value` now linearly interpolates Ovr toward Pot for players below peak age (27 pitchers, 28 hitters). Edwards went from -$0.8M (1yr, 0 WAR) to $13.7M (6yr, ramping WAR). Only applies when Pot > Ovr and no stat-based WAR override.
- **Re-ran fv_calc.py** — all 918 MLB players recomputed with the above fixes.

### Record Breakdown
- **Record breakdown panel on team page** — Main tab panel showing Overall, Home, Away, vs Division, 1-Run Games, Last 10, and Streak with W-L-Pct for each split.

### BABIP Investigation & Fix
- **BABIP rating investigation** — confirmed hidden BABIP rating is not in the API export. Regression analysis: Contact (r=0.41) and Speed (r=0.21) explain 23% of BABIP variance (R²=0.227). Residuals are stable across years (even/odd correlation r=0.46), confirming a persistent hidden trait.
- **Improved BABIP expected percentile** — replaced contact-only expected BABIP with regression model (cntct + speed) plus historical residual adjustment (avg actual-vs-predicted over 2+ prior qualifying seasons). Players with consistently high/low BABIP no longer falsely flagged as lucky/unlucky.

### Task List Updates
- Payroll summary marked complete, comparable teams dropped, roster rework added (Hitters/Pitchers/Organization tabs), similar players/prospect comps added, BABIP investigation resolved, stat/ratings divergence flag moved to long-term.

## Session 18 (2026-03-20)

- **Stats snapshot bug fix** — `bat_splits`/`pit_splits` changed from single dicts to arrays of rows (for year-by-year splits), but the Overview tab snapshot macro still treated them as single dicts. Fixed by extracting `[-1]` (latest year) from each split array.
- **SV/HLD zero display fix** — Jinja `or` treats `0` as falsy, showing `-` instead of `0`. Removed `or "-"` pattern.
- **Game history API fix** — `get_game_history()` returned empty without `year` param. Added `year` parameter to client. Endpoint has ~4 min rate limit.
- **Games table** — new `games` DB table storing game results (game_id, home/away teams, runs, WP/LP/SV pitchers). 23,694 games loaded (2024-2033). Added to `refresh.py` pipeline.
- **API field mapping: runs0=away, runs1=home** — discovered and documented that the StatsPlus game history API uses `runs0` for away team runs and `runs1` for home team runs (opposite of typical convention).
- **Actual W/L standings** — standings now use real win/loss records from game history instead of pythagorean estimates. Pythagorean W shown as supplementary column with Δ (delta) indicating over/underperformance. Falls back to pythagorean-only if games table is empty.
- **Division-grouped standings** — league page standings broken into 6 division tables under American League / National League headers, with per-division GB.
- **Power rankings** — composite ranking on league page. Score weights: pythagorean W% (50%), last-10 record (25%), run diff/game (25%). Surplus removed from score (display-only). Includes L10 record, streak (color-coded W/L), RD/G, MLB$/Farm$ columns.
- **Recent games on team page** — last 10 games on Main tab with date, vs/@, opponent (linked), W/L result (color-coded), WP/LP/SV pitcher names (linked to player pages) with running season records as of that game date.
- **Task list updates** — power rankings marked complete, game history items partially addressed.

## Session 17 (2026-03-20)

- **Ovr/Pot color box bug fix** — bare `[data-g]` CSS selectors applied background color to `.ovr-color` text spans, creating colored boxes instead of colored text. Scoped selectors to `.grade-cur[data-g]` and `.grade-pot[data-g]`.
- **Team stats leaders** — top 3 players in key batting (HR, RBI, AVG, OPS, SB, WAR) and pitching (ERA, W, SV, K, WHIP, WAR) categories on team Main tab. Stats are per-team so traded players retain their stats. Card grid layout with gold highlight on #1.
- **MLB qualification thresholds** — rate stat leaders (AVG, OPS, ERA, WHIP) use MLB standard qualifiers: 3.1 PA/team game for batters, 1.0 IP/team game for pitchers. Scales automatically with season progress.
- **Saves field fix** — API uses `s` for saves, not `sv`. Fixed in `refresh.py`, re-pulled all pitching stats.
- **Full stats schema expansion** — `batting_stats` expanded from 22→32 columns, `pitching_stats` from 21→52 columns. Now stores every API field including: er, cg, sho, hld, bs, svo, qs, gb, fb, pi, wp, bk, ir, irs, wpa, li, relief_app, md, sd (batting: g, gs, cs, gdp, ibb, pitches_seen, ubr, wpa). Batting avg/obp/slg now computed in upsert.
- **Full stats backfill** — pulled all batting, pitching, and fielding stats for 2020-2033 (14 seasons). 27,223 batting rows, 25,154 pitching rows, 24,529 fielding rows.
- **New batting stats displayed** — added G, ISO (SLG-AVG), SB/CS to player page batting tables.
- **New pitching stats displayed** — replaced K/9 and BB/9 with K%, BB%, K-BB% (superior rate stats). Added GB%, G, HLD. Removed HR/9 from display (captured by FIP).
- **Stats snapshot on Overview tab** — compact current-year stats panel between scouting report and percentiles. Pitchers show pitching stats (not batting). L/R split toggle for current year.
- **Stats tab split selector** — replaced old L/R toggle (current year only) with 3-button selector (Overall / vs L / vs R) showing full year-by-year history for each split.
- **SV/HLD zero display fix** — `0` was showing as `-` due to Jinja `or` treating 0 as falsy.
- **New backlog items added** — PAP score (1-10 value efficiency), career outcome probability chart, UI overhaul exploration.

## Session 16 (2026-03-20)

- **StatsPlus external links** — player and team pages link to StatsPlus web profiles via ↗ icon. League slug read from `statsplus/.env`, exposed as `statsplus_base` Jinja global.
- **Team navigation dropdown** — hover dropdown in nav bar with all 34 teams, accessible from any page. `all_teams` Jinja global. Pure CSS hover, no JS.
- **Fielding stats pipeline** — new `fielding_stats` DB table, `_upsert_fielding()` in `refresh.py`, API pull added to `refresh_league()`. IP stored as decimal (API returns outs). 1245 rows for 2033.
- **Fielding stats on player page** — query in `player_queries.py`, full-width table with Year/Pos/G/IP/TC/A/E/DP/FPCT/ZR/Arm. Pos column left-aligned.
- **Fielding percentile rankings** — `get_fielding_percentiles()` in `percentiles.py`. Position-aware metrics: FPCT+ZR for all, +Arm for OF, +Framing for C. Qualifier: 1.0 IP per team game, floor 15.
- **Fielding expected percentiles** — ZR expected from rating composites (IF: IFR×0.7+IFE×0.3, OF: OFR, C: IFR×0.35+CArm×0.35+CBlk×0.30). Framing expected from CFrm×0.7+CBlk×0.3. FPCT and Arm have no expected (too noisy). Includes expected range band.
- **Player header cleanup** — removed duplicate Ovr display for MLB players (was showing both Ovr/Pot and "MLB Ovr"). Prospects still show FV since FV≠Ovr. Ovr/Pot values color-coded using tier palette via JS.
- **Player page tabbed layout** — MLB players get 3 tabs: Overview (ratings, character, scouting report, percentiles, fielding percentiles), Stats (batting/pitching/fielding), Contract (contract years, surplus projection). Prospects keep single-page layout (no tabs). Reusable Jinja macros for all content blocks.
- **Split-specific expected percentiles** — hitter splits now use `cntct_l/pow_l/eye_l/ks_l` for vs-L and `cntct_r/pow_r/eye_r/ks_r` for vs-R instead of overall ratings. Pitcher splits use `stf_l/mov_l/ctrl_l` and `stf_r/mov_r/ctrl_r`. Verified with Trey Sweeney (cntct: 98 vs L, 49 vs R → expected AVG pctile 100 vs L, 34 vs R).
- **Steering doc update** — references `docs/changelog.md`, documentation checklist updated for task list/changelog split.

## Session 15 (2026-03-20)

- **Ratings CSV header truncation fix** — StatsPlus API truncates the ratings CSV header at 500 chars, dropping the last 17 columns (PotCutt onward including personality, Acc, Ovr, Pot). `client.py` now detects truncation and appends known missing column names. Also renames `Overall`/`Potential` to `Ovr`/`Pot` for downstream compatibility.
- **Ratings column order fix** — `_upsert_ratings()` in `refresh.py` had personality/league/height fields ordered after fielding/splits, but the DB schema has them before. All data from league-wide refreshes was being written to wrong columns (e.g. IFR values in the `int_` column). Reordered `row()` to match DB schema. All ratings data re-pulled.
- **`calc_fv()` None composite guard** — `_pos_composite()` could return `None` for COF players with missing LF/RF grades, crashing the `comp >= 60` comparison. Fixed with `or 0` guard.
- **fv_calc error propagation** — `_run_fv_calc()` in `refresh.py` was swallowing failures (printed error but exited 0). Now writes to stderr and exits non-zero so the web UI reports it.
- **Refresh error display** — web UI was truncating raw tracebacks to 200 chars. Now extracts the last line (actual exception message) and shows it in red in the modal.
- **Rate limit retry** — `get_ratings()` rate limit handling upgraded from single retry to 3-attempt loop.
- **Dynamic percentile qualification** — replaced hardcoded `min_pa=50` / `min_ip=10` with pro-rated thresholds: 2.0 PA per team game (hitters), 0.5 IP per team game (pitchers), with floors of 30 PA / 5 IP. Team games estimated from `max(team PA) / 38`.
- **Expected range band** — percentile expected indicator expanded from a single line to a shaded range band. Width is ±12 percentile points at the qualifier threshold, narrowing as `sqrt(qualifier / sample_size)`. Capped at ±25.
- **Personality text-snapshot workaround** — `player_queries.py` queries the most recent snapshot with text personality values (`WHERE wrk_ethic IN ('VL',...)`) as a safety net against bad numeric data from prior refreshes. Harmless after the column order fix since new snapshots have correct text values.
- **Task list / changelog split** — moved 141 completed items from `task_list.md` to new `docs/changelog.md`. Task list trimmed from 243 to 55 lines (open items only). Updated STRUCTURE.md and steering doc.

## Session 14 (2026-03-20)

- **Centralized `norm()`, `height_str()`, `display_pos()`** — canonical versions in `scripts/player_utils.py`. `queries.py` imports from `player_utils`.
- **Extracted `web/percentiles.py`** (264 lines) — hitter/pitcher percentile functions, helpers, stat/tag constants.
- **Extracted `web/player_queries.py`** (329 lines) — `get_player()` with ratings, stats, splits, contract, surplus, personality, scouting summary.
- **Extracted `web/team_queries.py`** (494 lines) — 12 team-specific query functions.
- **`queries.py` reduction** — 1294 → 141 lines (89% reduction). State helpers + league queries + re-exports.
- **Fix 20-rating grade bar** — minimum 5% width so grade 20 is always visible.
- **Player personality traits** — Character panel showing Intelligence, Work Ethic, Greed, Loyalty, Leadership. Color-coded VL/L/N/H/VH text.
- **Percentile expected range indicator** — white vertical line on each percentile bar showing where ratings predict performance.
- **Overall performance indicator** — "▲ Over" / "≈ Expected" / "▼ Under" in player header bar.
- **Deprecated Angels-only refresh** — removed `refresh()`, `org_ids()`, `org_players()`. League refresh is now the only path.

## Session 13 (2026-03-19)

- **IP storage fix** — API returns truncated integer `ip`; now derived from `outs` field (`outs/3`). ERA computed from outs. `fmt_ip` Jinja filter for baseball display.
- **Single-command refresh** — `refresh.py` auto-fetches game date, updates `state.json`, runs `fv_calc.py`.
- **SQLite WAL mode** — concurrent reads during writes. Web UI stays browsable during refresh.
- **`league_config.py`** — single abstraction for league-specific settings. All scripts migrated from hardcoded values.
- **Refresh button** — trigger full data refresh from web UI with progress indicator and error modal.

## Session 12 (2026-03-19)

- **Tab layout** — team page reorganized into 4 tabs: Main, Roster, Contracts, Player Development.
- **Roster construction summary** — SP/RP/Pos counts in summary bar.
- **Upcoming free agents** — multi-year deals expiring within 2 years on team page.
- **Surplus leaderboard** — top 15 surplus players combining MLB and farm.
- **Age distribution** — MLB roster and farm age brackets with horizontal bars and league average markers.
- **Farm system depth** — FV 40+ prospects by position bucket and level with league rank.
- **Page title header** — `<h1>` with full team name.
- **Rank color fix** — `.rank-top` changed to `#66ff99` for readability.
- [-] **Trade asset inventory** — built then removed. Redundant with contracts table and surplus leaderboard.

## Session 11 (2026-03-19)

- **Team page base migration** — `/team/<id>` renders full dashboard for any team. Dashboard queries parameterized.
- **Team links** — all team names clickable across all views.
- **Team stats with league rankings** — batting/pitching stats with rank out of 34.
- **Contract table** — MLB contracts sorted by salary with surplus and option flags.
- **Payroll in summary bar** — total MLB payroll from `is_major=1` contracts.
- **Surplus moved to contracts** — removed from roster tables, added to contracts table.
- **Pos/Role column first** — moved before Name in all tables.
- **Column alignment** — CSS switched to semantic rules.

## Session 10 (2026-03-19)

- **Platoon split penalty in FV model** — prospects penalized for severe L/R splits. 28 affected league-wide.
- **Position-weighted defensive score** — `defensive_score()` with position-specific tool weights.
- **Unified scaled defensive bonus** — composite-driven + weighted-score system replacing flat bonuses.
- **Scaffold context lines** — GB%, defensive detail, L/R split flags in farm and roster scaffolds.
- **data.py expanded** — `get_ratings()` returns all defensive + split fields.
- **Farm table formatting** — dashboard farm table matches league prospects table format.
- **Smart rank renumbering** — `sort.js` re-numbers `#` column after sort with direction-aware logic.

## Session 9 (2026-03-18)

- **Player page layout improvements** — grade bars fill width, contract option badges inline, surplus alignment fixed.
- **Expanded ratings** — Gap power, Steal, GB%, Defense (Error, Turn DP, Range), position-aware arm rating.
- **L/R split ratings toggle** — overall vs split grades for batting and pitching attributes.
- **L/R split stats and percentiles** — split stats from API, split percentile pools with lower thresholds.
- **Unqualified player percentiles** — grey percentile bars with "(small sample)" label.

## Session 7–8 (2026-03-18)

- **Bug fixes** — intl complex filtered from roster, batting rate stats computed from counting stats, .000 display fix, COF→OF display, non-MLB filtered from top 100, Montreal Expos added.
- **Player detail page** — `/player/<id>` with header bar, grade bars, scouting report, contract, surplus projection.
- **Player links** — all player names clickable to detail page.
- **Surplus projection panel** — year-by-year breakdown for MLB and prospects.
- **Advanced stats** — OPS+, BABIP, BB%, SO%, FIP, SIERA, ERA+, K/9, BB/9, HR/9.
- **Stats column ordering** — grouped by concept.
- **Percentile rankings (Savant style)** — horizontal bars with color gradient.
- **Performance tags** — rating-to-stat divergence indicators.
- **Phase 1 Dashboard (My Team + League)** — Flask web app with standings, roster, farm, leaders, prospects.
- **Team-agnostic configuration** — `my_team_id` in `state.json`, `/settings` page.
- **Division mappings** — all 34 teams mapped.
- **Client-side table sorting** — numeric, string, positional sort types.

## Session 6 (2026-03-18)

- **Standings script** — pythagorean W/L from team RS/RA.
- **Free agent analysis script** — expiring contracts with surplus data and filters.
- **Team stats in DB** — `team_batting_stats` and `team_pitching_stats` tables.
- **Trade target search workflow** — documented in `docs/trade_target_workflow.md`.
- **ERA fix** — computed from `er * 9 / ip`. Backfilled 2031-2033.
- **Height/bats/throws fix** — backfill UPDATE for demographics.
- **Aging docs updated** — exact calibrated values from `constants.py`.
- **STRUCTURE.md and system_overview.md rewritten**.
- **Deduplicated utility functions** — consolidated in `player_utils.py`.

## Session 5 (2026-03-18)

- **Farm report re-run** — Medina entered top 15, Posada rebucketed COF→C, Carrillo to watch list.
- **Fresh roster analysis** — 26 player assessments with contract health table.
- **Fresh org overview** — farm summary, MLB assessment, 1-3 year outlook.
- **Roster summary reuse** — `history/roster_notes.json` with rewrite flags.
- **Season-based refresh for both scaffolds** — new-season triggers rewrite flags.

## Session 4 (2026-03-18)

- **`fv_calc.py` integrated with `contract_value()`** — full control estimation for MLB surplus.
- **Circular import resolved** — `load_stat_history()` and `stat_peak_war()` moved to `player_utils.py`.
- **Batch performance optimization** — optional `_conn`/`_hist` params.
- **Pre-arb age gate** — age ≥ 28 on league minimum treated as 1yr FA.
- **WAR floor at 0** — negative WAR floored to prevent phantom negative surplus.
- **RP arb salary discount (0.80x)** — calibrated against OOTP data.
- **Non-tender gate** — control truncated when projected arb salary exceeds market value.

## Session 3 (2026-03-18)

- **`trade_calculator.py` fixed** — imports, FV+ display, sensitivity range, unified `net_surplus`.
- **Pre-arb/arb control estimation** — `_estimate_control()` validated 7/7 against game data.
- **ARB_PCT recalibrated** — 45/65/80% → 20/22/33% based on 86 OOTP arb players.
- **Arb salary model** — Ovr-based exponential/additive model. MAE $0.53M/yr.
- **DEVELOPMENT_DISCOUNT separated from time value** — bust-only realization rates.

## Session 2 (2026-03-18)

- **Prospect surplus model overhaul** — age-adjusted development discount, certainty multiplier, replacement WAR floor, zero floor, option value.
- **Farm systems ranking: surplus-based** — replaced point scoring with total surplus.
- **Prospect sort: surplus tiebreaker** — within same FV, sorted by surplus.
- **`prospect_query.py` enhancements** — `--sort` flag, `--n`, `--fv-min`.
- **FV model improvements** — critical tool floor penalty, level-adjusted development weight.

## Session 1 (2026-03-18)

- **Full league refresh** — all 102 teams.
- **`$/WAR` methodology fixed** — $6.28M → $8.62M.
- **`contract_value.py` rewritten** — fully team-agnostic.
- **Aging curves recalibrated** — consensus-based.
- **FV→WAR and OVR→WAR tables recalibrated**.
- **Valuation tables consolidated in `constants.py`**.

## Prior Sessions

- Farm report for 2033-04-25 — scaffold, summaries, published report.
- Prospect history + notes merged into `history/prospects.json`.
- Dev signal overhaul — stagnation/developing signals on 180-day baseline.
- Surplus value on farm cards.
- Height/Bats/Throws added to ratings DB.
- Bucket fallback to listed position.
- Intl complex loading fixed.
- Reports reorganized into year subdirectories.
- Prospect surplus model — `prospect_value.py` built and validated.
- Single source of truth for shared constants — `constants.py`.
- `roster_analysis.py` migrated to DB.

## Deferred

- **Marginal cost model for elite players** — flat $/WAR understates value of 5+ WAR players. Deferred until a trade involving a star player makes it necessary.
- **Transaction tracking** — no API endpoint for trades/DFAs/call-ups. Inferring from roster diffs unreliable. Shelved.
- **Starter game log** — requires box score data which the game history API does not have. Blocked.
