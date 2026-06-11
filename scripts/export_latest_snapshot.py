#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.api.app.service import (
    DatabasePredictionSource,
    DatabaseSimulationSource,
    PredictionService,
)
from scripts.database import create_database_engine
from scripts.generate_predictions import MODEL_VERSION, load_environment

OUTPUT_PATH = ROOT / "data" / "generated" / "latest.json"


def build_snapshot() -> dict:
    database_url = load_environment().get("DATABASE_URL")
    if not database_url:
        raise RuntimeError("DATABASE_URL is required")
    engine = create_database_engine(database_url)
    try:
        service = PredictionService(
            prediction_source=DatabasePredictionSource(engine),
            simulation_source=DatabaseSimulationSource(engine),
            prediction_cache_seconds=0,
        )
        predictions = service.latest_predictions_payload(force=True)
        simulation = service.latest_simulation()
    finally:
        engine.dispose()
    if predictions["model_version"] != MODEL_VERSION:
        raise RuntimeError(
            f"Latest prediction model is {predictions['model_version']}, "
            f"expected {MODEL_VERSION}"
        )
    if simulation["model_version"] != MODEL_VERSION:
        raise RuntimeError(
            f"Latest simulation model is {simulation['model_version']}, "
            f"expected {MODEL_VERSION}"
        )
    return {
        "model_version": MODEL_VERSION,
        "generated_at": predictions["generated_at"],
        "data_cutoff": predictions["data_cutoff"],
        "predictions": predictions["predictions"],
        "simulation": {
            key: value
            for key, value in simulation.items()
            if key
            not in {
                "model_version",
                "generated_at",
                "created_at",
                "data_cutoff",
                "source",
            }
        },
    }


def main() -> int:
    snapshot = build_snapshot()
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(snapshot, indent=2) + "\n")
    print(
        f"Wrote {OUTPUT_PATH.relative_to(ROOT)} "
        f"predictions={len(snapshot['predictions'])} "
        f"teams={len(snapshot['simulation']['teams'])}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
