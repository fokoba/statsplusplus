# StatsPlus API — Wiki Analysis & Action Items

Analysis of the updated StatsPlus API wiki (saved at
`docs/StatsPlus APIs _ StatsPlus Wiki.html`). Captures the authentication,
error-handling, rate-limit, and caching behavior the wiki documents, and how
Stats++ should adapt. Cross-reference for PR #11 (token auth) and future work.

Source: https://wiki.statsplus.net/web-tools/statsplus-api

---

## 1. Authentication

Two interchangeable ways to identify as a team:

- **Browser session** — logged in at `statsplus.net/LGURL` and linked to a team,
  visiting the API URL in the same browser. (This is what our current
  cookie-scraping approach mimics.)
- **API token** — `?token=XXXX` query param. **"This is the method to use from a
  script or tool."** Per-team, per-league, found on the league Preferences page.

Stats++ is a script/tool, so the **token is the sanctioned path**; the cookie is
the browser-session emulation. This confirms the premise of PR #11.

### Which endpoints require auth (everything else is open)

| Endpoint | Auth required? |
|---|---|
| `/ratings`, `/mycsv` | Yes — except `?osa=1` (open to anyone) |
| `/gamehistory` | Yes |
| `/tradeblock` | Yes |
| `/draftpool` | Only to include the demand column |
| everything else | No |

So auth failures can only surface on these endpoints. `/ratings` is the one that
matters most for us (refresh depends on it).

### Token lifecycle — the critical gap

- **Tokens expire after 90 days.** On expiry the API returns the plain-text
  `API token has expired...` **with HTTP 200** (not an error status).
- An unknown/invalid token returns `Invalid or unknown API token`, also **HTTP 200**.
- The wiki explicitly warns: *"A tool that stores a token should expect to be
  told to refresh it, and should show that message to the user rather than
  treating it as a network failure."*
- Refresh path: the user logs in on the website to get a fresh token.

### `/tokencheck` — the validation endpoint

`https://statsplus.net/LGURL/api/tokencheck/?token=TOKEN`

- Success → returns the **team ID** as plain text (HTTP 200).
- Unknown token / not 36 chars → **HTTP 400** `Invalid Token`.
- Older than 90 days → **HTTP 400** `Token expired`.

Note `/tokencheck` uses proper HTTP 400 error statuses (unlike the data
endpoints, which bury errors in HTTP-200 text). This makes it the clean,
documented way for a tool to validate its token and discover which team it maps
to. Ideal backing for a Settings "Test Connection".

---

## 2. Error handling — HTTP-200-with-plaintext trap (affects cookie AND token)

**This is the highest-value finding, and it affects our current code today, not
just the token PR.**

> "Some errors are returned as HTTP 200 with a plain text body. A tool that only
> checks the status code will save an error message into the file where it
> expected a CSV. Before parsing a response, check that the **Content-Type** is
> what you expected — `text/csv` or `application/json` for real data. Anything
> answered as `text/plain` when you asked for data is a message for a human."

HTTP-200 plain-text "human messages" include:

- `Request too soon, wait N seconds before requesting again` (rate limit)
- `This API requires user to be logged in...` (**expired/invalid cookie**)
- `Invalid or unknown API token`
- `API token has expired...`
- `Ratings are not published for this league`
- `The ratings are being updated, please try again in a few minutes`
- `Request ID ... still in progress, check back soon`

**Robust defense:** a single content-type / body check in the client's `_fetch`.
If we requested data but got `text/plain` (a human message), do not parse it as
data — surface it (retry for the rate-limit/in-progress cases; raise a clear,
user-facing "refresh your token/cookie" error for the auth cases). One guard
covers token expiry, cookie logout, ratings-not-published, and rate limits
uniformly. This is cleaner than string-matching each message.

**Current state:** our `_fetch` already string-matches two of these messages:
the `wait (\d+) seconds` rate-limit message (retries) and
`"requires user to be logged in"` (raises `CookieExpiredError`). So the cookie
path and throttling are partly covered *by specific string matches*. It does
**not** handle the token messages (`API token has expired`,
`Invalid or unknown API token`), nor `Ratings are not published`,
`The ratings are being updated`, or `Request ID ... still in progress`.

Two problems with the current string-match approach:
- It is brittle — each new human-message needs a new hardcoded substring, and
  the wiki lists several we don't cover.
- Under PR #11, an expired **token** would slip through entirely (no matching
  string), writing the error text into the DB pipeline.

The wiki's **content-type guard** is the robust generalization: treat any
`text/plain` response to a data request as a human message, then branch on its
content (retry vs. raise). This subsumes all current and future messages with
one check. Recommended over adding more string matches.

### Status codes

| Code | Meaning |
|---|---|
| 200 | Success — **but check Content-Type** (see trap above) |
| 204 | No data for this request (e.g. a stats year with no rows) |
| 400 | Too many repeated `year`/`lid` (max 25), or bad token on `/tokencheck` |
| 401 | Invalid parameter value (`year`/`lid`/`pid`/`split`/`retired`) — a bad request, NOT auth. Treat 400/401 the same: request is wrong, retrying won't help |
| 404 | No such endpoint |
| 405 | Wrong HTTP method, or endpoint not enabled for this league (`/exports`) |
| 429 | Team-stats render rate limit (see Rate limits) |

---

## 3. Rate limits

| Endpoint | Limit | Refusal |
|---|---|---|
| `/ratings` (session or token) | 1 request / 5 min / team | HTTP **200** plaintext `Request too soon, wait N seconds...` |
| `/ratings?osa=1` (anonymous) | 1 / 15 min / IP | same |
| `/gamehistory` | 1 / 5 min / team | same |
| `/teambatstats`, `/teampitchstats` | 1 render/min/caller + 30s per-year lock while rendering | HTTP **429** |

- Collecting an already-rendered copy costs nothing and never counts — only the
  render counts.
- The wait time is included in the message, so a tool can sleep exactly that long.
- **Note the inconsistency:** `/ratings` refusals are HTTP-200 plaintext; team
  stats refusals are HTTP-429. Our `_fetch` handles both today (200-body regex +
  429 retry), but this should be validated against the content-type guard above.

---

## 4. Best practices — `/date`-driven caching (optimization opportunity)

- **Poll `/date`** (returns just the game date as plain text) to decide whether
  anything else is worth re-fetching. These endpoints only change when the game
  date changes: `teams, players, date, contract, gamehistory, teamstats,
  playerstats, ballparks, lgdata, ratings`.
- `draft`, `exports`, `tradeblock` may change at any time.
- **No conditional GET** — no ETag/Last-Modified, so a re-request always
  transfers the whole body. `/date` is the cheap poll that gates the expensive
  fetches.
- Leagues sim a few times a week, rarely more than once a day — no point polling
  more than hourly.
- Send a **real User-Agent** identifying the tool (site has bot filtering; we
  already do this).

**Current state:** refresh is idempotent on the same game date, but still does
full fetches. Opportunity: short-circuit a refresh (or skip specific endpoint
pulls) when `/date` hasn't advanced since the last successful refresh —
meaningful because there's no conditional GET.

---

## 5. General API contract notes

- Endpoint pattern: `https://statsplus.net/LGURL/api/APINAME`.
- **Use column headers, never column count/order** — the wiki explicitly warns
  against positional parsing. (Relevant: our ratings-CSV handling registers
  formats by header, which is aligned; keep it that way.)
- Backwards compatibility is attempted but not guaranteed.
- `year`/`lid` repeated params capped at **25** (HTTP 400 if exceeded).

---

## 6. Action items

Ordered by priority. Ties into PR #11 (token auth).

### High — makes token auth (and cookie auth) safe
1. **Content-type / human-message guard in `_fetch`** (both `statsplus/client.py`
   and `src/statsplusplus/client/statsplus.py`). Generalize the existing
   string-match handling (rate-limit `wait N`, cookie `requires user to be
   logged in`) into one content-type check: if a data request returns
   `text/plain` (a human message), do not parse as data:
   - Rate-limit / in-progress / updating → retry with the stated wait (extend
     existing logic).
   - Auth messages (`token has expired`, `Invalid or unknown API token`,
     `requires user to be logged in`) → raise a clear, user-facing error telling
     the user to refresh their token/cookie. This is the wiki's explicit ask.
     (`CookieExpiredError` already exists; add the token analog.)
   - `Ratings are not published for this league` → clear message, non-fatal.
2. **Token-expiry surfacing** — when the auth message indicates an expired token,
   the web UI (and CLI) should show "your StatsPlus token expired, log in on the
   site to refresh it," not a generic failure.

### Medium — quality of the token integration
3. **Back the token "Test Connection" with `/tokencheck`** rather than a generic
   fetch — it validates the token and returns the team ID with proper HTTP 400s.
4. **Onboarding token option** — PR #11 leaves onboarding cookie-only; offer the
   token in the wizard so new installs start on the sanctioned path.

### Lower — optimization / roadmap
5. **`/date`-gated refresh** — skip or trim a refresh when the game date hasn't
   advanced (no conditional GET, so this is the intended pattern).
6. **`osa=1` anonymous ratings** — ties into API Roadmap Phase 5a (OSA ratings);
   note the 15-min/IP anonymous limit and that it needs no auth.

---

## 7. Endpoint coverage & untapped-data opportunities (Session 83 first pass)

Comparison of the wiki's documented endpoints against what the client actually
consumes (`statsplus/client.py`). Deeper field-level audit is a tracked recurring
task (`docs/task_list.md` — "StatsPlus API doc-diff review").

### Endpoints we already consume
`/teams`, `/players`, `/date`, `/contract`, `/contractextension`, `/exports`,
`/gamehistory`, `/teambatstats`, `/teampitchstats`, `/playerbatstatsv2`,
`/playerpitchstatsv2`, `/playerfieldstatsv2`, `/draftv2`, `/tradeblock`,
`/ballparks`, `/lgdata`, plus `/ratings` and (new) `/tokencheck`.

### Documented but NOT consumed — opportunities
- **`/ballparks` park factors (fetched but unused).** We call `/ballparks` but
  don't use the payload's park factors: `avg_r`, `avg_l`, `avg`, `d` (doubles),
  `t` (triples), `hr_r`, `hr_l`, `hr`, plus `capacity`, `stadium_type`,
  `surface`. **Opportunity:** park-adjusted offensive stats and HR normalization
  (a big HR park inflates raw power output); handedness-split HR factors
  (`hr_r`/`hr_l`) could refine platoon/park value. Accepts a `lid` for minor
  leagues too. *(Tracked: API Roadmap Phase 3b — park factors.)*
- **`/draftpool` demand column.** Adding `?token=` to `/draftpool` returns each
  prospect's **bonus demand** (as loaded when the draft started). **Opportunity:**
  factor demand into draft value (over-slot demand reduces attractiveness;
  under-slot creates value) and auto-draft list generation. *(Tracked in the
  Draft Tab backlog: "Bonus demand display + draft value integration.")*
- **`/ratings?osa=1`** — anonymous OSA (scout-independent) ratings; enables
  scouted-vs-OSA divergence analysis. *(Tracked as its own task.)*

### Field-level audit (TODO)
The wiki has per-endpoint field tables for the big endpoints (`/players` is the
richest). A systematic pass should diff each endpoint's documented columns
against what `refresh.py`'s upsert functions store, to surface fields we ignore.
Phase 1 (Session 69) already pulled 45 new `/players` fields; re-check for any
added since. Highest-value candidates to re-audit: `/players`, the `*statsv2`
stat endpoints (advanced stat columns), and `/contract` (option/incentive
fields, mostly captured in Phase 4).
