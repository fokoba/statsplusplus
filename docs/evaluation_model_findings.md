# Player Evaluation Model — Findings & User Guide

*Last validated: 2026-08-10 (Session 79)*
*Leagues tested: EMLB (1-100 scale, 2033 season), VMLB (20-80 scale, 2034 season)*

---

## Model Accuracy

### Composite Score vs OOTP OVR — Predicting Same-Year WAR

| League/Type | Our Composite R² | OOTP OVR R² | Gap | Verdict |
|---|---|---|---|---|
| EMLB Hitters (n=311) | 0.667 | 0.727 | -0.060 | OVR slightly better |
| EMLB Pitchers (n=242) | 0.531 | 0.415 | +0.116 | **We win** |
| VMLB Hitters (n=212) | 0.397 | 0.372 | +0.025 | **We win** |
| VMLB Pitchers (n=141) | 0.384 | 0.470 | -0.086 | OVR better (compressed tools) |

Measurement methodology: same-year only (current ratings snapshot vs current-year
qualifying stats). No stale comparisons. Min 300 PA (hitters) / 80 IP (pitchers).

### What R² Means for Users

R² = 0.67 means ~67% of WAR variance is explained by ratings. The remaining 33%
is unpredictable noise (injuries, luck, role changes, hidden attributes).

**Practical margin of error by composite tier:**

| Composite | Expected WAR | Realistic Range | Confidence |
|---|---|---|---|
| 40-45 | 0.0-0.5 | -0.5 to 1.5 | Low (replacement level, volatile) |
| 45-50 | 0.5-1.5 | 0.0 to 2.5 | Moderate |
| 50-55 | 1.5-2.5 | 0.5 to 4.0 | Moderate |
| 55-60 | 2.5-4.0 | 1.5 to 5.5 | Good (clearly above average) |
| 60-65 | 4.0-5.5 | 2.5 to 7.0 | Good (star-level production likely) |
| 65-70 | 5.5-7.5 | 4.0 to 9.0 | High (elite, but exact level varies) |
| 70+ | 7.5+ | 5.5+ | High (MVP candidate) |

---

## How to Use Our Ratings vs OVR

### When Our Composite Is More Reliable

- **Pitchers** — Our model captures tool importance (especially movement/HRA
  dominance) better than OVR in most leagues
- **Prospects** — No stat history means tools are the only signal; our composite
  is purpose-built for tool evaluation
- **Defense-first players** — We properly weight infield defense (15% for SS/2B/CF);
  OVR may undervalue elite defenders
- **Identifying undervalued profiles** — Players whose tools are distributed in
  ways OVR doesn't reward but the sim engine does

### When OVR Is More Reliable

- **Established hitters with 2+ years of stats** — OVR has access to hidden
  attributes (clutch, consistency, groundball tendency) that affect hitter WAR
- **DH/1B/COF types** — Pure offense evaluation where OVR's hidden factors matter
- **Leagues with compressed tool distributions** (20-80 scale) — Less information
  in tool ratings means OVR's additional hidden data gives it an edge

### When They Disagree

| OVR vs Composite | What It Means | Action |
|---|---|---|
| OVR >> Composite | Player has strong hidden attributes or is a better "gamer" than tools suggest | Trust OVR for current production; our model may underrate |
| Composite >> OVR | Tools project better than OVR thinks; possibly defense-driven or movement-dominant pitcher | Potential buy-low candidate; investigate WHY |
| Both agree: high | Strong confidence in production | Safe bet |
| Both agree: low | Strong confidence player is limited | Don't overpay |

---

## What Drives WAR Production (Empirical Findings)

### Hitters

| Factor | EMLB correlation | VMLB correlation | Notes |
|---|---|---|---|
| Contact | r = +0.59 | r = +0.35 | Dominant predictor in both |
| Power | r = +0.44 | r = +0.15 | Important in EMLB, low-signal in VMLB |
| Eye | r = +0.42 | r = -0.03 | League-dependent (irrelevant in VMLB) |
| Gap | r = +0.21 | r = +0.20 | Consistent moderate signal |
| Speed | r = +0.19 | r = +0.17 | Underrated — speed×contact interaction real |
| Infield range | r = +0.28 | r = +0.16 | Major missing factor before recalibration |
| ISO (stat) | r = +0.25 residual | r = +0.24 residual | Extra-base production beyond tool prediction |

**Key interaction:** Speed × contact synergy (r=+0.18 with residual). Fast players
with good contact produce more WAR than the linear sum predicts (infield hits,
extra bases, defensive pressure).

**Contact composition:** Contact = BABIP + AvoidK internally. BABIP drives WAR
(r=0.42), AvoidK adds minimal value (r=0.06). However, the composite contact rating
is already an optimal blend — decomposing it produces noisier individual predictions.

### Pitchers

| Factor | EMLB correlation | VMLB correlation | Notes |
|---|---|---|---|
| Movement/HRA | r = +0.56 | r = +0.50 | Dominant predictor (HR prevention) |
| Stuff | r = +0.38 | r = +0.16 | Important in EMLB, compressed in VMLB |
| Control | r = +0.37 | r = +0.13 | Similar pattern to stuff |
| Stamina | r = +0.33 | r = +0.18 | Innings volume effect |

**Key finding:** Movement (or its component HRA) is the single most important
pitcher tool. It prevents home runs, which are the largest single-event run
producers. When HRA is available as a separate rating, it's a marginally cleaner
signal than the composite movement rating (r=0.927 correlation between them).

### Defense

Defense was severely underweighted in the original model. Residual analysis showed
infield range (IFR) had r=+0.36 with WAR AFTER accounting for composite — the
largest missing factor.

Fixed: SS/2B defense now 15% of composite (was 5%), 3B at 10% (was 0%), CF at 15%.

### Aging

Longitudinal analysis (tracking same players over time, avoiding survivorship bias)
shows league-specific aging curves that differ significantly from real-baseball
assumptions. Per-league curves are calibrated and stored in `model_weights.json`.

### Imbalance Penalties

Tested whether extreme tool profiles (one dominant tool + weak others) predict
underperformance. Results are league-specific:
- EMLB hitters: penalty HURTS prediction (removed, threshold=999)
- VMLB hitters: penalty helps slightly (kept)
- Pitchers: marginal positive effect in both leagues

Thresholds calibrated per-league via `model_regression.py --calibrate`.

---

## Structural Limitations (Cannot Be Improved With Current Data)

1. **Hidden attributes** — OOTP's OVR includes clutch, consistency, durability,
   groundball/flyball tendency, and other factors not visible in scouting reports.
   These account for most of the remaining gap on hitters (~6% R²).

2. **Batted ball type** — Whether a hitter is a flyball, line drive, or groundball
   hitter affects ISO and WAR production. Not captured in tool ratings. Partially
   captured by stat blending (OPS+ includes SLG).

3. **Year-to-year randomness** — Even a perfect model can't predict WAR perfectly.
   Injuries, role changes, hot/cold streaks introduce ~30% irreducible variance.

4. **Compressed tool distributions** — In 20-80 scale leagues (VMLB), pitcher tools
   (stuff SD=3.6, control SD=4.2) simply don't differentiate players enough.
   There's not enough information in the ratings to predict outcomes.

5. **Platoon splits** — Players with strong/weak platoon splits perform differently
   against LHP vs RHP. Our model uses overall ratings with L/R averaging but doesn't
   fully model the platoon deployment effect on total WAR.

---

## Calibration Architecture

All model parameters are derived automatically from each league's data:

```
calibrate.py runs on league setup/recalibration:
  → tool_weights.json (per-position offensive/defensive/baserunning weights)
  → model_weights.json (aging curves, WAR tables, imbalance thresholds)

model_regression.py --calibrate (supplemental):
  → Aging curves (longitudinal, survivorship-bias-free)
  → Imbalance penalty thresholds (per-league R² validation)
```

Key calibration principles:
- Peak-age players only (27-32) for stable ratings
- 2-year window for robust sample size
- WAR as regression target (not FIP)
- No min-weight floors — data determines tool importance
- High-level ratings only (no component multicollinearity)
- Adaptive age range for smaller leagues

---

## Future Improvement Opportunities

*For when we return to model tuning:*

1. **Ratings history integration** — Use per-year rating snapshots in calibration
   instead of current ratings against historical stats (eliminates any remaining
   staleness). Requires `ratings_history` table.

2. **Weak-side contact bonus** — Identified r=+0.12 signal for balanced L/R splits.
   Not implemented yet.

3. **Stat blending for ISO** — ISO (SLG-AVG) has r=+0.25 with WAR residual.
   Current stat blending uses OPS+ which includes SLG, but a direct ISO component
   might add signal.

4. **VMLB pitcher improvement** — Compressed tool distributions limit us. Possible
   approaches: arsenal quality modeling, pitch-specific ratings integration.

5. **Platoon modeling** — L/R split ratings are available. A platoon-aware composite
   could add ~1-2% R² for hitters.
