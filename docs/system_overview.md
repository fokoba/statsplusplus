# EMLB Analytics Platform — System Overview

## Purpose

League-wide analytics platform for managing the Anaheim Angels (org ID 44) in the EMLB
StatsPlus simulation. Supports farm system evaluation, MLB roster analysis, trade
target identification, standings tracking, and free agent analysis across all 34 MLB teams.

---

## Data Flow

```
StatsPlus API
     │
     ▼
refresh.py ─────────────────────────────────────────────────────────────────────┐
  Writes: players, teams, ratings, contracts, batting_stats, pitching_stats,    │
          fielding_stats, team_batting_stats, team_pitching_stats, games,       │
          league_averages.json, league_settings.json, state.json                │
  Pulls:  /players, /ratings, /contracts, /batting, /pitching, /fielding,       │
          /lgdata (league hierarchy + MiLB league IDs), /gamehistory            │
     │                                                                          │
     ▼                                                                          │
calibrate.py (pass 1) ──────────────────────────────────────────────────────── │
  Reads:  ratings, batting_stats, pitching_stats, fielding_stats                │
  Writes: config/tool_weights.json (component regression), model_weights.json   │
     │                                                                          │
     ▼                                                                          │
evaluation_engine.py ───────────────────────────────────────────────────────── │
  Reads:  ratings, players, batting_stats, pitching_stats, tool_weights.json    │
  Writes: ratings (composite_score, ceiling_score, tool_only_score,             │
          secondary_composite, offensive_grade, baserunning_value,              │
          defensive_value, durability_score, offensive_ceiling),                │
          ratings_history                                                       │
     │                                                                          │
     ▼                                                                          │
calibrate.py (pass 2) ──────────────────────────────────────────────────────── │
  Reads:  ratings (composite_score), batting_stats, pitching_stats              │
  Writes: model_weights.json (COMPOSITE_TO_WAR tables)                          │
     │                                                                          │
     ▼                                                                          │
fv_calc.py ─────────────────────────────────────────────────────────────────── │
  Reads:  players, ratings (composite_score/ceiling_score), batting_stats,      │
          pitching_stats, contracts                                             │
  Writes: prospect_fv, player_surplus                                           │
     │                                                                          │
     ├──► farm_analysis.py         (prospect_fv → tmp/farm_scaffold)            │
     ├──► roster_analysis.py       (ratings, stats, contracts → tmp/roster_scaffold)
     ├──► prospect_query.py        (prospect_fv, players, teams — read-only)    │
     ├──► contract_value.py        (players, ratings, contracts — read-only)    │
     ├──► prospect_value.py        (prospect_fv, players — read-only)           │
     ├──► trade_calculator.py      (contract_value + prospect_value)            │
     ├──► trade_targets.py         (players, contracts, contract_extensions, player_surplus) │
     ├──► trade_assets.py          (player_surplus, prospect_fv, contracts)     │
     ├──► team_needs.py            (players, ratings, batting_stats, pitching_stats) │
     ├──► draft_board.py           (prospect_fv, ratings, players + draft_settings.json) │
     ├──► standings.py             (pitching_stats, team_*_stats — read-only)   │
     └──► free_agents.py           (contracts, player_surplus — read-only)      │

                    ┌──────────────────────────────────────────────────────────┐
                    │  web/app.py (Flask)                                      │
                    │  Reads: all DB tables via queries.py (read-only)         │
                    │  Routes: /, /team/<id>, /league, /player/<id>, /settings, │
                    │          /offseason (phase-aware, manual toggle)          │
                    └──────────────────────────────────────────────────────────┘
```

**Dynamic pages:** `/offseason` is a phase-aware page (behind a manual toggle in
Settings → Dynamic Pages, stored as `offseason_mode` in `state.json`). A sub-phase
stepper (`offseason_phase` in `state.json`) gates which panels show via
`offseason_queries.panels_for_phase`. Panels — Arbitration (tender decisions,
perpetual-arb-aware, `$/WAR`-scaled thresholds), Free Agency market board
(unsigned + league-played FAs, need-flag via `get_draft_org_depth`, Proj WAR via
the shared `compute_player_value`), Extension candidates, and always-on Trades —
all reuse existing valuation data. Query module: `web/offseason_queries.py`.
Endpoints: `/api/toggle-offseason`, `/api/set-offseason-phase`.

**Rule:** `fv_calc.py` is the sole writer of `prospect_fv` and `player_surplus`.
All other analysis scripts are read-only against the DB.

---

## Scripts

| Script | Lines | Purpose |
|---|---|---|
| `refresh.py` | 280 | Pulls data from StatsPlus API into DB (all teams). |
| `fv_calc.py` | 142 | Computes FV for all prospects + surplus for all MLB players. Uses composite_score/ceiling_score when available, falls back to OVR/POT. ~8s runtime. |
| `evaluation_engine.py` | ~2350 | Custom player evaluation — computes Composite_Score, Ceiling_Score, Tool_Only_Score from tool ratings. Non-linear piecewise tool transform, WAR-derived recombination shares, asymmetric pitcher stat blend, SP innings-volume adjustment. Component-level scores (offensive_grade, baserunning_value, defensive_value, durability_score) and component ceilings. Component-level regression for weight derivation. Two-way player handling. Age-weighted ceiling blend, POT soft cap. Pure functions + batch pipeline. |
| `farm_analysis.py` | 691 | Angels farm scaffold — ranked prospects, grade tables, FV history, dev signals. |
| `roster_analysis.py` | 515 | Angels MLB roster scaffold — player cards, stat lines, contract info, surplus. |
| `prospect_query.py` | 220 | League-wide prospect rankings and farm system comparisons. |
| `draft_board.py` | ~500 | Draft board analysis — board display, best-available, two-list-merge pick list, auto-draft upload, comparison, simulation. Configurable via `draft_settings.py` parameters. |
| `draft_settings.py` | ~180 | Draft board settings persistence and validation. Manages 11 slider parameters (ceiling weight, risk tolerance, needs, surplus, balance, arsenal, personality, RP discount, control/contact penalties, survival threshold) per round group. Stored at `config/draft_settings.json`. |
| `contract_value.py` | 344 | MLB contract surplus/deficit with year-by-year projection. CLI + library. |
| `prospect_value.py` | 290 | Prospect trade surplus with option value model + career outcome probabilities. CLI + library. |
| `trade_calculator.py` | 200 | Trade package evaluation — surplus balance with sensitivity ranges. Accepts player names or IDs via `--offer`/`--receive`. Team-agnostic. |
| `trade_targets.py` | 350 | Trade target finder — MLB players by position with contract status (RENTAL/ARB/RENTAL+EXT/OPTION/CONTROLLED), seller classification, split ratings, pro-rated salary. |
| `trade_assets.py` | 150 | Tradeable assets for any team — MLB surplus players + farm prospects ranked by value. |
| `team_needs.py` | 160 | Positional needs vs league average — OPS/ERA gaps flagged by severity, upgrade priority list, platoon flags, `--aaa-roster` for full AAA depth. Works for any team. |
| `standings.py` | 160 | League-wide standings — pythagorean W/L alongside actual W/L (from `standings` table) with delta (Δ) column. `--actual` flag shows team-specific actual record with delta. `--team <ABBR>` shows team-specific actual record + playoff picture (division leaders, WC race with GB). Importable: `actual_record()`, `all_actual_records()`, `league_standings_actual()`, `playoff_picture()`. |
| `free_agents.py` | 105 | Upcoming free agent class — expiring contracts with FA/ARB/TO status. ARB-eligible players (service time < 6 years) distinguished from true walk-year FAs. |
| `player_utils.py` | 324 | Shared evaluation logic — bucketing, FV calc, WAR/aging curves, normalization. |
| `league_config.py` | 120 | Single abstraction for league-specific settings. Loads from `league_settings.json` + `state.json`. |
| `constants.py` | 82 | Valuation tables — FV→WAR, OVR→WAR, aging curves, arb %, discount rates. |
| `db.py` | 142 | DB connection, schema init, WAL mode, busy timeout. |
| `data.py` | 103 | Data access helpers (used by farm_analysis.py). |

---

## DB Tables

| Table | Owner | Description |
|---|---|---|
| `players` | `refresh.py` | All players across all orgs and levels. Expanded fields: injury (DL status, days), service time (exact years/days), roster status (DFA/waivers/traded/FA), draft history, demographics. |
| `teams` | `refresh.py` | Team ID → name mapping (34 MLB orgs) |
| `ratings` | `refresh.py` | Scouting ratings (latest snapshot only). Full 121 columns. Old snapshots pruned on refresh. |
| `ratings_history` | `refresh.py` | Monthly in-game rating snapshots (53 cols). Ovr/pot, hitter/pitcher tools (cur+pot), all pitch types (cur+pot), extended ratings. ~1.3MB/snapshot. |
| `contracts` | `refresh.py` | Active contracts league-wide |
| `contract_extensions` | `refresh.py` | Pending contract extensions (only rows with years > 0) |
| `batting_stats` | `refresh.py` | Batting stats by player/year/split (33 cols). `league_id` NULL = MLB, non-null = MiLB league. |
| `pitching_stats` | `refresh.py` | Pitching stats by player/year/split (53 cols). `league_id` NULL = MLB, non-null = MiLB league. |
| `team_batting_stats` | `refresh.py` | Team-level batting aggregates (34 teams × 3 splits) |
| `team_pitching_stats` | `refresh.py` | Team-level pitching aggregates (34 teams × 3 splits) |
| `games` | `refresh.py` | Game results (23K+ games, 2024-2033). runs0=away, runs1=home |
| `fielding_stats` | `refresh.py` | Player fielding stats by position (G, IP, TC, E, ZR, framing, arm). `league_id` column for MiLB. |
| `prospect_fv` | `fv_calc.py` | FV grades for prospects and rookie-eligible MLB players (<130 AB, <50 IP, age ≤ 24). Includes `fv_continuous` (pre-rounding FV for interpolated surplus). Cleared and rewritten each run. |
| `player_surplus` | `fv_calc.py` | Surplus value for all MLB players. Cleared and rewritten each run. |
| `trade_block` | `refresh.py` | Player IDs on the trade block (from `/tradeblock` endpoint). Cleared and repopulated each refresh. |
| `standings` | `refresh.py` | Real W-L-GB-PCT-streak-magic# for all teams (from `/lgdata`). Used by seller classification and `standings.py` display. |

---

## Key Design Decisions

**Two-tier ratings storage** — `ratings` table keeps only the latest snapshot (all teams overwritten
via `INSERT OR REPLACE`). `ratings_history` stores monthly in-game snapshots with slim columns
for development tracking. Demographics (height/bats/throws) are backfilled on existing rows via UPDATE.

**`contract_team_id` is unreliable for org membership** — The StatsPlus API retains the original
signing team's ID in `contract_team_id` even after Rule 5 drafts or other transfers. All contract
queries in `team_queries.py` use `_CONTRACT_ORG_SQL` + `_contract_org_params()` to additionally
filter by `players.team_id` / `parent_team_id`, ensuring only players currently in the org appear.

**Multi-stint stat aggregation** — `batting_stats` and `pitching_stats` store one row per team per
year (PK includes `team_id`). `player_queries.py` aggregates stints into one combined row per year
for display, preserving per-team breakdown as a `stints` list. Rate stats (ERA, OPS, K%, etc.) are
recomputed from summed counting stats — `_bat_row`/`_pit_row` store raw counts (`_d`, `_t`, `_er`,
`_hra`, `_bf`, etc.) alongside computed rates for this purpose. Percentile queries use `GROUP BY
player_id` with `SUM` to aggregate stints before computing rankings.

**FV calculation** — `calc_fv()` / `calc_fv_v2()` in `fv_model.py` (re-exported via `player_utils.py`). Expected-peak model with ceiling blend and risk labels.

Three-score model inputs from evaluation engine:
- **Composite** (composite_score): current tool value, continuous 20-80 scale
- **Projected** (ceiling_score): age-blended likely outcome
- **True Ceiling** (true_ceiling): pure potential, no age blend — used as FV ceiling input

Core formula (developing prospects, gap > 3):
```
peak = composite + gap × closure × bust
ceil_weight = max(0, min(0.5, (ceiling - 50) / 30))
FV = peak × (1 - ceil_weight) + ceiling × ceil_weight
```
- `closure`: league-calibrated gap closure rate by age and player type
- `bust`: closure-normalized discount (`target_product / closure`, capped 0.85). Ensures leagues with higher closure rates get proportionally lower bust credit.
- `ceil_weight`: elite ceilings (>50) get partial ceiling credit (up to 50% at ceiling 80)
- Maxed players (gap ≤ 3): `FV = composite` (standard rounding)
- Developing players: floor rounding (must project to 50.0+ for FV 50)
- Ceiling cap: `FV ≤ ceiling - 3`
- Offensive ceiling cap: bat-limited hitters (offensive_ceiling < 45) capped at FV 50
- Accuracy L: -5 FV. Platoon splits: -5 FV. RP ceiling ×0.85, FV cap 55.

Risk labels (Low/Medium/High/Extreme) from development confidence:
- `dev_confidence = closure_rate × age_discount × gap_scale + character_adj`
- Stored in `prospect_fv.risk`, displayed on player page
- No "+" grades — clean FV tiers (40/45/50/55/60/65/70)

**Tool interaction terms** — Disabled (Session 57). Were `contact × eye`, `power × eye` (hitters) and `stuff × movement` (pitchers). Empirically redundant with the piecewise tool transform — residual correlation with WAR is ~0.01 after the linear model. Product features are collinear with their components (r=0.85-0.93). Code retained but guarded by weight=0 check.

**Pitcher composite extended ratings** — `compute_composite_pitcher()` in `evaluation_engine.py` uses HRA and PBABIP as optional weighted tools when available. Calibration produces weights automatically; leagues without extended ratings degrade gracefully to stuff/movement/control + arsenal.

**League-calibrated model** — `calibrate.py` derives valuation tables from the league's own data:

**StatsPlus API Ctrl bug** — The ratings CSV mislabels all three Ctrl columns. Data positions are correct (overall, vs_R, vs_L) but headers read `Ctrl_R`, `Ctrl_L`, `Ctrl_L` (duplicate). `_fix_ratings_header()` in `client.py` remaps: `Ctrl_R` → `Ctrl`, first `Ctrl_L` → `Ctrl_R`, second `Ctrl_L` stays. Applies to both 113-col and 126-col formats.

**League-calibrated model** — `calibrate.py` derives valuation tables from the league's own data:
- Position-specific `OVR_TO_WAR` regression (9 buckets: C, SS, 2B, 3B, CF, COF, 1B, SP, RP). All positions target mean WAR.
- `FV_TO_PEAK_WAR_BY_POS` for each hitter bucket (COF, SS, C, CF, 2B, 3B, 1B) — derived from OVR_TO_WAR via FV→peak Ovr mapping. COF produces less WAR per FV grade than SS or CF.
- `FV_TO_PEAK_WAR` (generic hitter average, fallback), `FV_TO_PEAK_WAR_SP`, `FV_TO_PEAK_WAR_RP`
- `ARB_PCT` from actual arb salary outcomes
- `SCARCITY_MULT` from mid-season FA availability by Pot (sigmoid mapping, monotonic)
- Stored in `config/model_weights.json`. `constants.py` loads calibrated values when present, falls back to hardcoded defaults. Runs automatically during refresh (before fv_calc).

**Contract surplus model** — `contract_value()` in `contract_value.py`:
- Pending contract extensions: checks `contract_extensions` table. If a pending extension exists, appends extension years and salary schedule after current contract ends instead of estimating arb control.
- MLB scarcity premium: positional multiplier on market value (`MLB_SCARCITY` in `constants.py`). SS +10%, CF/SP +6%, C/2B/3B +3%, COF/RP −6%, 1B −9%. Makes contract model consistent with prospect scarcity.
- `stat_peak_war` minimum: 1 qualifying season (was 2). Players with 1 season get stat-based projections instead of pure ratings fallback. Uses 4-year weighted average `[3, 3, 2, 1]` with the most recent year scaled by season completion fraction (season_pct). Current-year stats always included (no mid-season exclusion) — partial seasons get proportionally reduced weight.
- Pitcher role-change blending: when current role (SP/RP) has < 2 full seasons but the opposite role has history, blends new-role data with discounted prior-role stats (SP→RP × 0.46, RP→SP × 2.15). Blend weight increases as new-role data accumulates; at 2+ full seasons, prior role is fully phased out. Prevents single-year role converts from losing all track record.
- Unproven player discount: when `stat_peak_war` is None (0 qualifying seasons), ratings-based WAR is discounted by 0.5×. Data showed low-Ovr players with no track record produce far below ratings projections.
- Team-control estimation from exact MLB service time. All service-time interpretation lives in one helper, `arb.service_time(conn, pid)` → `ServiceTime` (fractional years, completed years, `years.days` display). The API's `mlb_service_days` is the *cumulative total* (full year = 172 days), so `completed_years = days // 172`. Only **completed** years reduce control (a player at 4 years 70 days has 2 control years left, not 1). Falls back to a games-based heuristic (hitters g/162, SP gs/32, RP g/65, summed) when no MLB service data exists. Age gates: age ≥30 on min salary → veteran minor league deal. **Super Two is not modeled** — flat 3-year arb threshold (see task_list).
- Arb salary projection: Ovr-based exponential model (MAE $0.53M/yr); RP-specific model calibrated from 35 arb contracts (566K × e^(0.0294 × Ovr), 25% annual raises)
- RP arb salary discount (0.80x)
- Non-tender gate: control truncated when projected arb salary exceeds 2× scarcity-adjusted market value
- Young player ratings blend: when ratings WAR > stat WAR and age < peak, blends the two (50% ratings at 21, fading to 0% at peak age). Upside-only — prevents undervaluing young players with limited track records.
- Development ramp: for young players with Pot > Ovr, projects Ovr growth toward Pot. Blends stat history (positive or negative) with ratings projection using 0.5^year decay. When no stat history, applies 0.5× discount to ratings projection.
- Veteran decline ratings blend: when stat WAR > ratings WAR and age > 31 (30 for pitchers), blends toward ratings. Weight = `age_w × gap_ratio` (capped 0.75). Prevents stale stat history from inflating projections for declining veterans.
- WAR floor at 0 (teams can release/DFA)
- Pre-arb age gate: age ≥28 on league minimum treated as 1yr FA deal

**Prospect surplus model** — `prospect_surplus_with_option()` in `prospect_value.py`:
- Probability-weighted surplus across base/mid/ceiling FV scenarios. Upside probabilities scale with youth (+5%/yr under 20) and Pot-FV gap (wider gap = more development runway, `gap_factor = min(1.0, (pot - fv) / 25)`).
- Position-specific `FV_TO_PEAK_WAR_BY_POS` for hitters (COF 3.0, SS 3.6, CF 3.9 at FV 50). Falls back to generic hitter average for unknown buckets.
- Age-adjusted development discount (bust probability only, not time value). Flattened curve: AAA 0.88, AA 0.78, A 0.68, Rookie 0.45, Intl 0.35. Age adjustment at 4%/yr vs level norm.
- Certainty multiplier from Ovr/Pot realization ratio, capped at 1.0 (no bonus for maxed prospects — proximity already rewarded by dev discount and time value).
- Realization blend: when Ovr/Pot > 0.7, blends FV-based peak WAR with Ovr-based current WAR (squared weight curve). Maxed players use OVR→WAR; developing players use FV→WAR. Downward-only.
- Scarcity multiplier by Pot (ceiling) — sigmoid-based mapping from FA availability rate. Calibrated from mid-season data: Pot 40 = 0.0 (12%+ FA rate), Pot 44 = 0.44 (3%), Pot 46 = 0.97 (<1%), Pot 50+ = 1.0 (0%). Monotonic, no single-point cliffs. Stored in `SCARCITY_MULT` (calibrated or default).
- SP-specific `FV_TO_PEAK_WAR_SP` table (1.5–4.9 WAR) — pitchers produce less WAR per FV grade than hitters
- RP-specific `FV_TO_PEAK_WAR_RP` table (0.7–1.9 WAR)
- Time-value discounting at 5%/yr
- Smooth market value ramp (linear from league min at 0 WAR to full $/WAR at 1.0 WAR)
- Prospect age cutoff: ≤24 (25yo minor leaguers excluded — MLB-bubble, not prospects)
- Zero surplus floor
- Component-aware scenario adjustments: `_adjust_scenario_probs()` shifts base/mid/ceiling probabilities based on offensive grade, offensive ceiling, defensive value, and durability score. Premium defense at SS/C/CF boosts development probability; low-durability SP increases bust risk; balanced profiles tighten distributions.

**Career outcome probabilities** — `career_outcome_probs()` in `prospect_value.py`. Three-scenario model (base/mid/ceiling) with logistic CDF for WAR variance within each scenario. Component scores adjust both scenario weights and development probability. Displayed as cumulative P(WAR ≥ threshold) bars on the player page.

**$/WAR** — FA market rate. Calibrated from multi-year contracts to players aged 28+ (past arbitration, salary ≥$5M). Represents the true open-market price for WAR. Falls back to all contracts if <30 FA deals available. Stored in `config/league_averages.json`, recalculated each league refresh.

**Arb salary projection** — Uses raise-based model from `arb_model.arb_salary()`:
- Arb 1: exponential base from player quality (`ARB_HITTER_BASE × exp(ARB_HITTER_EXP × composite)`)
- Arb 2-3: prior salary + raise (raise scales with composite)
- Properly escalates year over year (not flat percentage of market value)
- Salary discounted to present value same as market value for surplus calculation

**IP storage** — the API returns `ip` as a truncated integer; `outs` is precise. All IP
values in the DB are stored as true decimal innings (`outs / 3`). ERA computed from outs
(`er * 27 / outs`). Display uses `fmt_ip` Jinja filter (33.333 → "33.1").

**SQLite WAL mode** — `db.py` enables WAL journal mode with 30s busy timeout. Allows
concurrent reads during writes so the web UI stays browsable during background refreshes.

**League configuration** — all league-specific settings (team IDs, divisions, mappings,
year, pyth exponent, ratings scale) live in `config/league_settings.json` and are accessed via
`league_config.config`. No hardcoded team IDs or league assumptions in scripts or web code.
`ratings_scale` (`"1-100"` or `"20-80"`) controls how `norm()` in `player_utils.py` handles
tool grades and how projection models interpret raw inputs.

**Summary reuse** — Both scaffold scripts support summary reuse from history files:
- `farm_analysis.py` reads `history/prospects.json` — dev signals (FV movement, stagnation, new season)
- `roster_analysis.py` reads `history/roster_notes.json` — Ovr movement ±5 and season-based refresh triggers

---

## Standard Workflows

### Full evaluation cycle (new game date)
```bash
python3 -m statsplusplus.data.refresh 2033       # all teams — auto-fetches date, updates state, runs fv_calc
python3 scripts/farm_analysis.py                 # farm scaffold
python3 scripts/roster_analysis.py               # roster scaffold
# Read scaffolds, write/update summaries, assemble reports
```

### Angels-only refresh (faster, active session)
```bash
python3 -m statsplusplus.data.refresh 2033       # Angels only — same auto-state + fv_calc
python3 scripts/farm_analysis.py
python3 scripts/roster_analysis.py
```

### Standings and free agents
```bash
python3 scripts/standings.py                     # from DB
python3 scripts/standings.py --refresh           # fresh from API
python3 scripts/free_agents.py --bucket SP       # upcoming FA starters
python3 scripts/free_agents.py --angels          # Angels expiring contracts
```

### Prospect queries
```bash
python3 scripts/prospect_query.py top --bucket SP --fv-min 50 --age-max 23
python3 scripts/prospect_query.py systems
python3 scripts/prospect_query.py team "Texas"
```

### Contract / trade evaluation
```bash
python3 scripts/contract_value.py "McClanahan"
python3 scripts/prospect_value.py --player 52392
python3 scripts/trade_calculator.py --trade '{"angels_send": [...], "angels_receive": [...]}'
```

---

## Web UI

Local Flask app at `web/`. Dark theme, monospace font, no CSS/JS frameworks.

### Routes

| Route | Template | Description |
|---|---|---|
| `/` | redirect | Redirects to `/team/<my_team_id>` |
| `/dashboard` | redirect | Redirects to `/team/<my_team_id>` |
| `/team/<id>` | `team.html` or `team_minor.html` | MLB team page (roster, depth chart, contracts, farm) or minor league team page (notables, full roster). Detected by player level. |
| `/league` | `league.html` | League overview (vitals KPIs, 2×3 division standings grid with WC badges, scrollable power rankings with score heatmap, hero leader cards with MLB/AL/NL toggle), top 100 prospects with side panel, trade builder tab, draft tab with sim/upload |
| `/player/<id>` | `player.html` | Player detail — ratings, stats, percentiles, contract, surplus projection |
| `/settings` | `settings.html` | Team selector (`my_team_id` in `config/state.json`) |
| `/refresh` | JSON | POST — starts background refresh (`refresh.py`). Returns 409 if already running. |
| `/refresh/status` | JSON | GET — returns `{running, result, message}` for polling. |
| `/api/player-search` | JSON | GET — autocomplete player search (`?q=`), up to 15 results. Used by nav search bar and trade tab. |
| `/api/player-card/<pid>` | JSON | GET — side-panel-style player data (tools with grade bars, pitches, defense, stats) for any player. Used by comp inline expand. |
| `/api/draft-detail/<pid>` | JSON | GET — compact grid data for draft prospect detail panel (tools, pitches, fielding, positions, character). |
| `/api/draft-picks` | JSON | GET — fetch current draft picks from StatsPlus API. |
| `/api/draft-pool-upload` | JSON | POST — upload CSV of draft-eligible player IDs from OOTP export. |
| `/api/draft-sim` | JSON | POST — run draft simulation (`{pick, rounds, seed}`). Returns projected picks. Uses saved draft settings. |
| `/api/draft-upload-list` | JSON | POST — generate urgency-greedy auto-draft list (`{top}`). Writes file + returns preview. Uses saved draft settings. |
| `/api/draft-settings` | JSON | GET — load current draft settings (round groups, slider values). POST — save settings. |
| `/api/draft-settings/copy` | JSON | POST — copy draft settings from another league to the active league (`{from_league}`). |
| `/api/open-file-location` | JSON | POST — open system file explorer to a file's directory. |
| `/api/org-players/<tid>` | JSON | GET — full org roster (MLB + farm) for trade tab |
| `/api/trade-value` | JSON | POST — single-player trade valuation with retention support |

### Team Page Features

Tabbed layout (Main / Depth Chart / Organization / Hitters / Pitchers / Contracts / Finances / Player Development) with client-side JS tab switching. Page title (`<h1>`) shows full team name. Summary bar persists across all tabs.

- **Summary bar** — game date, MLB surplus, farm surplus, FV 50+ count, payroll, roster composition (SP/RP/Pos counts)
- **Main tab** — division standings (pythagorean W/L, viewed team highlighted, team names linked) and team batting/pitching stats side-by-side with league rank out of 34 (top-5 bright green, bottom-5 red). Record breakdown panel (Overall/Home/Away/vs Division/1-Run/L10/Streak). Recent games panel (last 10 with linked opponents and pitchers with running records).
- **Roster tab** — position players and pitchers sorted by WAR (Pos/Role first column), surplus leaders (MLB + farm combined, top 15 by surplus)
- **Contracts tab** — MLB contracts only (`is_major=1`) sorted by salary with surplus/option flags, upcoming free agents (multi-year deals expiring within 2 years, or age 30+ on 1-year deals — excludes pre-arb/arb players with team control)
- **Finances tab** — committed payroll table with 6-year horizon. Per-player salary by year with TO/PO option markers and NTC badges. Arb/pre-arb projections shown in italics with `est` superscript. Pre-arb summary row for min-salary players with no projected future. Total committed row in footer.
- **Organization tab** — cross-level org summary. Position depth table (8 field positions + SP 1-5 + RP top 3: MLB player with color-coded Ovr/WAR/surplus + top prospect with FV/level badge/surplus, deduplicated, OF labeled as LF/CF/RF + league rank pill). Surplus leaders (top 20 combined MLB+Farm with level badges). Retention priorities (positive-surplus players with ≤2yr control; multi-year contracts use contract years, 1-year uses `_estimate_control()`). Committed payroll 4-year bar chart.
- **Player Development tab** — farm top 15 (sorted by FV with surplus), farm depth (by position bucket and level, with total surplus, league average, and league rank), age distribution (MLB and farm FV 40+ with horizontal bars and league average markers)
- All queries parameterized by `team_id` — any team's page viewable via `/team/<id>`

### Player Page Features

MLB players use a three-tab layout (Overview / Stats / Contract). Prospects display all content on a single page without tabs.

- **Ratings panel** — overlaid current/potential grade bars (20-80 scale, color-coded by tier). Sections: Hitting tools → Running (Speed/Steal) → Positions → Defense. Defense ordered by position type (infielders: Range/Error/Arm/Turn DP; outfielders: Range/Error/Arm; catchers: Blocking/Framing/Arm). Pitchers show GB% as raw percentage.
- **L/R split toggles** — on Ratings, Percentile Rankings, and Overview stats snapshot. Split percentile expected values use split-specific ratings (e.g. `cntct_l` for vs-L).
- **Stats tab split selector** — 3-button selector (Overall / vs L / vs R) on batting and pitching stats tables. Each view shows full year-by-year history for that split (2020-2033).
- **Stats snapshot** — compact current-year stats on Overview tab between scouting report and percentiles. Pitchers show pitching stats, hitters show batting stats. L/R toggle for current year.
- **Percentile rankings** — Baseball Savant style with fill bars, dots, blue-to-red gradient. Performance tags for rating-to-stat divergence (Hot/Cold/Lucky/Unlucky). Scaled threshold (25 at midrange, 15 at extremes). Unqualified players shown in grey with "(small sample)" label. Hitter BABIP expected uses regression model (cntct + speed, R²=0.23) plus historical residual adjustment from 2+ prior qualifying seasons. Pitcher BABIP expected uses pbabip regression model (BABIP = 0.439 - 0.0028 × pbabip). Pitcher qualification: SP 0.7 IP/team game, RP 0.35 IP/team game (RP detected by GS/G < 0.25).
- **Fielding percentiles** — per-position FPCT/ZR (all), +Arm (OF), +Framing (C). ZR expected from rating composites (IF: IFR/IFE, OF: OFR, C: IFR/CArm/CBlk). Framing expected from CFrm/CBlk. Qualifier: 1.0 IP per team game.
- **Fielding stats** — Year/Pos/G/IP/TC/A/E/DP/FPCT/ZR/Arm table.
- **Contract panel** — year-by-year salary with NTC badge on header, TO/PO badges on final year row.
- **Surplus projection** — year-by-year breakdown (MLB: WAR/market/salary/surplus; prospects: control years/ETA/dev discount). Pessimistic/optimistic range.
- **Stats tables** — Batting: Year/G/PA → slash/ISO → BB%/SO%/BABIP → HR/RBI/SB-CS → OPS+/WAR. Pitching: Year/G/GS/IP → ERA/ERA+/FIP/SIERA → K%/BB%/K-BB%/GB%/BABIP → W/L/SV/HLD → WAR.
- **Header** — Compact identity bar: Team/Level, Name, Position, Age, Ht/B-T, Comp/Ceil (color-coded), Ovr/Pot, FV (prospects), Surplus, PAP (MLB), Performance indicator.
- **Player Evaluation panel** — Unified evaluation display replacing the old Tool Profile and Development Tracking panels. Two-box layout showing "Now" (composite + MLB percentile + tier label) and "Ceiling" (ceiling + MLB percentile + tier label). Component bars (Offense/Baserunning/Defense or Pitching/Durability). Carrying/red-flag tools. "vs. Game Rating" divergence section with Hidden Gem / Landmine badges. Development tracking deltas.

### Key Files

| File | Purpose |
|---|---|
| `web/app.py` | Flask routes |
| `web/queries.py` | State helpers, league queries (prospects, leaders, player search, draft pool), re-exports from extracted modules |
| `web/team_queries.py` | Team-level queries — standings, roster, farm, contracts, surplus, age distribution, farm depth, depth chart, draft org depth |
| `web/player_queries.py` | Player detail query — ratings, stats, splits, contract, surplus, personality, scouting summary |
| `web/trade_queries.py` | Trade tab queries — org roster (MLB + farm), trade valuation adapter |
| `web/percentiles.py` | Percentile rankings — hitter + pitcher, with expected range markers and performance tags |
| `web/promotion_readiness.py` | Promotion readiness and demotion risk assessment — league-calibrated badges (Ready/Knocking/Overmatched/Struggling) for minor league and MLB players |
| `web/templates/team.html` | Team page — standings, stats, roster, contracts, farm |
| `web/templates/player.html` | Player detail template with macros (`grade`, `pctile_grid`) and `toggleSplits()` JS |
| `web/templates/league.html` | League page — vitals KPIs, standings, power rankings, prospects tab, standings tab (expanded + H2H matrix), rankings tab, trade tab, draft tab (side-by-side layout: board left, detail/picks sidebar right, settings modal with per-round-group sliders) |
| `web/static/style.css` | Dark theme, grade bar tiers, percentile styling, rank coloring, draft bar chart styles |
| `web/static/sort.js` | Client-side table sorting (numeric, string, positional spectrum) with smart rank renumbering |

---

## Reference Docs

| Doc | Purpose |
|---|---|
| `docs/farm_analysis_guide.md` | FV methodology, bucketing rules, report format, checklist |
| `docs/roster_analysis_guide.md` | Roster analysis process, player card format, checklist |
| `docs/trade_analysis_guide.md` | Trade evaluation process, surplus model, calibration log |
| `docs/trade_target_workflow.md` | Trade target search workflow (6-step process) |
| `docs/org_overview_guide.md` | Org overview report template |
| `docs/prospect_query_guide.md` | prospect_query.py usage and examples |
| `docs/client_reference.md` | StatsPlus API client reference |
| `docs/ootp/ratings_and_attributes.md` | OOTP ratings system reference |
| `docs/ootp/financial_model.md` | OOTP financial mechanics |
| `docs/ootp/aging_and_development.md` | Aging curves and development factors |
| `RULES.md` | Data pull/storage rules, refresh workflow |
| `STRUCTURE.md` | Directory layout and file conventions |
| `PURPOSE.md` | Goals and affiliates |
