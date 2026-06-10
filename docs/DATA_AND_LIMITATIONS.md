# Data and limitations

## Current model

The model uses November 2025 FIFA ranking positions as a transparent strength
prior, then adjusts that prior with time-safe historical form, capped
head-to-head history, and sourced player availability. These values remain
uncalibrated estimates rather than a promoted production model.

## Historical results

`data/raw/international_results.csv` is collected from the CC0
`martj42/international_results` archive. Its metadata file records the source,
retrieval timestamp, byte count, and SHA-256 checksum.

- Form uses completed matches from the prior four years.
- Match weights decay with a one-year half-life.
- Competitive matches receive more weight than friendlies.
- Form adjustments shrink toward zero when fewer than 12 matches are present.
- Head-to-head uses an eight-year window, shrinks until six meetings, and is
  capped at 15 Elo-equivalent points per team.
- Rows with missing scores are schedules and are never treated as results.
- Results after a prediction's data cutoff are excluded.

## Squads and availability

Official squad selections and current availability reports are separate feeds.
Every record must contain a source URL and publication timestamp. Availability
also has an effective time window, so stale reports stop affecting forecasts.
When multiple records exist for one player, the newest applicable report wins.
If both a squad omission and injury report exist, only the larger impact is
counted to avoid double-penalizing the same absence.

Status, importance, and confidence produce a capped team adjustment. This is a
transparent heuristic until player-value and lineup models are calibrated.

Team profile player lists are collected from the 2026 World Cup squad tables
and stored with a retrieval timestamp and source checksum. The displayed key
players balance three established international contributors ranked from caps
and goals with one younger midfield/forward candidate. This is transparent and
repeatable, but it is still a heuristic rather than a current club-form,
tactical-importance, or projected-minutes model. Late replacements must be
refreshed through the squad collector.

All matches are modeled as neutral-site contests with respect to fixture
ordering. Being listed as the "home" team does not create an advantage.
Only the three tournament co-hosts, Mexico, Canada, and the United States,
receive the explicit host-country adjustment, and it applies whether they are
listed as the home or away team. The adjustment is currently 50 Elo-equivalent
points, corresponding to roughly +6.4% expected goals for the co-host and -6.1%
for its opponent before other model inputs. This is intentionally conservative
because the tournament is spread across three countries and many venues.

The team draw in `data/seed/teams.json` was checked against the published 2026
group pages on June 9, 2026. Fixture records preserve the official tournament
shape of 72 group matches and 32 knockout matches. Group-stage dates are
generated from the official matchday windows; connect the FIFA/provider
ingestion adapter before treating every kickoff and venue assignment as
canonical.

The Round-of-32 simulator currently selects the best eight third-place teams
correctly, then uses deterministic strength seeding for pairings. FIFA Annex C
mapping must be imported and exhaustively tested before production launch.

## Interpretation

- Probabilities describe uncertainty, not certainty.
- Published tournament probabilities use 50,000 Monte Carlo iterations. Their
  worst-case 95% simulation-sampling margin is about ±0.44 percentage points.
  This reduces Monte Carlo noise but does not reduce uncertainty in model inputs
  or assumptions.
- `Higher confidence` is relative to this model and is not a recommendation.
- Player props and market comparisons are intentionally absent.
- Injury and squad effects only appear when sourced records are present.
- Predictions are educational analytics and not betting advice.

## Promotion gate

The chronological evaluation is now implemented and published. Walk-forward
Elo beats equal probabilities, but the form/H2H context layer has not beaten
Elo on both log loss and Brier score. It remains experimental. See
`docs/MODEL_EVALUATION.md` and `data/evaluation/latest.json`.
