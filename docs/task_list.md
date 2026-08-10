# Task List

Open work items. Completed items are in `docs/changelog.md`.

---

## Code Quality

- [x] **Additional ratings scales** — Support 1-20 scale (maps to 20-80 via linear `20 + (raw-1)/19 * 60`). Auto-detection: max rating ≤20 → 1-20, >80 → 1-100, else 20-80. Added to settings and onboarding dropdowns. **Done Session 57.**
- [x] **Snapshot test fragility** — `test_prospect_value.py` now stubs `dollars_per_war()` and `league_minimum()` via `unittest.mock.patch` to fixed values ($7M, $800K). Tests are fully deterministic regardless of `league_averages.json` state. Structural invariants (monotonicity, option ≥ base, SP > RP) plus a $/WAR scaling test. `test_player_utils.py` was already stable (exact FV grades depend on model_weights.json which only changes during recalibration, not refresh). **Done Session 57.**
- [x] **Evaluation engine docs** — Add `evaluation_engine.py` to `docs/tools_reference.md` and `docs/system_overview.md`. Document the `run()` entry point, pure computation functions, and the batch pipeline integration in `refresh.py`. **LOE: Low.**
- [ ] **Comprehensive regression testing** — Current test suite has structural gaps: (1) `test_evaluation_pipeline.py` tests hitter stat-blending end-to-end but not pitcher stat-blending (caught by missing-ERA bug, Session 67), (2) depth chart tests are smoke tests only ("returns dict?") with no behavioral assertions for edge cases (NULL OVR, failed evaluation engine), (3) `_league_pos_rankings` has zero direct test coverage, (4) no integration tests that simulate a league without OVR ratings (PPL-like scenario). Need: systematic regression test pass covering all major features with both happy-path AND edge-case assertions — especially cross-league scenarios (different scales, missing data). Priority areas: evaluation pipeline (all player types × stat blending), depth chart (fallback chains, future-year projections), surplus model, and trade calculator. **LOE: Medium-High.**
- [x] **Composite function consolidation** — `compute_composite_hitter` and `compute_composite_pitcher` now have a single canonical implementation in `evaluation/composite.py`. `data/evaluation_engine.py` imports from the package (was maintaining identical copies). Also consolidated: all helper functions (tool_transform, sub_mlb_floor_penalty, offensive/baserunning/defensive raw, compensation, stat_to_2080, pitcher_stat_to_2080, combined_value) and ceiling functions (compute_ceiling, compute_true_ceiling, compute_component_ceilings). Fixed PITCHER_TOOL_KEYS bug: package version now includes hra/pbabip (was missing). Eval engine reduced from 3493 → 2450 lines. **Done Session 79.**
- [x] **Model parameter calibration from data** — All tuning parameters extracted to `constants.py` with per-league override via `ModelWeights.get_param()`. Regression testing script (`scripts/model_regression.py`) validates composite vs WAR and calibrates aging curves + imbalance thresholds per-league. Defense weights recalibrated (SS/2B 0.05→0.15, 3B 0→0.10). Speed×contact synergy bonus added. Position-specific stat blend reduction for defense-first positions. `calibrate.py` fully rewritten: peak-age regression (27-32), WAR target, no floors, adaptive age range, HRA as pitcher movement proxy. EMLB hitter R²=0.667, VMLB hitter R²=0.397 (beats OVR). See `docs/evaluation_model_findings.md`. **Done Session 79.**

---

## Evaluation Engine Tuning

- [x] **Score compression further tuning** — Replaced elite tool bonus with piecewise tool transform (Session 47). Peak tool bonus for ceiling added (Session 48). 1.2× above-60 bonus restored (Session 52) to decompress top end. Composite range now 41-80 on eMLB, 37-76 on VMLB.
- [x] **Calibration on VMLB** — Full calibrate → evaluation_engine → fv_calc pipeline run on VMLB (Session 48). R²-blended defaults and raised min_weight floors produce stable cross-league weights (cosine similarity 0.98+).
- [x] **Carrying tool config review** — Percentile-based threshold (P85) now adapts to league distributions. VMLB calibrates all 7 positions (was only 2). Default merge fills gaps. Carrying tool bonus fully disabled (composite Session 56, ceiling Session 57) as redundant with tool transform (1.3× above 60) + peak tool bonus. **Done Session 57.**
- [x] **Tool interaction terms** — contact×eye, power×eye (hitters) and stuff×movement (pitchers) added Session 56, disabled Session 57. Were dead code (weights never written to `tool_weights.json`). Multivariate OLS shows residual correlation ~0.01 — collinear with individual tools (r=0.85-0.93), no explanatory power beyond linear model. Tool transform already captures the non-linearity. **Removed Session 57.**
- [x] **Stat blending: ERA- conversion and P95 calibration** — `_compute_stat_signal` now uses ERA- for pitchers (was FIP-). OOTP WAR is RA9-based; FIP systematically undervalued contact-management pitchers. P95 calibration still pending (slopes not yet wired). **Done Session 54 (ERA- part).**
- [x] **Evaluation model documentation** — `docs/evaluation_model.md` written with full pipeline diagram, formulas, constants, weight tables, and design rationale. **Done Session 52.**
- [ ] **SP underrepresentation in prospect rankings** — 18-20/100 SP in VMLB, 6/100 in EMLB. Investigated Session 49: OOTP generates tighter pitcher tool distributions (stuff/mov SD ≈ 11 vs hitter power SD ≈ 18), especially on 1-100 scale. HRA/PBABIP now included in pitcher composite for differentiation. VMLB representation is close to real baseball (~25-30%); EMLB is a league composition issue. **LOE: N/A — understood limitation.**
- [x] **SP FV undervaluation** — Investigated Session 50. Pitcher true_ceiling runs ~5-6 below game POT (e.g. 59 vs 65 for Schwarzenberg). Root cause: stuff rating already incorporates individual pitch quality, so the arsenal bonus partially double-counts. The 5-6 point gap is a stable, predictable offset that affects all SP equally. The FV system compensates through ceiling-credit — Schwarzenberg correctly grades at FV 50 Medium despite the ceiling compression. Increasing arsenal weight would risk double-counting. **Accepted as known limitation.**
- [x] **Per-league dynamic aging/development curves** — Gap closure rates, age runway tables, and expected gap tables now calibrated per league from cross-sectional OVR/POT data during `calibrate.py`. Stored in `model_weights.json`, loaded by `fv_model.py` with hardcoded VMLB-derived fallbacks. EMLB shows significantly higher closure rates than VMLB (e.g. hitter age 22: 0.91 vs 0.67). **Done Session 51.**
- [ ] **Cross-sectional survivorship bias** — Investigated Session 51. OOTP revises POT downward for players who fail to develop: only 22% of POT 50+ players at age 17 still have POT 50+ at age 26. Cross-sectional realization rates are inflated because busts appear as "low-POT, high-realization" players. Impact: gap closure rates (→ risk labels) are slightly optimistic for high-ceiling prospects. FV grade formula is unaffected (uses current snapshot only). Expected gap tables are correct for their cross-sectional use case. The bust_discount (0.55-0.85 by age, raised Session 52 from 0.30-0.60) partially mitigates. True fix requires longitudinal tracking across multiple seasons via `ratings_history`. Player coverage is near-complete at ages 17-21 (100%) but drops to 72% at age 26 due to unsigned players lacking ratings. **LOE: N/A until multi-season data accumulated.**
- [x] **FV 45+ inflation residual** — Now at ~10/org vs Fangraphs ~8.3. Accepted as a methodology difference: our tool-based composite is intentionally more generous than the game's OVR for developing players. The sub-MLB floor penalty (Session 51) reduced it from ~11 to ~10. Further reduction would require penalizing unproven tools, which conflicts with the design principle that composite = pure tool value. **Accepted at current level.**
- [x] **Composite-OVR divergence investigation** — Prospect composites run +7-8 above OVR. Investigated Session 51: the divergence IS the development gap — the difference between what tools say and what the player has proven. The game's OVR likely includes a "proven-ness" factor our composite intentionally omits. The sub-MLB floor penalty addresses the portion caused by disqualifying tool weaknesses. Remaining divergence is accepted as a design feature. **Closed — accepted as design choice.**
- [x] **COMPOSITE_TO_WAR calibration** — Now that composite scores exist on both leagues, run a second calibration pass to produce COMPOSITE_TO_WAR tables. These feed into `peak_war_from_score()` for surplus calculations. **Done Session 48.**

### FV Pipeline Migration to Composite/Ceiling Inputs

Legacy components designed for OVR/POT that need updating for composite/ceiling:

- [x] **`dev_weight()` curve tuning** — diff>=2 now gets 0.60 (was 0.50), rookie cap raised to 0.55. Fixes Joe Read-type undervaluation. **Done Session 49.**
- [x] **`effective_pot()` override removal** — Was dead code (column name mismatch). Removed from calc_fv. **Done Session 49.**
- [x] **`RP_POT_DISCOUNT` (0.8×) review** — Reduced to 0.85×. Old value double-counted RP devaluation in pitcher composite weights. FV 57 cap still limits top end. **Done Session 49.**
- [x] **Prospect discount + dev_weight interaction** — Resolved by dev_weight fix. Discount impact on FV is now ~2 points (acceptable). **Done Session 49.**

---

## Model & Data

- [x] **Arb salary calibration broken** — Fixed Session 56. Added WAR ≥ 1.0 floor, outlier cap, N ≥ 10 minimum, monotonic enforcement. Switched from flat-percentage model to raise-based (arb_model.arb_salary). Fixed discount mismatch (salary now discounted same as value). EMLB: {1:0.24, 2:0.24, 3:0.32}. **Done Session 56.**
- [x] **Arb salary constants recalibration** — Perpetual arb leagues now use a growth+ceiling model: `salary = min(min_sal + k × max(0, career_WAR - discount)^exp, ceiling_pct × WAR × $/WAR)`. Calibrated per league from actual 1-year contract data during `calibrate.py`. FA leagues remain on the existing exponential model (unchanged). **Done Session 60b.**
- [x] **Prospect WAR projection methodology** — Current approach uses `FV_TO_PEAK_WAR` lookup table (FV 50 → 2.0 WAR) with aging curve and ramp. Investigated Session 56: using ceiling→WAR would double-count upside (FV already encodes development probability). FV→WAR is correct in principle. Now passes continuous FV (pre-rounding) to `prospect_surplus` for interpolation within tiers. **Done Session 57.**
- [ ] **`estimate_control()` rounds service time incorrectly** — Uses `math.ceil(svc)` to convert fractional service years into an integer for the `6 - est_svc` remaining-control calculation. A player with 4.70 service years gets ceil'd to 5, yielding 1 year of control — but they actually have 2 arb years remaining (years 5 and 6). Should use `int(svc)` (floor) or `math.floor(svc)` instead. Impact: surplus for arb-eligible players with fractional service ≥ X.5 is understated by one full year of control. Affects `contract_value.py` output and `player_evaluation.surplus`. Discovered via Pat Pethel (4yr + 809 days → 4.70 svc → incorrectly shows 1yr control instead of 2). **LOE: Low.**
- [ ] **Platoon exposure modeling** — Current composite uses overall ratings; FV platoon penalty (-2/-3) partially addresses severe splits. Full platoon modeling would value platoon contributors in context (e.g., a LHH with 70 contact vs RHP and 30 vs LHP has real value as a platoon piece). Requires research: how to weight L/R splits, what threshold defines "platoon only," how to reflect platoon value in surplus. **LOE: Medium-High.**
- [x] **Unified evaluation model** — Single player value model (`evaluation/player_value.py`) replaces the old dual-system. Smoothly transitions from tool-based to stat-based projection via `stat_confidence` ramp. Legacy tables (`prospect_fv`, `player_surplus`) are now SQL views on `player_evaluation`. Web layer fully decoupled from `contract_value.py` and `prospect_value.py`. Design: `docs/unified_evaluation_design.md`. **Done Session 78. Remaining: parameter calibration from data (separate task).**
- [x] **Historical fielding stats** — Refresh now fetches fielding in the historical stats loop (up to 15 years) with a backfill pass for leagues that already had batting but were missing fielding. **Done Session 63.**
- [x] **Surface tool_only_score on MLB player pages** — MLB players now show pure tool score in parentheses next to the stat-blended composite. **Done Session 51.**
- [x] **Risk labels in prospect list templates** — Risk initials with color coding now rendered in league prospect list, team org overview, and team farm top 15. **Done Session 51.**
- [ ] **Projection model reuse** — `projections.py` now has calibrated OPS+, ERA/FIP, WAR, and ratings interpolation models. Explore using these in: (1) prospect pages — show projected MLB stat lines at current and future development stages, (2) draft evaluation — project draft prospect ratings into expected MLB production, (3) trade calculator — replace or supplement surplus model with projected stat lines for more intuitive valuation, (4) farm analysis — add projected stat context to scouting summaries. **LOE: Varies per integration.**
- [ ] **Org overview scaffold** — automate the org overview report template pulling farm summary, roster summary, contracts, surplus rankings, extension priorities. Lower priority — report structure changes between evaluations.
- [ ] **Surplus model validation suite** — systematic validation of prospect and contract surplus models against real league data. Subtasks: (1) sanity-check top/bottom 25 prospects by surplus — do rankings match intuition? (2) cross-position trade equivalence — test model-fair swaps (FV 55 SP vs SS, FV 50 C vs 1B, elite RP vs mid SP) for smell test; (3) prospect→MLB crossover — compare AAA prospect value to same player's contract value after debut, check for discontinuities; (4) age sensitivity — verify younger prospects properly valued over older ones at same FV/Pot; (5) validate against actual league trades — run completed trades through calculator; (6) FA contract validation — compare $/WAR and market value projections to actual free agent signings; (7) SP/hitter arb salary spot-checks — extend the RP arb validation to other positions. **LOE: Medium.**

---

## Web UI — Team Page

- [x] **Stale players on org overview page** — Fixed: org overview queries now cross-reference `players.team_id/parent_team_id` to filter out traded/released players whose prior-year stats still reference the team. **Done Session 61.**
- [ ] **Playing time model edge cases** — current model is 77% within 100 PA, 92% within 200 PA across 4 test teams. Known gaps: (1) utility players with <3 games at every position get sprayed across the diamond via ratings fallback; (2) DH-primary detection uses 50% batting-fielding gap threshold — teams that rotate DH duties without a primary DH leave the slot empty; (3) bench players squeezed below realistic PA when starter is a multi-position player claiming the slot; (4) **global position optimization** — model assigns positions per-player, not globally. Would require a constraint-satisfaction or linear-programming approach to maximize total team WAR across all positions simultaneously. **LOE: Low-Medium per item, High for #4.**
- [ ] **Positional strength/weakness map** — starter Ovr + surplus vs league average at each position. CLI version done (`team_needs.py`). Web UI version (visual map on team page) still pending. **LOE: Medium.**
- [ ] **Pipeline view** — MLB starter → AAA depth → top prospect chain per position bucket. **LOE: Medium.**
- [ ] **Division rival comparison** — side-by-side surplus/farm/record for division teams. **LOE: Medium.**
- [ ] **Roster projection** — project next year's roster from contracts, arb estimates, FA departures. **LOE: High.**

---

## User Feedback (Beta Testers)

Items identified from beta tester usage and conversations.

### High Priority (Bugs)
- [x] **Org page — lineup card blank** — Starting lineup card on org overview shows blank for hitters (pitchers section was previously fixed). Root cause: `fielding_stats` empty for leagues onboarded mid-season (refresh only fetched current year). Fix: (1) refresh now always fetches prior-year fielding, (2) org overview falls back to `batting_stats` + `players.pos` when fielding is unavailable. Reported by Koba. **Done Session 62.**
- [x] **IP display uses float formatting instead of baseball notation** — Fixed Session 68. Applied `fmt_ip` filter to Jinja server-rendered view and added `fmtIp()` JS helper for client-rendered split view. Reported by Koba. **Done Session 68.**
- [x] **Split percentile qualification threshold too low** — Split PA/IP thresholds now scale with season progress (0.7×games for hitters, 0.25×games for pitchers). `pctile-unqualified` CSS class handles visual dimming for sub-threshold samples. **Done Session 75.**

### Feature Requests
- [ ] **CSV export for minor league rosters** — Extend CSV export to minor league team pages (individual affiliates) or create an "all minor leagues" org view with export. Enables external analysis of player development progression across the farm system. Reported by Koba. **LOE: Low-Medium. DONE.**
- [ ] **Development tab refinements** — (1) Rating change summary bullets alongside charts (like StatsPlus), (2) Level promotion markers on timeline x-axis, (3) Responsive chart width for narrow screens. **LOE: Medium.**
- [x] **CSV export from draft board** — Add export button to draft board that outputs the current filtered/sorted view as a CSV. Two use cases: (1) export remaining undrafted players for external LLM analysis, (2) export computed columns (FV, composite ceiling, surplus) to enrich external datasets. Should respect active filters (position, level, hide drafted). Reported by Koba. **Done Session 65.**
- [ ] **Split-based composite ratings on team roster** — Show composite/OVR recalculated using vs-LHP or vs-RHP ratings instead of overall ratings. On the team roster page, a "vR" / "vL" toggle would swap the rating column to show handedness-specific composites alongside the split counting stats. Enables platoon construction: identify which hitters are significantly better/worse against a specific hand. Requires: (1) evaluation engine to accept split ratings as input, (2) split stats (already fetchable via split_id 2/3) displayed alongside. Reported by Koba. **LOE: Medium.**
- [x] **Positional rankings: vs-average and component breakdown** — ±Avg column (vs positional median, color-coded green/red) for all positions. OFF/DEF component breakdown for hitter groups (uses actual positional defensive rating). Pitchers get ±Avg only. **Done Session 75.**
- [ ] **Custom pool / prospect explorer** — Allow users to upload or select a custom group of players (e.g., their full minor league system) and view them in a sortable/filterable table similar to the draft board. Users want to sort their org's players by specific tools, composite, ceiling, or FV without needing the draft pool context. Could reuse the draft board table component with a different data source. Reported by Koba. **LOE: Medium.**
- [ ] **Platoon finder** — Filter/search for players with significant platoon splits (strong vs LHP or vs RHP). Surface players meeting a platoon threshold across MLB and MiLB rosters, enabling platoon lineup construction. Criteria: large gap in L/R contact or power ratings (e.g., 15+ point split) AND the strong side is above average (55+). Display: list with name, bats, strong-side stats, weak-side stats, platoon label. Could live as a filter on the team roster page or as a standalone tool. Reported by Koba. **LOE: Medium.**
- [ ] **Fielding stats on evaluated position** — Show observed ZR, fielding %, and games played at each position the player has manned (from `fielding_stats` table). Displayed alongside the positional evaluation section so users can validate whether a position switch recommendation has real defensive track record behind it. Useful for utility players and position-change candidates. Reported by Koba. **LOE: Low-Medium.**

### Research / Model Tuning
- [x] **Prospect surplus inconsistency: fv_calc uses true_ceiling instead of OOTP Pot for scarcity** — Investigated Session 72. Using true_ceiling is correct: OOTP Pot changes over time (78% of players) and doesn't dictate sim outcomes (individual tools do). Our model's ceiling is the better production predictor. The actual issue was (1) the web UI recalculating surplus with rounded integer FV instead of `fv_continuous`, causing different numbers in header vs panel, and (2) the default scarcity table having breakpoints calibrated for the Pot scale. Fix: added `fv_continuous` column to `prospect_fv`, unified all calculation paths, recalibrated default scarcity table for composite ceiling scale. EMLB's calibrated table in `model_weights.json` was already correct. **Done Session 72.**
- [ ] **Extreme profile inflation** — Observation that players with one extreme tool + otherwise poor ratings (30/30/90 control pitcher, or 30 contact/90 power hitter) grade higher than balanced profiles in composite/FV. Other GMs tend to favor balanced players. The contact floor penalty partially addresses this for power-only hitters. Investigate: (1) do extreme profiles underperform their composite in-game? (2) would a general "tool imbalance" penalty be warranted? (3) could this be addressed via the existing draft settings sliders (contact floor, etc.) without formula changes? Reported by Koba. **LOE: Medium (research).**

### UX / Onboarding
- [x] **Draft pool import instructions** — Step-by-step OOTP export instructions now shown inline in the Upload Pool modal on the draft board. **Done Session 75.**

---

## Web UI — League Page

- [x] **Positional rankings page** — League-wide page showing top players by position group (C, IF, OF, SP, RP), split into MLB and prospect sections. For MLB: rank by composite/WAR. For prospects: rank by FV/surplus. **Already implemented.**
- [ ] **Power rankings trend indicators** — store historical rank snapshots (per eval_date or game_date) and show ▲/▼/— movement arrows next to rank. Needs: new DB table or JSON file for rank history, delta calculation. **LOE: Medium.**
- [ ] **League news / milestone ticker** — horizontal strip between standings and power rankings showing notable milestones (e.g. "Player X: 3 HR from 50"). Needs: milestone detection logic from stats. **LOE: Medium-High.**

### Draft Tab — Future Improvements
- [x] **Draft board: position filter breaks sorting** — Fixed: added `initSort(table)` to `sort.js` that rebinds click handlers after dynamic thead replacement. `renderDraftBoard()` already called it but the function didn't exist. **Done Session 61.**
- [x] **Draft prospect ranking** — sort by FV (primary) then surplus (secondary). Web UI already uses this sort. CLI `draft_board.py pick` uses draft value sort (FV + ceiling bonus + ctl penalty). **Done Session 53.**
- [x] **ADP and draft simulation** — Expected draft position based on POT rank. Draft sim with randomized other-team picks. Urgency-greedy list building for auto-draft upload. Org needs as tiebreaker. Web UI sim + upload buttons. **Done Session 55.**
- [ ] **"My List" draft board builder** — Sidebar panel where users build a ranked draft list by clicking prospects from the board. Features: (1) "Add to My List" button per prospect row, (2) reorderable list via drag-and-drop or up/down arrows, (3) localStorage persistence across sessions, (4) export as commissioner format (numbered list with game position + name) or StatsPlus upload format (plain text IDs, one per line). **LOE: Medium.**
- [x] **Visual flag badges** — Show Acc=L warning badge (⚠) and Extreme risk badge (☠) directly in the draft board table rows next to player name. **Done Session 57.**
- [x] **Sleeper/value flags** — Implemented via ADP system (Session 55). POT rank vs FV rank gap produces Sleeper/Value/Goes Early/Reach labels. Displayed in draft board table and CLI output. **Done Session 55.**
- [x] **Advanced filtering** — Collapsible tool filter panel on draft board with min-threshold inputs for potential tools (Con/Pow/Eye/Spd for hitters, Stf/Mov/Ctrl for pitchers) plus Pot and FV minimums. Filters apply in real-time. **Done Session 57.**
- [ ] **Post-draft grades** — team haul summaries after draft completion. **LOE: Medium.**
- [ ] **Bonus demand display + draft value integration** — Parse "Bonus Demand" column from uploaded draft pool CSV (if present). Store in `draft_pool.json` alongside player IDs. Display demand amount on the draft board table. Explore factoring demand into draft value calculations (over-slot demand reduces pick attractiveness, under-slot creates value). Would also inform auto-draft list generation — a player demanding $5M at a $2M slot is less draftable than their pure talent ranking suggests. **LOE: Medium.**
- [x] **Org needs for perpetual arb leagues** — Implemented weakness-based alternative: compares team's positional starter composite vs league median, gated by farm FV 50+ depth. SP uses 3rd-best starter (rotation depth indicator). Thresholds: +2 if ≥5 below median with no farm help, +1 if ≥2 below with no farm help, +1 if ≥8 below even with farm help. FA leagues unchanged (departure-based). **Done Session 60b.**
- [ ] **Positional versatility in depth calculations** — The draft board depth indicator (`get_draft_org_depth`) only counts players in their primary bucket, ignoring defensive versatility. A team with 5 SS/2B players who all have 65+ Pot3B shows "red" at 3B because nobody's *primary* bucket is 3B. Similarly, COF players with 50+ PotCF don't count toward CF depth. Fix: count players who qualify defensively at a position (e.g., pot rating ≥ 45) toward that position's depth, possibly at a discounted weight. This would give a much more accurate picture of true organizational depth at each position. **LOE: Medium.**
- [x] **League-aware defensive viability in bucket assignment** — `assign_bucket()` fallback now uses calibrated OLS positional models (R² 0.92-0.96) trained from each league's own defensive tool → positional rating data. Models predict estimated positional grades from IFR/IFA/IFE/TDP (infield), OFR/OFA/OFE (outfield), CArm/Blk/Frm (catcher), and Height (1B). Always uses potential ratings (age removed as factor). Stored in `model_weights.json` under `POSITIONAL_MODELS`, recalibrated during `calibrate.py`. **Done Session 66.**

---

## Web UI — Player Page

- [x] **Player development history chart** — SVG line charts showing rating trajectories over time using `ratings_history` snapshots. Tabbed by tool group (offensive, defensive, pitches). Current + potential lines with hover dots. **Done.**
- [x] **Percentile rankings: offseason display + multi-year history** — Percentile panel now auto-falls back to prior year during offseason. Year selector dropdown on all panels. New "Advanced" tab on player pages shows color-coded percentile history table (blue→white→red), fielding history by position, PA/IP context, career averages, value/percentile toggle. **Done Session 63.**
- [ ] **Level-relative ranking** — Show where a prospect ranks among all players at their level by OPS+/ERA-. Display as percentile bar ("Top 5% at level") on player page and as a column in prospect lists. Provides context for individual stat lines. **LOE: Low.**
- [ ] **Development pace indicator** — Compare a player's level progression to expected pace for their profile. Badges: "⚡ Fast Track" (1+ levels ahead), "📈 On Pace", "⚠️ Stalled" (1+ levels behind with high PA). Show on player page and farm overview. **LOE: Medium.**
- [ ] **Farm system health dashboard** — Aggregate MiLB stat signals at the org level: promotion-ready count, performance trajectory breakdown (rising/steady/falling), stats-vs-tools alignment chart. Enhances team farm page with system-level insight. **LOE: Medium.**
- [ ] **Historical prospect comparisons** — Show comparable players at the same age+level who went on to succeed/fail. "73% of similar performers reached MLB within 2 years." Requires accumulated multi-season data. **LOE: High.**

---

## Web UI — Navigation

- [x] **Minor league team pages** — ~~extend `/team/<id>` to affiliate team IDs~~ Done. Notable player filter tuned: "young for level" now requires ceiling ≥ 45 (was age-only). Fixes Intl/Rookie levels where every teenager qualified. Results: Intl 9.7→5.9/team, Rookie 6.2→4.7/team. Upper levels unchanged (~12-15/team, appropriate for prospect-heavy affiliates). **Done Session 57.**

---

## Web UI — Visual Overhaul

- [ ] **In-app help system** — Add contextual help for key concepts (FV, risk, composite, surplus, etc.). Options: (1) "?" icon next to metrics that opens a tooltip/popover with explanation, (2) a slide-out help panel accessible from the nav, (3) a glossary page. Current tooltips via `title` attributes cover basics; a richer system would improve onboarding for new users. **LOE: Medium.**
- [x] **Player page injury/status banner** — DL/60-Day DL/Day-to-Day/Injured/Out Indefinitely/DFA/Waivers classifications with color-coded banner. Data from `players` table. Team roster badges also fixed to distinguish DL vs INJ. **Done Session 73.**
- [ ] **Team logos** — add team logos to team pages and player pages. Source or generate logo assets for all 34 MLB teams. Display in page headers, standings, and anywhere team identity appears. **LOE: Low-Medium.**
- [ ] **UI overhaul exploration** — current layout is functional but generic. Investigate alternative visual styles, layouts, and design patterns to give the app more personality. **LOE: Medium-High.**

---

## Data & Research

- [x] **Team vs team history** — `get_head_to_head_matrix()` query function returns full NxN W-L matrix. Displayed on the league page Standings tab as a color-coded H2H grid. **Done Session 60b.**
- [ ] **Accuracy-scaled draft penalty research** — Investigate whether Acc=L players with elite ceilings (70+) hit at a higher rate than Acc=L players with modest ceilings. If so, scale the accuracy penalty in `draft_value()` by ceiling magnitude (e.g., `penalty = base × (1 - (ceiling - 55) * 0.02)`). Requires multi-draft historical data to validate. Context: eMLB 2033 draft analysis showed Gabriel Brown (Acc=L, 97 pow / 100 eye potential) was undervalued by our board due to flat -2 penalty. **LOE: Medium (data collection + validation).**
- [x] **SP stamina risk in draft valuation** — Investigated Session 60b. Data shows: (1) OOTP's SP/RP boundary is ~stm 40 in both EMLB and PPL; (2) low-stm SP who stay as starters produce ~17% less WAR/yr in EMLB (1.34 vs 1.62) but no penalty in PPL; (3) `assign_bucket()` already converts 35% of low-stm SP to RP bucket → -5 discount fires; (4) composite already penalizes stm<40 by -0.75; (5) stamina is developable for young pitchers. Conclusion: no `draft_value()` penalty needed — existing composite penalty + bucket assignment already captures the risk at appropriate magnitude. Additional penalty would double-count. **Closed — no change.**

---

## API Integration Roadmap

New StatsPlus API data discovered July 2026. Implementation plan in dependency order.
See `docs/client_reference.md` for full field documentation.

### Phase 1 — Expanded Player Fields ✅ (Completed Session 69)

The `/players` endpoint now returns 45 additional fields. Schema migration adds
30+ columns to `players` table; `_upsert_players()` stores all fields on refresh.

- [x] **1a. Injury status** — `injury_is_injured`, `injury_dl_left`, `injury_left`, `is_on_dl`, `is_on_dl60`, `dl_days_this_year`. DL/DFA/WVR badges on team roster pages. Trade targets shows 🏥 annotations and skips DFA'd players. `--exclude-injured` CLI flag.
- [x] **1b. Service time** — `mlb_service_years`, `mlb_service_days`, `mlb_service_days_this_year`, `pro_service_years`, `pro_service_days`. `arb_model.estimate_service_time()` reads exact values with games-based fallback. `free_agents.py` uses exact service for arb/FA classification.
- [x] **1c. Roster status flags** — `is_on_waivers`, `designated_for_assignment`, `is_on_secondary`, `free_agent`, `was_traded`, `days_on_waivers`, `days_on_waivers_left`, `has_received_arbitration`. DFA'd players filtered from trade targets.
- [x] **1d. Draft history** — `draft_year`, `draft_round`, `draft_pick`, `draft_overall_pick`, `draft_team_id`. Stored for 10,851 players.
- [x] **1e. Demographics** — `date_of_birth`, `weight`, `nation_id`, `uniform_number`. Stored alongside existing player fields.

### Phase 2 — Minor League Stats (Partially Complete)

Stats available for all minor league levels via `lid` parameter. Storage and refresh
pipeline complete; display and model integration remaining.

- [x] **2a. `/lgdata` client method** — `get_lgdata()` in client.py. Used during refresh to discover all league IDs and hierarchy. League structure stored in `league_settings.json`.
- [x] **2b. Schema: `league_id` column** — Added to `batting_stats`, `pitching_stats`, `fielding_stats`. NULL = MLB (backward compatible). MiLB rows use the league ID from `/lgdata`.
- [x] **2c. Refresh pipeline — MiLB stat pulls** — Fetches batting+pitching for all discovered MiLB leagues (13 leagues, ~10,600 rows total). Current year only. Adds ~15-20s to refresh time. Non-fatal on failure.
- [x] **2d. MiLB stats in prospect evaluation** — MiLB performance integrated into composite scoring (level-discounted blend), Performance-Adjusted Ceiling (PAC), and risk modifier. Calibrated from VMLB 2029-2034 regressions. Historical MiLB stats (5yr backfill) stored for multi-year analysis. **Done Session 74.**
- [x] **2e. MiLB stats on player pages** — Minor League Stats section on player page Stats tab. Batting and pitching tables with league names from `league_settings.json`. Hitter and pitcher pages both supported.

**✅ MiLB stats contamination — RESOLVED.** All MLB-only query sites use `mlb_*` views
(created Session 70). Full audit confirmed in Session 74: 100+ query sites verified,
evaluation engine, WAR model, calibration pipeline, and percentiles all correctly isolated.
One minor fix applied (fielding percentile year resolution).

**Known remaining issues:**
- Percentile rankings support per-level filtering (done Session 73)
- Offseason WAR projection still shows for completed season (display logic issue)

### Phase 3 — New Endpoints ✅ (3a, 3c completed Session 70)

Client methods implemented; trade block and standings integrated.

- [x] **3a. Trade block** — `trade_block` table populated during refresh. `trade_targets.py` shows 📋 annotation, `--on-block` flag filters to confirmed-available players.
- [ ] **3b. Ballparks** — `get_ballparks()` exists in client.py. Need: store park factors, use in stat normalization, display on team pages. **LOE: Low-Medium.**
- [x] **3c. League data (`/lgdata`) for standings** — `standings` table stores real W-L-GB for all teams. `_classify_sellers()` uses real wins. `standings.py` shows pythagorean + actual with delta.

### Phase 4 — Expanded Contract Data ✅ (Completed Session 70)

All 13 additional contract fields stored. Vesting options, buyouts, and incentives
integrated into trade targets, free agents, and player page contract display.

- [x] **4a. Option/vesting fields** — `last_year_vesting_option`, `next_last_year_team/player/vesting_option`, `last_year_option_buyout`, `next_last_year_option_buyout`. Trade targets shows VESTING status. Free agents shows buyout amounts.
- [x] **4b. Incentive fields** — `minimum_pa/ip` + bonuses, `mvp_bonus`, `cyyoung_bonus`, `allstar_bonus`. Player page contract data includes non-zero incentives dict.

### Phase 5 — OSA Ratings (Lower Priority)

- [ ] **5a. OSA ratings fetch** — Add `&osa=1` support to ratings export. Enables dual-rating comparison (scouted vs OSA) for accuracy assessment. Store in separate table or as additional columns. **LOE: Medium.**

### Implementation Notes

- Phase 1 complete — all player fields stored and used by downstream tools
- Phase 2 complete — MiLB stats ingested AND displayed on player pages. Model integration (2d) deferred.
- Phase 3a + 3c complete — trade block and real standings integrated into trade tools
- Phase 4 complete — all contract option/incentive fields stored and surfaced
- Phase 3b (park factors) and Phase 5 (OSA ratings) deferred — lower priority
- Refresh time: ~3:15 with MiLB stats + trade block + standings (was ~3:00). All new fetches non-fatal on failure.
- All changes maintain backward compatibility (NULL defaults for new columns, migrations idempotent)

---

## Performance & Code Quality

- [ ] **Page load performance optimization** — Pages are slow due to on-the-fly calculations (composite scores, surplus, percentiles, depth chart projections) on every request. Investigate and implement caching strategies: (1) pre-computed values stored in DB during refresh (already done for FV/surplus — extend to composites, percentiles, positional rankings), (2) Flask response caching (e.g., `flask-caching` with TTL tied to last refresh timestamp), (3) query-level memoization for expensive repeated calls within a single request, (4) identify the slowest pages/queries via profiling (`cProfile` or middleware timing) and target those first. Goal: sub-1s page loads for common pages (team roster, player page, prospect list). **Partial progress Session 76:** request-scoped connection sharing (via `_ScopedConnection` wrapper), `_get_state()` caching (16→1 file reads), `_get_eval_date()` caching (10→1 queries). **LOE: Medium (remaining items).**
- [x] **Codebase quality review & refactor** — Systematic audit and restructure into proper Python package (`src/statsplusplus/`). Complete: typed models, pure evaluation layer, config/client/data packages, web request-scoped context, Flask blueprints, CLI entry points, all shims eliminated. Package is fully self-contained. `scripts/` contains only CLI tools importing from the package. **Done Session 77.**

---

## Long-term

- [ ] **External data directory** — Move user data (`app_config.json`, league dirs, history) out of the install folder to the OS-standard location (`%LOCALAPPDATA%\StatsPlusPlus` on Windows, `~/.local/share/statsplusplus` on Linux, `~/Library/Application Support/StatsPlusPlus` on macOS). Enables zero-risk updates (delete old folder, extract new zip). Add "Data Location" display in Settings. Refactor `league_context.py` and launchers to resolve the external path. Currently all data lives in `data/` within the install — works but means updates require manually copying the data folder.
- [ ] **Phase 2 — Interactive tools** — trade workbench, prospect explorer, free agent planner. Trade analysis CLI toolset complete (Session 44): `trade_targets.py`, `trade_assets.py`, `team_needs.py`, `trade_calculator.py` improvements, `trade-analyst.md` agent. Remaining: web-based trade workbench UI, prospect explorer UI, FA planner UI.
- [ ] **Phase 3 — AI assistant (BYOK)** — Chat interface with league/team context, powered by the user's own LLM subscription. User links their API key (Claude, OpenAI, etc.) via Settings. We supply the agent system prompt (contextually loaded with league state, roster, farm, standings — similar to the existing `trade-analyst.md` / `beat-reporter.md` steering files). UI: chat bubble or slide-out panel available on any page. The agent acts as an assistant GM — can answer questions about the roster, suggest trades, evaluate prospects, explain metrics. **Open questions:** (1) How to authenticate — API key in settings vs OAuth vs browser extension proxy? (2) Context window management — how much league data to inject per conversation (full DB dump vs targeted retrieval)? (3) Tool use — can the agent call CLI tools server-side, or is it pure chat with pre-loaded context? (4) Cost transparency — users pay per-token, need visibility into usage. (5) Which providers to support first (Anthropic API is simplest, OpenAI similar, local models via Ollama?). **LOE: High (research + implementation). Feasibility study first.**
- [ ] **Discord integration** — ~~Set up a Discord channel for Stats++ users (development updates, feedback, feature requests).~~ Server created, webhook posting implemented (patch notes via `discord_post.py`), widget on settings page. Remaining: (1) inbound integration — create Discord bot application for read token, build `discord_sync.py` script to pull messages from #feature-requests and #bug-reports channels into structured session context, (2) explore slash commands for lightweight queries (/prospect, /standings). **LOE: Medium.**
- [ ] **Code architecture cleanup** — connection context manager, consistent row_factory, route-level error handling. Ongoing incremental work.
- [ ] **Stat/ratings divergence flag** — surface confidence signal when `stat_peak_war` and `peak_war_from_ovr` differ by >1.5 WAR in trade calculator output. Player page already shows over/underperformance; this extends it to trade evaluation context.

- [x] **Comp-based FV validation tool** — CLI tool (`scripts/comp_validate.py`) finds MLB players with similar tool profiles and shows WAR distribution. Web UI shows "Ceiling Profile" summary on prospect pages. Uses potential ratings, rate-normalized WAR (per 600PA/180IP), year filtering. Data limitation: uses current ratings vs historical stats; most reliable for recent seasons. **Done Session 56.**
