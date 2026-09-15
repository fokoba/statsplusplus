# PPL / eMLB Composite Score — Precise Calculation Reference

Prepared for handoff to another Claude session for player analysis and fit work.
This describes exactly how "Comp" (overall composite), "Comp vR" (vs-right-handed),
and "Comp vL" (vs-left-handed) are computed in this app, for both leagues:

- **PPL** — 1955, 20-80 in-game display scale, no DH, perpetual-arb (no free agency)
- **eMLB** — 2034, 1-100 in-game display scale, universal DH, standard FA/arbitration

Both leagues run through the **exact same formulas** (`compute_composite_hitter` /
`compute_composite_pitcher` in `src/statsplusplus/evaluation/composite.py`). What
differs between the two leagues is **only the calibrated inputs** — the tool
weights and per-tool transform curves — which are fit independently per league
from that league's own real historical stat outcomes. There is no era-specific
branching logic; the era-appropriateness comes entirely from calibration.

---

## 1. Universal first step: normalize to the 20-80 canonical scale

Every raw rating is converted to a canonical 20-80 float **before** anything else
happens, regardless of which scale the league displays ratings on:

- PPL displays natively on 20-80 — normalization is close to a no-op.
- eMLB displays on 1-100 — normalization rescales linearly to 20-80.

This means the composite math itself never needs to know which league it's
running for; by the time tools reach `compute_composite_hitter`/
`compute_composite_pitcher`, they're already on the same footing. (Function:
`norm_continuous` in `src/statsplusplus/config/ratings.py`.)

---

## 2. Hitter composite — exact algorithm

Inputs: `tools` (contact, gap, power, eye, speed, steal, stl_rt — all 20-80),
`weights` (this bucket's calibrated profile, see §5), `defense` (raw defensive
tool ratings), `def_weights` (this position's defensive-tool importance, see §6),
`transforms` (this league's per-tool calibrated curves, see §7).

### Step 1 — Tool compensation (small skill-interaction adjustment)
Before weighting, two below-average tools get a small boost if a *complementary*
tool is strong:
- If `power < 50`: boosted by 0.020 × (contact − 50) if contact > 50, plus 0.012 ×
  (eye − 50) if eye > 50 (a low-power hitter who makes a lot of contact/walks a
  lot does more with the power he has than the raw number suggests).
- If `eye < 50`: boosted by 0.020 × (contact − 50) if contact > 50.

### Step 2 — Per-tool transform curve
Each offensive tool (contact/gap/power/eye) is run through this league's
calibrated transform curve for that tool (§7) — a non-linear remapping fit from
real marginal-WAR data, e.g. an 80-contact hitter is worth *disproportionately*
more than a 60, and a 25-contact hitter is disproportionately worse than a 40.
If no curve exists, falls back to a generic piecewise curve: linear from 40-60,
×1.5 penalty below 40, ×1.3 bonus above 60.

### Step 3 — Weighted averages (three independent sub-scores)
- **Offensive raw** = weighted average of transformed contact/gap/power/eye,
  weights renormalized among whichever tools have real data.
- **Baserunning raw** = weighted average of speed/steal/stl_rt (no transform
  curve applied to these).
- **Defensive raw** = weighted average of this position's defensive tools (§6),
  each already normalized to 20-80.

  **Important**: if a tool category has zero present values (e.g. speed/steal
  missing), that whole sub-score returns `None` and is **dropped from the
  final blend entirely** — the weight is not renormalized into what remains.
  (This was the exact bug fixed on 2026-09-07 — see §9.)

### Step 4 — Recombination shares
The bucket's weight profile is collapsed into three shares that sum to 1.0:
```
defense_share = weights["defense"]
offense_share = (sum of offensive tool weights) / (offense+baserunning weight) × (1 − defense_share)
baserunning_share = (sum of baserunning tool weights) / (offense+baserunning weight) × (1 − defense_share)
```

### Step 5 — Contact-scaled baserunning boost
If contact > 50: baserunning_share is boosted (and offense_share correspondingly
reduced) by up to 100% extra, scaling with how far contact is above 50 (capped
at contact=80). A plus hit tool makes baserunning value more realizable (more
times on base to use the wheels).

### Step 6 — Elite defense boost
If the player's best individual defensive rating > 50: defense_share is boosted
similarly (up to +100% at a 80-rated defender), offense_share reduced to
compensate.

### Step 7 — Weighted recombination
```
raw = offensive_raw × offense_share + baserunning_raw × baserunning_share + defensive_raw × defense_share
```
(any `None` sub-score simply contributes 0 and its share is NOT redistributed —
see the warning in Step 3).

### Step 8 — Sub-MLB floor penalty
For every *offensive* tool (contact/gap/power/eye only) below 35: subtract
`(35 − value) × 0.25` from raw. Penalizes a disqualifying weakness beyond what a
linear weighted average would.

### Step 9 — Speed × contact synergy bonus
If speed > 45 AND contact > 50: add `0.10 × (speed − 45) × (contact / 60)` to
raw. A burner with a real hit tool produces more infield hits, extra bases, and
defensive pressure than a linear sum predicts.

### Step 10 — Tool-imbalance penalty
Among contact/gap/power/eye (only when ≥3 have real values): if
`max − min > 25`, subtract `(spread − 25) × 0.15`, plus for every one of those
tools below 45: subtract `(45 − value) × 0.12`. Penalizes one-dimensional
profiles (e.g. all-power-no-hit) beyond what the weighted average alone reflects.

### Step 11 — Clamp
`composite = round(raw)`, clamped to [20, 80].

---

## 3. Pitcher composite — exact algorithm

Inputs: `tools` (stuff, movement, control — 20-80), `weights` (SP or RP profile,
§5), `arsenal` (per-pitch-type ratings), `stamina`, `role` ("SP"/"RP"),
`transforms`.

### Step 1 — Tool compensation
Same idea as hitters: a below-average `control` gets a small boost if `stuff`
or `movement` is strong (specific coefficients in `_apply_pitcher_compensation`).

### Step 2 — Per-tool transform + weighted average
Stuff/movement/control each run through this league+role's calibrated curve
(§7), then weighted-averaged using the role's weight profile, scaled by
`(1 − arsenal_weight)` (arsenal_weight = 0.05 for both leagues/roles — pitch
arsenal depth/quality is a small, roughly constant factor).

### Step 3 — Arsenal bonus
```
pitches_45_plus = count of arsenal pitches rated ≥45
depth_bonus = min(3, max(0, pitches_45_plus − 3))     # rewards a 4th+ usable pitch
best_pitch = max(arsenal ratings)
quality_bonus = 2 if best_pitch ≥ 70, else 1 if ≥65, else 0
arsenal_score = clamp(50 + (depth_bonus + quality_bonus) × 5, 20, 80)
raw = tool_weighted_avg + arsenal_score × arsenal_weight
```

### Step 4 — Stamina adjustment (starters only)
- If `stamina < 40`: penalty up to −5 (rate 0.15/point below 40).
- If `stamina > 45`: bonus up to +4 (rate 0.12/point above 45).
(Relievers get neither — stamina isn't a differentiator for a 1-inning role.)

### Step 5 — Platoon-balance penalty
If both `stuff_l` and `stuff_r` are present and the weaker side < 35 with a gap
≥15 between them: subtract 2-3 (worse if the weak side is ≤25). Models a
pitcher whose repertoire is transparently exploitable by one-side platooning.

### Step 6 — Sub-MLB floor + tool-imbalance penalties
Same mechanics as hitters (§2 Steps 8/10), applied to stuff/movement/control
only. Floor threshold is the same (35, rate 0.25); imbalance threshold is
**20** (not 25) with rate 0.20, weakness threshold **50** (not 45) with rate
0.15 — pitchers are penalized more aggressively for a lopsided profile than
hitters are.

### Step 7 — Clamp
`composite = round(raw)`, clamped to [20, 80].

---

## 4. vR / vL (platoon-split) composite — what actually changes

**vR** = composite computed using this player's ratings *specifically against
right-handed opponents*; **vL** = specifically against lefties. Same exact
formulas as §2/§3 above — nothing about the algorithm changes, only which raw
inputs feed it:

- **Hitters**: contact/gap/power/eye are swapped for their handedness-split
  values (`Cntct vR`/`Cntct vL` etc. in the export, `cntct_r`/`cntct_l` etc. in
  the DB). **speed, steal, stl_rt, and defense are unchanged** between vR and
  vL — a player's legs and glove don't vary by which pitcher he's facing.
- **Pitchers**: stuff/movement/control are swapped for `stf_r`/`stf_l` etc.
  (his results specifically against right-handed / left-handed batters).
  **Arsenal and stamina are unchanged.**

vR/vL are about **platoon fit**, not overall quality — a player can have a
huge vR/vL gap (a real platoon candidate) while his overall Comp stays the
same. Use overall Comp to rank general talent; use vR/vL when the question is
specifically "how does he project against this specific handedness" (building
a platoon, evaluating a lefty specialist, etc).

---

## 5. Calibrated tool weights — exact current values

Weights are fit per league from real historical outcomes (regression against
actual WAR), stored in `data/<league>/config/tool_weights.json`, loaded via
`load_tool_weights(league_dir)`. **Every position bucket within a league shares
the same relative tool ratios** — only the `defense` share differs by bucket
(and 3B/COF/1B get a proportionally rescaled offensive mix since less/no
defensive weight has to go somewhere).

### PPL (1955, calibrated from n=164 hitters / 66 SP / 64 RP)

| Bucket | Contact | Gap | Power | Eye | Speed | Steal | StlRt | Defense |
|---|---|---|---|---|---|---|---|---|
| C / SS / 2B / CF | 0.1317 | 0.2934 | 0.2384 | 0.1264 | 0.0238 | 0.0181 | 0.0181 | 0.15 |
| 3B | 0.1401 | 0.3120 | 0.2535 | 0.1344 | 0.0238 | 0.0181 | 0.0181 | 0.10 |
| COF / 1B | 0.1568 | 0.3491 | 0.2837 | 0.1504 | 0.0238 | 0.0181 | 0.0181 | 0.00 |

| Pitcher role | Stuff | Movement | Control | Arsenal |
|---|---|---|---|---|
| SP | 0.3426 | 0.2680 | 0.3394 | 0.05 |
| RP | 0.4170 | 0.2893 | 0.2437 | 0.05 |

**Read this as**: 1955 hitters are **Gap/Power-dominant** (Gap is the single
biggest tool at ~29-35%!) with Contact and Eye roughly tied for next, and
baserunning tools deliberately small (~6% combined) — extra-base power in a
complete-game, pre-modern-bullpen era matters more than pure OBP skills, and
Gap (doubles/triples power) outweighs even Power (home runs) itself,
consistent with a lower home-run, more gap-to-gap run environment. SP is
almost perfectly balanced across all three pitching tools; RP skews toward
raw Stuff (short-relief power arms).

### eMLB (2034, calibrated from n=400 hitters / 211 SP / 172 RP)

| Bucket | Contact | Gap | Power | Eye | Speed | Steal | StlRt | Defense |
|---|---|---|---|---|---|---|---|---|
| C / SS / 2B / CF | 0.3039 | 0.0404 | 0.2030 | 0.2426 | 0.0300 | 0.0180 | 0.0120 | 0.15 |
| 3B | 0.3232 | 0.0430 | 0.2158 | 0.2580 | 0.0300 | 0.0180 | 0.0120 | 0.10 |
| COF / 1B | 0.3617 | 0.0481 | 0.2415 | 0.2887 | 0.0300 | 0.0180 | 0.0120 | 0.00 |

| Pitcher role | Stuff | Movement | Control | Arsenal |
|---|---|---|---|---|
| SP | 0.2495 | 0.5340 | 0.1664 | 0.05 |
| RP | 0.1936 | 0.5135 | 0.2429 | 0.05 |

**Read this as**: 2034 hitters are **Contact/Eye-dominant** (these two alone
are ~55% of the profile) with Gap almost irrelevant (0.04-0.05 — a fraction of
its 1955 weight) — a modern three-true-outcomes-adjacent environment where
plate discipline and raw hit tool matter far more than doubles power, and gap
power specifically has stopped being a differentiator (likely because in a
higher-power era, extra-base value is dominated by home runs, which Power
already captures, leaving Gap with little independent predictive signal).
Pitching is **overwhelmingly Movement-dominant** (~51-53%, more than double
Stuff's weight) — in the modern game, movement/deception (proxied partly via
HRA, home-run prevention) predicts run prevention far better than pure
velocity/stuff, the opposite emphasis from 1955's balanced arsenal.

**This is exactly the era-differentiation you asked me to verify earlier** —
confirmed correct and (as of the fixes shipped 2026-09-07) now consistently
applied everywhere in the app.

---

## 6. Defensive weights (position-specific, same across both leagues)

```
C:      CFrm 0.45, CBlk 0.35, CArm 0.20
SS:     IFR 0.40, IFE 0.20, IFA 0.20, TDP 0.20
2B:     IFR 0.35, TDP 0.30, IFE 0.20, IFA 0.15
3B:     IFA 0.35, IFE 0.30, IFR 0.25, TDP 0.10
CF:     OFR 0.55, OFE 0.25, OFA 0.20
COF_LF: OFR 0.50, OFE 0.30, OFA 0.20
COF_RF: OFR 0.40, OFA 0.35, OFE 0.25
```
(CFrm/CBlk/CArm = catcher framing/blocking/arm; IFR/IFE/IFA = infield
range/error/arm; OFR/OFE/OFA = outfield range/error/arm; TDP = turning double
plays.) 1B has no defensive category — real 1B defense barely moves value.

---

## 7. Per-tool transform curves — exact current values

Each curve is 5 delta values at fixed anchor ratings **(28, 40, 50, 60, 72)** on
the 20-80 scale — `apply_tool_transform` linearly interpolates between anchors,
clamping outside the range. The delta is *added* to the raw rating before
weighting. A steeper curve at the high end = that tool's marginal value
accelerates faster for elite grades in that league.

### PPL
| Tool | @28 | @40 | @50 | @60 | @72 |
|---|---|---|---|---|---|
| Hitter Contact | −9.00 | −4.25 | 0 | +11.75 | +13.26 |
| Hitter Gap | −3.00 | −1.52 | 0 | +2.78 | +6.24 |
| Hitter Power | −8.80 | −4.67 | 0 | +5.86 | +13.52 |
| Hitter Eye | −5.98 | −2.57 | 0 | +2.27 | +10.23 |
| Hitter Speed | −4.21 | −1.89 | 0 | 0 | 0 |
| SP Stuff | −9.17 | −5.50 | 0 | +5.19 | +12.30 |
| SP Movement | −6.00 | −3.14 | 0 | +9.38 | +9.38 |
| SP Control | −6.00 | −4.66 | 0 | +6.95 | +8.57 |
| RP Stuff | −8.94 | −4.08 | 0 | +4.56 | +11.51 |
| RP Movement | −6.00 | −3.14 | 0 | +3.16 | +7.00 |
| RP Control | −6.01 | −3.31 | 0 | +3.32 | +7.00 |

### eMLB
| Tool | @28 | @40 | @50 | @60 | @72 |
|---|---|---|---|---|---|
| Hitter Contact | −9.00 | −5.78 | 0 | +10.95 | +15.00 |
| Hitter Gap | −2.96 | −1.27 | 0 | +0.46 | +0.46 |
| Hitter Power | −10.56 | −7.82 | 0 | +9.71 | +15.00 |
| Hitter Eye | −6.13 | −3.91 | 0 | +6.81 | +13.99 |
| Hitter Speed | −3.88 | −3.57 | 0 | +1.09 | +6.66 |
| SP Stuff | −8.94 | −4.71 | 0 | +4.60 | +10.99 |
| SP Movement | −6.00 | −4.82 | 0 | +7.57 | +9.61 |
| SP Control | −6.08 | −4.97 | 0 | +5.50 | +7.20 |
| RP Stuff | −9.02 | −3.08 | 0 | +4.27 | +10.34 |
| RP Movement | −6.05 | −3.81 | 0 | +3.63 | +7.52 |
| RP Control | −6.18 | −3.12 | 0 | +3.23 | +6.66 |

Notable pattern: **Gap's transform curve is nearly flat in eMLB** (max ±0.46 —
essentially no reward/penalty for extreme Gap grades) vs. a real, meaningful
curve in PPL (up to +6.24) — reinforcing the weight-table finding that Gap has
lost almost all standalone predictive signal in the modern game. **Contact's
high end is steeper in eMLB** (+15.00 at 72 vs. PPL's +13.26) — an elite hit
tool is worth even more at the top end in 2034 than in 1955.

---

## 8. What is deliberately NOT part of the composite

- **Home park factors** — completely separate calculation (`park_fit.py`),
  shown as its own "Park Fit"/"Park Value%" field. A high-composite player can
  have a poor park fit and vice versa; check both.
- **Salary / contract cost** — composite is pure skill; "surplus" (a different,
  separately-computed field) nets projected value against cost/years of
  control. Use surplus, not composite, to judge "is he worth acquiring at his
  price."
- **Positional need** — "fills_need" (Free Agent Adds/Add Candidates pages) is
  a separate flag comparing a candidate's composite against your org's current
  incumbent at that position; composite alone doesn't know your roster.
- **In-season stat performance** — the *stored* `composite_score` (what's
  in the `ratings` table, shown as the main "Comp" column) blends in a small
  amount of real stat performance for MLB players with a track record via
  `compute_composite_mlb`. However, this blend is close to inert in practice —
  only ~3% of established MLB players have `composite != tool_only_score`, and
  by small amounts. **vR/vL are always the pure tool-based recomputation
  described above — they never include the stat blend**, even for the same
  player where the overall Comp does. Small unexplained gaps between overall
  Comp and vR/vL beyond a genuine platoon effect may be this.

---

## 9. Known accuracy bounds + very recent fix history

- Composite explains roughly **40-67% of same-year WAR variance** (R²),
  varying by league and player type — real signal, not a guarantee. From the
  last full validation pass (`docs/evaluation_model_findings.md`): EMLB
  hitters R²=0.667, EMLB pitchers R²=0.531, PPL(VMLB) hitters R²=0.397, PPL
  pitchers R²=0.384. Treat small composite gaps (a few points) as noise.
- **2026-09-07**: fixed a real bug where vR/vL on two of three player-finding
  pages (Scouting Targets, and the Team page's Add Candidates/Waiver/Free
  Agent panels) silently omitted speed/steal/stl_rt from the hitter
  calculation entirely, and pitcher vR/vL didn't exist at all on the Team
  page. Confirmed via a real player: vR read 54 via the correct path (Custom
  Upload) vs. 45 via the buggy ones — a 9-point gap with no handedness
  explanation. All three pages now compute vR/vL identically and correctly;
  if you're comparing against an older screenshot/memory of these pages,
  treat any pre-2026-09-07 vR/vL notes as unreliable for speed-tool players
  and all pitchers.

---

## 10. File pointers (for a Claude session with repo access)

- `src/statsplusplus/evaluation/composite.py` — the formulas themselves
  (§2-§4 above).
- `src/statsplusplus/evaluation/constants.py` — all penalty/threshold
  constants (§8's imbalance/floor numbers) and `DEFENSIVE_WEIGHTS` (§6).
- `data/ppl/config/tool_weights.json`, `data/emlb/config/tool_weights.json` —
  the live calibrated weights + transform curves per league (§5, §7);
  regenerated periodically by the calibration pipeline
  (`scripts/calibrate.py` / `model_regression.py`), so re-read these files
  directly rather than trusting this document's numbers if calibration has
  been re-run since 2026-09-07.
- `web/team_queries.py` (`_add_candidate_vr_vl_def`,
  `_add_candidate_pitcher_vr_vl`), `web/scouting_queries.py`
  (`_build_entries`), `scripts/custom_upload.py` (`import_ratings_sync`) —
  the three call sites that compute vR/vL for display.
- `docs/evaluation_model_findings.md` — the empirical validation write-up
  (R² figures, per-tool WAR correlations, aging curves) referenced in §9.
