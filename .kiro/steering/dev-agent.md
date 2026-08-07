# Stats++ Development Agent

Stable rules and conventions for developing the Stats++ application.
Update only when project fundamentals shift, not for incremental feature work.

---

## Context Loading

### Tier 1 — Always loaded (read on every session start)

- `PURPOSE.md` — project goals and design principles
- `STRUCTURE.md` — directory layout, DB tables, key conventions
- `RULES.md` — data pull/storage rules, refresh workflow
- `docs/task_list.md` — open work items, bugs, backlog

These give you the project shape and current work state. Do not skip them.

### Tier 2 — Load on first code touch

Read these when you're about to modify or analyze code, not for planning/discussion:

- `docs/system_overview.md` — architecture, data flow, DB schema, design decisions, workflows
- `docs/tools_reference.md` — CLI tools, query functions, data sources

### Tier 3 — Load on demand

Only load when the task directly involves that area. Use the gate table below.

| Task type | Load these |
|---|---|
| Web UI (routes, templates, queries) | `docs/system_overview.md` §Web UI, `.kiro/specs/ui_spec.md` |
| Valuation models (FV, surplus, WAR) | `docs/assistant_gm_requirements.md`, `docs/valuation_model.md`, `scripts/constants.py`, `scripts/calibrate.py` |
| FV grade calculation | `scripts/fv_model.py` |
| WAR projection / stat history | `scripts/war_model.py` |
| Arb salary / service time | `scripts/arb_model.py` |
| Rating normalization | `scripts/ratings.py` |
| Trade/prospect features | `docs/trade_analysis_guide.md`, `docs/trade_target_workflow.md`, `.kiro/specs/phase4-trade-analysis.md`, `.kiro/specs/trade-review-tab.md` |
| Farm system / prospect analysis | `docs/farm_analysis_guide.md`, `docs/prospect_query_guide.md` |
| Roster analysis | `docs/roster_analysis_guide.md`, `docs/org_overview_guide.md` |
| Depth chart | `docs/depth_chart_spec.md` |
| StatsPlus API / client changes | `docs/client_reference.md` |
| DB schema / migrations | `docs/system_overview.md` §DB Tables, `scripts/db.py` |
| Multi-league support | `docs/multi_league_spec.md` |
| OOTP domain knowledge | `docs/ootp/ratings_and_attributes.md`, `docs/ootp/financial_model.md`, `docs/ootp/aging_and_development.md` |
| Code architecture / refactoring | `docs/code_cleanup.md` |
| Strategic planning | `docs/expansion_roadmap.md` |
| SQLite migration | `.kiro/specs/sqlite-migration.md` |
| League sync | `.kiro/specs/phase3-league-sync.md` |
| Game history | `.kiro/specs/game_history_spec.md` |
| End-of-session docs | `docs/changelog.md`, `docs/task_list.md` |
| Historical context ("when/why did X change?") | `docs/changelog.md` |
| Beat reporter agent | `.kiro/steering/beat-reporter.md` |
| User-facing setup / troubleshooting | `README.md` |

---

## Session Workflow

Every session that modifies code, schema, or project conventions must end with a
documentation pass.

### During the session

- When a design decision is made, note it — it goes into the relevant doc at session end.
- When a new query function, route, or DB column is added, note it for system_overview.

### End-of-session documentation checklist

1. **`docs/task_list.md`** — add new backlog items. Remove completed items (they go to changelog).
2. **`docs/changelog.md`** — add completed items under the current session heading.
3. **`docs/system_overview.md`** — update if scripts, routes, DB tables, query functions, data flow, UI layout, or design decisions changed.
4. **`STRUCTURE.md`** — update if files/directories were added or removed.
5. **Guide docs** (`farm_analysis_guide.md`, `roster_analysis_guide.md`, `trade_analysis_guide.md`) — update only if methodology changed.
6. **`docs/tools_reference.md`** — update if any script, query function, or data source interface changed.
7. **`RULES.md`** — update only if data pull/storage conventions changed.
8. **Discord patch notes** — post a summary of the session's changes to Discord. Skip if `data/discord_config.json` doesn't exist (webhook not configured on this environment).

   **Format requirements:**
   - **Title**: concise and descriptive of what changed — e.g. "WAR Projection Overhaul & Standings Tools", "Pitcher Evaluation Fixes", "Draft Board UX Improvements". NOT "Session X" or "Stats++ Update".
   - **Content**: write fresh bullet points for this post. Do NOT repeat items already posted in a prior Discord message. Each bullet should be complete (no truncation/ellipsis).
   - **Tracking**: check `data/discord_posts.json` for `last_posted_commit`. Only include changes from commits AFTER that hash. After posting, update the file with the new commit hash, date, and title.
   - **Sections**: group into Bug Fixes / Improvements / other categories as appropriate. Omit empty sections.
   - **Tone**: concise but informative. One sentence per bullet explaining what changed and why it matters.
   - Post using `discord_post.py message` with a custom formatted embed (not `latest` which parses changelog with truncation). See prior session examples for the Python webhook pattern.

### What does NOT need updating

- CSS-only changes, template formatting tweaks, sort order changes
- Bug fixes that don't change interfaces or conventions
- Intermediate work that gets revised before session end

---

## Code Style & Principles

- Minimal code — no surplus abstractions, no speculative generalization.
- **Team/league agnostic** — no hardcoded team IDs, league size, year, or org-specific
  assumptions. Use `my_team_id` from `state.json`, pass team/league context as parameters.
- **All logic in the package** (`src/statsplusplus/`). `scripts/` contains only CLI tools
  that import from the package. No utility modules, no shared libraries in `scripts/`.
- **No singletons or global state.** Every function receives what it needs as parameters.
  Entry points (CLI `main()`, web `before_request`) resolve context and pass it through.
- **Typed interfaces** — cross-module interfaces use dataclasses from `statsplusplus.models`.
  Pure computation goes in `statsplusplus.evaluation` (no I/O, no DB, no global state).
- `statsplusplus.evaluation.constants` — single source of truth for all model constants.
- `statsplusplus.config.ratings` — pure normalization functions taking explicit `scale` parameter.
- `statsplusplus.config.league_config` — `LeagueConfig` class, `dollars_per_war(league_dir)`, `league_minimum(league_dir)`.
- `statsplusplus.data.db` — connection management (request-scoped in web, explicit `league_dir` in CLI).
- SQLite WAL mode for concurrent reads during writes.
- Web layer is read-only against the DB. All writes go through `data/` pipelines.
- **Do not run tests** unless the user explicitly asks to run them.
- **Do not push to git** without explicit user permission. Prepare commits but wait for approval before pushing.

---

## Data Rules

All data fetched and written to `league.db` by `scripts/refresh.py` from the StatsPlus API.
No JSON data files read by analysis scripts (config files in `data/<league>/config/` are
the exception). MCP tools are for targeted interactive queries only.

### Refresh: `python3 scripts/refresh.py [year]`

Fetches game date → updates state → pulls all data → computes league averages → runs
fv_calc (FV + surplus). Idempotent on same game date.

### IP storage

API returns `ip` as truncated integer; `outs` is precise. DB stores true decimal innings
(`outs / 3`). ERA from outs (`er * 27 / outs`). Display via `fmt_ip` Jinja filter.

### Pitcher WAR

Blended: `(war + ra9war) / 2`. Fall back to `war` alone if `ra9war` is NULL.

---

## Web UI Conventions

- All DB access through query modules (`queries.py`, `team_queries.py`, `player_queries.py`,
  `percentiles.py`) — no business logic in queries, no direct DB in templates/routes.
- Templates use `base.html` layout shell. Dark theme, no CSS/JS frameworks.
- `sort.js` handles client-side table sorting with smart rank renumbering.
- Team pages parameterized by `team_id`. `my_team_id` controls highlighting/defaults.
- Column alignment: Name/Team left, numeric right, Pos/Role left via `data-sort-value`.
- Surplus color-coded: green (positive), red (negative).

---

## Auto-Write Permissions

These writes do not require user confirmation:

- `data/<league>/reports/<year>/*.md` — final reports
- `data/<league>/history/prospects.json` — scouting summaries and FV snapshots
- `data/<league>/history/roster_notes.json` — MLB player summaries
- `data/<league>/tmp/*.md` — intermediate scaffold files
- `data/<league>/config/state.json` — game date and year state
- `docs/task_list.md` — task status updates

After writing a report, do not print full contents to terminal. Output only the file
path and a brief summary.
