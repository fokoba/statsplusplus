# API Integration Impact Analysis

Comprehensive mapping of how newly available StatsPlus API data integrates with
existing Stats++ subsystems. Documents where new data replaces estimates, feeds
into models, and touches the UI/CLI.

**Date:** 2026-07-27
**Source:** `docs/client_reference.md` (API endpoint documentation)
**Target:** `docs/task_list.md` → API Integration Roadmap (implementation plan)

---

## 1. Service Time — Replaces Estimation with Exact Data

### Current State (Estimation)

`arb_model.estimate_service_time(conn, player_id)` estimates fractional MLB service
years from game appearances using role-adjusted denominators:

```python
# Hitters: games / 162
# SP (gs >= g/2): gs / 32
# RP (gs < g/2): g / 65
# Caps each year at 1.0, sums across all years
```

This is a **heuristic** that systematically underestimates service time because:
- Roster days count toward service time even without game appearances
- September callups who appear in 30 games get credited with ~0.18 years, but their
  actual service time may be 0.5+ years from roster days
- Injured players on the active roster accrue service time without playing
- Players who are traded mid-season have games split across teams

### New Data Available

| API Field | Type | Precision |
|---|---|---|
| `mlb_service_years` | int | Exact years of service |
| `mlb_service_days` | int | Exact days beyond full years |
| `mlb_service_days_this_year` | int | Days accrued this season so far |
| `pro_service_years` | int | Total professional service |
| `pro_service_days` | int | Days beyond full pro years |

### Systems Impacted

| System | File | Function | Current Use | New Data Impact |
|---|---|---|---|---|
| **Arb eligibility detection** | `arb_model.py` | `estimate_service_time()` | Games heuristic | **Replaced entirely** — read from DB |
| **Team control estimation** | `arb_model.py` | `estimate_control()` | Uses `estimate_service_time()` + salary heuristics | Becomes **deterministic** — `svc >= 6` = FA, `3 <= svc < 6` = arb, `svc < 3` = pre-arb |
| **Contract surplus projection** | `contract_value.py` | `contract_value()` | Calls `estimate_control()` for 1yr contracts | More accurate remaining control → better surplus |
| **Free agent detection** | `free_agents.py` | `upcoming_fas()` | Calls `estimate_service_time()` to detect arb vs FA | Exact arb/FA classification |
| **Trade target classification** | `trade_targets.py` | `find_targets()` | Calls `estimate_service_time()` for ARB label | Exact RENTAL vs ARB distinction |
| **Payroll projection** | `team_queries.py` | `get_payroll_summary()` | Calls `estimate_control()` for future years | Correct control period → accurate payroll horizon |
| **Depth chart (future years)** | `team_queries.py` | `get_depth_chart()` | Calls `estimate_control()` for departure detection | Knows exactly when players leave control |
| **Super Two detection** | — | Not implemented | — | Now possible: `mlb_service_years == 2` + `mlb_service_days >= 130` |

### Migration Path

1. Add `mlb_service_years`, `mlb_service_days` columns to `players` table
2. Store during `_upsert_players()` in refresh.py
3. Create `get_service_time(conn, player_id) -> (years, days)` that reads from DB
4. Modify `estimate_service_time()` to prefer DB value, fall back to games heuristic
   for players without the field (backward compat)
5. Modify `estimate_control()` to use exact service time thresholds

### Key Design Decisions

- **Backward compatibility:** Keep `estimate_service_time()` as fallback for leagues
  that haven't refreshed since the API update. The function should check if the player
  has `mlb_service_years` in DB first.
- **Super Two boundary:** In OOTP, the exact Super Two threshold varies. With exact
  service days, we can detect it precisely (typically `2.130+`).
- **Perpetual arb leagues:** The `_cfg.perpetual_arb` path in `estimate_control()`
  doesn't use service time at all — unaffected.

---

## 2. Injury & DL Status — Fills a Critical Gap

### Current State

No injury awareness anywhere in the system. This causes:
- Trade targets recommended that are injured/unavailable
- Depth charts counting DL players in projections at full playing time
- `team_needs.py` flagging positions as weak when the starter is on the DL
  (the backup's stats are poor, but the issue is temporary)
- No way to distinguish DFA'd players from active roster

### New Data Available

| API Field | Type | Description |
|---|---|---|
| `injury_is_injured` | int | 1 = currently injured |
| `injury_dl_left` | int | Days remaining on DL |
| `injury_left` | int | Days until fully healthy (may differ from DL time) |
| `is_on_dl` | int | On short-term DL |
| `is_on_dl60` | int | On 60-day DL |
| `dl_days_this_year` | int | Total DL days this season |

### Systems Impacted

| System | File | Function | Impact |
|---|---|---|---|
| **Trade targets** | `trade_targets.py` | `find_targets()` | Flag injured targets ("DL: 45d left"), exclude or mark unavailable |
| **Depth chart** | `team_queries.py` | `get_depth_chart()` | Exclude DL players from current-year playing time allocation; show as "injured reserve" |
| **Team needs** | `team_needs.py` | `analyze()` | Distinguish "starter injured" (temporary gap) from "position is weak" (actual need) |
| **Player pages** | `player_queries.py` | `get_player()` | Show injury status banner |
| **Roster display** | `team_queries.py` | `get_roster_hitters/pitchers()` | Tag DL players, separate from active roster |
| **Org overview** | `team_queries.py` | `get_org_overview()` | Injury indicator next to affected players |
| **Trade analyst agent** | `.kiro/steering/trade-analyst.md` | Target verification | No longer needs to ask user about availability for every target |
| **Prospect pages** | `web/queries.py` | `get_player_card()` | Show if prospect is injured at MiLB level |

### Migration Path

1. Add injury columns to `players` table schema
2. Store during `_upsert_players()` in refresh.py
3. Create utility: `is_injured(player_row) -> bool`, `injury_info(player_row) -> dict`
4. Modify `find_targets()` to add injury flag to output
5. Modify `get_depth_chart()` to reduce injured player PT allocation to 0 for current year
6. Modify `team_needs.py` to note when a position's weakness is injury-driven
7. Add injury badge to web templates (roster, player page, trade targets)

### Key Design Decisions

- **Depth chart:** Injured players should appear in future-year projections (they'll be
  healthy) but have reduced/zero playing time in current year based on `injury_dl_left`
- **Trade targets:** Don't exclude injured players entirely — some injuries are minor.
  Flag with severity: `DL 7d` (back soon) vs `DL 180d` (season-ending)
- **Team needs:** Add "★ INJURED" annotation when the position's starter is on DL,
  to distinguish from genuine weakness

---

## 3. Roster Status Flags — Transaction Awareness

### Current State

No visibility into DFA, waivers, trades, or FA status between refreshes. The trade
analyst agent must ask the user about every recent transaction.

### New Data Available

| API Field | Type | Description |
|---|---|---|
| `designated_for_assignment` | int | Player has been DFA'd |
| `is_on_waivers` | int | Player is currently on waivers |
| `was_traded` | int | Player was traded this season |
| `free_agent` | int | Player is a free agent |
| `is_on_secondary` | int | On taxi squad / secondary roster |
| `is_active` | int | On active roster |
| `days_on_waivers` | int | Days spent on waivers |
| `days_on_waivers_left` | int | Days remaining on waiver period |

### Systems Impacted

| System | File | Impact |
|---|---|---|
| **Trade targets** | `trade_targets.py` | Exclude DFA'd/waiver players from targets; flag recently traded players |
| **Free agents** | `free_agents.py` | Use `free_agent` flag directly instead of inferring from contract expiry |
| **Roster queries** | `team_queries.py` | Separate active vs DFA vs taxi in roster displays |
| **Depth chart** | `team_queries.py` | Exclude DFA'd players from depth chart entirely |
| **Player pages** | `player_queries.py` | Show status badge (DFA, Waivers, FA) |
| **Trade analyst** | Steering doc | Can verify availability without asking user in many cases |

### Key Design Decision

- DFA'd players with `team_id = 0` or `parent_team_id = 0` should be filtered from
  roster displays and depth charts but remain searchable in player pages.
- `was_traded` flag enables showing "NEW" badge on recently acquired players.

---

## 4. Real Standings via `/lgdata` — Replaces Pythagorean for Classification

### Current State

`standings.py` computes **pythagorean** W-L from team RS/RA using exponent 1.83.
This is used for:
- Seller classification in `trade_targets.py` (>8 GB from playoff spot)
- Display on standings pages and team overviews
- Team role classification in the trade analyst agent

Pythagorean standings are a projection of "true talent" but diverge from actual
record (luck, bullpen, clutch). The `--actual` flag on `standings.py` pulls W-L from
the `games` table, but this only works when game history is complete.

### New Data Available

`/lgdata` provides authoritative W-L-T-PCT-GB-Streak-Magic# for all 348 teams
(including minor leagues).

### Systems Impacted

| System | File | Function | Impact |
|---|---|---|---|
| **Seller classification** | `trade_targets.py` | `_classify_sellers()` | Use real GB instead of pythagorean — more accurate mid-season |
| **Standings display** | `standings.py` | `print_standings()` | Show real W-L alongside pythagorean (both have value) |
| **Division detection** | `refresh.py` | `_detect_league_structure()` | `/lgdata` provides authoritative division assignments — no more game-frequency clustering needed |
| **Playoff picture** | `team_queries.py` | `get_standings()` | Real GB and magic numbers |
| **Trade analyst** | Steering doc | Team role classification | Real record for buyer/seller determination |
| **League state** | All | — | `/lgdata` `state` field tells us preseason/regular/playoffs/offseason |

### Migration Path

1. Add `get_lgdata() -> dict` to client.py
2. During refresh, store standings in a new `standings` table or update existing flow
3. Modify `_classify_sellers()` to use real GB when available
4. Modify `standings.py` to show both pythagorean and actual
5. Simplify `_detect_league_structure()` to use `/lgdata` division/team hierarchy

### Key Design Decision

- **Keep pythagorean alongside actual.** Pythagorean tells you team *talent*; actual
  tells you team *record*. Both are useful. A team 5 games over .500 in actual but
  5 games under in pythagorean is a regression candidate.
- **Division detection simplification:** The 450-line `_detect_league_structure()`
  function can be reduced to ~50 lines reading `/lgdata` hierarchy directly.
  Keep the game-frequency approach as a fallback for leagues where `/lgdata` is unavailable.

---

## 5. Trade Block — Direct Availability Signal

### Current State

No way to know which players are actually available for trade without asking the user
or inferring from team record (seller classification). The trade analyst agent always
asks: "Do you know what [team] is looking for?"

### New Data Available

`/tradeblock` returns `{"player_ids": [20394, 25681, ...]}` — all players explicitly
placed on the trade block by their teams.

### Systems Impacted

| System | File | Impact |
|---|---|---|
| **Trade targets** | `trade_targets.py` | Add "📋 ON BLOCK" flag; create `--on-block` filter |
| **Trade analyst** | Steering doc | Can surface confirmed-available players without asking |
| **Trade workbench** | Future web UI | Pre-populate with trade block players |

### Migration Path

1. Add `get_tradeblock() -> dict` to client.py
2. Store in a simple `trade_block` table (player_id, fetched_date)
3. Refresh during `refresh_league()` (fast — single JSON call)
4. Modify `find_targets()` to join against trade_block and flag matches

---

## 6. Minor League Stats — Transforms Prospect Evaluation

### Current State

**Farm analysis is entirely ratings-based.** FV grades, surplus values, prospect
rankings — all computed from tool ratings + age/level context alone. No statistical
performance data for any minor league player.

This means:
- A prospect with elite ratings but terrible stats looks identical to one raking
- A breakout performer with modest ratings gets no credit for production
- Development tracking is ratings-only (did Pot/Ovr increase?)
- No WAR-based validation of FV grades for MiLB players

### New Data Available

Full batting/pitching/fielding stat lines for all minor league levels via `lid` parameter:
- **Batting:** AB, H, HR, BB, K, SB, PA, WAR, UBR, WPA (same schema as MLB)
- **Pitching:** IP, ERA, K, BB, WAR, RA9WAR, GB%, FIP components (same schema as MLB)
- **Fielding:** G, GS, E, TC, PO, A, DP, PB, SBA, RTO

League IDs for eMLB: 151/152 (AAA), 153-155 (AA), 156-160 (A), 165 (Rookie)

### Systems Impacted

| System | File | Function | Impact |
|---|---|---|---|
| **FV calculation** | `fv_calc.py` | `run()` | Could adjust FV confidence based on stat performance |
| **FV model** | `fv_model.py` | `calc_fv()` | Add stat-performance modifier (outperforming ratings → boost) |
| **Evaluation engine** | `evaluation_engine.py` | `compute_ceiling()` | MiLB stats as confidence signal |
| **WAR model** | `war_model.py` | `stat_peak_war()` | Extend to MiLB — give young callups a stats baseline |
| **Prospect value** | `prospect_value.py` | `prospect_surplus()` | Performance-validated prospects worth more |
| **Player pages** | `player_queries.py` | `get_player()` | Show MiLB career stats |
| **Prospect pages** | `web/queries.py` | `get_player_card()` | MiLB stat lines by level |
| **Farm analysis** | `farm_analysis.py` | Report generation | Statistical context for each prospect |
| **Draft board** | `draft_board.py` | Draft value | Validate ratings against performance |
| **Calibration** | `calibrate.py` | Tool weight regression | Could extend to MiLB-level calibration |

### Specific Model Integration Points

**A. FV Confidence Adjustment (Medium complexity)**

Currently `calc_fv()` returns a grade + risk label. With MiLB stats:
- Prospect with FV 55 and AAA WAR of 4.0 → higher confidence, risk reduced
- Prospect with FV 55 and AAA WAR of 0.5 → lower confidence, risk elevated
- Could implement as: `risk_adj = f(expected_war_for_level_and_fv, actual_war)`

**B. Stat Blending for Recent Callups (High value)**

`war_model.stat_peak_war()` currently only looks at MLB stats. For a player with
20 MLB PA but 500 AAA PA, the MLB stats are noise. Integration:
- Combine MiLB WAR (discounted by level) with limited MLB stats
- Smooth the prospect→MLB evaluation crossover (already on task list)
- `NO_TRACK_RECORD_DISCOUNT = 0.50` could be adjusted based on MiLB performance

**C. Development Tracking (New capability)**

Compare MiLB stats year-over-year alongside ratings history:
- K% declining + strikeout rating unchanged → ratings may be ahead of reality
- WAR trending up while ratings flat → breakout developing
- Enables data-driven development curves per league

### Migration Path

1. Implement `/lgdata` client method (discover league IDs)
2. Add `league_id` column to existing stat tables (default NULL for MLB)
3. Fetch MiLB stats during refresh (current year, maybe prior year)
4. Build `milb_stat_peak_war()` variant with level-adjustment factors
5. Add MiLB stat display to player/prospect pages
6. Research: FV confidence adjustment model
7. Research: stat blending for young players with limited MLB time

### Key Design Decisions

- **Same tables vs separate tables:** Recommend adding `league_id` to existing
  `batting_stats`/`pitching_stats`/`fielding_stats`. Keeps one query path, avoids
  code duplication. NULL = MLB (backward compat).
- **Level adjustment factors:** MiLB WAR is not MLB WAR. Need empirically-derived
  conversion: AAA WAR × 0.6 ≈ MLB WAR? Requires research.
- **Refresh time:** Currently ~3 min. Adding all MiLB leagues (~10 league IDs × 3
  stat types × current year) adds ~10-20 API calls. Estimate +30-60s.
- **Selective fetching:** Could only fetch for org players (faster) vs all players
  (enables league-wide MiLB rankings). Recommend all players for full utility.

---

## 7. Expanded Contract Fields — Better Option/Incentive Handling

### Current State

`contract_value.py` handles team/player options simply:
- `last_year_team_option` / `last_year_player_option` → flag in output
- No awareness of vesting options, next-year options, or buyouts
- No incentive bonuses factored into true contract cost

### New Data Available

| Field | Impact |
|---|---|
| `last_year_vesting_option` | Auto-exercises if performance threshold met |
| `next_last_year_team/player/vesting_option` | Multiple option years |
| `last_year_option_buyout` | Buyout cost for declining option |
| `next_last_year_option_buyout` | Buyout for penultimate year |
| `minimum_pa/ip + bonus` | Performance incentives |
| `mvp/cyyoung/allstar_bonus` | Award incentives |

### Systems Impacted

| System | File | Impact |
|---|---|---|
| **Contract value** | `contract_value.py` | Model vesting likelihood, include buyout in surplus calc |
| **Free agents** | `free_agents.py` | Distinguish option years from true FA |
| **Trade targets** | `trade_targets.py` | Better OPTION classification (vesting vs team vs player) |
| **Payroll projection** | `team_queries.py` | Factor in option buyouts and incentive likelihoods |
| **Contract display** | Web templates | Show option details, incentives on player/team pages |

### Key Design Decision

- Vesting options that are likely to vest (based on player health/production) should
  be treated as guaranteed years for surplus purposes. Need a `vesting_probability()`
  function that estimates likelihood from PA/IP projections.

---

## 8. Park Factors via `/ballparks` — Context for Stat Evaluation

### Current State

No park factor awareness. All stats treated uniformly regardless of home park.

### New Data Available

Per-team park factors: AVG (vs L, vs R, overall), doubles, triples, HR (vs L, vs R, overall).

### Systems Impacted

| System | File | Impact |
|---|---|---|
| **Stat evaluation** | `war_model.py` | Park-adjust stats before WAR comparison |
| **Team needs** | `team_needs.py` | Context: ".720 OPS in Coors ≠ .720 OPS in pitcher's park" |
| **Projections** | `projections.py` | Park-adjust projected OPS+ |
| **Player pages** | `player_queries.py` | Show park factor context |
| **Percentiles** | `percentiles.py` | Park-adjusted percentile rankings |

### Priority Assessment

Lower priority than service time/injury/standings. OOTP park factors are typically
modest (0.95-1.10 range), and our evaluation is already primarily ratings-based.
Most valuable for the trade analyst workflow when comparing hitters from extreme parks.

---

## 9. Draft History Fields — Context Enhancement

### Current State

`draft_board.py` works with the current draft pool. No historical draft context
stored per player (which team drafted them, what round, what pick).

### New Data Available

| Field | Use |
|---|---|
| `draft_year` | Historical context |
| `draft_round` | Pedigree signal |
| `draft_pick` / `draft_overall_pick` | Exact draft position |
| `draft_team_id` | Organizational history |

### Systems Impacted

| System | Impact |
|---|---|
| **Player pages** | "Drafted by [Team] in Round N, Pick M overall (year)" |
| **Prospect pages** | Draft pedigree as context (high pick = high investment = longer leash) |
| **Farm analysis** | Draft capital tracking (how well has the org drafted?) |
| **Trade analyst** | Context: "this is a former 1st rounder, team invested heavily" |

### Priority Assessment

Low complexity, low risk, nice-to-have context. Good "quick win" to implement
alongside Phase 1 player fields.

---

## 10. OSA Ratings — Dual Evaluation Capability

### Current State

We only pull scouted ratings (team scout's view). Accuracy varies by scout quality.

### New Data Available

`/ratings?osa=1` returns OSA (public) ratings for comparison.

### Systems Impacted

| System | Impact |
|---|---|
| **Evaluation engine** | Could blend or compare scouted vs OSA |
| **Player pages** | Show "Your scout says X, OSA says Y" |
| **Accuracy validation** | Compare Acc=L scouted grades against OSA ground truth |
| **Draft board** | OSA ratings as secondary input when scout accuracy is low |

### Priority Assessment

Medium value, medium complexity. Most useful for leagues with scout accuracy issues.
Not needed if scouted ratings are Acc=VH/H for most players.

---

## Summary: Dependency Graph

```
/players (expanded)
├── Service time ──→ arb_model ──→ contract_value ──→ trade_calculator
│                                ──→ free_agents
│                                ──→ trade_targets
│                                ──→ payroll_summary
│                                ──→ depth_chart
├── Injury status ──→ trade_targets (flag)
│                  ──→ depth_chart (exclude from PT)
│                  ──→ team_needs (annotate)
│                  ──→ roster display (badge)
│                  ──→ player pages (banner)
├── Roster flags ──→ trade_targets (exclude DFA)
│                ──→ roster display (separate sections)
│                ──→ depth_chart (exclude DFA)
└── Draft info ──→ player pages (context)

/lgdata
├── Real standings ──→ trade_targets._classify_sellers()
│                  ──→ standings.py display
│                  ──→ team_queries.get_standings()
├── League hierarchy ──→ refresh._detect_league_structure() [simplification]
│                    ──→ MiLB stats (league ID discovery)
└── League state ──→ UI (season phase awareness)

/tradeblock
└── Available players ──→ trade_targets (flag/filter)
                      ──→ trade analyst (confirmed targets)

/playerbatstatsv2?lid=N (MiLB)
├── MiLB WAR ──→ fv_calc (confidence adjustment)
│            ──→ war_model (young player blending)
│            ──→ prospect_value (performance-validated surplus)
├── MiLB stats ──→ player pages (career stats by level)
│              ──→ farm_analysis (statistical context)
│              ──→ evaluation_engine (confidence signal)
└── Development tracking ──→ ratings_history (cross-reference)

/ballparks
└── Park factors ──→ stat normalization (context)
                 ──→ projections (adjusted OPS+)
                 ──→ player pages (park context)

/contract (expanded)
├── Vesting options ──→ contract_value (model exercise probability)
│                   ──→ free_agents (better classification)
├── Buyouts ──→ payroll_summary (accurate future cost)
└── Incentives ──→ contract display (show on player/team pages)
```

---

## Implementation Priority Matrix

| Change | Effort | Value | Risk | Recommended Phase |
|---|---|---|---|---|
| Service time (replaces estimation) | Low | **Very High** | Low | Phase 1b |
| Injury status (fills critical gap) | Low | **Very High** | Low | Phase 1a |
| Roster status flags | Low | High | Low | Phase 1c |
| Real standings (`/lgdata`) | Low-Med | High | Low | Phase 3c |
| Trade block | Low | Medium-High | Low | Phase 3a |
| MiLB stats (display only) | Medium | High | Low | Phase 2a-c |
| MiLB stats (model integration) | High | **Very High** | Medium | Phase 2d |
| Draft history | Low | Low-Med | Low | Phase 1d |
| Expanded contracts | Medium | Medium | Low | Phase 4 |
| Park factors | Low-Med | Low-Med | Low | Phase 3b |
| OSA ratings | Medium | Low-Med | Low | Phase 5 |

**Recommended first sprint:** Phase 1a + 1b + 1c (injury + service time + roster flags).
All three are schema additions to the same `players` table, share the same refresh code
change (`_upsert_players()`), and immediately improve every downstream system that
touches player status or contract analysis.
