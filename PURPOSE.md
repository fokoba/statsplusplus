# Purpose

Stats++ is a multi-league assistant GM dashboard for OOTP Baseball leagues
managed through StatsPlus. It pulls live league data from the StatsPlus API
and presents it as an interactive local web dashboard.

## What It Does

- **League overview** — Standings, stat leaders, power rankings, league averages
- **Team pages** — Roster, depth chart, farm system, contracts, payroll, upcoming FAs
- **Player pages** — Ratings with grade bars, splits, percentile rankings with expected-value tags
- **Prospect system** — Top 100, by-team/position views, FV grades, surplus values, scouting panels
- **CLI analysis tools** — Roster scaffolds, farm reports, prospect rankings, trade calculator
- **Multi-league** — Manage multiple leagues from one install, switch via nav dropdown

## Design Principles

- All data flows through the refresh pipeline (`statsplusplus.data.refresh`) → SQLite. The web layer is read-only.
- League-specific config lives in `data/<league>/config/`. No hardcoded team IDs or league structure.
- Extended OOTP ratings (BABIP, HRA, PBABIP, Prone) are supported when the league provides them.
- Graceful degradation — features that depend on extended ratings simply don't render for leagues without them.
