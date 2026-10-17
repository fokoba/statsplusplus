# Prospect Valuation Model — Hand-off for Integration
### How "OOTP Dominator" turns OOTP ratings into a Current Value grade, a Potential grade, and a Future Value (FV) grade

*Prepared 2026-10-16 from the live code and calibration files of the Dominator fork (PPL league, game date 1955-07-14; eMLB league, game date 2034-06). Every formula and constant below was read from the source, which is included verbatim in Appendix B. Where a number is league-specific it is marked, and Appendix A has the live values for the two leagues this was built for.*

**Origin and licence.** The engine is Stats++ by Thomas Falsone (MIT licence). This document describes the "Dominator" fork's current state, which tracks upstream v1.17.1 plus fork modifications (section 11). The MIT notice in Appendix B must stay with any reuse of the code.

**How to use this file with an LLM (e.g. GPT-Astra).** Sections 1–10 are the specification in plain language with exact formulas. Section 12 is a worked example you can use as a test case. Appendix A holds the calibrated parameters. Appendix B is the reference implementation; where prose and code disagree, the code is authoritative. Do not treat any constant as universal: nearly all of them are fitted per league.

---

## 0. One-paragraph summary

The system turns scouted OOTP ratings into three grades on the 20–80 scouting scale. **Current Value ("composite" / "Ovr")** is how good a player is *now*, expressed through runs for hitters and a tool-weighted score for pitchers. **Potential ("ceiling")** is the same formula applied to the *potential* ratings. **Future Value (FV)** is a risk-discounted blend of current value and potential, rounded to the nearest 5, that reads like a scout's 20–80 FV: 50 = average regular, 60 = plus regular/#2 starter, 70 = star. A separate **risk label** (Low/Medium/High/Extreme) says how likely the gap between current and potential is to close, and a separate **development-pace tag** says whether a prospect is tracking above or below his age-peers. A **surplus** figure converts FV into dollars over a six-year control window.

---

## 1. Glossary: what each grade means

| Term | Scale | Meaning | Where computed |
|---|---|---|---|
| **Tool grades** | 20–80, multiples of 5 | Game ratings normalised to scouting scale (see §3) | `config/ratings.norm` |
| **Composite / "Ovr" / Current Value** | 20–80 | Present-tense overall value. Hitters: run-space (bat + baserunning + defence + positional adjustment). Pitchers: weighted tools + arsenal + stamina. | `composite.py` |
| **tool_only_score** | 20–80 | Composite before any observed-stats blend | engine |
| **ceiling_score** | 20–80 | Age-weighted blend of potential-composite and current composite ("display" ceiling) | `ceiling.compute_ceiling` |
| **true_ceiling / "Pot" / Potential** | 20–80 | Pure potential-ratings composite (no age blend). This is the *Pot* that drives FV and surplus. Floored at current composite. | `ceiling.compute_true_ceiling` |
| **Performance-adjusted ceiling (PAC)** | 20–80 | true_ceiling ±6 max, nudged by minor-league production relative to level and age | `fv.compute_performance_adjusted_ceiling` |
| **FV (grade)** | 20–80, multiples of 5 | Expected-peak-value grade. `fv_continuous` is the unrounded value used for dollars. | `fv.calc_fv` |
| **Risk** | Low / Medium / High / Extreme | Probability-style label for closing the gap (§7) | `fv.calc_fv` |
| **Dev-speed tag** | z-score + labels | Pace of development vs same-bucket, same-age-band peers, and a schedule tag (Ahead / On / Behind / At Risk) | `dev_speed.py` |
| **Surplus** | $ (league units) | (market value of projected WAR − projected salary) over 6 control years × discounts | `surplus.prospect_surplus` |

**Reading the scale:** 50 ≈ league-average regular; 45 ≈ bench/platoon; 55 ≈ solid regular; 60 ≈ plus regular / #2–3 starter; 65 ≈ all-star level; 70 ≈ MVP-calibre. In FV the sub-40 grades follow a role ladder: 40 bench/depth, 35 up-and-down/AAAA, 30 organisational, 25/20 fringe.

---

## 2. Pipeline overview

```
OOTP ratings export (ratings table: current + pot_* columns, splits, accuracy, makeup)
        │
        ▼  normalise every tool to 20-80 (rounded to 5); combine L/R splits (60% vR / 40% vL)
tool grades ───────────────┬──────────────────────────────────────────────┐
        │                  │                                              │
        ▼                  ▼                                              ▼
 CURRENT composite   POTENTIAL composite (same formula on pot_* tools)   (hitters also: component grades
 (hitter run-space;  = true_ceiling ; age-blended = ceiling_score         offensive_grade, baserunning_value,
  pitcher tools)                                                          defensive_value, offensive_ceiling)
        │                  │
        ▼                  ▼
  + observed-stats blend (MLB career runs; pitcher stats)      PAC = true_ceiling ± ≤6 from MiLB production
        │                  │
        └───────┬──────────┘
                ▼
   FV = f(current, potential, age, bucket, level-norm-age, accuracy, platoon, RP cap)
   risk = f(gap, age, closure rate, expected gap, makeup, MiLB stat modifier)
                │
                ▼
   Surplus = Σ over 6 control years [ market value(WAR(FV)) − salary ] × development × certainty × scarcity
```

Order of operations inside one refresh: `calibrate` (fit league parameters) → `evaluation engine` (composite, ceiling, components, history write-back) → `fv_calc` (FV, risk, surplus, dev-speed). Calibration must run before the engine, and the engine before FV.

---

## 3. Inputs and normalisation

**Source table `ratings`** (one row per player per snapshot date; OOTP export). Columns used:

- **Hitting (current / potential):** `cntct gap pow eye` / `pot_cntct pot_gap pot_pow pot_eye`; splits `cntct_l cntct_r gap_l gap_r pow_l pow_r eye_l eye_r`; also `ks`, `babip`. 
- **Running:** `speed`, `steal`, `stl_rt` (no potential variant — current is used).
- **Defence (current; positional potentials `pot_c pot_ss pot_second_b …` exist but the composite uses tool ratings):** catcher `c_frm c_blk c_arm`; infield `ifr ife ifa tdp`; outfield `ofr ofe ofa`.
- **Pitching (current / potential):** `stf mov ctrl hra pbabip` / `pot_stf pot_mov pot_ctrl pot_hra pot_pbabip`; stuff splits `stf_l stf_r`; pitch ratings `fst snk crv sld chg splt cutt cir_chg scr frk kncrv knbl` with `pot_*` variants; `stm` (stamina), `vel`.
- **Makeup/info:** `acc` (scouting accuracy VL/L/A/H/VH), `wrk_ethic`, `int_`, `lead`, `greed`, `loy`, `prone` (injury proneness), `height bats throws`.
- **Players table:** age, level (0 draft/FA pool, 1 MLB, 2 AAA, 3 AA, 4 A, 5 A-short, 6 rookie, 8 international complex), team/parent team, draft year/round, contract, position, role.

**Normalisation (`norm`).** League scale is "20-80", "1-100", or "1-20". Raw values are mapped to 20–80 and rounded to the nearest 5; PPL is natively 20–80, eMLB is 1–100.

**Hitter tool extraction.** `contact/gap/power/eye` = `0.6 × vR + 0.4 × vL` when both splits exist, else the overall rating. `speed/steal/stl_rt` as-is.

**Pitcher tool extraction.** `stuff = stf`, `control = ctrl`, `movement = hra` if hra > 20 else `mov` (HRA correlates r = 0.93 with movement and is the cleaner home-run signal). Stuff L/R kept for a platoon-balance penalty. Potential tools use `pot_stf/pot_mov/pot_ctrl` (movement keeps `pot_mov`), plus `pot_hra/pot_pbabip` falling back to current. Arsenal = every pitch rating normalised; potential arsenal = `pot_*` else current. Stamina = `stm` (default 50).

---

## 4. CURRENT VALUE (the composite)

### 4.1 Hitters — "run-space" composite (preferred model)

Value is built in **runs** and mapped to 20–80, so a player's grade *is* his projected value above an average player at his position, not an arbitrary weighted average of grades.

1. **Projected wOBA from tools** (league-fitted linear regression on the four hit tools):
   `wOBA = a + b₁·contact + b₂·gap + b₃·power + b₄·eye` (coefficients `tool_woba_fit`, Appendix A).
2. **Bat runs** (wRAA per 600 PA): `((wOBA − lg_wOBA) / woba_scale) × 600`.
3. **Baserunning runs:** league-fitted linear curve on speed (`br_curve`), plus a small steal adjustment if calibrated.
4. **Fielding runs:** per-position, per-league calibrated **multi-tool** curve, clamped to the 5th–95th percentile of observed ZR runs.
   - **C** = framing + blocking + arm (target is ZR + OOTP framing runs).
   - **SS/2B/3B** = range + error + arm + double-play (target is ZR − 0.5 × errors). 3B is error/arm-led.
   - **CF/COF** = range + error + arm.
   - **1B/DH** ≈ 0 fielding runs.
   Per-tool blend weights come from each tool's regression *slope* (runs per rating point × SD), shrunk toward a cross-league prior by sample size.
5. **Positional adjustment** (runs) = `POSITIONAL_WAR_ADJUSTMENTS[bucket] × runs_per_win`. League-specific; see §9.
6. **Total runs** = bat + baserunning + fielding + positional.
7. **Map to 20–80** (`runs_to_composite`, "hybrid" mapping):
   `comp = 50 + posAdjWAR × pts_per_war + (total_runs − pos_center[bucket]) × slope`
   with `pts_per_war = 4`, `slope ≈ 0.43` (= 4 pts/WAR ÷ runs per win), soft-tanh compression above 70 toward a cap of 80, hard floor 20. `pos_center[bucket]` is the population mean runs at that position, so an average player at *every* position maps to ≈ 50 plus his positional credit.
8. **Prospects (non-MLB and age ≤ 24) are scored "neutral-position":** positional adjustment = 0 and fielding credit is replaced by a small range bump: +6 runs if the position's range grade is ≥ 60 (OF) / ≥ 65 (IF), +12 runs if ≥ 70 (both). Applies for age ≤ 24 (`PROSPECT_NEUTRAL_POS_MAX_AGE`). Reason: young players' positions are still moving, so neither a premium-position bonus nor a weak-glove penalty is a reliable signal.
9. **MLB hitters blend observed career facet runs** into the bat / baserunning / fielding runs using stabilisation ramps: `confidence = min(cap, (PA_or_IP / full)^1.1)` with full = 600 PA (bat, cap 0.90), 250 PA (baserunning, cap 0.95), 900 IP (fielding, cap 0.80). Positive observed fielding at hard positions is multiplied by a calibrated "positional ZR difficulty" factor (`fld_difficulty`, 1.0–1.5).
10. **Prospect composites are tool-only by design** (since upstream v1.16.0): MiLB production is *not* blended into a prospect's present-ability grade (it inflated ~85% of prospects). It enters only through PAC (ceiling) and the risk modifier (§6, §7).

*Fallback (no run-space calibration available):* a grade-space blend — transformed offensive grade, baserunning and defence recombined with per-bucket shares, plus a speed×contact synergy term, a sub-MLB floor penalty for tools below 35, and a tool-imbalance penalty. See `compute_composite_hitter` in Appendix B.

**Component grades stored alongside** (hitters): `offensive_grade` (transformed weighted hit tools), `baserunning_value`, `defensive_value` (the engine's defensive grade), `durability_score`, `offensive_ceiling`.

### 4.2 Pitchers

```
effective_tools = compensation(stuff, movement, control)          # strong tool partially offsets weak one
tool_sum  = Σ transform(tool) × (w_tool / Σ w) × (1 − arsenal_weight)
arsenal_score = 50 + 5 × (min(3, max(0, #pitches ≥45 − 3)) + quality_bonus)   # quality_bonus 2 if best pitch ≥70, 1 if ≥65
raw = tool_sum + arsenal_score × arsenal_weight
SP stamina: if stm < 40 → −min(5, (40−stm)×0.15);  if stm > 45 → +min(4, (stm−45)×0.12)
platoon: if min(stuff_l, stuff_r) < 35 and |gap| ≥ 15 → −2 (or −3 if weak side ≤ 25)
sub-MLB floor penalty on core tools (below 35, rate 0.25/pt)
imbalance penalty: spread of (stuff, movement, control) > 20 → 0.20/pt over, plus weakness (<50) at 0.15/pt
composite = clamp(round(raw), 20, 80)
```

Weights are league-calibrated (PPL: SP control .325 / movement .329 / stuff .296 / arsenal .05; RP stuff-heavy .399). Each tool passes through a calibrated per-league **tool transform** (piecewise-linear at anchors 28/40/50/60/72 → grade deltas) that bends the grade→value curve (e.g. strong tools worth more than linear). Pitcher tool_only_score = composite before stats.

**Stats blending for pitchers (unlike prospect hitters).** MLB pitchers are blended with a stat-based 20–80 signal (`compute_composite_mlb`, up to ~60% weight with a large sample). Minor-league pitchers still receive an additive MiLB blend of up to 25% (level-discounted, recency-decayed, faded as MLB sample grows, discounted for young players with weak stats). Only run-space *hitters* skip this.

### 4.3 Two-way players
Composite is computed for both roles; the primary role's score is the composite, the secondary is kept, and `compute_combined_value` blends them. Ceiling takes the higher of the two.

---

## 5. POTENTIAL (the ceiling)

Two related numbers, both from the **potential** tool ratings run through the *same* composite machinery as §4:

- **`true_ceiling`** — what the player is *if every tool reaches its potential*. No age blend. Floored at current composite. For hitters it uses the run-space spine on potential tools with the same neutral-position rule for prospects; for pitchers `compute_composite_pitcher` on potential tools and potential arsenal. **This is the "Pot" that FV and surplus consume.**
- **`ceiling_score`** (display) — `potential_composite × w + composite × (1 − w)` where `w = clamp(1 − (age − 16) × 0.05, 0.30, 0.95)`. Young players lean on potential (0.95 at 16); veterans on current (0.30 from age 30). For pitchers (and grade-space hitters) a peak-tool bonus is added first: +1 per potential tool point above 60, capped at 10 on 1–100 leagues / 15 on 20–80 leagues. The run-space hitter path skips the bonus because it is a grade-space compensator.
- Both are floored at the *final* composite after any observed-stats blend (a player can't have a ceiling below what he already is).
- **PAC (applied just before FV):** `Pot' = true_ceiling + adjustment`, `adjustment = signal × age_mult × sample_confidence × 6` bounded to ±6, where `signal = clamp((stat_2080 − tool_only_score)/30, −1, 1)`; `age_mult` is larger for young outperformers and damps older ones; `sample_confidence = min(1, (effective_PA − 30)/270)`. This is the **only** place MiLB performance moves a hitter's ceiling.

**Reading the gap.** `gap = Pot − composite` is the single most important driver of FV and risk. Scouting accuracy (`acc`) is applied as a −5 FV penalty for "L" accuracy; it is otherwise a confidence note.

---

## 6. FUTURE VALUE (FV)

Inputs: `ovr` (current composite incl. stats blend), `pot` (true_ceiling after PAC), `age`, `bucket` (C, 1B, 2B, 3B, SS, CF, COF, SP, RP), `norm_age` (expected age at the player's level; §6.4), accuracy, platoon splits, makeup, and `stat_risk_modifier`.

Pre-step: RP potential is discounted: `effective_pot = round(pot × 0.85)` for relievers.
`gap = max(0, effective_pot − ovr)`.

Two paths exist. **Which path a hitter uses is a per-league switch** (`MODEL_PARAMS.FV_CEILING_ANCHORED`; PPL = off, eMLB = on). **Pitchers always use the legacy path.**

### 6.1 Legacy composite-anchored path (pitchers everywhere; hitters in PPL)

```
if gap ≤ 3:           fv = ovr
else:
    closure = interp(GAP_CLOSURE_table, age)          # fraction of the gap players of this age typically close
    target  = _TARGET_PRODUCT[age]                    # {17:.47,18:.47,19:.46,20:.48,21:.52,22:.50,23:.53,24:.30,25:.30}
    bust    = min(0.85, target / closure)             # bust discount; <1 means some don't develop
    peak    = ovr + gap × closure × bust              # expected peak composite
    ceil_weight = clamp((effective_pot − 50)/30, 0, 0.5)
    fv = peak × (1 − ceil_weight) + effective_pot × ceil_weight
```
- Then: accuracy "L" → −5; platoon penalty −5 (hitter: |Cntct_L − Cntct_R| ≥ 10 and weak side ≤ 25; pitcher: same on stuff); RP cap `fv ≤ 55`; legacy caps `fv ≤ effective_pot − 3` and, for hitters with `offensive_ceiling < 45`, `fv ≤ 50`; floor 20.
- **`fv_grade = round(fv / 5) × 5`**; `fv_continuous` (pre-rounding) is retained for dollars. Rounding is symmetric (nearest 5) so there is no cliff between players 0.1 apart.
- **Closure tables** `GAP_CLOSURE_*` (by age 17–25) default to upstream empirical values (hitters .87→.38, pitchers .93→.49) and are **overridden per league**: PPL pitchers are `{17:.93,18:.92,19:.91,20:.90,21:.494,22:.355,23:.206,24:.094,25:.035}`. Ages ≤ 20 are kept at upstream defaults because there is too little data to refit them.

### 6.2 Ceiling-anchored path (hitters in leagues where enabled)

```
ceiling_war = saturate(runs_to_war(composite_to_runs(Pot, mapping, bucket)))
p_dev = 1.0 if gap ≤ 2 else clamp(closure(age) × 1.1, 0, 1)         # FV_CEILING_STRENGTH = 1.1 (upside-friendly)
expected_war = p_dev × ceiling_war + (1 − p_dev) × (−0.3)          # a non-developer lands ≈ replacement
fv_continuous = fv_from_peak_war(expected_war, bucket)             # read off the per-position FV→WAR ladder
```
Same accuracy/platoon/RP adjustments follow. Rationale: grade the *projected WAR of the ceiling* discounted by the chance of realising it, so a high-ceiling bench bat can't ride a modest ceiling to FV 55. `runs_to_war = (runs + replacement_runs) / runs_per_win`; `saturate_war` is a tanh that bends WAR toward the league's real p02/p98 caps.

### 6.3 FV → WAR ladder (for context and for dollars)
Default peak-WAR per season: FV 80 = 10.0, 70 = 7.0, 65 = 5.5, 60 = 4.2, 55 = 2.9, 50 = 2.0, 45 = 1.2, 40 = 0.5 (RP: 3.2/2.6/2.3/2.0/1.6/1.2/0.8/0.5). Calibrated per league and per position (Appendix A). Sub-40 grades: 35 = 0.0, 30 = −0.6, 25 = −1.2, 20 = −2.0 WAR.

### 6.4 Level-normal age (`norm_age`) — how "young for the level" is judged
`LEVEL_NORM_AGE`: draft 18, MLB 26, AAA 26, AA 24, A 22, A-short 21, rookie 19, USL 19, DSL 18, intl 17. MiLB stat context uses per-league `MILB_NORM_AGES` from `model_weights.json`.

### 6.5 What FV deliberately is not
FV is *potential-weighted expected value at peak*, not "how good now". A 22-year-old with composite 41 and ceiling 64 can be FV 55 / Extreme risk — the grade reflects the high ceiling discounted for the probability of getting there.

---

## 7. RISK label

```
base_discount = 0.30 (age ≤19) | 0.35 (≤21) | 0.45 (≤23) | 0.60 (older)
expected_gap  = EXPECTED_GAP_table[age]            # pitcher defaults {17:18,18:15,19:13,20:11,21:9,22:7,23:5,24:4,25:3}
excess_gap    = max(0, gap − expected_gap)
gap_scale     = 0.70 if excess_gap ≥ 15 else 0.85 if excess_gap ≥ 8 else 1.0
char_adj      = +.03 (work ethic H/VH) / −.03 (L)  +.02 (intelligence H/VH) / −.02 (L)
dev_confidence = clamp(closure(age) × base_discount × gap_scale + char_adj + stat_risk_modifier, 0, 1)

risk = Low      if gap < 3 or dev_confidence ≥ 0.40
       Medium   if ≥ 0.25
       High     if ≥ 0.15
       Extreme  otherwise
```
`stat_risk_modifier ∈ [−0.12, +0.12]` is the MiLB performance signal (production above tools lowers risk, especially for players young for their level; production below tools raises it). **Consequence worth knowing:** risk is dominated by the size of the gap. A wide gap relative to age's expected gap makes `gap_scale` bite and pushes toward High/Extreme regardless of how the player is actually developing. The risk label is independent of the development-pace tag in §8.

---

## 8. DEVELOPMENT-PACE (dev-speed) tags — separate from FV

Computed after FV (does not feed back into FV, risk, or surplus).

- **Window:** trailing 12 months of monthly rating snapshots; needs ≥ 3 snapshots spanning ≥ 150 days; only reported for age ≥ 18 and composite ≥ 25.
- **Signal:** annualised change in `offensive_grade` for hitters (composite for pitchers), converted to a **z-score against same-group (SP / RP / C / other hitters), same 2-year-age-band peers** (≥ 20 peers per cell, ≤ 25-year-olds).
- **Label** from z and gap: ≥ +1 "Rising (room)" if gap ≥ 12 else "Rising (realizing)"; ≤ −1 "Stalled — upside at risk" (gap ≥ 12) or "Regressing/plateaued"; ≤ −0.5 with gap ≥ 12 "Watch"; else "On pace".
- **Schedule tag** (Ahead / On Schedule / Behind Schedule / At Risk) combines pace, gap-closure rate, runway to peak age and tool stagnation. As of 2026-10-12 it was hardened: (1) rating history is first **rescaled to the latest model's scale** (steps measured from players whose tools didn't change), because composites in history are not on one scale; (2) "behind" and "not closing" use **peer-relative cutoffs** (bottom/top quartile of same-group, same-age peers), not fixed numbers; (3) pitchers need **all three tools flat and below-peer pace** to count as stagnant (grades move in 5-point steps, so two flat tools is normal); (4) players **drafted in the current season get no schedule tag**.
- Confidence tier High/Medium/Low from window length, accuracy, playing time.
- It can scale a facet's development *timing* via `dev_pace_from_z` (clamped modifier 0.6–1.4); it never changes the ceiling.

---

## 9. Per-league calibration (what is fitted vs fixed)

`calibrate.py` re-derives, per league, from that league's own ratings and stats: hitter/pitcher tool weights; tool transforms; wOBA weights and `woba_scale`; the tool→wOBA regression; baserunning and per-position fielding curves (multi-tool weights); runs-per-win and replacement runs; WAR saturation caps (p02/p50/p98 of full-time hitters); the composite→runs mapping with per-position centres; FV→peak-WAR ladders per position; and **positional WAR adjustments**.

**Positional WAR adjustments.** Real-MLB defaults: C +1.2, SS +0.7, 2B +0.3, CF +0.25, 3B +0.2, COF −0.7, 1B −1.2, DH −1.7, RP −1.0, SP 0. These did **not** hold in either league (PPL catchers produced the *lowest* WAR per 600 PA of any position). Since 2026-10-15 the calibration estimates per-league values by regressing WAR/600 PA on offence, baserunning and defence grades plus a position indicator (PA-weighted, MLB regulars, last 3 seasons), centring, and **shrinking toward the defaults by n/(n+20)**. Derived: PPL C +0.36, 1B −0.72, 2B +0.22, 3B −0.13, SS +0.54, CF +0.20, COF −0.30; eMLB C +0.63, 1B −0.64, 2B +0.12, 3B −0.01, SS +0.21, CF +0.03, COF −0.25. A hand-set `MODEL_PARAMS.POSITIONAL_WAR_ADJ` overrides them.

**Hand-set per-league overrides (preserved across recalibration)** live in `model_weights.json → MODEL_PARAMS`: `FV_CEILING_ANCHORED`, `PEAK_AGE_HITTER/PITCHER`, `GAP_CLOSURE_HITTER/PITCHER_OVERRIDE`, `POSITIONAL_WAR_ADJ`.

---

## 10. Surplus (FV → dollars) in brief

`prospect_surplus(fv_continuous, age, level, bucket, dpw, min_sal, ovr, pot, …)`:
1. `years_to_mlb` by level (draft/intl prospects are placed at an effective level from their composite: ≥50 → AAA, ≥42 → AA, ≥35 → A, else A-short).
2. `peak_WAR = ladder(FV, bucket)`; for near-maxed players (`ovr/pot > 0.7`) it blends toward the WAR implied by current ovr.
3. For each of 6 control years: `WAR = (peak_WAR + posAdj) × aging_mult(age) × ramp[year]`; `market_value = market_value(WAR, $/WAR, min_salary) × (1 − 0.05)^(years_out + year)` (PROSPECT_DISCOUNT_RATE = 0.05); salary = league minimum for pre-arb years then an arbitration (or perpetual-arbitration) model.
4. `surplus_year = market_value − salary × discount`; sum over years, then multiply by `dev_discount(level, age, ovr) × certainty_multiplier(ovr, pot) × scarcity_multiplier(pot, bucket, def)`.
5. Floored at $0 (you never pay negative dollars).

League economics: PPL $/WAR ≈ 23,812 (units are the sim's dollars, 1955 scale; league minimum 6,800); eMLB uses its own.

---

## 11. Differences from upstream Stats++ (the fork)

1. **Per-league gap-closure overrides** for PPL ages ≥ 21 (hitters 21: .432, 22: .287, 23: .133, 24: .045, 25: .035; pitchers 21: .494, 22: .355, 23: .206, 24: .094, 25: .035), fitted from 17 months of PPL rating history (Feb 1954–Jul 1955). Ages ≤ 20 unchanged. Upstream defaults are much more generous at 22–25.
2. **PPL FV for hitters is on the legacy composite path** (`FV_CEILING_ANCHORED = 0`) because PPL's compressed composite→runs mapping turned a 55 ceiling into ~4.6 WAR and graded ~250 hitter prospects FV 60+.
3. **Per-league positional WAR adjustments** (§9) instead of fixed MLB constants.
4. **Positional ZR difficulty bonus** (`fld_difficulty`) on observed positive fielding at hard positions.
5. **Dev-speed hardening** (§8): scale-drift neutralisation, peer-relative cutoffs, pitcher stagnation rule, no tag in draft year. (Upstream had none of these.)
6. **`true_ceiling` history write-back** in addition to composite/ceiling.
7. Tracks upstream through v1.17.1 (hitter-grade decompression + tool-only prospects v1.16.0; per-position multi-tool defensive runs v1.17.0).

---

## 12. Worked example (use as a regression test)

**Matthew Willis** — PPL, SP, age 21, AAA, scouting accuracy VH, makeup N/N. Ratings snapshot 1955-07-14: composite (current) **41**, ceiling_score 61, **true_ceiling 64**. (No MiLB PAC adjustment applied in this example.) Level: AAA → `norm_age` 26.

| Step | Value |
|---|---|
| gap = pot − ovr | 64 − 41 = **23** |
| closure (PPL pitcher, age 21) | **0.494** |
| target_product(21) | 0.52 → bust = min(0.85, 0.52 / 0.494) = **0.85** |
| peak = ovr + gap × closure × bust | 41 + 23 × 0.494 × 0.85 = **50.66** |
| ceil_weight = clamp((64 − 50)/30, 0, 0.5) | **0.467** |
| fv_continuous = 50.66 × 0.533 + 64 × 0.467 | **56.88** |
| accuracy VH (no penalty), no platoon penalty, not RP | — |
| **FV grade** = round(56.88/5)×5 | **55** |
| risk: base_discount(21) = 0.35; expected_gap(21) = 9 → excess = 14 → gap_scale 0.85; dev_confidence = 0.494 × 0.35 × 0.85 = **0.147** | **Extreme** (< 0.15; it would be High at 0.173 without the gap_scale) |

The stored values in the live database for this player on this date are FV 55, risk Extreme, fv_continuous 56.88 — matching the hand calculation. Surplus 266,310 (league dollars) follows from §10.

---

## 13. Known limitations and honest caveats (read before porting)

1. **Validation basis is thin and in-sample.** Calibration fits to a league's own WAR/ratings; PPL's rating history is only 17 months. Composite explains only part of WAR variance (a rough check found ~0.10 WAR per composite point in PPL, ~0.16 in eMLB). The model is a ranking/valuation aid, not a validated forecast.
2. **Development is lumpy and does not persist.** In PPL, growth in tool ratings moves in 5-point steps; pace over one window did not predict pace in the next (correlation ≈ 0). Short-window "behind schedule" is a relative statement, not a bust forecast.
3. **Who develops (PPL data, SPs):** growth in summed stuff + movement + control peaks at ages 20–22 (~+3.6 to +3.9 per year), is small at 18–19 (~+2.4) and ~+1.7 at 23, and is flat/negative from 25. 21–22-year-old SPs are *not* done developing; very few are capped before 24 (share with ≤5 points of headroom to potential: 3% at 22, 11% at 23, 28% at 24, 49% at 25).
4. **Potential is a scouted estimate.** The `acc` field matters; low-accuracy potential is penalised only as a flat −5 on FV.
5. **Risk is dominated by gap size** (§7), so wide-gap young pitchers often read High/Extreme even when tracking normally.
6. **Positional adjustments are estimated with shrinkage and small n** (e.g. PPL catchers n = 34, SE ≈ 0.3 WAR). Catcher value in particular depends on a league's framing value (0.08 runs per rating point in PPL vs 0.69 in vMLB, 0.34 in eMLB).
7. **Pitchers and hitters follow different FV paths** in PPL (legacy for both) and eMLB (ceiling-anchored only for hitters). Do not compare FV across paths without recalibrating.
8. **Environment dependence.** OOTP "talent change randomness" (PPL = 125%) inflates real-world variance in development; any model trained here will over-weight volatility relative to a 100% league.

---

## 14. Suggestions for integrating into another model

- Keep the **three-grade separation** (current / potential / FV) and the **risk label separate from FV**; both make comparison far easier than a single number.
- Compute **current value in runs and convert with a league-fitted map** rather than averaging grades; it makes positions comparable and lets you attach dollars.
- Use **`true_ceiling` (no age blend) as Pot** for FV and keep the age-blended `ceiling_score` only for display.
- Fit **gap-closure by age per league** from your own rating snapshots; do not import the defaults blindly.
- Treat **minor-league production as a ceiling/risk modifier, not a current-ability input** for hitters.
- If you add development-pace measures, **neutralise any history written on an older model scale** before differencing, or every prospect will look like he regressed.
- Re-run the worked example in §12 as a unit test after porting.

Contact for questions about the original design: the upstream project's docs (`docs/composite_calculation_reference.md`, `docs/valuation_model.md`, `docs/unified_evaluation_design.md`, `docs/evaluation_model.md`) go deeper on derivations; they predate some of the fork's changes.



---

# APPENDIX A — Calibrated parameters (as of PPL 1955-07-14, eMLB 2034-06)

These are the live per-league numbers. Everything is re-fit by the calibration script; nothing here is a universal constant except where marked default.


## A.1 PPL (20-80 native scale, 154 games, 1955 run environment)

### tool_weights.json (hitter/pitcher weights, recombination, run-space calibration, tool transforms)
```json
{
 "version": 1,
 "source": "calibrated",
 "calibration_date": "1955-01-01",
 "calibration_n": {"hitter_offensive": 152, "pitcher_SP": 64, "pitcher_RP": 57},
 "hitter": {"C": {"contact": 0.1674, "eye": 0.1534, "gap": 0.1285, "power": 0.3407, "speed": 0.0141, "steal": 0.0229, "stl_rt": 0.0229, "defense": 0.15}, "SS": {"contact": 0.1674, "eye": 0.1534, "gap": 0.1285, "power": 0.3407, "speed": 0.0141, "steal": 0.0229, "stl_rt": 0.0229, "defense": 0.15}, "2B": {"contact": 0.1674, "eye": 0.1534, "gap": 0.1285, "power": 0.3407, "speed": 0.0141, "steal": 0.0229, "stl_rt": 0.0229, "defense": 0.15}, "3B": {"contact": 0.178, "eye": 0.1632, "gap": 0.1366, "power": 0.3623, "speed": 0.0141, "steal": 0.0229, "stl_rt": 0.0229, "defense": 0.1}, "CF": {"contact": 0.1674, "eye": 0.1534, "gap": 0.1285, "power": 0.3407, "speed": 0.0141, "steal": 0.0229, "stl_rt": 0.0229, "defense": 0.15}, "COF": {"contact": 0.1992, "eye": 0.1826, "gap": 0.1529, "power": 0.4054, "speed": 0.0141, "steal": 0.0229, "stl_rt": 0.0229, "defense": 0.0}, "1B": {"contact": 0.1992, "eye": 0.1826, "gap": 0.1529, "power": 0.4054, "speed": 0.0141, "steal": 0.0229, "stl_rt": 0.0229, "defense": 0.0}},
 "pitcher": {"SP": {"control": 0.3252, "movement": 0.3293, "stuff": 0.2955, "arsenal": 0.05}, "RP": {"control": 0.2715, "movement": 0.2794, "stuff": 0.3991, "arsenal": 0.05}},
 "recombination": {"C": {"offense": 0.79, "defense": 0.15, "baserunning": 0.06}, "SS": {"offense": 0.79, "defense": 0.15, "baserunning": 0.06}, "2B": {"offense": 0.79, "defense": 0.15, "baserunning": 0.06}, "3B": {"offense": 0.84, "defense": 0.1, "baserunning": 0.06}, "CF": {"offense": 0.79, "defense": 0.15, "baserunning": 0.06}, "COF": {"offense": 0.94, "defense": 0.0, "baserunning": 0.06}, "1B": {"offense": 0.94, "defense": 0.0, "baserunning": 0.06}},
 "run_space": {"woba_scale": 1.2927, "lg_woba": 0.3306, "woba_weights": {"ubb": 0.6984, "hbp": 0.7308, "b1": 0.8989, "b2": 1.2866, "b3": 1.6358, "hr": 2.1267}, "tool_woba_fit": [-0.029345, 0.003179, 0.001067, 0.001966, 0.001088], "br_curve": {"intercept": -1.8474205284010008, "slope": 0.03513644160626211, "r": 0.323, "n": 223, "mean_grade": 52.6, "clamp_lo": -4.5, "clamp_hi": 2.7}, "def_curve": {"CF": {"intercept": -70.7038, "slope": 1.2990543659275768, "r": 0.516, "n": 27, "mean_grade": 54.4, "clamp_lo": -27.3, "clamp_hi": 18.4, "weights": {"ofr": 0.731, "ofe": 0.149, "ofa": 0.119}, "multitool": true}, "COF": {"intercept": -56.7543, "slope": 1.1712139150356626, "r": 0.717, "n": 43, "mean_grade": 48.5, "clamp_lo": -29.1, "clamp_hi": 15.5, "weights": {"ofr": 0.616, "ofe": 0.252, "ofa": 0.131}, "multitool": true}, "SS": {"intercept": -114.2749, "slope": 2.083901573790351, "r": 0.936, "n": 13, "mean_grade": 54.8, "clamp_lo": -43.3, "clamp_hi": 11.1, "weights": {"ifr": 0.37, "ife": 0.236, "ifa": 0.263, "tdp": 0.131}, "multitool": true}, "2B": {"intercept": -66.929, "slope": 1.3759348636618678, "r": 0.772, "n": 11, "mean_grade": 48.6, "clamp_lo": -33.9, "clamp_hi": 6.7, "weights": {"ifr": 0.436, "ife": 0.227, "ifa": 0.211, "tdp": 0.125}, "multitool": true}, "3B": {"intercept": -33.4622, "slope": 0.682170902211939, "r": 0.77, "n": 9, "mean_grade": 49.1, "clamp_lo": -40.0, "clamp_hi": 0.2, "weights": {"ifr": 0.24, "ife": 0.314, "ifa": 0.267, "tdp": 0.18}, "multitool": true}, "C": {"intercept": -12.0672, "slope": 0.23552238862302727, "r": 0.685, "n": 26, "mean_grade": 51.2, "clamp_lo": -6.0, "clamp_hi": 5.6, "weights": {"c_frm": 0.447, "c_blk": 0.369, "c_arm": 0.184}, "multitool": true}}, "fld_difficulty": {"SS": 1.344, "2B": 1.031, "3B": 1.0, "CF": 1.429, "COF": 1.0, "C": 1.339}, "anchor": {"runs_per_win": 9.137, "replacement_runs": 10.64, "sat_top": 7.22, "sat_bot": -0.74, "sat_mid": 2.37}, "comp_mapping": {"slope": 0.4377804531027689, "pts_per_war": 4.0, "target_center": 50.0, "pos_centers": {"C": 6.0, "3B": 5.62, "CF": 15.14, "COF": 9.96, "2B": 12.7, "1B": -3.83, "SS": 11.32}, "global_center": 8.47, "top_knee": 70.0, "cap": 80.0, "floor": 20.0}},
 "tool_transforms": {"hitter": {"contact": [-9.0, -4.16, 0.0, 8.33, 12.87], "gap": [-3.0, -1.68, 0.0, 3.99, 4.74], "power": [-8.25, -4.36, 0.0, 6.83, 12.73], "eye": [-5.99, -2.81, 0.0, 3.27, 10.27], "speed": [-5.13, -2.0, 0.0, 0.0, 0.0]}, "SP": {"stuff": [-9.15, -5.75, 0.0, 4.67, 12.42], "movement": [-6.0, -3.14, 0.0, 10.77, 10.77], "control": [-6.0, -3.82, 0.0, 6.22, 8.39]}, "RP": {"stuff": [-9.0, -4.02, 0.0, 4.63, 11.19], "movement": [-6.0, -3.08, 0.0, 3.15, 7.19], "control": [-6.0, -3.28, 0.0, 3.49, 7.0]}}
}
```

### model_weights.json (selected: FV->peak-WAR ladders, development curves, per-league overrides)
```json
{
 "calibration_date": "1955-07-14",
 "calibration_years": "1952-1954",
 "FV_TO_PEAK_WAR": {"80": 9.0, "70": 7.5, "65": 6.0, "60": 4.5, "55": 3.2, "50": 2.2, "45": 2.0, "40": 1.0},
 "FV_TO_PEAK_WAR_BY_POS": {"C": {"80": 9.0, "70": 7.5, "65": 6.0, "60": 4.5, "55": 3.2, "50": 2.2, "45": 2.0, "40": 1.0}, "SS": {"80": 9.0, "70": 7.5, "65": 6.0, "60": 4.5, "55": 3.2, "50": 2.2, "45": 2.0, "40": 1.0}, "2B": {"80": 9.0, "70": 7.5, "65": 6.0, "60": 4.5, "55": 3.2, "50": 2.2, "45": 2.0, "40": 1.0}, "3B": {"80": 9.0, "70": 7.5, "65": 6.0, "60": 4.5, "55": 3.2, "50": 2.2, "45": 2.0, "40": 1.0}, "CF": {"80": 9.0, "70": 7.5, "65": 6.0, "60": 4.5, "55": 3.2, "50": 2.2, "45": 2.0, "40": 1.0}, "COF": {"80": 9.0, "70": 7.5, "65": 6.0, "60": 4.5, "55": 3.2, "50": 2.2, "45": 2.0, "40": 1.0}, "1B": {"80": 9.0, "70": 7.5, "65": 6.0, "60": 4.5, "55": 3.2, "50": 2.2, "45": 2.0, "40": 1.0}},
 "FV_TO_PEAK_WAR_SP": {"80": 8.0, "70": 6.5, "65": 5.5, "60": 4.0, "55": 2.8, "50": 1.9, "45": 1.7, "40": 0.8},
 "FV_TO_PEAK_WAR_RP": {"80": 2.5, "70": 2.0, "65": 1.5, "60": 1.2, "55": 1.0, "50": 0.7, "45": 0.5, "40": 0.3},
 "SCARCITY_MULT": {"42": 0.0, "44": 0.03, "45": 0.1, "46": 0.2, "47": 0.35, "48": 0.5, "49": 0.65, "50": 0.8, "51": 0.9, "52": 0.97, "53": 1.0, "80": 1.0},
 "PAP_SCALE": 25000000,
 "MODEL_PARAMS": {"FV_CEILING_ANCHORED": 0, "PEAK_AGE_HITTER": 26, "PEAK_AGE_PITCHER": 26, "GAP_CLOSURE_HITTER_OVERRIDE": {"17": 0.87, "18": 0.86, "19": 0.84, "20": 0.83, "21": 0.432, "22": 0.287, "23": 0.133, "24": 0.045, "25": 0.035}, "GAP_CLOSURE_PITCHER_OVERRIDE": {"17": 0.93, "18": 0.92, "19": 0.91, "20": 0.9, "21": 0.494, "22": 0.355, "23": 0.206, "24": 0.094, "25": 0.035}}
}
```


## A.2 eMLB (1-100 scale, 162 games, modern run environment)

### tool_weights.json (hitter/pitcher weights, recombination, run-space calibration, tool transforms)
```json
{
 "version": 1,
 "source": "calibrated",
 "calibration_date": "2034-01-01",
 "calibration_n": {"hitter_offensive": 359, "pitcher_SP": 210, "pitcher_RP": 176},
 "hitter": {"C": {"contact": 0.2307, "eye": 0.2181, "gap": 0.0919, "power": 0.2493, "speed": 0.03, "steal": 0.018, "stl_rt": 0.012, "defense": 0.15}, "SS": {"contact": 0.2307, "eye": 0.2181, "gap": 0.0919, "power": 0.2493, "speed": 0.03, "steal": 0.018, "stl_rt": 0.012, "defense": 0.15}, "2B": {"contact": 0.2307, "eye": 0.2181, "gap": 0.0919, "power": 0.2493, "speed": 0.03, "steal": 0.018, "stl_rt": 0.012, "defense": 0.15}, "3B": {"contact": 0.2453, "eye": 0.2319, "gap": 0.0977, "power": 0.2651, "speed": 0.03, "steal": 0.018, "stl_rt": 0.012, "defense": 0.1}, "CF": {"contact": 0.2307, "eye": 0.2181, "gap": 0.0919, "power": 0.2493, "speed": 0.03, "steal": 0.018, "stl_rt": 0.012, "defense": 0.15}, "COF": {"contact": 0.2745, "eye": 0.2595, "gap": 0.1093, "power": 0.2967, "speed": 0.03, "steal": 0.018, "stl_rt": 0.012, "defense": 0.0}, "1B": {"contact": 0.2745, "eye": 0.2595, "gap": 0.1093, "power": 0.2967, "speed": 0.03, "steal": 0.018, "stl_rt": 0.012, "defense": 0.0}},
 "pitcher": {"SP": {"control": 0.1792, "movement": 0.4734, "stuff": 0.2974, "arsenal": 0.05}, "RP": {"control": 0.3552, "movement": 0.4342, "stuff": 0.1606, "arsenal": 0.05}},
 "recombination": {"C": {"offense": 0.79, "defense": 0.15, "baserunning": 0.06}, "SS": {"offense": 0.79, "defense": 0.15, "baserunning": 0.06}, "2B": {"offense": 0.79, "defense": 0.15, "baserunning": 0.06}, "3B": {"offense": 0.84, "defense": 0.1, "baserunning": 0.06}, "CF": {"offense": 0.79, "defense": 0.15, "baserunning": 0.06}, "COF": {"offense": 0.94, "defense": 0.0, "baserunning": 0.06}, "1B": {"offense": 0.94, "defense": 0.0, "baserunning": 0.06}},
 "run_space": {"woba_scale": 1.234, "lg_woba": 0.3214, "woba_weights": {"ubb": 0.6668, "hbp": 0.6977, "b1": 0.8581, "b2": 1.2282, "b3": 1.5616, "hr": 2.0303}, "tool_woba_fit": [0.06333, 0.001904, 0.000469, 0.001905, 0.00063], "br_curve": {"intercept": -2.7156655798481286, "slope": 0.04924186020060155, "r": 0.376, "n": 535, "mean_grade": 55.1, "clamp_lo": -4.0, "clamp_hi": 3.0}, "def_curve": {"CF": {"intercept": -69.5316, "slope": 1.215735870718008, "r": 0.788, "n": 58, "mean_grade": 57.2, "clamp_lo": -23.9, "clamp_hi": 18.3, "weights": {"ofr": 0.512, "ofe": 0.297, "ofa": 0.191}, "multitool": true}, "COF": {"intercept": -47.101, "slope": 0.9069874825180356, "r": 0.761, "n": 90, "mean_grade": 51.9, "clamp_lo": -23.4, "clamp_hi": 11.3, "weights": {"ofr": 0.691, "ofe": 0.197, "ofa": 0.112}, "multitool": true}, "SS": {"intercept": -81.6839, "slope": 1.4022219445747286, "r": 0.737, "n": 36, "mean_grade": 58.3, "clamp_lo": -29.2, "clamp_hi": 11.2, "weights": {"ifr": 0.412, "ife": 0.26, "ifa": 0.209, "tdp": 0.119}, "multitool": true}, "2B": {"intercept": -30.4503, "slope": 0.579009852327272, "r": 0.558, "n": 12, "mean_grade": 52.6, "clamp_lo": -16.4, "clamp_hi": 9.4, "weights": {"ifr": 0.423, "ife": 0.243, "ifa": 0.171, "tdp": 0.162}, "multitool": true}, "3B": {"intercept": -17.5063, "slope": 0.3367973191661651, "r": 0.526, "n": 23, "mean_grade": 52.0, "clamp_lo": -13.5, "clamp_hi": 0.8, "weights": {"ifr": 0.285, "ife": 0.304, "ifa": 0.236, "tdp": 0.175}, "multitool": true}, "C": {"intercept": -36.4026, "slope": 0.6692370332370124, "r": 0.841, "n": 58, "mean_grade": 54.4, "clamp_lo": -12.4, "clamp_hi": 18.7, "weights": {"c_frm": 0.46, "c_blk": 0.382, "c_arm": 0.158}, "multitool": true}}, "fld_difficulty": {"SS": 1.449, "2B": 1.154, "3B": 1.056, "CF": 1.466, "COF": 1.0, "C": 1.5}, "anchor": {"runs_per_win": 9.246, "replacement_runs": 14.42, "sat_top": 8.58, "sat_bot": -1.44, "sat_mid": 1.89}, "comp_mapping": {"slope": 0.4326195111399524, "pts_per_war": 4.0, "target_center": 50.0, "pos_centers": {"COF": -4.1, "3B": 1.5, "C": 0.88, "2B": 2.74, "1B": -6.97, "SS": 6.73, "CF": 13.46}, "global_center": 0.69, "top_knee": 70.0, "cap": 80.0, "floor": 20.0}},
 "tool_transforms": {"hitter": {"contact": [-9.0, -6.32, 0.0, 10.03, 15.0], "gap": [-2.94, -0.64, 0.0, 1.77, 1.77], "power": [-10.58, -7.01, 0.0, 8.55, 15.0], "eye": [-6.08, -3.01, 0.0, 6.62, 13.96], "speed": [-2.98, -1.58, 0.0, 2.72, 8.37]}, "SP": {"stuff": [-8.94, -3.96, 0.0, 4.53, 11.02], "movement": [-6.0, -4.66, 0.0, 7.38, 9.61], "control": [-6.08, -5.3, 0.0, 4.68, 7.22]}, "RP": {"stuff": [-8.97, -3.19, 0.0, 4.14, 10.38], "movement": [-6.17, -3.98, 0.0, 4.33, 7.03], "control": [-6.08, -3.08, 0.0, 3.65, 6.57]}}
}
```

### model_weights.json (selected: FV->peak-WAR ladders, development curves, per-league overrides)
```json
{
 "calibration_date": "2034-06-09",
 "calibration_years": "2031-2033",
 "FV_TO_PEAK_WAR": {"80": 7.5, "70": 6.6, "65": 5.6, "60": 4.7, "55": 3.7, "50": 2.8, "45": 1.8, "40": 0.8},
 "FV_TO_PEAK_WAR_BY_POS": {"C": {"80": 7.5, "70": 6.5, "65": 5.4, "60": 4.3, "55": 3.3, "50": 2.2, "45": 1.1, "40": 0.1}, "SS": {"80": 8.8, "70": 7.7, "65": 6.6, "60": 5.5, "55": 4.5, "50": 3.4, "45": 2.3, "40": 1.2}, "2B": {"80": 7.2, "70": 6.3, "65": 5.4, "60": 4.5, "55": 3.7, "50": 2.8, "45": 1.9, "40": 1.0}, "3B": {"80": 7.4, "70": 6.5, "65": 5.7, "60": 4.8, "55": 3.9, "50": 3.1, "45": 2.2, "40": 1.4}, "CF": {"80": 8.8, "70": 7.6, "65": 6.5, "60": 5.4, "55": 4.2, "50": 3.1, "45": 2.0, "40": 0.8}, "COF": {"80": 6.5, "70": 5.7, "65": 4.8, "60": 4.0, "55": 3.2, "50": 2.4, "45": 1.6, "40": 0.8}, "1B": {"80": 6.6, "70": 5.8, "65": 4.9, "60": 4.1, "55": 3.2, "50": 2.4, "45": 1.5, "40": 0.7}},
 "FV_TO_PEAK_WAR_SP": {"80": 4.9, "70": 4.4, "65": 3.9, "60": 3.4, "55": 2.9, "50": 2.4, "45": 1.9, "40": 1.4},
 "FV_TO_PEAK_WAR_RP": {"80": 1.6, "70": 1.4, "65": 1.2, "60": 1.0, "55": 0.8, "50": 0.6, "45": 0.5, "40": 0.3},
 "SCARCITY_MULT": {"40": 1.0, "80": 1.0},
 "PAP_SCALE": 25000000,
 "gap_closure_hitter": {"17": 0.9, "18": 0.9, "19": 0.89, "20": 0.88, "21": 0.85, "22": 0.78, "23": 0.64, "24": 0.52, "25": 0.43, "26": 0.3},
 "gap_closure_pitcher": {"17": 0.94, "18": 0.94, "19": 0.93, "20": 0.92, "21": 0.9, "22": 0.87, "23": 0.79, "24": 0.68, "25": 0.54, "26": 0.39},
 "age_runway_hitter": {"17": 1.49, "18": 1.46, "19": 1.33, "20": 1.18, "21": 1.0, "22": 0.83, "23": 0.7, "24": 0.61, "25": 0.38, "26": 0.14},
 "age_runway_pitcher": {"17": 1.34, "18": 1.39, "19": 1.25, "20": 1.13, "21": 1.0, "22": 0.81, "23": 0.64, "24": 0.53, "25": 0.34, "26": 0.09},
 "expected_gap_hitter": {"17": 17, "18": 17, "19": 15, "20": 13, "21": 11, "22": 10, "23": 8, "24": 7, "25": 4, "26": 1},
 "expected_gap_pitcher": {"17": 13, "18": 15, "19": 13, "20": 12, "21": 11, "22": 9, "23": 7, "24": 6, "25": 4, "26": 1},
 "YEARS_TO_MLB": {"MLB": 0, "AAA": 1.1, "AA": 1.9, "A": 3.1, "Rookie": 5.0, "USL": 5.0, "Intl": 3.7},
 "MODEL_PARAMS": {"PEAK_AGE_HITTER": 26, "PEAK_AGE_PITCHER": 27}
}
```



---

# APPENDIX B — Source code (verbatim, Python 3.10+, no I/O in the pure modules)

Original project: Stats++ by Thomas Falsone (MIT License, github.com/tfalsone/statsplusplus). This is the "OOTP Dominator" fork (github.com/fokoba/statsplusplus) with the modifications listed in section 11. MIT notice must travel with any reuse of this code:

> MIT License — Copyright (c) 2026 Thomas Falsone. Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated documentation files, to deal in the Software without restriction, including without limitation the rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software, subject to inclusion of this copyright notice and permission notice in all copies or substantial portions of the Software. THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND.


## B.1 fv.py — FV grade, risk label, stat-based risk modifier, PAC (complete file)
```python
"""FV (Future Value) grade calculation.

Pure computation of prospect FV grades and risk labels from composite/ceiling
scores. No DB access, no global state.

Public API:
    calc_fv(ovr, pot, age, bucket, norm_age, ...) -> tuple[int, str]
    dev_weight(age, norm_age, level, is_pitcher) -> float
    age_development_mult(age, is_pitcher) -> float
"""

from __future__ import annotations

from typing import Any, Optional

from statsplusplus.evaluation.constants import RP_POT_DISCOUNT


# Realization-credit strength for the ceiling-anchored FV (hitters). Scales
# p(develops) = closure_rate(age) * strength. 1.0 = raw empirical closure;
# >1.0 is upside-friendly (credits more of the ceiling for toolsy prospects).
# Chosen 1.1 per the prototype sweep (user preference: grade raw high-upside
# players generously, while the WAR anchor still caps no-bat role players).
FV_CEILING_STRENGTH: float = 1.1


# ---------------------------------------------------------------------------
# Development curve defaults (overridable via model_weights)
# ---------------------------------------------------------------------------

# Age-based development runway multiplier.
# Derived from cross-sectional OVR/POT gap analysis. Normalized to age 21 = 1.0.
AGE_RUNWAY_HITTER_DEFAULT: dict[int, float] = {
    17: 1.60, 18: 1.43, 19: 1.26, 20: 1.16, 21: 1.00,
    22: 0.73, 23: 0.55, 24: 0.43, 25: 0.33, 26: 0.19,
}
AGE_RUNWAY_PITCHER_DEFAULT: dict[int, float] = {
    17: 1.56, 18: 1.42, 19: 1.30, 20: 1.16, 21: 1.00,
    22: 0.79, 23: 0.62, 24: 0.48, 25: 0.35, 26: 0.20,
}

# Gap closure rates by age.
GAP_CLOSURE_HITTER_DEFAULT: dict[int, float] = {
    17: 0.87, 18: 0.86, 19: 0.84, 20: 0.83, 21: 0.80,
    22: 0.68, 23: 0.48, 24: 0.38, 25: 0.38,
}
GAP_CLOSURE_PITCHER_DEFAULT: dict[int, float] = {
    17: 0.93, 18: 0.92, 19: 0.91, 20: 0.90, 21: 0.87,
    22: 0.83, 23: 0.72, 24: 0.59, 25: 0.49,
}

# Expected gap by age.
EXPECTED_GAP_HITTER_DEFAULT: dict[int, float] = {
    17: 20, 18: 17, 19: 13, 20: 12, 21: 10, 22: 6, 23: 4, 24: 3, 25: 3,
}
EXPECTED_GAP_PITCHER_DEFAULT: dict[int, float] = {
    17: 18, 18: 15, 19: 13, 20: 11, 21: 9, 22: 7, 23: 5, 24: 4, 25: 3,
}

# Bust discount targets by age (target_product / closure).
_TARGET_PRODUCT: dict[int, float] = {
    17: 0.47, 18: 0.47, 19: 0.46, 20: 0.48, 21: 0.52,
    22: 0.50, 23: 0.53, 24: 0.30, 25: 0.30,
}


# ---------------------------------------------------------------------------
# Development weight
# ---------------------------------------------------------------------------

def age_development_mult(
    age: int | float,
    is_pitcher: bool = False,
    runway_table: Optional[dict[int, float]] = None,
) -> float:
    """Multiplier on dev_weight reflecting remaining development runway.

    Returns 1.0 for age ≤ 21, decays based on empirical gap-closure rates.
    Linearly interpolates between defined age points.

    Args:
        age: Player's current age.
        is_pitcher: Pitchers retain more runway at each age.
        runway_table: Override table (from calibrated model_weights).
    """
    if runway_table is None:
        runway_table = AGE_RUNWAY_PITCHER_DEFAULT if is_pitcher else AGE_RUNWAY_HITTER_DEFAULT

    if age <= 17:
        return runway_table.get(17, 1.58)
    if age >= 26:
        return runway_table.get(26, 0.20)
    lo = int(age)
    hi = lo + 1
    frac = age - lo
    lo_val = runway_table.get(lo, 0.20)
    hi_val = runway_table.get(hi, 0.20)
    return lo_val * (1 - frac) + hi_val * frac


def dev_weight(
    age: int,
    norm_age: int,
    level: Optional[str] = None,
    is_pitcher: bool = False,
    runway_table: Optional[dict[int, float]] = None,
) -> float:
    """Development weight: how much to blend ceiling vs current based on age vs level norm.

    Args:
        age: Player's current age.
        norm_age: Expected age at this level for on-track prospect.
        level: Level string (e.g., "usl", "dsl", "intl").
        is_pitcher: Whether this is a pitcher.
        runway_table: Override age runway table.

    Returns:
        Float weight in [0, 1], higher = more weight on ceiling.
    """
    diff = norm_age - age
    if diff >= 3:
        w = 0.55 if age <= 17 else 0.65
    elif diff >= 2:
        w = 0.45 if age <= 17 else 0.60
    elif diff >= 1:
        w = 0.40 if age <= 17 else 0.50
    elif diff >= -1:
        w = 0.35
    elif diff >= -2:
        w = 0.20
    else:
        w = 0.10

    low_level = level and level.lower().replace(" ", "-") in (
        "usl", "dsl", "intl", "rookie", "a-short"
    )
    if low_level:
        w += 0.10
        if level and level.lower().replace(" ", "-") in ("usl", "dsl", "intl", "rookie"):
            w = min(w, 0.55)

    # Apply empirical age decay for prospects past peak development age
    if age > 21:
        w *= age_development_mult(age, is_pitcher=is_pitcher, runway_table=runway_table)

    return w


# ---------------------------------------------------------------------------
# FV grade calculation
# ---------------------------------------------------------------------------

def _interp_table(table: dict[int, float], age: int | float) -> float:
    """Interpolate from an age-keyed table."""
    if age <= 17:
        return table.get(17, table[min(table.keys())])
    if age >= 25:
        return table.get(25, table[max(table.keys())])
    lo = int(age)
    hi = lo + 1
    frac = age - lo
    lo_val = table.get(lo, 0.38)
    hi_val = table.get(hi, 0.38)
    return lo_val * (1 - frac) + hi_val * frac


def calc_fv(
    ovr: int,
    pot: int,
    age: int,
    bucket: str,
    norm_age: int,
    is_pitcher: bool = False,
    accuracy: str = "N",
    contact_l: int = 0,
    contact_r: int = 0,
    stuff_l: int = 0,
    stuff_r: int = 0,
    offensive_ceiling: Optional[int] = None,
    stat_risk_modifier: float = 0.0,
    work_ethic: str = "N",
    intelligence: str = "N",
    gap_closure_table: Optional[dict[int, float]] = None,
    expected_gap_table: Optional[dict[int, float]] = None,
    run_anchor: Optional[dict[str, float]] = None,
    comp_mapping: Optional[dict[str, float]] = None,
    weights: Any = None,
    fv_strength: float = 1.0,
) -> tuple[int, str, float]:
    """Compute FV grade and risk label for a prospect.

    WAR-based FV with risk label. FV reflects ceiling quality relative to
    MLB position median. Risk captures development probability.

    Args:
        ovr: Current composite score (20-80).
        pot: True ceiling score (20-80).
        age: Player's current age.
        bucket: Positional bucket ("SS", "SP", etc.).
        norm_age: Expected age at current level.
        is_pitcher: Whether this is a pitcher.
        accuracy: Scouting accuracy code ("L" = low).
        contact_l/contact_r: Split contact ratings (for platoon penalty).
        stuff_l/stuff_r: Split stuff ratings (for platoon penalty).
        offensive_ceiling: Offensive ceiling (caps FV for bat-limited players).
        stat_risk_modifier: MiLB stat-based risk adjustment.
        work_ethic: Work ethic code.
        intelligence: Intelligence code.
        gap_closure_table: Override gap closure rates (from model_weights).
        expected_gap_table: Override expected gap table (from model_weights).

    Returns:
        Tuple of (fv_grade: int, risk: str, fv_continuous: float).
        fv_grade is rounded to nearest 5 (20-80).
        risk is "Low", "Medium", "High", or "Extreme".
        fv_continuous is the pre-rounding value for surplus interpolation.
    """
    # Apply RP pot discount
    effective_pot = pot
    if bucket == "RP":
        effective_pot = round(pot * RP_POT_DISCOUNT)

    gap = max(0, effective_pot - ovr)

    # --- Ceiling-anchored WAR path (hitters) ---------------------------------
    # When run-space calibration is available and this is a hitter, derive the FV
    # grade by PROJECTING FUTURE WAR FROM THE CEILING (potential ratings) and
    # discounting it by the probability of realizing that ceiling, then reading
    # the grade off the FV→WAR ladder. This replaces the composite-anchored
    # formula below (which graded off current composite and let bench/role
    # players ride a modest ceiling to FV 55). Design: docs/fv_war_pipeline_diagnosis.md.
    ceiling_anchored = (
        not is_pitcher
        and bucket not in ("SP", "RP")
        and run_anchor is not None
        and comp_mapping is not None
        and ("slope" in comp_mapping or comp_mapping.get("comp_sd", 0) > 0)
    )
    if ceiling_anchored:
        from statsplusplus.evaluation.facet_runs import (
            runs_to_war, saturate_war, composite_to_runs,
        )
        from statsplusplus.evaluation.surplus import fv_from_peak_war

        # The ceiling_anchored guard already proves these are populated.
        assert comp_mapping is not None and run_anchor is not None
        _cm: dict[str, Any] = comp_mapping
        _anchor: dict[str, float] = run_anchor

        # Future WAR from POTENTIAL (ceiling), tail-saturated to realistic caps.
        # composite_to_runs inverts whichever mapping shape is calibrated
        # (hybrid per-position affine, or the legacy z-map) back to runs.
        _ceil_runs = composite_to_runs(float(pot), _cm, bucket)
        ceiling_war = saturate_war(runs_to_war(_ceil_runs, _anchor), _anchor)

        # p(develops): probability of realizing the ceiling. Near-maxed (small
        # gap) → ~1.0 (what you see is what you get). Else empirical gap-closure
        # rate for age, scaled by fv_strength (upside-friendliness).
        if gap <= 2:
            p_dev = 1.0
        else:
            closure_tbl = gap_closure_table or GAP_CLOSURE_HITTER_DEFAULT
            p_dev = max(0.0, min(1.0, _interp_table(closure_tbl, age) * fv_strength))

        # Bust fallback: a prospect who doesn't develop lands ~replacement
        # (sent down / washes out), NOT a negative full-season regular.
        fallback_war = -0.3
        expected_war = p_dev * ceiling_war + (1.0 - p_dev) * fallback_war

        war_floor = _anchor.get("sat_bot")
        fv = fv_from_peak_war(expected_war, bucket, weights, war_floor=war_floor)
    else:
        # --- Legacy composite-anchored path (pitchers + no-run-space fallback) ---
        # Compute expected peak composite
        if gap <= 3:
            fv = float(ovr)
        else:
            closure_tbl = gap_closure_table or (
                GAP_CLOSURE_PITCHER_DEFAULT if is_pitcher else GAP_CLOSURE_HITTER_DEFAULT
            )
            closure = _interp_table(closure_tbl, age)

            # Bust discount: derived from target product / closure
            age_key = max(17, min(25, int(age)))
            target = _TARGET_PRODUCT.get(age_key, 0.47)
            bust = min(0.85, target / closure) if closure > 0 else 0.55

            peak = ovr + gap * closure * bust
            ceil_weight = max(0.0, min(0.5, (effective_pot - 50) / 30.0))
            fv = peak * (1.0 - ceil_weight) + effective_pot * ceil_weight

    # Accuracy penalty
    if accuracy == "L":
        fv -= 5

    # Platoon split penalty
    if is_pitcher:
        if stuff_l and stuff_r:
            g = abs(stuff_l - stuff_r)
            weak = min(stuff_l, stuff_r)
            if weak <= 25 and g >= 10:
                fv -= 5
    else:
        if contact_l and contact_r:
            g = abs(contact_l - contact_r)
            weak = min(contact_l, contact_r)
            if weak <= 25 and g >= 10:
                fv -= 5

    # RP cap
    if bucket == "RP":
        fv = min(fv, 55)

    if not ceiling_anchored:
        # Composite-scale caps — only meaningful for the legacy composite path.
        # Ceiling cap: FV cannot exceed true_ceiling - 3
        fv = min(fv, effective_pot - 3)
        # Offensive ceiling cap for bat-limited hitters
        if offensive_ceiling is not None and offensive_ceiling < 45 and bucket not in ("SP", "RP"):
            fv = min(fv, 50)

    fv = max(20.0, fv)

    # Snap to nearest 5. fv_continuous already discounts the unrealized ceiling
    # (via the peak = ovr + gap*closure*bust blend and ceil_weight above), so we
    # round consistently regardless of gap. Previously high-gap players were
    # FLOORED (int()) while near-ceiling players were ROUNDED — a second penalty
    # on upside that produced a full-grade cliff between two players 0.1 apart in
    # continuous FV (e.g. a near-ceiling glove-first SS at 54.0 -> 55 vs a
    # high-upside COF at 53.96 -> 50). The gap discount belongs in the continuous
    # value, not the rounding step.
    fv_grade = round(fv / 5) * 5

    # Continuous FV for surplus interpolation
    fv_continuous = fv

    # --- Risk Label ---
    gap = max(0, effective_pot - ovr)
    closure_tbl = gap_closure_table or (
        GAP_CLOSURE_PITCHER_DEFAULT if is_pitcher else GAP_CLOSURE_HITTER_DEFAULT
    )
    closure = _interp_table(closure_tbl, age)

    if age <= 19:
        base_discount = 0.30
    elif age <= 21:
        base_discount = 0.35
    elif age <= 23:
        base_discount = 0.45
    else:
        base_discount = 0.60

    eg_table = expected_gap_table or (
        EXPECTED_GAP_PITCHER_DEFAULT if is_pitcher else EXPECTED_GAP_HITTER_DEFAULT
    )
    expected_gap = eg_table.get(max(17, min(25, int(age))), 5)
    excess_gap = max(0, gap - expected_gap)
    if excess_gap >= 15:
        gap_scale = 0.70
    elif excess_gap >= 8:
        gap_scale = 0.85
    else:
        gap_scale = 1.00

    char_adj = 0.0
    if work_ethic in ("H", "VH"):
        char_adj += 0.03
    elif work_ethic == "L":
        char_adj -= 0.03
    if intelligence in ("H", "VH"):
        char_adj += 0.02
    elif intelligence == "L":
        char_adj -= 0.02

    dev_confidence = closure * base_discount * gap_scale + char_adj
    dev_confidence = max(0.0, min(1.0, dev_confidence))

    # MiLB stat-based risk modifier
    if stat_risk_modifier:
        dev_confidence = max(0.0, min(1.0, dev_confidence + stat_risk_modifier))

    if gap < 3:
        risk = "Low"
    elif dev_confidence >= 0.40:
        risk = "Low"
    elif dev_confidence >= 0.25:
        risk = "Medium"
    elif dev_confidence >= 0.15:
        risk = "High"
    else:
        risk = "Extreme"

    return fv_grade, risk, fv_continuous


def compute_performance_adjusted_ceiling(
    true_ceiling: float,
    stat_2080: float,
    player_age: int,
    norm_age: int,
    effective_pa: float,
    tool_only_score: float,
) -> int:
    """Compute performance-adjusted ceiling from MiLB stat signal.

    Adjusts the ceiling up or down based on how the player's level-relative
    production compares to their pure tool composite. Young players get more
    credit for outperformance; older players get penalized more for
    underperformance.

    Args:
        true_ceiling: Ceiling score on 20-80 scale.
        stat_2080: Level-relative production converted to 20-80 scale.
        player_age: Player's current age.
        norm_age: Expected age for a typical player at this level.
        effective_pa: Level-discounted effective PA (sample size).
        tool_only_score: Pure tool composite score.

    Returns:
        Adjusted ceiling, clamped to 20-80.
    """
    if effective_pa < 30 or true_ceiling <= 0:
        return int(true_ceiling)

    signal = (stat_2080 - tool_only_score) / 30.0
    signal = max(-1.0, min(1.0, signal))
    age_context = norm_age - player_age

    if signal > 0:
        age_mult = 1.0 + age_context * 0.15 if age_context > 0 else max(0.4, 1.0 + age_context * 0.20)
    else:
        age_mult = max(0.25, 1.0 - age_context * 0.25) if age_context > 0 else 1.0 + abs(age_context) * 0.15

    sample_confidence = min(1.0, (effective_pa - 30) / 270.0)
    max_adjustment = 6.0
    adjustment = signal * age_mult * sample_confidence * max_adjustment
    adjustment = max(-max_adjustment, min(max_adjustment, adjustment))

    result = true_ceiling + round(adjustment)
    return max(20, min(80, int(result)))


def compute_stat_risk_modifier(
    stat_2080: float,
    player_age: int,
    norm_age: int,
    effective_pa: float,
    tool_only_score: float,
) -> float:
    """Compute risk modifier based on MiLB stat performance.

    Returns a value between -0.12 and +0.12 that adjusts the development
    confidence used in risk label assignment. Positive = reduced risk (player
    is outperforming tools), negative = increased risk.

    Args:
        stat_2080: Level-relative production on 20-80 scale.
        player_age: Player's current age.
        norm_age: Expected age for a typical player at this level.
        effective_pa: Level-discounted effective PA.
        tool_only_score: Pure tool composite score.

    Returns:
        Risk modifier in [-0.12, +0.12].
    """
    if effective_pa < 50:
        return 0.0

    perf_delta = stat_2080 - tool_only_score
    normalized = max(-1.0, min(1.0, perf_delta / 15.0))
    age_context = norm_age - player_age
    sample_factor = min(1.0, (effective_pa - 50) / 200.0)
    base_mod = normalized * 0.08 * sample_factor

    if normalized > 0 and age_context > 0:
        base_mod *= (1.0 + age_context * 0.25)
    elif normalized < 0 and age_context < 0:
        base_mod *= (1.0 + abs(age_context) * 0.20)
    elif normalized < 0 and age_context > 0:
        base_mod *= max(0.2, 1.0 - age_context * 0.30)

    return max(-0.12, min(0.12, base_mod))


# ---------------------------------------------------------------------------
# Positional access premium
# ---------------------------------------------------------------------------

POSITIONAL_ACCESS: dict[str, dict[str, float]] = {
    "SS": {"access_threshold": 50, "base_premium": 2.0, "offense_scale": 0.06},
    "C":  {"access_threshold": 50, "base_premium": 1.5, "offense_scale": 0.05},
    "CF": {"access_threshold": 50, "base_premium": 1.5, "offense_scale": 0.05},
}


def positional_access_premium(
    bucket: str,
    offensive_grade: float,
    defensive_value: float,
    access_threshold: float = 50,
) -> float:
    """Compute positional value premium for premium defensive positions.

    Premium positions (SS, C, CF) get a bonus when the player has sufficient
    defensive ability to stick at the position AND elite offensive production.

    Args:
        bucket: Positional bucket.
        offensive_grade: Offensive composite grade (20-80 scale).
        defensive_value: Defensive rating at the position.
        access_threshold: Minimum defensive value to qualify for premium.

    Returns:
        Premium value (0-5 range, typically 1-3 for qualifying players).
    """
    params = POSITIONAL_ACCESS.get(bucket)
    if params is None:
        return 0.0
    if defensive_value < access_threshold:
        return 0.0
    base_premium = params["base_premium"]
    offense_scale = params["offense_scale"]
    return base_premium + (offensive_grade - 40) * offense_scale


# ---------------------------------------------------------------------------
# Dict-based adapter (legacy calling convention)
# ---------------------------------------------------------------------------


def calc_fv_from_dict(
    p: dict,
    scale: str = "1-100",
    league_dir: "Path | None" = None,
) -> tuple[int, str]:
    """Compute FV for a prospect from a player dict (legacy interface).

    This is a convenience wrapper around calc_fv() for callers that work
    with pre-built player dicts. Mutates p by setting p["_fv_continuous"].

    Args:
        p: Player dict with keys: Ovr, Pot, Age, _bucket, _is_pitcher,
           Acc, Cntct_L, Cntct_R, Stf_L, Stf_R, _offensive_ceiling,
           _stat_risk_modifier, WrkEthic, Int, _norm_age.
        scale: Ratings scale for norm_floor.
        league_dir: League directory for loading calibrated dev curves.

    Returns:
        (fv_grade, risk_label) tuple.
    """
    from pathlib import Path
    from statsplusplus.config.ratings import norm_floor as _nf

    # Load calibrated dev curves
    gap_closure_h = dict(GAP_CLOSURE_HITTER_DEFAULT)
    gap_closure_p = dict(GAP_CLOSURE_PITCHER_DEFAULT)
    expected_gap_h = dict(EXPECTED_GAP_HITTER_DEFAULT)
    expected_gap_p = dict(EXPECTED_GAP_PITCHER_DEFAULT)

    if league_dir is not None:
        import json
        mw_path = Path(league_dir) / "config" / "model_weights.json"
        if mw_path.exists():
            try:
                w = json.loads(mw_path.read_text())
                for key, target in [
                    ("gap_closure_hitter", gap_closure_h),
                    ("gap_closure_pitcher", gap_closure_p),
                    ("expected_gap_hitter", expected_gap_h),
                    ("expected_gap_pitcher", expected_gap_p),
                ]:
                    raw = w.get(key)
                    if isinstance(raw, dict):
                        target.clear()
                        target.update({int(k): v for k, v in raw.items()})
                # Hand-set per-league overrides live in MODEL_PARAMS (which
                # calibrate() preserves) so a recalibration can't wipe them:
                # e.g. GAP_CLOSURE_HITTER_OVERRIDE = {"22": 0.287, ...}.
                _mp = w.get("MODEL_PARAMS") or {}
                for key, target in [
                    ("GAP_CLOSURE_HITTER_OVERRIDE", gap_closure_h),
                    ("GAP_CLOSURE_PITCHER_OVERRIDE", gap_closure_p),
                ]:
                    raw = _mp.get(key)
                    if isinstance(raw, dict) and raw:
                        target.clear()
                        target.update({int(k): float(v) for k, v in raw.items()})
            except (json.JSONDecodeError, OSError):
                pass

    # Load run-space anchor + comp_mapping (for the ceiling-anchored hitter path)
    # and the calibrated FV→WAR tables (ModelWeights) for ladder inversion.
    run_anchor = None
    comp_mapping = None
    _weights = None
    if league_dir is not None:
        import json
        tw_path = Path(league_dir) / "config" / "tool_weights.json"
        if tw_path.exists():
            try:
                _rs = json.loads(tw_path.read_text()).get("run_space", {})
                run_anchor = _rs.get("anchor")
                comp_mapping = _rs.get("comp_mapping")
            except (json.JSONDecodeError, OSError):
                pass
        try:
            from statsplusplus.evaluation.constants import load_model_weights
            _weights = load_model_weights(Path(league_dir))
        except Exception:
            _weights = None

    # Per-league switch (MODEL_PARAMS "FV_CEILING_ANCHORED", default on). Off →
    # legacy composite-anchored FV for hitters. PPL is off pending recalibration:
    # its compressed composite->runs mapping (comp_sd ~3.4) turns a 55 ceiling
    # into ~4.6 WAR, which graded ~250 hitter prospects FV 60+ (eMLB, sd ~5.4,
    # produces a normal pyramid under the same model).
    if _weights is not None and not int(_weights.get_param("FV_CEILING_ANCHORED", 1)):
        run_anchor = None
        comp_mapping = None

    ovr = p.get("Ovr") or 0
    pot = p.get("Pot") or 0
    age = p["Age"]
    bucket = p["_bucket"]
    is_pitcher = bool(p.get("_is_pitcher"))

    accuracy = p.get("Acc", "N")
    contact_l = _nf(p.get("Cntct_L", 0), scale)
    contact_r = _nf(p.get("Cntct_R", 0), scale)
    stuff_l = _nf(p.get("Stf_L", 0), scale)
    stuff_r = _nf(p.get("Stf_R", 0), scale)
    offensive_ceiling = p.get("_offensive_ceiling")
    stat_risk_modifier = p.get("_stat_risk_modifier", 0.0)
    work_ethic = p.get("WrkEthic", "N")
    intelligence = p.get("Int", "N")
    norm_age = p.get("_norm_age", 22)

    gap_closure = gap_closure_p if is_pitcher else gap_closure_h
    expected_gap = expected_gap_p if is_pitcher else expected_gap_h

    fv_grade, risk, fv_continuous = calc_fv(
        ovr=ovr, pot=pot, age=age, bucket=bucket, norm_age=norm_age,
        is_pitcher=is_pitcher, accuracy=accuracy,
        contact_l=contact_l, contact_r=contact_r,
        stuff_l=stuff_l, stuff_r=stuff_r,
        offensive_ceiling=offensive_ceiling,
        stat_risk_modifier=stat_risk_modifier,
        work_ethic=work_ethic, intelligence=intelligence,
        gap_closure_table=gap_closure,
        expected_gap_table=expected_gap,
        run_anchor=run_anchor,
        comp_mapping=comp_mapping,
        weights=_weights,
        fv_strength=FV_CEILING_STRENGTH,
    )

    # Mutate player dict (legacy behavior)
    p["_fv_continuous"] = fv_continuous

    return fv_grade, risk

```


## B.2 ceiling.py — potential / ceiling scores (complete file)
```python
"""Ceiling score computation.

Pure functions for computing a player's ceiling (projected peak ability)
from potential tool ratings. No DB access, no global state.

Public API:
    compute_ceiling(potential_tools, weights, composite, ...) -> int
    compute_true_ceiling(potential_tools, weights, composite, ...) -> int
    compute_component_ceilings(potential_tools, weights, current_components, ...) -> dict
"""

from __future__ import annotations

from typing import Any, Optional

from statsplusplus.evaluation.composite import (
    compute_composite_hitter,
    compute_composite_pitcher,
    compute_offensive_grade,
    compute_baserunning_value,
    compute_defensive_value,
)


# ---------------------------------------------------------------------------
# Age-weighted potential blend
# ---------------------------------------------------------------------------

def _potential_weight(age: int) -> float:
    """Compute the potential-vs-current weight based on age.

    Younger players weight potential tools more heavily (reflecting upside),
    while veterans weight current composite more heavily.

    Ramps from 0.95 (age 16) to 0.30 (age 30+).
    """
    return max(0.30, min(0.95, 1.0 - (age - 16) * 0.05))


# ---------------------------------------------------------------------------
# Ceiling score
# ---------------------------------------------------------------------------

def compute_ceiling(
    potential_tools: dict[str, float | int | None],
    weights: dict[str, float],
    composite_score: int,
    accuracy: str = "A",
    work_ethic: str = "N",
    defense: Optional[dict[str, float | int | None]] = None,
    def_weights: Optional[dict[str, float]] = None,
    is_pitcher: bool = False,
    arsenal: Optional[dict[str, float | int]] = None,
    stamina: int = 50,
    role: str = "SP",
    age: int = 25,
    ratings_scale: str = "1-100",
    transforms: dict[str, list[float]] | None = None,
    run_space: dict | None = None,
    bucket: str | None = None,
    positional_models: dict | None = None,
    neutral_position: bool = False,
) -> int:
    """Compute Ceiling_Score from potential tool ratings.

    Uses the same positional weight formula as the composite, applied to
    potential ratings instead of current. The ceiling is age-weighted:
    younger players weight potential more heavily.

    Args:
        potential_tools: Potential tool ratings (20-80 scale).
        weights: Positional weight profile (same as composite).
        composite_score: Player's current Composite_Score (floor).
        accuracy: Scouting accuracy ("A" normal, "L" low).
        work_ethic: Work ethic code ("H"/"VH" high, "L" low, "N" normal).
        defense: Defensive potential tool ratings (hitters only).
        def_weights: Defensive weight profile (hitters only).
        is_pitcher: Whether the player is a pitcher.
        arsenal: Pitch arsenal dict (pitchers only).
        stamina: Stamina rating (pitchers only).
        role: Pitcher role "SP" or "RP" (pitchers only).
        age: Player's current age (for age-weighted blend).
        ratings_scale: League ratings scale for peak bonus cap.
        run_space/bucket/positional_models: run-space calibration — when present
            for a hitter, the raw ceiling is computed via the run-space spine on
            POTENTIAL tools (consistent with the run-space composite; a maxed
            player's ceiling then ≈ his composite, no phantom grade-space upside).

    Returns:
        Integer ceiling score in [20, 80], never below composite_score.
    """
    # Compute raw potential composite
    if is_pitcher:
        raw_ceiling = compute_composite_pitcher(
            potential_tools, weights, arsenal or {}, stamina, role, transforms,
        )
    elif run_space and bucket:
        # Run-space ceiling: potential tools through the same run spine as the
        # composite (no observed blend — ceiling is the tool-projected peak).
        # Skip the grade-space peak-tool bonus below (a grade-space compensator).
        raw_ceiling = compute_composite_hitter(
            potential_tools, weights, defense or {}, def_weights or {}, transforms,
            run_space=run_space, bucket=bucket, positional_models=positional_models,
            neutral_position=neutral_position,
        )
        pot_weight = _potential_weight(age)
        raw_ceiling = round(raw_ceiling * pot_weight + composite_score * (1.0 - pot_weight))
        return max(20, min(80, max(raw_ceiling, composite_score)))
    else:
        raw_ceiling = compute_composite_hitter(
            potential_tools, weights, defense or {}, def_weights or {}, transforms,
        )

    # Peak tool bonus: rewards uneven profiles with elite carrying tools.
    # +1 per potential tool point above 60, capped.
    if is_pitcher:
        ceiling_tools = [
            potential_tools.get(k) or 0
            for k in ("stuff", "movement", "control")
        ]
        if role == "SP" and stamina >= 55:
            ceiling_tools.append(min(stamina, 65))
    else:
        ceiling_tools = [
            potential_tools.get(k) or 0
            for k in ("contact", "gap", "power", "eye")
        ]

    peak_bonus = sum(max(0, int(t) - 60) for t in ceiling_tools)
    # Scale-aware cap: 1-100 scale normalizes higher so bonus accumulates faster
    peak_cap = 10 if ratings_scale == "1-100" else 15
    raw_ceiling += min(peak_bonus, peak_cap)

    # Age-weighted blend
    pot_weight = _potential_weight(age)
    raw_ceiling = round(raw_ceiling * pot_weight + composite_score * (1.0 - pot_weight))

    # Floor: never below composite
    raw_ceiling = max(raw_ceiling, composite_score)

    return max(20, min(80, raw_ceiling))


def compute_true_ceiling(
    potential_tools: dict[str, float | int | None],
    weights: dict[str, float],
    composite_score: int,
    accuracy: str = "A",
    work_ethic: str = "N",
    defense: Optional[dict[str, float | int | None]] = None,
    def_weights: Optional[dict[str, float]] = None,
    is_pitcher: bool = False,
    arsenal: Optional[dict[str, float | int]] = None,
    stamina: int = 50,
    role: str = "SP",
    transforms: dict[str, list[float]] | None = None,
    run_space: dict | None = None,
    bucket: str | None = None,
    positional_models: dict | None = None,
    neutral_position: bool = False,
) -> int:
    """Compute the true ceiling from potential tools with no age blend.

    Pure potential-driven score: what happens if every tool reaches its
    potential rating. No peak tool bonus (already reflected in uncompressed
    potential composite). No age-weighted blend.

    Use compute_ceiling() for the age-blended projected score.

    Returns:
        Integer ceiling score in [20, 80], never below composite_score.
    """
    if is_pitcher:
        raw = compute_composite_pitcher(
            potential_tools, weights, arsenal or {}, stamina, role, transforms,
        )
    elif run_space and bucket:
        raw = compute_composite_hitter(
            potential_tools, weights, defense or {}, def_weights or {}, transforms,
            run_space=run_space, bucket=bucket, positional_models=positional_models,
            neutral_position=neutral_position,
        )
    else:
        raw = compute_composite_hitter(
            potential_tools, weights, defense or {}, def_weights or {}, transforms,
        )

    # Floor: never below composite
    raw = max(raw, composite_score)
    return max(20, min(80, raw))


def compute_component_ceilings(
    potential_tools: dict[str, float | int | None],
    weights: dict[str, float],
    current_components: dict[str, Optional[int]],
    defense: Optional[dict[str, float | int | None]] = None,
    def_weights: Optional[dict[str, float]] = None,
    is_pitcher: bool = False,
    arsenal: Optional[dict[str, float | int]] = None,
    stamina: int = 50,
    role: str = "SP",
    age: int = 25,
    ct_config: Optional[dict[str, Any]] = None,
    position: str = "",
    transforms: dict[str, list[float]] | None = None,
) -> dict[str, Any]:
    """Compute component-level ceilings from potential tool ratings.

    Applies the same component formulas to potential tools, with age-weighted
    blend per-component (each floored at its current value).

    When ct_config is provided with a non-empty position, carrying tool bonus
    is computed on potential offensive tools and added to the offensive ceiling.

    Args:
        potential_tools: Potential tool ratings (20-80 scale).
        weights: Positional weight profile.
        current_components: Current component scores (offensive_grade,
            baserunning_value, defensive_value, durability_score).
        defense: Defensive potential tool ratings (hitters only).
        def_weights: Defensive weight profile (hitters only).
        is_pitcher: Whether the player is a pitcher.
        arsenal: Pitch arsenal dict (pitchers only).
        stamina: Stamina rating (pitchers only).
        role: "SP" or "RP".
        age: Player's current age.
        ct_config: Carrying tool config dict (optional).
        position: Position bucket for carrying tool bonus.

    Returns:
        Dict with offensive_ceiling, baserunning_ceiling, defensive_ceiling,
        ceiling_carrying_tool_bonus, ceiling_carrying_tool_breakdown.
    """
    pot_weight = _potential_weight(age)

    result: dict[str, Any] = {
        "offensive_ceiling": None,
        "baserunning_ceiling": None,
        "defensive_ceiling": None,
        "ceiling_carrying_tool_bonus": 0.0,
        "ceiling_carrying_tool_breakdown": [],
    }

    if is_pitcher:
        raw_pitching = compute_composite_pitcher(
            potential_tools, weights, arsenal or {}, stamina, role, transforms,
        )
        current_off = current_components.get("offensive_grade")
        if current_off is not None:
            blended = round(raw_pitching * pot_weight + current_off * (1.0 - pot_weight))
            blended = max(blended, current_off)
            result["offensive_ceiling"] = max(20, min(80, blended))
        else:
            result["offensive_ceiling"] = max(20, min(80, raw_pitching))
    else:
        raw_offensive = compute_offensive_grade(potential_tools, weights, transforms)
        raw_baserunning = compute_baserunning_value(potential_tools, weights, transforms)
        raw_defensive = compute_defensive_value(defense or {}, def_weights or {})

        # Carrying tool bonus on potential offensive ceiling
        ceiling_ct_bonus = 0.0
        ceiling_ct_breakdown: list[dict[str, Any]] = []
        if raw_offensive is not None and ct_config and position:
            from statsplusplus.evaluation.carrying_tools import compute_carrying_tool_bonus
            ceiling_ct_bonus, ceiling_ct_breakdown = compute_carrying_tool_bonus(
                potential_tools, position, ct_config,
            )

        raw_off_adjusted: float = float(raw_offensive) + ceiling_ct_bonus if raw_offensive is not None else 0.0

        result["ceiling_carrying_tool_bonus"] = ceiling_ct_bonus
        result["ceiling_carrying_tool_breakdown"] = ceiling_ct_breakdown

        current_off = current_components.get("offensive_grade")
        current_br = current_components.get("baserunning_value")
        current_def = current_components.get("defensive_value")

        # Offensive ceiling
        if raw_offensive is not None:
            if current_off is not None:
                blended = round(raw_off_adjusted * pot_weight + current_off * (1.0 - pot_weight))
                blended = max(blended, current_off)
                result["offensive_ceiling"] = max(20, min(80, blended))
            else:
                result["offensive_ceiling"] = max(20, min(80, round(raw_off_adjusted)))

        # Baserunning ceiling
        if raw_baserunning is not None:
            if current_br is not None:
                blended = round(raw_baserunning * pot_weight + current_br * (1.0 - pot_weight))
                blended = max(blended, current_br)
                result["baserunning_ceiling"] = max(20, min(80, blended))
            else:
                result["baserunning_ceiling"] = max(20, min(80, raw_baserunning))

        # Defensive ceiling
        if raw_defensive is not None:
            if current_def is not None:
                blended = round(raw_defensive * pot_weight + current_def * (1.0 - pot_weight))
                blended = max(blended, current_def)
                result["defensive_ceiling"] = max(20, min(80, blended))
            else:
                result["defensive_ceiling"] = max(20, min(80, raw_defensive))

    return result

```


## B.3 composite.py — current-value grade (selected functions)
```python
OFFENSIVE_TOOL_KEYS: tuple[str, ...] = ("contact", "gap", "power", "eye")
BASERUNNING_TOOL_KEYS: tuple[str, ...] = ("speed", "steal", "stl_rt")
PITCHER_TOOL_KEYS: tuple[str, ...] = ("stuff", "movement", "control", "hra", "pbabip")
PITCHER_CORE_KEYS: tuple[str, ...] = ("stuff", "movement", "control")
RANGE_BUMP_MILD_RUNS = 6.0
RANGE_BUMP_BIG_RUNS = 12.0
_RANGE_BUMP_THRESHOLDS = {"OF": (60, 70), "IF": (65, 70)}
_OF_BUCKETS = ("CF", "COF", "LF", "RF")
_IF_BUCKETS = ("SS", "2B", "3B")


def tool_transform(val: float) -> float:
    """Apply non-linear piecewise transformation to a tool rating.

    Linear in the middle (40-60), with 1.5× penalty below 40 and 1.3× bonus
    above 60. Matches empirical marginal WAR data.

    Args:
        val: Tool value on the 20-80 scale.

    Returns:
        Transformed value on the 20-80 scale.
    """
    if val >= TOOL_TRANSFORM_HIGH_THRESHOLD:
        return TOOL_TRANSFORM_HIGH_THRESHOLD + (val - TOOL_TRANSFORM_HIGH_THRESHOLD) * TOOL_TRANSFORM_HIGH_BONUS
    elif val <= TOOL_TRANSFORM_LOW_THRESHOLD:
        return TOOL_TRANSFORM_LOW_THRESHOLD - (TOOL_TRANSFORM_LOW_THRESHOLD - val) * TOOL_TRANSFORM_LOW_PENALTY
    else:
        return float(val)


def apply_tool_transform(
    val: float,
    curve: list[float] | None,
    anchors: tuple[float, ...] = TOOL_TRANSFORM_ANCHORS,
) -> float:
    """Apply a per-tool transform curve to a rating via linear interpolation.

    ``curve`` is a list of effective-rating *deltas* at ``anchors`` (as produced
    by :func:`derive_tool_transform`). The returned value is
    ``val + interpolated_delta``. When ``curve`` is ``None`` the function falls
    back to the global :func:`tool_transform`, preserving legacy behavior for
    leagues without calibrated curves.
    """
    if not curve:
        return tool_transform(val)
    # Clamp to the anchor range, then linearly interpolate the delta.
    if val <= anchors[0]:
        delta = curve[0]
    elif val >= anchors[-1]:
        delta = curve[-1]
    else:
        delta = curve[-1]
        for i in range(len(anchors) - 1):
            a0, a1 = anchors[i], anchors[i + 1]
            if a0 <= val <= a1:
                span = a1 - a0
                frac = (val - a0) / span if span else 0.0
                delta = curve[i] + (curve[i + 1] - curve[i]) * frac
                break
    return float(val) + delta


def sub_mlb_floor_penalty(tools: dict[str, float | int | None]) -> float:
    """Compute composite penalty for tools below the MLB floor (35).

    Players with tools below 35 underperform their OVR-predicted WAR.
    Penalty captures the nonlinear cost of a disqualifying weakness.

    Args:
        tools: Tool ratings on the 20-80 scale.

    Returns:
        Penalty as a positive float to subtract from the composite.
    """
    penalty = 0.0
    for val in tools.values():
        if val is not None and val < MLB_TOOL_FLOOR:
            penalty += (MLB_TOOL_FLOOR - val) * FLOOR_PENALTY_RATE
    return penalty


def _compensated_transform(val: float, compensators: list[tuple[float, float]]) -> float:
    """Transform a tool with compensation that pulls below-average toward 50.

    Applies tool_transform first, then pulls toward 50 proportionally to
    compensating tools' surplus above 50. Capped at 0.75 pull fraction.
    """
    transformed = tool_transform(val)
    deficit = 50.0 - transformed
    if deficit <= 0:
        return transformed

    pull_fraction = 0.0
    for comp_val, strength in compensators:
        if comp_val > 50.0:
            surplus = comp_val - 50.0
            pull_fraction += surplus * strength

    pull_fraction = min(pull_fraction, 0.75)
    return transformed + deficit * pull_fraction


def _apply_hitter_compensation(tools: dict[str, float | int | None]) -> dict[str, float | int | None]:
    """Apply compensation for hitter tools below average."""
    cnt = float(tools.get("contact") or 0)
    pow_ = float(tools.get("power") or 0)
    eye = float(tools.get("eye") or 0)

    effective: dict[str, float | int | None] = dict(tools)

    if pow_ < 50 and pow_ > 0:
        compensators = []
        if cnt > 50:
            compensators.append((cnt, 0.020))
        if eye > 50:
            compensators.append((eye, 0.012))
        if compensators:
            effective["_power_transformed"] = _compensated_transform(pow_, compensators)

    if eye < 50 and eye > 0:
        compensators = []
        if cnt > 50:
            compensators.append((cnt, 0.020))
        if compensators:
            effective["_eye_transformed"] = _compensated_transform(eye, compensators)

    return effective


def _apply_pitcher_compensation(tools: dict[str, float | int | None]) -> dict[str, float | int | None]:
    """Apply compensation for pitcher tools below average."""
    stf = float(tools.get("stuff") or 0)
    mov = float(tools.get("movement") or 0)
    ctrl = float(tools.get("control") or 0)

    effective: dict[str, float | int | None] = dict(tools)

    if stf < 50 and stf > 0:
        compensators = []
        if mov > 50:
            compensators.append((mov, 0.020))
        if ctrl > 50:
            compensators.append((ctrl, 0.012))
        if compensators:
            effective["_stuff_transformed"] = _compensated_transform(stf, compensators)

    if ctrl < 50 and ctrl > 0:
        compensators = []
        if stf > 50:
            compensators.append((stf, 0.018))
        if mov > 50:
            compensators.append((mov, 0.012))
        if compensators:
            effective["_control_transformed"] = _compensated_transform(ctrl, compensators)

    return effective


def offensive_grade_raw(
    tools: dict[str, float | int | None],
    weights: dict[str, float],
    transforms: dict[str, list[float]] | None = None,
) -> Optional[float]:
    """Unclamped offensive weighted average with tool compensation.

    ``transforms`` optionally maps a tool name to a per-tool transform curve
    (from ``derive_tool_transform``); tools without a curve fall back to the
    global :func:`tool_transform`.
    """
    effective = _apply_hitter_compensation(tools)
    _COMPENSATED_KEYS = {"power": "_power_transformed", "eye": "_eye_transformed"}

    available: list[tuple[float, float]] = []
    for key in OFFENSIVE_TOOL_KEYS:
        val = effective.get(key)
        w = weights.get(key, 0.0)
        if val is not None and w > 0:
            comp_key = _COMPENSATED_KEYS.get(key)
            comp_val = effective.get(comp_key) if comp_key else None
            if comp_val is not None:
                transformed = float(comp_val)
            else:
                transformed = apply_tool_transform(
                    float(val), (transforms or {}).get(key))
            available.append((transformed, w))

    if not available:
        return None

    total_weight = sum(w for _, w in available)
    if total_weight <= 0:
        return None

    return sum(val * (w / total_weight) for val, w in available)


def baserunning_value_raw(
    tools: dict[str, float | int | None],
    weights: dict[str, float],
    transforms: dict[str, list[float]] | None = None,
) -> Optional[float]:
    """Unclamped baserunning weighted average.

    ``transforms`` optionally maps a tool name to a per-tool transform curve
    (from ``derive_tool_transform``), same as ``offensive_grade_raw``. Only
    "speed" has a calibrated curve today (steal/stl_rt are left linear — no
    curve was fit for them); a tool with no curve of its own passes through
    unchanged rather than falling back to the global tool_transform, since
    that default curve was derived from offensive-tool marginal-WAR data and
    doesn't apply to baserunning tools.
    """
    available: list[tuple[float, float]] = []
    for key in BASERUNNING_TOOL_KEYS:
        val = tools.get(key)
        w = weights.get(key, 0.0)
        if val is not None and w > 0:
            curve = (transforms or {}).get(key)
            transformed = apply_tool_transform(float(val), curve) if curve else float(val)
            available.append((transformed, w))

    if not available:
        return None

    total_weight = sum(w for _, w in available)
    if total_weight <= 0:
        return None

    return sum(val * (w / total_weight) for val, w in available)


def defensive_value_raw(
    defense: dict[str, float | int | None],
    def_weights: dict[str, float],
) -> Optional[float]:
    """Unclamped defensive weighted average."""
    available: list[tuple[float, float]] = []
    for key, w in def_weights.items():
        val = defense.get(key)
        if val is not None and w > 0:
            available.append((float(val), w))

    if not available:
        return None

    total_weight = sum(w for _, w in available)
    if total_weight <= 0:
        return None

    return sum(val * (w / total_weight) for val, w in available)


def compute_offensive_grade(
    tools: dict[str, float | int | None],
    weights: dict[str, float],
    transforms: dict[str, list[float]] | None = None,
) -> Optional[int]:
    """Compute offensive component from hitting tools only.

    Uses contact, gap, power, eye with piecewise tool transform and
    calibrated weights. Returns integer on 20-80 scale.
    """
    raw = offensive_grade_raw(tools, weights, transforms)
    if raw is None:
        return None
    return max(20, min(80, round(raw)))


def compute_baserunning_value(
    tools: dict[str, float | int | None],
    weights: dict[str, float],
    transforms: dict[str, list[float]] | None = None,
) -> Optional[int]:
    """Compute baserunning component from speed/steal tools.

    Returns integer on 20-80 scale.
    """
    raw = baserunning_value_raw(tools, weights, transforms)
    if raw is None:
        return None
    return max(20, min(80, round(raw)))


def compute_defensive_value(
    defense: dict[str, float | int | None],
    def_weights: dict[str, float],
) -> Optional[int]:
    """Compute defensive component from positional defensive tools.

    Returns integer on 20-80 scale.
    """
    raw = defensive_value_raw(defense, def_weights)
    if raw is None:
        return None
    return max(20, min(80, round(raw)))


def _prospect_range_bump_runs(defense, bucket):
    """Runs credit for a prospect's range grade (OF: OFR, IF: IFR); 0 otherwise."""
    if bucket in _OF_BUCKETS:
        fam, keys = "OF", ("OFR", "ofr")
    elif bucket in _IF_BUCKETS:
        fam, keys = "IF", ("IFR", "ifr")
    else:
        return 0.0
    grade = next((defense[k] for k in keys if (defense or {}).get(k) is not None), None)
    if grade is None:
        return 0.0
    mild_floor, big_floor = _RANGE_BUMP_THRESHOLDS[fam]
    if grade >= big_floor:
        return RANGE_BUMP_BIG_RUNS
    if grade >= mild_floor:
        return RANGE_BUMP_MILD_RUNS
    return 0.0


def _run_space_hitter_composite(tools, defense, bucket, run_space, positional_models,
                                observed=None, neutral_position=False):
    """Run-space composite (20-80) or None if calibration is incomplete.

    Projects wOBA from tools via the calibrated tool->wOBA fit, combines facet
    runs additively, and maps to 20-80 via the calibrated composite mapping.

    ``observed`` (optional, B2 per-facet convergence): dict with observed career
    facet runs and their playing-time samples::

        {"bat_runs": wRAA, "bat_pa": PA,
         "br_runs": UBR,   "br_pa": PA,
         "fld_runs": ZR,   "fld_ip": IP}

    When present, each facet blends its tool projection with the observed value
    by a facet-specific stabilization confidence — replacing the old OPS+
    ``compute_composite_mlb`` blend with a per-facet run-space blend so the
    composite and the WAR projection share one run total.
    """
    if not run_space or not bucket:
        return None
    fit = run_space.get("tool_woba_fit")
    mapping = run_space.get("comp_mapping")
    if not fit or not mapping:
        return None
    con = tools.get("contact"); gap = tools.get("gap")
    pw = tools.get("power"); eye = tools.get("eye")
    if None in (con, gap, pw, eye):
        return None
    from statsplusplus.evaluation import facet_runs as _fr
    proj_woba = (fit[0] + fit[1] * float(con) + fit[2] * float(gap)
                 + fit[3] * float(pw) + fit[4] * float(eye))
    lg_woba = run_space.get("lg_woba", 0.320)
    woba_scale = run_space.get("woba_scale", 1.28)
    rpw = run_space.get("anchor", {}).get("runs_per_win", 9.5)

    # Tool-projected facet runs
    bat_tool = _fr.bat_runs(proj_woba, lg_woba, woba_scale, 600.0)
    br_tool = _fr.baserunning_runs(tools, run_space.get("br_curve"))
    fld = _fr.fielding_runs(defense or {}, bucket, run_space.get("def_curve"), positional_models)
    pos = _fr.positional_adj_runs(bucket, rpw)
    # Prospects (non-MLB, age <= 24): ignore both the positional adjustment and
    # fielding credit — their position skills are still developing, so neither
    # a premium-position buff nor a weak-glove penalty is a reliable signal yet.
    # Value is then bat + baserunning only (a neutral-position, neutral-glove player).
    if neutral_position:
        fld = _prospect_range_bump_runs(defense, bucket)
        pos = 0.0

    # B2: per-facet blend with observed MLB career runs (bat/baserunning only;
    # defense stays tool-based — MLB ZR is used directly via the tool curve and
    # observed-ZR blending is handled at the run level when provided).
    if observed:
        bat_pa = observed.get("bat_pa", 0.0)
        if observed.get("bat_runs") is not None and bat_pa > 0:
            scb = _fr.facet_stat_confidence("bat", bat_pa)
            bat_tool = (1 - scb) * bat_tool + scb * observed["bat_runs"]
        br_pa = observed.get("br_pa", 0.0)
        if observed.get("br_runs") is not None and br_pa > 0:
            scr = _fr.facet_stat_confidence("baserunning", br_pa)
            br_tool = (1 - scr) * br_tool + scr * observed["br_runs"]
        fld_ip = observed.get("fld_ip", 0.0)
        if not neutral_position and observed.get("fld_runs") is not None and fld_ip > 0:
            scf = _fr.facet_stat_confidence("fielding", fld_ip)
            obs_fld = observed["fld_runs"]
            # Positional ZR difficulty bonus (data-driven, calibrated per
            # league): real demonstrated +ZR at a position where few players
            # clear 0 (e.g. CF/SS) counts for more than the same +ZR at an
            # easy position. Only rewards a POSITIVE observed reading — it
            # doesn't add extra punishment for below-average defense.
            if obs_fld > 0:
                diff_mult = (run_space.get("fld_difficulty") or {}).get(bucket, 1.0)
                obs_fld = obs_fld * diff_mult
            fld = (1 - scf) * fld + scf * obs_fld

    total = bat_tool + br_tool + fld + pos
    return _fr.runs_to_composite(total, mapping, bucket)


def compute_composite_hitter(
    tools: dict[str, float | int | None],
    weights: dict[str, float],
    defense: dict[str, float | int | None],
    def_weights: dict[str, float],
    transforms: dict[str, list[float]] | None = None,
    run_space: dict | None = None,
    bucket: str | None = None,
    positional_models: dict | None = None,
    observed: dict | None = None,
    neutral_position: bool = False,
) -> int:
    """Compute hitter Composite_Score from tool ratings and weights.

    Two modes:
      - **Run-space (preferred):** when ``run_space`` calibration + ``bucket`` are
        provided, values are combined additively in RUNS (bat wRAA + baserunning
        + fielding + positional adjustment) then mapped to 20-80. This is the
        principled model (spec: run-space-facet-model).
      - **Grade-space (fallback):** the legacy share-weighted grade blend, used
        when run-space calibration is absent (backward compatible).

    Args:
        tools: Hitter tool ratings on 20-80 scale (contact, gap, power, eye,
            speed, steal, stl_rt). None values are skipped with weight renorm.
        weights: Positional weight profile summing to ~1.0.
        defense: Defensive tool ratings (IFR, OFR, CArm, etc.).
        def_weights: Positional defensive importance weights.
        transforms: Optional per-tool transform curves. Falls back to the
            global tool_transform for any tool without a curve.

    Returns:
        Integer composite score in [20, 80].
    """
    rs_score = _run_space_hitter_composite(
        tools, defense, bucket, run_space, positional_models, observed,
        neutral_position=neutral_position)
    if rs_score is not None:
        return rs_score

    off_raw = offensive_grade_raw(tools, weights, transforms)
    br_raw = baserunning_value_raw(tools, weights, transforms)
    def_raw = defensive_value_raw(defense, def_weights)

    if off_raw is None and br_raw is None and def_raw is None:
        return 20

    # Derive recombination shares from weight profile
    defense_weight = weights.get("defense", 0.0)
    off_w = sum(weights.get(k, 0.0) for k in OFFENSIVE_TOOL_KEYS)
    br_w = sum(weights.get(k, 0.0) for k in BASERUNNING_TOOL_KEYS)
    tool_w_total = off_w + br_w

    if tool_w_total > 0:
        offense_share = off_w / tool_w_total * (1.0 - defense_weight)
        baserunning_share = br_w / tool_w_total * (1.0 - defense_weight)
    else:
        offense_share = 1.0 - defense_weight
        baserunning_share = 0.0

    # Contact-scaled baserunning boost
    cnt = float(tools.get("contact") or 0)
    if cnt > 50 and baserunning_share > 0:
        br_boost_factor = min(1.0, (cnt - 50.0) / 30.0)
        br_addition = baserunning_share * br_boost_factor
        baserunning_share += br_addition
        offense_share -= br_addition

    # Elite defense boost
    if def_raw is not None and defense_weight > 0 and defense:
        primary_def = max((v for v in defense.values() if v is not None), default=0)
        if primary_def > 50:
            def_boost_factor = min(1.0, (primary_def - 50.0) / 30.0)
            def_addition = defense_weight * def_boost_factor
            defense_weight += def_addition
            offense_share -= def_addition

    # Recombine
    raw = 0.0
    if off_raw is not None:
        raw += off_raw * offense_share
    if br_raw is not None:
        raw += br_raw * baserunning_share
    if def_raw is not None:
        raw += def_raw * defense_weight

    # Sub-MLB floor penalty (offensive tools only)
    hitting_tools = {k: v for k, v in tools.items() if k in OFFENSIVE_TOOL_KEYS}
    raw -= sub_mlb_floor_penalty(hitting_tools)

    # Speed × contact synergy: fast players with good contact produce more
    # value than the linear sum suggests (infield hits, extra bases, pressure).
    # Additive bonus when both speed and contact exceed thresholds.
    spd = float(tools.get("speed") or 0)
    if spd > 45 and cnt > 50:
        synergy = 0.10 * (spd - 45) * (cnt / 60.0)
        raw += synergy

    # Tool imbalance penalty for one-tool hitters
    _hit_vals = [v for v in hitting_tools.values() if v]
    if len(_hit_vals) >= 3:
        tool_spread = max(_hit_vals) - min(_hit_vals)
        if tool_spread > HITTER_IMBALANCE_SPREAD_THRESHOLD:
            spread_penalty = (tool_spread - HITTER_IMBALANCE_SPREAD_THRESHOLD) * HITTER_IMBALANCE_SPREAD_RATE
            weakness_penalty = sum(
                max(0, HITTER_IMBALANCE_WEAKNESS_THRESHOLD - v) * HITTER_IMBALANCE_WEAKNESS_RATE
                for v in _hit_vals if v < HITTER_IMBALANCE_WEAKNESS_THRESHOLD
            )
            raw -= spread_penalty + weakness_penalty

    return max(20, min(80, round(raw)))


def compute_composite_pitcher(
    tools: dict[str, float | int | None],
    weights: dict[str, float],
    arsenal: dict[str, float | int],
    stamina: float | int,
    role: str,
    transforms: dict[str, list[float]] | None = None,
) -> int:
    """Compute pitcher Composite_Score from tools, arsenal, and role.

    Args:
        tools: Pitcher tool ratings (stuff, movement, control) on 20-80 scale.
        weights: Pitcher weight profile (stuff, movement, control, arsenal).
        arsenal: Pitch arsenal {pitch_name: rating}.
        stamina: Stamina rating on 20-80 scale.
        role: "SP" or "RP".

    Returns:
        Integer composite score in [20, 80].
    """
    arsenal_weight = weights.get("arsenal", 0.0)

    # Apply compensation
    effective_tools = _apply_pitcher_compensation(tools)
    _COMPENSATED_KEYS = {"stuff": "_stuff_transformed", "control": "_control_transformed"}

    available: list[tuple[float, float]] = []
    for key in PITCHER_TOOL_KEYS:
        val = effective_tools.get(key)
        w = weights.get(key, 0.0)
        if val is not None and w > 0:
            comp_key = _COMPENSATED_KEYS.get(key)
            comp_val = effective_tools.get(comp_key) if comp_key else None
            if comp_val is not None:
                transformed = float(comp_val)
            else:
                transformed = apply_tool_transform(
                    float(val), (transforms or {}).get(key))
            available.append((transformed, w))

    if not available:
        return 20

    total_tool_weight = sum(w for _, w in available)
    tool_share = 1.0 - arsenal_weight

    if total_tool_weight > 0:
        tool_sum = sum(
            val * (w / total_tool_weight) * tool_share
            for val, w in available
        )
    else:
        tool_sum = 0.0

    # Arsenal depth bonus
    pitches_45_plus = sum(1 for r in arsenal.values() if r >= 45)
    depth_bonus = min(3, max(0, pitches_45_plus - 3))

    # Top-pitch quality bonus
    best_pitch = max(arsenal.values()) if arsenal else 0
    if best_pitch >= 70:
        quality_bonus = 2
    elif best_pitch >= 65:
        quality_bonus = 1
    else:
        quality_bonus = 0

    arsenal_score = 50.0 + (depth_bonus + quality_bonus) * 5.0
    arsenal_score = max(20.0, min(80.0, arsenal_score))

    raw = tool_sum + arsenal_score * arsenal_weight

    # Stamina penalty for SP
    if role == "SP" and stamina < 40:
        penalty = min(5.0, (40 - stamina) * 0.15)
        raw -= penalty

    # SP innings-volume adjustment
    if role == "SP" and stamina > 45:
        bonus = min(4.0, (stamina - 45) * 0.12)
        raw += bonus

    # Platoon balance penalty
    stuff_l = tools.get("stuff_l")
    stuff_r = tools.get("stuff_r")
    if stuff_l is not None and stuff_r is not None:
        weak_side = min(stuff_l, stuff_r)
        gap = abs(stuff_l - stuff_r)
        if weak_side < 35 and gap >= 15:
            raw -= 3 if weak_side <= 25 else 2

    # Sub-MLB floor penalty for core tools
    core_tools = {k: v for k, v in tools.items() if k in PITCHER_CORE_KEYS}
    raw -= sub_mlb_floor_penalty(core_tools)

    # Tool imbalance penalty for one-dimensional pitchers
    _core_vals = [v for k, v in tools.items() if k in PITCHER_CORE_KEYS and v]
    if len(_core_vals) >= 2:
        tool_spread = max(_core_vals) - min(_core_vals)
        if tool_spread > PITCHER_IMBALANCE_SPREAD_THRESHOLD:
            spread_penalty = (tool_spread - PITCHER_IMBALANCE_SPREAD_THRESHOLD) * PITCHER_IMBALANCE_SPREAD_RATE
            weakness_penalty = sum(
                max(0, PITCHER_IMBALANCE_WEAKNESS_THRESHOLD - v) * PITCHER_IMBALANCE_WEAKNESS_RATE
                for v in _core_vals if v < PITCHER_IMBALANCE_WEAKNESS_THRESHOLD
            )
            raw -= spread_penalty + weakness_penalty

    return max(20, min(80, round(raw)))


def compute_tool_only_score(
    player_type: str,
    tools: dict[str, float | int | None],
    weights: dict[str, float],
    defense: Optional[dict[str, float | int | None]] = None,
    def_weights: Optional[dict[str, float]] = None,
    arsenal: Optional[dict[str, float | int]] = None,
    stamina: int = 50,
    role: str = "SP",
    transforms: dict[str, list[float]] | None = None,
) -> int:
    """Compute the pre-stat-blend score for a player.

    Delegates to compute_composite_hitter or compute_composite_pitcher.
    ``transforms`` optionally supplies per-tool value curves.
    """
    if player_type == "hitter":
        return compute_composite_hitter(tools, weights, defense or {}, def_weights or {}, transforms)
    else:
        return compute_composite_pitcher(tools, weights, arsenal or {}, stamina, role, transforms)


def compute_composite_mlb(
    tool_score: int,
    stat_seasons: list[float],
    peak_age: int = 28,
    player_age: int = 28,
    is_pitcher: bool = False,
    bucket: str = "",
) -> int:
    """Blend tool-based score with stat performance for MLB players.

    Args:
        tool_score: Pre-blend tool-only score (20-80).
        stat_seasons: Normalized stat values on 20-80 scale (most recent first).
        peak_age: Expected peak age for position.
        player_age: Player's current age.
        is_pitcher: Whether the player is a pitcher.
        bucket: Positional bucket (SS, CF get reduced blend because OPS+
            doesn't capture their defensive WAR contribution).

    Returns:
        Integer composite score in [20, 80].
    """
    if not stat_seasons:
        return tool_score

    # Recency weighting
    recency_weights = [3.0, 2.0, 1.0]
    weighted_sum = 0.0
    total_weight = 0.0
    for i, stat_val in enumerate(stat_seasons[:3]):
        w = recency_weights[i] if i < len(recency_weights) else 1.0
        weighted_sum += stat_val * w
        total_weight += w

    stat_signal = weighted_sum / total_weight if total_weight > 0 else tool_score

    # Blend weight based on seasons available
    seasons_available = min(len(stat_seasons), 3)
    blend_weight = {1: 0.20, 2: 0.35, 3: 0.60}[seasons_available]

    # Defense-first position adjustment: OPS+ doesn't capture defensive WAR,
    # so stat blending at these positions pulls the composite away from the
    # defensive value that actually drives their production. Reduce blend.
    _DEFENSE_POSITIONS = {"SS": 0.50, "CF": 0.50, "2B": 0.75, "C": 0.75}
    if bucket in _DEFENSE_POSITIONS:
        blend_weight *= _DEFENSE_POSITIONS[bucket]

    # Young player adjustment: a young player's tools are more predictive than
    # a small early-career stat sample, so dampen the blend when tools exceed
    # the stat signal below peak age.
    if player_age < peak_age and tool_score > stat_signal:
        age_factor = max(0.3, 1.0 - (peak_age - player_age) * 0.1)
        blend_weight *= age_factor

    # Aging player adjustment (symmetric): past peak age, a player's tools
    # reflect current, declining ability, while recent-but-fading results can
    # overstate what he still is. When the stat signal exceeds the tools for a
    # post-peak player, dampen the blend so the declined tools carry more
    # weight. Without this, aging relievers coasting on good ERAs are lifted
    # well above their tools (empirically the age-35+ group showed the largest
    # positive blend lift, concentrated in low-stuff arms).
    elif player_age > peak_age and stat_signal > tool_score:
        age_factor = max(0.3, 1.0 - (player_age - peak_age) * 0.1)
        blend_weight *= age_factor

    composite = tool_score * (1.0 - blend_weight) + stat_signal * blend_weight
    return max(20, min(80, round(composite)))


def stat_to_2080(stat_plus: float) -> float:
    """Convert a league-normalized rate stat (OPS+) to the 20-80 scale.

    Formula: 20 + (stat_plus / 200) * 60, clamped to [20, 80].
    """
    raw = 20.0 + (stat_plus / 200.0) * 60.0
    return max(20.0, min(80.0, raw))


def pitcher_stat_to_2080(stat_plus: float) -> float:
    """Convert inverted FIP- to 20-80 scale with asymmetric mapping.

    Above-average (stat_plus > 100): steeper slope (0.45/point) rewards excellence.
    Below-average (stat_plus < 100): standard slope (0.30/point) avoids over-penalizing.
    """
    if stat_plus >= 100:
        raw = 50.0 + (stat_plus - 100.0) * 0.45
    else:
        raw = 50.0 + (stat_plus - 100.0) * 0.30
    return max(20.0, min(80.0, raw))


def compute_combined_value(primary_composite: int, secondary_composite: int) -> int:
    """Compute the combined value for a two-way player.

    Formula: ``primary + min(8, max(0, (secondary - 35) * 0.3))``

    The secondary bonus reflects partial additional value from the secondary
    role. Only applies when the secondary score exceeds replacement level (35).
    Capped at +8 to prevent unrealistically high combined scores.

    Args:
        primary_composite: The higher of the two role scores (20-80).
        secondary_composite: The lower of the two role scores (20-80).

    Returns:
        Combined value as an integer. Always >= primary_composite.
    """
    secondary_bonus = min(8, max(0, (secondary_composite - 35) * 0.3))
    return min(80, round(primary_composite + secondary_bonus))
```


## B.4 facet_runs.py — run-space spine (selected)
```python
DEFAULT_RUNS_PER_WIN: float = 9.5
DEFAULT_REPLACEMENT_RUNS: float = 20.0
BASERUNNING_PRIOR_SLOPE: float = 0.20   # runs per grade point above 50
FIELDING_PRIOR_SLOPE: float = 0.50      # runs per grade point above 50 (range)
AGING_BAT: dict[int, float] = {
    24: 0.93, 25: 0.96, 26: 0.99, 27: 1.00, 28: 1.00, 29: 0.98, 30: 0.94,
    31: 0.89, 32: 0.83, 33: 0.76, 34: 0.68, 35: 0.59, 36: 0.50, 38: 0.34, 40: 0.18,
}
AGING_BASERUNNING: dict[int, float] = {  # peaks early, declines earliest/steepest
    22: 1.00, 23: 1.00, 24: 0.99, 25: 0.97, 26: 0.93, 27: 0.88, 28: 0.82,
    29: 0.75, 30: 0.67, 31: 0.58, 32: 0.49, 33: 0.40, 34: 0.31, 36: 0.15, 38: 0.0,
}
AGING_DEFENSE: dict[int, float] = {  # tracks speed but experience offsets => moderate
    23: 1.00, 24: 1.00, 25: 0.99, 26: 0.97, 27: 0.94, 28: 0.90, 29: 0.85,
    30: 0.79, 31: 0.72, 32: 0.64, 33: 0.56, 34: 0.47, 35: 0.38, 37: 0.22, 40: 0.05,
}
DEV_BAT: dict[int, float] = {  # slow, extends latest — big gains through mid-20s
    17: 0.10, 18: 0.18, 19: 0.28, 20: 0.40, 21: 0.52, 22: 0.63,
    23: 0.73, 24: 0.82, 25: 0.90, 26: 0.96, 27: 1.00,
}
DEV_BASERUNNING: dict[int, float] = {  # near-fixed early; little to develop
    17: 0.55, 18: 0.65, 19: 0.75, 20: 0.84, 21: 0.91, 22: 0.96, 23: 1.00,
}
DEV_DEFENSE: dict[int, float] = {  # moderate; reps/positioning through early 20s
    17: 0.30, 18: 0.42, 19: 0.54, 20: 0.66, 21: 0.77, 22: 0.86,
    23: 0.93, 24: 0.98, 25: 1.00,
}
CATCHER_DEF_TOOLS: tuple[str, ...] = ("c_frm", "c_blk", "c_arm")
INFIELD_DEF_TOOLS: tuple[str, ...] = ("ifr", "ife", "ifa", "tdp")
OUTFIELD_DEF_TOOLS: tuple[str, ...] = ("ofr", "ofe", "ofa")


def project_facet_runs(
    facet: str,
    current_runs: float,
    ceiling_runs: float,
    age: float,
    dev_pace: float = 1.0,
) -> float:
    """Project a facet's runs at ``age`` (development up-slope only, pre-peak).

    Grows ``current_runs`` toward ``ceiling_runs`` by the per-facet development
    progress at ``age``, scaled by ``dev_pace`` (a clamped rate modifier from
    dev_speed; 1.0 = neutral). Only closes the gap when there IS room
    (ceiling > current); never projects below current.
    """
    curve = {"bat": DEV_BAT, "baserunning": DEV_BASERUNNING, "fielding": DEV_DEFENSE}.get(facet, DEV_BAT)
    if ceiling_runs <= current_runs:
        return current_runs
    progress = min(1.0, _dev_progress(age, curve) * max(0.0, dev_pace))
    return current_runs + (ceiling_runs - current_runs) * progress


def dev_pace_from_z(z: Optional[float], confidence: float = 1.0) -> float:
    """Map a dev_speed z-score to a growth-rate MODIFIER, clamped to [0.6, 1.4].

    A modifier, never a driver: a fast developer (z>0) closes the gap sooner, a
    stalled one (z<0) slower. Pulled toward 1.0 by ``confidence`` in [0,1] (low
    dev_speed confidence → little/no adjustment). dev_speed changes the TIMING of
    reaching the (already-determined) ceiling, never the ceiling itself.
    """
    if z is None:
        return 1.0
    raw = 1.0 + max(-1.0, min(1.0, z / 2.0)) * 0.4   # z=±2 -> ±0.4
    raw = max(0.6, min(1.4, raw))
    conf = max(0.0, min(1.0, confidence))
    return 1.0 + (raw - 1.0) * conf


def facet_aging_mult(age: float, bat_runs: float, br_runs: float, fld_runs: float) -> float:
    """Per-facet blended aging multiplier on peak WAR (P1 of the projection spec).

    Instead of one whole-player aging curve, age each facet by its own curve
    (baserunning earliest/steepest, bat latest, defense moderate) and recombine
    weighted by each facet's share of the player's POSITIVE run contribution.
    Positional adjustment is age-invariant and excluded from the weighting.

    Returns a single multiplier equivalent to the run-weighted average of the
    three facet aging multipliers — so it slots in where ``aging_mult`` is used
    without changing the surplus math shape. Falls back to the bat curve when
    the player has no meaningful positive facet runs.
    """
    m_bat = _aging_mult(age, AGING_BAT)
    m_br = _aging_mult(age, AGING_BASERUNNING)
    m_fld = _aging_mult(age, AGING_DEFENSE)
    # Weight by positive run contribution (a facet that costs runs doesn't get
    # "aged" as a value source — clamp negatives to 0 for weighting only).
    w_bat = max(0.0, bat_runs)
    w_br = max(0.0, br_runs)
    w_fld = max(0.0, fld_runs)
    total = w_bat + w_br + w_fld
    if total <= 0:
        return m_bat
    return (m_bat * w_bat + m_br * w_br + m_fld * w_fld) / total


def facet_stat_confidence(facet: str, pa_or_ip: float) -> float:
    """Per-facet stabilization ramp (spec §3.1a).

    Different metrics stabilize at different rates:
      bat (wRAA): SLOW  — full ~600 PA
      baserunning (UBR): FAST — low variance, full ~250 PA
      fielding (ZR): SLOWEST — noisy, full ~900 PA (~1.5 seasons)
    """
    full = {"bat": 600.0, "baserunning": 250.0, "fielding": 900.0}.get(facet, 600.0)
    cap = {"bat": 0.90, "baserunning": 0.95, "fielding": 0.80}.get(facet, 0.9)
    if pa_or_ip <= 0:
        return 0.0
    return min(cap, (pa_or_ip / full) ** 1.1)


def bat_runs(
    proj_woba: float,
    lg_woba: float,
    woba_scale: float,
    pa: float = 600.0,
) -> float:
    """wRAA = ((wOBA - lg_wOBA) / wOBA_scale) * PA. Runs above average."""
    if woba_scale <= 0:
        return 0.0
    return ((proj_woba - lg_woba) / woba_scale) * pa


def _linear_curve(grade: float, curve: Optional[dict[str, float]], prior_slope: float) -> float:
    """Apply a calibrated linear grade->runs curve, or a prior slope fallback.

    curve = {"intercept": a, "slope": b} → runs = a + b*grade.
    Fallback: prior_slope * (grade - 50).
    """
    if curve and "slope" in curve:
        return float(curve.get("intercept", 0.0)) + float(curve["slope"]) * grade
    return prior_slope * (grade - 50.0)


def baserunning_runs(
    tools: dict[str, float | int | None],
    curve: Optional[dict[str, float]] = None,
) -> float:
    """Baserunning runs (UBR-scale) from speed/steal grades.

    Uses a calibrated speed->UBR curve when available, else a prior slope.
    Steal contributes a small secondary adjustment.
    """
    spd = tools.get("speed")
    if spd is None:
        return 0.0
    runs = _linear_curve(float(spd), curve, BASERUNNING_PRIOR_SLOPE)
    # Secondary: aggressive/successful basestealing adds a touch on top of raw speed.
    stl = tools.get("steal")
    if stl is not None and curve and "steal_slope" in curve:
        runs += float(curve["steal_slope"]) * (float(stl) - 50.0)
    return runs


def fielding_runs(
    def_tools: dict[str, float | int | None],
    bucket: str,
    curve: Optional[dict[str, Any]] = None,
    positional_models: Optional[dict[str, Any]] = None,
) -> float:
    """Fielding runs (ZR-scale) at the given bucket from defensive tools.

    curve maps bucket -> {"intercept", "slope"} on the position's primary
    range grade. Falls back to a prior slope. 1B/DH get ~0 (little defensive
    spread captured by range tools).
    """
    if bucket in ("1B", "DH"):
        return 0.0
    bcurve = (curve or {}).get(bucket) if curve else None
    # Multi-tool positions (catcher: framing+blocking+arm; infield: range+error+
    # arm+DP) blend several tools via calibrated per-tool weights instead of a
    # single range grade. Any curve carrying a "weights" dict uses the blend.
    if bcurve and bcurve.get("weights") and (bcurve.get("multitool") or bcurve.get("catcher")):
        grade = _blended_def_grade(def_tools, bcurve["weights"])
        if grade is None:
            return 0.0
        runs = _linear_curve(float(grade), bcurve, FIELDING_PRIOR_SLOPE)
        if "clamp_lo" in bcurve and "clamp_hi" in bcurve:
            runs = max(bcurve["clamp_lo"], min(bcurve["clamp_hi"], runs))
        return runs
    # Single range-grade path (outfield; any position without a multi-tool curve).
    grade = _primary_def_grade(def_tools, bucket, positional_models)
    if grade is None:
        return 0.0
    runs = _linear_curve(float(grade), bcurve, FIELDING_PRIOR_SLOPE)
    # Clamp to the position's observed ZR range (prevent steep-slope extrapolation
    # to absurd runs far from the calibration sample's grade range).
    if bcurve and "clamp_lo" in bcurve and "clamp_hi" in bcurve:
        runs = max(bcurve["clamp_lo"], min(bcurve["clamp_hi"], runs))
    return runs


def _primary_def_grade(
    def_tools: dict[str, float | int | None],
    bucket: str,
    positional_models: Optional[dict[str, Any]],
) -> Optional[float]:
    """Best available range grade for the bucket.

    The fielding curves are calibrated on the position's RANGE tool (OFR for
    outfield, IFR for infield, CArm for catcher), so evaluation must use the
    same grade type. Prefer the actual range rating; the positional-model
    estimate is only a last resort (it under/over-estimates vs the true rating
    — e.g. an elite CF with OFR 70 was estimated at 58, wrongly costing runs).
    """
    # 1) Explicit per-bucket grade if the caller supplied one.
    direct = def_tools.get(bucket)
    if direct is not None:
        return float(direct)
    # 2) The position-appropriate RANGE tool (matches the calibration grade).
    if bucket in ("CF", "COF", "LF", "RF"):
        for k in ("OFR", "ofr"):
            if def_tools.get(k) is not None:
                return float(def_tools[k])
    elif bucket in ("SS", "2B", "3B"):
        for k in ("IFR", "ifr"):
            if def_tools.get(k) is not None:
                return float(def_tools[k])
    elif bucket == "C":
        for k in ("CArm", "c_arm"):
            if def_tools.get(k) is not None:
                return float(def_tools[k])
    # 3) Positional-model estimate (last resort).
    if positional_models:
        ests = estimate_all_positions(def_tools, positional_models)
        if bucket in ests:
            return ests[bucket]
    # 4) Any generic range tool.
    for k in ("ifr", "ofr", "IFR", "OFR"):
        if def_tools.get(k) is not None:
            return float(def_tools[k])
    return None


def positional_adj_runs(bucket: str, runs_per_win: float = DEFAULT_RUNS_PER_WIN) -> float:
    """Positional adjustment in runs (POSITIONAL_WAR_ADJUSTMENTS is in WAR)."""
    war_adj = POSITIONAL_WAR_ADJUSTMENTS.get(bucket, 0.0)
    return war_adj * runs_per_win


def runs_to_war(runs_above_avg: float, anchor: Optional[dict[str, float]] = None) -> float:
    """Convert runs-above-average to WAR (OOTP-anchored)."""
    rpw = (anchor or {}).get("runs_per_win", DEFAULT_RUNS_PER_WIN)
    repl_runs = (anchor or {}).get("replacement_runs", DEFAULT_REPLACEMENT_RUNS)
    if rpw <= 0:
        rpw = DEFAULT_RUNS_PER_WIN
    return (runs_above_avg + repl_runs) / rpw


def saturate_war(war: float, anchor: Optional[dict[str, float]] = None) -> float:
    """Saturate a (possibly extrapolated) WAR toward data-grounded tail caps.

    The linear runs→WAR map is only valid in the fitted ~45-65 composite band;
    projecting a prospect's CEILING score linearly runs off to absurd WAR
    (13-18 at score 80) / absurd negatives at the bottom. This compresses the
    tails toward each league's REAL full-time-hitter WAR distribution using a
    tanh that is ~linear near the median (`sat_mid`) and asymptotes to the
    top/bottom caps (`sat_top`=p98, `sat_bot`=p02). Center/slope preserved;
    only the tails bend. No caps in `anchor` → returned unchanged (back-compat).
    """
    if not anchor:
        return war
    top = anchor.get("sat_top")
    bot = anchor.get("sat_bot")
    mid = anchor.get("sat_mid")
    if top is None or bot is None or mid is None:
        return war
    if war >= mid:
        span = max(0.1, top - mid)
        return mid + span * math.tanh((war - mid) / span)
    span = max(0.1, mid - bot)
    return mid + span * math.tanh((war - mid) / span)


def runs_to_composite(
    runs_above_avg: float,
    mapping: Optional[dict[str, Any]] = None,
    bucket: Optional[str] = None,
) -> int:
    """Map runs-above-average to a 20-80 composite.

    Two mapping shapes are supported:

    **Hybrid (preferred)** — a WAR-anchored, per-position-centered map that
    decompresses the scale (a global ``runs_sd``/``comp_sd`` z-map packed ~1 WAR
    into ~2 composite points). ``mapping`` carries::

        {"slope": pts_per_run,          # = pts_per_war / runs_per_win
         "target_center": 50.0,         # a league-avg, avg-position hitter
         "pts_per_war": 4.0,
         "pos_centers": {bucket: population_runs_mean, ...},
         "global_center": float,        # fallback center
         "top_knee": 70.0, "cap": 80.0, # soft tanh above the knee
         "floor": 20.0}                 # display clamp (internal value may be <20)

        comp = target_center + pos_adj(bucket)*pts_per_war
               + (runs - pos_center[bucket]) * slope
        comp = soft-tanh toward cap above top_knee
        comp = clamp(comp, floor, 80)   # bottom is a DISPLAY clamp only

      Per-position centering makes a league-average player at EVERY position map
      to ~``target_center`` plus its positional credit, so composites are
      comparable across positions. The WAR-anchored slope fixes the compression.

    **Legacy affine (back-compat)** — ``{"runs_mean","runs_sd","comp_mean",
    "comp_sd"}``: z-score vs the MLB composite distribution. Used when the hybrid
    keys are absent.

    **Fallback** — ~0 runs → 50 with a modest slope.
    """
    if mapping and "slope" in mapping:
        tc = mapping.get("target_center", 50.0)
        ppw = mapping.get("pts_per_war", 4.0)
        slope = mapping["slope"]
        centers = mapping.get("pos_centers") or {}
        gc = mapping.get("global_center", 0.0)
        center = centers.get(bucket, gc) if bucket else gc
        pos_pts = POSITIONAL_WAR_ADJUSTMENTS.get(bucket or "", 0.0) * ppw
        comp = tc + pos_pts + (runs_above_avg - center) * slope
        # Soft-saturate the TOP tail toward the cap (a hard clip would re-compress
        # the handful of true elites that overshoot). The bottom is NOT soft-
        # saturated — a large, genuinely sub-replacement minor-league population
        # legitimately falls below 20; we clamp it to the floor for DISPLAY only.
        top_knee = mapping.get("top_knee", 70.0)
        cap = mapping.get("cap", 80.0)
        if comp > top_knee:
            span = cap - top_knee
            if span > 0:
                comp = top_knee + span * math.tanh((comp - top_knee) / span)
            else:
                comp = min(comp, cap)
        floor = mapping.get("floor", 20.0)
        return int(round(max(floor, min(cap, comp))))

    if mapping and mapping.get("runs_sd", 0) > 0 and mapping.get("comp_sd", 0) > 0:
        z = (runs_above_avg - mapping["runs_mean"]) / mapping["runs_sd"]
        comp = mapping["comp_mean"] + z * mapping["comp_sd"]
    else:
        comp = 50.0 + runs_above_avg * 0.5
    return max(20, min(80, round(comp)))


def composite_to_runs(
    comp: float,
    mapping: Optional[dict[str, Any]] = None,
    bucket: Optional[str] = None,
) -> float:
    """Invert ``runs_to_composite``: map a 20-80 composite/ceiling score back to
    runs-above-average. Used by the ceiling-anchored FV to recover the WAR a
    POTENTIAL (ceiling) score implies.

    Handles both mapping shapes. For the hybrid map it undoes the per-position
    affine (and reverses the top soft-saturation so an elite, saturated ceiling
    recovers its true pre-saturation run total). The bottom DISPLAY clamp is not
    invertible and is irrelevant here — the FV path never passes a floored score
    through this (ceilings are not sub-replacement).

    Returns runs-above-average (unclamped).
    """
    # --- Hybrid inverse ---
    if mapping and "slope" in mapping:
        slope = mapping["slope"]
        if not slope:
            return 0.0
        tc = mapping.get("target_center", 50.0)
        ppw = mapping.get("pts_per_war", 4.0)
        centers = mapping.get("pos_centers") or {}
        gc = mapping.get("global_center", 0.0)
        center = centers.get(bucket, gc) if bucket else gc
        pos_pts = POSITIONAL_WAR_ADJUSTMENTS.get(bucket or "", 0.0) * ppw
        # Reverse the top soft-saturation (tanh) so a saturated score recovers
        # its true pre-saturation value before the linear inverse.
        top_knee = mapping.get("top_knee", 70.0)
        cap = mapping.get("cap", 80.0)
        c = float(comp)
        if c > top_knee:
            span = cap - top_knee
            # forward: comp = knee + span*tanh((raw-knee)/span); invert via atanh.
            if span > 0:
                frac = (c - top_knee) / span
                frac = max(-0.999999, min(0.999999, frac))
                c = top_knee + span * math.atanh(frac)
        return center + (c - tc - pos_pts) / slope

    # --- Legacy affine inverse ---
    if mapping and mapping.get("comp_sd", 0) > 0 and mapping.get("runs_sd", 0) > 0:
        z = (comp - mapping["comp_mean"]) / mapping["comp_sd"]
        return mapping["runs_mean"] + z * mapping["runs_sd"]

    # Fallback inverse of comp = 50 + runs*0.5
    return (comp - 50.0) / 0.5


def calibrate_grade_runs_curve(
    grades: list[float],
    observed_runs: list[float],
    prior_slope: float,
    shrink: float = 0.75,
    min_n: int = 8,
    clamp_pad: float = 1.1,
    center_grade: Optional[float] = None,
) -> Optional[dict[str, float]]:
    """Fit a centered, clamped grade->runs curve from samples.

    Centers at ``center_grade`` if given (the POPULATION-average grade at the
    position — so a league-average fielder gets ~0 runs), else the calibration
    sample's mean grade. The distinction matters when the calibration sample
    (e.g. high-IP starters) is more selective than the population being scored:
    centering on the selective sample shifts the whole population negative.
    Shrinks the slope toward the prior and clamps to the observed run range.
    Returns None if the sample is too thin (caller falls back to the prior slope).
    """
    if len(grades) < min_n:
        return None
    _, slope, r = _ols_1var(grades, observed_runs)
    slope *= shrink
    sample_mean = sum(grades) / len(grades)
    center = center_grade if center_grade is not None else sample_mean
    # Clamp to ROBUST percentiles of observed runs (5th/95th), not min/max — a
    # single noisy extreme season otherwise sets the ceiling and lets the linear
    # slope over-extrapolate at the high end (observed fielding runs plateau,
    # e.g. corner-OF ZR tops out ~+6-7, but a steep slope reached +10.8).
    sr = sorted(observed_runs)
    lo_i = max(0, int(0.05 * (len(sr) - 1)))
    hi_i = min(len(sr) - 1, int(round(0.95 * (len(sr) - 1))))
    return {
        "intercept": -slope * center,
        "slope": slope,
        "r": round(r, 3),
        "n": len(grades),
        "mean_grade": round(center, 1),
        "clamp_lo": round(sr[lo_i], 1),
        "clamp_hi": round(sr[hi_i], 1),
    }


def _blended_def_grade(def_tools: dict[str, float | int | None],
                       weights: dict[str, float]) -> Optional[float]:
    """Weighted-blend defensive grade (20-80) from a multi-tool `weights` dict.

    `weights` is the calibrated per-tool blend (keyed by canonical lowercase tool
    names). Falls back to equal weights over the tools present. Returns None if no
    weighted tool is available. Accepts either key casing (see aliases).
    """
    vals = [(_def_tool(def_tools, k), w) for k, w in weights.items()]
    present = [(float(v), w) for v, w in vals if v is not None]
    if not present:
        return None
    tw = sum(w for _, w in present)
    if tw <= 0:  # no calibrated weights for the present tools — equal-weight them
        return sum(v for v, _ in present) / len(present)
    return sum(v * (w / tw) for v, w in present)


def calibrate_multitool_curve(
    tools: tuple[str, ...],
    tool_rows: list[dict[str, float]],
    observed_runs: list[float],
    shrink: float = 0.6,
    min_n: int = 8,
    center_grade: Optional[float] = None,
    weight_prior: Optional[dict[str, float]] = None,
    prior_strength: float = 40.0,
) -> Optional[dict[str, Any]]:
    """Fit a multi-tool defensive-runs curve (catcher / infield).

    Generalizes the single-range-tool curve to a weighted blend of several tools:
      1) derive per-tool blend weights from each tool's standalone correlation
         with observed defensive runs (negatives floored to 0),
      2) SHRINK those weights toward `weight_prior` by sample size
         (n/(n+prior_strength)) so thin per-position/per-league cells fall back to
         the cross-league prior instead of fitting noise,
      3) build the blended grade and fit a centered/clamped linear grade->runs
         curve on it (reusing calibrate_grade_runs_curve).

    `tool_rows`: list of normalized-grade dicts keyed by `tools` (aligned with
    `observed_runs`). Returns a curve dict shaped for ``fielding_runs`` (with a
    "weights" sub-dict + "multitool": True), or None if the sample is too thin.
    """
    if len(observed_runs) < min_n:
        return None
    n = len(observed_runs)
    # 1) data-driven weights from each tool's regression SLOPE (runs per rating
    # point) — NOT correlation. Correlation measures how much of the run VARIANCE
    # a tool explains, which conflates importance with noise: a position where a
    # tool drives large but noisy run swings gets a LOW correlation despite the
    # tool mattering more (e.g. CF range vs corner — equal runs-per-point, but CF
    # outcomes are noisier, so correlation wrongly down-weighted CF range). Slope
    # is the right quantity: "how many runs does a point of this tool buy." Scaled
    # by the tool's rating SD so the weight reflects realized run spread, then
    # floored at 0 (a tool with no positive marginal value gets no weight).
    data_w: dict[str, float] = {}
    for k in tools:
        pairs = [(r[k], observed_runs[i]) for i, r in enumerate(tool_rows)
                 if r.get(k) is not None]
        if len(pairs) >= min_n:
            xs = [p[0] for p in pairs]
            ys = [p[1] for p in pairs]
            _, slope, _ = _ols_1var(xs, ys)
            sd = _pstdev(xs)
            data_w[k] = max(0.0, slope) * sd  # runs of spread this tool contributes
    dsum = sum(data_w.values())
    if dsum > 0:
        data_w = {k: v / dsum for k, v in data_w.items()}
    # 2) shrink toward the prior by sample size.
    if weight_prior:
        alpha = n / (n + prior_strength)  # 0..1 weight on the data fit
        merged = {}
        for k in tools:
            dv = data_w.get(k, 0.0)
            pv = weight_prior.get(k, 0.0)
            merged[k] = alpha * dv + (1 - alpha) * pv
        weights = merged
    elif data_w:
        weights = data_w
    else:
        weights = {k: 1.0 / len(tools) for k in tools}
    wsum = sum(weights.values()) or 1.0
    weights = {k: round(v / wsum, 3) for k, v in weights.items()}
    # 3) blended grade + centered/clamped linear fit.
    blended = [_blended_def_grade(r, weights) for r in tool_rows]
    gx = [g for g in blended if g is not None]
    gy = [observed_runs[i] for i, g in enumerate(blended) if g is not None]
    if len(gx) < min_n:
        return None
    curve = calibrate_grade_runs_curve(gx, gy, FIELDING_PRIOR_SLOPE,
                                       shrink=shrink, min_n=min_n,
                                       center_grade=center_grade)
    if curve is None:
        return None
    curve["weights"] = weights
    curve["multitool"] = True
    return curve
```


## B.5 surplus.py — FV to peak WAR ladder and prospect dollar value (selected)
```python
FV_TO_PEAK_WAR_DEFAULT: dict[int, float] = {
    80: 10.0, 70: 7.0, 65: 5.5, 60: 4.2, 55: 2.9, 50: 2.0, 45: 1.2, 40: 0.5,
}
FV_TO_PEAK_WAR_RP_DEFAULT: dict[int, float] = {
    80: 3.2, 70: 2.6, 65: 2.3, 60: 2.0, 55: 1.6, 50: 1.2, 45: 0.8, 40: 0.5,
}
FV_SUB40_WAR_LADDER: dict[int, float] = {
    35: 0.0, 30: -0.6, 25: -1.2, 20: -2.0,
}
PROSPECT_DISCOUNT_RATE: float = 0.05
LEVEL_AGE_DISCOUNT_RATE: float = 0.04
RP_POT_DISCOUNT: float = 0.85
TOOL_TRANSFORM_LOW_THRESHOLD: float = 40.0
TOOL_TRANSFORM_HIGH_THRESHOLD: float = 60.0
TOOL_TRANSFORM_LOW_PENALTY: float = 1.5
TOOL_TRANSFORM_HIGH_BONUS: float = 1.3
MLB_TOOL_FLOOR: int = 35
FLOOR_PENALTY_RATE: float = 0.25
HITTER_IMBALANCE_SPREAD_THRESHOLD: int = 25
HITTER_IMBALANCE_SPREAD_RATE: float = 0.15
HITTER_IMBALANCE_WEAKNESS_THRESHOLD: int = 45
HITTER_IMBALANCE_WEAKNESS_RATE: float = 0.12
PITCHER_IMBALANCE_SPREAD_THRESHOLD: int = 20
PITCHER_IMBALANCE_SPREAD_RATE: float = 0.20
PITCHER_IMBALANCE_WEAKNESS_THRESHOLD: int = 50
PITCHER_IMBALANCE_WEAKNESS_RATE: float = 0.15
DEFENSIVE_WEIGHTS: dict[str, dict[str, float]] = {
    "C":      {"CFrm": 0.45, "CBlk": 0.35, "CArm": 0.20},
    "SS":     {"IFR": 0.40, "IFE": 0.20, "IFA": 0.20, "TDP": 0.20},
    "2B":     {"IFR": 0.35, "TDP": 0.30, "IFE": 0.20, "IFA": 0.15},
    "3B":     {"IFA": 0.35, "IFE": 0.30, "IFR": 0.25, "TDP": 0.10},
    "CF":     {"OFR": 0.55, "OFE": 0.25, "OFA": 0.20},
    "COF_LF": {"OFR": 0.50, "OFE": 0.30, "OFA": 0.20},
    "COF_RF": {"OFR": 0.40, "OFA": 0.35, "OFE": 0.25},
}


POSITIONAL_WAR_ADJUSTMENTS: dict[str, float] = {
    "C": 1.2, "SS": 0.7, "2B": 0.3, "CF": 0.25, "3B": 0.2,
    "COF": -0.7, "1B": -1.2, "DH": -1.7, "SP": 0.0, "RP": -1.0,
}
LEVEL_NORM_AGE: dict[str, int] = {
    "draft": 18, "mlb": 26, "aaa": 26, "aa": 24, "a": 22,
    "a-short": 21, "rookie": 19, "usl": 19, "dsl": 18, "intl": 17,
}
DEVELOPMENT_DISCOUNT: dict[str, float] = {
    "mlb": 1.00, "aaa": 0.88, "aa": 0.78, "a": 0.68,
    "a-short": 0.55, "rookie": 0.45, "usl": 0.45, "dsl": 0.45, "intl": 0.35,
}


def _interp_dict(tbl: dict[int, float], value: float) -> float:
    """Interpolate from a sorted {key: value} dict."""
    pts = sorted(tbl.keys())
    if value >= pts[-1]:
        return tbl[pts[-1]]
    if value <= pts[0]:
        return tbl[pts[0]]
    for i in range(len(pts) - 1):
        if pts[i] <= value <= pts[i + 1]:
            t = (value - pts[i]) / (pts[i + 1] - pts[i])
            return tbl[pts[i]] + t * (tbl[pts[i + 1]] - tbl[pts[i]])
    return tbl[pts[0]]


def peak_war_from_fv(
    fv: float,
    bucket: str,
    weights: Optional[ModelWeights] = None,
) -> float:
    """Map FV grade to expected peak WAR/year.

    Uses position-specific tables when calibrated (SP, RP, per-hitter-bucket).

    Args:
        fv: FV grade (continuous, supports interpolation).
        bucket: Positional bucket.
        weights: Calibrated model weights (for per-position tables).

    Returns:
        Expected peak WAR per season at this FV.
    """
    if weights is not None:
        if bucket == "RP":
            return _interp_dict(weights.fv_to_peak_war_rp, fv)
        elif bucket == "SP":
            return _interp_dict(weights.fv_to_peak_war_sp, fv)
        else:
            by_pos = weights.fv_to_peak_war_by_pos
            if by_pos and bucket in by_pos:
                return _interp_dict(by_pos[bucket], fv)
            return _interp_dict(weights.fv_to_peak_war, fv)

    # Defaults
    if bucket == "RP":
        return _interp_dict(FV_TO_PEAK_WAR_RP_DEFAULT, fv)
    return _interp_dict(FV_TO_PEAK_WAR_DEFAULT, fv)


def fv_from_peak_war(
    peak_war: float,
    bucket: str,
    weights: Optional[ModelWeights] = None,
    war_floor: Optional[float] = None,
) -> float:
    """Invert the FV→peak-WAR ladder: map a projected peak WAR to a continuous FV.

    The inverse of peak_war_from_fv, used by the ceiling-anchored FV grade (which
    projects a WAR from potential ratings and reads off the grade). Builds the
    per-position FV→WAR ladder (40..70/80) and extends it below 40 with the
    industry role ladder (FV_SUB40_WAR_LADDER: 35 AAAA, 30 org, 25/20 fringe) so
    below-replacement talent grades 35/30/25/20 instead of flooring at 40.

    Args:
        peak_war: Projected (already risk-discounted + saturated) peak WAR.
        bucket: Positional bucket.
        weights: Calibrated model weights (per-position FV→WAR tables).
        war_floor: Optional WAR corresponding to the FV-20 floor (per-league
            run-space saturation bottom cap). Overrides the default 20-anchor.

    Returns:
        Continuous FV grade (interpolated), clamped to [20, 80].
    """
    # Upper ladder (40+) from the calibrated per-position table.
    pts: list[tuple[float, float]] = []
    for fv in (40, 45, 50, 55, 60, 65, 70, 80):
        pts.append((float(fv), peak_war_from_fv(float(fv), bucket, weights)))
    # Sub-40 role ladder (industry role scale), with an optional per-league floor.
    for fv, war in FV_SUB40_WAR_LADDER.items():
        w = war
        if fv == 20 and war_floor is not None:
            w = war_floor
        pts.append((float(fv), float(w)))
    # Sort ascending by WAR and interpolate FV from the WAR axis.
    pts.sort(key=lambda t: t[1])
    war = max(pts[0][1], min(pts[-1][1], peak_war))
    if war <= pts[0][1]:
        return pts[0][0]
    if war >= pts[-1][1]:
        return pts[-1][0]
    for i in range(len(pts) - 1):
        f0, w0 = pts[i]
        f1, w1 = pts[i + 1]
        if w0 <= war <= w1:
            t = (war - w0) / (w1 - w0) if w1 > w0 else 0.0
            return max(20.0, min(80.0, f0 + t * (f1 - f0)))
    return pts[0][0]


def prospect_surplus(
    fv: float,
    age: int,
    level: str,
    bucket: str,
    dpw: int,
    min_sal: int,
    ovr: Optional[int] = None,
    pot: Optional[int] = None,
    def_rating: Optional[int] = None,
    positional_adjust: bool = False,
    perpetual_arb: bool = False,
    perp_model: Optional[dict[str, Any]] = None,
    weights: Optional[ModelWeights] = None,
    scarcity_table: Optional[dict[int, float]] = None,
) -> dict[str, Any]:
    """Compute surplus value for a prospect over their 6-year control period.

    Args:
        fv: Continuous FV grade (supports fractional for interpolation).
        age: Player's current age.
        level: Current level string (e.g., "AAA", "AA").
        bucket: Positional bucket.
        dpw: Dollars per WAR.
        min_sal: League minimum salary.
        ovr: Current composite score (optional, for realization blend).
        pot: Ceiling score (optional, for scarcity).
        def_rating: Defensive rating (optional, for scarcity adjustment).
        positional_adjust: Whether to add positional WAR adjustment.
        perpetual_arb: Whether this is a perpetual arb league.
        perp_model: Perpetual arb model parameters.
        weights: Calibrated model weights.
        scarcity_table: Override scarcity table.

    Returns:
        Dict with total_surplus, breakdown, and component multipliers.
    """
    # Determine years to MLB
    effective_level = level
    if level.lower() in ("draft", "dsl", "intl", "college", "hs") and ovr is not None:
        if ovr >= 50:
            effective_level = "AAA"
        elif ovr >= 42:
            effective_level = "AA"
        elif ovr >= 35:
            effective_level = "A"
        else:
            effective_level = "A-Short"

    ytm_key = _LEVEL_ALIAS.get(effective_level.lower(), effective_level)
    years_out = YEARS_TO_MLB.get(ytm_key.lower(), 3.5)
    debut_age = age + years_out

    pw = peak_war_from_fv(fv, bucket, weights)
    pos_adj = POSITIONAL_WAR_ADJUSTMENTS.get(bucket, 0.0) if positional_adjust else 0.0

    # Near-maxed prospect blend
    if ovr and pot and pot > 0:
        realization = ovr / pot
        ovr_war = peak_war_from_score(ovr, bucket, weights)
        if realization > 0.7 and ovr_war < pw:
            blend_w = max(0.0, (realization - 0.7) / 0.3) ** 2
            pw = pw * (1 - blend_w) + ovr_war * blend_w

    rows: list[dict[str, Any]] = []
    total_surplus = 0.0
    dev_discount = age_adjusted_discount(level, age, ovr=ovr)

    for yr in range(6):
        ctrl_year = yr + 1
        player_age = debut_age + yr
        discount = (1 - PROSPECT_DISCOUNT_RATE) ** (years_out + yr)
        ramp = PROSPECT_WAR_RAMP.get(ctrl_year, 1.0)

        war = (pw + pos_adj) * aging_mult(player_age, bucket) * ramp
        mkt_val = market_value(war, dpw, min_sal) * discount

        # Salary projection
        if perpetual_arb:
            cum_war = sum(r["war"] for r in rows) + war
            salary = arb_salary_perpetual(
                int(player_age), war, dpw, min_sal,
                career_war=cum_war, model=perp_model,
            )
        elif ctrl_year <= 3:
            salary = min_sal
        else:
            arb_yr = ctrl_year - 3
            arb_ovr = max(40, min(75, int((pw + pos_adj) / 0.19 + 50)))
            if arb_yr == 1:
                salary = arb_salary(arb_ovr, bucket, 1, min_sal, min_sal)
            else:
                prior_sal = rows[-1]["salary"]
                salary = arb_salary(arb_ovr, bucket, arb_yr, prior_sal, min_sal)

        surplus = mkt_val - salary * discount
        total_surplus += surplus

        rows.append({
            "control_year": ctrl_year,
            "player_age": round(player_age, 1),
            "war": round(war, 2),
            "market_value": round(mkt_val),
            "salary": round(salary),
            "surplus": round(surplus),
        })

    cert_mult = certainty_multiplier(ovr or 0, pot or 0)
    scar_mult = scarcity_multiplier(
        float(pot) if pot else fv, bucket=bucket,
        def_rating=def_rating, scarcity_table=scarcity_table,
    )
    combined = dev_discount * cert_mult * scar_mult
    base_surplus = max(0, round(total_surplus * combined))

    for r in rows:
        r["surplus"] = round(r["surplus"] * combined)

    return {
        "fv": fv, "bucket": bucket, "level": level, "age": age,
        "years_to_mlb": years_out, "debut_age": round(debut_age, 1),
        "dev_discount": dev_discount, "certainty_mult": cert_mult,
        "scarcity_mult": scar_mult,
        "total_surplus": base_surplus, "breakdown": rows,
    }
```


## B.6 dev_speed.py — development-pace tags (complete file)
```python
"""Prospect development-speed metric — pure computation.

Measures how fast the developmentally-relevant component (offensive grade for
hitters, composite for pitchers) is moving over a trailing window, relative to
same-bucket / same-age-band / same-league peers, as a z-score. Qualified by the
POT gap and its ΔOVR-vs-ΔPOT decomposition, and gated / graded by a confidence
tier (history length, scouting accuracy, playing time).

Design + validation: `.kiro/specs/development-speed-metric/design.md`.

This module is pure: it operates on pre-loaded snapshot rows and a pre-built
baseline. The DB loading, bucketing (via ``assign_bucket``), and persistence live
in the data pipeline (``fv_calc``).

Public API:
    component_delta(snaps, key) -> (annual_delta|None, first, last)
    build_baseline(records) -> {(bucket, band): (mean, sd, n)}
    dev_speed_z(annual_move, bucket, band, baseline) -> (z|None, group|None)
    classify(z, gap, signal) -> (label, css_class, note)
    confidence_tier(window_years, n_snaps, acc, playing_time, is_pitcher) -> str
    age_band(age) -> str
    compute_dev_speed(...) -> dict   # the top-level per-player result
"""

from __future__ import annotations

import statistics as _st
from datetime import date
from typing import Any, Optional

from statsplusplus.evaluation.constants import PEAK_AGE_HITTER, PEAK_AGE_PITCHER

# --- Tunable parameters (validated defaults; may move to model_weights later) ---
WINDOW_MONTHS = 12          # trailing window for "current" dev pace
MIN_SNAPSHOTS = 3           # minimum points in the window to compute
MIN_WINDOW_DAYS = 150       # ~5 months minimum span (also the reporting gate)
MIN_GROUP_N = 20            # minimum peers to form a baseline cell
AGE_BAND_WIDTH = 2
MAX_BASELINE_AGE = 25       # prospect age ceiling for the baseline population
WIDE_GAP = 12               # POT-gap threshold for the "upside" qualifier

# Reporting floor (below this the signal is too noisy to show a verdict).
# MIN_COMPOSITE_REPORT is on the run-space composite scale (Session 97
# decompression + per-position centering). The runs→composite map display-clamps
# the bottom at 20, so a composite at/near 20 is floored and its movement is
# noise. 25 sits just above that clamp: it excludes the clamped mass while
# admitting essentially the full FV-45+ prospect population (whose trajectory is
# meaningful) across all leagues. Recalibrated from 38 (an old-compressed-scale
# value that, post-decompression, suppressed ~85-90% of prospects).
MIN_AGE_REPORT = 18
MIN_COMPOSITE_REPORT = 25

# Confidence-tier thresholds.
_CONF_PA_FULL = 250         # PA (hitter) for full playing-time credit in the window
_CONF_IP_FULL = 60          # IP (pitcher)

# --- Schedule/risk tag (2026-09-29) — SECOND, independent signal from the
# z-score label above. Built from a real case (Ethan Wilson, PPL): the
# z-score correctly showed him out-developing a DECLINING peer baseline, but
# that alone doesn't say whether he's actually closing his own gap to
# ceiling, across all his tools, before his runway to peak age runs out. See
# schedule_tag() below for how these combine; each threshold here is
# independently tunable. ---
HITTER_TOOLS = ("cntct", "gap", "pow", "eye")
PITCHER_TOOLS = ("stf", "mov", "ctrl")
STAGNANT_EPS = 1.0          # abs(annual tool movement) below this = stagnant
GAP_SMOOTH_POINTS = 3       # trailing snapshots averaged for gap_smoothed
NEAR_PEAK_YEARS = 1.5       # "near/past peak" runway threshold
SHORT_RUNWAY_YEARS = 3.0    # "limited runway" threshold (softer than near-peak)
GAP_NOT_CLOSING = -0.5      # annualized gap_trend at/above this = "not closing"
AHEAD_GAP_CLOSED_PCT = 0.15 # >15%/yr of the start-of-window gap closed
BEHIND_GAP_CLOSED_PCT = 0.05
AT_RISK_SCORE = 4           # composite risk-score threshold for "at_risk"


DRIFT_MIN_N = 15


def neutralize_scale_drift(by_player: dict, group_of: dict, keys=("composite_score", "ceiling_score", "offensive_grade")) -> None:
    """Put every snapshot of every player's history on the LATEST model scale, in place.

    ratings_history is not on one scale: when the evaluation model changes, or
    the engine re-scores only a player's newest row, composites step by several
    points for everybody at once while the underlying tools haven't moved. Raw
    composite deltas then read as development (or decline) that never happened
    and poison gap-closure / gap-trend. A step is measured, per dev group and
    per snapshot, as the median change in `key` among players whose tools were
    IDENTICAL between consecutive snapshots (their true change is ~0), then
    accumulated and subtracted so older rows are expressed on today's scale.
    """
    from collections import defaultdict
    tool_keys = ("cntct", "gap", "pow", "eye", "stf", "mov", "ctrl")
    for key in keys:
        steps = defaultdict(list)   # (group, date_next) -> [delta among unchanged-tool players]
        for pid, snaps in by_player.items():
            g = group_of.get(pid)
            for a, b in zip(snaps, snaps[1:]):
                if a.get(key) is None or b.get(key) is None:
                    continue
                if all(a.get(t) == b.get(t) for t in tool_keys):
                    steps[(g, b["snapshot_date"])].append(b[key] - a[key])
        by_group_dates = defaultdict(dict)
        for (g, d), v in steps.items():
            if len(v) >= DRIFT_MIN_N:
                by_group_dates[g][d] = _st.median(v)
        for g, dd in by_group_dates.items():
            dates = sorted(dd)
            cum, run = {}, 0.0
            for d in dates:
                run += dd[d]
                cum[d] = run
            total = run
            def cum_at(d):
                c = 0.0
                for dt in dates:
                    if dt <= d:
                        c = cum[dt]
                    else:
                        break
                return c
            for pid, snaps in by_player.items():
                if group_of.get(pid) != g:
                    continue
                for sn in snaps:
                    if sn.get(key) is not None:
                        sn[key] = sn[key] + (total - cum_at(sn["snapshot_date"]))


def peer_gap_cuts(results: list[dict]) -> dict:
    """{(dev_group, age_band): {behind_closed_cut, not_closing_cut}} from the
    population itself. The absolute gap-closure thresholds (5%/yr, trend >= -0.5)
    were calibrated on a scale-contaminated history and flagged ~half of ALL
    prospects; "behind" should mean slower than same-group, same-age peers:
    gap closure in the bottom quartile, "not closing" = gap trend in the top
    quartile (widening fastest). Cells under MIN_GROUP_N fall back to the
    absolute defaults.
    """
    from collections import defaultdict
    closed, trend = defaultdict(list), defaultdict(list)
    for r in results:
        k = (dev_group(r["bucket"]), r["band"])
        if r.get("gap_closed_pct_yr") is not None:
            closed[k].append(r["gap_closed_pct_yr"])
        if r.get("gap_trend") is not None:
            trend[k].append(r["gap_trend"])
    def q(v, f):
        v = sorted(v); return v[min(len(v) - 1, int(f * len(v)))]
    out = {}
    for k in set(closed) | set(trend):
        cut = {}
        if len(closed[k]) >= MIN_GROUP_N:
            cut["behind_closed_cut"] = min(BEHIND_GAP_CLOSED_PCT, q(closed[k], 0.25))
        if len(trend[k]) >= MIN_GROUP_N:
            cut["not_closing_cut"] = max(GAP_NOT_CLOSING, q(trend[k], 0.75))
        out[k] = cut
    return out


def age_band(age: int) -> str:
    lo = (int(age) // AGE_BAND_WIDTH) * AGE_BAND_WIDTH
    return f"{lo}-{lo + AGE_BAND_WIDTH - 1}"


def _years_between(d1: str, d2: str) -> float:
    y1, m1, day1 = map(int, d1.split("-"))
    y2, m2, day2 = map(int, d2.split("-"))
    return (date(y2, m2, day2) - date(y1, m1, day1)).days / 365.25


def component_delta(snaps: list[dict], key: str) -> tuple[Optional[float], Optional[float], Optional[float]]:
    """Annualized delta of a component over the sub-window where it's populated.

    Components (offensive_grade, defensive_value) are only written on snapshots
    where the eval engine ran, which can start later than the composite history.
    Returns (annual_delta or None, first_val, last_val).
    """
    pts = [s for s in snaps if s.get(key) is not None and s[key] > 0]
    if len(pts) < 2:
        return None, (pts[0][key] if pts else None), (pts[-1][key] if pts else None)
    f, l = pts[0], pts[-1]
    yrs = _years_between(f["snapshot_date"], l["snapshot_date"])
    if yrs * 365.25 < MIN_WINDOW_DAYS:
        return None, f[key], l[key]
    return (l[key] - f[key]) / yrs, f[key], l[key]


def dev_group(bucket: str) -> str:
    """Coarser grouping for the dev-speed *baseline*.

    Offensive development rate is essentially position-independent for hitters
    (validated: 1B/2B/3B/SS/CF/COF offensive-dev means cluster within ~0.5/yr),
    so we do NOT slice hitters by fielding position — that only thins the sample.
    The one meaningful exception is catcher, whose bat develops slower (workload/
    defensive-demand drag). Pitchers keep the SP/RP split (real, validated).

    Groups: "SP", "RP", "C", "HIT" (all non-catcher hitters).
    The player's true position bucket is still used for display, not the baseline.
    """
    if bucket in ("SP", "RP"):
        return bucket
    if bucket == "C":
        return "C"
    return "HIT"


def build_baseline(records: list[dict]) -> dict[str, dict[tuple, tuple]]:
    """Per (dev_group, age-band) mean/SD for each dev signal.

    Returns {signal: {(group, band): (mean, sd, n)}} for signal in {"comp","off"}
    — annualized movement of composite / offensive grade. `group` is the coarse
    dev_group (SP/RP/C/HIT), not the fine fielding bucket — see dev_group().
    A (group, "ALL") all-ages fallback cell is also emitted for tiny leagues.
    """
    field = {"comp": "annual_comp", "off": "annual_off"}
    out: dict[str, dict[tuple, tuple]] = {}
    from collections import defaultdict
    for sig, fld in field.items():
        fine: dict[tuple, list] = defaultdict(list)
        grp_all: dict[str, list] = defaultdict(list)
        for r in records:
            if r["age"] <= MAX_BASELINE_AGE and r.get(fld) is not None:
                g = dev_group(r["bucket"])
                fine[(g, r["band"])].append(r[fld])
                grp_all[g].append(r[fld])
        b = {}
        for key, vals in fine.items():
            if len(vals) >= MIN_GROUP_N:
                b[key] = (round(_st.mean(vals), 3), round(_st.pstdev(vals) or 1.0, 3), len(vals))
        for g, vals in grp_all.items():
            if len(vals) >= MIN_GROUP_N:
                b[(g, "ALL")] = (round(_st.mean(vals), 3), round(_st.pstdev(vals) or 1.0, 3), len(vals))
        out[sig] = b
    return out


def primary_signal(bucket: str, has_off: bool) -> str:
    """Which dev signal headlines this player: offensive for hitters (defense is
    experience-inflated and often maxed), composite for pitchers."""
    is_hitter = bucket not in ("SP", "RP")
    return "off" if (is_hitter and has_off) else "comp"


def dev_speed_z(annual_move: Optional[float], bucket: str, band: str,
                sig: str, baseline: dict) -> tuple[Optional[float], Optional[tuple]]:
    if annual_move is None:
        return None, None
    b = baseline.get(sig, {})
    g = dev_group(bucket)
    # (group, band) → (group, all ages) fallback for tiny leagues
    for key in ((g, band), (g, "ALL")):
        if key in b:
            mean, sd, n = b[key]
            if sd <= 0:
                return None, (mean, sd, n)
            return round((annual_move - mean) / sd, 2), (mean, sd, n)
    return None, None


def classify(z: Optional[float], gap: int, sig: str) -> tuple[str, str, str]:
    """(label, css_class, note) from z (pace vs peers) qualified by POT gap.

    css_class in {rising, onpace, watch, stalled, regressing, none}.
    Labels are descriptive (observation, not verdict).
    """
    if z is None:
        return "—", "none", ""
    what = "bat" if sig == "off" else "overall"
    wide = (gap or 0) >= WIDE_GAP
    if z >= 1.0:
        if wide:
            return "Rising (room)", "rising", f"{what} developing, ceiling room remains"
        return "Rising (realizing)", "rising", f"{what} developing, near ceiling"
    if z <= -1.0:
        if wide:
            return "Stalled — upside at risk", "stalled", f"{what} not tracking a wide ceiling gap"
        return "Regressing/plateaued", "regressing", f"{what} flat/declining, near his level"
    if z <= -0.5 and wide:
        return "Watch — slow vs upside", "watch", f"{what} slow vs a wide ceiling gap"
    return "On pace", "onpace", f"{what} developing about as expected"


def confidence_tier(window_years: float, n_snaps: int, acc: Optional[str],
                    playing_time: float, is_pitcher: bool) -> str:
    """High / Medium / Low confidence in the reading, from the factors validation
    showed matter: window length + snapshot count, scouting accuracy, playing time.
    """
    score = 0
    # window length / density
    if window_years >= 1.0 and n_snaps >= 6:
        score += 2
    elif window_years >= 0.6 and n_snaps >= 4:
        score += 1
    # scouting accuracy
    a = (acc or "").upper()
    if a in ("VH", "H"):
        score += 1
    elif a in ("L", "VL"):
        score -= 1
    # playing time in the window
    full = _CONF_IP_FULL if is_pitcher else _CONF_PA_FULL
    if playing_time >= full:
        score += 1
    elif playing_time < full * 0.4:
        score -= 1
    if score >= 3:
        return "High"
    if score >= 1:
        return "Medium"
    return "Low"


def stagnant_tools(window: list[dict], bucket: str) -> list[str]:
    """Tool keys (from HITTER_TOOLS or PITCHER_TOOLS) whose annualized
    movement across the window is within STAGNANT_EPS of zero.

    A tool-blended signal like offensive_grade can rise on one hot tool
    while others sit completely flat — the aggregate reads as broad-based
    progress when it isn't. Ignores tools without 2+ populated points in
    the window (no data ≠ stagnant); requires the same MIN_WINDOW_DAYS span
    component_delta() already enforces.
    """
    tools = PITCHER_TOOLS if bucket in ("SP", "RP") else HITTER_TOOLS
    out = []
    for t in tools:
        delta, first, last = component_delta(window, t)
        if delta is not None and abs(delta) < STAGNANT_EPS:
            out.append(t)
    return out


def _gap_series(window: list[dict]) -> list[tuple[str, float]]:
    """[(snapshot_date, ceiling_score - composite_score), ...] for snapshots
    where both are populated. Uses ceiling_score (not true_ceiling) even
    though true_ceiling is the app's preferred *current* ceiling estimate
    (see compute_dev_speed's headline `gap`) — true_ceiling only exists
    going forward from the migration that added it to ratings_history, so
    mixing it into a historical series would silently jump-discontinuity
    the trend the day that column started populating. ceiling_score has
    been tracked the whole time and is internally consistent across it.
    """
    out = []
    for s in window:
        c, g = s.get("composite_score"), s.get("ceiling_score")
        if c is not None and g is not None and c > 0:
            out.append((s["snapshot_date"], max(0, g - c)))
    return out


def gap_smoothed_and_trend(window: list[dict]) -> tuple[Optional[float], Optional[float]]:
    """(gap_smoothed, gap_trend) — trailing-average gap (last
    GAP_SMOOTH_POINTS snapshots) and its annualized change across the window
    (first GAP_SMOOTH_POINTS avg -> last GAP_SMOOTH_POINTS avg). Negative
    trend = gap closing (good); positive = widening.

    Smoothing matters because ceiling_score itself is noisy month to month
    (a scouting-report wobble, not real ceiling movement) — a point-in-time
    gap reading can flip the WIDE_GAP qualifier on pure noise.
    """
    series = _gap_series(window)
    if len(series) < 2:
        return None, None
    vals = [v for _, v in series]
    smoothed = _st.mean(vals[-GAP_SMOOTH_POINTS:])
    first_avg = _st.mean(vals[:GAP_SMOOTH_POINTS])
    last_avg = _st.mean(vals[-GAP_SMOOTH_POINTS:])
    yrs = _years_between(series[0][0], series[-1][0])
    if yrs <= 0:
        return round(smoothed, 2), None
    return round(smoothed, 2), round((last_avg - first_avg) / yrs, 2)


def schedule_tag(
    *, z: Optional[float], gap: int, gap_first: Optional[float], gap_closed_pct_yr: Optional[float],
    gap_trend: Optional[float], stagnant: list[str], age: int, bucket: str,
    peak_age_hitter: int = PEAK_AGE_HITTER, peak_age_pitcher: int = PEAK_AGE_PITCHER,
    behind_closed_cut: float = BEHIND_GAP_CLOSED_PCT, not_closing_cut: float = GAP_NOT_CLOSING,
) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """(status, label, note) — the schedule/risk tag. Independent of
    classify()'s peer-relative label; combines age runway to typical peak,
    whether the gap to ceiling is actually closing (not just out-relative-
    to-a-declining-peer-baseline), and tool-breadth.

    status in {"ahead", "behind", "at_risk", "on_track", None}. None means
    not enough signal to tag at all (no gap history, etc.) — callers should
    treat that the same as not reportable, not as "on_track".
    """
    if z is None:
        return None, None, None
    peak = peak_age_pitcher if bucket in ("SP", "RP") else peak_age_hitter
    years_to_peak = peak - age
    near_peak = years_to_peak <= NEAR_PEAK_YEARS
    short_runway = years_to_peak <= SHORT_RUNWAY_YEARS
    wide_gap = gap >= WIDE_GAP
    not_closing = gap_trend is not None and gap_trend >= not_closing_cut
    n_stagnant = len(stagnant)
    is_pitcher = bucket in ("SP", "RP")
    # Pitchers have only three tools, and grades move in 5-pt steps, so two flat
    # tools is the NORMAL state of a healthy pitcher (~half of 21-yo SPs were
    # flat-or-down over a year). Count stagnation for them only when all three
    # are flat AND the aggregate pace is below peers.
    if is_pitcher and not (n_stagnant >= 3 and z < 0):
        n_stagnant = 0

    risk = 0
    reasons = []
    if near_peak and wide_gap:
        risk += 2; reasons.append("near peak age with a wide unclosed gap")
    elif short_runway and wide_gap:
        risk += 1; reasons.append("limited runway to peak with a wide gap")
    if wide_gap and not_closing:
        risk += 2; reasons.append("gap isn't closing")
    if n_stagnant >= 3:
        risk += 2; reasons.append(f"{n_stagnant} tools stagnant")
    elif n_stagnant == 2:
        risk += 1; reasons.append("2 tools stagnant")

    if risk >= AT_RISK_SCORE:
        return "at_risk", "At Risk", "; ".join(reasons) + " — real chance this doesn't come together"

    ahead = (z >= 1.0 and n_stagnant <= 1
             and gap_closed_pct_yr is not None and gap_closed_pct_yr > AHEAD_GAP_CLOSED_PCT)
    if ahead:
        return "ahead", "Ahead of Schedule", "outpacing peers with the gap genuinely closing, broadly across tools"

    # NOTE: tool stagnation alone is NOT a standalone trigger — validated
    # against real data (2026-09-29): most tools move less than 1 grade
    # point in any given year even for healthy, normally-developing
    # prospects, so a blanket "2+ flat tools" rule flagged ~84% of the
    # reportable pool "behind", which is meaningless as a discriminator.
    # Stagnant tools only matter here when they CONTRADICT an otherwise
    # decent z-score — the actual pattern this tag exists to catch (a
    # player whose aggregate/peer-relative read looks fine because of one
    # hot tool, while the rest of his profile hasn't moved) — matching the
    # Ethan Wilson case this whole feature was built from.
    stagnant_despite_ok_z = (not is_pitcher) and z is not None and z >= 0.5 and n_stagnant >= 2
    behind = (
        (z is not None and z <= -0.5)
        or (wide_gap and (gap_closed_pct_yr is None or gap_closed_pct_yr <= behind_closed_cut))
        or stagnant_despite_ok_z
    )
    if behind:
        note_bits = []
        if n_stagnant >= 2:
            note_bits.append(f"{n_stagnant} tools ({', '.join(stagnant)}) flat over the window")
        if wide_gap and (gap_closed_pct_yr is None or gap_closed_pct_yr <= behind_closed_cut):
            note_bits.append("gap to ceiling not meaningfully closing")
        if z is not None and z <= -0.5:
            note_bits.append("trailing same-age/bucket peers")
        return "behind", "Behind Schedule", "; ".join(note_bits) or "not tracking toward ceiling"

    return "on_track", "On Schedule", ""


def compute_dev_speed(
    *,
    bucket: str,
    age: int,
    window: list[dict],
    baseline: dict,
    acc: Optional[str] = None,
    playing_time: float = 0.0,
    peak_age_hitter: int = PEAK_AGE_HITTER,
    peak_age_pitcher: int = PEAK_AGE_PITCHER,
    rookie: bool = False,
    peer_cuts: Optional[dict] = None,
) -> Optional[dict[str, Any]]:
    """Top-level per-player dev-speed result, or None if the window is too short.

    `window` is the player's trailing-window snapshot rows (each a dict with
    snapshot_date, composite_score, ovr, pot, offensive_grade, defensive_value),
    already filtered/selected by the caller. `baseline` is from build_baseline.

    Returns a dict shaped for storage + display (see the spec / query layer):
        {z, signal, label, css_class, note, gap, d_ovr, d_pot,
         confidence, available, comp_first, comp_last, off_first, off_last, ...}
    Returns None when there is not enough history to compute at all.
    """
    if len(window) < MIN_SNAPSHOTS:
        return None
    first, last = window[0], window[-1]
    yrs = _years_between(first["snapshot_date"], last["snapshot_date"])
    if yrs * 365.25 < MIN_WINDOW_DAYS:
        return None

    d_comp = (last["composite_score"] - first["composite_score"]) / yrs
    d_off, off_f, off_l = component_delta(window, "offensive_grade")
    d_def, def_f, def_l = component_delta(window, "defensive_value")
    # Gap + trajectory use OUR model's composite/ceiling (not the game's OVR/POT),
    # consistent with the rest of the app. OVR/POT are NULL in OVR-less leagues
    # (e.g. PPL); composite/ceiling are always populated. d_ovr/d_pot are stored
    # under legacy names but represent current-composite / ceiling deltas.
    def _cur(s):
        return s.get("composite_score") if s.get("composite_score") else (s.get("ovr") or 0)
    def _ceil(s):
        return s.get("ceiling_score") if s.get("ceiling_score") else (s.get("pot") or 0)
    cur_last, cur_first = _cur(last), _cur(first)
    ceil_last, ceil_first = _ceil(last), _ceil(first)
    # Headline gap prefers true_ceiling on the latest point when available —
    # the app's own preferred ceiling estimate elsewhere (contract_value.
    # _resolve), and less conservative than ceiling_score alone (see the
    # true_ceiling migration note in db.py). d_pot / gap_first stay on
    # ceiling_score for both ends since true_ceiling has no history yet.
    ceil_last_display = last.get("true_ceiling") or ceil_last
    gap = max(0, ceil_last_display - cur_last)
    gap_first = max(0, ceil_first - cur_first)
    d_ovr = cur_last - cur_first       # Δ current ability (composite)
    d_pot = ceil_last - ceil_first     # Δ ceiling
    band = age_band(age)
    is_pitcher = bucket in ("SP", "RP")

    sig = primary_signal(bucket, has_off=d_off is not None)
    annual_move = d_off if sig == "off" else d_comp
    z, grp = dev_speed_z(annual_move, bucket, band, sig, baseline)

    comp_last = last["composite_score"]
    reportable = age >= MIN_AGE_REPORT and (comp_last or 0) >= MIN_COMPOSITE_REPORT
    available = reportable and z is not None

    conf = confidence_tier(yrs, len(window), acc, playing_time, is_pitcher)
    label, css, note = classify(z, gap, sig) if available else ("—", "none", "")

    stagnant = stagnant_tools(window, bucket) if available else []
    gap_smoothed, gap_trend = gap_smoothed_and_trend(window)
    gap_closed_pct_yr = round(d_ovr / gap_first, 3) if gap_first > 0 else None
    if available and not rookie:
        sched_status, sched_label, sched_note = schedule_tag(
            z=z, gap=gap, gap_first=gap_first, gap_closed_pct_yr=gap_closed_pct_yr,
            gap_trend=gap_trend, stagnant=stagnant, age=age, bucket=bucket,
            peak_age_hitter=peak_age_hitter, peak_age_pitcher=peak_age_pitcher,
            **(peer_cuts or {}),
        )
    else:
        sched_status = sched_label = sched_note = None

    return {
        "available": available,
        "z": z,
        "signal": sig,
        "label": label,
        "css_class": css,
        "note": note,
        "gap": gap,
        "d_ovr": d_ovr,
        "d_pot": d_pot,
        "confidence": conf,
        "annual_move": round(annual_move, 2) if annual_move is not None else None,
        "peer_mean": grp[0] if grp else None,
        "peer_sd": grp[1] if grp else None,
        "peer_n": grp[2] if grp else None,
        "comp_first": first["composite_score"], "comp_last": comp_last,
        "off_first": off_f, "off_last": off_l,
        "def_first": def_f, "def_last": def_l,
        "window_years": round(yrs, 2), "n_snaps": len(window),
        "band": band,
        "schedule_status": sched_status,
        "schedule_label": sched_label,
        "schedule_note": sched_note,
        "stagnant_tools": stagnant,
        "gap_closed_pct_yr": gap_closed_pct_yr,
        "bucket": bucket, "gap_trend": gap_trend,
        "years_to_peak": round((peak_age_pitcher if is_pitcher else peak_age_hitter) - age, 1),
        "gap_smoothed": gap_smoothed,
        "gap_trend": gap_trend,
    }

```


## B.7 ratings normalization and engine extraction helpers
```python
def norm(raw: Any, scale: str = "1-100") -> Optional[int]:
    """Normalize a tool rating to 20-80 scouting scale, rounded to nearest 5.

    Args:
        raw: Raw rating value (int, str, or None).
        scale: Rating scale — "1-100", "20-80", or "1-20".

    Returns:
        Normalized grade (20-80 in steps of 5), or None for invalid input.
    """
    if raw is None:
        return None
    try:
        raw_int = int(raw)
    except (ValueError, TypeError):
        return None
    if raw_int <= 0:
        return None

    if scale == "20-80":
        return int(max(20, min(80, round(raw_int / 5) * 5)))
    if scale == "1-20":
        val = 20.0 + (min(raw_int, 20) - 1) / 19.0 * 60.0
        return int(max(20, min(80, round(val / 5) * 5)))
    # 1-100 (default)
    return int(round((20 + (min(raw_int, 100) / 100) * 60) / 5) * 5)


def _extract_hitter_tools(row: dict, norm_fn) -> dict[str, float | None]:
    """Extract and normalize hitter tool ratings from a DB row.

    Handles L/R splits: when both _l and _r variants exist, computes
    weighted average (60% vs-RHP, 40% vs-LHP). Falls back to overall.

    Returns dict with keys: contact, gap, power, eye, speed, steal, stl_rt.
    """
    tool_map = {
        "contact": ("cntct", "cntct_r", "cntct_l"),
        "gap": ("gap", "gap_r", "gap_l"),
        "power": ("pow", "pow_r", "pow_l"),
        "eye": ("eye", "eye_r", "eye_l"),
    }
    tools: dict[str, float | None] = {}
    for key, (overall_col, r_col, l_col) in tool_map.items():
        r_val = norm_fn(row.get(r_col))
        l_val = norm_fn(row.get(l_col))
        if r_val is not None and l_val is not None:
            tools[key] = r_val * 0.6 + l_val * 0.4
        else:
            tools[key] = norm_fn(row.get(overall_col))

    tools["speed"] = norm_fn(row.get("speed"))
    tools["steal"] = norm_fn(row.get("steal"))
    tools["stl_rt"] = norm_fn(row.get("stl_rt"))
    return tools


def _extract_potential_hitter_tools(row: dict, norm_fn) -> dict[str, float | None]:
    """Extract potential hitter tool ratings from a DB row."""
    return {
        "contact": norm_fn(row.get("pot_cntct")),
        "gap": norm_fn(row.get("pot_gap")),
        "power": norm_fn(row.get("pot_pow")),
        "eye": norm_fn(row.get("pot_eye")),
        "speed": norm_fn(row.get("speed")),  # speed doesn't have a pot_ variant
        "steal": norm_fn(row.get("steal")),
        "stl_rt": norm_fn(row.get("stl_rt")),
    }


def _extract_pitcher_tools(row: dict, norm_fn) -> dict[str, float | None]:
    """Extract and normalize pitcher tool ratings from a DB row.

    When extended ratings are available (hra column), uses HRA as the
    "movement" value. HRA correlates r=0.927 with movement but is a cleaner
    signal — it directly measures home run prevention, which is the primary
    WAR driver that movement represents. The composite movement rating mixes
    HRA with ground ball tendency (PBABIP), diluting the signal.

    Includes stuff L/R splits for platoon balance detection.
    """
    tools: dict[str, float | None] = {}
    tools["stuff"] = norm_fn(row.get("stf"))
    tools["control"] = norm_fn(row.get("ctrl"))

    # Movement: use HRA when available (more predictive component)
    hra_val = norm_fn(row.get("hra"))
    if hra_val and hra_val > 20:
        tools["movement"] = hra_val
    else:
        tools["movement"] = norm_fn(row.get("mov"))

    # L/R splits for platoon balance penalty
    tools["stuff_l"] = norm_fn(row.get("stf_l"))
    tools["stuff_r"] = norm_fn(row.get("stf_r"))
    return tools


def _extract_potential_pitcher_tools(row: dict, norm_fn) -> dict[str, float | None]:
    """Extract potential pitcher tool ratings from a DB row."""
    tools = {
        "stuff": norm_fn(row.get("pot_stf")),
        "movement": norm_fn(row.get("pot_mov")),
        "control": norm_fn(row.get("pot_ctrl")),
    }
    # Extended ratings potential (pot_ when available, else current)
    for ext_key, pot_col, cur_col in [("hra", "pot_hra", "hra"), ("pbabip", "pot_pbabip", "pbabip")]:
        val = norm_fn(row.get(pot_col))
        if not val or val <= 20:
            val = norm_fn(row.get(cur_col))
        if val and val > 20:
            tools[ext_key] = val
    return tools


def _extract_arsenal(row: dict, norm_fn) -> dict[str, float]:
    """Extract pitch arsenal from a DB row. Returns {pitch_name: normalized_rating}."""
    arsenal: dict[str, float] = {}
    for col in _PITCH_COLS:
        raw = row.get(col)
        val = norm_fn(raw)
        if val is not None:
            arsenal[col] = val
    return arsenal


def _extract_potential_arsenal(row: dict, norm_fn) -> dict[str, float]:
    """Extract potential pitch arsenal. Uses pot_ columns, falls back to current."""
    arsenal: dict[str, float] = {}
    for col in _PITCH_COLS:
        pot_val = norm_fn(row.get("pot_" + col))
        if pot_val is not None:
            arsenal[col] = pot_val
        else:
            cur_val = norm_fn(row.get(col))
            if cur_val is not None:
                arsenal[col] = cur_val
    return arsenal


def _extract_defense_tools(row: dict, norm_fn=None) -> dict[str, float | None]:
    """Extract defensive tool ratings from a DB row, normalized to 20-80.

    Maps DB column names to the keys expected by DEFENSIVE_WEIGHTS
    (CFrm, CBlk, CArm, IFR, IFE, IFA, TDP, OFR, OFE, OFA).
    """
    raw = {
        "CFrm": row.get("c_frm"),
        "CBlk": row.get("c_blk"),
        "CArm": row.get("c_arm"),
        "IFR": row.get("ifr"),
        "IFE": row.get("ife"),
        "IFA": row.get("ifa"),
        "TDP": row.get("tdp"),
        "OFR": row.get("ofr"),
        "OFE": row.get("ofe"),
        "OFA": row.get("ofa"),
        "LF": row.get("lf"),
        "RF": row.get("rf"),
    }
    if norm_fn is None:
        return raw
    return {k: norm_fn(v) for k, v in raw.items()}


def _apply_milb_context(
    p: dict,
    conn,
    pid: int,
    milb_averages: dict,
    milb_discounts: dict,
    milb_norm_ages: dict,
    load_fn,
) -> None:
    """Apply MiLB stat context (PAC and risk modifier) to a player dict in-place."""
    from statsplusplus.evaluation.fv import (
        compute_performance_adjusted_ceiling,
        compute_stat_risk_modifier,
    )

    if not milb_averages:
        return
    milb_s = load_fn(conn, pid, p["_is_pitcher"], milb_averages)
    if not milb_s:
        return

    disc_key = "pitcher" if p["_is_pitcher"] else "hitter"
    weighted_sum = 0.0
    total_w = 0.0
    for ms in milb_s[:3]:
        lv = str(ms.get("level", 0))
        disc = float(milb_discounts.get(disc_key, {}).get(lv, 0.0))
        if disc <= 0:
            continue
        pa = ms.get("pa", 0) if not p["_is_pitcher"] else ms.get("ip", 0) * 4.3
        w = pa * disc
        weighted_sum += ms["stat_2080"] * w
        total_w += w

    if total_w > 0:
        stat_2080 = weighted_sum / total_w
        eff_pa = total_w
        level_str = str(int(p.get("level", 2)))
        norm_age_lv = int(milb_norm_ages.get(level_str, p["_norm_age"]))
        tool_only = p.get("composite_score") or p.get("Ovr") or 0

        p["Pot"] = compute_performance_adjusted_ceiling(
            p["Pot"], stat_2080, p["Age"], norm_age_lv, eff_pa, tool_only
        )
        p["_stat_risk_modifier"] = compute_stat_risk_modifier(
            stat_2080, p["Age"], norm_age_lv, eff_pa, tool_only
        )
```
