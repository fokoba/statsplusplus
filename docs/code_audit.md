# Codebase Quality Audit

Comprehensive review of the Stats++ codebase conducted Session 76.
Organized by severity and category.

---

## Executive Summary

The codebase is functional and feature-rich (~20K lines across core modules), but has accumulated significant structural debt from rapid iterative development. The main risks are:

1. **Maintainability** — Several 2000+ line files with no internal modularity
2. **Fragility** — Positional tuple indexing across 2600-line query modules
3. **Duplication** — Functions, SQL, and patterns repeated across modules
4. **Coupling** — Module-level globals and `sys.path` hacks create invisible dependencies
5. **Type safety** — Near-zero type annotations outside `evaluation_engine.py`

The core model logic (`evaluation_engine.py`, `fv_model.py`, `war_model.py`) is well-designed with pure functions, clear docstrings, and proper separation of concerns. The web layer and CLI scripts are where most of the debt lives.

---

## 1. SOLID Violations

### 1.1 Single Responsibility — File Size and Scope

| File | Lines | Concern |
|------|-------|---------|
| `evaluation_engine.py` | 3449 | Hitter composite, pitcher composite, ceiling, stat blend, two-way detection, carrying tools, arsenal quality, divergence detection, archetype classification, batch DB orchestration |
| `team_queries.py` | 2601 | 50 query functions covering roster, depth chart, contracts, farm, org overview, minor leagues, stats, standings |
| `player_queries.py` | 2176 | Player detail, ratings, stats, splits, insights, milb stats, contract display, percentile context |
| `app.py` | 1517 | Routes, refresh logic, template filters, settings CRUD, onboarding wizard, CSV export, API endpoints |
| `queries.py` | 1427 | State helpers, prospect rankings, standings, leaders, draft board, farm depth, org minor league roster |

**Recommendation:** Break these into focused modules:
- `evaluation_engine.py` → `evaluation/hitter.py`, `evaluation/pitcher.py`, `evaluation/ceiling.py`, `evaluation/batch.py`
- `team_queries.py` → `web/queries/roster.py`, `web/queries/depth_chart.py`, `web/queries/contracts.py`, `web/queries/farm.py`
- `app.py` → `web/routes/team.py`, `web/routes/league.py`, `web/routes/settings.py`, `web/routes/api.py`

### 1.2 Open/Closed Principle

- **Position bucket logic** is spread across `assign_bucket()` (player_utils.py), composite weight selection (evaluation_engine.py), defensive score (fv_model.py), WAR lookup (war_model.py), and surplus calculation (prospect_value.py). Adding a new position requires touching 5+ files.
- **No plugin/extension architecture** for evaluation components — e.g., adding a new tool bonus requires modifying `compute_composite_hitter()` directly rather than registering a bonus function.

### 1.3 Dependency Inversion

- `fv_calc.py` directly imports from `evaluation_engine` at the function level (inside `run()`), creating a circular-feel dependency chain.
- `player_utils.py` re-exports from `fv_model.py`, `war_model.py`, `ratings.py`, and `constants.py` — it's a grab-bag rather than a focused utility module.
- `constants.py` calls `_load_weights()` which calls `get_league_dir()` — a "constants" file that performs I/O at import time.

---

## 2. Data Modeling — Untyped Interfaces

### 2.1 Player Dict as Universal Interface

The entire system passes "player dicts" (`dict[str, Any]`) between functions. There is no `Player` dataclass, no `Ratings` dataclass, no `ProspectEvaluation` type. This creates problems:

- **No IDE support** — Can't autocomplete or verify keys
- **Key typos are silent** — `p.get("PotCntct")` vs `p.get("PotCnct")` — runtime None, not a compile error
- **Contract unclear** — `calc_fv(p)` requires `Ovr, Pot, Age, _is_pitcher, _bucket, _norm_age, _mlb_median` but nothing enforces this
- **Internal state mixed with data** — `_fv_continuous`, `_stat_risk_modifier`, `_defensive_value` are set as side effects inside `calc_fv()`

**Current state:** Only `EvaluationResult` in evaluation_engine.py uses a dataclass. Everything else is raw dicts.

**Recommendation:** Introduce typed models:
```python
@dataclass
class PlayerRatings:
    player_id: int
    name: str
    age: int
    # ... explicit fields
    
@dataclass
class ProspectEvaluation:
    fv_grade: int
    fv_continuous: float
    risk: str
    bucket: str
    ceiling: int
    composite: int
```

### 2.2 Return Types — Tuples vs Dicts

`team_queries.py` sets `conn.row_factory = None` in 22 functions, returning tuples with positional indexing (`r[0]`, `r[1]`, etc.). This means:
- Any schema change or SELECT reorder silently corrupts all downstream code
- Impossible to understand what `r[7]` means without reading the SQL
- Code review cannot catch off-by-one index errors

Meanwhile, other functions in the same module use `sqlite3.Row` (dict-like). The inconsistency makes the codebase harder to navigate.

**Recommendation:** Use `sqlite3.Row` everywhere. The claimed "performance" benefit of tuples is negligible for page-load queries hitting a local SQLite DB. If profiling shows otherwise, use named tuples or dataclasses.

---

## 3. Code Duplication (DRY Violations)

### 3.1 Duplicated Functions

| Function | Locations | Notes |
|----------|-----------|-------|
| `defensive_score()` | `fv_model.py`, `player_utils.py` | Identical implementations |
| `_pos_composite()` | `fv_model.py`, `player_utils.py` | Identical implementations |
| `_fmt_money()` / `_fmt_money_py()` | `app.py`, `player_queries.py` | Same logic, different names |
| `RATINGS_SQL` | `fv_calc.py` (canonical), imported into `queries.py`, `player_queries.py` | 90-line SQL string shared via import |

The `player_utils.py` versions exist because it re-exports from `fv_model.py`, but it also defines its OWN `defensive_score()` before the re-export at line 322 overwrites it. This is confusing and fragile.

### 3.2 Duplicated Patterns

**State loading:** `_get_state()` is called 16 times in `team_queries.py`, each time reading `state.json` from disk and querying `MAX(year)` from the DB. This should be done once per request.

**DB connection acquisition:** 63 calls to `conn = get_db()` across web modules — each opens a new connection. No connection reuse within a request.

**Eval date query:** `conn.execute("SELECT MAX(eval_date) FROM player_surplus")` appears in nearly every team query function. Should be computed once per request.

### 3.3 SQL Duplication

Several large SQL blocks are repeated with minor variations across query functions. The `RATINGS_SQL` pattern (90 lines of column aliases) appears in at least 4 files.

---

## 4. Module Organization and Import Structure

### 4.1 sys.path Manipulation

**48 instances** of `sys.path.insert()` across 38 files. This is the #1 structural smell in the codebase. Each module hacks the Python path to find its siblings, creating:
- Import order sensitivity
- Impossible-to-verify dependency graphs
- IDE confusion (can't resolve imports without running the code)
- Circular import risk

**Root cause:** The project isn't structured as a proper Python package. There's no `__init__.py`, no `pyproject.toml`, no package install.

**Recommendation:** Convert to a proper package with `pyproject.toml` and install in editable mode (`pip install -e .`). This eliminates ALL sys.path hacks.

### 4.2 Module-Level Global State

| Module | Global | Risk |
|--------|--------|------|
| `ratings.py` | `_ratings_scale = None` | Mutated at request time by `app.py` — not thread-safe |
| `constants.py` | `_weights = None` | Lazy-loaded, cached forever — stale after recalibration |
| `player_utils.py` | `_positional_models = None` | Same — cached forever |
| `league_config.py` | `config = LeagueConfig()` | Module-level singleton — breaks multi-league if not reloaded |
| `contract_value.py` | `_contract_cache = {}` | Unbounded cache, never cleared |

The `ratings._ratings_scale` pattern is particularly dangerous: `app.py` sets it on every request in `_set_league_context()`. In a multi-threaded server, requests for different leagues would race on this global.

---

## 5. Error Handling

### 5.1 Broad Exception Swallowing

71 instances of `except Exception` or `except:` across 15 files. The worst offenders:
- `app.py` (20) — routes catching everything
- `player_queries.py` (16) — silently returning None/empty on failures

Pattern observed in `player_queries.py`:
```python
try:
    from evaluation_engine import detect_divergence
    # ... complex logic ...
except Exception:
    pass  # silently drops errors
```

This means bugs in evaluation logic are invisible — the player page just shows blanks with no logging and no indication something failed.

**Recommendation:** 
- Log at `warning` level when catching broadly
- Use specific exception types where possible
- At minimum, add `log.debug("...", exc_info=True)` in catch blocks

### 5.2 Connection Leak Risk

66 manual `conn.close()` calls with no corresponding `try/finally` or context manager usage. If any code between `get_db()` and `conn.close()` raises, the connection leaks.

`data.py` correctly uses `with get_conn() as conn:` — but it's the only file that does.

---

## 6. Single Source of Truth Violations

### 6.1 Position/Bucket Constants

Position-related constants are defined in multiple places:
- `ROLE_MAP` in `constants.py` (canonical) — re-exported via `player_utils.py`
- Position display logic in `player_utils.display_pos()`
- Bucket ordering in `league_config.py` (`pos_order` property)
- Position game-number mapping in `draft_board.py`, `app.py`, `team_queries.py`
- Defensive weight dicts in `fv_model.py` AND `player_utils.py`

### 6.2 Financial Constants

- `DEFAULT_MINIMUM_SALARY` in `constants.py` — correct single source
- `DEFAULT_DOLLARS_PER_WAR` in `constants.py` — correct
- But `dollars_per_war()` in `player_utils.py` re-derives from JSON file on every call

### 6.3 Level Hierarchy

Level mappings are repeated in:
- `LEVEL_INT_KEY` / `LEVEL_INT_LABEL` in `fv_calc.py`
- `_ETA_BASE` in `queries.py`
- `LEVEL_NORM_AGE` in `fv_model.py`
- `LEVEL_ORDER` / `LEVEL_NAMES` in `promotion_readiness.py`
- `DEVELOPMENT_DISCOUNT` and `YEARS_TO_MLB` in `constants.py`

Each defines its own mapping between level integers and string labels. If a new level is added, 6 files need updating.

---

## 7. API / Interface Design

### 7.1 No Clear Layer Boundaries

The architecture doc says "web layer is read-only" but the boundaries are blurred:
- Web query modules directly import from `scripts/` via sys.path hacks
- `player_queries.py` imports `evaluation_engine` functions inline to recompute values
- `queries.py` imports `fv_calc.RATINGS_SQL` — a 90-line internal SQL string

There's no clean `services` layer between "compute stuff" and "serve web pages."

### 7.2 Function Signature Inconsistency

Query functions have inconsistent patterns:
- Some accept `team_id` param with `my_team_id()` default
- Some read from `_get_state()` internally 
- Some return tuples, some return dicts, some return custom nested structures
- Some close their own connections, some leave them open

---

## 8. Performance-Related Design Issues

### 8.1 Per-Request Overhead

The `/team/<tid>` route calls **17 separate query functions**, each of which:
1. Opens a new DB connection (`get_db()`)
2. Reads state from disk (`_get_state()` → reads JSON file)
3. Queries `MAX(eval_date)` from the DB
4. Executes its own SQL
5. (Sometimes) closes the connection

That's 17 new connections, 16 JSON file reads, and 15+ eval_date lookups for a single page load.

### 8.2 No Request-Scoped Caching

`web_league_context.py` provides accessor functions but no caching. Each call to `team_abbr_map()` hits `get_cfg()._load()` which checks if `_settings is None` — it's cached on the config object, but a new LeagueConfig is created per request in `_set_league_context()`.

### 8.3 evaluation_engine.py Inline in Web Path

`player_queries.py` imports and calls evaluation engine functions (archetype classification, carrying tool bonus) inline during page renders. These should be pre-computed during refresh and stored.

---

## 9. Documentation

### 9.1 Coverage

- `evaluation_engine.py` — Excellent: full module docstring, public API documented, pure functions explained
- `fv_model.py`, `war_model.py` — Good: clear docstrings, explain formulas
- `team_queries.py` — Module docstring only; individual functions have ~72 docstrings for 50 functions (good)
- `player_queries.py` — 15 docstrings for 16 functions (good ratio but many helper blocks undocumented)
- `app.py` — 28 docstrings for 52 functions (many routes undocumented)
- CLI scripts — inconsistent; some have full argparse help, others have minimal comments

### 9.2 Type Annotations

Only `evaluation_engine.py` (28 typed functions), `draft_settings.py` (9), `data.py` (5), and `db.py` (3) use type annotations. The remaining ~25 modules have zero annotations.

---

## 10. Testing

### 10.1 Coverage Gaps

- `team_queries.py` (2601 lines) has a test file but tests are primarily smoke tests
- `player_queries.py` (2176 lines) has some tests, but many paths untested
- `percentiles.py` (1110 lines) has no dedicated test file
- `calibrate.py` (1719 lines) — the model calibration pipeline — has only smoke tests
- `promotion_readiness.py` — no tests

### 10.2 Test Isolation

Tests use `conftest.py` with a shared in-memory DB fixture, which is good. But some tests reach through to module-level singletons that carry state across test boundaries.

---

## Priority Recommendations

### Phase 1 — Quick Wins (Low risk, high impact)

1. **Eliminate duplicate functions** — Remove `defensive_score` and `_pos_composite` from `player_utils.py` (they're re-imported from `fv_model.py` on the same file's line 322)
2. **Consolidate money formatting** — Single `fmt_money()` in a shared utils module
3. **Add request-scoped connection + state caching** — One connection per request, `_get_state()` result cached on Flask `g`
4. **Consolidate level mappings** — Single `LEVEL_HIERARCHY` dict in `constants.py`
5. **Pin dependencies** — `flask>=3.0` and `pytest>=8.0` should be exact versions

### Phase 2 — Structural (Medium risk, foundational)

6. **Package structure** — Add `pyproject.toml`, make `scripts/` and `web/` proper packages, eliminate all sys.path hacks
7. **Typed models** — Introduce `PlayerRatings`, `ProspectEvaluation`, `EvaluationContext` dataclasses for core interfaces
8. **Connection context manager** — Wrap all DB access in `with get_conn() as conn:` or provide request-scoped connection
9. **Split large files** — Start with `team_queries.py` → focused sub-modules
10. **Fix global mutable state** — Pass `ratings_scale` as parameter rather than setting module global

### Phase 3 — Architecture (Higher risk, long-term)

11. **Pre-compute web display values during refresh** — Archetypes, carrying tools, percentile context should be stored in DB, not computed per-request
12. **Service layer** — Clean interface between "compute" and "serve" that doesn't require web modules to import from scripts
13. **Eliminate tuple indexing** — Use `sqlite3.Row` or named tuples everywhere
14. **Evaluation engine modularization** — Break the 3449-line file into focused sub-modules
15. **Replace _weights global caching** — Use explicit config passing or a cache-invalidation-aware loader

---

## Metrics Summary

| Metric | Before (Session 76 start) | After (Session 76 end) | Target |
|--------|---------------------------|------------------------|--------|
| Files > 1000 lines | 10 | 10 (legacy) | ≤ 3 |
| `sys.path.insert` calls | 48 | 48 (legacy, bypassed by package) | 0 |
| Bare `except Exception` | 71 | 71 (legacy) | < 10 (with logging) |
| Manual `conn.close()` | 66 | 35 (31 removed from web/) | 0 |
| Typed function signatures | ~49/200+ | ~150+ (package is fully typed) | 80%+ |
| Duplicated function defs | 4+ | 0 (shims delegate to package) | 0 |
| Connections per team page | ~17 | 1 (via _ScopedConnection) | 1 ✓ |
| State file reads per team page | ~16 | 1 (via _get_state cache) | 1 ✓ |
| Eval_date queries per team page | ~10 | 1 (via _get_eval_date cache) | 1 ✓ |
| mypy strict coverage | 0 files | 25 files | All core modules ✓ |
| Test count | 499 | 696 | Growing |
