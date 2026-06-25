#!/usr/bin/env python3
from __future__ import annotations

import json
import math
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from sqlalchemy import MetaData, Table, inspect, select

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from modeling.src.evaluation.metrics import ProbabilityVector, brier_score, log_loss
from scripts.database import create_database_engine
from scripts.evaluate_calibrated_v2 import normalize_probabilities
from scripts.generate_predictions import (
    MODEL_VERSION,
    SHOT_VOLUME_WEIGHT,
    V3_ATTACK_WEIGHT,
    V3_DEFENSE_WEIGHT,
    build_score_probabilities,
    calculate_prediction,
    projected_total_goals_proxy,
)
from scripts.validate_xg_proxy_v4 import build_validation_rows, load_database_matches

REPORT_PATH = ROOT / "data" / "evaluation" / "v421_draw_backtest_report.json"
HIGH_DRAW_THRESHOLD = 0.30
SENSITIVITY_DRAW_THRESHOLD = 0.25
DRAW_BUCKETS = (
    (0.00, 0.15, "0-15%"),
    (0.15, 0.20, "15-20%"),
    (0.20, 0.25, "20-25%"),
    (0.25, 0.30, "25-30%"),
    (0.30, 1.01, "30%+"),
)


def _round(value: float | None) -> float | None:
    return round(value, 6) if value is not None and math.isfinite(value) else None


def _outcome_name(outcome: int) -> str:
    return ("home", "draw", "away")[outcome]


def _v41_probabilities(row: Any) -> ProbabilityVector:
    """Reconstruct production v4.1: v4 no-rest plus validated shot-volume tilt."""
    base = row.v4_no_rest or row.v3
    tilt = row.shot_volume_signal * SHOT_VOLUME_WEIGHT
    return normalize_probabilities(
        (
            base[0] * math.exp(tilt),
            base[1],
            base[2] * math.exp(-tilt),
        )
    )


def _clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, value))


def _production_side_tilt_base(row: Any) -> tuple[ProbabilityVector, float, float]:
    elo_gap = row.home_elo - row.away_elo
    home_xg = _clamp(1.35 * math.exp(elo_gap / 800.0), 0.2, 4.5)
    away_xg = _clamp(1.35 * math.exp(-elo_gap / 800.0), 0.2, 4.5)
    scores = build_score_probabilities(home_xg, away_xg)
    elo = (
        sum(
            float(score["probability"])
            for score in scores
            if score["home_goals"] > score["away_goals"]
        ),
        sum(
            float(score["probability"])
            for score in scores
            if score["home_goals"] == score["away_goals"]
        ),
        sum(
            float(score["probability"])
            for score in scores
            if score["home_goals"] < score["away_goals"]
        ),
    )
    attack_signal = _clamp(
        (row.home_attack_rating - row.away_attack_rating) / 100.0,
        -1.0,
        1.0,
    )
    defense_signal = _clamp(
        (row.home_defense_rating - row.away_defense_rating) / 100.0,
        -1.0,
        1.0,
    )
    attack_defense_tilt = (
        V3_ATTACK_WEIGHT * attack_signal
        + V3_DEFENSE_WEIGHT * defense_signal
    )
    attack_defense = normalize_probabilities(
        (
            elo[0] * math.exp(attack_defense_tilt),
            elo[1],
            elo[2] * math.exp(-attack_defense_tilt),
        )
    )
    shot_volume_signal = _clamp(
        (row.home_shot_volume_rating - row.away_shot_volume_rating) / 100.0,
        -1.0,
        1.0,
    )
    shot_volume_tilt = shot_volume_signal * SHOT_VOLUME_WEIGHT
    return (
        normalize_probabilities(
            (
                attack_defense[0] * math.exp(shot_volume_tilt),
                attack_defense[1],
                attack_defense[2] * math.exp(-shot_volume_tilt),
            )
        ),
        elo_gap,
        projected_total_goals_proxy(
            home_xg,
            away_xg,
            {
                "attack_rating": row.home_attack_rating,
                "defense_rating": row.home_defense_rating,
            },
            {
                "attack_rating": row.away_attack_rating,
                "defense_rating": row.away_defense_rating,
            },
        ),
    )


def _matchup_draw_calibration(
    row: Any,
    base: ProbabilityVector,
    elo_gap: float,
    projected_total_goals: float,
    *,
    floor: float,
    close_boost: float,
    low_total_boost: float,
    clear_edge_penalty: float,
    high_total_penalty: float,
    high_attack_penalty: float,
) -> ProbabilityVector:
    home, draw, away = normalize_probabilities(base)
    elo_edge = abs(elo_gap)
    attack_edge = abs(row.home_attack_rating - row.away_attack_rating)
    defense_average = (row.home_defense_rating + row.away_defense_rating) / 2.0
    attack_average = (row.home_attack_rating + row.away_attack_rating) / 2.0
    shot_volume_gap = row.shot_volume_signal * 100.0

    closeness = 1.0 - _clamp(elo_edge / 260.0, 0.0, 1.0)
    no_attack_edge = 1.0 - _clamp(attack_edge / 35.0, 0.0, 1.0)
    defensive_profile = _clamp((defense_average - 55.0) / 25.0, 0.0, 1.0)
    low_total = _clamp((2.70 - projected_total_goals) / 0.80, 0.0, 1.0)
    high_total = _clamp((projected_total_goals - 2.85) / 0.75, 0.0, 1.0)
    clear_edge = max(
        _clamp((elo_edge - 110.0) / 240.0, 0.0, 1.0),
        _clamp((attack_edge - 16.0) / 42.0, 0.0, 1.0),
        _clamp((abs(shot_volume_gap) - 25.0) / 130.0, 0.0, 1.0),
    )
    high_attack_environment = _clamp((attack_average - 62.0) / 28.0, 0.0, 1.0)

    boost = (
        close_boost * closeness * no_attack_edge
        + low_total_boost
        * closeness
        * low_total
        * (0.55 + 0.45 * defensive_profile)
    )
    penalty = (
        clear_edge_penalty * clear_edge
        + high_total_penalty * high_total
        + high_attack_penalty * high_attack_environment * (1.0 - defensive_profile)
    )
    target_draw = _clamp(draw + boost - penalty, floor, 0.40)
    side_total = home + away
    remaining = 1.0 - target_draw
    return remaining * home / side_total, target_draw, remaining * away / side_total


def _v42_probabilities(row: Any) -> ProbabilityVector:
    base, elo_gap, total_goals = _production_side_tilt_base(row)
    return _matchup_draw_calibration(
        row,
        base,
        elo_gap,
        total_goals,
        floor=0.12,
        close_boost=0.050,
        low_total_boost=0.070,
        clear_edge_penalty=0.075,
        high_total_penalty=0.055,
        high_attack_penalty=0.025,
    )


def _v421_probabilities(row: Any) -> ProbabilityVector:
    prediction = calculate_prediction(
        {
            "elo_rating": row.home_elo,
            "attack_rating": row.home_attack_rating,
            "defense_rating": row.home_defense_rating,
            "matches_played": row.home_rating_sample,
        },
        {
            "elo_rating": row.away_elo,
            "attack_rating": row.away_attack_rating,
            "defense_rating": row.away_defense_rating,
            "matches_played": row.away_rating_sample,
        },
        home_shot_volume_rating=row.home_shot_volume_rating,
        away_shot_volume_rating=row.away_shot_volume_rating,
    )
    return (
        prediction["home_win_probability"],
        prediction["draw_probability"],
        prediction["away_win_probability"],
    )


def _accuracy(probabilities: list[ProbabilityVector], outcomes: list[int]) -> float:
    correct = sum(
        max(range(3), key=lambda index: vector[index]) == outcome
        for vector, outcome in zip(probabilities, outcomes)
    )
    return correct / len(outcomes)


def _winner_only_accuracy(
    probabilities: list[ProbabilityVector],
    outcomes: list[int],
) -> float | None:
    decisive = [
        (vector, outcome)
        for vector, outcome in zip(probabilities, outcomes)
        if outcome != 1
    ]
    if not decisive:
        return None
    correct = sum(
        (0 if vector[0] >= vector[2] else 2) == outcome
        for vector, outcome in decisive
    )
    return correct / len(decisive)


def _draw_threshold_metrics(
    probabilities: list[ProbabilityVector],
    outcomes: list[int],
    threshold: float,
) -> dict[str, Any]:
    actual_draws = [index for index, outcome in enumerate(outcomes) if outcome == 1]
    high_draws = [
        index
        for index, vector in enumerate(probabilities)
        if vector[1] >= threshold
    ]
    identified_draws = [
        index
        for index in actual_draws
        if probabilities[index][1] >= threshold
    ]
    true_high_draws = [
        index
        for index in high_draws
        if outcomes[index] == 1
    ]
    return {
        "threshold": threshold,
        "actual_draws": len(actual_draws),
        "high_draw_predictions": len(high_draws),
        "identified_actual_draws": len(identified_draws),
        "true_high_draw_predictions": len(true_high_draws),
        "draw_recall": (
            len(identified_draws) / len(actual_draws) if actual_draws else None
        ),
        "draw_precision": (
            len(true_high_draws) / len(high_draws) if high_draws else None
        ),
    }


def _average_draw_probability_by_outcome(
    probabilities: list[ProbabilityVector],
    outcomes: list[int],
) -> dict[str, Any]:
    actual = [vector[1] for vector, outcome in zip(probabilities, outcomes) if outcome == 1]
    non_draw = [vector[1] for vector, outcome in zip(probabilities, outcomes) if outcome != 1]
    return {
        "actual_draws": sum(actual) / len(actual) if actual else None,
        "non_draws": sum(non_draw) / len(non_draw) if non_draw else None,
        "separation": (
            (sum(actual) / len(actual)) - (sum(non_draw) / len(non_draw))
            if actual and non_draw
            else None
        ),
    }


def _draw_calibration_buckets(
    probabilities: list[ProbabilityVector],
    outcomes: list[int],
) -> list[dict[str, Any]]:
    output = []
    for lower, upper, label in DRAW_BUCKETS:
        indices = [
            index
            for index, vector in enumerate(probabilities)
            if lower <= vector[1] < upper
        ]
        mean_probability = (
            sum(probabilities[index][1] for index in indices) / len(indices)
            if indices
            else None
        )
        observed_rate = (
            sum(1 for index in indices if outcomes[index] == 1) / len(indices)
            if indices
            else None
        )
        output.append(
            {
                "bucket": label,
                "lower": lower,
                "upper": None if label == "30%+" else upper,
                "matches": len(indices),
                "mean_draw_probability": mean_probability,
                "actual_draw_rate": observed_rate,
                "actual_draws": sum(1 for index in indices if outcomes[index] == 1),
            }
        )
    return output


def _draw_probability_distribution(probabilities: list[ProbabilityVector]) -> dict[str, Any]:
    draws = [vector[1] for vector in probabilities]
    mean = sum(draws) / len(draws)
    variance = sum((value - mean) ** 2 for value in draws) / len(draws)
    return {
        "min": min(draws),
        "max": max(draws),
        "mean": mean,
        "standard_deviation": math.sqrt(variance),
        "above_30_percent": sum(value >= HIGH_DRAW_THRESHOLD for value in draws),
    }


def _normalization_summary(probabilities: list[ProbabilityVector]) -> dict[str, Any]:
    deviations = [abs(sum(vector) - 1.0) for vector in probabilities]
    return {
        "all_probability_triples_sum_to_one": all(
            deviation <= 1e-12 for deviation in deviations
        ),
        "max_abs_deviation": max(deviations) if deviations else None,
    }


def _load_team_names(engine: Any) -> dict[str, str]:
    schema = None if engine.dialect.name == "sqlite" else "public"
    inspector = inspect(engine)
    if "teams" not in inspector.get_table_names(schema=schema):
        return {}
    metadata = MetaData()
    teams = Table("teams", metadata, schema=schema, autoload_with=engine)
    if not {"id", "name"}.issubset(teams.c.keys()):
        return {}
    with engine.connect() as connection:
        return {
            str(row["id"]): str(row["name"])
            for row in connection.execute(
                select(teams.c.id, teams.c.name)
            ).mappings()
        }


def _lowering_assessment(
    v41_probabilities: list[ProbabilityVector],
    v42_probabilities: list[ProbabilityVector],
    outcomes: list[int],
) -> dict[str, Any]:
    actual_draw_rate = sum(1 for outcome in outcomes if outcome == 1) / len(outcomes)
    v41_mean = sum(vector[1] for vector in v41_probabilities) / len(v41_probabilities)
    v42_mean = sum(vector[1] for vector in v42_probabilities) / len(v42_probabilities)
    v41_draws = _average_draw_probability_by_outcome(v41_probabilities, outcomes)
    v42_draws = _average_draw_probability_by_outcome(v42_probabilities, outcomes)
    lowered_too_aggressively = (
        v42_mean < actual_draw_rate - 0.03
        and (v42_draws["actual_draws"] or 0.0) <= (v41_draws["actual_draws"] or 0.0)
    )
    if lowered_too_aggressively:
        summary = (
            "Yes. Mean candidate draw probability is materially below the observed "
            "draw rate and actual draws did not receive a higher average draw "
            "probability than under v4.1."
        )
    elif v42_mean < v41_mean and v42_mean < actual_draw_rate:
        summary = (
            "Possibly. Candidate draw probability is below the observed draw "
            "rate; check whether the improved spread offsets this underforecast."
        )
    else:
        summary = (
            "No clear evidence. The candidate lowered some mismatches, but the aggregate "
            "draw level is not materially below the observed draw rate."
        )
    return {
        "lowered_too_aggressively": lowered_too_aggressively,
        "summary": summary,
        "actual_draw_rate": actual_draw_rate,
        "v41_mean_draw_probability": v41_mean,
        "candidate_mean_draw_probability": v42_mean,
        "candidate_minus_v41_mean_draw_probability": v42_mean - v41_mean,
        "v41_actual_draw_average": v41_draws["actual_draws"],
        "candidate_actual_draw_average": v42_draws["actual_draws"],
    }


def _model_report(
    label: str,
    probabilities: list[ProbabilityVector],
    outcomes: list[int],
) -> dict[str, Any]:
    primary = _draw_threshold_metrics(probabilities, outcomes, HIGH_DRAW_THRESHOLD)
    sensitivity = _draw_threshold_metrics(
        probabilities,
        outcomes,
        SENSITIVITY_DRAW_THRESHOLD,
    )
    return {
        "model_version": label,
        "matches": len(outcomes),
        "overall_accuracy": _accuracy(probabilities, outcomes),
        "winner_only_accuracy": _winner_only_accuracy(probabilities, outcomes),
        "draw_recall": primary["draw_recall"],
        "draw_precision": primary["draw_precision"],
        "high_draw_threshold": HIGH_DRAW_THRESHOLD,
        "draw_threshold_counts": primary,
        "draw_threshold_sensitivity_25_percent": sensitivity,
        "average_predicted_draw_probability": (
            _average_draw_probability_by_outcome(probabilities, outcomes)
        ),
        "brier_score": brier_score(probabilities, outcomes),
        "log_loss": log_loss(probabilities, outcomes),
        "draw_probability_distribution": _draw_probability_distribution(probabilities),
        "draw_calibration_buckets": _draw_calibration_buckets(probabilities, outcomes),
    }


def _rounded(value: Any) -> Any:
    if isinstance(value, float):
        return _round(value)
    if isinstance(value, dict):
        return {key: _rounded(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_rounded(item) for item in value]
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if not isinstance(value, (str, int, float, bool)):
        return str(value)
    return value


def build_report() -> dict[str, Any]:
    load_dotenv(ROOT / ".env", override=False)
    load_dotenv(ROOT / "backend" / ".env", override=False)
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        raise RuntimeError("DATABASE_URL is required for completed-match draw backtest")

    engine = create_database_engine(database_url.strip())
    try:
        matches, coverage = load_database_matches(engine)
        team_names = _load_team_names(engine)
    finally:
        engine.dispose()

    rows = build_validation_rows(matches)
    if not rows:
        raise RuntimeError("No completed matches with sufficient prior stats were available")

    outcomes = [row.outcome for row in rows]
    v41 = [_v41_probabilities(row) for row in rows]
    v42 = [_v42_probabilities(row) for row in rows]
    v421 = [_v421_probabilities(row) for row in rows]
    completed_draws = sum(1 for outcome in outcomes if outcome == 1)

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "report_version": "v421_draw_backtest_v1",
        "comparison": "elo-context-v4.1 vs elo-context-v4.2 vs elo-context-v4.2.1",
        "completed_match_source": "database matches joined to team_match_stats",
        "protocol": {
            "matches_loaded": len(matches),
            "eligible_completed_matches": len(rows),
            "actual_draws": completed_draws,
            "actual_draw_rate": completed_draws / len(rows),
            "minimum_prior_matches_per_team": 5,
            "minimum_prior_stat_rows_per_team": 5,
            "same_day_updates": "batched after predictions",
            "v41_definition": (
                "v4 no-rest probabilities plus the promoted shot-volume tilt "
                f"at weight {SHOT_VOLUME_WEIGHT}"
            ),
            "v42_definition": (
                "frozen v4.2 production calculation with 12% draw floor"
            ),
            "v421_definition": (
                f"{MODEL_VERSION} production calculation with 18.5% draw floor "
                "and softened mismatch/high-goals penalties"
            ),
            "high_draw_threshold": HIGH_DRAW_THRESHOLD,
            "draw_recall_definition": (
                "actual draws with predicted draw probability >= high_draw_threshold"
            ),
            "draw_precision_definition": (
                "matches with predicted draw probability >= high_draw_threshold "
                "that actually drew"
            ),
            "source_coverage": coverage,
        },
        "v4_1": _model_report("elo-context-v4.1", v41, outcomes),
        "v4_2": _model_report("elo-context-v4.2", v42, outcomes),
        "v4_2_1": _model_report(MODEL_VERSION, v421, outcomes),
        "delta_v42_minus_v41": {},
        "delta_v421_minus_v42": {},
        "delta_v421_minus_v41": {},
        "lowered_draw_probability_assessment": _lowering_assessment(
            v41,
            v421,
            outcomes,
        ),
        "normalization": {
            "tolerance": "1e-12",
            "v4_1": _normalization_summary(v41),
            "v4_2": _normalization_summary(v42),
            "v4_2_1": _normalization_summary(v421),
        },
        "sample_matches": [
            {
                "match_id": row.match_id,
                "played_on": row.played_on.isoformat(),
                "home_team_id": row.home_team_id,
                "home_team": team_names.get(str(row.home_team_id)),
                "away_team_id": row.away_team_id,
                "away_team": team_names.get(str(row.away_team_id)),
                "outcome": _outcome_name(row.outcome),
                "v41_draw_probability": v41[index][1],
                "v42_draw_probability": v42[index][1],
                "v421_draw_probability": v421[index][1],
                "v421_minus_v42_draw_probability": v421[index][1] - v42[index][1],
                "v421_minus_v41_draw_probability": v421[index][1] - v41[index][1],
            }
            for index, row in sorted(
                enumerate(rows),
                key=lambda item: abs(v421[item[0]][1] - v42[item[0]][1]),
                reverse=True,
            )[:10]
        ],
    }
    for key in (
        "overall_accuracy",
        "winner_only_accuracy",
        "draw_recall",
        "draw_precision",
        "brier_score",
        "log_loss",
    ):
        first = report["v4_1"][key]
        second = report["v4_2"][key]
        report["delta_v42_minus_v41"][key] = (
            second - first if first is not None and second is not None else None
        )
        current = report["v4_2_1"][key]
        report["delta_v421_minus_v42"][key] = (
            current - second if current is not None and second is not None else None
        )
        report["delta_v421_minus_v41"][key] = (
            current - first if current is not None and first is not None else None
        )
    return _rounded(report)


def main() -> int:
    report = build_report()
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
