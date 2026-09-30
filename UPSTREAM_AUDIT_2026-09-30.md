# Upstream audit — v1.10.4 through v1.13.1 (48 commits), 2026-09-30

## Scope and method

`git fetch upstream && git log main..upstream/main --oneline` lists 48 commits
not in our history (`4a7f770`..`c609842`, roughly upstream v1.10.4→v1.13.1).
This is a **research/report task only** — no code changes were made beyond
this file. Prior session-level context: `git diff upstream/main --stat` shows
~100 files differ (~18k insertions / ~4.5k deletions), but that number
reflects two codebases evolving the *same* files independently, not missing
functionality — every file that differs already exists in our tree in some
form.

Method: walked the 48-commit list, skipped pure `chore: bump version` /
`chore: update discord post record` commits (not content), read every
substantive commit's diff/message, then diffed the specific files it touched
against our current tree (`diff <(git show upstream/main:<file>) <file>`) to
check whether the same ground is already covered, partially covered, or
genuinely missing.

## Headline finding

**Nothing found in this sweep is a genuine gap worth a porting task.**
Every substantive upstream commit in this range turned out to be either
(a) already present in our tree — often ported before, sometimes
byte-identical — or (b) superseded by different, already-shipped work in this
fork that solves the same underlying problem a different way. This matches
the outcome of the *previous* upstream-check pass (the FV-rounding-cliff /
composite-centering fix, confirmed byte-identical fv.py).

## Per-commit-cluster findings

### 1. `4a7f770` — composite→WAR alignment gap closed as a data ceiling (docs only)
**Classification: (a) already-relevant context, no code to port.**
Pure documentation commit (`docs/changelog.md`, `docs/task_list.md`, no code).
Upstream's own investigation concluded the eMLB-vs-vMLB/PPL same-year
composite→WAR correlation gap is a **data ceiling** from OOTP's compressed
source ratings in those leagues, not a model defect — the composite is at/
above the optimal achievable tool-fit in every league, ranks players
monotonically by actual WAR everywhere, and beats prior-year stats badly
against *next-year* WAR (0.71 vs 0.42 on vMLB). This is directly relevant
background for how to think about this fork's own composite/WAR alignment
work (the empirically-recalibrated per-league peak-age work done this
session touches the same evaluation surface). Nothing to port — it's a
finding, not a fix. Worth reading if/when this fork revisits composite
calibration, but doesn't block or require any action.

### 2. `c25ac63` / `6f129e3` — per-facet aging (P1) + per-facet development + dev_speed tie-in (P2/P3)
**Classification: (a) already covered — byte-identical.**
Checked file-by-file against our tree:
- `src/statsplusplus/evaluation/facet_runs.py` — **byte-identical** to
  upstream (0-line diff).
- `src/statsplusplus/evaluation/player_value.py` — **byte-identical** to
  upstream (0-line diff).
- `src/statsplusplus/evaluation/war.py` — differs only by this fork's later,
  additive WAR-pace-tracking feature (current-season pace tiers/confidence,
  `war_pace()`), which upstream doesn't have. No overlap or conflict with the
  aging logic.
- `src/statsplusplus/data/fv_calc.py` — differs substantially (747 lines),
  but that's this fork's independent surrounding feature work, not a missing
  piece of the aging/dev commits (the facet-aging call site itself,
  `facet_aging_mult`, is present and wired the same way — confirmed via
  `grep` in `player_value.py`).

This fork already has upstream's per-facet aging (bat/baserunning/fielding
age on separate curves, weighted by facet run-share) and per-facet
development curves + dev_speed pace-modifier tie-in, apparently already
ported in an earlier session. The `.kiro/specs/per-facet-aging-projection/`
design doc from upstream wasn't checked for presence but is inconsequential
(a design doc, not runtime behavior).

**On the "is our per-league peak-age recalibration redundant with upstream's
per-facet aging" question the task specifically asked about:** they are
**not** in tension — they're orthogonal axes. Upstream's per-facet aging
splits *one* age curve into three (bat/baserunning/fielding), each still
built around the same global `PEAK_AGE_HITTER`/`PEAK_AGE_PITCHER` constants
(28/27) defined in `evaluation/constants.py`. This fork's per-league peak-age
recalibration (per project memory) would apply *across* leagues, independent
of the bat/baserunning/fielding split. However: **the per-league
recalibration does not appear to be wired into code yet** — `constants.py`
still has a single global `PEAK_AGE_HITTER = 28` / `PEAK_AGE_PITCHER = 27`,
and `evaluation_engine.py` still hardcodes `peak_age = 27 if is_pitcher else
28` in two places rather than reading a per-league value. No per-league
`peak_age` key exists in either league's `tool_weights.json`. If the
per-league calibration was computed this session, it hasn't yet been
threaded through the aging/composite code — worth flagging to the user
directly (separate from this upstream-audit task) since it's not an upstream
question at all.

### 3. Fielding-runs / ceiling cluster: `2efad4a`, `c9bf665`, `3822f10`, `f1174cd`, `48c2c98`
**Classification: (a) already covered.**
Every file this cluster touches is already in our tree with the same fixes
present:
- `evaluation/facet_runs.py` — byte-identical (see above; this cluster's
  fielding-run-curve and plateau/clamp fixes are baked in).
- `evaluation/ceiling.py` — 1-line diff, and it's cosmetic (an extra
  `transforms` parameter our fork threads through elsewhere, not a missing
  fix).
- `data/calibrate.py` — 21-line diff, entirely a cosmetic difference in a
  `print()` calibration-report block (our version reports 3 pooled
  buckets instead of a per-position breakdown, matching how
  `_calibrate_tool_weights()` actually pools its regression — not a gap).
- `data/evaluation_engine.py` — 80-line diff, all attributable to this
  fork's own independent improvements (a docstring correction about
  `decompose_composite`'s lossiness, a `player_snapshot_date` correctness
  fix, `true_ceiling` column handling, per-player-row snapshot-date
  subquery instead of a single global MAX). None of it reverts or
  conflicts with the fielding-run/ceiling fixes upstream made.

### 4. `f0d71d9` — scope "MLB" to primary league, exclude co-resident NPB (evaluation/calibration layer)
**Classification: (a) already covered, via already-ported infrastructure.**
This fork already has the primary-league infrastructure this commit
introduces: `LeagueConfig.primary_league_id`,
`db.primary_league_predicate()`, `league_meta` table, and
`tests/test_cross_league_scoping.py` all exist in our tree (confirmed by
`grep -rl` across `src/` and `scripts/`, and the test file is present
verbatim). This is a *different* NPB-exclusion mechanism than the one named
in the task's scoping note: `_NIPPON_TEAM_IDS` in `web/team_queries.py`
(confirmed present, lines ~1937-2067) is an **application-layer** exclusion
used for free-agency signability filtering (excluding players
drafted-by-NPB or of Nippon nationality from FA pools), while `f0d71d9`'s
fix is an **evaluation/calibration-layer** exclusion (keeping NPB stats out
of WAR-regression training data, positional medians, org-needs, and
arb/scarcity models). They're complementary, not overlapping, and this fork
already has both.

### 5. `96bcd92` — draft board fixes + single active-league source of truth
**Classification: mixed — (a) partially covered by different architecture; two narrow sub-fixes not found verbatim.**
This fork took a structurally different (and arguably stronger) approach to
the underlying problem: rather than upstream's per-request `g.league_dir`
pattern with a raise-on-no-slug guard (`tests/test_league_dir_guard.py`,
which does **not** exist in our tree), this fork scoped the active league to
**per-browser-session** state across several dedicated commits already in
our own history: `e280592`/`cb2e953` "Scope active league to per-browser
session instead of a single global setting", `c66e58a` "Fix: scope active
league to per-browser session (closes #7)", and `6572cdd`/`b566565` cleanup.
That's a more thorough fix for the same class of bug (cross-league state
leakage from a single global) than upstream's per-request guard.

Two narrow items from this commit were **not** found replicated verbatim and
would need a specific look if they still reproduce here:
- The `dollars_per_war` blank-`$Val`-on-draft-board bug (a swallowed
  `NameError` from a missing import) — no `dollars_per_war` reference found
  in `scripts/draft_board.py` at all, so this exact failure mode may not
  apply to our version of that file, but wasn't independently verified to be
  absent.
- The specific "3 more latent instances" upstream found and patched
  (projections ratings scale, player-popup/role-map lookups) were surface-
  checked (`scripts/projections.py`, `web/player_queries.py` both already
  import league config functions with explicit league-dir threading) and
  look handled, but not exhaustively line-matched against upstream's patch.

Given this fork's per-session architecture is a different (broader) fix for
the same underlying class of bug, and the specific narrow sub-fixes show no
evidence of still being broken, this is **not** worth a porting task — if
anything, worth a quick manual sanity check of `$Val` on the draft board
next time it's opened, but not code work.

### 6. Draft-pool/board fixes: `f42bc12`, `0411fb0`, `59c5c5c`, `4629cca`
**Classification: (a) already ported.**
All three already carry "Ported from upstream tfalsone/statsplusplus
<hash>" docstring citations in `scripts/draft_board.py`/`web/queries.py`:
phantom-picks-carried-across-drafts fix (`f42bc12`, cited in
`web/queries.py:1187`), auto-draft-list-wrong-league fix (`0411fb0`, cited
in `draft_board.py`), and the stale-draft-pool-discard fix (`59c5c5c`,
cited in `draft_board.py` with an extended fork-specific comment about why
it samples the first 300 IDs). Note: project memory
(`project_pending_upstream_stale_pool_port.md`) flags `59c5c5c` as still
pending "port after the PPL draft is done" — that memory is **stale**; the
code confirms it's already in the tree. Worth updating/removing that memory
entry. The standings-preseason/retro-season fix (`4629cca`) is also already
present (preseason fallback-to-prior-year logic exists in both
`refresh.py` and `team_queries.py` at multiple call sites).

### 7. `a5c8cf1` — refresh: rate-limit pacing + ratings-export expiry recovery
**Classification: (a) already covered.**
`src/statsplusplus/client/statsplus.py` already has the rate-limit-wait
handling (`log.info("ratings: rate limited — waiting %ds...", ...)`) and a
`reexported` recovery flag for expired ratings exports. The 28-line diff
against upstream is not a missing feature.

### 8. `886ee50` — Development-speed metric v1 (display)
**Classification: (a) already ported, and since extended.**
This is the origin of this fork's own `dev_speed` system — confirmed via
this repo's own `DEV_SPEED_SCHEDULE_TAG_PORT.md`, which states the base
z-score metric "was originally ported from upstream tfalsone/statsplusplus
commit 886ee50." This fork then built a second, independent "schedule/risk"
tag on top of it this session. Fully covered and then some.

### 9. `bb7f85f` — Offseason: Season in Review tab (replaces Playoffs placeholder)
**Classification: (b) genuine gap — small, cosmetic/UX, low priority.**
No `season_in_review` / "Season in Review" string found anywhere in
`web/offseason_queries.py` or `web/templates/offseason.html`. This looks
like a real UI feature upstream added (a tab replacing a placeholder) that
this fork hasn't picked up. Scope: touches `web/offseason_queries.py` (478
lines changed upstream) and `web/templates/offseason.html` (428 lines
changed upstream) — moderate-sized, self-contained UI feature, no
evaluation-model coupling. **This is the one item in this sweep worth a
follow-up porting task if the user wants the offseason page filled out**,
but it's cosmetic, not correctness-affecting, and this fork's
`offseason_queries.py`/`offseason.html` have diverged enough (665/unknown
line diff) that a straight cherry-pick likely won't apply cleanly — it'd
need to be re-implemented against this fork's current offseason page
structure rather than ported mechanically.

## What to explicitly skip, and why

- All `chore: bump version` / `chore: update discord post record` commits —
  no content, purely upstream's own release bookkeeping.
- `fe8cbd0` "docs: log hitter tool-weight-vs-WAR-target calibration bug" and
  `ba049a5` "docs: accurately document aging/dev curve provenance" — docs-
  only commits describing upstream's own investigation process; superseded
  by this fork having already ported the actual fixes they lead up to.
- `7edc245` "feat: run-space facet hitter evaluation model" and
  `14d4932` "eval: fix FV rounding cliff + population-center the composite
  mapping" — already confirmed byte-identical / previously ported (per the
  prior audit session and `fv.py` identity check restated in the task
  brief).
- `56c937f` "docs: bring API/tools reference current with stored /players
  fields" — pure docs commit about upstream's own API surface; not
  applicable, this fork's API/tools docs are independently maintained.

## Prioritized recommendation

1. **Nothing urgent.** No correctness bug or missing evaluation-model fix
   was found uncovered in this sweep.
2. **Optional, low priority:** port/re-implement upstream's "Season in
   Review" offseason tab (`bb7f85f`) if the offseason page's current
   Playoffs placeholder is something the user actually wants replaced —
   scope as its own task, re-implemented against this fork's current
   `offseason_queries.py`/`offseason.html`, not a direct cherry-pick.
3. **Not an upstream item, but surfaced by this audit:** the per-league
   peak-age recalibration mentioned in project memory as done this session
   does not appear wired into `evaluation/constants.py` or
   `evaluation_engine.py` yet (both still hardcode global 28/27). Worth
   raising with the user directly — orthogonal to this upstream-porting
   task, but adjacent enough to flag here rather than let it sit
   undiscovered.
4. **Housekeeping:** the project memory entry
   `project_pending_upstream_stale_pool_port.md` (flagging `59c5c5c` as
   pending) is stale — that commit is already ported. Update or remove it.
