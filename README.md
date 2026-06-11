# 2026 World Cup Model

An educational prediction and analytics app for the 2026 FIFA World Cup.
It combines an Elo-informed Poisson score model, tournament simulation,
a FastAPI API, and a React dashboard.

The model does not provide betting recommendations.

## Repository

```text
apps/api       FastAPI service
apps/web       React/Vite frontend
modeling       Prediction and simulation package
data/seed      Versioned tournament data
supabase       Postgres schema
```

## Quick start

Backend:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r apps/api/requirements.txt
uvicorn apps.api.app.main:app --reload
```

Frontend:

```bash
cd apps/web
npm install
npm run dev
```

Model tests use only the Python standard library:

```bash
python3 -m unittest discover -s modeling/tests -v
```

Generate a versioned prediction snapshot:

```bash
python3 -m modeling.src.generate_snapshot
```

## Collect context data

Refresh the CC0 international-results archive and validate all local context
feeds:

```bash
python3 -m modeling.src.ingestion.collect_historical_results
python3 -m modeling.src.ingestion.collect_squads
python3 -m modeling.src.ingestion.validate_context
python3 -m modeling.src.evaluation.backtest
python3 -m modeling.src.generate_snapshot
```

The download writes a retrieval timestamp and SHA-256 checksum beside the raw
CSV. Team-name mappings live in `data/seed/team_aliases.json`.

The squad collector stores all player rows plus a timestamped source metadata
file. Team pages derive their key-player list from caps and goals rather than
an unsourced manual ranking. Refresh it after official replacement deadlines.

Current squad selections and player availability are stored separately:

```text
data/context/squad_selections.json
data/context/availability_reports.json
```

Every record requires a source URL, publication timestamp, confidence, and
player-importance estimate. Use the adjacent `.example.json` files as schemas.
Do not add unsourced social-media claims.

## Daily API-Football ingestion

API-Football is used only by server-side ingestion scripts. Configure
`backend/.env` from `backend/.env.example`; never add its API key to a Vite
environment variable or frontend code. Activate the project virtual
environment before running the Python command:

Apply `supabase/migrations/202606100001_daily_prediction_pipeline.sql`, then
run:

```bash
source .venv/bin/activate
python scripts/update_data.py
```

After applying `supabase/migrations/202606100002_rating_updates.sql`, calculate
the current team and player ratings from the ingested raw statistics:

```bash
python scripts/update_ratings.py
```

The rating update is deterministic and repeat-safe. It maintains one current
rating row per team and player while preserving model-run-specific rating rows
for future prediction snapshots. An empty raw-statistics database exits
successfully so the command can run immediately after ingestion in a cron job.

After applying `supabase/migrations/202606100003_prediction_generation.sql`,
generate current predictions for future matches:

```bash
python scripts/generate_predictions.py
```

The command creates a model run when at least one match can be predicted and
updates the existing prediction for each match. It uses current team ratings,
an Elo probability base, validated attack/defense ratings, draw
calibration, and a normalized score grid from 0-0 through 6-6. The production
model version is `elo-context-v4.1`: the v4 shot-volume model with the
rest/context component removed. The canonical fixture catalog from
`modeling/src/data.py` is authoritative; database match rows only enrich it.
Predictions retain IDs such as `WC26-001` even before provider match rows exist.
V4 preserves the complete v3 pipeline and adds only the validated
`shot_volume_rating` ablation at weight `0.2`. Shot quality, defensive
suppression, and the combined all-feature xG-proxy model remain research-only.

After applying `supabase/migrations/202606100005_tournament_simulation.sql`,
run the persisted tournament simulation:

```bash
python scripts/run_simulations.py
```

The command reads the latest canonical prediction run and simulates 50,000
tournaments by default. For a smaller manual check, use
`python scripts/run_simulations.py --simulations 1000 --seed 2026`.

The script imports completed fixtures, team match statistics, player match
statistics, and lineups. Provider fixture/team/player IDs and unique database
indexes make repeated runs update existing records instead of duplicating
them. If `API_FOOTBALL_KEY` is absent, the command clearly reports sample mode
and reads `backend/ingestion/sample-data/api-football.json`.

It defaults to yesterday's date. For a specific date:

```bash
python scripts/update_data.py --date 2026-06-09
python scripts/update_data.py --date 2026-06-09 --max-fixtures 3
python scripts/update_data.py --sample
```

`--sample` always uses every fixture in the checked-in local sample dataset,
even when an API key is configured or the sample fixture date is not today.
It makes no API-Football requests and exercises the same Supabase upserts as a
real ingestion run.

The Python provider interface lives in `scripts/data_ingestion/providers.py`,
so a future data source can implement the same normalized fixture methods
without changing the database writer or orchestration script.

API-Football requests are filtered before download using
`API_FOOTBALL_LEAGUE_ID` and `API_FOOTBALL_SEASON`. League ID `1` is the
default World Cup filter. `API_FOOTBALL_REQUEST_DELAY_SECONDS` defaults to
`1.0`, and `--max-fixtures` defaults to `5` to limit detail requests. HTTP 429
responses stop further detail fetching cleanly while preserving rows already
committed.

### Historical team-stat backfill

For xG-proxy-v4 research, backfill completed fixtures and team-level match
statistics without fetching player data or changing production predictions:

```bash
python scripts/backfill_historical_stats.py \
  --league-id 1 \
  --season 2022 \
  --date-from 2022-11-20 \
  --date-to 2022-12-18 \
  --max-fixtures 10
python scripts/build_xg_proxy_features.py
python scripts/validate_xg_proxy_v4.py
```

The backfill requires `DATABASE_URL` and `API_FOOTBALL_KEY`, honors
`API_FOOTBALL_REQUEST_DELAY_SECONDS`, and defaults to only five fixtures from
yesterday. Repeated runs skip fixtures that already have both team-stat rows,
then upsert the remaining provider fixture and team-stat keys. The final
summary reports exact inserted and updated counts separately. A 429 stops new
requests while preserving rows already committed.

List available API-Football competition IDs and seasons without touching the
database:

```bash
python scripts/list_competitions.py --filter euro
python scripts/list_competitions.py --filter copa-america
python scripts/list_competitions.py --filter nations-league
python scripts/list_competitions.py --filter world-cup-qualification
python scripts/list_competitions.py --search "Gold Cup"
```

### Render Cron

Create a Render Cron Job with the repository root as its working directory.
Use this build command:

```bash
pip install -r apps/api/requirements.txt
```

Use this cron command:

```bash
python scripts/update_data.py --max-fixtures 5
```

Required Render environment variables:

```text
DATABASE_URL
SPORTS_PROVIDER=api_football
API_FOOTBALL_KEY
API_FOOTBALL_BASE_URL=https://v3.football.api-sports.io
API_FOOTBALL_LEAGUE_ID=1
API_FOOTBALL_SEASON=2026
API_FOOTBALL_REQUEST_DELAY_SECONDS=1.0
```

Render sets `RENDER=true`; in that environment a missing API key is a
configuration failure, not an automatic sample run. Set
`INGESTION_USE_SAMPLE=true` only when intentionally testing sample ingestion.
Leave it unset or set it to `false` for the production cron. The default
ingestion date is yesterday in UTC. Normal empty-match days and HTTP 429
responses exit successfully so the next scheduled run can continue. Database,
schema, or fixture-processing errors exit nonzero and appear as failed cron
runs.

## Evaluate the model

After applying `supabase/migrations/202606100006_model_evaluation.sql`, replay
the current `poisson-ratings-v1` generator against historical results:

```bash
python scripts/evaluate_model.py
```

This writes an immutable row to `evaluation_results` and a detailed local
report to `data/evaluation/current_model_latest.json`. It reports Brier score,
log loss, probability-bucket calibration, accuracy by confidence tier, and
comparisons with walk-forward Elo. When complete pre-match 1X2 odds snapshots
can be matched to a result, it also compares de-vigged market probabilities.

Generate match-level error diagnostics without changing production predictions:

```bash
python scripts/diagnose_model.py
```

This writes `data/evaluation/diagnostics_latest.json` and prints the highest
impact error patterns plus diagnostic counterfactuals.

Evaluate the frozen experimental calibrated model without changing production:

```bash
python scripts/evaluate_calibrated_v2.py
```

This writes `data/evaluation/calibrated_v2_report.json` with comparisons
against the current model and Elo baseline.

Run independent chronological tuning and validation for v2:

```bash
python scripts/validate_calibrated_v2.py
```

This writes `data/evaluation/calibrated_v2_validation.json`. The newest
matchdays are held out completely and are not used for parameter selection.

Evaluate the experimental Elo-first context model on the same holdout:

```bash
python scripts/validate_elo_context_v3.py
```

This writes `data/evaluation/elo_context_v3_validation.json` with context
feature coverage, tuning-only weights, and validation ablations.

The older context-model experiment writes `data/evaluation/latest.json`:

```bash
python3 -m modeling.src.evaluation.backtest
```

It warms up dynamic Elo ratings on matches before 2022, predicts later matches
using only prior information, batches same-day rating updates, and reports log
loss, Brier score, ranked probability score, accuracy, and calibration.

The current context layer has not passed its promotion gate. See
[docs/MODEL_EVALUATION.md](docs/MODEL_EVALUATION.md).

See [docs/DATA_AND_LIMITATIONS.md](docs/DATA_AND_LIMITATIONS.md) before
interpreting probabilities.
