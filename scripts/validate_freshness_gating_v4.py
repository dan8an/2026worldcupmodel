#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
from collections import defaultdict
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy.engine import Engine

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from modeling.src.data import build_fixtures, load_teams
from modeling.src.evaluation.metrics import evaluate
from scripts.database import create_database_engine
from scripts.freshness_gating import (
    EXPERIMENTAL_MODEL_VERSION,
    calculate_freshness_gated_prediction,
)
from scripts.generate_predictions import (
    MODEL_VERSION,
    PredictionRepository,
    SHOT_VOLUME_WEIGHT,
    calculate_prediction,
    canonical_prior_elo,
    load_environment,
)
from scripts.validate_calibrated_v2 import chronological_split
from scripts.validate_xg_proxy_v4 import (
    XgProxyValidationRow,
    _apply_signal,
    build_validation_rows,
    load_database_matches,
)

REPORT_PATH = ROOT / "data" / "evaluation" / "freshness_gating_v4_report.json"
ABLATIONS = (
    "current_v4",
    "v4_freshness_gated",
    "v4_without_rest_penalty",
    "v4_attack_defense_freshness_only",
    "v4_full_reliability_gating",
)


def _probability(result: dict[str, Any]) -> tuple[float, float, float]:
    return (
        result["home_win_probability"],
        result["draw_probability"],
        result["away_win_probability"],
    )


def _rating(row: XgProxyValidationRow, home: bool) -> dict[str, Any]:
    prefix = "home" if home else "away"
    return {
        "elo_rating": getattr(row, f"{prefix}_elo"),
        "attack_rating": getattr(row, f"{prefix}_attack_rating"),
        "defense_rating": getattr(row, f"{prefix}_defense_rating"),
        "matches_played": getattr(row, f"{prefix}_rating_sample"),
    }


def _current_probability(
    row: XgProxyValidationRow,
) -> tuple[float, float, float]:
    return _apply_signal(
        row.v3, row.shot_volume_signal, SHOT_VOLUME_WEIGHT
    )


def _gated_probability(
    row: XgProxyValidationRow,
    ablation: str,
) -> tuple[float, float, float]:
    if ablation == "current_v4":
        return _current_probability(row)
    if ablation == "v4_without_rest_penalty":
        result = calculate_prediction(
            _rating(row, True),
            _rating(row, False),
            home_rest_days=None,
            away_rest_days=None,
            home_shot_volume_rating=row.home_shot_volume_rating,
            away_shot_volume_rating=row.away_shot_volume_rating,
        )
        return _probability(result)
    gates = {
        "v4_attack_defense_freshness_only": {
            "gate_elo": False,
            "gate_attack_defense": True,
            "gate_rest": False,
            "gate_shot_volume": False,
        },
        "v4_freshness_gated": {
            "gate_elo": True,
            "gate_attack_defense": True,
            "gate_rest": True,
            "gate_shot_volume": False,
        },
        "v4_full_reliability_gating": {
            "gate_elo": True,
            "gate_attack_defense": True,
            "gate_rest": True,
            "gate_shot_volume": True,
        },
    }[ablation]
    result = calculate_freshness_gated_prediction(
        _rating(row, True),
        _rating(row, False),
        home_rating_age_days=row.home_rating_age_days,
        away_rating_age_days=row.away_rating_age_days,
        home_shot_volume_age_days=row.home_shot_volume_age_days,
        away_shot_volume_age_days=row.away_shot_volume_age_days,
        home_shot_volume_rating=row.home_shot_volume_rating,
        away_shot_volume_rating=row.away_shot_volume_rating,
        home_shot_volume_sample=row.home_shot_volume_sample,
        away_shot_volume_sample=row.away_shot_volume_sample,
        home_rest_days=row.home_rating_age_days,
        away_rest_days=row.away_rating_age_days,
        **gates,
    )
    return _probability(result)


def _holdout_report(matches: list[dict[str, Any]]) -> dict[str, Any]:
    rows = build_validation_rows(matches)
    tuning, holdout = chronological_split(rows, 0.22)
    outcomes = [row.outcome for row in holdout]
    metrics = {
        ablation: evaluate(
            [_gated_probability(row, ablation) for row in holdout],
            outcomes,
        )
        for ablation in ABLATIONS
    }
    current = metrics["current_v4"]
    comparisons = {}
    for name, result in metrics.items():
        comparisons[name] = {
            "brier_delta_vs_current": round(
                result["brier_score"] - current["brier_score"], 6
            ),
            "log_loss_delta_vs_current": round(
                result["log_loss"] - current["log_loss"], 6
            ),
            "calibration_delta_vs_current": round(
                result["expected_calibration_error"]
                - current["expected_calibration_error"],
                6,
            ),
        }
    return {
        "protocol": {
            "method": "existing xg-proxy-v4 chronological holdout",
            "tuning_matches": len(tuning),
            "holdout_matches": len(holdout),
            "holdout_start": min(row.played_on for row in holdout).isoformat(),
            "holdout_end": max(row.played_on for row in holdout).isoformat(),
            "same_day_updates": "batched after prediction",
            "historical_elo_prior": 1500.0,
            "historical_prior_note": (
                "Point-in-time FIFA snapshots are unavailable. Historical Elo "
                "shrinks to the time-safe neutral initialization prior; live "
                "canonical fixtures use repository rank priors."
            ),
        },
        "metrics": metrics,
        "comparisons": comparisons,
    }


def _live_prediction(
    team_ids: tuple[str, str],
    kickoff: date,
    ratings: dict[str, dict[str, Any]],
    shot_volume: dict[str, dict[str, Any]],
    latest_dates: dict[str, date],
    teams: dict[str, Any],
    full_gating: bool,
) -> dict[str, Any]:
    home_id, away_id = team_ids
    home_rest = (kickoff - latest_dates[home_id]).days
    away_rest = (kickoff - latest_dates[away_id]).days
    if not full_gating:
        return calculate_prediction(
            ratings[home_id],
            ratings[away_id],
            home_rest_days=home_rest,
            away_rest_days=away_rest,
            home_team_name=teams[home_id].name,
            away_team_name=teams[away_id].name,
            home_shot_volume_rating=shot_volume.get(home_id, {}).get(
                "shot_volume_rating"
            ),
            away_shot_volume_rating=shot_volume.get(away_id, {}).get(
                "shot_volume_rating"
            ),
        )
    return calculate_freshness_gated_prediction(
        ratings[home_id],
        ratings[away_id],
        home_canonical_elo_prior=canonical_prior_elo(teams[home_id].rank),
        away_canonical_elo_prior=canonical_prior_elo(teams[away_id].rank),
        home_rating_age_days=home_rest,
        away_rating_age_days=away_rest,
        home_shot_volume_age_days=home_rest,
        away_shot_volume_age_days=away_rest,
        home_shot_volume_rating=shot_volume.get(home_id, {}).get(
            "shot_volume_rating"
        ),
        away_shot_volume_rating=shot_volume.get(away_id, {}).get(
            "shot_volume_rating"
        ),
        home_shot_volume_sample=shot_volume.get(home_id, {}).get(
            "sample_matches"
        ),
        away_shot_volume_sample=shot_volume.get(away_id, {}).get(
            "sample_matches"
        ),
        home_rest_days=home_rest,
        away_rest_days=away_rest,
        home_team_name=teams[home_id].name,
        away_team_name=teams[away_id].name,
    )


def _live_report(engine: Engine) -> dict[str, Any]:
    repository = PredictionRepository(engine)
    database_team_ids = repository.load_database_team_ids()
    ratings = repository.load_current_team_ratings(database_team_ids)
    shot_volume = repository.load_current_shot_volume_details(database_team_ids)
    latest_dates = repository.load_latest_team_match_dates(database_team_ids)
    teams = {team.id: team for team in load_teams()}
    kickoff = date(2026, 6, 11)
    before = _live_prediction(
        ("MEX", "RSA"),
        kickoff,
        ratings,
        shot_volume,
        latest_dates,
        teams,
        False,
    )
    after = _live_prediction(
        ("MEX", "RSA"),
        kickoff,
        ratings,
        shot_volume,
        latest_dates,
        teams,
        True,
    )

    impacts = []
    for fixture in build_fixtures(list(teams.values())):
        if (
            fixture.stage != "group"
            or fixture.home_team_id not in latest_dates
            or fixture.away_team_id not in latest_dates
        ):
            continue
        fixture_date = fixture.kickoff.date()
        current = _live_prediction(
            (fixture.home_team_id, fixture.away_team_id),
            fixture_date,
            ratings,
            shot_volume,
            latest_dates,
            teams,
            False,
        )
        gated = _live_prediction(
            (fixture.home_team_id, fixture.away_team_id),
            fixture_date,
            ratings,
            shot_volume,
            latest_dates,
            teams,
            True,
        )
        impacts.append(
            {
                "match_id": fixture.id,
                "home_team": teams[fixture.home_team_id].name,
                "away_team": teams[fixture.away_team_id].name,
                "home_probability_change": round(
                    gated["home_win_probability"]
                    - current["home_win_probability"],
                    6,
                ),
                "away_probability_change": round(
                    gated["away_win_probability"]
                    - current["away_win_probability"],
                    6,
                ),
                "maximum_absolute_change": round(
                    max(
                        abs(
                            gated["home_win_probability"]
                            - current["home_win_probability"]
                        ),
                        abs(
                            gated["draw_probability"]
                            - current["draw_probability"]
                        ),
                        abs(
                            gated["away_win_probability"]
                            - current["away_win_probability"]
                        ),
                    ),
                    6,
                ),
            }
        )
    impacts.sort(
        key=lambda row: row["maximum_absolute_change"], reverse=True
    )
    team_changes: dict[str, list[float]] = defaultdict(list)
    for impact in impacts:
        team_changes[impact["home_team"]].append(
            abs(impact["home_probability_change"])
        )
        team_changes[impact["away_team"]].append(
            abs(impact["away_probability_change"])
        )
    team_impacts = [
        {
            "team": team,
            "group_matches": len(changes),
            "mean_absolute_win_probability_change": round(
                sum(changes) / len(changes), 6
            ),
            "maximum_absolute_win_probability_change": round(
                max(changes), 6
            ),
        }
        for team, changes in team_changes.items()
    ]
    team_impacts.sort(
        key=lambda row: (
            row["mean_absolute_win_probability_change"],
            row["maximum_absolute_win_probability_change"],
        ),
        reverse=True,
    )
    return {
        "mexico_vs_south_africa": {
            "before_current_v4": {
                "mexico": before["home_win_probability"],
                "draw": before["draw_probability"],
                "south_africa": before["away_win_probability"],
            },
            "after_full_reliability_gating": {
                "mexico": after["home_win_probability"],
                "draw": after["draw_probability"],
                "south_africa": after["away_win_probability"],
            },
            "input_reliability": after["input_reliability"],
        },
        "teams_most_affected": team_impacts[:15],
        "most_affected_group_fixtures": impacts[:15],
    }


def build_report(engine: Engine) -> dict[str, Any]:
    matches, source_coverage = load_database_matches(engine)
    holdout = _holdout_report(matches)
    live = _live_report(engine)
    current = holdout["metrics"]["current_v4"]
    candidates = {
        name: metrics
        for name, metrics in holdout["metrics"].items()
        if name != "current_v4"
    }
    best_name, best = min(
        candidates.items(),
        key=lambda item: (
            item[1]["brier_score"],
            item[1]["log_loss"],
        ),
    )
    best_beats_current = (
        best["brier_score"] < current["brier_score"]
        and best["log_loss"] < current["log_loss"]
    )
    experimental = holdout["metrics"]["v4_freshness_gated"]
    experimental_beats_current = (
        experimental["brier_score"] < current["brier_score"]
        and experimental["log_loss"] < current["log_loss"]
    )
    mexico_change = (
        live["mexico_vs_south_africa"]["after_full_reliability_gating"][
            "mexico"
        ]
        - live["mexico_vs_south_africa"]["before_current_v4"]["mexico"]
    )
    experimental_no_material_harm = (
        experimental["brier_score"] - current["brier_score"] <= 0.002
        and experimental["log_loss"] - current["log_loss"] <= 0.002
    )
    pathology_fixed = mexico_change >= 0.05
    recommend_experimental = experimental_beats_current or (
        pathology_fixed and experimental_no_material_harm
    )
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "production_model_version": MODEL_VERSION,
        "experimental_model_version": EXPERIMENTAL_MODEL_VERSION,
        "market_odds_used": False,
        "source_coverage": source_coverage,
        "reliability_policy": {
            "freshness_grace_days": 90,
            "freshness_half_life_days": 180,
            "sample_reliability": "matches / (matches + 10)",
            "combined_reliability": "sqrt(freshness * sample_reliability)",
            "elo_prior_live": "canonical rank prior",
            "elo_prior_holdout": 1500.0,
            "attack_defense_prior": 50.0,
            "shot_volume_prior": 50.0,
            "rest_context": (
                "rest difference multiplied by the lower team freshness"
            ),
        },
        "historical_holdout": holdout,
        "live_diagnostics": live,
        "promotion": {
            "best_candidate": best_name,
            "best_candidate_beats_current_on_brier_and_log_loss": (
                best_beats_current
            ),
            "experimental_beats_current_on_brier_and_log_loss": (
                experimental_beats_current
            ),
            "clearly_fixes_mexico_stale_pathology": pathology_fixed,
            "experimental_no_material_holdout_harm": (
                experimental_no_material_harm
            ),
            "recommend_experimental_promotion": recommend_experimental,
            "recommend_narrow_rest_change_for_review": (
                best_name == "v4_without_rest_penalty"
                and best_beats_current
            ),
            "production_changed": False,
            "decision": (
                "do not promote full freshness gating; review removal or "
                "stale-only disabling of the rest penalty"
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
        REPORT_PATH.write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps(report["promotion"], indent=2))
        print(f"Wrote {REPORT_PATH.relative_to(ROOT)}")
        return 0
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
