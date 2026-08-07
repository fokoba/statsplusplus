# Directory Structure

The project uses a dual-structure during migration: the new `src/statsplusplus/`
package contains the refactored architecture, while `scripts/` and `web/` contain
the legacy code that still runs the application. The legacy code is being migrated
into the package incrementally.

```
statsplusplus/
├── pyproject.toml              # Package definition, entry points, pinned deps
├── README.md
├── PURPOSE.md                  # Project goals and design principles
├── STRUCTURE.md                # This file
├── RULES.md                    # Data pull and storage rules
│
├── src/statsplusplus/          # ← NEW: Proper Python package
│   ├── models/                     # Typed dataclasses (contracts between layers)
│   ├── evaluation/                 # Pure computation (no I/O)
│   ├── config/                     # League resolution, ratings normalization
│   ├── client/                     # StatsPlus API client
│   ├── data/                       # DB schema, connections, migrations
│   ├── web/                        # Flask app factory, blueprints, context
│   ├── cli/                        # CLI entry points (spp-* commands)
│   └── utils/                      # Shared formatting, positions, logging
│
├── scripts/                    # ← LEGACY: Still active (being migrated)
│   ├── evaluation_engine.py        # Player evaluation (3449 lines)
│   ├── refresh.py                  # API → DB pipeline
│   ├── calibrate.py                # Model calibration
│   ├── fv_calc.py                  # Batch FV/surplus
│   └── ... (20+ CLI and utility scripts)
│
├── web/                        # ← LEGACY: Running Flask server
│   ├── app.py                      # Routes + refresh (patched for shared conn)
│   ├── web_league_context.py       # Request context (patched for caching)
│   ├── team_queries.py             # Team queries (patched for caching)
│   ├── queries.py, player_queries.py, percentiles.py, ...
│   ├── templates/                  # Jinja2 templates
│   └── static/                     # CSS, JS
│
├── tests/                      # Test suite (696 tests)
│   ├── models/                     # Model + utility tests
│   ├── evaluation/                 # Pure computation tests
│   ├── data/                       # DB integration tests
│   ├── web/                        # Web context tests
│   └── test_*.py                   # Legacy integration tests
│
├── data/                       # Runtime data (gitignored)
│   ├── app_config.json
│   └── <league>/league.db, config/, history/, reports/
│
└── docs/
    ├── code_audit.md               # Codebase quality findings
    ├── refactoring_plan.md         # Migration plan + status
    └── ...
```

## Architecture Layers

```
┌───────────────────────────────────────────────────────────────┐
│  CLI (cli/)                     │  Web (web/)                   │
│  spp-* entry points             │  Flask routes + templates     │
├─────────────────────────────────┴─────────────────────────────┤
│  Evaluation (evaluation/)                                       │
│  Pure computation — no I/O, no DB, no global state              │
├───────────────────────────────────────────────────────────────┤
│  Data (data/)                   │  Config (config/)             │
│  DB schema + connections        │  League resolution + ratings  │
├─────────────────────────────────┴─────────────────────────────┤
│  Models (models/)                                               │
│  Typed dataclasses — contracts between all layers               │
└───────────────────────────────────────────────────────────────┘
```

## CLI Entry Points

After `pip install -e .`, all tools are available as `spp-*` commands:

| Command | Purpose |
|---------|---------|
| `spp-standings` | Pythagorean standings |
| `spp-targets` | Trade target finder by position |
| `spp-trade` | Trade surplus balance calculator |
| `spp-assets` | Tradeable assets for any team |
| `spp-needs` | Positional needs vs league average |
| `spp-fa` | Free agent class analysis |
| `spp-prospects` | League-wide prospect rankings |
| `spp-draft` | Draft board, simulation, auto-draft |
| `spp-contract` | Contract surplus analysis |
| `spp-prospect-value` | Prospect surplus calculator |
| `spp-farm` | Farm system report |
| `spp-roster` | Roster scaffold generator |
| `spp-refresh` | Full league data refresh |
| `spp-calibrate` | Model calibration |

Also callable as: `python3 -m statsplusplus.cli.standings --help`

## DB Tables (league.db)

| Table | Owner | Description |
|---|---|---|
| `players` | `refresh.py` | All players across all orgs and levels |
| `teams` | `refresh.py` | Team ID → name, level, parent org, league |
| `ratings` | `refresh.py` | Scouting ratings (121+ cols, latest snapshot). PK: `(player_id, snapshot_date)` |
| `ratings_history` | `refresh.py` | Monthly in-game rating snapshots. PK: `(player_id, snapshot_date)` |
| `contracts` | `refresh.py` | Active contracts (up to 15 salary years, options, incentives) |
| `contract_extensions` | `refresh.py` | Pending extensions |
| `batting_stats` | `refresh.py` | Player batting stats by year/split/team. `league_id` NULL = MLB |
| `pitching_stats` | `refresh.py` | Player pitching stats. `league_id` NULL = MLB |
| `fielding_stats` | `refresh.py` | Fielding by position (G, IP, ZR, framing) |
| `team_batting_stats` | `refresh.py` | Team-level batting aggregates |
| `team_pitching_stats` | `refresh.py` | Team-level pitching aggregates |
| `games` | `refresh.py` | Game results with scores, WP/LP/SV |
| `standings` | `refresh.py` | Real W-L-GB from StatsPlus API |
| `trade_block` | `refresh.py` | Players confirmed available |
| `prospect_fv` | `fv_calc.py` | FV grades + risk labels for prospects |
| `player_surplus` | `fv_calc.py` | Surplus value for MLB players |

| View | Description |
|---|---|
| `latest_ratings` | Most recent snapshot only |
| `mlb_batting_stats` | Batting stats filtered to MLB (league_id IS NULL) |
| `mlb_pitching_stats` | Pitching stats filtered to MLB |
| `mlb_fielding_stats` | Fielding stats filtered to MLB |

## Key Conventions

- All league data in `data/<league>/league.db`. Web layer is read-only.
- `data/<league>/config/` — JSON configuration and league-level aggregates.
- `data/<league>/history/` — Scouting summaries and FV tracking.
- `data/app_config.json` — Global config: active league + session cookie.
- Request-scoped DB connection: single connection per page load, cached on Flask `g`.
- All model constants in `src/statsplusplus/evaluation/constants.py`.
- Typed models in `src/statsplusplus/models/` define all cross-module interfaces.
