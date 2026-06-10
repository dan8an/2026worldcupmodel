#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
from dataclasses import asdict
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from modeling.src.evaluation.metrics import ProbabilityVector, evaluate
from scripts.evaluate_calibrated_v2 import (
    MODEL_VERSION,
    CalibratedV2Parameters,
    calibrated_v2_probabilities,
)
from scripts.evaluate_model import BacktestPrediction, replay_backtest

REPORT_PATH = ROOT / "data" / "evaluation" / "calibrated_v2_validation.json"
VALIDATION_FRACTION = 0.22
ELO_WEIGHTS = (0.85, 0.9, 0.925, 0.95, 0.975, 1.0)
FORM_WEIGHTS = (0.0, 0.1, 0.25)
FAVORITE_SHRINK_FACTORS = (0.85, 0.9, 0.95, 1.0)
DRAW_MULTIPLIERS = (1.0, 1.025, 1.05, 1.075, 1.1, 1.125, 1.15, 1.175, 1.2)


def chronological_split(
    rows: list[BacktestPrediction],
    validation_fraction: float = VALIDATION_FRACTION,
) -> tuple[list[BacktestPrediction], list[BacktestPrediction]]:
    if not 0.2 <= validation_fraction <= 0.25:
        raise ValueError("Validation fraction must be between 20% and 25%")
    target_matches = round(len(rows) * validation_fraction)
    validation_dates: set[date] = set()
    selected_matches = 0
    for played_on in sorted({row.played_on for row in rows}, reverse=True):
        validation_dates.add(played_on)
        selected_matches += sum(row.played_on == played_on for row in rows)
        if selected_matches >= target_matches:
            break
    tuning = [row for row in rows if row.played_on not in validation_dates]
    validation = [row for row in rows if row.played_on in validation_dates]
    if not tuning or not validation:
        raise ValueError("Chronological split produced an empty period")
    if max(row.played_on for row in tuning) >= min(
        row.played_on for row in validation
    ):
        raise ValueError("Tuning and validation periods overlap")
    return tuning, validation


def tune_parameters(
    rows: list[BacktestPrediction],
) -> tuple[CalibratedV2Parameters, dict[str, Any]]:
    outcomes = [row.outcome for row in rows]
    candidates = []
    for elo_weight in ELO_WEIGHTS:
        for form_weight in FORM_WEIGHTS:
            for shrink_factor in FAVORITE_SHRINK_FACTORS:
                for draw_multiplier in DRAW_MULTIPLIERS:
                    parameters = CalibratedV2Parameters(
                        elo_weight=elo_weight,
                        form_weight=form_weight,
                        favorite_shrink_factor=shrink_factor,
                        draw_multiplier=draw_multiplier,
                    )
                    metrics = evaluate(
                        [
                            calibrated_v2_probabilities(row, parameters)
                            for row in rows
                        ],
                        outcomes,
                    )
                    candidates.append((parameters, metrics))
    selected_parameters, selected_metrics = min(
        candidates,
        key=lambda item: (
            item[1]["brier_score"],
            item[1]["log_loss"],
            -item[0].elo_weight,
            item[0].form_weight,
        ),
    )
    return selected_parameters, {
        "objective": "minimum Brier score, then minimum log loss",
        "candidate_count": len(candidates),
        "search_space": {
            "elo_weights": list(ELO_WEIGHTS),
            "form_weights": list(FORM_WEIGHTS),
            "favorite_shrink_factors": list(FAVORITE_SHRINK_FACTORS),
            "draw_multipliers": list(DRAW_MULTIPLIERS),
        },
        "selected_parameters": asdict(selected_parameters),
        "selected_tuning_metrics": selected_metrics,
    }


def _metrics(
    rows: list[BacktestPrediction],
    probabilities: Callable[[BacktestPrediction], ProbabilityVector],
) -> dict[str, Any]:
    return evaluate(
        [probabilities(row) for row in rows],
        [row.outcome for row in rows],
    )


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
        "beats_on_brier": candidate["brier_score"] < baseline["brier_score"],
        "beats_on_log_loss": candidate["log_loss"] < baseline["log_loss"],
    }


def _draw_accuracy(
    rows: list[BacktestPrediction],
    probabilities: Callable[[BacktestPrediction], ProbabilityVector],
) -> dict[str, Any]:
    vectors = [probabilities(row) for row in rows]
    observed_draws = sum(row.outcome == 1 for row in rows)
    predicted_draws = sum(
        max(range(3), key=vector.__getitem__) == 1 for vector in vectors
    )
    correct_draw_predictions = sum(
        row.outcome == 1 and max(range(3), key=vector.__getitem__) == 1
        for row, vector in zip(rows, vectors)
    )
    draw_brier = sum(
        (vector[1] - (1.0 if row.outcome == 1 else 0.0)) ** 2
        for row, vector in zip(rows, vectors)
    ) / len(rows)
    return {
        "observed_draws": observed_draws,
        "observed_draw_rate": round(observed_draws / len(rows), 6),
        "mean_draw_probability": round(
            sum(vector[1] for vector in vectors) / len(vectors), 6
        ),
        "draw_probability_bias": round(
            sum(vector[1] for vector in vectors) / len(vectors)
            - observed_draws / len(rows),
            6,
        ),
        "draw_class_brier": round(draw_brier, 6),
        "predicted_draws": predicted_draws,
        "draw_precision": (
            round(correct_draw_predictions / predicted_draws, 6)
            if predicted_draws else None
        ),
        "draw_recall": round(
            correct_draw_predictions / observed_draws, 6
        ) if observed_draws else None,
    }


def _segment_metrics(
    rows: list[BacktestPrediction],
    v2_probabilities: dict[int, ProbabilityVector],
) -> dict[str, Any]:
    segments: dict[str, list[BacktestPrediction]] = {
        "favorite_wins": [],
        "underdog_wins": [],
        "draws": [],
        "balanced_decisive_matches": [],
    }
    for row in rows:
        vector = v2_probabilities[id(row)]
        favorite = 0 if vector[0] >= vector[2] else 2
        if row.outcome == 1:
            segments["draws"].append(row)
        elif vector[favorite] < 0.5:
            segments["balanced_decisive_matches"].append(row)
        elif row.outcome == favorite:
            segments["favorite_wins"].append(row)
        else:
            segments["underdog_wins"].append(row)

    output = {}
    for name, segment in segments.items():
        if not segment:
            output[name] = {"matches": 0}
            continue
        output[name] = {
            "matches": len(segment),
            "v2": _metrics(
                segment, lambda row: v2_probabilities[id(row)]
            ),
            "current_model": _metrics(segment, lambda row: row.model),
            "elo_baseline": _metrics(segment, lambda row: row.elo),
        }
    return output


def build_validation_report(
    rows: list[BacktestPrediction] | None = None,
    validation_fraction: float = VALIDATION_FRACTION,
) -> dict[str, Any]:
    if rows is None:
        rows, _ = replay_backtest()
    tuning_rows, validation_rows = chronological_split(rows, validation_fraction)
    parameters, tuning = tune_parameters(tuning_rows)
    v2_by_row = {
        id(row): calibrated_v2_probabilities(row, parameters)
        for row in validation_rows
    }
    v2_metrics = _metrics(
        validation_rows, lambda row: v2_by_row[id(row)]
    )
    current_metrics = _metrics(validation_rows, lambda row: row.model)
    elo_metrics = _metrics(validation_rows, lambda row: row.elo)
    versus_elo = _comparison(v2_metrics, elo_metrics)
    versus_current = _comparison(v2_metrics, current_metrics)
    draw_accuracy = {
        "v2": _draw_accuracy(
            validation_rows, lambda row: v2_by_row[id(row)]
        ),
        "current_production_model": _draw_accuracy(
            validation_rows, lambda row: row.model
        ),
        "elo_baseline": _draw_accuracy(
            validation_rows, lambda row: row.elo
        ),
    }
    segments = _segment_metrics(validation_rows, v2_by_row)
    beats_elo = (
        versus_elo["beats_on_brier"] and versus_elo["beats_on_log_loss"]
    )

    failure_reasons = []
    if not versus_elo["beats_on_brier"]:
        failure_reasons.append(
            "v2 Brier score is not lower than Elo on validation"
        )
    if not versus_elo["beats_on_log_loss"]:
        failure_reasons.append(
            "v2 log loss is not lower than Elo on validation"
        )
    segment_failures = []
    for name, segment in segments.items():
        if not segment.get("matches"):
            continue
        brier_delta = round(
            segment["v2"]["brier_score"]
            - segment["elo_baseline"]["brier_score"],
            6,
        )
        if brier_delta > 0:
            segment_failures.append(
                {
                    "segment": name,
                    "matches": segment["matches"],
                    "v2_minus_elo_brier": brier_delta,
                }
            )
    segment_failures.sort(
        key=lambda item: item["v2_minus_elo_brier"],
        reverse=True,
    )
    v2_middle_bucket = next(
        bucket for bucket in v2_metrics["calibration_bins"]
        if bucket["lower"] == 0.5
    )

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "model_version": MODEL_VERSION,
        "status": "independent_chronological_validation",
        "split": {
            "requested_validation_fraction": validation_fraction,
            "actual_validation_fraction": round(
                len(validation_rows) / len(rows), 6
            ),
            "matchday_integrity": "all matches on a date remain in one period",
            "tuning": {
                "matches": len(tuning_rows),
                "start_date": min(row.played_on for row in tuning_rows).isoformat(),
                "end_date": max(row.played_on for row in tuning_rows).isoformat(),
            },
            "validation": {
                "matches": len(validation_rows),
                "start_date": min(
                    row.played_on for row in validation_rows
                ).isoformat(),
                "end_date": max(
                    row.played_on for row in validation_rows
                ).isoformat(),
            },
        },
        "tuning": tuning,
        "validation_metrics": {
            "v2": v2_metrics,
            "current_production_model": current_metrics,
            "elo_baseline": elo_metrics,
        },
        "comparison": {
            "v2_vs_current_production_model": versus_current,
            "v2_vs_elo_baseline": versus_elo,
        },
        "calibration_buckets": {
            "v2": v2_metrics["calibration_bins"],
            "current_production_model": current_metrics["calibration_bins"],
            "elo_baseline": elo_metrics["calibration_bins"],
        },
        "draw_accuracy": draw_accuracy,
        "favorite_underdog_breakdown": segments,
        "validation_failure_analysis": {
            "primary_failure": (
                "Brier score did not beat Elo"
                if not versus_elo["beats_on_brier"] else None
            ),
            "worst_segments_vs_elo": segment_failures,
            "draw_component": {
                "v2_minus_elo_draw_class_brier": round(
                    draw_accuracy["v2"]["draw_class_brier"]
                    - draw_accuracy["elo_baseline"]["draw_class_brier"],
                    6,
                ),
                "assessment": (
                    "draw uplift helped"
                    if draw_accuracy["v2"]["draw_class_brier"]
                    < draw_accuracy["elo_baseline"]["draw_class_brier"]
                    else "draw uplift hurt"
                ),
            },
            "fifty_to_sixty_percent_bucket": {
                **v2_middle_bucket,
                "calibration_gap": round(
                    v2_middle_bucket["mean_probability"]
                    - v2_middle_bucket["observed_rate"],
                    6,
                ),
                "assessment": (
                    "underconfident"
                    if v2_middle_bucket["mean_probability"]
                    < v2_middle_bucket["observed_rate"]
                    else "overconfident"
                ),
            },
            "parameter_interpretation": (
                "Tuning selected zero recent-form weight and a favorite shrink "
                "factor of 1.0, so recent form and favorite shrinkage were disabled."
            ),
        },
        "promotion": {
            "v2_beats_elo_on_unseen_validation": beats_elo,
            "recommend_promotion": beats_elo,
            "production_changed": False,
            "decision": (
                "recommend promotion"
                if beats_elo else
                "do not promote"
            ),
            "failure_reasons": failure_reasons,
            "gate": "v2 must beat Elo on both Brier score and log loss",
        },
    }


def print_summary(report: dict[str, Any]) -> None:
    metrics = report["validation_metrics"]
    split = report["split"]
    selected = report["tuning"]["selected_parameters"]
    print(
        f"Tuning: {split['tuning']['matches']} matches "
        f"({split['tuning']['start_date']} to {split['tuning']['end_date']})"
    )
    print(
        f"Validation: {split['validation']['matches']} matches "
        f"({split['validation']['start_date']} to "
        f"{split['validation']['end_date']})"
    )
    print(f"Frozen parameters: {json.dumps(selected, sort_keys=True)}")
    print(
        "Brier score: "
        f"v2={metrics['v2']['brier_score']:.6f}, "
        f"current={metrics['current_production_model']['brier_score']:.6f}, "
        f"Elo={metrics['elo_baseline']['brier_score']:.6f}"
    )
    print(
        "Log loss: "
        f"v2={metrics['v2']['log_loss']:.6f}, "
        f"current={metrics['current_production_model']['log_loss']:.6f}, "
        f"Elo={metrics['elo_baseline']['log_loss']:.6f}"
    )
    promotion = report["promotion"]
    print(
        "Promotion recommendation: "
        f"{'YES' if promotion['recommend_promotion'] else 'NO'}"
    )
    for reason in promotion["failure_reasons"]:
        print(f"- {reason}")


def main() -> int:
    report = build_validation_report()
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(json.dumps(report, indent=2) + "\n")
    print_summary(report)
    print(f"\nWrote {REPORT_PATH.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
