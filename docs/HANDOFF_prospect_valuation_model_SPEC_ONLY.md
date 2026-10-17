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



# APPENDIX B — omitted in this short version. The full file (HANDOFF_prospect_valuation_model.md) contains the verbatim reference source code.
