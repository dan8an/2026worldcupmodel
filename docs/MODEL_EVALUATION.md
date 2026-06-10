# Model evaluation

## Protocol

The current report evaluates 557 completed international matches involving the
48 tournament teams from January 1, 2022 through June 8, 2026.

- All earlier mapped results are used only to warm up walk-forward Elo ratings.
- Each prediction uses information strictly before the match date.
- Matches on the same date are predicted before any results from that date
  update ratings or form.
- Teams require at least five prior mapped matches.
- Historical home advantage applies only to non-neutral matches.
- Current FIFA ranks are excluded because using them in older matches would
  leak future information.
- Current availability data is excluded because no historical point-in-time
  injury archive is available.

## Results

| Model | Log loss | Brier | RPS | Accuracy | ECE |
|---|---:|---:|---:|---:|---:|
| Equal probability | 1.0986 | 0.6667 | 0.2326 | 43.45% | 0.0000 |
| Walk-forward Elo | 1.0184 | 0.6099 | 0.2066 | 50.63% | 0.0217 |
| Elo + form/H2H | 1.0186 | 0.6104 | 0.2068 | 50.09% | 0.0148 |

Lower is better for log loss, Brier score, RPS, and ECE.

Walk-forward Elo clearly beats equal probabilities. The form/H2H layer has
slightly better calibration error but slightly worse log loss, Brier score,
RPS, and accuracy than Elo alone.

## Promotion decision

**Failed.** The context layer must beat Elo on both held-out log loss and Brier
score. It does not currently do so, and therefore remains experimental.

The next experiments should focus on opponent-adjusted form, a properly fitted
attack/defense goal model, and train-only tuning of context weights. Any change
must be evaluated on later untouched windows before promotion.

The machine-readable report, including annual splits and calibration bins, is
stored at `data/evaluation/latest.json` and served by
`GET /v1/model/performance`.
