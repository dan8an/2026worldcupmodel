#!/usr/bin/env python3
from __future__ import annotations

import json
import math
import os
import sys
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from itertools import product
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from sqlalchemy import MetaData, Table, inspect, select
from sqlalchemy.engine import Engine

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from modeling.src.evaluation.metrics import ProbabilityVector, evaluate
from scripts.database import create_database_engine
from scripts.evaluate_calibrated_v2 import normalize_probabilities
from scripts.validate_calibrated_v2 import chronological_split
from scripts.validate_xg_proxy_v4 import (
    _apply_signal,
    build_validation_rows,
    load_database_matches,
)

MODEL_VERSION = "squad-v4.1-research"
REPORT_PATH = ROOT / "data" / "evaluation" / "squad_v41_validation.json"
MINIMUM_TUNING_MATCHES = 20
MINIMUM_VALIDATION_MATCHES = 10
WEIGHTS = (-0.20, -0.10, 0.0, 0.10, 0.20)
ABLATIONS = (
    "v4_only",
    "v4_plus_squad_strength",
    "v4_plus_unavailable_player_penalty",
    "v4_plus_projected_lineup_strength",
    "v4_plus_all_squad_features",
)
PROVIDER_CAPABILITY_AUDIT = {
    "injuries": {
        "api_football": "supported via /injuries",
        "code": "get_injuries",
        "expected_return": "not supplied by the endpoint; stored as null",
    },
    "suspensions": {
        "api_football": (
            "no separate endpoint; suspension reasons/types can appear in /injuries"
        ),
        "code": "normalized by get_injuries when the payload identifies suspension",
    },
    "lineups": {
        "api_football": "supported via /fixtures/lineups",
        "code": "get_lineups",
    },
    "squads": {
        "api_football": "supported via /players/squads",
        "code": "get_squad",
    },
    "player_statistics": {
        "api_football": "supported via paginated /players",
        "code": "get_player_statistics",
    },
    "player_ratings_and_minutes": {
        "api_football": (
            "fixture ratings/minutes are available in /fixtures/players; "
            "season totals and average rating are available in /players"
        ),
        "code": "preserved by get_fixture_players and get_player_statistics",
    },
}


@dataclass(frozen=True)
class SquadValidationRow:
    played_on: date
    outcome: int
    v4: ProbabilityVector
    squad_strength_signal: float | None = None
    unavailable_penalty_signal: float | None = None
    projected_lineup_signal: float | None = None
    match_id: Any | None = None
    injury_data: bool = False
    squad_data: bool = False
    lineup_data: bool = False


def _apply_features(
    row: SquadValidationRow,
    weights: tuple[float, float, float],
) -> ProbabilityVector:
    values = (
        row.squad_strength_signal or 0.0,
        row.unavailable_penalty_signal or 0.0,
        row.projected_lineup_signal or 0.0,
    )
    tilt = sum(weight * value for weight, value in zip(weights, values))
    return normalize_probabilities(
        (
            row.v4[0] * math.exp(tilt),
            row.v4[1],
            row.v4[2] * math.exp(-tilt),
        )
    )


def _required_features(ablation: str) -> tuple[str, ...]:
    return {
        "v4_only": (),
        "v4_plus_squad_strength": ("squad_strength_signal",),
        "v4_plus_unavailable_player_penalty": ("unavailable_penalty_signal",),
        "v4_plus_projected_lineup_strength": ("projected_lineup_signal",),
        "v4_plus_all_squad_features": (
            "squad_strength_signal",
            "unavailable_penalty_signal",
            "projected_lineup_signal",
        ),
    }[ablation]


def _covered(rows: list[SquadValidationRow], ablation: str) -> list[SquadValidationRow]:
    required = _required_features(ablation)
    return [
        row
        for row in rows
        if all(getattr(row, feature) is not None for feature in required)
    ]


def _weight_grid(ablation: str) -> list[tuple[float, float, float]]:
    active = _required_features(ablation)
    choices = [
        WEIGHTS if name in active else (0.0,)
        for name in (
            "squad_strength_signal",
            "unavailable_penalty_signal",
            "projected_lineup_signal",
        )
    ]
    return list(product(*choices))


def _metrics(
    rows: list[SquadValidationRow],
    weights: tuple[float, float, float],
) -> dict[str, Any]:
    return evaluate(
        [_apply_features(row, weights) for row in rows],
        [row.outcome for row in rows],
    )


def _comparison(candidate: dict[str, Any], baseline: dict[str, Any]) -> dict[str, Any]:
    return {
        "brier_score_delta": round(
            candidate["brier_score"] - baseline["brier_score"], 6
        ),
        "log_loss_delta": round(candidate["log_loss"] - baseline["log_loss"], 6),
        "beats_v4_on_brier": candidate["brier_score"] < baseline["brier_score"],
        "beats_v4_on_log_loss": candidate["log_loss"] < baseline["log_loss"],
        "beats_v4": (
            candidate["brier_score"] < baseline["brier_score"]
            and candidate["log_loss"] < baseline["log_loss"]
        ),
    }


def _coverage(rows: list[SquadValidationRow]) -> dict[str, Any]:
    total = len(rows)

    def item(predicate: str) -> dict[str, Any]:
        count = sum(bool(getattr(row, predicate)) for row in rows)
        return {
            "matches": count,
            "coverage_level": round(count / total, 6) if total else 0.0,
        }

    return {
        "eligible_v4_matches": total,
        "matches_with_injury_data": item("injury_data"),
        "matches_with_squad_data": item("squad_data"),
        "matches_with_lineup_data": item("lineup_data"),
        "matches_with_all_squad_features": {
            "matches": len(_covered(rows, "v4_plus_all_squad_features")),
            "coverage_level": (
                round(
                    len(_covered(rows, "v4_plus_all_squad_features")) / total,
                    6,
                )
                if total
                else 0.0
            ),
        },
    }


def _feature_usability(
    rows: list[SquadValidationRow],
    research_coverage: dict[str, Any] | None,
) -> dict[str, Any]:
    all_feature_matches = len(_covered(rows, "v4_plus_all_squad_features"))
    usable = all_feature_matches >= (
        MINIMUM_TUNING_MATCHES + MINIMUM_VALIDATION_MATCHES
    )
    coverage = research_coverage or {}
    return {
        "teams_with_squad_strength": int(
            coverage.get("teams_with_squad_strength", 0)
        ),
        "teams_with_injury_data": int(coverage.get("teams_with_injury_data", 0)),
        "teams_with_lineup_data": int(coverage.get("teams_with_lineup_data", 0)),
        "teams_with_unavailable_players": int(
            coverage.get("teams_with_unavailable_players", 0)
        ),
        "chronological_matches_with_all_features": all_feature_matches,
        "usable_for_validation": usable,
        "reason": (
            "sufficient point-in-time squad feature coverage"
            if usable
            else "insufficient chronological point-in-time squad feature coverage"
        ),
    }


def build_report(
    rows: list[SquadValidationRow],
    research_coverage: dict[str, Any] | None = None,
) -> dict[str, Any]:
    rows = sorted(rows, key=lambda row: (row.played_on, str(row.match_id or "")))
    base = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "model_version": MODEL_VERSION,
        "baseline_model": "elo-context-v4",
        "status": "research_only",
        "production_predictions_changed": False,
        "production_simulation_changed": False,
        "provider_capability_audit": PROVIDER_CAPABILITY_AUDIT,
        "coverage": _coverage(rows),
        "squad_feature_usability": _feature_usability(rows, research_coverage),
        "protocol": {
            "validation": "chronological holdout",
            "same_date_rows_kept_together": True,
            "point_in_time_rule": (
                "availability collected_at and strength rated_at must be before kickoff"
            ),
            "selection_objective": "minimum tuning Brier score, then log loss",
            "promotion_gate": (
                "lower holdout Brier score and log loss than v4 on identical matches"
            ),
        },
    }
    if len(rows) < MINIMUM_TUNING_MATCHES + MINIMUM_VALIDATION_MATCHES:
        return {
            **base,
            "status": "insufficient_data",
            "split": None,
            "ablations": {
                name: {
                    "status": "not_evaluated",
                    "reason": "insufficient eligible chronological matches",
                }
                for name in ABLATIONS
            },
            "promotion": {
                "recommend_promotion": False,
                "decision": "do not promote",
                "reason": "insufficient chronological point-in-time squad coverage",
            },
        }

    tuning_rows, validation_rows = chronological_split(rows, 0.22)
    results = {}
    for ablation in ABLATIONS:
        tuning = _covered(tuning_rows, ablation)
        validation = _covered(validation_rows, ablation)
        if (
            len(tuning) < MINIMUM_TUNING_MATCHES
            or len(validation) < MINIMUM_VALIDATION_MATCHES
        ):
            results[ablation] = {
                "status": "not_evaluated",
                "tuning_matches": len(tuning),
                "validation_matches": len(validation),
                "reason": "insufficient point-in-time feature coverage",
            }
            continue
        candidates = [
            (weights, _metrics(tuning, weights))
            for weights in _weight_grid(ablation)
        ]
        weights, tuning_metrics = min(
            candidates,
            key=lambda item: (
                item[1]["brier_score"],
                item[1]["log_loss"],
                sum(abs(value) for value in item[0]),
            ),
        )
        validation_metrics = _metrics(validation, weights)
        v4_metrics = _metrics(validation, (0.0, 0.0, 0.0))
        results[ablation] = {
            "status": "evaluated",
            "selected_weights": {
                "squad_strength": weights[0],
                "unavailable_player_penalty": weights[1],
                "projected_lineup_strength": weights[2],
            },
            "tuning_matches": len(tuning),
            "validation_matches": len(validation),
            "tuning_metrics": tuning_metrics,
            "validation_metrics": validation_metrics,
            "v4_same_match_metrics": v4_metrics,
            "comparison_vs_v4": _comparison(validation_metrics, v4_metrics),
            "calibration": validation_metrics["calibration_bins"],
        }

    promoted = [
        (name, result)
        for name, result in results.items()
        if name != "v4_only"
        and result.get("status") == "evaluated"
        and result["comparison_vs_v4"]["beats_v4"]
    ]
    selected = (
        min(
            promoted,
            key=lambda item: (
                item[1]["validation_metrics"]["brier_score"],
                item[1]["validation_metrics"]["log_loss"],
            ),
        )
        if promoted
        else None
    )
    evaluated_squad_ablation = any(
        name != "v4_only" and result.get("status") == "evaluated"
        for name, result in results.items()
    )
    return {
        **base,
        "status": (
            "chronological_holdout_complete"
            if evaluated_squad_ablation
            else "insufficient_squad_coverage"
        ),
        "split": {
            "tuning_matches": len(tuning_rows),
            "validation_matches": len(validation_rows),
            "validation_start": min(row.played_on for row in validation_rows).isoformat(),
            "validation_end": max(row.played_on for row in validation_rows).isoformat(),
        },
        "ablations": results,
        "promotion": {
            "recommend_promotion": selected is not None,
            "decision": (
                f"recommend further promotion review for {selected[0]}"
                if selected
                else "do not promote"
            ),
            "selected_ablation": selected[0] if selected else None,
            "reason": (
                "selected ablation beat v4 on holdout Brier and log loss"
                if selected
                else "insufficient point-in-time squad coverage"
                if not evaluated_squad_ablation
                else "no squad ablation beat v4 on both holdout metrics"
            ),
        },
    }


def _as_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime.combine(value, time.min)
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def load_validation_rows(engine: Engine) -> list[SquadValidationRow]:
    matches, _ = load_database_matches(engine)
    xg_rows = build_validation_rows(matches)
    schema = None if engine.dialect.name == "sqlite" else "public"
    tables = set(inspect(engine).get_table_names(schema=schema))
    required = {
        "matches",
        "squad_strength_ratings",
        "player_availability_reports",
        "projected_lineups",
    }
    if not required.issubset(tables):
        return [
            SquadValidationRow(
                played_on=row.played_on,
                outcome=row.outcome,
                v4=_apply_signal(row.v3, row.shot_volume_signal, 0.20),
                match_id=row.match_id,
            )
            for row in xg_rows
        ]

    metadata = MetaData()
    matches_table = Table("matches", metadata, schema=schema, autoload_with=engine)
    ratings_table = Table(
        "squad_strength_ratings", metadata, schema=schema, autoload_with=engine
    )
    availability_table = Table(
        "player_availability_reports", metadata, schema=schema, autoload_with=engine
    )
    lineups_table = Table(
        "projected_lineups", metadata, schema=schema, autoload_with=engine
    )
    date_column = next(
        (matches_table.c[name] for name in ("match_date", "kickoff") if name in matches_table.c),
        None,
    )
    if date_column is None:
        return []
    with engine.connect() as connection:
        kickoff_by_match = {
            match_id: kickoff
            for match_id, kickoff in connection.execute(
                select(matches_table.c.id, date_column)
            ).tuples()
        }
        rating_rows = [
            dict(row) for row in connection.execute(select(ratings_table)).mappings()
        ]
        availability_rows = [
            dict(row) for row in connection.execute(select(availability_table)).mappings()
        ]
        lineup_rows = [
            dict(row) for row in connection.execute(select(lineups_table)).mappings()
        ]

    def latest_rating(match_id: Any, team_id: Any, kickoff: datetime) -> dict[str, Any] | None:
        eligible = [
            row
            for row in rating_rows
            if row.get("team_id") == team_id
            and row.get("fixture_id") in (None, match_id)
            and (_as_datetime(row.get("rated_at")) or kickoff) < kickoff
        ]
        return max(
            eligible,
            key=lambda row: (
                _as_datetime(row.get("rated_at"))
                or datetime.min.replace(tzinfo=timezone.utc)
            ),
            default=None,
        )

    output = []
    for row in xg_rows:
        kickoff = _as_datetime(kickoff_by_match.get(row.match_id))
        if kickoff is None:
            continue
        home = latest_rating(row.match_id, row.home_team_id, kickoff)
        away = latest_rating(row.match_id, row.away_team_id, kickoff)
        reports = [
            item
            for item in availability_rows
            if item.get("fixture_id") == row.match_id
            and (_as_datetime(item.get("collected_at")) or kickoff) < kickoff
        ]
        lineups = [
            item
            for item in lineup_rows
            if item.get("fixture_id") == row.match_id
            and (_as_datetime(item.get("collected_at")) or kickoff) < kickoff
        ]
        both_ratings = home is not None and away is not None
        output.append(
            SquadValidationRow(
                played_on=row.played_on,
                outcome=row.outcome,
                v4=_apply_signal(row.v3, row.shot_volume_signal, 0.20),
                squad_strength_signal=(
                    (float(home["squad_strength"]) - float(away["squad_strength"])) / 100.0
                    if both_ratings
                    and home.get("squad_strength") is not None
                    and away.get("squad_strength") is not None
                    else None
                ),
                unavailable_penalty_signal=(
                    (
                        float(away["unavailable_player_penalty"])
                        - float(home["unavailable_player_penalty"])
                    )
                    / 25.0
                    if both_ratings
                    and home.get("unavailable_player_penalty") is not None
                    and away.get("unavailable_player_penalty") is not None
                    else None
                ),
                projected_lineup_signal=(
                    (
                        float(home["projected_lineup_strength"])
                        - float(away["projected_lineup_strength"])
                    )
                    / 100.0
                    if both_ratings
                    and home.get("projected_lineup_strength") is not None
                    and away.get("projected_lineup_strength") is not None
                    else None
                ),
                match_id=row.match_id,
                injury_data=bool(reports),
                squad_data=both_ratings,
                lineup_data=(
                    both_ratings
                    and home.get("projected_lineup_strength") is not None
                    and away.get("projected_lineup_strength") is not None
                ),
            )
        )
    return output


def load_research_coverage(engine: Engine) -> dict[str, Any]:
    schema = None if engine.dialect.name == "sqlite" else "public"
    tables = set(inspect(engine).get_table_names(schema=schema))
    required = {
        "squad_strength_ratings",
        "player_availability_reports",
        "projected_lineups",
    }
    if not required.issubset(tables):
        return {
            "teams_with_squad_strength": 0,
            "teams_with_injury_data": 0,
            "teams_with_lineup_data": 0,
            "teams_with_unavailable_players": 0,
        }

    metadata = MetaData()
    ratings_table = Table(
        "squad_strength_ratings", metadata, schema=schema, autoload_with=engine
    )
    availability_table = Table(
        "player_availability_reports", metadata, schema=schema, autoload_with=engine
    )
    lineups_table = Table(
        "projected_lineups", metadata, schema=schema, autoload_with=engine
    )
    with engine.connect() as connection:
        ratings = [
            dict(row) for row in connection.execute(select(ratings_table)).mappings()
        ]
        availability = [
            dict(row)
            for row in connection.execute(select(availability_table)).mappings()
        ]
        lineups = [
            dict(row) for row in connection.execute(select(lineups_table)).mappings()
        ]

    def team_identity(row: dict[str, Any]) -> str | None:
        value = row.get("team_code") or row.get("team_id")
        return str(value) if value is not None else None

    rating_teams = {
        team_identity(row)
        for row in ratings
        if team_identity(row) is not None
        and row.get("model_version") == MODEL_VERSION
        and (
            "squad_size" not in ratings_table.c
            or int(row.get("squad_size") or 0) > 0
        )
    }
    availability_teams = {
        team_identity(row)
        for row in availability
        if team_identity(row) is not None
    }
    unavailable_teams = {
        team_identity(row)
        for row in availability
        if team_identity(row) is not None
        and str(row.get("status") or "").casefold() in {"injured", "suspended"}
    }
    lineup_teams = {
        team_identity(row)
        for row in lineups
        if team_identity(row) is not None
    }
    return {
        "teams_with_squad_strength": len(rating_teams),
        "teams_with_injury_data": len(availability_teams),
        "teams_with_lineup_data": len(lineup_teams),
        "teams_with_unavailable_players": len(unavailable_teams),
    }


def print_summary(report: dict[str, Any]) -> None:
    coverage = report["coverage"]
    usability = report["squad_feature_usability"]
    print("Squad v4.1 research validation")
    print(
        "Coverage: "
        f"eligible={coverage['eligible_v4_matches']} "
        f"injury={coverage['matches_with_injury_data']['matches']} "
        f"squad={coverage['matches_with_squad_data']['matches']} "
        f"lineup={coverage['matches_with_lineup_data']['matches']}"
    )
    print(
        "Research teams: "
        f"squad_strength={usability['teams_with_squad_strength']} "
        f"injury_data={usability['teams_with_injury_data']} "
        f"lineup_data={usability['teams_with_lineup_data']} "
        f"usable_for_validation={usability['usable_for_validation']}"
    )
    for name, result in report["ablations"].items():
        if result["status"] != "evaluated":
            print(f"- {name}: not evaluated ({result['reason']})")
            continue
        metrics = result["validation_metrics"]
        comparison = result["comparison_vs_v4"]
        print(
            f"- {name}: matches={result['validation_matches']} "
            f"Brier={metrics['brier_score']:.6f} "
            f"log_loss={metrics['log_loss']:.6f} "
            f"beats_v4={comparison['beats_v4']}"
        )
    print(f"Promotion recommendation: {report['promotion']['decision']}")


def main() -> int:
    load_dotenv(ROOT / ".env", override=False)
    load_dotenv(ROOT / "backend" / ".env", override=False)
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        report = build_report([])
    else:
        engine = create_database_engine(database_url)
        try:
            report = build_report(
                load_validation_rows(engine),
                load_research_coverage(engine),
            )
        finally:
            engine.dispose()
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(json.dumps(report, indent=2, default=str) + "\n")
    print_summary(report)
    print(f"Wrote {REPORT_PATH.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
