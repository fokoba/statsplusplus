# Full Migration Plan: Eliminating scripts/ Shims

## Goal

Remove all `scripts/` shim files so that:
- The package (`src/statsplusplus/`) contains ALL logic
- CLI scripts either live in the package (`cli/`) or import directly from it
- The web layer imports directly from the package
- Zero indirection layers remain
- No module-level singletons or hidden global state

## Design Principle: Explicit Dependencies

Every function receives what it needs as parameters. No function reads from disk,
resolves league context, or accesses global state implicitly.

**Entry points** (CLI `main()`, web `before_request`) are the only places that
resolve league context from `app_config.json`. They create a `LeagueConfig` and
pass it (or its components) to the functions they call.

```python
# CLI entry point pattern
def main():
    league_dir = get_league_dir(get_active_league_slug())
    cfg = LeagueConfig(base_dir=league_dir)
    conn = get_connection(league_dir)
    # ... call functions with explicit args ...

# Web entry point pattern (already works this way)
@app.before_request
def _set_league_context():
    cfg = LeagueConfig(base_dir=league_dir)
    g.league_config = cfg
```

**Pure functions** take values, not config objects:
```python
def norm(raw: int, scale: str) -> int | None: ...
def dollars_per_war(league_dir: Path) -> int: ...
def assign_bucket(p: dict, role_map: dict[int, str]) -> str: ...
```

**Why no singleton:** The module-level `config = LeagueConfig()` singleton creates
hidden coupling, makes tests brittle, and prevents multi-league operations within
a single process. The 3-line boilerplate at each CLI entry point is a worthwhile
trade for correctness and testability.

---

## Current State

```
Package (13,135 lines across 58 files)
├── Has: typed models, evaluation math, DB schema, orchestration pipelines
├── Missing: ~15 functions from player_utils, calc_fv, weight loaders, assign_bucket

Scripts (16 shims)
├── Pure re-exports (deletable once consumers migrate): 7 files
│   league_config.py, league_context.py, refresh.py, evaluation_engine.py,
│   fv_calc.py, calibrate.py, log_config.py
├── Hybrids (re-export + retain real logic): 9 files
│   player_utils.py, fv_model.py, constants.py, ratings.py,
│   arb_model.py, war_model.py, db.py, data.py, discord_post.py
```

---

## Migration Phases

### Phase A: Complete the package's function coverage

Move remaining functions from hybrid shims into the package. All functions take
explicit parameters — no reading from globals or config singletons.

| Function | Source | Target | Key parameter change |
|----------|--------|--------|---------------------|
| `assign_bucket()` | player_utils.py | `utils/positions.py` | Add `role_map: dict` param |
| `display_pos()` | player_utils.py | `utils/positions.py` | Already there? Verify |
| `dollars_per_war()` | player_utils.py | `config/league_config.py` | Takes `league_dir: Path` |
| `league_minimum()` | player_utils.py | `config/league_config.py` | Takes `league_dir: Path` or read from cfg |
| `calc_pap()` | player_utils.py | `evaluation/surplus.py` | Already pure (takes values) |
| `defensive_score()` | player_utils.py | `evaluation/composite.py` | Already mostly pure |
| `estimate_positional_rating()` | player_utils.py | `evaluation/composite.py` | Takes `models: dict` param |
| `estimate_all_positions()` | player_utils.py | `evaluation/composite.py` | Takes `models: dict` param |
| `_load_positional_models()` | player_utils.py | `config/` or `evaluation/` | Takes `league_dir: Path` |
| `calc_fv()` | fv_model.py | `evaluation/fv.py` | Takes `dev_curves: dict`, `scale: str` |
| `positional_access_premium()` | fv_model.py | `evaluation/fv.py` | Already pure |
| `_load_weights()` | constants.py | `evaluation/constants.py` | Takes `league_dir: Path` |
| `_load_fv_by_pos()` | constants.py | `evaluation/constants.py` | Takes `league_dir: Path` |
| `_load_composite_to_war()` | constants.py | `evaluation/constants.py` | Takes `league_dir: Path` |
| `estimate_service_time()` | arb_model.py | `evaluation/arb.py` | Already takes `conn` |
| `estimate_control()` | arb_model.py | `evaluation/arb.py` | Add `perpetual_arb: bool` param |
| `load_stat_history()` | war_model.py | `evaluation/war.py` | Already takes `conn, game_date` |
| `norm()` | ratings.py | `config/ratings.py` | Already pure: `norm(raw, scale)` exists |
| `get_logger()` | log_config.py | `utils/logging.py` | Already there |

**The ratings scale problem solved:** The package already has `norm(raw, scale)`.
Callers will pass scale explicitly: `norm(raw, cfg.ratings_scale)`. No global needed.

**The assign_bucket problem solved:** Currently reads `role_map` from the config
singleton and loads positional models from disk. New signature:
```python
def assign_bucket(
    p: dict,
    role_map: dict[int, str],
    positional_models: dict | None = None,
) -> str:
```
Callers load the models once at the top and pass them through.

**The constants loading problem solved:** Currently `constants.py` has `_load_weights()`
that reads `model_weights.json` at import time and caches in module globals.
New pattern:
```python
def load_model_weights(league_dir: Path) -> ModelWeights:
    """Load calibrated weights from league_dir/config/model_weights.json."""
    ...
```
CLI entry points call this once and pass the weights to functions that need them.

**Estimated effort:** 2-3 sessions.

### Phase B: Rewrite CLI script imports

For each of the ~20 real CLI scripts, change:
```python
# Before
from league_config import config as _cfg
import db as _db
from player_utils import assign_bucket, dollars_per_war
from constants import ARB_PCT
from ratings import norm

# After
from statsplusplus.config.league_context import get_league_dir, get_active_league_slug
from statsplusplus.config.league_config import LeagueConfig
from statsplusplus.data.db import get_connection
from statsplusplus.utils.positions import assign_bucket
from statsplusplus.evaluation.constants import ARB_PCT
from statsplusplus.config.ratings import norm

def main():
    league_dir = get_league_dir(get_active_league_slug())
    cfg = LeagueConfig(base_dir=league_dir)
    conn = get_connection(league_dir)
    scale = cfg.ratings_scale
    role_map = cfg.role_map
    # ... use explicit params throughout ...
```

Scripts that need many config values can destructure at the top:
```python
    my_team_id = cfg.my_team_id
    year = cfg.year
    min_salary = cfg.minimum_salary
```

**Estimated effort:** 2-3 sessions (many files, but each is mechanical).

### Phase C: Rewrite web layer imports

Same pattern for web modules. The web layer already has `g.league_config` so it's
mostly just changing import paths:

```python
# Before
from league_config import LeagueConfig
from constants import ROLE_MAP
from player_utils import norm, assign_bucket

# After
from statsplusplus.config.league_config import LeagueConfig
from statsplusplus.evaluation.constants import ROLE_MAP
from statsplusplus.config.ratings import norm
from statsplusplus.utils.positions import assign_bucket
```

For web query functions that need config, they already receive it via `get_cfg()`
which reads from `g.league_config`. No change to the calling pattern — just the
import paths.

**Estimated effort:** 1-2 sessions.

### Phase D: Delete shims + verify

Once no file imports from a shim:
```bash
# For each shim, verify no remaining imports
grep -rl "from league_config import\|import league_config" scripts/ web/ tests/
# If clean, delete
rm scripts/league_config.py
```

Run full test suite + smoke tests after each deletion.

**Estimated effort:** 1 session.

### Phase E: Move real CLI scripts into package (optional)

Move `scripts/contract_value.py` logic into `src/statsplusplus/cli/contract_value.py`.
The current `cli/` modules are thin wrappers — this makes them the real implementation.

Only worth doing if we want `scripts/` to be fully empty. The scripts work fine
where they are as long as they import from the package.

**Estimated effort:** 3-4 sessions.

---

## Dependency Order

```
Phase A (complete package functions — explicit params)
    ↓
Phase B (rewrite CLI imports)  ←→  Phase C (rewrite web imports)
    ↓
Phase D (delete shims)
    ↓
Phase E (optional: move CLI into package)
```

Phases B and C are independent and can be done in either order or interleaved.

---

## The `assign_bucket` Migration (Most Complex Single Function)

`assign_bucket()` is the most-imported function from `player_utils.py` and has the
most complex dependency chain:

```
assign_bucket(p)
  → reads role_map from config singleton
  → calls estimate_positional_rating(p, pos_col)
      → calls _load_positional_models()
          → reads model_weights.json from disk via get_league_dir()
          → caches in module-level _positional_models global
```

**Migrated version:**
```python
# src/statsplusplus/utils/positions.py

def load_positional_models(league_dir: Path) -> dict[str, Any]:
    """Load OLS positional models from model_weights.json."""
    mw_path = league_dir / "config" / "model_weights.json"
    if not mw_path.exists():
        return {}
    data = json.loads(mw_path.read_text())
    return data.get("POSITIONAL_MODELS", {})

def estimate_positional_rating(
    p: dict, pos_col: str, models: dict[str, Any]
) -> float | None:
    """Estimate a positional rating from defensive tools using OLS model."""
    ...

def assign_bucket(
    p: dict,
    role_map: dict[int, str],
    positional_models: dict[str, Any] | None = None,
) -> str:
    """Assign evaluation bucket (C, SS, 2B, ..., SP, RP)."""
    ...
```

**Callers become:**
```python
# At CLI entry point (once)
models = load_positional_models(league_dir)
role_map = cfg.role_map

# At call site
bucket = assign_bucket(player_dict, role_map, models)
```

---

## Risk Assessment

| Phase | Risk | Mitigation |
|-------|------|-----------|
| A | Medium — function behavior must match exactly | Comparison tests: old vs new output for same inputs |
| B | Low — mechanical import rewriting | Run full test suite after each file |
| C | Medium — web layer has implicit state via Flask `g` | Page-load smoke tests after each module |
| D | Low — just deletion | grep confirms no remaining imports |
| E | Medium-High — large scripts with internal complexity | Per-script with full regression |

---

## Total Estimated Effort

| Milestone | Sessions | Result |
|-----------|----------|--------|
| Phase A | 2-3 | Package is functionally complete |
| Phase B + C | 3-4 | All code imports from package directly |
| Phase D | 1 | Shims deleted, scripts/ only has real CLI tools |
| **Shims eliminated** | **6-8 total** | |
| Phase E (optional) | 3-4 | scripts/ fully empty |

---

## What Stays the Same

- DB schema unchanged
- Web UI unchanged (same pages, same behavior)
- CLI tool behavior unchanged (same flags, same output)
- `spp-targets --bucket SP` works (package entry points in pyproject.toml)
- `python3 -m statsplusplus.cli.trade_targets --bucket SP` works
- Test suite passes at every phase boundary
- Users and agents never see a behavioral difference

## End State

```
src/statsplusplus/           ← ALL code lives here
    cli/                     ← CLI entry points (spp-* commands)
    evaluation/              ← Pure computation
    config/                  ← League resolution, ratings
    data/                    ← DB, orchestration pipelines
    models/                  ← Typed dataclasses
    utils/                   ← Formatting, positions
    web/                     ← Flask blueprints + context

web/                         ← Flask server (templates, static, query modules)
                               Imports from statsplusplus.* directly

scripts/                     ← DELETED (no longer exists)
```

The `scripts/` directory is fully eliminated. All CLI functionality is accessed via
`spp-*` commands (installed by `pip install -e .`) or `python3 -m statsplusplus.cli.*`.
Agent steering docs reference `spp-targets`, `spp-draft`, etc.

The web layer's refresh button calls the package function directly instead of
spawning a subprocess to `scripts/refresh.py`.
