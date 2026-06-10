# 2026 World Cup Prediction and Analytics App

## 1. Product Definition

Build an educational prediction and analytics application for the 2026 FIFA
World Cup. The product estimates match and tournament outcomes, explains the
main drivers behind each estimate, and optionally compares model probabilities
with market-implied probabilities.

It must not recommend wagers, stake sizes, parlays, or betting actions.

Primary users:

- Fans exploring match and tournament probabilities
- Students reviewing an applied sports-analytics project
- Analysts comparing model assumptions and outputs

Core user questions:

- Who is favored, by how much, and why?
- What scorelines are most plausible?
- How do match results change each team's path through the tournament?
- How confident and well-calibrated is the model?
- Where does the model differ from the market?

## 2. Scope and Release Strategy

As of June 9, 2026, kickoff is June 11, 2026. A reliable full-featured model
cannot be built and validated from scratch in two days. Use a staged release.

### Launch Baseline: June 10-11

- Import and validate all fixtures, teams, groups, venues, and kickoff times
- Publish Elo-based team ratings with transparent adjustment rules
- Generate expected goals with a Poisson model
- Derive win/draw/loss and correct-score probabilities from the score model
- Simulate the group stage and full tournament
- Show model version, generated time, data freshness, and uncertainty
- Deliver dashboard, match detail, and simulator pages

### Release 1: During Group Stage

- Train and evaluate a gradient-boosted result model
- Add recent form, opponent-adjusted performance, rest, travel, and venue
- Calibrate probabilities and compare them with the Poisson baseline
- Automate result ingestion and prediction refreshes
- Add team pages and model-performance dashboards

### Release 2: When Data Quality Is Proven

- Add team corners, cards, shots, and shots-on-target models
- Add projected lineups and player minutes
- Add player goals, assists, shots, SOT, tackles, and saves
- Add market comparison and AI-assisted explanations

Player props remain hidden until lineup, minutes, and event-data coverage pass
explicit quality checks.

## 3. Recommended Architecture

Use a monorepo:

```text
2026worldcupmodel/
  apps/
    web/                 React, Vite, TypeScript, Tailwind
    api/                 FastAPI application
  packages/
    contracts/           Generated API types or shared schemas
  modeling/
    src/
      ingestion/
      features/
      models/
      simulation/
      evaluation/
    tests/
    notebooks/           Exploration only; no production logic
  data/
    seed/                Small, versioned reference files
  supabase/
    migrations/
    seed.sql
  docs/
  .github/workflows/
```

Technology choices:

- Frontend: React, Vite, TypeScript, Tailwind, TanStack Query
- Charts: Recharts
- Backend: FastAPI, Pydantic, SQLAlchemy, Alembic
- Modeling: Python, pandas or Polars, scikit-learn, LightGBM, SciPy
- Database: Supabase Postgres
- Jobs: Render cron jobs initially; move to a queue only if needed
- Frontend hosting: Vercel
- API and scheduled modeling jobs: Render
- Tests: Vitest, React Testing Library, pytest
- Quality: ESLint, Prettier, Ruff, mypy, pre-commit

Keep model training and batch inference outside HTTP request handlers. The API
should serve immutable, versioned prediction snapshots from Postgres.

## 4. Data Plan

### Source Classes

| Data | Initial source | Refresh | Notes |
|---|---|---:|---|
| Fixtures, groups, venues | FIFA official schedule | Daily, then after matches | Treat FIFA as canonical |
| Results | Licensed/API source plus validation | After each match | Store provider IDs and raw payload |
| Elo ratings | Public Elo source or internal calculation | Daily | Preserve rating history |
| FIFA ranking | FIFA | On release | Secondary strength feature |
| Historical internationals | Licensed API/open dataset | Weekly | Include competition and neutral-site flags |
| Event data/xG | Licensed provider | Per provider | Do not mix xG definitions silently |
| Squads and lineups | FIFA/provider | Daily and pre-match | Track confirmed vs projected |
| Injuries/suspensions | Provider/manual review | Daily | Store source and confidence |
| Venue/weather | Venue reference + weather API | Daily/hourly | Snapshot forecast used by model |
| Odds | Licensed odds API | Periodic | Analysis only; remove margin |

### Ingestion Rules

- Retain raw provider responses for audit and reprocessing.
- Normalize provider-specific IDs through mapping tables.
- Store `source`, `source_updated_at`, `ingested_at`, and data-quality status.
- Make ingestion idempotent with provider ID plus source as the natural key.
- Reject impossible fixtures, duplicate teams, invalid kickoff times, and
  probability rows that do not sum to one within tolerance.
- Never scrape a source whose terms prohibit it.

### Pre-Launch Data Audit

Before publishing:

- All 104 fixtures exist exactly once.
- All 48 teams map to one canonical team ID.
- Twelve groups contain four teams each.
- Venue coordinates and time zones are present.
- Knockout slot rules include all best-third-place combinations.
- No feature uses information published after the prediction timestamp.

## 5. Database Model

Minimum tables:

- `teams`
- `players`
- `venues`
- `tournaments`
- `groups`
- `matches`
- `match_results`
- `team_rating_snapshots`
- `team_feature_snapshots`
- `player_feature_snapshots`
- `squads`
- `lineups`
- `availability_reports`
- `model_versions`
- `prediction_runs`
- `match_predictions`
- `scoreline_predictions`
- `prop_predictions`
- `simulation_runs`
- `team_tournament_probabilities`
- `market_snapshots`
- `data_sources`
- `ingestion_runs`

Important fields:

```text
model_versions:
  id, name, semantic_version, git_sha, training_cutoff,
  feature_schema_version, metrics_json, artifact_uri, created_at

prediction_runs:
  id, model_version_id, data_cutoff, random_seed,
  status, generated_at

match_predictions:
  match_id, prediction_run_id, home_win, draw, away_win,
  home_xg, away_xg, confidence_tier, explanation_factors_json
```

Add uniqueness constraints so one prediction run has only one prediction per
match. Use row-level security for user-owned saved simulations, while public
prediction snapshots remain read-only.

## 6. Modeling Plan

### 6.1 Baseline Strength Model

Maintain an international-team Elo rating:

- Weight competitive matches above friendlies.
- Account for opponent strength and margin of victory.
- Model neutral venues separately.
- Add host-country advantage only when supported by validation.
- Apply time decay or rating reversion for inactive teams.

This is both a launch model and a benchmark that later models must beat.

### 6.2 Expected Goals and Scorelines

Fit attack and defense strengths using a Poisson or Dixon-Coles model:

```text
log(home_lambda) =
  intercept + home_attack - away_defense + context

log(away_lambda) =
  intercept + away_attack - home_defense + context
```

Generate a score matrix, initially 0-0 through 8-8, and fold residual tail
probability into the displayed totals. Derive:

- Home win, draw, and away win probabilities
- Expected goals
- Correct-score probabilities
- Both-teams-to-score and totals only if presented as analytics

Use Dixon-Coles low-score correction if it improves time-based validation.

### 6.3 Result Model

Train LightGBM multiclass classification on historical international matches.

Candidate pre-match features:

- Elo level and difference
- FIFA ranking level and difference
- Rolling goals and opponent-adjusted xG for/against
- Recent form with time decay
- Competitive-match and tournament-stage indicators
- Squad quality and weighted player availability
- Neutral site and host-country indicators
- Rest days, travel distance, altitude, and weather
- Tactical/style matchup features when coverage is consistent

Do not feed the Poisson probabilities into the first boosted model. Evaluate
the models independently, then blend only if the ensemble improves held-out
log loss and calibration.

### 6.4 Probability Calibration

Use time-based out-of-fold predictions to fit isotonic regression or
temperature scaling. Select the method on validation data, not test data.

Report:

- Multiclass log loss
- Brier score
- Ranked probability score
- Calibration error and reliability plots
- Accuracy as a secondary, non-probabilistic metric

Compare against:

- Equal-probability baseline
- Elo-only baseline
- Poisson/Dixon-Coles baseline
- Market closing probabilities when legally available

### 6.5 Tournament Simulation

Simulate at least 50,000 tournament iterations for published snapshots.

Each iteration:

1. Sample every group match score.
2. Apply official points and tiebreak rules.
3. Rank all third-place teams and select the best eight.
4. Resolve the official Round-of-32 mapping for that exact qualifier set.
5. Sample knockout scores, extra time, and penalties.
6. Record stage reached and champion.

Use deterministic random seeds and unit-test the third-place mapping. This is
the highest-risk rules implementation in the project.

### 6.6 Props

Team counts such as corners and cards should use count models such as negative
binomial regression when variance exceeds the mean.

Player models require:

- Probability of starting
- Expected minutes conditional on starting or appearing as a substitute
- Per-90 event rate with partial pooling
- Team expected event volume
- Opponent and game-state adjustments

Combine appearance, minutes, and event uncertainty rather than multiplying a
single per-90 number by a fixed 90-minute assumption.

## 7. Confidence and Explanations

Confidence is not the largest class probability alone. Base it on:

- Predictive entropy
- Agreement among Elo, Poisson, and boosted models
- Data completeness and freshness
- Lineup uncertainty
- Historical calibration for similar predictions

Suggested labels:

- Higher confidence
- Moderate confidence
- High uncertainty
- Insufficient data

Explanations should come from structured factors first:

- Elo advantage
- Attack/defense mismatch
- Venue or host effect
- Rest/travel difference
- Important availability change

An LLM may turn those facts into prose, but it must receive only computed,
source-backed factors and may not invent injuries, tactics, or news.

## 8. API Surface

Initial endpoints:

```text
GET  /health
GET  /v1/tournament
GET  /v1/matches
GET  /v1/matches/{match_id}
GET  /v1/teams
GET  /v1/teams/{team_id}
GET  /v1/predictions/latest
GET  /v1/simulations/latest
POST /v1/simulations/custom
GET  /v1/model/versions/current
GET  /v1/model/performance
```

Every prediction response includes:

- `model_version`
- `generated_at`
- `data_cutoff`
- `probabilities`
- `uncertainty`
- `key_factors`

Cache public reads at the CDN/API layer. Rate-limit custom simulations and cap
their iteration count.

## 9. Frontend Plan

### `/dashboard`

- Next matches and probability cards
- Group qualification probabilities
- Championship favorites
- Data freshness and model status

### `/matches` and `/match/:id`

- Filters by date, group, stage, and team
- W/D/L probability bar
- Expected goals and top scorelines
- Key factors and uncertainty
- Optional model-vs-market chart

### `/teams/:team`

- Rating history
- Fixtures and predictions
- Stage-reach probabilities
- Squad availability when reliable

### `/players/:player`

- Add only in Release 2
- Expected minutes and prop distributions
- Data-source and lineup-status labels

### `/simulator`

- Current tournament projection
- User-controlled match outcomes
- Recomputed group tables and bracket paths
- Shareable scenario ID

### `/model-explainer`

- Data sources and freshness
- Model architecture
- Backtest and calibration results
- Known limitations
- Responsible-use statement

## 10. Responsible Product Boundary

Allowed:

- Model probability
- Vig-free market-implied probability
- Difference between model and market
- Confidence and uncertainty
- Historical calibration

Disallowed product behavior:

- "Bet this" calls to action
- Stake sizing or bankroll advice
- Parlays or bet-slip construction
- Guaranteed-win language
- Push notifications designed around wagering urgency

Use neutral labels such as `Model-market difference`, `No material difference`,
`High uncertainty`, and `Market and model broadly agree`. Avoid the word
`edge` in the primary UI because it can read as a recommendation.

## 11. Testing and Operations

### Tests

- Unit tests for feature timestamps and no-leakage guarantees
- Probability-sum and score-matrix tests
- FIFA standings and tiebreak test fixtures
- Tests for all third-place qualifier mappings
- Deterministic simulation snapshot tests
- API schema and database integration tests
- Critical frontend flow tests

### Scheduled Jobs

- Fixtures/results ingestion: before and after match windows
- Ratings/features: after new verified results
- Predictions: after feature refresh or lineup update
- Tournament simulation: after every completed match
- Monitoring: continuous health plus scheduled freshness checks

### Monitoring

- Ingestion failures and stale-source alerts
- Missing or duplicate fixtures
- Prediction-run status and duration
- API latency and error rate
- Probability drift and calibration after results arrive

Never overwrite a published prediction after kickoff. Preserve it for honest
evaluation, then create a separate post-match record.

## 12. Delivery Backlog

### P0: Publishable Baseline

- Initialize monorepo, linting, tests, and CI
- Create Supabase schema and seed tournament entities
- Implement canonical fixture importer and validator
- Implement Elo and Poisson baseline
- Implement official standings/bracket engine
- Run deterministic tournament simulation
- Build match list, match detail, dashboard, and simulator
- Deploy and add freshness/error monitoring

Exit criteria:

- All 104 fixtures pass validation
- Predictions sum correctly and show model metadata
- Official tournament rules are covered by tests
- A full simulation completes reproducibly
- No betting-action language appears in the UI

### P1: Calibrated Match Model

- Build historical training dataset
- Add time-safe feature pipeline
- Train LightGBM model
- Backtest by chronological tournament windows
- Calibrate and compare against baselines
- Add model-performance page

Current progress:

- Historical dataset and time-safe feature pipeline implemented
- Chronological walk-forward evaluation implemented for 2022-2026
- Equal-probability, Elo, and form/H2H models compared
- Metrics and calibration bins published through the API and model page
- Form/H2H layer failed the promotion gate and remains experimental
- LightGBM training and probability calibration remain pending

Exit criteria:

- Model beats Elo-only baseline on held-out log loss or is not promoted
- Calibration plots and metrics are published
- Training and inference are reproducible from versioned inputs

### P2: Context and Automation

- Add travel, rest, weather, injuries, and lineup snapshots
- Automate post-match ingestion and reruns
- Add source confidence and data-quality indicators
- Add structured explanation generation

### P3: Props and Market Comparison

- Secure an event-data and lineup provider
- Validate team-count models
- Validate player-minute and event models
- Add vig removal and timestamped market snapshots
- Release props only where coverage thresholds are met

## 13. Principal Risks

| Risk | Mitigation |
|---|---|
| Two-day pre-kickoff runway | Ship transparent baseline first |
| Player/event data licensing | Make props provider-gated |
| Sparse national-team samples | Use Elo, partial pooling, and club-derived priors carefully |
| Data leakage | Enforce feature cutoff timestamps and chronological tests |
| Third-place bracket complexity | Encode official mapping table and exhaustively test it |
| Injury/news unreliability | Source, timestamp, and confidence-label every report |
| Misleading confidence | Publish calibration and data-quality state |
| Provider outage during tournament | Cache raw data and support manual verified overrides |
| LLM hallucinated explanations | Generate only from structured model factors |

## 14. First Implementation Sequence

1. Initialize Git and scaffold the monorepo.
2. Create the tournament schema and import the canonical 104 fixtures.
3. Implement validation and official tournament rules before UI polish.
4. Produce an Elo/Poisson prediction snapshot.
5. Build and verify the full tournament simulator.
6. Expose versioned predictions through FastAPI.
7. Build the three launch pages: dashboard, match detail, simulator.
8. Deploy, monitor, and preserve every pre-match prediction.
9. Start the historical ML pipeline as a parallel Release 1 track.

The launch model should be called a baseline in the UI until its out-of-sample
calibration has been measured. Transparent simplicity is preferable to an
unvalidated complex model presented with false precision.
