# Prospect Query Guide

## Overview

`scripts/prospect_query.py` provides league-wide prospect rankings and farm system comparisons using the `prospect_fv` table. Data must be current — run `fv_calc.py` after any league refresh before querying.

Only EMLB teams are included. International leagues (KBO, Mexican League, South African League, etc.) are automatically excluded.

---

## Commands

### Top Prospects List

```bash
python3 scripts/prospect_query.py top [--n 100] [--bucket SP] [--age-max 22] [--fv-min 50]
```

Ranks all EMLB prospects by FV (descending), ties broken by age (younger first).

| Flag | Description | Default |
|---|---|---|
| `--n` | Number of prospects to show | 100 |
| `--bucket` | Filter by position bucket (SP, RP, C, SS, 2B, CF, COF, 3B, 1B) | all |
| `--age-max` | Maximum age | none |
| `--fv-min` | Minimum FV | none |

Examples:
```bash
python3 scripts/prospect_query.py top                        # top 100 overall
python3 scripts/prospect_query.py top --n 50 --bucket SP     # top 50 SP prospects
python3 scripts/prospect_query.py top --fv-min 55            # all FV 55+ prospects
python3 scripts/prospect_query.py top --age-max 19 --n 30    # top 30 teenagers
```

---

### Farm System Rankings

```bash
python3 scripts/prospect_query.py systems [--n 34]
```

Ranks all EMLB farm systems by a weighted prospect score:

| FV Tier | Points |
|---|---|
| 60+ | 4 |
| 55 | 3 |
| 50 | 2 |
| 45 | 1 |
| Below 45 | 0 |

Output shows score, count of prospects at each tier, and the top prospect FV for each org.

```bash
python3 scripts/prospect_query.py systems          # top 30 systems
python3 scripts/prospect_query.py systems --n 34   # all 34 EMLB systems
```

---

### Team Prospect List

```bash
python3 scripts/prospect_query.py team <team_name>
```

Shows top 30 prospects for a specific org. Partial name match supported.

```bash
python3 scripts/prospect_query.py team Anaheim
python3 scripts/prospect_query.py team "Tampa Bay"
python3 scripts/prospect_query.py team Toronto
```

---

## FV Scoring Notes

- FV is the integer floor — `60+` and `60` both have `fv=60` in the DB. Tier counts include both.
- MLB-level players are excluded from all prospect queries.
- FV values come from `fv_calc.py` for all players including Angels. `farm_analysis.py` reads `prospect_fv` and does not write to it.

---

## Keeping Data Current

Rankings reflect the most recent `eval_date` in `prospect_fv`. After a league refresh:

```bash
python3 -m statsplusplus.data.refresh 2033           # pulls data + runs fv_calc automatically
python3 scripts/farm_analysis.py
```

Then query as needed.
