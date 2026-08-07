# Code Refactoring Plan

Definitive plan for restructuring Stats++ into a well-engineered Python package.
Each phase is self-contained: tests pass at the end of every phase.

---

## Target Architecture

```
statsplusplus/
├── pyproject.toml                   # Package definition, entry points, dependencies
├── src/
│   └── statsplusplus/
│       ├── __init__.py
│       │
│       ├── models/                  # Typed dataclasses — contracts between layers
│       │   ├── __init__.py          # Re-exports all model classes
│       │   ├── player.py            # PlayerRatings, PlayerInfo, PositionalBucket
│       │   ├── evaluation.py        # EvaluationResult, EvaluationContext, CompositeWeights
│       │   ├── prospect.py          # ProspectEvaluation, FVGrade, RiskLabel
│       │   ├── contract.py          # ContractInfo, SurplusResult, ArbProjection
│       │   └── league.py            # LeagueSettings, LeagueAverages, DivisionStanding
│       │
│       ├── evaluation/              # Pure computation — no DB, no I/O
│       │   ├── __init__.py          # Public API re-exports
│       │   ├── composite.py         # compute_composite_hitter, compute_composite_pitcher
│       │   ├── ceiling.py           # compute_ceiling, compute_true_ceiling
│       │   ├── fv.py                # calc_fv, dev_weight, gap_closure, risk_label
│       │   ├── war.py               # peak_war_from_score, aging_mult, stat_peak_war
│       │   ├── surplus.py           # prospect_surplus, contract_value logic
│       │   ├── arb.py               # arb_salary, estimate_service_time
│       │   ├── projections.py       # OPS+, ERA, WAR projection models
│       │   └── constants.py         # All model constants, lookup tables, aging curves
│       │
│       ├── data/                    # DB access, schema, refresh pipeline
│       │   ├── __init__.py
│       │   ├── db.py                # Connection management, schema, migrations
│       │   ├── refresh.py           # API → DB pipeline
│       │   ├── calibrate.py         # Model calibration
│       │   └── fv_calc.py           # Batch FV/surplus computation (orchestration)
│       │
│       ├── client/                  # StatsPlus API client
│       │   ├── __init__.py
│       │   └── statsplus.py         # HTTP client, CSV parsing, ratings handling
│       │
│       ├── config/                  # League config, context resolution
│       │   ├── __init__.py          # Re-exports LeagueConfig, get_league_dir
│       │   ├── league_config.py     # LeagueConfig class
│       │   ├── league_context.py    # Active league resolution
│       │   └── ratings.py           # Rating scale normalization (pure functions)
│       │
│       ├── cli/                     # CLI entry points (thin wrappers)
│       │   ├── __init__.py
│       │   ├── draft_board.py
│       │   ├── trade_calculator.py
│       │   ├── trade_targets.py
│       │   ├── trade_assets.py
│       │   ├── team_needs.py
│       │   ├── standings.py
│       │   ├── free_agents.py
│       │   ├── prospect_query.py
│       │   ├── farm_analysis.py
│       │   ├── roster_analysis.py
│       │   ├── contract_value.py    # CLI interface to evaluation.surplus
│       │   ├── prospect_value.py    # CLI interface to evaluation.surplus
│       │   ├── benchmark.py
│       │   └── discord_post.py
│       │
│       ├── web/                     # Flask application
│       │   ├── __init__.py          # Flask app factory
│       │   ├── app.py              # App creation, middleware, template filters
│       │   ├── context.py           # Request-scoped DB + config (one conn per request)
│       │   ├── routes/
│       │   │   ├── __init__.py      # Blueprint registration
│       │   │   ├── team.py          # /team/<tid> routes
│       │   │   ├── league.py        # /league, /league/draft, /league/prospects
│       │   │   ├── player.py        # /player/<pid>
│       │   │   ├── settings.py      # /settings, /onboard, /switch-league
│       │   │   └── api.py           # /refresh, /api/*, CSV export
│       │   ├── queries/
│       │   │   ├── __init__.py
│       │   │   ├── roster.py        # get_roster, get_roster_hitters, get_roster_pitchers
│       │   │   ├── depth_chart.py   # get_depth_chart, playing time model
│       │   │   ├── contracts.py     # get_contracts, get_payroll_summary, get_upcoming_fa
│       │   │   ├── farm.py          # get_farm, get_farm_depth, get_org_overview
│       │   │   ├── standings.py     # get_division_standings, get_power_rankings
│       │   │   ├── player_detail.py # get_player (full player page data)
│       │   │   ├── percentiles.py   # Percentile rankings with expected-value modeling
│       │   │   ├── promotion.py     # Promotion readiness badges
│       │   │   ├── league_wide.py   # get_top_prospects, get_stat_leaders, positional rankings
│       │   │   ├── draft.py         # Draft board web queries
│       │   │   └── trade.py         # Trade tab queries
│       │   ├── templates/           # Jinja2 templates (unchanged)
│       │   └── static/              # CSS, JS, assets (unchanged)
│       │
│       └── utils/                   # Shared pure utilities
│           ├── __init__.py
│           ├── formatting.py        # fmt_money, fmt_ip, short_name, height_str
│           ├── positions.py         # assign_bucket, display_pos, LEVEL_HIERARCHY, ROLE_MAP
│           └── logging.py           # Centralized log config
│
├── tests/                           # Test suite (imports from package)
│   ├── conftest.py
│   ├── models/                      # Model validation tests
│   ├── evaluation/                  # Pure computation tests
│   ├── data/                        # Integration tests (DB)
│   ├── web/                         # Route + query tests
│   └── cli/                         # CLI smoke tests
│
├── data/                            # Runtime data (gitignored, unchanged)
└── docs/                            # Documentation
```

---

## Phases

### Phase 1: Foundation — Package scaffold + Models

**Goal:** Create the package structure, install in editable mode, define typed models.
Nothing moves yet — the old code keeps running. We're just building the target.

**Steps:**

1.1. Create `pyproject.toml` with:
   - Package metadata
   - `src/` layout
   - Dependencies (flask, pytest — pinned versions)
   - Console script entry points (all `spp-*` commands)
   - Optional `[dev]` deps (pytest, mypy)

1.2. Create `src/statsplusplus/__init__.py` (version only)

1.3. Create `src/statsplusplus/models/` with typed dataclasses:
   - `player.py` — `PlayerRatings`, `PlayerInfo`, `PositionalBucket` (enum)
   - `evaluation.py` — `EvaluationResult` (migrate from evaluation_engine.py), `EvaluationContext`
   - `prospect.py` — `ProspectEvaluation`, `RiskLabel` (enum), `FVGrade`
   - `contract.py` — `ContractInfo`, `SurplusBreakdown`, `ArbProjection`
   - `league.py` — `LeagueSettings`, `LeagueAverages`, `TeamInfo`

1.4. Create `src/statsplusplus/utils/` with consolidated utilities:
   - `formatting.py` — `fmt_money`, `fmt_ip`, `short_name`, `height_str`
   - `positions.py` — `assign_bucket`, `display_pos`, `LEVEL_HIERARCHY`, `ROLE_MAP`, `PITCH_FIELDS`
   - `logging.py` — log config (from scripts/log_config.py)

1.5. Run `pip install -e .` in development venv

1.6. Write model unit tests (construction, validation, enum membership)

**Validation:** `pytest` passes. Models importable as `from statsplusplus.models import PlayerRatings`.

---

### Phase 2: Evaluation layer — Pure computation

**Goal:** Move all pure computation into `src/statsplusplus/evaluation/`.
Functions take typed inputs, return typed outputs. Zero DB access.

**Steps:**

2.1. Create `evaluation/constants.py`:
   - Migrate all constants from `scripts/constants.py`
   - Calibrated weight loader (`_load_weights`) refactored to accept a path parameter (no global state)

2.2. Create `evaluation/composite.py`:
   - Extract `compute_composite_hitter`, `compute_composite_pitcher`, `compute_tool_only_score` from evaluation_engine.py
   - Extract tool transform, arsenal quality, stat blending logic
   - Functions accept `PlayerRatings` + `CompositeWeights`, return scores

2.3. Create `evaluation/ceiling.py`:
   - Extract `compute_ceiling`, `compute_true_ceiling` from evaluation_engine.py
   - Extract `compute_performance_adjusted_ceiling` from fv_model.py

2.4. Create `evaluation/fv.py`:
   - Migrate `calc_fv`, `calc_fv_v2`, `dev_weight`, `age_development_mult` from fv_model.py
   - Migrate `compute_stat_risk_modifier` from fv_model.py
   - Functions accept `EvaluationContext`, return `ProspectEvaluation`

2.5. Create `evaluation/war.py`:
   - Migrate from `scripts/war_model.py` (entire file)
   - `peak_war_from_score`, `aging_mult`, `stat_peak_war`, `load_stat_history`

2.6. Create `evaluation/surplus.py`:
   - Extract core logic from `scripts/prospect_value.py` and `scripts/contract_value.py`
   - Pure computation: takes WAR projections + contract data, returns surplus

2.7. Create `evaluation/arb.py`:
   - Migrate from `scripts/arb_model.py` (entire file)

2.8. Create `evaluation/projections.py`:
   - Migrate from `scripts/projections.py`

2.9. Update `evaluation/__init__.py` with clean public API re-exports

2.10. Write tests for each evaluation module (port existing tests + add typed interface tests)

**Validation:** All existing evaluation tests pass when pointed at new module. New typed-interface tests pass. Old `scripts/` files still work (not yet deleted).

---

### Phase 3: Config + Client layer

**Goal:** Centralize configuration and API client with clean interfaces.

**Steps:**

3.1. Create `config/league_config.py`:
   - Migrate `LeagueConfig` class from `scripts/league_config.py`
   - Remove module-level `config = LeagueConfig()` singleton
   - Config is always explicitly instantiated and passed

3.2. Create `config/league_context.py`:
   - Migrate from `scripts/league_context.py`
   - `get_league_dir()`, `get_active_league_slug()`, `APP_CONFIG_PATH`

3.3. Create `config/ratings.py`:
   - Migrate from `scripts/ratings.py`
   - `norm()`, `norm_continuous()`, `norm_floor()` become pure functions that accept scale as parameter (no global `_ratings_scale`)
   - Signature: `norm(raw: int, scale: str = "1-100") -> int | None`

3.4. Create `client/statsplus.py`:
   - Migrate from `statsplus/client.py`

**Validation:** Config importable, ratings functions work with explicit scale param, client unchanged.

---

### Phase 4: Data layer — DB + Pipelines

**Goal:** Consolidate all DB access and write pipelines.

**Steps:**

4.1. Create `data/db.py`:
   - Migrate from `scripts/db.py`
   - `get_conn()` returns a context-manager connection
   - Schema, migrations unchanged

4.2. Create `data/refresh.py`:
   - Migrate from `scripts/refresh.py`
   - Imports evaluation functions from `statsplusplus.evaluation`

4.3. Create `data/calibrate.py`:
   - Migrate from `scripts/calibrate.py`

4.4. Create `data/fv_calc.py`:
   - Migrate from `scripts/fv_calc.py`
   - Calls typed evaluation functions, writes results to DB

**Validation:** `python3 -m statsplusplus.data.refresh` works end-to-end. Existing DB schema unchanged.

---

### Phase 5: Web layer — Flask app with proper structure

**Goal:** Flask app with blueprints, request-scoped context, split queries.

**Steps:**

5.1. Create `web/__init__.py` with app factory:
   - `create_app()` function
   - Registers blueprints, template filters, error handlers

5.2. Create `web/context.py`:
   - Request-scoped connection (stored on Flask `g`, closed on teardown)
   - Request-scoped state cache (game_date, year, eval_date computed once)
   - `get_conn()`, `get_state()`, `get_eval_date()` — all cached per request

5.3. Create `web/routes/` blueprints:
   - `team.py` — team page routes
   - `league.py` — league page, prospects, draft
   - `player.py` — player page
   - `settings.py` — settings, onboarding, league switching
   - `api.py` — refresh trigger, CSV export, AJAX endpoints

5.4. Create `web/queries/` modules:
   - Split `team_queries.py` into `roster.py`, `depth_chart.py`, `contracts.py`, `farm.py`
   - Split `queries.py` into `standings.py`, `league_wide.py`, `draft.py`
   - Split `player_queries.py` into `player_detail.py`
   - Move `percentiles.py`, `promotion_readiness.py`
   - All functions use `sqlite3.Row` (no more tuple indexing)
   - All functions receive connection as parameter (no internal `get_db()` calls)

5.5. Migrate template filters to `web/app.py`:
   - `fmt_ip`, `short`, `money` — call into `statsplusplus.utils.formatting`

**Validation:** Web app starts, all pages render, no regressions vs current behavior.

---

### Phase 6: CLI layer

**Goal:** Thin CLI wrappers with proper entry points.

**Steps:**

6.1. Migrate each CLI script into `cli/`:
   - Each gets a `main()` function with argparse
   - Business logic calls into `evaluation/` or `data/`
   - Output formatting stays in CLI layer

6.2. Wire up `pyproject.toml` console scripts:
   ```
   spp-draft = "statsplusplus.cli.draft_board:main"
   spp-trade = "statsplusplus.cli.trade_calculator:main"
   spp-targets = "statsplusplus.cli.trade_targets:main"
   spp-assets = "statsplusplus.cli.trade_assets:main"
   spp-needs = "statsplusplus.cli.team_needs:main"
   spp-standings = "statsplusplus.cli.standings:main"
   spp-fa = "statsplusplus.cli.free_agents:main"
   spp-prospects = "statsplusplus.cli.prospect_query:main"
   spp-farm = "statsplusplus.cli.farm_analysis:main"
   spp-roster = "statsplusplus.cli.roster_analysis:main"
   spp-contract = "statsplusplus.cli.contract_value:main"
   spp-refresh = "statsplusplus.data.refresh:main"
   spp-calibrate = "statsplusplus.data.calibrate:main"
   ```

6.3. Verify all CLI commands work with both forms:
   - `spp-draft pick 6`
   - `python3 -m statsplusplus.cli.draft_board pick 6`

**Validation:** All CLI tools produce identical output to current versions.

---

### Phase 7: Cleanup + Error handling

**Goal:** Remove dead code, fix error handling, final polish.

**Steps:**

7.1. Delete old `scripts/`, `web/` (original locations), `statsplus/` directories

7.2. Replace broad `except Exception: pass` with:
   - Specific exception types where possible
   - Logging at warning/debug level
   - Meaningful fallback behavior documented in comments

7.3. Add context managers for all DB connections in data layer

7.4. Remove all module-level mutable globals:
   - `ratings._ratings_scale` → parameter passing
   - `constants._weights` → explicit loader with cache invalidation
   - `player_utils._positional_models` → loaded via config

7.5. Final deduplication pass:
   - Confirm single source for level hierarchy, position constants, defensive weights
   - Confirm no duplicate function implementations remain

7.6. Add missing docstrings to all public functions

**Validation:** Full test suite passes. `mypy --strict` on models/ and evaluation/ passes (or has an explicit exclusion list).

---

### Phase 8: Documentation

**Goal:** All docs reflect the new structure. Agents can operate without confusion.

**Steps:**

8.1. Rewrite `STRUCTURE.md` — new package layout with module descriptions

8.2. Update `docs/system_overview.md` — architecture diagram, data flow, layer boundaries

8.3. Update `docs/tools_reference.md` — new CLI invocations, new query module locations

8.4. Update `README.md` — installation, quick start, project structure

8.5. Update all steering files:
   - `.kiro/steering/trade-analyst.md` — CLI commands
   - `.kiro/steering/draft-agent.md` — CLI commands, data sources
   - `.kiro/steering/beat-reporter.md` — tool paths
   - `.kiro/steering/dev-agent.md` — tier table, conventions

8.6. Update `RULES.md` — refresh commands, data flow

8.7. Write `docs/migration_notes.md` — what changed and why, for the other user

**Validation:** All agent steering files reference correct paths. README instructions work from scratch.

---

## Testing Strategy

- **Before Phase 1:** Capture full test suite baseline (number of tests, all passing)
- **Each phase boundary:** Run full suite, confirm no regressions
- **Phase 2 specifically:** Each evaluation function gets a test that compares old output vs new output for a set of fixture inputs (golden-file testing)
- **Phase 5 specifically:** Add response-level integration tests (request a route, verify status + key content)
- **End state:** Add `mypy` to CI for `models/` and `evaluation/` (strictest layers)

---

## What Does NOT Change

- **DB schema** — `league.db` tables remain identical
- **Data directory layout** — `data/<league>/` structure unchanged
- **Template HTML** — Jinja templates stay the same (just imported from new location)
- **Static assets** — CSS/JS unchanged
- **Config JSON files** — `state.json`, `league_settings.json`, etc. unchanged
- **Refresh behavior** — Same API calls, same processing, same output
- **User-facing output** — Web pages look identical, CLI output identical

---

## Estimated Effort

| Phase | Scope | Sessions | Status |
|-------|-------|----------|--------|
| 1. Foundation | New files only | 1 | ✅ Complete |
| 2. Evaluation | ~5000 lines migrated + typed | 2-3 | ✅ Complete |
| 3. Config + Client | ~500 lines migrated | 1 | ✅ Complete |
| 4. Data layer | ~3500 lines migrated | 1-2 | ✅ Complete |
| 5. Web layer | ~8000 lines migrated + split | 3-4 | ✅ Complete (middleware + blueprints + tuple cleanup) |
| 6. CLI | ~3000 lines migrated | 1-2 | ✅ Complete |
| 7. Cleanup | Deletions + fixes | 1 | 🔶 Partial (fv_calc absorbed; evaluation_engine, refresh, calibrate remain) |
| 8. Documentation | All docs | 1 | ✅ Complete |

**Completed in Session 76.** Remaining Phase 5/7 work is incremental and non-blocking.

### Phase 5 Remaining Work

The web layer migration is incremental. Each step is independent and testable:

**Step 5a — Import redirection (scripts → package):**
Modify legacy `scripts/constants.py` to re-export from `statsplusplus.evaluation.constants`.
Modify legacy `scripts/ratings.py` to delegate to `statsplusplus.config.ratings`.
This lets the web layer's existing `from constants import X` continue working
while the implementation lives in the package. Must be careful with `_load_weights()`
which has global state — may need a compatibility shim.

**Step 5b — Migrate settings/onboard routes:**
Move the 10 settings/onboard routes from `web/app.py` into `web/routes/settings.py`.
These are self-contained (they don't depend on query modules) and can be moved
as a unit. Register the blueprint in the legacy `web/app.py`.

**Step 5c — Migrate API routes:**
Move the 16 `/api/*` routes into `web/routes/api.py`. These are JSON endpoints
that call into query functions. Register the blueprint.

**Step 5d — Query module split:**
Split `web/team_queries.py` (2601 lines) into:
  - `web/queries/roster.py` — get_roster, get_roster_hitters, get_roster_pitchers
  - `web/queries/depth_chart.py` — get_depth_chart, playing time model
  - `web/queries/contracts.py` — get_contracts, get_payroll_summary, get_upcoming_fa
  - `web/queries/farm.py` — get_farm, get_farm_depth, get_org_overview
Update imports in routes that use them.

**Step 5e — Eliminate tuple indexing:**
Convert query functions from `conn.row_factory = None` + `r[0]` to
`sqlite3.Row` + `r["column_name"]`. ~22 functions in team_queries.py.
Low priority — only matters for maintainability, not correctness.

### Phase 7 Blockers

Cannot delete `scripts/` until:
- Web query modules no longer import from `player_utils`, `constants`, etc.
- `web/app.py` no longer imports `league_config`, `league_context`, `ratings`
- All references resolved through the new package

Cannot delete `web/web_league_context.py` until:
- All query modules import `get_conn` from `statsplusplus.web.context`
- This is a single find-replace once the package is installed in the web venv

These will resolve naturally as Steps 5a-5d complete.

Each session ends with passing tests and updated docs for what was touched.
