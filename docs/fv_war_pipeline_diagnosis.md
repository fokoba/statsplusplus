# FV / peak_war Pipeline Diagnosis (recovered research)

Investigation into: (a) too many players in the 45/50/55 FV tier, and (b) FV-50
players outputting less WAR than the FanGraphs standard (50 FV ≈ 2.0 WAR regular).

League baselined: **emlb**, eval_date 2034-05-08, 9,127 evaluated players.

## FanGraphs reference (what FV *should* mean)

The canonical FanGraphs FV→WAR shape lives in
`evaluation/constants.py :: FV_TO_PEAK_WAR_DEFAULT`:

```
FV 50 → 2.0 WAR   FV 55 → 2.9   FV 45 → 1.2   FV 60 → 4.2
```

The **calibrated** per-position table (`model_weights.json :: FV_TO_PEAK_WAR_BY_POS`)
is consistent with this — FV 50 maps to 2.2 (C), 2.5 (2B), 3.1 (SS), 3.0 (CF),
2.6 (COF), 2.4 (1B). **The FV→WAR table is NOT the bug.**

## Observed symptom

Stored `peak_war` for the FV-50 cohort has a **median of 1.6** (69% below 2.0),
~0.7–1.3 WAR *below* what `peak_war_from_fv()` returns for the same player.

| bucket | n | mean fv_continuous | table@fv_continuous | stored peak_war |
|---|---|---|---|---|
| C | 311 | 49.4 | 2.1 | **1.3** |
| 2B | 234 | 49.7 | 2.5 | **1.4** |
| SS | 170 | 49.6 | 3.0 | **1.8** |
| CF | 160 | 49.7 | 2.9 | **1.7** |
| COF | 380 | 49.4 | 2.5 | **1.8** |

`fv_continuous` (49.4) is ~= the rounded grade, so rounding is NOT the cause.
The deflation happens AFTER the FV→WAR lookup.

## Root cause (two compounding bugs in `evaluation/player_value.py`)

`compute_player_value()` does NOT trust the FV→WAR table. It computes:

```
fv_war       = peak_war_from_fv(fv_continuous, bucket)      # ~2.5  (correct)
composite_war = peak_war_from_score(composite, bucket)       # ~0.0  (BROKEN)
tool_war     = lerp(fv_war, composite_war, stat_confidence)  # sc≈0 → should = fv_war
# then the NEAR-MAXED BLEND overrides it:
if realization > 0.7 and composite_war < fv_war:
    blend_w = ((realization-0.7)/0.3)**2
    tool_war = tool_war*(1-blend_w) + composite_war*blend_w   # drags toward ~0
peak_war = tool_war            # stat_war is None, sc≈0
```

### Bug 1 — `COMPOSITE_TO_WAR` reads average players as 0 WAR

`model_weights.json :: COMPOSITE_TO_WAR` has its zero-crossing at composite ~50:

```
C:  {50: 0.0, 55: 1.89, 60: 4.02, ...}
2B: {50: 0.0, 55: 1.64, ...}
SS: {50: 0.31, 55: 2.04, ...}
```

But the composite scale is **population-centered at ~55** (Session 93 fix: eMLB
comp_mean 54.8). So composite 50 is a below-average-but-playable regular, NOT
replacement level. `peak_war_from_score(49, '2B') = 0.0` — clearly wrong; that
player is ~a 2 WAR talent. The calibration regression put the intercept at 50,
so everything at/below the population center reads as 0 WAR.

### Bug 2 — the near-maxed blend fires on nearly the whole population

The blend was intended as an EDGE-CASE correction for near-maxed *average*
players who shouldn't project elite upside. With threshold 0.7, it triggers on
essentially every prospect (a 49/56 player is 88% realized). It has become the
DOMINANT term, pulling the correct FV-based WAR toward the broken composite_war≈0
anchor. Result: FV-50 regulars stored as 1.3–1.6 WAR bench pieces.

## Why BOTH symptoms share one origin

The composite scale is centered ~55, and two parts of the pipeline treat that
center inconsistently:
- **FV grade** anchors to the population-centered composite (avg player ≈ comp 55
  → FV 55) with NO risk discount to the grade → too many 45/50/55 players.
- **COMPOSITE_TO_WAR** treats composite 50 as 0 WAR → those same players output
  too little WAR.

## Fix directions (not yet implemented — for discussion)

1. **Stop overriding the FV table.** The near-maxed blend should be a true edge
   case (raise threshold / gate on absolute realization + low ceiling), or be
   removed for low-stat-confidence prospects where FV is the intended estimator.
2. **Recalibrate `COMPOSITE_TO_WAR` so the center maps to league-average WAR**,
   not 0. The intercept at the population center (~55) should be ~2 WAR, not the
   current ~50→0. Mirror of the Session 93 composite population-centering, applied
   to the WAR regression.
3. **Reconsider what FV represents + make risk discount the grade** (the original
   research framing): re-anchor FV to an absolute / positional-WAR standard
   (restore the median-gate the valuation_model.md doc still describes but the
   code dropped), and have risk actively lower the FV output instead of sitting
   beside it as a cosmetic label.

Cross-check vmlb/ppl before any model change to confirm the inflation + WAR
deflation are systemic and not an emlb calibration quirk.

---

# Cross-League Impact (full picture)

Investigated all three leagues on current eval dates. Findings refine the
single-league diagnosis above into THREE distinct problems with different blast
radii.

## Correction to earlier claims
- **MLB vets are NOT broken.** ~300–711 full-stat-confidence (sc≥0.9) vets per
  league have `peak_war` EXACTLY equal to `stat_war` (median |diff| 0.00). The
  stat path works; the bug is confined to the prospect/tool path (sc<0.5).
- The "zero players on stat path" claim was a stale-eval-date artifact. ~700–900
  players/league carry a stat signal (sc>0.05); the ramp works.

## Problem A — near-maxed blend over-fires (SYSTEMIC, all leagues)
The near-maxed blend (threshold 0.7) fires on hitting prospects at:
- **eMLB 92%**, **vMLB 79%**, **PPL 46%** of the sc<0.5 hitter population.
- Median WAR lost: eMLB 0.63, vMLB 0.63, PPL 0.42; max up to 2.3 WAR.
It is the dominant term, not an edge case. It drags the correct FV-based WAR
toward `composite_war`.

## Problem B — COMPOSITE_TO_WAR center is miscalibrated IN eMLB ONLY
WAR at the MLB-median composite (should be ~2.0 for an average regular):
- **eMLB: 0.95** at median composite 52  ← BROKEN (center reads as <1 WAR)
- vMLB: 2.12 at 51  ✓
- PPL:  2.07 at 51  ✓
So the composite→WAR curve is correctly centered in vMLB/PPL but NOT eMLB. This
is why Problem A does real damage in eMLB (blend pulls toward a 0.95 anchor) but
far less in vMLB/PPL (pulls toward ~2.0). eMLB's curve needs recentering (zero-
crossing moved down ~5 composite points), mirroring the Session 93 composite
population-centering but applied to the WAR regression.

Consequence — FV tier inflation is largely an eMLB artifact:
| league | 45/50/55 band | FV55 | FV50 | FV45 |
|---|---|---|---|---|
| eMLB | **42%** | 306 | 1552 | 1934 |
| vMLB | 25% | 95 | 804 | 2055 |
| PPL  | 21% | 109 | 426 | 1297 |

## Problem C — PPL $/WAR is broken (PPL-specific, makes surplus meaningless)
`dollars_per_war` resolves to **$0.02M ($20K/WAR)** on PPL vs $7.3M (eMLB) /
$9.6M (vMLB). PPL is a 1955 retro league (min salary $7K) so a LOW $/WAR is
era-appropriate, but $20K/WAR is implausibly low. Result: a healthy 2.2-WAR
FV-50 PPL prospect gets market value 2.2×$20K=$44K → **$0.2M total surplus**.
All PPL surplus/trade dollar values are currently meaningless. Independent of the
FV/WAR issue; a PPL calibration failure (likely the $/WAR regression on sparse /
compressed 1955 contract data).

FV-50 prospect surplus by league (same nominal grade, wildly different output):
- eMLB: peak_war 1.64 → $13.7M   (deflated WAR, ok $/WAR)
- vMLB: peak_war 2.35 → $41.8M   (healthy WAR + $/WAR)
- PPL:  peak_war 1.96 → $0.1M    (healthy WAR, broken $/WAR)

## Net
Three independent faults stack:
1. **Near-maxed blend** over-fires everywhere → deflates prospect peak_war.
2. **eMLB COMPOSITE_TO_WAR** center at ~0.95 WAR → amplifies #1 in eMLB and
   inflates the eMLB FV 45/50/55 tiers.
3. **PPL $/WAR ≈ $20K** → zeroes all PPL surplus regardless of WAR.

Fix order: recenter eMLB COMPOSITE_TO_WAR (B) and gate/soften the near-maxed
blend (A) together — they interact. Fix PPL $/WAR (C) separately. Re-run
fv_calc + spot-check tier counts and FV-50→~2.0 WAR after each.

---

# PROTOTYPE RESULTS (A + B) — revised conclusion

Prototyped both fixes standalone (`scripts/proto_fv_fix.py`, no pipeline/DB
changes). Result overturns the A/B framing: **A is the fix; B is a red herring
and net-harmful.**

Fix A tested: near-maxed blend fires only when genuinely maxed —
threshold 0.85 (was 0.7), denom 0.15, AND remaining ceiling gap ≤ 4
(don't blend a young player who is merely near a HIGH ceiling).

Prospect-hitter median peak_war by FV tier (anchor: FV55~2.9 FV50~2.0 FV45~1.2):

| scenario | eMLB FV50 | vMLB FV50 | PPL FV50 |
|---|---|---|---|
| CURRENT  | **1.62** | 2.42 | 2.11 |
| fix A     | **2.07** ✓ | 2.63 | 2.16 |
| fix B     | 1.65 | 2.42 | 1.84 ✗ |
| fix A+B   | 2.05 | 2.62 | 2.13 |

Findings:
1. **Fix A alone aligns every league to the FanGraphs anchor.** eMLB FV50
   1.62→2.07, FV45 0.86→1.21, FV55 2.83→3.34. Near-maxed firing drops
   92%→42% (eMLB), 79%→40% (vMLB), 46%→25% (PPL).
2. **eMLB was the outlier; vMLB/PPL were already ~on-anchor** (if anything
   slightly high). Fix A nudges them up modestly without breaking them.
3. **Fix B (recenter COMPOSITE_TO_WAR via lower AB gate) is WRONG.** Lowering
   the AB-qualification gate pulls in weak part-timers and drags the regression
   DOWN (WAR@52: gate300=1.46 → gate100=1.17), making the center worse. B barely
   helps eMLB (1.62→1.65) and HURTS vMLB/PPL (PPL FV50 2.11→1.84). The ≥300 AB
   gate was not the bug.
4. **Why the composite_war≈1 curve was a red herring:** it only hurt *because*
   the near-maxed blend over-weighted it. Fix the blend and the FanGraphs-aligned
   FV table shines through correctly — composite_war's center no longer matters
   for the prospect projection.

Revised plan:
- **Ship Fix A** (gate the near-maxed blend: higher threshold + low-remaining-gap
  requirement). This is the single change that corrects the FV→WAR output across
  all leagues. Make the params ModelWeights-overridable.
- **Drop Fix B** (do not recenter COMPOSITE_TO_WAR / do not lower the AB gate).
- Separately: the FV TIER COUNTS (too many 45/50/55 in eMLB, 42%) are a distinct
  question from peak_war. Fix A corrects the WAR each tier OUTPUTS; it does not
  change how many players LAND in each tier. The tier-population question
  (re-anchor FV to positional-WAR standard + make risk discount the grade) is
  still open and separate.
- C (PPL $/WAR) still separate, ignored for now per user.

---

# PROTOTYPE 2 — WAR-anchored FV grade (hitters) + realization-discount sweep

User clarified the real complaint: NOT that raw high-upside players grade high
(that's fine/wanted), but that FINISHED LOW-CEILING ROLE PLAYERS grade average+.
Canonical cases (eMLB): Mike French (SS, 52 bat / 65 glove, FV55) and Luis
Carrasco (SS, **25 bat** / 63 glove, FV55) — glove-only utility guys graded as
above-average regulars because composite averages the elite glove up and the FV
reads off composite.

Design prototyped (`scripts/proto_fv_waranchor.py`, hitters, no pipeline change):
```
current_WAR  = peak_war_from_score(composite, bucket)   # stand-in for facet-run WAR
ceiling_WAR  = peak_war_from_score(ceiling,   bucket)
p(develops)  = closure_rate(age) * DISCOUNT_STRENGTH   (near-maxed gap<=2 => p=1)
expected_WAR = p*ceiling_WAR + (1-p)*current_WAR
FV grade     = invert(FV_TO_PEAK_WAR_BY_POS)[expected_WAR]   # WAR -> grade
```
This REPLACES the near-maxed blend entirely (realization credit subsumes Fix A).

## Result: solves the stated problem
- **Mike French**: near-maxed (p=1.0) → exp_war = current 2.73 → **FV 50** (was 55).
  Correctly a solid-average regular, not above-average.
- **Luis Carrasco**: 25 bat → current_WAR **0.06** (run-space exposes that a bad
  bat can't be rescued by glove). Even full ceiling credit → exp_war 0.8–1.5 →
  **FV 40–45** (was 55). The glove-only utility guy finally grades like one.
- **Key insight**: in run-space a below-replacement bat produces negative/zero
  run value defense can't offset. Composite-averaging hid this; the WAR anchor
  exposes it. THIS is the mechanism that fixes the role-player inflation.

## Bucket distribution — fixing WAR-per-tier DOES thin the tiers (user's intuition confirmed)
eMLB 45/50/55 band: CURRENT **3,475** → str0.8 1,014 → str1.0 1,182. 55+ count:
316 → 46 → 95. The inflated middle collapses; overflow lands at FV40.
Same direction in vMLB (band 2,530→1,023 @1.0) and PPL (1,367→1,444 @1.0; PPL was
already less inflated).

## Discount-strength lever (DISCOUNT_STRENGTH) behaves monotonically
- 0.6 conservative (hug current): eMLB 16 players reach 55+.
- 1.0 raw closure rate: 95 at 55+.
- 1.2 upside-friendly: 163 at 55+, FV70s appear; raw high-ceiling youth credited.
Carrasco stays ≤45 at ALL strengths (full ceiling credit on a 25 bat is still
modest) — the WAR anchor caps no-bat players regardless of strength. Given the
user's preference (upside OK, role-player inflation is the problem), **strength
~1.0–1.2** is the target: preserves prospect upside, ruthlessly caps no-bat guys.

## Two refinements needed for a production version
1. **Extend the FV→WAR ladder below 40** (down to ~20 with sub-replacement WAR).
   The prototype's ladder bottoms at 40, so `war_to_fv` clamps ~4,000–5,600
   marginal players at FV40 and empties the 35/30/25 tiers (which currently hold
   a correct gradient, e.g. PPL 1,148@35 / 1,165@30). Marginal/org-filler must
   spread down, not pile at 40.
2. **Use real facet-run WAR, not peak_war_from_score(composite)**. The prototype
   approximates with the composite→WAR curve; production should go
   bat+baserunning+fielding+positional runs → WAR → FV directly (that's what made
   Carrasco's 25 bat read as 0.06 WAR). More faithful, and removes dependence on
   the composite curve entirely.

## Risk design (resolved)
Risk factors into the grade AS the realization credit `p(develops)` (weighting
ceiling-WAR vs current-WAR) AND reports separately as a variance label. Same
inputs (age, gap, closure, character, MiLB stat signal), two outputs. MiLB stats
enter as a Bayesian update: adjust ceiling_WAR (perf-adjusted ceiling) and nudge
p(develops) — never replace the run projection. Confirmed by prototype that this
is the mechanism thinning the tiers.

---

# NEGATIVE WAR + sub-40 FV — grounded in real data & industry docs

Question: should low-end players (even FV ~40) be able to project NEGATIVE WAR?
The model currently FLOORS WAR at 0 (replacement) in two places —
`calibrate._war_at` does `max(0.0,…)` and `surplus.market_value` returns min_sal
for WAR≤0 — so it cannot express below-replacement talent and piles marginal
players at FV 40.

## Industry grounding — the FV scale is ROLE-based and extends well below 40
Canonical 20-80 role scale (David Lee / Braves Prospects, standard FanGraphs-
style; corroborated by FanGraphs "50 FV ≈ 2.0 WAR"):

| FV | Hitter role |
|----|-------------|
| 55 | Above-average regular |
| 50 | Average regular (~2.0 WAR) |
| 45 | Utility player / platoon bat |
| 40 | **Bench player / depth** |
| 35 | **Up/down (AAAA) player** |
| 30 | **Organizational player** |
| 20 | Won't pass A-ball |

Takeaways:
- The scale is explicitly ROLE-based and the 35/30/20 tiers are REAL, used
  grades for bench/org players. Flooring everyone at 40 is wrong per the standard.
- FV 40 = "bench player", FV 45 = "utility/platoon bat" — exactly where the
  prototype put Mike French (solid regular → 50) and Luis Carrasco (glove-only
  utility SS → 40-45). The run-anchored prototype's output MATCHES the industry
  role definitions.
- Grades 30/20 ("org player", "won't pass A-ball") implicitly encode NEGATIVE
  MLB WAR — a 30 forced into an MLB role is a below-replacement talent.

## Data grounding — negative WAR is common, not an edge case
MLB hitter-seasons in our leagues (ab≥100, last 6 game-years):

| league | negative-WAR seasons | even among regulars (ab≥400) | worst |
|---|---|---|---|
| eMLB | **16%** (260/1666) | 6% (55/923) | -2.6 |
| vMLB | 12% (174/1407) | 5% (46/898) | -3.4 |
| PPL  | 16% (136/831) | 2% (8/356) | -2.0 |

12-16% of real MLB hitter-seasons post NEGATIVE WAR; 5-6% of everyday REGULARS
do. The 0-floor discards ~a sixth of the real outcome distribution. A -1 WAR
talent is a real, frequently-observed thing.

## Design conclusion (grounded)
Separate the two numbers:
- **Talent/WAR projection → FV grade: ALLOW NEGATIVE.** Extend the FV→WAR ladder
  below 40 (FV40≈0.5, FV35≈0.0, FV30≈-0.5, FV25≈-1.0, FV20≈-1.5) so marginal /
  bench / org players spread into 35/30/25 per the industry role scale instead of
  piling at 40. Don't clamp bat_runs/fielding_runs positives-only — let a bad bat
  or miscast glove carry its real negative runs through to the grade.
- **Surplus / dollar value: KEEP the ~0 floor.** You never pay negative dollars;
  trade value bottoms at "freely available replacement." A -1 WAR talent and a
  0.0 WAR talent are both ~$0 in a trade. The 0-floor is correct HERE, wrong for
  grading.

Current conflation: WAR is floored at 0 BEFORE it reaches FV. Fix = let the WAR
projection go negative for grading, map to sub-40 FV, keep the surplus floor.
Check whether `fielding_runs` clamp (clamp_lo) is truncating legit negative
defenders — a concrete place negative value is currently suppressed.

---

# NEGATIVE-WAR PROTOTYPE RESULTS + surfaced calibration issues

## Fielding clamp — NOT the suppression point (checked)
`def_curve.clamp_lo` already permits deep negatives (eMLB SS -18.2, CF -15.1,
COF -13.9; similar vMLB/PPL). `bat_runs` (wRAA) is unbounded-negative. The run
COMPONENTS flow negative correctly. The 0-floor is ONLY at the final conversion
(`runs_to_war` + `market_value`/`peak_war_from_score`).

## Realistic negative floor (data-grounded)
Worst full-season regulars (ab≥400): -2.6 (eMLB), -3.4 (vMLB), -1.1 (PPL).
Fringe part-timers (100-250 AB) WAR/600 p5 ≈ -2.6. So the realistic rostered
floor is ~ -2 to -3 WAR; below that a player simply isn't rostered (never gets
the PA to post -5). The linear comp→runs→WAR extrapolation yields absurd -9 to
-14 for low composites because it assumes full-season PA. **Clamp projected WAR
at ~-3 before grading.**

Grounded sub-40 ladder: FV45=1.2 (utility), 40=0.5 (bench), 35=0.0 (AAAA),
30=-1.0 (org), 25=-2.0, 20=-3.0 (floor). WAR_FLOOR = -3.0.

## Prototype result — the gradient now exists and Carrasco is correct
Extended `scripts/proto_fv_waranchor.py` to use run-based WAR (negatives allowed)
+ the sub-40 ladder:
- **Luis Carrasco**: cur_war now **-2.28** (25 bat → genuinely negative, no
  longer floored). Grades **FV 30-35** — "organizational / up-down player," the
  correct industry role for a no-bat glove-only SS. (was FV 55.)
- **Mike French**: stable **FV 50** (near-maxed solid regular).
- The 35/30/25 tiers populate instead of piling at 40 — a real role gradient.

## Two calibration issues surfaced (mechanism right, calibration not yet clean)
1. **FV 20 overloaded** (eMLB 846-1821, PPL up to 3658). The WAR_FLOOR clamp is
   catching too-negative extrapolations: the comp→runs→WAR map (fit on MLB
   regulars) over-penalizes LOW composites, dragging A-ball teenagers to FV 20 on
   CURRENT ability. This is a category error — a 19-yo's current MLB-equivalent
   composite→WAR is meaningless.
2. **PPL top-heavy at high strength** (812 @55+ at str1.2) — PPL run-space
   calibration is noisy (known-broken $/WAR); its comp→WAR map is less trustworthy.

## KEY DESIGN INSIGHT (next step)
For prospects the grade must lean on **CEILING** (where the realization credit +
development curve live), treating current-WAR as a weak floor — NOT drag young/
low-level players to FV 20 on current ability. Either (a) weight ceiling much
more heavily in the realization blend for young/low-level players, or (b)
development-project current_war to a realistic debut age BEFORE blending (reuse
the per-facet DEV curves). The current prototype blends raw current-WAR, which is
why teenagers crater. This is the piece to fix before the sub-40 ladder is sound.

Target discount strength remains ~1.0-1.2 (upside-friendly) per user preference;
revisit after the current-WAR-for-prospects fix, which will re-shape the low tiers.

---

# FINAL PROTOTYPE — saturating run→WAR + ceiling-anchored FV (DESIGN COMPLETE, hitters)

Resolves the tail-extrapolation blowup. `scripts/proto_fv_saturate.py`.

## Saturating run→WAR (fixes both tails)
The linear comp→WAR map is only valid in the fitted middle (~45-65). A tanh
saturation keeps the center/slope intact but asymptotes to **data-grounded caps**
(top = p98, bottom = p02 of each league's real full-time hitter WAR):

| league | top cap | bot cap | median |
|---|---|---|---|
| eMLB | 9.9 | -0.9 | 2.8 |
| vMLB | 7.0 | -0.6 | 2.6 |
| PPL  | 8.1 | -0.3 | 3.2 |

ceiling-WAR at score 80: eMLB 13.1→**9.2**, vMLB 14.7→**6.9**, PPL 18.1→**8.1**.
Scores 55-60 unchanged (linear); compression only above ~65 where data runs out.

## Full FV formula (hitters), design complete
```
ceiling_WAR  = saturate( runs_to_war( runs_from(ceiling_score) ), top_cap, bot_cap )
p(develops)  = closure_rate(age) * strength        (near-maxed gap<=2 -> 1.0)
expected_WAR = p * ceiling_WAR + (1 - p) * FALLBACK_WAR   (fallback ~ -0.3, replacement bust)
FV grade     = invert( FV→WAR ladder + sub-40 role ladder )[ max(bot_cap, expected_WAR) ]
```
sub-40 ladder: 40=0.5(bench) 35=0.0(AAAA) 30=-0.6(org) 25=-1.2 20≈bot_cap.

## Results (strength 1.0) — all goals met
- **50/55 inflation FIXED**: eMLB FV55 285→51, FV50 1476→189; band 3,475→800.
- **FV 20/25 pile-up GONE** (empty all leagues); bottom settles at 35/30.
- **Distribution now a prospect PYRAMID**: narrow top, bulk in the 35-45 fringe/
  org band — matches the industry role scale.
- **Carrasco → FV 40** (ceiling-WAR 2.26, capped by his weak 41 POTENTIAL bat —
  low for the RIGHT reason: low ceiling, not current ability).
- **Mike French → FV 50** (near-maxed, stable).
- **Raw teens graded by RISK on good ceilings**: eMLB median FV 50, Griffin/
  Anthony FV 60 — real prospects, not FV 20, not absurd FV 70s.

## Remaining concern (upstream, not FV logic)
PPL runs HOT — raw-teen median FV 60, 342 at 60+ (vs eMLB 31). The WAR curve is
NOT the cause (saturation capped ceiling-WAR at 8.1). PPL assigns very high
CEILING SCORES (teens at ceiling 70-72) far more liberally than eMLB — an
upstream composite/ceiling calibration difference (compressed ratings + generous
potentials; same league with the broken $/WAR). Address PPL ceiling-score
calibration separately; the FV design itself is sound.

## Productionization sketch (when ready)
1. Add the saturating anchor (top/bot caps from real-WAR p98/p02) to
   `calibrate._calibrate_run_space` → `run_space` block; apply in
   `facet_runs.runs_to_war` (or a new `runs_to_war_saturated`).
2. Rewrite `calc_fv` (hitters) to the ceiling-anchored formula above; keep risk
   as BOTH the p(develops) grade discount AND the separate variance label.
3. Extend the FV→WAR ladder below 40 (role ladder) for grading; keep the surplus
   $0 floor unchanged (talent can be negative; trade value cannot).
4. Pitchers remain on the existing path (out of scope — run spine is hitters-only).
5. Re-run fv_calc; validate tier pyramid + named cases per league.

---

# PRODUCTION IMPLEMENTATION — SHIPPED (hitters)

Implemented the ceiling-anchored FV in the real pipeline:
- `constants.py`: `FV_SUB40_WAR_LADDER` {35:0, 30:-0.6, 25:-1.2, 20:-2.0}.
- `facet_runs.py`: `saturate_war(war, anchor)` (tanh toward sat_top/sat_bot,
  center=sat_mid, no-caps passthrough) + `runs_to_war_saturated`.
- `surplus.py`: `fv_from_peak_war(peak_war, bucket, weights, war_floor)` inverts
  the per-position FV→WAR ladder + sub-40 role ladder.
- `calibrate.py` `_calibrate_run_space`: computes `anchor.sat_top`=p98,
  `sat_bot`=p02, `sat_mid`=median from the real qualified-hitter WAR sample.
- `fv.py`: `FV_CEILING_STRENGTH=1.1`; `calc_fv` gains `run_anchor`,
  `comp_mapping`, `weights`, `fv_strength`. Ceiling-anchored branch for hitters:
  `ceiling_war = saturate_war(runs_to_war(score_to_runs(pot), anchor), anchor)`;
  `p_dev = 1.0 if gap<=2 else closure(age)*strength`;
  `expected = p*ceiling_war + (1-p)*(-0.3)`;
  `fv = fv_from_peak_war(expected, bucket, weights, war_floor=sat_bot)`.
  Legacy composite path retained for pitchers + no-run-space fallback;
  composite-scale caps guarded to legacy only. Risk label UNCHANGED (separate).
- `fv_calc.py`: passes `scale` + `league_dir` to `calc_fv` (bug found+fixed —
  calls were `calc_fv(p)` with no league_dir, so the branch never fired until
  wired through).
- Surplus/`player_value` UNTOUCHED → $0 trade-value floor preserved.

## Validation (all 3 leagues recalibrated + fv_calc re-run)
Hitter FV distribution — now a clean prospect PYRAMID:

| league | 45/50/55 band | 60+ | ≤35 (depth/org) |
|---|---|---|---|
| eMLB | **20%** (was 42% broken) | 1% | 63% |
| vMLB | 11% | 2% | 71% |
| PPL  | 11% | 4% | 73% |

Named cases (eMLB production): Mike French comp57/ceil57 → **FV 50 Low** (was 55);
Luis Carrasco comp52/ceil56 → **FV 45 Low** (was 55, down via low ceiling);
Roman Anthony age18 comp47/ceil64 → **FV 60 Medium** (raw upside preserved).
Pitchers unchanged (legacy path, full 20-70 spread intact).

PPL still slightly top-rich (4% at 60+ vs eMLB 1%) — the known ceiling-score
calibration difference (low scouting accuracy + young-prospect fall-off),
backlogged separately; vastly improved from the 70%-band blowout.

## Caveats / follow-ups
- `calc_fv_from_dict` now loads `tool_weights.json` + `load_model_weights` per
  call (9k+ players). Works (~10s run) but wasteful — hoist the per-league load
  out of the per-player path when productionizing further.
- Full pytest suite NOT yet run (dev-agent steering: don't run tests without
  explicit ask). Change validated via the pipeline (recalibrate→fv_calc→
  distribution+named cases) across all three leagues instead.
- Prototype scratch scripts (`scripts/proto_fv_*.py`) can be removed at release.
