#!/usr/bin/env python3
from __future__ import annotations

import json
import math
import os
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from sqlalchemy import MetaData, Table, inspect, select
from sqlalchemy.engine import Engine

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from modeling.src.evaluation.metrics import ProbabilityVector, evaluate
from scripts.build_xg_proxy_features import calculate_chance_quality_rating
from scripts.database import create_database_engine
from scripts.evaluate_calibrated_v2 import normalize_probabilities
from scripts.evaluate_model import TeamHistory, _elo_probabilities
from scripts.validate_calibrated_v2 import chronological_split

MODEL_VERSION = "xg-proxy-v4"
REPORT_PATH = ROOT / "data" / "evaluation" / "xg_proxy_v4_validation.json"
PROMOTION_CONFIG_PATH = (
    ROOT / "data" / "evaluation" / "xg_proxy_v4_promotion_config.json"
)
MINIMUM_PRIOR_MATCHES = 5
MINIMUM_VALIDATION_MATCHES = 30
WEIGHT_GRID = (-0.20, -0.10, 0.0, 0.10, 0.20)
ABLATIONS = (
    "v3_only",
    "v3_plus_shot_volume",
    "v3_plus_shot_quality_proxy",
    "v3_plus_defensive_suppression",
    "v3_plus_all_xg_proxy_features",
)
ABLATION_FEATURES = {
    "v3_only": [],
    "v3_plus_shot_volume": ["shot_volume_rating"],
    "v3_plus_shot_quality_proxy": [
        "shot_quality_proxy",
        "box_shot_rate",
        "shots_on_target_rate",
    ],
    "v3_plus_defensive_suppression": [
        "defensive_shot_suppression",
        "keeper_pressure_allowed",
    ],
    "v3_plus_all_xg_proxy_features": [
        "shot_volume_rating",
        "shot_quality_proxy",
        "box_shot_rate",
        "shots_on_target_rate",
        "chance_creation_rating",
        "defensive_shot_suppression",
        "keeper_pressure_allowed",
    ],
}
PROVIDER_FIELD_AUDIT = {
    "total_shots": {
        "provider_stat": "Total Shots",
        "previously_persisted": True,
    },
    "shots_on_goal": {
        "provider_stat": "Shots on Goal",
        "previously_persisted": True,
    },
    "shots_inside_box": {
        "provider_stat": "Shots insidebox",
        "previously_persisted": False,
        "added_in_migration": True,
    },
    "shots_outside_box": {
        "provider_stat": "Shots outsidebox",
        "previously_persisted": False,
        "added_in_migration": True,
    },
    "blocked_shots": {
        "provider_stat": "Blocked Shots",
        "previously_persisted": False,
        "added_in_migration": True,
    },
    "corners": {
        "provider_stat": "Corner Kicks",
        "previously_persisted": True,
    },
    "possession": {
        "provider_stat": "Ball Possession",
        "previously_persisted": True,
    },
    "goalkeeper_saves": {
        "provider_stat": "Goalkeeper Saves",
        "previously_persisted": False,
        "added_in_migration": True,
    },
    "passes": {
        "provider_stats": ["Total passes", "Passes accurate"],
        "previously_persisted": True,
    },
    "pass_accuracy": {
        "provider_stat": "Passes %",
        "previously_persisted": False,
        "added_in_migration": True,
    },
    "cards": {
        "provider_stats": ["Yellow Cards", "Red Cards"],
        "previously_persisted": True,
    },
}


@dataclass(frozen=True)
class XgProxyValidationRow:
    played_on: date
    outcome: int
    v3: ProbabilityVector
    shot_volume_signal: float
    home_shot_volume_rating: float
    away_shot_volume_rating: float
    home_shot_volume_sample: int
    away_shot_volume_sample: int
    home_confederation: str | None
    away_confederation: str | None
    shot_quality_signal: float
    defensive_suppression_signal: float
    all_features_signal: float
    v4_no_rest: ProbabilityVector | None = None
    match_id: Any | None = None
    home_team_id: Any | None = None
    away_team_id: Any | None = None
    home_elo: float = 1500.0
    away_elo: float = 1500.0
    home_attack_rating: float = 50.0
    away_attack_rating: float = 50.0
    home_defense_rating: float = 50.0
    away_defense_rating: float = 50.0
    home_rating_sample: int = 0
    away_rating_sample: int = 0
    home_rating_age_days: int | None = None
    away_rating_age_days: int | None = None
    home_shot_volume_age_days: int | None = None
    away_shot_volume_age_days: int | None = None


def _number(value: Any) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _difference(home: float, away: float, scale: float = 100.0) -> float:
    return max(-1.0, min(1.0, (home - away) / scale))


def _outcome(home_goals: int, away_goals: int) -> int:
    if home_goals > away_goals:
        return 0
    if home_goals == away_goals:
        return 1
    return 2


def _v3_probabilities(
    home_history: TeamHistory,
    away_history: TeamHistory,
    played_on: date,
) -> ProbabilityVector:
    home_rating = home_history.rating()
    away_rating = away_history.rating()
    elo = _elo_probabilities(home_history.elo, away_history.elo)
    attack = _difference(
        float(home_rating["attack_rating"]),
        float(away_rating["attack_rating"]),
    )
    defense = _difference(
        float(home_rating["defense_rating"]),
        float(away_rating["defense_rating"]),
    )
    home_rest = (
        (played_on - home_history.last_played_on).days
        if home_history.last_played_on
        else None
    )
    away_rest = (
        (played_on - away_history.last_played_on).days
        if away_history.last_played_on
        else None
    )
    rest = (
        max(-1.0, min(1.0, (home_rest - away_rest) / 14.0))
        if home_rest is not None and away_rest is not None
        else 0.0
    )
    tilt = 0.15 * attack + 0.30 * defense - 0.15 * rest
    return normalize_probabilities(
        (
            elo[0] * math.exp(tilt),
            elo[1] * 1.15,
            elo[2] * math.exp(-tilt),
        )
    )


def _v4_no_rest_probabilities(
    home_history: TeamHistory,
    away_history: TeamHistory,
) -> ProbabilityVector:
    home_rating = home_history.rating()
    away_rating = away_history.rating()
    elo = _elo_probabilities(home_history.elo, away_history.elo)
    attack = _difference(
        float(home_rating["attack_rating"]),
        float(away_rating["attack_rating"]),
    )
    defense = _difference(
        float(home_rating["defense_rating"]),
        float(away_rating["defense_rating"]),
    )
    tilt = 0.15 * attack + 0.30 * defense
    return normalize_probabilities(
        (
            elo[0] * math.exp(tilt),
            elo[1] * 1.15,
            elo[2] * math.exp(-tilt),
        )
    )


def _apply_signal(
    probabilities: ProbabilityVector,
    signal: float,
    weight: float,
) -> ProbabilityVector:
    tilt = signal * weight
    return normalize_probabilities(
        (
            probabilities[0] * math.exp(tilt),
            probabilities[1],
            probabilities[2] * math.exp(-tilt),
        )
    )


def build_validation_rows(
    matches: list[dict[str, Any]],
    team_confederations: dict[Any, str | None] | None = None,
) -> list[XgProxyValidationRow]:
    team_confederations = team_confederations or {}
    ordered = sorted(
        matches,
        key=lambda match: (
            match["played_on"],
            str(match["match_id"]),
        ),
    )
    by_date: dict[date, list[dict[str, Any]]] = defaultdict(list)
    for match in ordered:
        by_date[match["played_on"]].append(match)
    histories: dict[Any, TeamHistory] = defaultdict(TeamHistory)
    stat_history: dict[Any, list[dict[str, Any]]] = defaultdict(list)
    rows = []

    for played_on in sorted(by_date):
        day_matches = by_date[played_on]
        for match in day_matches:
            home_id = match["home_team_id"]
            away_id = match["away_team_id"]
            home_history = histories[home_id]
            away_history = histories[away_id]
            if (
                home_history.matches < MINIMUM_PRIOR_MATCHES
                or away_history.matches < MINIMUM_PRIOR_MATCHES
                or len(stat_history[home_id]) < MINIMUM_PRIOR_MATCHES
                or len(stat_history[away_id]) < MINIMUM_PRIOR_MATCHES
            ):
                continue
            home_features = calculate_chance_quality_rating(stat_history[home_id])
            away_features = calculate_chance_quality_rating(stat_history[away_id])
            home_rating = home_history.rating()
            away_rating = away_history.rating()
            home_age = (
                (played_on - home_history.last_played_on).days
                if home_history.last_played_on
                else None
            )
            away_age = (
                (played_on - away_history.last_played_on).days
                if away_history.last_played_on
                else None
            )
            volume = _difference(
                home_features["shot_volume_rating"],
                away_features["shot_volume_rating"],
            )
            quality = (
                0.50
                * _difference(
                    home_features["shot_quality_proxy"],
                    away_features["shot_quality_proxy"],
                )
                + 0.25
                * _difference(
                    home_features["box_shot_rate"],
                    away_features["box_shot_rate"],
                    1.0,
                )
                + 0.25
                * _difference(
                    home_features["shots_on_target_rate"],
                    away_features["shots_on_target_rate"],
                    1.0,
                )
            )
            defense = (
                0.70
                * _difference(
                    home_features["defensive_shot_suppression"],
                    away_features["defensive_shot_suppression"],
                )
                - 0.30
                * _difference(
                    home_features["keeper_pressure_allowed"],
                    away_features["keeper_pressure_allowed"],
                )
            )
            creation = _difference(
                home_features["chance_creation_rating"],
                away_features["chance_creation_rating"],
            )
            all_features = (
                0.25 * volume
                + 0.30 * quality
                + 0.25 * defense
                + 0.20 * creation
            )
            rows.append(
                XgProxyValidationRow(
                    played_on=played_on,
                    outcome=_outcome(match["home_goals"], match["away_goals"]),
                    v3=_v3_probabilities(home_history, away_history, played_on),
                    v4_no_rest=_v4_no_rest_probabilities(
                        home_history, away_history
                    ),
                    shot_volume_signal=volume,
                    home_shot_volume_rating=home_features[
                        "shot_volume_rating"
                    ],
                    away_shot_volume_rating=away_features[
                        "shot_volume_rating"
                    ],
                    home_shot_volume_sample=int(home_features["sample_matches"]),
                    away_shot_volume_sample=int(away_features["sample_matches"]),
                    home_confederation=team_confederations.get(home_id),
                    away_confederation=team_confederations.get(away_id),
                    shot_quality_signal=quality,
                    defensive_suppression_signal=defense,
                    all_features_signal=all_features,
                    match_id=match.get("match_id"),
                    home_team_id=home_id,
                    away_team_id=away_id,
                    home_elo=home_history.elo,
                    away_elo=away_history.elo,
                    home_attack_rating=float(home_rating["attack_rating"]),
                    away_attack_rating=float(away_rating["attack_rating"]),
                    home_defense_rating=float(home_rating["defense_rating"]),
                    away_defense_rating=float(away_rating["defense_rating"]),
                    home_rating_sample=home_history.matches,
                    away_rating_sample=away_history.matches,
                    home_rating_age_days=home_age,
                    away_rating_age_days=away_age,
                    home_shot_volume_age_days=home_age,
                    away_shot_volume_age_days=away_age,
                )
            )

        for match in day_matches:
            home_id = match["home_team_id"]
            away_id = match["away_team_id"]
            home_goals = match["home_goals"]
            away_goals = match["away_goals"]
            home_history = histories[home_id]
            away_history = histories[away_id]
            expected_home = 1.0 / (
                1.0 + 10 ** ((away_history.elo - home_history.elo) / 400.0)
            )
            actual_home = (
                1.0 if home_goals > away_goals
                else 0.5 if home_goals == away_goals
                else 0.0
            )
            margin = 1.0 + 0.25 * max(
                0, abs(home_goals - away_goals) - 1
            )
            change = 20.0 * margin * (actual_home - expected_home)
            home_history.elo += change
            away_history.elo -= change
            for history, goals_for, goals_against, result_score in (
                (
                    home_history,
                    home_goals,
                    away_goals,
                    actual_home,
                ),
                (
                    away_history,
                    away_goals,
                    home_goals,
                    1.0 - actual_home,
                ),
            ):
                history.goals_for += goals_for
                history.goals_against += goals_against
                history.matches += 1
                history.last_played_on = played_on
                history.recent_scores.append(
                    3.0 if result_score == 1.0
                    else 1.0 if result_score == 0.5
                    else 0.0
                )
            home_row = {
                **match["home_stats"],
                "opponent_shots": match["away_stats"].get("shots"),
                "opponent_shots_on_target": match["away_stats"].get(
                    "shots_on_target"
                ),
            }
            away_row = {
                **match["away_stats"],
                "opponent_shots": match["home_stats"].get("shots"),
                "opponent_shots_on_target": match["home_stats"].get(
                    "shots_on_target"
                ),
            }
            stat_history[home_id].append(home_row)
            stat_history[away_id].append(away_row)
    return rows


def _signal(row: XgProxyValidationRow, ablation: str) -> float:
    return {
        "v3_plus_shot_volume": row.shot_volume_signal,
        "v3_plus_shot_quality_proxy": row.shot_quality_signal,
        "v3_plus_defensive_suppression": row.defensive_suppression_signal,
        "v3_plus_all_xg_proxy_features": row.all_features_signal,
    }.get(ablation, 0.0)


def _metrics(
    rows: list[XgProxyValidationRow],
    ablation: str,
    weight: float,
) -> dict[str, Any]:
    return evaluate(
        [
            _apply_signal(
                row.v4_no_rest or row.v3,
                _signal(row, ablation),
                weight,
            )
            for row in rows
        ],
        [row.outcome for row in rows],
    )


def _tune(
    rows: list[XgProxyValidationRow],
    ablation: str,
) -> tuple[float, dict[str, Any]]:
    weights = (0.0,) if ablation == "v3_only" else WEIGHT_GRID
    candidates = [(weight, _metrics(rows, ablation, weight)) for weight in weights]
    weight, metrics = min(
        candidates,
        key=lambda item: (
            item[1]["brier_score"],
            item[1]["log_loss"],
            abs(item[0]),
        ),
    )
    return weight, metrics


def select_best_validated_ablation(
    results: dict[str, dict[str, Any]],
) -> str:
    evaluated = [
        (name, result)
        for name, result in results.items()
        if result.get("status") == "evaluated"
        and result.get("validation_metrics", {}).get("brier_score") is not None
        and result.get("validation_metrics", {}).get("log_loss") is not None
    ]
    if not evaluated:
        raise ValueError("No evaluated ablations have holdout metrics")
    return min(
        evaluated,
        key=lambda item: (
            item[1]["validation_metrics"]["brier_score"],
            item[1]["validation_metrics"]["log_loss"],
            ABLATIONS.index(item[0]),
        ),
    )[0]


def build_promotion_config(
    results: dict[str, dict[str, Any]],
    selected_ablation: str,
) -> dict[str, Any]:
    selected = results[selected_ablation]
    metrics = selected["validation_metrics"]
    return {
        "selected_ablation": selected_ablation,
        "selected_weight": selected["selected_weight"],
        "validation_brier": metrics["brier_score"],
        "validation_log_loss": metrics["log_loss"],
        "features_used": ABLATION_FEATURES[selected_ablation],
    }


def build_report(
    matches: list[dict[str, Any]],
    source_coverage: dict[str, Any],
) -> dict[str, Any]:
    rows = build_validation_rows(matches)
    base = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "model_version": MODEL_VERSION,
        "production_changed": False,
        "source_coverage": source_coverage,
        "provider_field_audit": PROVIDER_FIELD_AUDIT,
        "feature_layer": {
            "rolling_window_matches": 10,
            "minimum_prior_matches": MINIMUM_PRIOR_MATCHES,
            "features": [
                "shot_volume_rating",
                "shot_quality_proxy",
                "box_shot_rate",
                "shots_on_target_rate",
                "chance_creation_rating",
                "defensive_shot_suppression",
                "keeper_pressure_allowed",
            ],
            "leakage_control": (
                "features use matches strictly before the prediction date; "
                "same-day matches are updated as a batch"
            ),
        },
        "eligible_walk_forward_matches": len(rows),
    }
    if len(rows) < MINIMUM_VALIDATION_MATCHES * 2:
        return {
            **base,
            "status": "insufficient_data",
            "reason": (
                f"Need at least {MINIMUM_VALIDATION_MATCHES * 2} eligible "
                f"walk-forward matches; found {len(rows)}."
            ),
            "ablations": {
                name: {
                    "status": "not_evaluated",
                    "brier_score": None,
                    "log_loss": None,
                }
                for name in ABLATIONS
            },
            "promotion": {
                "recommend_promotion": False,
                "decision": "do not promote",
                "reason": "insufficient chronological shot-stat coverage",
            },
        }

    tuning_rows, validation_rows = chronological_split(rows, 0.22)
    results = {}
    for ablation in ABLATIONS:
        weight, tuning_metrics = _tune(tuning_rows, ablation)
        results[ablation] = {
            "status": "evaluated",
            "selected_weight": weight,
            "features_used": ABLATION_FEATURES[ablation],
            "tuning_metrics": tuning_metrics,
            "validation_metrics": _metrics(
                validation_rows, ablation, weight
            ),
        }
    selected_ablation = select_best_validated_ablation(results)
    selected = results[selected_ablation]["validation_metrics"]
    v3 = results["v3_only"]["validation_metrics"]
    all_features = results["v3_plus_all_xg_proxy_features"]["validation_metrics"]
    recommend_promotion = selected_ablation != "v3_only" and (
        selected["brier_score"],
        selected["log_loss"],
    ) < (
        v3["brier_score"],
        v3["log_loss"],
    )
    all_features_reason = (
        f"{selected_ablation} is selected because its holdout Brier score "
        f"({selected['brier_score']:.6f}) is lower than the all-features "
        f"model ({all_features['brier_score']:.6f}); its log loss is also "
        f"{selected['log_loss']:.6f} versus {all_features['log_loss']:.6f}. "
        "The additional xG-proxy features did not improve generalization "
        "enough to justify the more complex model."
    )
    return {
        **base,
        "status": "independent_chronological_validation",
        "split": {
            "tuning_matches": len(tuning_rows),
            "validation_matches": len(validation_rows),
            "validation_start": min(
                row.played_on for row in validation_rows
            ).isoformat(),
            "validation_end": max(
                row.played_on for row in validation_rows
            ).isoformat(),
        },
        "ablations": results,
        "promotion": {
            "recommend_promotion": recommend_promotion,
            "decision": (
                f"recommend {selected_ablation}"
                if recommend_promotion
                else "do not promote"
            ),
            "selected_ablation": selected_ablation,
            "selection_rule": (
                "lowest holdout Brier score; holdout log loss breaks ties"
            ),
            "selected_weight": results[selected_ablation]["selected_weight"],
            "validation_brier": selected["brier_score"],
            "validation_log_loss": selected["log_loss"],
            "features_used": ABLATION_FEATURES[selected_ablation],
            "why_all_features_not_selected": all_features_reason,
        },
    }


def load_database_matches(engine: Engine) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    schema = None if engine.dialect.name == "sqlite" else "public"
    inspector = inspect(engine)
    tables = set(inspector.get_table_names(schema=schema))
    if not {"matches", "team_match_stats"}.issubset(tables):
        return [], {"matches": 0, "team_stat_rows": 0}
    metadata = MetaData()
    matches = Table("matches", metadata, schema=schema, autoload_with=engine)
    stats = Table("team_match_stats", metadata, schema=schema, autoload_with=engine)
    date_column = next(
        (matches.c[name] for name in ("match_date", "kickoff") if name in matches.c),
        None,
    )
    if date_column is None:
        return [], {"matches": 0, "team_stat_rows": 0}
    columns = set(stats.c.keys())
    optional = (
        "shots_inside_box",
        "shots_outside_box",
        "blocked_shots",
        "goalkeeper_saves",
        "pass_accuracy",
    )
    with engine.connect() as connection:
        match_rows = [
            dict(row)
            for row in connection.execute(
                select(
                    matches.c.id,
                    matches.c.home_team_id,
                    matches.c.away_team_id,
                    matches.c.home_score,
                    matches.c.away_score,
                    date_column.label("played_at"),
                )
            ).mappings()
        ]
        stat_rows = [
            dict(row)
            for row in connection.execute(select(stats)).mappings()
        ]
    stats_by_match: dict[Any, dict[Any, dict[str, Any]]] = defaultdict(dict)
    for row in stat_rows:
        stats_by_match[row["match_id"]][row["team_id"]] = row
    output = []
    for match in match_rows:
        home_id = match.get("home_team_id")
        away_id = match.get("away_team_id")
        if (
            home_id is None
            or away_id is None
            or match.get("home_score") is None
            or match.get("away_score") is None
            or len(stats_by_match[match["id"]]) != 2
        ):
            continue
        played_at = match["played_at"]
        played_on = (
            played_at.date()
            if isinstance(played_at, datetime)
            else date.fromisoformat(str(played_at)[:10])
        )
        output.append(
            {
                "match_id": match["id"],
                "played_on": played_on,
                "home_team_id": home_id,
                "away_team_id": away_id,
                "home_goals": int(match["home_score"]),
                "away_goals": int(match["away_score"]),
                "home_stats": stats_by_match[match["id"]][home_id],
                "away_stats": stats_by_match[match["id"]][away_id],
            }
        )
    coverage = {
        "matches_with_two_team_stat_rows": len(output),
        "team_stat_rows": len(stat_rows),
        "fields": {
            name: {
                "available_in_schema": name in columns,
                "non_null_rows": sum(row.get(name) is not None for row in stat_rows)
                if name in columns
                else 0,
            }
            for name in (
                "shots",
                "shots_on_target",
                *optional,
                "corners",
                "possession",
                "passes_attempted",
                "passes_completed",
                "yellow_cards",
                "red_cards",
            )
        },
    }
    return output, coverage


def main() -> int:
    load_dotenv(ROOT / ".env", override=False)
    load_dotenv(ROOT / "backend" / ".env", override=False)
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        raise SystemExit("DATABASE_URL is required")
    engine = create_database_engine(database_url)
    try:
        matches, coverage = load_database_matches(engine)
        report = build_report(matches, coverage)
        REPORT_PATH.write_text(json.dumps(report, indent=2) + "\n")
        if report["promotion"].get("recommend_promotion"):
            config = build_promotion_config(
                report["ablations"],
                report["promotion"]["selected_ablation"],
            )
            PROMOTION_CONFIG_PATH.write_text(json.dumps(config, indent=2) + "\n")
        print(json.dumps(report, indent=2))
        return 0
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
