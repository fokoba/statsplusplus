# Testing & Release Pipeline Design

Status: **Draft for discussion** (Session 81). Not yet implemented.

## Context: what Stats++ is (and isn't)

A conventional alpha/beta/gamma/prod pipeline assumes a *deployed service* you
push to and promote between environments. Stats++ doesn't fit that shape:

- It's a **locally-installed Flask app** (`localhost:5001`), distributed as a
  **release zip** users extract and run via `start.sh` / `start.bat`.
- There is **no prod environment we control**. "Prod" is the user's machine —
  their OS, their Python version, their StatsPlus cookie, their league's data
  quirks (OVR/POT-less leagues like PPL, different rating scales, sparse data).
- The **StatsPlus API is a third-party dependency** we don't control and can't
  run repeatable tests against (rate limits, live data, auth).

So the useful translation of the pipeline model is:

> **The artifact is the release zip. The "environments" are the dimensions
> along which it can break** — Python version, OS, league data shape, and the
> live API. Pipeline stages are gates the artifact passes before a human tags a
> release.

## The core problem (from the Session 80 audit)

The test suite validates **the source tree with mocks**, but users run **the zip
artifact against real data and the real API**. Nothing tested the thing users
actually get. Every bug that session (broken entry points, PPL crashes, ratings
CSV format change, stale `start.sh` behavior) was invisible to a green suite.

## Proposed stages

### Stage 1 — "Alpha": commit/PR CI (fast, every push, hermetic)
GitHub Actions, no network.
- Unit + integration tests (existing ~720) — logic correctness.
- Smoke tests (entry points, CLI tools, web routes vs fixture leagues).
- `mypy` on the typed packages (configured today, not enforced in CI).
- **Matrix: Python 3.10 / 3.11 / 3.12 / 3.13** — we advertise "3.10+" but CI
  tests none. The original bug report was on 3.14.
- Gate: green → mergeable.

### Stage 2 — "Beta": artifact validation (on tag / pre-release)
Build the release zip exactly as `release.yml` does, then in a clean container:
- Run the real installer path from `start.sh` and assert the app imports/boots.
- Run smoke tests against the extracted artifact. (Today the zip excludes
  `tests/` — needs a shippable `tests/smoke/` or a checkout-based run.)
- Gate: the thing users download actually starts.

### Stage 3 — "Gamma": frontend / rendering (pre-release, slower)
Playwright (already used interactively — see `.playwright-mcp/`) promoted to a
headless check: boot against a fixture league, drive key pages, assert no
console errors, no 500s, and key elements render (standings rows, player rating
bars, draft board). Catches "renders but broken JS / empty table" that HTTP-200
smoke tests miss.

### Stage 4 — "Prod": live-integration canary (manual / scheduled, non-blocking)
Validates the StatsPlus API **contract** — the thing that broke this week.
Can't run in public CI (cookie, live third party, rate limits). A scheduled or
`make canary` job using a real cookie from a secret, hitting the live API
read-only, asserting: ratings CSV column count is a known format, `/date`
responds, endpoints return expected shapes. Alerts when the external contract
drifts *before* a user's refresh silently corrupts data.

## Priority order

1. **CI workflow** (pytest + mypy on the Python matrix, every push/PR) — highest
   value; nothing runs the suite automatically today.
2. **Fix release bugs** in `start.sh`/`start.bat` (install command + shim
   handling — see separate discussion).
3. **Artifact boot test** in the release workflow.
4. **Playwright rendering check.**
5. **API contract canary.**

## Non-goals
- Chasing code-coverage percentages.
- Mocking the StatsPlus API for "integration" tests — a mock of a contract we
  don't control gives false confidence (a mock would have happily passed the old
  126-col format forever). The value is a canary against the *real* API.
- A heavyweight multi-environment promotion system — there's no environment to
  promote to.

## Recommendation

Do Stages 1–2 first: ~80% of the risk reduction for ~30% of the effort, directly
addressing this session's failures. Stages 3–4 are worthwhile follow-ups, not
blockers.
