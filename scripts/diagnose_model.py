#!/usr/bin/env python3
from __future__ import annotations

import json
import math
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from modeling.src.data import load_teams
from modeling.src.evaluation.metrics import ProbabilityVector, evaluate
from scripts.evaluate_model import (
    DEFAULT_START_YEAR,
    BacktestPrediction,
    replay_backtest,
)

REPORT_PATH = ROOT / "data" / "evaluation" / "diagnostics_latest.json"
OUTCOME_NAMES = ("home_win", "draw", "away_win")
CONFEDERATIONS = {
    "AFC": {"AUS", "IRN", "IRQ", "JOR", "JPN", "KOR", "KSA", "QAT", "UZB"},
    "CAF": {"ALG", "CIV", "COD", "CPV", "EGY", "GHA", "MAR", "RSA", "SEN", "TUN"},
    "CONCACAF": {"CAN", "CUW", "HAI", "MEX", "PAN", "USA"},
    "CONMEBOL": {"ARG", "BRA", "COL", "ECU", "PAR", "URU"},
    "OFC": {"NZL"},
    "UEFA": {
        "AUT", "BEL", "BIH", "CRO", "CZE", "ENG", "ESP", "FRA", "GER", "NED",
        "NOR", "POR", "SCO", "SUI", "SWE", "TUR",
    },
}


def _brier(probabilities: ProbabilityVector, outcome: int) -> float:
    return sum(
        (probability - (1.0 if index == outcome else 0.0)) ** 2
        for index, probability in enumerate(probabilities)
    )


def _log_loss(probabilities: ProbabilityVector, outcome: int) -> float:
    return -math.log(max(1e-15, probabilities[outcome]))


def _metrics(
    rows: list[BacktestPrediction],
    probabilities: Callable[[BacktestPrediction], ProbabilityVector],
) -> dict[str, Any]:
    return evaluate([probabilities(row) for row in rows], [row.outcome for row in rows])


def _metric_delta(
    rows: list[BacktestPrediction],
    first: Callable[[BacktestPrediction], ProbabilityVector],
    second: Callable[[BacktestPrediction], ProbabilityVector],
) -> dict[str, Any]:
    if not rows:
        return {
            "matches": 0,
            "brier_score_delta": None,
            "log_loss_delta": None,
            "accuracy_delta": None,
        }
    first_metrics = _metrics(rows, first)
    second_metrics = _metrics(rows, second)
    return {
        "matches": len(rows),
        "brier_score_delta": round(
            first_metrics["brier_score"] - second_metrics["brier_score"], 6
        ),
        "log_loss_delta": round(
            first_metrics["log_loss"] - second_metrics["log_loss"], 6
        ),
        "accuracy_delta": round(
            first_metrics["accuracy"] - second_metrics["accuracy"], 6
        ),
    }


def _probability_buckets(rows: list[BacktestPrediction]) -> list[dict[str, Any]]:
    buckets: list[list[tuple[float, float]]] = [[] for _ in range(10)]
    for row in rows:
        for index, probability in enumerate(row.model):
            bucket = min(9, int(probability * 10))
            buckets[bucket].append(
                (probability, 1.0 if row.outcome == index else 0.0)
            )
    output = []
    for index, values in enumerate(buckets):
        predicted = sum(value[0] for value in values) / len(values) if values else None
        observed = sum(value[1] for value in values) / len(values) if values else None
        gap = predicted - observed if predicted is not None and observed is not None else None
        output.append(
            {
                "lower": index / 10,
                "upper": (index + 1) / 10,
                "count": len(values),
                "mean_probability": round(predicted, 6) if predicted is not None else None,
                "observed_rate": round(observed, 6) if observed is not None else None,
                "calibration_gap": round(gap, 6) if gap is not None else None,
                "direction": (
                    "overconfident" if gap and gap > 0 else
                    "underconfident" if gap and gap < 0 else
                    "calibrated"
                ) if gap is not None else None,
            }
        )
    return output


def _top_choice_calibration(rows: list[BacktestPrediction]) -> dict[str, Any]:
    buckets: list[list[tuple[float, float]]] = [[] for _ in range(5)]
    for row in rows:
        predicted = max(range(3), key=lambda index: row.model[index])
        probability = row.model[predicted]
        bucket = min(4, max(0, int((probability - 0.3) / 0.1)))
        buckets[bucket].append((probability, 1.0 if predicted == row.outcome else 0.0))
    output = []
    for index, values in enumerate(buckets):
        mean_probability = (
            sum(value[0] for value in values) / len(values) if values else None
        )
        accuracy = sum(value[1] for value in values) / len(values) if values else None
        output.append(
            {
                "lower": round(0.3 + index * 0.1, 1),
                "upper": round(0.4 + index * 0.1, 1) if index < 4 else 1.0,
                "matches": len(values),
                "mean_top_probability": (
                    round(mean_probability, 6) if mean_probability is not None else None
                ),
                "accuracy": round(accuracy, 6) if accuracy is not None else None,
                "gap": (
                    round(mean_probability - accuracy, 6)
                    if mean_probability is not None and accuracy is not None else None
                ),
            }
        )
    mean_top = sum(max(row.model) for row in rows) / len(rows)
    accuracy = sum(
        max(range(3), key=lambda index: row.model[index]) == row.outcome
        for row in rows
    ) / len(rows)
    return {
        "mean_top_probability": round(mean_top, 6),
        "accuracy": round(accuracy, 6),
        "gap": round(mean_top - accuracy, 6),
        "assessment": "overconfident" if mean_top > accuracy else "underconfident",
        "buckets": output,
    }


def _outcome_diagnostics(rows: list[BacktestPrediction]) -> dict[str, Any]:
    by_actual = {}
    class_components = {}
    for index, name in enumerate(OUTCOME_NAMES):
        actual_rows = [row for row in rows if row.outcome == index]
        model_brier = (
            sum(_brier(row.model, row.outcome) for row in actual_rows)
            / len(actual_rows)
            if actual_rows else None
        )
        elo_brier = (
            sum(_brier(row.elo, row.outcome) for row in actual_rows)
            / len(actual_rows)
            if actual_rows else None
        )
        by_actual[name] = {
            "matches": len(actual_rows),
            "rate": round(len(actual_rows) / len(rows), 6),
            "model_mean_probability": round(
                sum(row.model[index] for row in rows) / len(rows), 6
            ),
            "model_brier_score": round(model_brier, 6) if model_brier is not None else None,
            "elo_brier_score": round(elo_brier, 6) if elo_brier is not None else None,
            "model_minus_elo_brier": (
                round(model_brier - elo_brier, 6)
                if model_brier is not None and elo_brier is not None else None
            ),
        }
        class_components[name] = {
            "model_mean_squared_error": round(
                sum(
                    (row.model[index] - (1.0 if row.outcome == index else 0.0)) ** 2
                    for row in rows
                ) / len(rows),
                6,
            ),
            "elo_mean_squared_error": round(
                sum(
                    (row.elo[index] - (1.0 if row.outcome == index else 0.0)) ** 2
                    for row in rows
                ) / len(rows),
                6,
            ),
        }

    segments: dict[str, list[BacktestPrediction]] = defaultdict(list)
    for row in rows:
        favorite = 0 if row.model[0] >= row.model[2] else 2
        favorite_probability = row.model[favorite]
        if row.outcome == 1:
            segments["draws"].append(row)
        elif favorite_probability < 0.5:
            segments["balanced_decisive_matches"].append(row)
        elif row.outcome == favorite:
            segments["favorite_wins"].append(row)
        else:
            segments["underdog_wins"].append(row)
    segment_metrics = {
        name: {
            **_metric_delta(segment, lambda row: row.model, lambda row: row.elo),
            "model_brier_score": _metrics(segment, lambda row: row.model)["brier_score"],
        }
        for name, segment in segments.items()
        if segment
    }
    return {
        "by_actual_outcome": by_actual,
        "class_error_components": class_components,
        "favorite_underdog_draw_segments": segment_metrics,
    }


def _draw_diagnostics(rows: list[BacktestPrediction]) -> dict[str, Any]:
    predicted = sum(row.model[1] for row in rows) / len(rows)
    elo_predicted = sum(row.elo[1] for row in rows) / len(rows)
    observed = sum(row.outcome == 1 for row in rows) / len(rows)
    return {
        "model_mean_draw_probability": round(predicted, 6),
        "elo_mean_draw_probability": round(elo_predicted, 6),
        "observed_draw_rate": round(observed, 6),
        "model_bias": round(predicted - observed, 6),
        "elo_bias": round(elo_predicted - observed, 6),
        "assessment": "too_high" if predicted > observed else "too_low",
    }


def _xg_diagnostics(rows: list[BacktestPrediction]) -> dict[str, Any]:
    predicted_totals = [row.home_xg + row.away_xg for row in rows]
    actual_totals = [row.home_score + row.away_score for row in rows]
    bias = sum(
        predicted - actual
        for predicted, actual in zip(predicted_totals, actual_totals)
    ) / len(rows)
    return {
        "mean_predicted_total_xg": round(sum(predicted_totals) / len(rows), 6),
        "mean_actual_total_goals": round(sum(actual_totals) / len(rows), 6),
        "mean_error": round(bias, 6),
        "mean_absolute_error": round(
            sum(
                abs(predicted - actual)
                for predicted, actual in zip(predicted_totals, actual_totals)
            ) / len(rows),
            6,
        ),
        "root_mean_squared_error": round(
            math.sqrt(
                sum(
                    (predicted - actual) ** 2
                    for predicted, actual in zip(predicted_totals, actual_totals)
                ) / len(rows)
            ),
            6,
        ),
        "assessment": "too_aggressive" if bias > 0 else "too_conservative",
        "home_xg_bias": round(
            sum(row.home_xg - row.home_score for row in rows) / len(rows), 6
        ),
        "away_xg_bias": round(
            sum(row.away_xg - row.away_score for row in rows) / len(rows), 6
        ),
    }


def _entity_diagnostics(
    rows: list[BacktestPrediction],
    memberships: dict[str, str],
    names: dict[str, str],
    minimum_matches: int,
) -> list[dict[str, Any]]:
    grouped: dict[str, list[BacktestPrediction]] = defaultdict(list)
    for row in rows:
        for team_id in {row.home_team_id, row.away_team_id}:
            entity = memberships.get(team_id)
            if entity:
                grouped[entity].append(row)
    output = []
    for entity, entity_rows in grouped.items():
        if len(entity_rows) < minimum_matches:
            continue
        delta = _metric_delta(
            entity_rows, lambda row: row.model, lambda row: row.elo
        )
        output.append(
            {
                "id": entity,
                "name": names.get(entity, entity),
                **delta,
                "model_brier_score": _metrics(
                    entity_rows, lambda row: row.model
                )["brier_score"],
                "model_log_loss": _metrics(
                    entity_rows, lambda row: row.model
                )["log_loss"],
            }
        )
    return sorted(
        output,
        key=lambda item: (item["brier_score_delta"] or 0.0, item["matches"]),
        reverse=True,
    )


def _poisson_probabilities(home_xg: float, away_xg: float) -> ProbabilityVector:
    scores = []
    for home_goals in range(7):
        for away_goals in range(7):
            probability = (
                math.exp(-home_xg) * home_xg**home_goals / math.factorial(home_goals)
                * math.exp(-away_xg) * away_xg**away_goals / math.factorial(away_goals)
            )
            scores.append((home_goals, away_goals, probability))
    total = sum(score[2] for score in scores)
    home = sum(score[2] for score in scores if score[0] > score[1]) / total
    draw = sum(score[2] for score in scores if score[0] == score[1]) / total
    return home, draw, 1.0 - home - draw


def _renormalize(values: Iterable[float]) -> ProbabilityVector:
    raw = tuple(values)
    total = sum(raw)
    return tuple(value / total for value in raw)  # type: ignore[return-value]


def _counterfactuals(rows: list[BacktestPrediction]) -> dict[str, Any]:
    current = _metrics(rows, lambda row: row.model)

    blends = []
    for model_weight_int in range(0, 101, 5):
        model_weight = model_weight_int / 100
        metrics = _metrics(
            rows,
            lambda row, weight=model_weight: tuple(
                weight * row.model[index] + (1.0 - weight) * row.elo[index]
                for index in range(3)
            ),  # type: ignore[arg-type]
        )
        blends.append((model_weight, metrics))
    best_blend_weight, best_blend = min(
        blends, key=lambda item: item[1]["brier_score"]
    )

    draw_scales = []
    for scale_int in range(50, 151, 5):
        scale = scale_int / 100
        metrics = _metrics(
            rows,
            lambda row, multiplier=scale: _renormalize(
                (row.model[0], row.model[1] * multiplier, row.model[2])
            ),
        )
        draw_scales.append((scale, metrics))
    best_draw_scale, best_draw = min(
        draw_scales, key=lambda item: item[1]["brier_score"]
    )

    xg_scales = []
    for scale_int in range(70, 121, 5):
        scale = scale_int / 100
        metrics = _metrics(
            rows,
            lambda row, multiplier=scale: _poisson_probabilities(
                row.home_xg * multiplier, row.away_xg * multiplier
            ),
        )
        xg_scales.append((scale, metrics))
    best_xg_scale, best_xg = min(xg_scales, key=lambda item: item[1]["brier_score"])

    neutral_rows = [row for row in rows if row.neutral]
    neutral_correction = _metrics(
        neutral_rows,
        lambda row: _poisson_probabilities(row.home_xg / 1.08, row.away_xg),
    )
    neutral_current = _metrics(neutral_rows, lambda row: row.model)
    no_form = _metrics(rows, lambda row: row.no_form)

    def result(
        description: str,
        candidate: dict[str, Any],
        baseline: dict[str, Any] = current,
        **parameters: Any,
    ) -> dict[str, Any]:
        return {
            "description": description,
            **parameters,
            "brier_score": candidate["brier_score"],
            "log_loss": candidate["log_loss"],
            "estimated_brier_improvement": round(
                baseline["brier_score"] - candidate["brier_score"], 6
            ),
            "estimated_log_loss_improvement": round(
                baseline["log_loss"] - candidate["log_loss"], 6
            ),
            "validation_note": "in-sample diagnostic estimate; validate out of sample",
        }

    return {
        "remove_recent_form": result(
            "Set both recent-form ratings to neutral while retaining other inputs.",
            no_form,
        ),
        "blend_with_elo": result(
            "Linear probability blend with the walk-forward Elo baseline.",
            best_blend,
            model_probability_weight=best_blend_weight,
            elo_probability_weight=round(1.0 - best_blend_weight, 2),
        ),
        "draw_probability_scale": result(
            "Multiply draw probability then renormalize all three outcomes.",
            best_draw,
            draw_multiplier=best_draw_scale,
        ),
        "total_xg_scale": result(
            "Scale home and away xG equally before deriving outcome probabilities.",
            best_xg,
            xg_multiplier=best_xg_scale,
        ),
        "remove_neutral_home_advantage": result(
            "Remove the fixed 8% home xG boost on neutral-site matches.",
            neutral_correction,
            baseline=neutral_current,
            matches=len(neutral_rows),
        ),
    }


def _recommendations(
    form: dict[str, Any],
    draw: dict[str, Any],
    xg: dict[str, Any],
    counterfactuals: dict[str, Any],
) -> list[dict[str, Any]]:
    candidates = [
        {
            "change": "Shrink current probabilities toward Elo",
            "reason": "The rating components add error relative to the Elo baseline.",
            **counterfactuals["blend_with_elo"],
        },
        {
            "change": "Dampen or remove recent-form weighting",
            "reason": (
                "The form-neutral ablation improves performance."
                if form["brier_score_delta"] > 0
                else "Recent form helps overall; retain it and tune only weak segments."
            ),
            **counterfactuals["remove_recent_form"],
        },
        {
            "change": "Recalibrate draw probability",
            "reason": (
                f"Mean draw probability bias is {draw['model_bias']:+.4f}."
            ),
            **counterfactuals["draw_probability_scale"],
        },
        {
            "change": "Recalibrate total expected goals",
            "reason": (
                f"Mean total-xG error is {xg['mean_error']:+.3f} goals."
            ),
            **counterfactuals["total_xg_scale"],
        },
        {
            "change": "Disable home advantage for neutral-site matches",
            "reason": "The current generator applies its fixed home boost to every fixture.",
            **counterfactuals["remove_neutral_home_advantage"],
        },
    ]
    recommended = [
        candidate
        for candidate in candidates
        if candidate["estimated_brier_improvement"] > 0
    ]
    return sorted(
        recommended,
        key=lambda item: item["estimated_brier_improvement"],
        reverse=True,
    )


def build_diagnostics(
    rows: list[BacktestPrediction] | None = None,
    start_year: int = DEFAULT_START_YEAR,
) -> dict[str, Any]:
    if rows is None:
        rows, _ = replay_backtest(start_year=start_year)
    team_names = {team.id: team.name for team in load_teams()}
    team_membership = {team_id: team_id for team_id in team_names}
    confederation_membership = {
        team_id: confederation
        for confederation, team_ids in CONFEDERATIONS.items()
        for team_id in team_ids
    }

    years = []
    for year in sorted({row.played_on.year for row in rows}):
        year_rows = [row for row in rows if row.played_on.year == year]
        years.append(
            {
                "year": year,
                **_metric_delta(
                    year_rows, lambda row: row.model, lambda row: row.elo
                ),
                "model_brier_score": _metrics(
                    year_rows, lambda row: row.model
                )["brier_score"],
                "elo_brier_score": _metrics(
                    year_rows, lambda row: row.elo
                )["brier_score"],
            }
        )

    form_delta = _metric_delta(
        rows, lambda row: row.model, lambda row: row.no_form
    )
    form_delta["assessment"] = (
        "hurts" if form_delta["brier_score_delta"] > 0 else "improves"
    )
    form_delta["by_year"] = [
        {
            "year": year,
            **_metric_delta(
                [row for row in rows if row.played_on.year == year],
                lambda row: row.model,
                lambda row: row.no_form,
            ),
        }
        for year in sorted({row.played_on.year for row in rows})
    ]
    draw = _draw_diagnostics(rows)
    xg = _xg_diagnostics(rows)
    counterfactuals = _counterfactuals(rows)
    teams = _entity_diagnostics(
        rows, team_membership, team_names, minimum_matches=8
    )
    confederations = _entity_diagnostics(
        rows,
        confederation_membership,
        {name: name for name in CONFEDERATIONS},
        minimum_matches=10,
    )
    neutral = {
        "neutral": _metric_delta(
            [row for row in rows if row.neutral],
            lambda row: row.model,
            lambda row: row.elo,
        ),
        "non_neutral": _metric_delta(
            [row for row in rows if not row.neutral],
            lambda row: row.model,
            lambda row: row.elo,
        ),
    }
    outcome = _outcome_diagnostics(rows)
    calibration = {
        "top_choice": _top_choice_calibration(rows),
        "all_probability_buckets": _probability_buckets(rows),
    }
    recommendations = _recommendations(form_delta, draw, xg, counterfactuals)

    worst_year = max(years, key=lambda item: item["brier_score_delta"])
    worst_bucket = max(
        (
            bucket for bucket in calibration["all_probability_buckets"]
            if bucket["count"] >= min(20, len(rows))
        ),
        key=lambda item: abs(item["calibration_gap"]),
    )
    actual_errors = outcome["by_actual_outcome"]
    worst_outcome = max(
        (
            item for item in actual_errors.items()
            if item[1]["model_minus_elo_brier"] is not None
        ),
        key=lambda item: item[1]["model_minus_elo_brier"],
    )
    segments = outcome["favorite_underdog_draw_segments"]
    worst_segment = max(
        segments.items(),
        key=lambda item: item[1]["brier_score_delta"],
    )
    worst_team = teams[0] if teams else None
    worst_confederation = confederations[0] if confederations else None
    top_patterns = [
        {
            "pattern": "Favorite/underdog failure",
            "finding": (
                f"{worst_segment[0]}: model minus Elo Brier "
                f"{worst_segment[1]['brier_score_delta']:+.4f} across "
                f"{worst_segment[1]['matches']} matches."
            ),
        },
        {
            "pattern": "Largest annual regression",
            "finding": (
                f"{worst_year['year']}: model minus Elo Brier "
                f"{worst_year['brier_score_delta']:+.4f}."
            ),
        },
        {
            "pattern": "Worst calibration bucket",
            "finding": (
                f"{worst_bucket['lower']:.1f}-{worst_bucket['upper']:.1f}: "
                f"predicted {worst_bucket['mean_probability']:.3f}, observed "
                f"{worst_bucket['observed_rate']:.3f}."
            ),
        },
        {
            "pattern": "Largest outcome-class error",
            "finding": (
                f"{worst_outcome[0]} outcomes add "
                f"{worst_outcome[1]['model_minus_elo_brier']:+.4f} Brier vs Elo."
            ),
        },
        {
            "pattern": "Team and confederation concentration",
            "finding": (
                (
                    f"{worst_team['name']} is worst by team "
                    f"({worst_team['brier_score_delta']:+.4f}); "
                    if worst_team else ""
                )
                + (
                    f"{worst_confederation['name']} is worst by confederation "
                    f"({worst_confederation['brier_score_delta']:+.4f})."
                    if worst_confederation else ""
                )
            ),
        },
    ]

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "model_version": "poisson-ratings-v1",
        "dataset": {
            "matches": len(rows),
            "start_date": min(row.played_on for row in rows).isoformat(),
            "end_date": max(row.played_on for row in rows).isoformat(),
            "protocol": "same leak-free walk-forward replay as evaluate_model.py",
        },
        "top_5_error_patterns": top_patterns,
        "years": sorted(
            years, key=lambda item: item["brier_score_delta"], reverse=True
        ),
        "confidence_and_calibration": calibration,
        "outcome_errors": outcome,
        "draw_probability": draw,
        "expected_goals": xg,
        "teams": {
            "minimum_matches": 8,
            "worst_model_minus_elo": teams[:15],
        },
        "confederations": confederations,
        "neutral_site": neutral,
        "recent_form": form_delta,
        "counterfactual_estimates": counterfactuals,
        "recommended_model_changes": recommendations,
        "limitations": [
            "Counterfactual impacts are in-sample diagnostic estimates, not promotion evidence.",
            "Only the 48 mapped 2026 tournament teams are represented.",
            "Historical player availability and point-in-time player ratings are unavailable.",
            "Team and confederation errors count each match for both participating teams.",
        ],
    }


def print_summary(report: dict[str, Any]) -> None:
    print("Top 5 error patterns")
    for index, item in enumerate(report["top_5_error_patterns"], start=1):
        print(f"{index}. {item['pattern']}: {item['finding']}")
    print("\nRecommended model changes")
    for index, item in enumerate(report["recommended_model_changes"], start=1):
        print(
            f"{index}. {item['change']}: estimated Brier improvement "
            f"{item['estimated_brier_improvement']:+.4f}, log-loss improvement "
            f"{item['estimated_log_loss_improvement']:+.4f}. {item['reason']}"
        )


def main() -> int:
    report = build_diagnostics()
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(json.dumps(report, indent=2) + "\n")
    print_summary(report)
    print(f"\nWrote {REPORT_PATH.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
