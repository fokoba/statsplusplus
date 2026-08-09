# Directory Structure

```
statsplusplus/
├── pyproject.toml              # Package definition, entry points, pinned deps
├── README.md
├── PURPOSE.md                  # Project goals and design principles
├── STRUCTURE.md                # This file
├── RULES.md                    # Data pull and storage rules
│
├── src/statsplusplus/          # Core package (all logic lives here)
│   ├── models/                     # Typed dataclasses (contracts between layers)
│   ├── evaluation/                 # Pure computation (no I/O, no global state)
│   │   ├── composite.py               # Composite scores, tool transforms, defensive scoring
│   │   ├── ceiling.py                  # Ceiling computation
│   │   ├── fv.py                       # FV grades, risk labels, PAC, positional access
│   │   ├── player_value.py            # Player surplus model (stat_confidence gradient)
│   │   ├── outcomes.py                # Career outcome probability distributions
│   │   ├── war.py                      # WAR projection, aging curves, stat history
│   │   ├── surplus.py                  # Prospect/contract surplus helpers, scarcity
│   │   ├── arb.py                      # Arb salary, service time, team control
│   │   ├── carrying_tools.py           # Carrying tool bonus
│   │   └── constants.py                # All model constants, weight loaders
│   ├── config/                     # League resolution, ratings normalization
│   │   ├── league_config.py            # LeagueConfig class, dollars_per_war, league_minimum
│   │   ├── league_context.py           # Active league resolution, cookie management
│   │   └── ratings.py                  # norm(), norm_continuous(), norm_floor() (pure, explicit scale)
│   ├── client/                     # StatsPlus API client
│   │   └── statsplus.py
│   ├── data/                       # DB access + write pipelines
│   │   ├── db.py                       # Connection management, schema, all migrations
│   │   ├── evaluation_engine.py        # Batch player evaluation (composite/ceiling)
│   │   ├── refresh.py                  # API → DB pipeline
│   │   ├── calibrate.py               # Per-league model calibration
│   │   ├── fv_calc.py                  # Batch FV/surplus computation
│   │   └── milb.py                     # MiLB stat loading
│   ├── web/                        # Flask app factory, blueprints, context
│   │   ├── app.py                      # App creation + middleware
│   │   ├── context.py                  # Request-scoped DB + config
│   │   └── routes/                     # Blueprint stubs
│   ├── cli/                        # CLI entry points (spp-* commands)
│   └── utils/                      # Shared utilities
│       ├── formatting.py               # fmt_money, fmt_ip, height_str, fmt_table
│       ├── positions.py                # assign_bucket, display_pos, level maps, pitch fields
│       └── logging.py                  # Centralized log config
│
├── scripts/                    # CLI tools (import from package)
│   ├── contract_value.py           # MLB player surplus calculation
│   ├── prospect_value.py           # Prospect surplus calculation
│   ├── draft_board.py              # Draft board, simulation, auto-draft
│   ├── trade_calculator.py         # Trade surplus balance calculator
│   ├── trade_targets.py            # Trade target finder by position
│   ├── trade_assets.py             # Tradeable assets for any team
│   ├── team_needs.py               # Positional needs vs league average
│   ├── standings.py                # Pythagorean standings
│   ├── free_agents.py              # Free agent class analysis
│   ├── prospect_query.py           # Prospect rankings
│   ├── farm_analysis.py            # Farm system report generator
│   ├── roster_analysis.py          # Roster scaffold generator
│   ├── projections.py              # OPS+/ERA/WAR projection models
│   ├── benchmark.py                # Evaluation accuracy benchmark
│   ├── comp_validate.py            # Comp-based FV validation
│   └── draft_settings.py           # Draft board settings management
│
├── web/                        # Flask web application
│   ├── app.py                      # Core routes (team, league, player) + middleware
│   ├── settings_routes.py          # Settings/onboarding blueprint
│   ├── api_routes.py               # API/refresh blueprint
│   ├── web_league_context.py       # Request-scoped DB connection
│   ├── queries.py                  # League-wide queries
│   ├── team_queries.py             # Team-specific queries
│   ├── player_queries.py           # Player page data
│   ├── percentiles.py              # Percentile rankings
│   ├── trade_queries.py            # Trade tab queries
│   ├── templates/                  # Jinja2 templates
│   └── static/                     # CSS, JS, favicon assets
│
├── tests/                      # Test suite (674 tests)
│   ├── models/                     # Model + utility tests
│   ├── evaluation/                 # Pure computation tests
│   ├── data/                       # DB integration tests
│   ├── web/                        # Web context tests
│   └── test_*.py                   # Integration tests
│
├── data/                       # Runtime data (gitignored)
│   ├── app_config.json
│   └── <league>/league.db, config/, history/, reports/
│
├── statsplus/                  # StatsPlus API client library
│   └── client.py
│
└── docs/                       # Documentation
```

## Architecture Layers

```
┌───────────────────────────────────────────────────────────────┐
│  CLI (scripts/ + cli/)          │  Web (web/)                   │
│  spp-* commands                 │  Flask routes + templates     │
├─────────────────────────────────┴─────────────────────────────┤
│  Evaluation (evaluation/)                                       │
│  Pure computation — no I/O, no DB, no global state              │
├───────────────────────────────────────────────────────────────┤
│  Data (data/)                   │  Config (config/)             │
│  DB + pipelines (only writer)   │  League resolution + ratings  │
├─────────────────────────────────┴─────────────────────────────┤
│  Models (models/)                                               │
│  Typed dataclasses — contracts between all layers               │
└───────────────────────────────────────────────────────────────┘
```

## Design Principles

- **No singletons or global state.** Every function receives what it needs as parameters.
- **Entry points resolve context.** CLI `main()` and web `before_request` are the only
  places that read `app_config.json` and create `LeagueConfig`.
- **Package is self-contained.** `src/statsplusplus/` has zero dependency on `scripts/`.
- **Web layer is read-only.** All DB writes go through `data/` pipelines.
- **Pure computation in `evaluation/`.** No DB, no I/O, no global state.

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
Or legacy: `python3 scripts/standings.py --actual`

## DB Tables (league.db)

| Table | Owner | Description |
|---|---|---|
| `players` | refresh | All players across all orgs and levels |
| `teams` | refresh | Team ID → name, level, parent org, league |
| `ratings` | refresh | Scouting ratings (121+ cols). PK: `(player_id, snapshot_date)` |
| `ratings_history` | refresh | Monthly in-game rating snapshots |
| `contracts` | refresh | Active contracts (up to 15 salary years, options, incentives) |
| `batting_stats` | refresh | Player batting stats by year/split/team. `league_id` NULL = MLB |
| `pitching_stats` | refresh | Player pitching stats |
| `fielding_stats` | refresh | Fielding by position |
| `team_batting_stats` | refresh | Team-level batting aggregates |
| `team_pitching_stats` | refresh | Team-level pitching aggregates |
| `games` | refresh | Game results with scores |
| `standings` | refresh | Real W-L-GB from StatsPlus API |
| `trade_block` | refresh | Players confirmed available |
| `player_evaluation` | fv_calc | Unified player value (surplus, WAR projection, FV) |
| `prospect_fv` | fv_calc | View on player_evaluation (prospects only) |
| `player_surplus` | fv_calc | View on player_evaluation (MLB only) |

| View | Description |
|---|---|
| `latest_ratings` | Most recent snapshot only |
| `prospect_fv` | player_evaluation filtered to prospects (sc < 0.5, age ≤ 25) |
| `player_surplus` | player_evaluation filtered to MLB level |
| `mlb_batting_stats` | Batting filtered to MLB (league_id IS NULL) |
| `mlb_pitching_stats` | Pitching filtered to MLB |
| `mlb_fielding_stats` | Fielding filtered to MLB |
