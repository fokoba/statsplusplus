# Stats++

An assistant GM dashboard for [OOTP Baseball](https://www.ootpdevelopments.com/) leagues managed through [StatsPlus](https://statsplus.net/). Pulls live league data from the StatsPlus API and presents it as an interactive local web dashboard with ratings, percentiles, prospect rankings, contract analysis, and more.

![Python](https://img.shields.io/badge/python-3.10+-blue) ![Flask](https://img.shields.io/badge/flask-web_ui-green) ![SQLite](https://img.shields.io/badge/sqlite-storage-orange)

## Features

- **League overview** — Standings, stat leaders, power rankings, league-wide averages
- **Team pages** — Roster, depth chart, farm system, contracts, payroll breakdown, upcoming free agents
- **Player pages** — Ratings with grade bars, batting/pitching splits, percentile rankings with expected-value tags (hot/cold/lucky/unlucky)
- **Prospect system** — Top 100, by-team and by-position views, FV grades with risk labels, surplus values, scouting panel with tools and pitch repertoire
- **Player evaluation engine** — Independent tool-weighted composites, ceiling scores, and FV grades computed from raw ratings (not the game's OVR/POT)
- **Multi-league support** — Manage multiple OOTP leagues from one install, switch between them in the nav
- **League-calibrated models** — Tool weights, WAR curves, development curves, and financial tables derived from each league's own data
- **Extended ratings** — BABIP, HR Avoidance, PBABIP, Injury Proneness (when the league's OOTP version provides them)
- **One-click refresh** — Pull latest game data from StatsPlus without leaving the browser
- **CSV export** — Export any table (draft board, roster, prospects) as CSV for external analysis
- **Discord integration** — Automated patch notes posting to a Discord server for development updates
- **CLI analysis tools** — Roster scaffolds, farm reports, prospect rankings, free agent analysis, trade calculator

## Screenshots

### League Overview
Division standings, power rankings with surplus values, and league-wide stat leaders at a glance.

![League Overview](assets/screenshots/league-overview.png)

### Team Page
Standings context, team batting/pitching stats with league rank, record breakdown, and recent games.

![Team Overview](assets/screenshots/team-overview.png)

### Depth Chart
3-year roster projection with WAR-based playing time allocation, league rank coloring, and prospect pipeline integration.

![Depth Chart](assets/screenshots/depth-chart.png)

### Player Page
Ratings with grade bars, current-year stats, and Baseball Savant-style percentile rankings with expected-value tags (Hot/Cold/Lucky/Unlucky).

![Player Page](assets/screenshots/player-page.png)

### Prospect Rankings
League-wide Top 100 with FV grades, risk labels, composite scores, and surplus values. Filter by team or position.

![Prospects](assets/screenshots/prospects.png)

### Draft Board
Full draft pool with position filters, tool thresholds, simulation, auto-draft list generation, and ADP-based value labels.

![Draft Board](assets/screenshots/draft-board.png)

## Prerequisites

- Python 3.10+
- A [StatsPlus](https://statsplus.net/) league with API access
- Your StatsPlus session cookie (see [Getting Your Cookie](#getting-your-statsplus-cookie) below)

## Quick Start

### Option A: Download (recommended for most users)

1. Download the latest release from the [Releases page](../../releases)
2. Extract the zip to a folder
3. Run the launcher:
   - **Windows:** Double-click `start.bat`
   - **Mac/Linux:** Open a terminal in the folder and run `./start.sh`
4. Your browser will open to the onboarding wizard

The launcher handles Python environment setup and dependency installation automatically. You just need [Python 3.10+](https://www.python.org/downloads/) installed.

### Option B: Clone and install (for developers)

```bash
git clone <repo-url> statsplusplus
cd statsplusplus
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

The only dependency is Flask. Everything else uses the Python standard library.

> **Note for Ubuntu 24.04+ / Debian users:** The system Python is externally managed (PEP 668) and `pip install` will be blocked without a virtual environment. The `python3 -m venv` step above handles this. If `python3 -m venv` fails, install the venv package first: `sudo apt install python3-full`.

### 2. Launch the web UI

```bash
.venv/bin/python3 web/app.py
```

The server starts on `http://localhost:5001`. You'll be redirected to the onboarding wizard on first visit.

### 3. Onboard a league

The wizard walks you through:

1. **Connect** — Paste your StatsPlus session cookie and league slug (e.g. `emlb`, `vmlb`)
2. **Configure** — Select your team, confirm league structure (divisions auto-detected)
3. **Refresh** — Pull all league data (rosters, stats, ratings, contracts). Takes 2-3 minutes.

Once complete, the dashboard is fully populated.

### 4. Add more leagues

Go to **Settings → Add League** and repeat the onboarding process. Switch between leagues via the dropdown in the nav bar.

## Project Structure

```
statsplusplus/
├── src/statsplusplus/      # Core package (typed, tested, mypy-strict)
│   ├── models/                 # Typed dataclasses — contracts between layers
│   ├── evaluation/             # Pure computation (composite, FV, WAR, surplus)
│   ├── config/                 # League context resolution, ratings normalization
│   ├── client/                 # StatsPlus API client
│   ├── data/                   # DB schema, pipelines (evaluation, refresh, calibrate, fv_calc)
│   ├── web/                    # Flask app factory, request-scoped context
│   ├── cli/                    # CLI entry points (spp-* commands)
│   └── utils/                  # Formatting, positions, logging
│
├── web/                    # Flask web application
│   ├── app.py                  # Core routes (team, league, player) + middleware
│   ├── settings_routes.py      # Settings/onboarding blueprint
│   ├── api_routes.py           # API/refresh blueprint
│   ├── web_league_context.py   # Per-request DB connection (shared, scoped)
│   ├── queries.py              # League-wide queries
│   ├── team_queries.py         # Team-specific queries
│   ├── player_queries.py       # Player page data
│   ├── templates/              # Jinja2 templates
│   └── static/                 # CSS, JS, favicon assets
│
├── scripts/                # CLI tools (import from package)
│   ├── draft_board.py          # CLI: Draft board, simulation, auto-draft
│   ├── trade_calculator.py     # CLI: Trade surplus balance calculator
│   ├── trade_targets.py        # CLI: Trade target finder by position
│   ├── contract_value.py       # MLB player surplus calculation
│   ├── prospect_value.py       # Prospect surplus calculation
│   └── ...                     # Additional CLI tools (see CLI Tools below)
│
├── data/                   # Runtime data (gitignored)
│   ├── app_config.json         # Active league, session cookie
│   └── <league>/               # Per-league: league.db, config/, history/, reports/
│
└── docs/                   # Design docs, analysis guides, changelog
```

## Data Flow

```
StatsPlus API → client.py → refresh.py → league.db (SQLite)
                                              ↓
                                     web/app.py (Flask)
                                              ↓
                                        Browser UI
```

`refresh.py` handles the full pipeline: fetches rosters, batting/pitching/fielding stats, ratings, contracts, and team stats for all teams in the league, then computes league averages and runs FV/surplus calculations. The web layer reads from SQLite only.

## Player Evaluation Model

Stats++ builds its own player evaluation independent of the game's OVR/POT ratings. This gives you a second opinion grounded in tool analysis and league-calibrated data.

### Composite Score (20-80 scale)

Every player gets a **composite score** — a tool-weighted assessment of their current ability. For hitters, this combines offensive tools (contact, gap, power, eye, K-rate), baserunning (speed, steal), and defense (position-specific weights). For pitchers, it combines stuff, movement, control, and arsenal depth/quality.

The composite is the same methodology for prospects and MLB players, providing a unified basis for comparison across the prospect/MLB threshold. MLB players additionally get a **stat-blended score** that incorporates recent performance (OPS+, ERA/FIP), shown alongside the pure tool score on player pages.

Tools below the MLB floor (35 on the 20-80 scale) receive an additional penalty — empirical analysis shows MLB hitters with a sub-35 tool underperform their OVR by ~0.3-0.5 WAR per season.

### FV Grade and Risk Label

Prospects receive an **FV grade** (Future Value) on the standard 20-80 scale in 5-point increments (40/45/50/55/60/65/70). FV answers: *how good could this player become if he develops?*

FV is computed as ceiling quality relative to the MLB positional median. A prospect whose ceiling projects as an above-average MLB starter at their position gets FV 50. The grade scales with how far above (or below) the positional median their ceiling sits, weighted by how much of that ceiling they've already realized.

A separate **risk label** (Low / Medium / High / Extreme) captures development probability. Risk is derived from the player's age, gap between current ability and ceiling, empirical gap closure rates for the league, and character traits (work ethic, intelligence). This separation means FV tells you the ceiling quality while risk tells you the likelihood of getting there.

### Surplus Value

Every player — prospect and MLB — has a **surplus value** in dollars. This is the difference between projected on-field value and salary cost over remaining team control. Surplus is the currency of trades: a fair trade has roughly equal surplus on both sides.

For prospects, surplus projects six years of team control using the FV grade, position-specific WAR tables, aging curves, and development discounts by level. For MLB players, surplus uses a blend of stat-based and ratings-based WAR projections against actual contract costs.

### League Calibration

All model parameters are derived from each league's own data during calibration:

- **Tool weights** — Per-position regression of tool ratings against WAR
- **WAR curves** — OVR-to-WAR and Composite-to-WAR tables per position
- **Development curves** — Gap closure rates, age runway, and expected gaps from cross-sectional OVR/POT analysis
- **Financial tables** — Arbitration percentages, scarcity multipliers, years-to-MLB by level

This means the model adapts to each league's settings (development speed, aging, financial structure) rather than using one-size-fits-all assumptions.

For full model details, see `docs/valuation_model.md`.

## CLI Tools

All scripts operate on the active league (set in `data/app_config.json` or via the web UI).

```bash
# Full league refresh
python3 scripts/refresh.py [year]

# Generate analysis scaffolds for your team
python3 scripts/roster_analysis.py
python3 scripts/farm_analysis.py

# League-wide queries
python3 scripts/prospect_query.py
python3 scripts/free_agents.py
python3 scripts/standings.py
python3 scripts/standings.py --team ANA    # Team-specific playoff picture

# Trade evaluation
python3 scripts/trade_targets.py --bucket SP
python3 scripts/trade_assets.py --team ANA
python3 scripts/team_needs.py --team ANA
python3 scripts/trade_calculator.py --trade '<json>'

# Draft
python3 scripts/draft_board.py pick 6      # Pre-draft ranked list for pick #6
python3 scripts/draft_board.py upload      # Generate auto-draft file
python3 scripts/draft_board.py sim 6       # Simulate draft from pick #6
```

After `pip install -e .`, short commands are also available:

```bash
spp-standings --actual
spp-targets --bucket SP --sellers-only
spp-needs --team ANA
spp-draft pick 6
spp-trade --offer '...' --receive '...'
spp-fa --my-team
spp-prospects team ANA --n 20
```

## Configuration

League data lives in `data/<league>/config/`:

- **`state.json`** — Current game date and year
- **`league_settings.json`** — Team names/abbreviations, role/position maps, division structure, minimum salary, DH rules
- **`league_averages.json`** — Computed batting/pitching averages and $/WAR

Global config in `data/app_config.json`:

- **`active_league`** — Which league slug is currently selected
- **`statsplus_cookie`** — Session cookie for API access

## Tech Stack

- **Backend**: Python, Flask, SQLite
- **Frontend**: Server-rendered Jinja2 templates, vanilla JS, CSS custom properties (dark theme)
- **Data source**: StatsPlus REST API (CSV exports for ratings)
- **No external JS frameworks** — lightweight, fast, works offline after refresh

## License

MIT License. See [LICENSE](LICENSE) for details.

## Getting Your StatsPlus Cookie

The StatsPlus API requires authentication via session cookie. To get yours:

1. Log in to [statsplus.net](https://statsplus.net/) in your browser
2. Open Developer Tools (F12) → Application tab → Cookies → `statsplus.net`
3. Copy the values for `sessionid` and `csrftoken`
4. Format as: `sessionid=<value>;csrftoken=<value>`

Paste this into the onboarding wizard when prompted. The cookie is stored locally in `data/app_config.json` and never transmitted anywhere except to the StatsPlus API.

Cookies expire periodically — if refreshes start failing with authentication errors, grab a fresh cookie from your browser.

## Troubleshooting

**"No module named flask"** — Run `.venv/bin/pip install -r requirements.txt` from the project root.

**Refresh fails or times out** — The StatsPlus API can be slow. Ratings exports in particular may take 45+ seconds. The refresh will retry automatically. If it consistently fails, check that your session cookie is still valid.

**Empty data after refresh** — Some StatsPlus leagues don't expose minor league stats via the API. MLB-level stats should always populate. Check the DB validation counts in the Settings page.

**Port already in use** — The default port is 5001. If it's taken, edit the `app.run()` call in `web/app.py`.
