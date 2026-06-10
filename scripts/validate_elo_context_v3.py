#!/usr/bin/env python3
from __future__ import annotations

import json
import math
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from modeling.src.evaluation.metrics import ProbabilityVector, evaluate
from scripts.evaluate_calibrated_v2 import normalize_probabilities
from scripts.evaluate_model import BacktestPrediction, replay_backtest
from scripts.validate_calibrated_v2 import chronological_split

MODEL_VERSION = "elo-context-v3"
REPORT_PATH = ROOT / "data" / "evaluation" / "elo_context_v3_validation.json"
ATTACK_WEIGHTS = (-0.3, -0.15, 0.0, 0.15, 0.3)
DEFENSE_WEIGHTS = (-0.3, -0.15, 0.0, 0.15, 0.3)
REST_WEIGHTS = (-0.15, 0.0, 0.15)
DRAW_MULTIPLIERS = (0.95, 1.0, 1.025, 1.05, 1.075, 1.1, 1.15)


@dataclass(frozen=True)
class EloContextParameters:
    attack_weight: float = 0.0
    defense_weight: float = 0.0
    player_weight: float = 0.0
    rest_weight: float = 0.0
    travel_weight: float = 0.0
    availability_weight: float = 0.0
    draw_multiplier: float = 1.0


def _difference(home: float | None, away: float | None, scale: float) -> float:
    if home is None or away is None:
        return 0.0
    return max(-1.0, min(1.0, (home - away) / scale))


def context_signals(row: BacktestPrediction) -> dict[str, float]:
    return {
        "attack": _difference(
            row.home_attack_rating, row.away_attack_rating, 100.0
        ),
        "defense": _difference(
            row.home_defense_rating, row.away_defense_rating, 100.0
        ),
        "player": _difference(
            row.home_player_strength, row.away_player_strength, 100.0
        ),
        "rest": _difference(row.home_rest_days, row.away_rest_days, 14.0),
        # Less travel is favorable, hence away minus home.
        "travel": _difference(row.away_travel_km, row.home_travel_km, 5000.0),
        "availability": _difference(
            row.home_availability_adjustment,
            row.away_availability_adjustment,
            50.0,
        ),
    }


def elo_context_probabilities(
    row: BacktestPrediction,
    parameters: EloContextParameters,
) -> ProbabilityVector:
    signals = context_signals(row)
    tilt = (
        parameters.attack_weight * signals["attack"]
        + parameters.defense_weight * signals["defense"]
        + parameters.player_weight * signals["player"]
        + parameters.rest_weight * signals["rest"]
        + parameters.travel_weight * signals["travel"]
        + parameters.availability_weight * signals["availability"]
    )
    values = (
        row.elo[0] * math.exp(tilt),
        row.elo[1] * parameters.draw_multiplier,
        row.elo[2] * math.exp(-tilt),
    )
    return normalize_probabilities(values)


def _parameter_grid(ablation: str) -> Iterable[EloContextParameters]:
    attack_weights = ATTACK_WEIGHTS if ablation in {
        "elo_attack_defense", "elo_all_context"
    } else (0.0,)
    defense_weights = DEFENSE_WEIGHTS if ablation in {
        "elo_attack_defense", "elo_all_context"
    } else (0.0,)
    rest_weights = REST_WEIGHTS if ablation == "elo_all_context" else (0.0,)
    draw_multipliers = DRAW_MULTIPLIERS if ablation in {
        "elo_draw_calibration", "elo_all_context"
    } else (1.0,)
    for attack_weight in attack_weights:
        for defense_weight in defense_weights:
            for rest_weight in rest_weights:
                for draw_multiplier in draw_multipliers:
                    yield EloContextParameters(
                        attack_weight=attack_weight,
                        defense_weight=defense_weight,
                        rest_weight=rest_weight,
                        draw_multiplier=draw_multiplier,
                    )


def tune_ablation(
    rows: list[BacktestPrediction],
    ablation: str,
) -> tuple[EloContextParameters, dict[str, Any]]:
    outcomes = [row.outcome for row in rows]
    candidates = []
    for parameters in _parameter_grid(ablation):
        metrics = evaluate(
            [elo_context_probabilities(row, parameters) for row in rows],
            outcomes,
        )
        candidates.append((parameters, metrics))
    parameters, metrics = min(
        candidates,
        key=lambda item: (
            item[1]["brier_score"],
            item[1]["log_loss"],
            sum(abs(value) for value in asdict(item[0]).values()),
        ),
    )
    return parameters, {
        "candidate_count": len(candidates),
        "selected_parameters": asdict(parameters),
        "tuning_metrics": metrics,
    }


def _metrics(
    rows: list[BacktestPrediction],
    parameters: EloContextParameters,
) -> dict[str, Any]:
    return evaluate(
        [elo_context_probabilities(row, parameters) for row in rows],
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


def _segment_breakdown(
    rows: list[BacktestPrediction],
    parameters: EloContextParameters,
) -> dict[str, Any]:
    segments: dict[str, list[BacktestPrediction]] = {
        "favorite_wins": [],
        "underdog_wins": [],
        "draws": [],
        "balanced_decisive_matches": [],
    }
    for row in rows:
        vector = elo_context_probabilities(row, parameters)
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
            "v3": _metrics(segment, parameters),
            "elo_baseline": evaluate(
                [row.elo for row in segment],
                [row.outcome for row in segment],
            ),
        }
    return output


def _feature_coverage(rows: list[BacktestPrediction]) -> dict[str, Any]:
    def coverage(home_name: str, away_name: str) -> dict[str, Any]:
        available = sum(
            getattr(row, home_name) is not None
            and getattr(row, away_name) is not None
            for row in rows
        )
        return {
            "matches_available": available,
            "coverage": round(available / len(rows), 6),
        }

    return {
        "attack_rating": coverage(
            "home_attack_rating", "away_attack_rating"
        ),
        "defense_rating": coverage(
            "home_defense_rating", "away_defense_rating"
        ),
        "player_strength": {
            **coverage("home_player_strength", "away_player_strength"),
            "status": "unavailable historically; no point-in-time player archive",
        },
        "rest": coverage("home_rest_days", "away_rest_days"),
        "travel": {
            **coverage("home_travel_km", "away_travel_km"),
            "status": "unavailable; historical venue coordinates are not retained",
        },
        "injuries_suspensions": {
            **coverage(
                "home_availability_adjustment",
                "away_availability_adjustment",
            ),
            "status": "unavailable historically; current reports are not backfilled",
        },
        "recent_form": {
            "coverage": 1.0,
            "status": "disabled; independent v2 tuning selected zero weight",
        },
    }


def build_report(
    rows: list[BacktestPrediction] | None = None,
) -> dict[str, Any]:
    if rows is None:
        rows, _ = replay_backtest()
    tuning_rows, validation_rows = chronological_split(rows, 0.22)
    ablation_names = (
        "elo_only",
        "elo_attack_defense",
        "elo_player_ratings",
        "elo_draw_calibration",
        "elo_all_context",
    )
    tuning_results = {}
    validation_results = {}
    for name in ablation_names:
        parameters, tuning = tune_ablation(tuning_rows, name)
        validation_metrics = _metrics(validation_rows, parameters)
        tuning_results[name] = tuning
        validation_results[name] = {
            "parameters": asdict(parameters),
            "metrics": validation_metrics,
        }

    elo_metrics = validation_results["elo_only"]["metrics"]
    all_context_metrics = validation_results["elo_all_context"]["metrics"]
    comparison = _comparison(all_context_metrics, elo_metrics)
    beats_elo = (
        comparison["beats_on_brier"] and comparison["beats_on_log_loss"]
    )
    all_context_parameters = EloContextParameters(
        **validation_results["elo_all_context"]["parameters"]
    )

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "model_version": MODEL_VERSION,
        "status": "experimental_chronological_validation",
        "split": {
            "tuning_matches": len(tuning_rows),
            "tuning_start": min(row.played_on for row in tuning_rows).isoformat(),
            "tuning_end": max(row.played_on for row in tuning_rows).isoformat(),
            "validation_matches": len(validation_rows),
            "validation_start": min(
                row.played_on for row in validation_rows
            ).isoformat(),
            "validation_end": max(
                row.played_on for row in validation_rows
            ).isoformat(),
            "matches_on_same_date_kept_together": True,
        },
        "feature_coverage": _feature_coverage(rows),
        "tuning": {
            "objective": "minimum tuning Brier score, then log loss",
            "search_space": {
                "attack_weights": list(ATTACK_WEIGHTS),
                "defense_weights": list(DEFENSE_WEIGHTS),
                "rest_weights": list(REST_WEIGHTS),
                "draw_multipliers": list(DRAW_MULTIPLIERS),
            },
            "ablations": tuning_results,
        },
        "validation_ablations": validation_results,
        "v3_vs_elo": comparison,
        "calibration": {
            "v3": all_context_metrics["calibration_bins"],
            "elo_baseline": elo_metrics["calibration_bins"],
        },
        "favorite_underdog_breakdown": _segment_breakdown(
            validation_rows, all_context_parameters
        ),
        "promotion": {
            "v3_beats_elo_on_validation": beats_elo,
            "recommend_promotion": beats_elo,
            "production_changed": False,
            "gate": "lower Brier score and log loss than Elo on validation",
            "decision": "recommend promotion" if beats_elo else "do not promote",
            "failure_reasons": [
                reason
                for reason, failed in (
                    (
                        "v3 Brier score is not lower than Elo",
                        not comparison["beats_on_brier"],
                    ),
                    (
                        "v3 log loss is not lower than Elo",
                        not comparison["beats_on_log_loss"],
                    ),
                )
                if failed
            ],
        },
    }


def print_summary(report: dict[str, Any]) -> None:
    print(f"Model: {MODEL_VERSION}")
    print(
        f"Validation: {report['split']['validation_matches']} matches "
        f"({report['split']['validation_start']} to "
        f"{report['split']['validation_end']})"
    )
    print("Ablation validation results")
    for name, result in report["validation_ablations"].items():
        metrics = result["metrics"]
        print(
            f"- {name}: Brier={metrics['brier_score']:.6f}, "
            f"log_loss={metrics['log_loss']:.6f}, "
            f"parameters={json.dumps(result['parameters'], sort_keys=True)}"
        )
    promotion = report["promotion"]
    print(
        "Promotion recommendation: "
        f"{'YES' if promotion['recommend_promotion'] else 'NO'}"
    )
    for reason in promotion["failure_reasons"]:
        print(f"- {reason}")


def main() -> int:
    report = build_report()
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(json.dumps(report, indent=2) + "\n")
    print_summary(report)
    print(f"\nWrote {REPORT_PATH.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
