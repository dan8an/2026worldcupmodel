#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from modeling.src.evaluation.metrics import calibration_bins, evaluate
from scripts.confidence_v1 import (
    CONFIDENCE_VERSION,
    HIGH_THRESHOLD,
    MEDIUM_THRESHOLD,
    DataCompleteness,
    calculate_confidence,
)
from scripts.evaluate_model import BacktestPrediction, replay_backtest
from scripts.validate_calibrated_v2 import chronological_split
from scripts.validate_elo_context_v3 import (
    EloContextParameters,
    elo_context_probabilities,
)

REPORT_PATH = ROOT / "data" / "evaluation" / "confidence_v1_validation.json"
V3_PARAMETERS = EloContextParameters(
    attack_weight=0.15,
    defense_weight=0.30,
    rest_weight=-0.15,
    draw_multiplier=1.15,
)


def historical_completeness(row: BacktestPrediction) -> DataCompleteness:
    return DataCompleteness(
        team_ratings=True,
        attack_defense_ratings=(
            row.home_attack_rating is not None
            and row.away_attack_rating is not None
            and row.home_defense_rating is not None
            and row.away_defense_rating is not None
        ),
        # No historical point-in-time player archive exists.
        player_ratings=(
            row.home_player_strength is not None
            and row.away_player_strength is not None
        ),
        context=(
            row.home_rest_days is not None
            and row.away_rest_days is not None
        ),
    )


def build_report(
    rows: list[BacktestPrediction] | None = None,
) -> dict[str, Any]:
    if rows is None:
        rows, _ = replay_backtest()
    tuning_rows, validation_rows = chronological_split(rows, 0.22)
    tuning_probabilities = [
        elo_context_probabilities(row, V3_PARAMETERS)
        for row in tuning_rows
    ]
    buckets, _ = calibration_bins(
        tuning_probabilities,
        [row.outcome for row in tuning_rows],
    )
    tiers: dict[str, list[tuple[BacktestPrediction, tuple[float, float, float], float]]] = {
        "Low": [],
        "Medium": [],
        "High": [],
    }
    for row in validation_rows:
        probabilities = elo_context_probabilities(row, V3_PARAMETERS)
        confidence = calculate_confidence(
            row.elo,
            probabilities,
            historical_completeness(row),
            buckets,
        )
        tiers[confidence["confidence_tier"]].append(
            (row, probabilities, confidence["confidence_score"])
        )

    tier_metrics = {}
    for tier, tier_rows in tiers.items():
        tier_metrics[tier] = {
            **evaluate(
                [item[1] for item in tier_rows],
                [item[0].outcome for item in tier_rows],
            ),
            "mean_confidence_score": round(
                sum(item[2] for item in tier_rows) / len(tier_rows),
                1,
            ),
        }
    ordered = (
        tier_metrics["High"]["brier_score"] < tier_metrics["Medium"]["brier_score"]
        < tier_metrics["Low"]["brier_score"]
        and tier_metrics["High"]["log_loss"] < tier_metrics["Medium"]["log_loss"]
        < tier_metrics["Low"]["log_loss"]
    )
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "confidence_version": CONFIDENCE_VERSION,
        "model_version": "elo-context-v3",
        "status": "independent_chronological_validation",
        "split": {
            "tuning_matches": len(tuning_rows),
            "validation_matches": len(validation_rows),
            "validation_start": min(row.played_on for row in validation_rows).isoformat(),
            "validation_end": max(row.played_on for row in validation_rows).isoformat(),
        },
        "thresholds": {
            "medium": MEDIUM_THRESHOLD,
            "high": HIGH_THRESHOLD,
            "selection": "rounded tuning-period 40th and 75th score percentiles",
        },
        "historical_data_limitations": [
            (
                "Player ratings receive no completeness credit because no "
                "point-in-time archive exists."
            ),
            (
                "Calibration reliability is estimated on tuning rows only, "
                "then tested on the holdout."
            ),
        ],
        "tier_metrics": tier_metrics,
        "quality_ordering": {
            "high_beats_medium_beats_low": ordered,
            "criterion": "strictly lower Brier score and log loss at each higher tier",
        },
    }


def main() -> int:
    report = build_report()
    REPORT_PATH.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report["tier_metrics"], indent=2))
    print(
        "Tier ordering: "
        f"{'PASS' if report['quality_ordering']['high_beats_medium_beats_low'] else 'FAIL'}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
