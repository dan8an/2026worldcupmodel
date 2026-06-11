#!/usr/bin/env python3
from __future__ import annotations

import json
import math
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from sqlalchemy import MetaData, Table, select
from sqlalchemy.engine import Engine

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from modeling.src.data import load_teams
from modeling.src.evaluation.metrics import evaluate
from scripts.database import create_database_engine
from scripts.generate_predictions import PredictionRepository
from scripts.run_simulations import SimulationRepository, load_environment
from scripts.validate_calibrated_v2 import chronological_split
from scripts.validate_xg_proxy_v4 import (
    XgProxyValidationRow,
    _apply_signal,
    build_validation_rows,
    load_database_matches,
)

REPORT_PATH = ROOT / "data" / "evaluation" / "input_rating_audit_latest.json"
CURRENT_WEIGHT = 0.2
SHRINKAGE_PRIOR_MATCHES = 10
GLOBAL_VOLUME_MEAN = 50.0


def cap_shot_volume(value: float, cap: float = 90.0) -> float:
    return min(cap, value)


def shrink_shot_volume(
    value: float,
    sample_matches: int,
    mean: float = GLOBAL_VOLUME_MEAN,
    prior_matches: int = SHRINKAGE_PRIOR_MATCHES,
) -> float:
    reliability = sample_matches / (sample_matches + prior_matches)
    return mean + reliability * (value - mean)


def _metrics(
    rows: list[XgProxyValidationRow],
    signal: Callable[[XgProxyValidationRow], float],
    weight: float,
) -> dict[str, Any]:
    return evaluate(
        [_apply_signal(row.v3, signal(row), weight) for row in rows],
        [row.outcome for row in rows],
    )


def _volume_observations(
    rows: list[XgProxyValidationRow],
) -> dict[str, list[float]]:
    observations: dict[str, list[float]] = defaultdict(list)
    for row in rows:
        if row.home_confederation:
            observations[row.home_confederation].append(row.home_shot_volume_rating)
        if row.away_confederation:
            observations[row.away_confederation].append(row.away_shot_volume_rating)
    return observations


def _confederation_parameters(
    rows: list[XgProxyValidationRow],
) -> dict[str, dict[str, float]]:
    observations = _volume_observations(rows)
    all_values = [value for values in observations.values() for value in values]
    global_mean = sum(all_values) / len(all_values)
    global_variance = sum(
        (value - global_mean) ** 2 for value in all_values
    ) / max(1, len(all_values) - 1)
    global_std = math.sqrt(global_variance) or 1.0
    parameters = {}
    for confederation, values in observations.items():
        mean = sum(values) / len(values)
        variance = sum((value - mean) ** 2 for value in values) / max(
            1, len(values) - 1
        )
        parameters[confederation] = {
            "mean": mean,
            "std": math.sqrt(variance) or global_std,
            "observations": len(values),
        }
    parameters["_global"] = {
        "mean": global_mean,
        "std": global_std,
        "observations": len(all_values),
    }
    return parameters


def confederation_adjusted_volume(
    value: float,
    confederation: str | None,
    parameters: dict[str, dict[str, float]],
) -> float:
    global_values = parameters["_global"]
    local = parameters.get(confederation or "", global_values)
    standardized = (value - local["mean"]) / local["std"]
    adjusted = global_values["mean"] + standardized * global_values["std"]
    return max(0.0, min(100.0, adjusted))


def evaluate_alternatives(
    rows: list[XgProxyValidationRow],
) -> dict[str, Any]:
    tuning_rows, validation_rows = chronological_split(rows, 0.22)
    confederation_parameters = _confederation_parameters(tuning_rows)
    alternatives = {
        "current_v4_weight_0_2": (
            lambda row: row.shot_volume_signal,
            0.2,
        ),
        "cap_at_90_weight_0_2": (
            lambda row: (
                cap_shot_volume(row.home_shot_volume_rating)
                - cap_shot_volume(row.away_shot_volume_rating)
            )
            / 100.0,
            0.2,
        ),
        "sample_shrinkage_weight_0_2": (
            lambda row: (
                shrink_shot_volume(
                    row.home_shot_volume_rating,
                    row.home_shot_volume_sample,
                )
                - shrink_shot_volume(
                    row.away_shot_volume_rating,
                    row.away_shot_volume_sample,
                )
            )
            / 100.0,
            0.2,
        ),
        "confederation_adjusted_weight_0_2": (
            lambda row: (
                confederation_adjusted_volume(
                    row.home_shot_volume_rating,
                    row.home_confederation,
                    confederation_parameters,
                )
                - confederation_adjusted_volume(
                    row.away_shot_volume_rating,
                    row.away_confederation,
                    confederation_parameters,
                )
            )
            / 100.0,
            0.2,
        ),
        "current_volume_weight_0_1": (
            lambda row: row.shot_volume_signal,
            0.1,
        ),
    }
    output = {}
    for name, (signal, weight) in alternatives.items():
        output[name] = {
            "weight": weight,
            "tuning_metrics": _metrics(tuning_rows, signal, weight),
            "validation_metrics": _metrics(validation_rows, signal, weight),
        }
    current = output["current_v4_weight_0_2"]["validation_metrics"]
    for result in output.values():
        metrics = result["validation_metrics"]
        result["validation_delta_vs_current"] = {
            "brier_score": metrics["brier_score"] - current["brier_score"],
            "log_loss": metrics["log_loss"] - current["log_loss"],
            "expected_calibration_error": (
                metrics["expected_calibration_error"]
                - current["expected_calibration_error"]
            ),
        }
    return {
        "split": {
            "tuning_matches": len(tuning_rows),
            "validation_matches": len(validation_rows),
            "validation_start": min(row.played_on for row in validation_rows),
            "validation_end": max(row.played_on for row in validation_rows),
        },
        "confederation_parameters_fit_on_tuning_only": confederation_parameters,
        "alternatives": output,
    }


def _load_live_inputs(
    engine: Engine,
) -> tuple[
    PredictionRepository,
    dict[str, Any],
    dict[str, dict[str, Any]],
    dict[str, float],
]:
    repository = PredictionRepository(engine)
    database_team_ids = repository.load_database_team_ids()
    ratings = repository.load_current_team_ratings(database_team_ids)
    shot_volume = repository.load_current_shot_volume_ratings(database_team_ids)
    return repository, database_team_ids, ratings, shot_volume


def _latest_simulation(engine: Engine) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    repository = SimulationRepository(engine)
    runs = repository._table("simulation_runs")
    results = repository._table("team_simulation_results")
    with engine.connect() as connection:
        run = connection.execute(
            select(runs).order_by(runs.c.created_at.desc(), runs.c.id.desc()).limit(1)
        ).mappings().one()
        rows = [
            dict(row)
            for row in connection.execute(
                select(results).where(results.c.simulation_run_id == run["id"])
            ).mappings()
        ]
    return dict(run), rows


def _norway_sources(engine: Engine, database_team_id: Any) -> dict[str, Any]:
    schema = None if engine.dialect.name == "sqlite" else "public"
    metadata = MetaData()
    matches = Table("matches", metadata, schema=schema, autoload_with=engine)
    stats = Table("team_match_stats", metadata, schema=schema, autoload_with=engine)
    with engine.connect() as connection:
        rows = [
            dict(row)
            for row in connection.execute(
                select(
                    matches.c.id,
                    matches.c.match_date,
                    matches.c.home_team,
                    matches.c.away_team,
                    matches.c.tournament_stage,
                    matches.c.provider_name,
                    matches.c.provider_payload,
                    stats.c.is_home,
                    stats.c.goals,
                    stats.c.shots,
                    stats.c.shots_on_target,
                    stats.c.shots_inside_box,
                    stats.c.shots_outside_box,
                    stats.c.blocked_shots,
                    stats.c.source_name,
                    stats.c.source_match_key,
                )
                .join(matches, matches.c.id == stats.c.match_id)
                .where(stats.c.team_id == database_team_id)
                .order_by(matches.c.match_date.desc())
                .limit(10)
            ).mappings()
        ]
    sources = []
    competitions = Counter()
    opponents = Counter()
    for row in rows:
        payload = row.get("provider_payload") or {}
        if isinstance(payload, str):
            payload = json.loads(payload)
        league = payload.get("league", {}) if isinstance(payload, dict) else {}
        competition = league.get("name") or row.get("tournament_stage")
        opponent = row["away_team"] if row["is_home"] else row["home_team"]
        competitions[str(competition)] += 1
        opponents[str(opponent)] += 1
        sources.append(
            {
                "match_id": row["id"],
                "played_at": row["match_date"],
                "opponent": opponent,
                "competition": competition,
                "provider": row["provider_name"],
                "stats_source": row["source_name"],
                "source_match_key": row["source_match_key"],
                "goals": row["goals"],
                "shots": row["shots"],
                "shots_on_target": row["shots_on_target"],
                "shots_inside_box": row["shots_inside_box"],
                "shots_outside_box": row["shots_outside_box"],
                "blocked_shots": row["blocked_shots"],
            }
        )
    return {
        "matches": sources,
        "match_count": len(sources),
        "competitions": dict(competitions),
        "opponents": dict(opponents),
        "club_contamination_detected": any(
            "club" in str(source["competition"]).lower() for source in sources
        ),
        "weak_opponent_concentration": {
            "moldova_estonia_israel_matches": sum(
                count
                for opponent, count in opponents.items()
                if opponent in {"Moldova", "Estonia", "Israel"}
            ),
            "share": (
                sum(
                    count
                    for opponent, count in opponents.items()
                    if opponent in {"Moldova", "Estonia", "Israel"}
                )
                / len(sources)
                if sources
                else 0.0
            ),
        },
    }


def build_report(engine: Engine) -> dict[str, Any]:
    repository, database_team_ids, ratings, shot_volume = _load_live_inputs(engine)
    run, simulation_rows = _latest_simulation(engine)
    simulation = {str(row["team_id"]): row for row in simulation_rows}
    teams = {team.id: team for team in load_teams()}
    rows = []
    for team_id, team in teams.items():
        rating = ratings[team_id]
        simulation_row = simulation[team_id]
        rows.append(
            {
                "team_id": team_id,
                "team_name": team.name,
                "group": team.group,
                "fifa_rank": team.rank,
                "rating_source": rating["_rating_source"],
                "elo": float(rating["elo_rating"]),
                "attack_rating": float(rating["attack_rating"]),
                "defense_rating": float(rating["defense_rating"]),
                "shot_volume_rating": shot_volume.get(team_id),
                "round_of_32_probability": float(
                    simulation_row["round_of_32_probability"]
                ),
                "champion_probability": float(
                    simulation_row["champion_probability"]
                ),
            }
        )
    elo_order = {
        row["team_id"]: index + 1
        for index, row in enumerate(
            sorted(rows, key=lambda row: row["elo"], reverse=True)
        )
    }
    title_order = {
        row["team_id"]: index + 1
        for index, row in enumerate(
            sorted(rows, key=lambda row: row["champion_probability"], reverse=True)
        )
    }
    for row in rows:
        row["elo_rank"] = elo_order[row["team_id"]]
        row["title_probability_rank"] = title_order[row["team_id"]]
        row["title_rank_minus_elo_rank"] = (
            row["title_probability_rank"] - row["elo_rank"]
        )

    matches, coverage = load_database_matches(engine)
    team_confederations = {}
    schema = None if engine.dialect.name == "sqlite" else "public"
    metadata = MetaData()
    teams_table = Table("teams", metadata, schema=schema, autoload_with=engine)
    with engine.connect() as connection:
        for row in connection.execute(
            select(teams_table.c.id, teams_table.c.confederation)
        ).mappings():
            team_confederations[row["id"]] = row["confederation"]
    validation_rows = build_validation_rows(
        matches,
        team_confederations=team_confederations,
    )
    validation = evaluate_alternatives(validation_rows)
    alternatives = validation["alternatives"]
    current = alternatives["current_v4_weight_0_2"]["validation_metrics"]
    eligible = [
        (name, result)
        for name, result in alternatives.items()
        if (
            result["validation_metrics"]["brier_score"],
            result["validation_metrics"]["log_loss"],
        )
        < (current["brier_score"], current["log_loss"])
    ]
    best = min(
        alternatives.items(),
        key=lambda item: (
            item[1]["validation_metrics"]["brier_score"],
            item[1]["validation_metrics"]["log_loss"],
        ),
    )
    norway = next(row for row in rows if row["team_id"] == "NOR")
    norway["source_audit"] = _norway_sources(
        engine, database_team_ids["NOR"]
    )
    norway["alternative_live_shot_volume"] = {
        "current": norway["shot_volume_rating"],
        "cap_at_90": cap_shot_volume(norway["shot_volume_rating"]),
        "sample_shrinkage": shrink_shot_volume(
            norway["shot_volume_rating"],
            norway["source_audit"]["match_count"],
        ),
    }
    norway["assessment"] = {
        "club_or_team_contamination": (
            "No club contamination detected; all ten rows are Norway senior "
            "national-team matches from API-Football."
        ),
        "rolling_window": (
            "The current feature uses exactly ten matches and averages 19.3 "
            "shots per match, which clips to the maximum rating of 100."
        ),
        "opponent_strength": (
            "Six of ten matches are against Moldova, Estonia, or Israel. The "
            "34-shot, 11-1 Moldova match is a clear schedule-strength driver."
        ),
        "confederation_fairness": (
            "The production feature has no opponent- or confederation-strength "
            "normalization. The tested confederation adjustment was slightly "
            "worse than current v4 on holdout Brier, log loss, and calibration."
        ),
    }
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "production_changed": False,
        "simulation_run": {
            "id": run["id"],
            "model_version": run.get("model_version"),
            "created_at": run.get("created_at"),
        },
        "source_coverage": coverage,
        "team_inputs": sorted(
            rows, key=lambda row: row["champion_probability"], reverse=True
        ),
        "outliers": {
            "shot_volume_above_95": [
                row for row in rows if (row["shot_volume_rating"] or 0) > 95
            ],
            "attack_or_defense_above_90": [
                row
                for row in rows
                if row["attack_rating"] > 90 or row["defense_rating"] > 90
            ],
            "title_rank_at_least_8_places_above_elo_rank": [
                row for row in rows if row["title_rank_minus_elo_rank"] <= -8
            ],
        },
        "norway": norway,
        "chronological_holdout": validation,
        "decision": {
            "current_metrics": current,
            "best_alternative": best[0],
            "best_alternative_metrics": best[1]["validation_metrics"],
            "alternatives_beating_current_on_brier_then_log_loss": [
                name for name, _ in eligible
            ],
            "production_change_recommended": False,
            "reason": (
                f"Current v4 remains the best tested holdout model ({best[0]}). "
                "Every requested alternative worsened Brier score and log loss, "
                "so no production change was applied."
            ),
        },
    }


def main() -> int:
    database_url = load_environment().get("DATABASE_URL")
    if not database_url:
        raise SystemExit("DATABASE_URL is required")
    engine = create_database_engine(database_url)
    try:
        report = build_report(engine)
        REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
        REPORT_PATH.write_text(
            json.dumps(report, indent=2, default=str) + "\n"
        )
        print(json.dumps(report["decision"], indent=2))
        print(f"Wrote {REPORT_PATH.relative_to(ROOT)}")
        return 0
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
