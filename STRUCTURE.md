# Directory Structure

```
statsplusplus/
├── README.md
├── PURPOSE.md              # Project goals and design principles
├── STRUCTURE.md            # This file
├── RULES.md                # Data pull and storage rules
│
├── data/                           # Runtime data (gitignored)
│   ├── app_config.json                 # Global config: active_league, statsplus_cookie
│   ├── logs/                           # Rotating log files
│   └── <league>/                       # Per-league data directory (e.g. data/vmlb/)
│       ├── league.db                       # SQLite database
│       ├── config/
│       │   ├── state.json                      # Current game date, year, my_team_id
│       │   ├── league_settings.json            # Team names/abbr, divisions, role/pos/level maps, financial settings
│       │   ├── league_averages.json            # League-wide batting/pitching averages + $/WAR
│       │   ├── model_weights.json              # Calibrated valuation tables (OVR_TO_WAR, COMPOSITE_TO_WAR, FV, ARB, scarcity)
│       │   ├── tool_weights.json               # Per-league calibrated tool weights (component regression coefficients)
│       │   └── draft_settings.json             # Draft board slider settings (per-round-group parameters)
│       ├── history/
│       │   ├── prospects.json                  # Scouting summaries + FV history (keyed by player_id)
│       │   └── roster_notes.json               # MLB player summaries (keyed by player_id)
│       ├── reports/<year>/                     # Published analysis reports
│       └── tmp/                                # Intermediate scaffolds (script output)
│
├── scripts/
│   ├── league_context.py       # Active league resolver (get_league_dir, get_active_league_slug)
│   ├── league_config.py        # Single abstraction for league-specific settings
│   ├── log_config.py           # Centralized logging (rotating file + console)
│   ├── constants.py            # Shared constants (sections: identifiers, prospect model, MLB model, WAR tables, aging curves)
│   ├── ratings.py              # Rating normalization (norm, norm_floor, get_ratings_scale)
│   ├── db.py                   # SQLite schema, migrations, connection management
│   ├── data.py                 # Data access layer (typed query functions)
│   ├── player_utils.py         # Shared utilities (bucketing, display helpers, league settings, PAP). Re-exports from ratings/fv_model/war_model.
│   ├── fv_model.py             # Prospect FV grade calculation (calc_fv, dev_weight, defensive_score)
│   ├── war_model.py            # WAR projection and stat history (peak_war_from_score, aging_mult, load_stat_history, stat_peak_war)
│   ├── arb_model.py            # Arb salary and service time estimation (arb_salary, estimate_service_time, estimate_control)
│   ├── refresh.py              # API → DB pipeline (full league refresh)
│   ├── fv_calc.py              # Batch FV + surplus computation (prospect_fv, player_surplus)
│   ├── evaluation_engine.py     # Custom player evaluation (Composite_Score, Ceiling_Score from tool ratings)
│   ├── calibrate.py            # League-specific model calibration (tool weights, OVR_TO_WAR, COMPOSITE_TO_WAR, FV, ARB, scarcity)
│   ├── benchmark.py            # Evaluation engine performance benchmark (composite vs WAR, prospect inflation, ceiling collapse)
│   ├── comp_validate.py        # CLI: Comp-based FV validation (tool profile → MLB WAR distribution)
│   ├── projections.py          # Player projections for depth chart planning
│   ├── contract_value.py       # MLB contract surplus/deficit breakdown
│   ├── prospect_value.py       # Prospect surplus calculator
│   ├── farm_analysis.py        # CLI: Farm system report generator
│   ├── roster_analysis.py      # CLI: MLB roster scaffold generator
│   ├── prospect_query.py       # CLI: League-wide prospect rankings
│   ├── draft_board.py          # CLI: Draft board analysis (board, available, pick, upload, compare, sim)
│   ├── draft_settings.py      # Draft board settings: per-league, per-round-group slider persistence and validation
│   ├── trade_calculator.py     # CLI: Trade surplus balance evaluator (--offer/--receive or JSON)
│   ├── trade_targets.py        # CLI: Trade target finder by position (contract status, seller classification, split ratings)
│   ├── trade_assets.py         # CLI: Tradeable assets for any team (MLB surplus + farm prospects)
│   ├── team_needs.py           # CLI: Positional needs vs league average (upgrade priority report)
│   ├── standings.py            # CLI: Pythagorean standings
│   ├── free_agents.py          # CLI: Free agent class analysis
│   └── discord_post.py         # CLI: Post changelog to Discord webhook
│
├── web/
│   ├── app.py                  # Flask app, routes, onboarding wizard, refresh endpoint
│   ├── web_league_context.py   # Request-scoped league context (DB connection, config)
│   ├── queries.py              # League-wide queries (prospects, standings, leaders)
│   ├── team_queries.py         # Team queries (roster, depth chart, contracts, farm)
│   ├── player_queries.py       # Player detail (ratings, stats, splits, surplus)
│   ├── trade_queries.py        # Trade tab queries (org roster, trade valuation)
│   ├── percentiles.py          # Percentile rankings with expected-value modeling
│   ├── templates/
│   │   ├── base.html               # Layout (header, nav, breadcrumbs, refresh, player hover popup)
│   │   ├── league.html             # League dashboard (standings, leaders, prospects, draft)
│   │   ├── team.html               # Team page (roster, depth chart, contracts, farm)
│   │   ├── team_minor.html         # Minor league team page (notables, full roster)
│   │   ├── player.html             # Player page (ratings, stats, percentiles)
│   │   ├── settings.html           # Settings (team, identity, structure, financial, connection)
│   │   ├── onboard.html            # Onboarding wizard (connect, configure, refresh)
│   │   └── _structure_editor.html  # League structure editor (shared partial)
│   └── static/
│       ├── style.css               # Dark theme CSS
│       ├── sort.js                 # Client-side table sorting
│       └── assets/                 # Favicon and logo files
│
├── statsplus/
│   └── client.py               # StatsPlus API client (HTTP, CSV parsing, ratings format handling)
│
├── tests/
│   ├── conftest.py             # Shared fixtures: in-memory SQLite DB, mock LeagueConfig, autouse web context patch
│   ├── test_client.py          # API client tests (live API)
│   ├── test_queries.py         # Integration tests for web/queries.py (league-level queries)
│   ├── test_team_queries.py    # Integration tests for web/team_queries.py
│   ├── test_player_queries.py  # Integration tests for web/player_queries.py
│   ├── test_evaluation_engine.py # Evaluation engine unit + property tests (Hypothesis)
│   └── test_scripts.py         # Unit/smoke tests for scripts layer (assign_bucket edge cases, calibrate, fv_calc)
│
└── docs/                       # Design docs, analysis guides, changelog
    └── valuation_model.md          # Plain-language explanation of FV, surplus, and WAR models
```

## DB Tables (league.db)

| Table | Owner | Description |
|---|---|---|
| `players` | `refresh.py` | All players across all orgs and levels |
| `teams` | `refresh.py` | Team ID → name, level, parent org, league |
| `ratings` | `refresh.py` | Scouting ratings (121 cols, latest snapshot only). Overall + L/R splits for batting and pitching tools, defensive grades, character traits, demographics. Extended ratings (BABIP, HRA, PBABIP, Prone) populated when the league's OOTP version provides them. PK: `(player_id, snapshot_date)`. |
| `ratings_history` | `refresh.py` | Monthly in-game rating snapshots (53 cols). Ovr/pot, hitter/pitcher tools (cur+pot), all 12 pitch types (cur+pot), extended ratings when available. PK: `(player_id, snapshot_date)`. |
| `contracts` | `refresh.py` | Active contracts league-wide (up to 15 salary years) |
| `contract_extensions` | `refresh.py` | Pending contract extensions signed during the season |
| `batting_stats` | `refresh.py` | MLB batting stats by player/year/split/team |
| `pitching_stats` | `refresh.py` | MLB pitching stats by player/year/split/team |
| `fielding_stats` | `refresh.py` | Player fielding stats by position (G, IP, TC, E, ZR, framing, arm) |
| `team_batting_stats` | `refresh.py` | Team-level batting aggregates by year/split |
| `team_pitching_stats` | `refresh.py` | Team-level pitching aggregates by year/split |
| `games` | `refresh.py` | Game results with scores, WP/LP/SV |
| `prospect_fv` | `fv_calc.py` | FV grades + risk labels for all non-MLB prospects. Includes `fv_continuous` for interpolated surplus. |
| `player_surplus` | `fv_calc.py` | Surplus value for all MLB players (includes `surplus_yr1` for current-season) |

| View | Description |
|---|---|
| `latest_ratings` | Most recent snapshot only — used by all web queries to avoid join multiplication |

## Key Conventions

- Each league's data lives in `data/<league>/league.db`. The web layer is read-only.
- `data/<league>/config/` contains JSON configuration and league-level aggregates.
- `data/<league>/history/` contains hand-written scouting summaries and FV tracking.
- `data/<league>/tmp/` contains intermediate scaffolds — script output that feeds report writing.
- `data/<league>/reports/` contains final published reports organized by game-year.
- `data/app_config.json` stores global config: `active_league` slug and `statsplus_cookie`.
- League context resolved via `scripts/league_context.py` (shared) and `web/web_league_context.py` (Flask request-scoped).
