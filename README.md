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

## Evaluate the model

The chronological backtest writes `data/evaluation/latest.json`:

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
