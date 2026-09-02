# Data Pull & Storage Rules

## Golden Rule

All data is fetched and written to SQLite by the refresh pipeline
(`python3 -m statsplusplus.data.refresh`) via the StatsPlus API.
The web layer is read-only. MCP tools are for targeted interactive queries only.

---

## Pull Rules

1. **Single pipeline.** All data flows through `refresh.py` → `league.db`. No JSON data files
   are written or read by analysis scripts (config files in `data/<league>/config/` are the exception).

2. **Default split is overall (`split=1`).** Splits 2 (vs L) and 3 (vs R) are also pulled for the current
   and prior year during refresh. Historical years (year-2 and older) only pull overall.

3. **Refresh before analysis.** Either use the web UI refresh button or run `python3 -m statsplusplus.data.refresh`
   to ensure data is current before any analysis.

4. **Farm stats are unavailable.** The StatsPlus API returns empty for minor league batting/pitching/fielding
   stats. Only MLB-level stats are populated.

---

## Storage Layout

Each league's data lives in `data/<league>/`:

| Path | Contents |
|------|----------|
| `league.db` | SQLite database — all player, team, stat, contract, and ratings data |
| `config/state.json` | Current game date, year, my_team_id |
| `config/league_settings.json` | Team names/abbreviations, division structure, role/pos/level maps, financial settings |
| `config/league_averages.json` | Computed league-wide batting/pitching averages and $/WAR |
| `history/prospects.json` | Scouting summaries and FV history (keyed by player_id) |
| `history/roster_notes.json` | MLB player summaries (keyed by player_id) |
| `reports/<year>/` | Published analysis reports |
| `tmp/` | Intermediate scaffolds (script output for report writing) |

Global config: `data/app_config.json` — active league slug and StatsPlus session cookie.

---

## Refresh Workflow

### Via web UI (recommended)
Click the ⟳ Refresh button in the nav bar. Takes 2-3 minutes. The site remains browsable during refresh.

### Via CLI
```bash
python3 -m statsplusplus.data.refresh [year]
```

This fetches the game date from the API, updates `config/state.json`, pulls all data for all teams,
computes league averages and $/WAR, and runs FV/surplus calculations.

Re-running on the same game date is idempotent.

### Analysis scaffolds (separate from refresh)
```bash
python3 scripts/farm_analysis.py      # Farm system report for your team
python3 scripts/roster_analysis.py    # MLB roster scaffold for your team
```

---

## Farm Analysis Rules

- **Never read raw data files during farm analysis.** Run `scripts/farm_analysis.py` and read its output scaffold.
- **Do not perform FV calculations manually.** The script handles bucketing, normalization, and FV math.
- **Do not read `scripts/farm_analysis.py` source code** during an analysis run — treat it as a black box.

---

## Prospect Readiness Signals

When evaluating whether a prospect is ready to advance:
- **Age vs. level** — Is the player old for their current level?
- **Stats vs. league average** — Significantly outperforming peers?
- **Ratings** — High overall/potential relative to current level?
- **Service time at level** — Has the player had a full season to develop?
