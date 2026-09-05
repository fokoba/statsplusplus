# StatsPlus Client Reference

Python API client at `statsplus/client.py`. Credentials resolved from league context
(`data/app_config.json` → `league_settings.json`) or explicit `configure()`.
All methods return parsed data (list of dicts, or dict) and raise on error.

Import: `from statsplus import client`

Wiki source: https://wiki.statsplus.net/web-tools/statsplus-api (last updated 2026-07-14)

---

## Authentication

Two methods:

1. **Session cookie** — `sessionid=<value>;csrftoken=<value>` from browser. Used by our client.
2. **Team token** — `?token=XXXX` query parameter from the Preferences page on StatsPlus.
   Valid for one team in one league. Works without cookie for `/ratings` and `/tradeblock`.

Cookie stored in `data/app_config.json`. Expires periodically — refresh from browser if auth fails.

---

## Roster & Teams

### `get_players() -> list[dict]`
All players across all orgs and levels. Supports `?retired=0` filter.

**Core fields (currently stored):**
`ID`, `First Name`, `Last Name`, `Team ID`, `Parent Team ID`, `Level`, `Pos`, `Role`, `Age`, `Retired`

**Extended fields (available, not yet stored — added April/July 2026):**

| Field | Type | Description |
|---|---|---|
| `Organization ID` | int | Reliable org assignment (sometimes `Parent Team ID` is unset for MLB teams) |
| `League ID` | int | Negative for international complex players |
| `date_of_birth` | str | Player DOB |
| `height` | int | Player height |
| `weight` | int | Player weight |
| `bats` | int | Batting hand |
| `throws` | int | Throwing hand |
| `draft_year` | int | Year drafted |
| `draft_round` | int | Round drafted |
| `draft_supplemental` | int | Supplemental pick flag |
| `draft_pick` | int | Pick within round |
| `draft_overall_pick` | int | Overall pick number |
| `draft_team_id` | int | Team that drafted the player |
| `draft_league_id` | int | League ID at time of draft |
| `hall_of_fame` | int | HOF flag |
| `inducted` | int | HOF induction year |
| `uniform_number` | int | Jersey number |
| `is_active` | int | Active roster flag |
| `is_on_secondary` | int | On secondary (e.g., taxi squad) |
| `is_on_waivers` | int | Currently on waivers |
| `designated_for_assignment` | int | DFA'd |
| `is_on_dl` | int | On disabled list (short-term) |
| `is_on_dl60` | int | On 60-day DL |
| `dl_days_this_year` | int | Days spent on DL this season |
| `mlb_service_years` | int | MLB service — completed full years (= floor(days/172)) |
| `mlb_service_days` | int | MLB service — **cumulative total days** (full year = 172 days) |
| `mlb_service_days_this_year` | int | MLB service days accrued this season |
| `pro_service_years` | int | Professional service — completed full years |
| `pro_service_days` | int | Professional service — cumulative total days |
| `pro_service_days_this_year` | int | Pro service days this season |
| `secondary_service_years` | int | Secondary (MiLB) service — completed full years |
| `secondary_service_days` | int | Secondary service — cumulative total days |
| `secondary_service_days_this_year` | int | Secondary service days this season |
| `days_on_waivers` | int | Days spent on waivers |
| `days_on_waivers_left` | int | Days remaining on waivers |
| `has_received_arbitration` | int | Has been offered arbitration |
| `was_traded` | int | Traded this season flag |
| `free_agent` | int | Free agent flag |
| `nation_id` | int | Nationality ID |
| `last_team_id` | int | Previous team (before trade/FA) |
| `years_protected_from_rule_5` | int | Rule 5 protection remaining |
| `draft_eligible` | int | Eligible for upcoming draft |
| `injury_is_injured` | int | **Currently injured** |
| `injury_dl_left` | int | Days left on DL |
| `injury_left` | int | Days until fully healthy |

Level values: 1=ML, 2=AAA, 3=AA, 4=A, 5=Short-A, 6=Rookie, 7=Indy, 8=International, 10=College, 11=High School

```python
roster = client.get_players()
angels_ml = [p for p in roster if p["Parent Team ID"] == 44 and p["Level"] == 1]

# Injured players
injured = [p for p in roster if p.get("injury_is_injured") == 1]

# DFA'd players
dfa = [p for p in roster if p.get("designated_for_assignment") == 1]
```

### `get_teams() -> list[dict]`
Team ID ↔ name mapping for all teams.

Key fields: `ID`, `Name`, `Nickname`, `Parent Team ID`

---

## Game Info

### `get_date() -> str`
Current game date as a string, e.g. `"2033-04-22"`.

### `get_exports() -> dict`
Export status for the last 10 game dates. Returns a dict with `current_date` and one
key per game date mapping to a list of team IDs with valid exports.

```python
exports = client.get_exports()
print(exports["current_date"])   # "2033-04-22"
print(exports["2033-04-22"])     # [44, 12, 7, ...]
```

### `get_game_history(year: int = None) -> list[dict]`
All major league games since the league started. Includes score, starters, W/L/S pitchers, date, game type.

Key fields: `game_id`, `league_id`, `home_team`, `away_team`, `attendance`, `date`, `time`,
`game_type`, `played`, `dh`, `innings`, `runs0`, `runs1`, `hits0`, `hits1`, `errors0`, `errors1`,
`winning_pitcher`, `losing_pitcher`, `save_pitcher`, `starter0`, `starter1`, `cup`

---

## Player Stats

All three stat methods share the same optional parameters:

| Param | Type | Description |
|---|---|---|
| `year` | int | Season year. Defaults to current year (all years if `pid` also set). **Repeatable.** |
| `pid` | int | Single player ID. Omit for all players |
| `split` | int | 1=Overall, 2=vsL, 3=vsR, 21=Playoffs. Omit for all splits |
| `lid` | int | League ID filter. Defaults to primary top-level league. **Repeatable.** |

**Important:** The `lid` parameter enables **minor league stats**. Pass the appropriate
league ID to get stats for that level:

| Level | League IDs (eMLB) |
|---|---|
| MLB | 150 |
| AAA | 151, 152 |
| AA | 153, 154, 155 |
| A | 156, 157, 158, 159, 160 |
| Rookie | 165 |

League IDs are league-specific. Use `/lgdata` to discover the full hierarchy for any league.
Multiple `lid` params can be combined: `?lid=151&lid=152` returns both AAA leagues.

### `get_player_batting_stats(...) -> list[dict]`
Player batting stat lines. Each row is one player+year+split combination.

Key fields: `player_id`, `year`, `team_id`, `league_id`, `level_id`, `split_id`,
`ab`, `h`, `d`, `t`, `hr`, `r`, `rbi`, `sb`, `cs`, `bb`, `k`, `pa`, `pitches_seen`,
`g`, `gs`, `ibb`, `gdp`, `sh`, `sf`, `hp`, `ci`, `wpa`, `stint`, `ubr`, `war`

```python
# MLB stats (default behavior)
stats = client.get_player_batting_stats(year=2033, split=1)

# AAA stats
aaa_stats = client.get_player_batting_stats(year=2033, split=1, lid=151)

# Multiple minor leagues at once
milb_stats = client.get_player_batting_stats(year=2033, split=1, lid=151)  # per-lid call
```

### `get_player_pitching_stats(...) -> list[dict]`
Player pitching stat lines.

Key fields: `player_id`, `year`, `team_id`, `league_id`, `level_id`, `split_id`,
`outs`, `ab`, `tb`, `ha`, `k`, `bf`, `rs`, `bb`, `r`, `er`, `gb`, `fb`, `pi`,
`g`, `gs`, `w`, `l`, `s`, `sa`, `da`, `sh`, `sf`, `ta`, `hra`, `bk`, `ci`, `iw`, `wp`, `hp`,
`gf`, `dp`, `qs`, `svo`, `bs`, `ra`, `cg`, `sho`, `sb`, `cs`, `hld`, `ir`, `irs`,
`wpa`, `li`, `stint`, `sd`, `md`, `war`, `ra9war`

### `get_player_fielding_stats(...) -> list[dict]`
Player fielding stats by position. No splits — `split` param is ignored.
Supports `year`, `pid`, and `lid` parameters.

Key fields: `player_id`, `year`, `team_id`, `league_id`, `level_id`, `position`,
`tc`, `a`, `po`, `er`, `ip`, `g`, `gs`, `e`, `dp`, `tp`, `pb`, `sba`, `rto`, `ipf`, `plays`, `plays_bas`

**Note:** Minor league fielding returns data via `lid` parameter (confirmed working for AAA/AA).

---

## Team Stats

### `get_team_batting_stats(year: int = None, split: int = None) -> list[dict]`
Team batting stats for major league teams. Split values: 1=Overall, 2=vsL, 3=vsR.

### `get_team_pitching_stats(year: int = None, split: int = None) -> list[dict]`
Team pitching stats for major league teams.

```python
team_bat = client.get_team_batting_stats(year=2033, split=1)
angels_bat = next(r for r in team_bat if r.get("tid") == 44)
```

---

## Contracts

### `get_contracts() -> list[dict]`
All current active contracts across the league, including farm players.

**Currently stored fields:**
`player_id`, `team_id`, `contract_team_id`, `is_major`, `season_year`,
`salary0`–`salary14`, `years`, `current_year`, `no_trade`,
`last_year_team_option`, `last_year_player_option`

**Additional fields available (not yet stored):**

| Field | Description |
|---|---|
| `league_id` | Contract league context |
| `last_year_vesting_option` | Vesting option in final year |
| `next_last_year_team_option` | Team option in penultimate year |
| `next_last_year_player_option` | Player option in penultimate year |
| `next_last_year_vesting_option` | Vesting option in penultimate year |
| `contract_league_id` | League where contract was signed |
| `minimum_pa` | PA threshold for incentive |
| `minimum_pa_bonus` | Bonus if PA threshold met |
| `minimum_ip` | IP threshold for incentive |
| `minimum_ip_bonus` | Bonus if IP threshold met |
| `mvp_bonus` | MVP award bonus |
| `cyyoung_bonus` | Cy Young award bonus |
| `allstar_bonus` | All-Star selection bonus |
| `next_last_year_option_buyout` | Buyout for penultimate year option |
| `last_year_option_buyout` | Buyout for final year option |

### `get_contract_extensions() -> list[dict]`
Signed extensions that take effect in future seasons. Same schema as contracts. May be empty.

---

## Ratings

### `get_ratings(player_ids: list[int] = None, poll_url: str = None) -> list[dict]`
Ratings for all active players (scouted if league uses scouts, otherwise OSA). This endpoint
enforces a ~4 min rate limit between requests.

- `player_ids` — optional filter to specific player IDs (full job still runs, filtered client-side)
- `poll_url` — pass a previously returned poll URL to skip job startup

**OSA ratings:** Add `&osa=1` to the initial `/ratings` request to get OSA ratings instead
of scouted ratings. Not yet implemented in client.

The endpoint is async: the initial request returns a poll URL; the client polls until CSV is ready.
Export takes at least 30 seconds; client handles retries automatically (up to ~5 minutes).

**International complex players** have a negative `League` field (e.g. `-150`).

Key fields: `ID`, `Name`, `Team`, `Pos`, `Ovr`, `Pot`, plus per-attribute ratings.

Two known column formats:
- **113-column** — older OOTP versions
- **126-column** — adds BABIP, HRA, PBABIP splits + PotBABIP/PotHRA/PotPBABIP + Prone

Overall/Potential are stored as `(stars × 2)`, e.g. 3.5 stars = `7`.

The API has a known bug: mislabels Ctrl columns (Ctrl_R → Ctrl, first Ctrl_L → Ctrl_R).
Client's `_fix_ratings_header()` corrects this automatically.

### `start_ratings_export() -> str`
Kick off the ratings export and return the poll URL without waiting. Used by refresh to
overlap the export generation with other API calls.

---

## League Structure

### `/lgdata` ⚡ NEW — Not Yet Implemented

Returns a single JSON structure with complete league hierarchy and current standings.

**Top-level keys:** `leagues`, `subleagues`, `divisions`, `teams`, `standings`

```python
# Not yet wrapped in client.py
data = client._json("/lgdata/")
```

| Key | Fields |
|---|---|
| `leagues[]` | `league_id`, `name`, `abbr`, `level`, `state`, `parent_league_id`, `primary_league` |
| `subleagues[]` | `league_id`, `sub_league_id`, `name`, `abbr`, `designated_hitter` |
| `divisions[]` | `division_id`, `league_id`, `sub_league_id`, `name` |
| `teams[]` | `team_id`, `name`, `nickname`, `abbr`, `league_id`, `sub_league_id`, `division_id`, `parent_team_id`, `level` |
| `standings[]` | `team_id`, `g`, `w`, `l`, `t`, `pos`, `pct`, `gb`, `streak`, `magic_number` |

**League state values:** 0=Preseason, 1=Spring Training, 2=Regular Season, 3=Playoffs, 4=Offseason

**Key uses:**
- Replace our game-frequency clustering for division detection (authoritative source)
- Real W-L standings (vs pythagorean-only)
- Minor league structure discovery (league IDs for stats queries)
- DH rule detection per subleague

---

## Trade Block

### `/tradeblock` ⚡ NEW — Not Yet Implemented

Returns JSON list of all player IDs currently on the trade block in the league.

```python
# Not yet wrapped in client.py
data = client._json("/tradeblock/")
# {"player_ids": [20394, 25681, 28271, ...]}
```

Requires authentication (session cookie or team token).

**Key uses:**
- Flag confirmed-available targets in `trade_targets.py`
- Surface trade block players in trade workbench UI
- Reduce false positives in target identification

---

## Ballparks

### `/ballparks` ⚡ NEW — Not Yet Implemented

Returns JSON with OOTP park factors, capacity, stadium type, and surface for all ballparks.
Optional `?lid=N` parameter for specific league.

```python
# Not yet wrapped in client.py
data = client._json("/ballparks/")
```

Response structure:
```json
{
  "league_id": 0,
  "ballparks": [
    {
      "team_id": 44,
      "league_id": 150,
      "park_id": 25,
      "name": "Anaheim",
      "nickname": "Angels",
      "display_name": "Anaheim Angels",
      "abbr": "ANA",
      "avg_r": 0.96,
      "avg_l": 0.97,
      "avg": 0.9635,
      "d": 0.96,
      "t": 0.71,
      "hr_r": 1.03,
      "hr_l": 1.12,
      "hr": 1.0615,
      "capacity": 46000,
      "stadium_type": "Outdoor",
      "surface": "Grass"
    }
  ]
}
```

**Key uses:**
- Park-adjusted projections and stat normalization
- Context for player evaluation (pitcher-friendly vs hitter-friendly)
- Display on team pages

---

## Draft

### `get_draft(lid: int = None) -> list[dict]`
Current draft status — players picked so far. Returns full draft results when complete.

Key fields: `ID`, `Round`, `Pick In Round`, `Supp`, `Overall`, `Player Name`, `Team`,
`Team ID`, `Position`, `Age`, `College`, `Auto Pick`, `Time (UTC)`

---

## Notes

### What IS Available via API
- ✅ Minor league stats (batting, pitching, fielding) — via `lid` parameter
- ✅ Injury status — via expanded `/players` fields
- ✅ Service time (exact years + days) — via expanded `/players` fields
- ✅ DFA/waiver status — via expanded `/players` fields
- ✅ Trade block — via `/tradeblock` endpoint
- ✅ Real standings (W-L-GB) — via `/lgdata` endpoint
- ✅ Park factors — via `/ballparks` endpoint
- ✅ Full league hierarchy (minor league IDs) — via `/lgdata` endpoint
- ✅ Draft history — via `/players` draft fields
- ✅ Contract incentives/options — via expanded `/contract` fields
- ✅ OSA ratings (in addition to scouted) — via `&osa=1` parameter

### What is NOT Available
- ❌ Transaction log / trade history
- ❌ Game-by-game player stats (box scores)
- ❌ Advanced Statcast-style data (exit velo, launch angle, etc.)
- ❌ Morale / chemistry data
- ❌ Scouting reports (text-based)
- ❌ Minor league game history

### Implementation Status

| Endpoint | Client Method | Stored in DB | Used by Refresh |
|---|---|---|---|
| `/players` (core 10 fields) | `get_players()` | ✅ | ✅ |
| `/players` (extended 45 fields) | — | ❌ | ❌ |
| `/teams` | `get_teams()` | ✅ | ✅ |
| `/date` | `get_date()` | ✅ | ✅ |
| `/exports` | `get_exports()` | ❌ | ❌ |
| `/gamehistory` | `get_game_history()` | ✅ | ✅ |
| `/playerbatstatsv2` (MLB) | `get_player_batting_stats()` | ✅ | ✅ |
| `/playerbatstatsv2` (MiLB) | — | ❌ | ❌ |
| `/playerpitchstatsv2` (MLB) | `get_player_pitching_stats()` | ✅ | ✅ |
| `/playerpitchstatsv2` (MiLB) | — | ❌ | ❌ |
| `/playerfieldstatsv2` (MLB) | `get_player_fielding_stats()` | ✅ | ✅ |
| `/playerfieldstatsv2` (MiLB) | — | ❌ | ❌ |
| `/teambatstats` | `get_team_batting_stats()` | ✅ | ✅ |
| `/teampitchstats` | `get_team_pitching_stats()` | ✅ | ✅ |
| `/contract` (core) | `get_contracts()` | ✅ | ✅ |
| `/contract` (incentives/options) | — | ❌ | ❌ |
| `/contractextension` | `get_contract_extensions()` | ✅ | ✅ |
| `/ratings` (scouted) | `get_ratings()` | ✅ | ✅ |
| `/ratings` (OSA via `&osa=1`) | — | ❌ | ❌ |
| `/draftv2` | `get_draft()` | ✅ | ❌ |
| `/tradeblock` | — | ❌ | ❌ |
| `/ballparks` | — | ❌ | ❌ |
| `/lgdata` | — | ❌ | ❌ |
