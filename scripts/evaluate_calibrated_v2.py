#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from modeling.src.evaluation.metrics import ProbabilityVector, evaluate
from scripts.evaluate_model import BacktestPrediction, replay_backtest

MODEL_VERSION = "poisson-ratings-calibrated-v2"
REPORT_PATH = ROOT / "data" / "evaluation" / "calibrated_v2_report.json"
ELO_WEIGHT = 0.95
CURRENT_MODEL_WEIGHT = 0.05
FAVORITE_SHRINK_FACTOR = 0.95
DRAW_MULTIPLIER = 1.05


@dataclass(frozen=True)
class CalibratedV2Parameters:
    elo_weight: float = ELO_WEIGHT
    form_weight: float = 0.0
    favorite_shrink_factor: float = FAVORITE_SHRINK_FACTOR
    draw_multiplier: float = DRAW_MULTIPLIER


def normalize_probabilities(values: Iterable[float]) -> ProbabilityVector:
    probabilities = tuple(float(value) for value in values)
    total = sum(probabilities)
    if total <= 0:
        raise ValueError("Probabilities must have a positive sum")
    return tuple(value / total for value in probabilities)  # type: ignore[return-value]


def calibrated_v2_probabilities(
    row: BacktestPrediction,
    parameters: CalibratedV2Parameters | None = None,
) -> ProbabilityVector:
    """Apply the frozen v2 calibration recipe to one walk-forward prediction."""
    parameters = parameters or CalibratedV2Parameters()
    current_probabilities = tuple(
        (1.0 - parameters.form_weight) * row.no_form[index]
        + parameters.form_weight * row.model[index]
        for index in range(3)
    )
    probabilities = [
        parameters.elo_weight * row.elo[index]
        + (1.0 - parameters.elo_weight) * current_probabilities[index]
        for index in range(3)
    ]

    favorite = max(range(3), key=probabilities.__getitem__)
    favorite_probability = probabilities[favorite]
    if 0.5 <= favorite_probability < 0.6:
        shrunk_probability = (
            0.5
            + parameters.favorite_shrink_factor * (favorite_probability - 0.5)
        )
        released_probability = favorite_probability - shrunk_probability
        other_probability = 1.0 - favorite_probability
        for index in range(3):
            if index != favorite:
                probabilities[index] += (
                    released_probability * probabilities[index] / other_probability
                )
        probabilities[favorite] = shrunk_probability

    probabilities[1] *= parameters.draw_multiplier
    return normalize_probabilities(probabilities)


def _comparison(
    candidate: dict[str, Any],
    baseline: dict[str, Any],
) -> dict[str, Any]:
    return {
        "brier_score_delta": round(
            candidate["brier_score"] - baseline["brier_score"], 6
        ),
        "log_loss_delta": round(
            candidate["log_loss"] - baseline["log_loss"], 6
        ),
        "accuracy_delta": round(
            candidate["accuracy"] - baseline["accuracy"], 6
        ),
        "beats_on_brier": candidate["brier_score"] < baseline["brier_score"],
        "beats_on_log_loss": candidate["log_loss"] < baseline["log_loss"],
    }


def _top_choice_calibration(
    rows: list[BacktestPrediction],
    probabilities: list[ProbabilityVector],
) -> list[dict[str, Any]]:
    buckets: list[list[tuple[float, float]]] = [[] for _ in range(5)]
    for row, vector in zip(rows, probabilities):
        favorite = max(range(3), key=vector.__getitem__)
        probability = vector[favorite]
        bucket = min(4, max(0, int((probability - 0.3) / 0.1)))
        buckets[bucket].append(
            (probability, 1.0 if favorite == row.outcome else 0.0)
        )

    output = []
    for index, values in enumerate(buckets):
        mean_probability = (
            sum(value[0] for value in values) / len(values) if values else None
        )
        observed_rate = (
            sum(value[1] for value in values) / len(values) if values else None
        )
        output.append(
            {
                "lower": round(0.3 + index * 0.1, 1),
                "upper": round(0.4 + index * 0.1, 1) if index < 4 else 1.0,
                "matches": len(values),
                "mean_probability": (
                    round(mean_probability, 6)
                    if mean_probability is not None else None
                ),
                "observed_rate": (
                    round(observed_rate, 6)
                    if observed_rate is not None else None
                ),
                "calibration_gap": (
                    round(mean_probability - observed_rate, 6)
                    if mean_probability is not None and observed_rate is not None
                    else None
                ),
            }
        )
    return output


def build_report(
    rows: list[BacktestPrediction] | None = None,
) -> dict[str, Any]:
    if rows is None:
        rows, _ = replay_backtest()
    outcomes = [row.outcome for row in rows]
    v2_probabilities = [calibrated_v2_probabilities(row) for row in rows]
    current_metrics = evaluate([row.model for row in rows], outcomes)
    elo_metrics = evaluate([row.elo for row in rows], outcomes)
    v2_metrics = evaluate(v2_probabilities, outcomes)

    versus_current = _comparison(v2_metrics, current_metrics)
    versus_elo = _comparison(v2_metrics, elo_metrics)
    passes_metric_gate = all(
        (
            versus_current["beats_on_brier"],
            versus_current["beats_on_log_loss"],
            versus_elo["beats_on_brier"],
            versus_elo["beats_on_log_loss"],
        )
    )

    yearly = {}
    for year in sorted({row.played_on.year for row in rows}):
        indices = [
            index for index, row in enumerate(rows) if row.played_on.year == year
        ]
        yearly[str(year)] = {
            "v2": evaluate(
                [v2_probabilities[index] for index in indices],
                [outcomes[index] for index in indices],
            ),
            "current_model": evaluate(
                [rows[index].model for index in indices],
                [outcomes[index] for index in indices],
            ),
            "elo_baseline": evaluate(
                [rows[index].elo for index in indices],
                [outcomes[index] for index in indices],
            ),
        }

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "model_version": MODEL_VERSION,
        "status": "experimental",
        "dataset": {
            "matches": len(rows),
            "start_date": min(row.played_on for row in rows).isoformat(),
            "end_date": max(row.played_on for row in rows).isoformat(),
            "protocol": "same leak-free walk-forward replay as evaluate_model.py",
        },
        "recipe": {
            "elo_probability_weight": ELO_WEIGHT,
            "current_model_probability_weight": CURRENT_MODEL_WEIGHT,
            "current_model_form": "removed by using form-neutral replay probabilities",
            "favorite_calibration": {
                "range": [0.5, 0.6],
                "formula": "0.5 + 0.95 * (favorite_probability - 0.5)",
                "released_probability": "distributed proportionally to other outcomes",
            },
            "draw_probability_multiplier": DRAW_MULTIPLIER,
            "final_normalization": True,
        },
        "metrics": {
            "v2": v2_metrics,
            "current_model": current_metrics,
            "elo_baseline": elo_metrics,
        },
        "comparison": {
            "v2_vs_current_model": versus_current,
            "v2_vs_elo_baseline": versus_elo,
        },
        "calibration_table": v2_metrics["calibration_bins"],
        "top_choice_calibration": _top_choice_calibration(rows, v2_probabilities),
        "years": yearly,
        "promotion": {
            "passes_metric_gate": passes_metric_gate,
            "should_be_promoted": False,
            "should_advance_to_independent_holdout": passes_metric_gate,
            "gate": "lower Brier score and log loss than both current model and Elo",
            "production_changed": False,
            "decision": (
                "hold production; validate on an independent chronological holdout"
                if passes_metric_gate
                else "do not promote"
            ),
            "caveat": (
                "The calibration recipe was derived from diagnostics on this same "
                "557-match dataset, so the measured gain is not independent evidence."
            ),
        },
    }


def print_report(report: dict[str, Any]) -> None:
    metrics = report["metrics"]
    print(f"Model: {report['model_version']}")
    print(f"Matches: {report['dataset']['matches']}")
    print(
        "Brier score: "
        f"v2={metrics['v2']['brier_score']:.6f}, "
        f"current={metrics['current_model']['brier_score']:.6f}, "
        f"Elo={metrics['elo_baseline']['brier_score']:.6f}"
    )
    print(
        "Log loss: "
        f"v2={metrics['v2']['log_loss']:.6f}, "
        f"current={metrics['current_model']['log_loss']:.6f}, "
        f"Elo={metrics['elo_baseline']['log_loss']:.6f}"
    )
    print("\nCalibration table")
    print("bucket  count  predicted  observed  gap")
    for bucket in report["calibration_table"]:
        gap = bucket["mean_probability"] - bucket["observed_rate"]
        print(
            f"{bucket['lower']:.1f}-{bucket['upper']:.1f}"
            f"  {bucket['count']:5d}"
            f"  {bucket['mean_probability']:.3f}"
            f"      {bucket['observed_rate']:.3f}"
            f"    {gap:+.3f}"
        )
    promotion = report["promotion"]
    print(
        "\nPromotion: "
        f"{'NO' if not promotion['should_be_promoted'] else 'YES'} "
        f"({promotion['decision']}; metric gate "
        f"{'passed' if promotion['passes_metric_gate'] else 'failed'})"
    )


def main() -> int:
    report = build_report()
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(json.dumps(report, indent=2) + "\n")
    print_report(report)
    print(f"\nWrote {REPORT_PATH.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
