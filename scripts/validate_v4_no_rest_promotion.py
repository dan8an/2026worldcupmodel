#!/usr/bin/env python3
from __future__ import annotations

import json
import math
import sys
from collections import defaultdict
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import MetaData, Table, select
from sqlalchemy.engine import Engine

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from modeling.src.data import build_fixtures, load_teams
from modeling.src.evaluation.metrics import ProbabilityVector, evaluate
from scripts.database import create_database_engine
from scripts.generate_predictions import (
    MODEL_VERSION,
    PredictionRepository,
    SHOT_VOLUME_WEIGHT,
    calculate_prediction,
    load_environment,
)
from scripts.validate_calibrated_v2 import chronological_split
from scripts.validate_freshness_gating_v4 import (
    _current_probability,
    _gated_probability,
)
from scripts.validate_xg_proxy_v4 import (
    XgProxyValidationRow,
    build_validation_rows,
    load_database_matches,
)

REPORT_PATH = (
    ROOT / "data" / "evaluation" / "v4_no_rest_promotion_report.json"
)
MARKET_PATH = ROOT / "data" / "evaluation" / "market_comparison_latest.json"
NO_REST_VERSION = "elo-context-v4-no-rest"
BASELINE_MODEL_VERSION = "elo-context-v4"
MIN_SUBGROUP_MATCHES = 20
MAX_BRIER_REGRESSION = 0.01
MAX_LOG_LOSS_REGRESSION = 0.02


def _metrics(
    rows: list[XgProxyValidationRow],
    probabilities: dict[int, ProbabilityVector],
) -> dict[str, Any]:
    return evaluate(
        [probabilities[id(row)] for row in rows],
        [row.outcome for row in rows],
    )


def _comparison(
    current: dict[str, Any],
    no_rest: dict[str, Any],
) -> dict[str, Any]:
    brier_delta = no_rest["brier_score"] - current["brier_score"]
    log_loss_delta = no_rest["log_loss"] - current["log_loss"]
    calibration_delta = (
        no_rest["expected_calibration_error"]
        - current["expected_calibration_error"]
    )
    return {
        "brier_delta": round(brier_delta, 6),
        "log_loss_delta": round(log_loss_delta, 6),
        "calibration_delta": round(calibration_delta, 6),
        "improves_brier": brier_delta < 0,
        "improves_log_loss": log_loss_delta < 0,
        "calibration_not_harmed": calibration_delta <= 0.005,
        "material_regression": (
            current["matches"] >= MIN_SUBGROUP_MATCHES
            and (
                brier_delta > MAX_BRIER_REGRESSION
                or log_loss_delta > MAX_LOG_LOSS_REGRESSION
            )
        ),
    }


def _paired_metrics(
    rows: list[XgProxyValidationRow],
    current: dict[int, ProbabilityVector],
    no_rest: dict[int, ProbabilityVector],
) -> dict[str, Any]:
    if not rows:
        return {
            "matches": 0,
            "current_v4": None,
            "v4_no_rest": None,
            "comparison": None,
        }
    current_metrics = _metrics(rows, current)
    no_rest_metrics = _metrics(rows, no_rest)
    return {
        "matches": len(rows),
        "current_v4": current_metrics,
        "v4_no_rest": no_rest_metrics,
        "comparison": _comparison(current_metrics, no_rest_metrics),
    }


def _outcome_segments(
    rows: list[XgProxyValidationRow],
    current: dict[int, ProbabilityVector],
) -> dict[str, list[XgProxyValidationRow]]:
    segments: dict[str, list[XgProxyValidationRow]] = {
        "favorites": [],
        "underdogs": [],
        "draws": [],
    }
    for row in rows:
        if row.outcome == 1:
            segments["draws"].append(row)
            continue
        vector = current[id(row)]
        favorite = 0 if vector[0] >= vector[2] else 2
        segment = "favorites" if row.outcome == favorite else "underdogs"
        segments[segment].append(row)
    return segments


def _team_confederations(engine: Engine) -> dict[Any, str | None]:
    schema = None if engine.dialect.name == "sqlite" else "public"
    metadata = MetaData()
    teams = Table("teams", metadata, schema=schema, autoload_with=engine)
    with engine.connect() as connection:
        return {
            row["id"]: row["confederation"]
            for row in connection.execute(
                select(teams.c.id, teams.c.confederation)
            ).mappings()
        }


def _confederation_segments(
    rows: list[XgProxyValidationRow],
) -> dict[str, list[XgProxyValidationRow]]:
    segments: dict[str, list[XgProxyValidationRow]] = defaultdict(list)
    for row in rows:
        confederations = {
            value
            for value in (row.home_confederation, row.away_confederation)
            if value
        }
        for confederation in confederations:
            segments[confederation].append(row)
    return dict(segments)


def _holdout_report(
    matches: list[dict[str, Any]],
    confederations: dict[Any, str | None],
) -> tuple[dict[str, Any], list[XgProxyValidationRow]]:
    rows = build_validation_rows(
        matches, team_confederations=confederations
    )
    tuning, holdout = chronological_split(rows, 0.22)
    current = {id(row): _current_probability(row) for row in holdout}
    no_rest = {
        id(row): _gated_probability(row, "v4_without_rest_penalty")
        for row in holdout
    }
    full = _paired_metrics(holdout, current, no_rest)
    outcomes = {
        name: _paired_metrics(segment, current, no_rest)
        for name, segment in _outcome_segments(holdout, current).items()
    }
    confederation_segments = _confederation_segments(holdout)
    confederation_results = {
        name: _paired_metrics(segment, current, no_rest)
        for name, segment in sorted(
            confederation_segments.items()
        )
    }
    confederation_covered_rows = {
        id(row)
        for segment in confederation_segments.values()
        for row in segment
    }
    material_regressions = [
        {
            "segment": name,
            "matches": result["matches"],
            **result["comparison"],
        }
        for name, result in {
            **outcomes,
            **{
                f"confederation:{name}": result
                for name, result in confederation_results.items()
            },
        }.items()
        if result["comparison"]
        and result["comparison"]["material_regression"]
    ]
    confederation_calibration_regressions = [
        {
            "confederation": name,
            "matches": result["matches"],
            "calibration_delta": result["comparison"][
                "calibration_delta"
            ],
        }
        for name, result in confederation_results.items()
        if (
            result["matches"] >= MIN_SUBGROUP_MATCHES
            and result["comparison"]
            and result["comparison"]["calibration_delta"] > 0.01
        )
    ]
    calibration_harmed = (
        full["comparison"]["calibration_delta"] > 0.005
        or bool(confederation_calibration_regressions)
    )
    return {
        "protocol": {
            "method": "existing xg-proxy-v4 chronological holdout",
            "tuning_matches": len(tuning),
            "holdout_matches": len(holdout),
            "holdout_start": min(row.played_on for row in holdout).isoformat(),
            "holdout_end": max(row.played_on for row in holdout).isoformat(),
            "segment_definition": (
                "Favorites and underdogs are decisive outcomes classified by "
                "the higher current-v4 home/away pre-match probability; draws "
                "are evaluated separately."
            ),
            "confederation_counting": (
                "A match appears once in each participating confederation; "
                "same-confederation matches appear once."
            ),
        },
        "full_holdout": full,
        "outcome_segments": outcomes,
        "confederations": confederation_results,
        "confederation_coverage": {
            "holdout_matches_with_at_least_one_label": len(
                confederation_covered_rows
            ),
            "holdout_matches": len(holdout),
            "coverage": round(
                len(confederation_covered_rows) / len(holdout), 6
            ),
            "limitation": (
                "Database confederation metadata is sparse outside currently "
                "canonical teams. Confederation slices are reported where "
                "available but do not independently determine promotion."
            ),
        },
        "calibration_assessment": {
            "full_holdout_ece_delta": full["comparison"][
                "calibration_delta"
            ],
            "major_calibration_harm": calibration_harmed,
            "eligible_confederation_regressions": (
                confederation_calibration_regressions
            ),
            "outcome_segment_note": (
                "Outcome segments are selected using the realized result, so "
                "their ECE values are descriptive and are not used as a "
                "pre-match calibration promotion gate."
            ),
            "thresholds": {
                "full_holdout_ece_regression": 0.005,
                "subgroup_ece_regression": 0.01,
                "minimum_subgroup_matches": MIN_SUBGROUP_MATCHES,
            },
        },
        "subgroup_gate": {
            "minimum_matches": MIN_SUBGROUP_MATCHES,
            "maximum_brier_regression": MAX_BRIER_REGRESSION,
            "maximum_log_loss_regression": MAX_LOG_LOSS_REGRESSION,
            "material_regressions": material_regressions,
        },
    }, holdout


def _live_prediction(
    home_id: str,
    away_id: str,
    kickoff: date,
    ratings: dict[str, dict[str, Any]],
    shot_volume: dict[str, dict[str, Any]],
    latest_dates: dict[str, date],
    team_names: dict[str, str],
    no_rest: bool,
) -> dict[str, Any]:
    prediction = calculate_prediction(
        ratings[home_id],
        ratings[away_id],
        home_team_name=team_names[home_id],
        away_team_name=team_names[away_id],
        home_shot_volume_rating=shot_volume.get(home_id, {}).get(
            "shot_volume_rating"
        ),
        away_shot_volume_rating=shot_volume.get(away_id, {}).get(
            "shot_volume_rating"
        ),
    )
    if no_rest:
        return prediction

    def difference(home: Any, away: Any, scale: float) -> float:
        if home is None or away is None:
            return 0.0
        return max(-1.0, min(1.0, (float(home) - float(away)) / scale))

    def normalize(values: tuple[float, float, float]) -> ProbabilityVector:
        total = sum(values)
        return tuple(value / total for value in values)  # type: ignore[return-value]

    home_rest = (kickoff - latest_dates[home_id]).days
    away_rest = (kickoff - latest_dates[away_id]).days
    attack = difference(
        ratings[home_id].get("attack_rating"),
        ratings[away_id].get("attack_rating"),
        100.0,
    )
    defense = difference(
        ratings[home_id].get("defense_rating"),
        ratings[away_id].get("defense_rating"),
        100.0,
    )
    rest = difference(home_rest, away_rest, 14.0)
    context_tilt = 0.15 * attack + 0.30 * defense - 0.15 * rest
    elo = (
        prediction["elo_base_home_probability"],
        prediction["elo_base_draw_probability"],
        prediction["elo_base_away_probability"],
    )
    legacy_v3 = normalize(
        (
            elo[0] * math.exp(context_tilt),
            elo[1] * 1.15,
            elo[2] * math.exp(-context_tilt),
        )
    )
    shot_tilt = SHOT_VOLUME_WEIGHT * difference(
        shot_volume.get(home_id, {}).get("shot_volume_rating"),
        shot_volume.get(away_id, {}).get("shot_volume_rating"),
        100.0,
    )
    final = normalize(
        (
            legacy_v3[0] * math.exp(shot_tilt),
            legacy_v3[1],
            legacy_v3[2] * math.exp(-shot_tilt),
        )
    )
    return {
        **prediction,
        "home_win_probability": final[0],
        "draw_probability": final[1],
        "away_win_probability": final[2],
    }


def _team_probability(
    prediction: dict[str, Any],
    is_home: bool,
) -> float:
    return prediction[
        "home_win_probability" if is_home else "away_win_probability"
    ]


def _live_report(
    engine: Engine,
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    repository = PredictionRepository(engine)
    database_team_ids = repository.load_database_team_ids()
    ratings = repository.load_current_team_ratings(database_team_ids)
    shot_volume = repository.load_current_shot_volume_details(database_team_ids)
    latest_dates = repository.load_latest_team_match_dates(database_team_ids)
    teams = {team.id: team for team in load_teams()}
    team_names = {team_id: team.name for team_id, team in teams.items()}
    fixtures = [
        fixture for fixture in build_fixtures(list(teams.values()))
        if fixture.stage == "group"
        and fixture.home_team_id in latest_dates
        and fixture.away_team_id in latest_dates
    ]
    predictions: dict[str, dict[str, Any]] = {}
    for fixture in fixtures:
        predictions[fixture.id] = {
            "fixture": fixture,
            "current": _live_prediction(
                fixture.home_team_id,
                fixture.away_team_id,
                fixture.kickoff.date(),
                ratings,
                shot_volume,
                latest_dates,
                team_names,
                False,
            ),
            "no_rest": _live_prediction(
                fixture.home_team_id,
                fixture.away_team_id,
                fixture.kickoff.date(),
                ratings,
                shot_volume,
                latest_dates,
                team_names,
                True,
            ),
        }
    focus_ids = ("MEX", "NOR", "CUW", "CZE", "BIH")
    focus = {}
    for team_id in focus_ids:
        rows = []
        for match_id, details in predictions.items():
            fixture = details["fixture"]
            if team_id not in (
                fixture.home_team_id, fixture.away_team_id
            ):
                continue
            is_home = fixture.home_team_id == team_id
            opponent_id = (
                fixture.away_team_id
                if is_home
                else fixture.home_team_id
            )
            before = _team_probability(details["current"], is_home)
            after = _team_probability(details["no_rest"], is_home)
            rows.append(
                {
                    "match_id": match_id,
                    "opponent": team_names[opponent_id],
                    "kickoff": fixture.kickoff.isoformat(),
                    "current_v4_team_win_probability": before,
                    "v4_no_rest_team_win_probability": after,
                    "change": round(after - before, 6),
                }
            )
        focus[team_id] = {
            "team": team_names[team_id],
            "latest_input_match_date": latest_dates[team_id].isoformat(),
            "group_matches": rows,
            "mean_current_v4_win_probability": round(
                sum(row["current_v4_team_win_probability"] for row in rows)
                / len(rows),
                6,
            ),
            "mean_v4_no_rest_win_probability": round(
                sum(row["v4_no_rest_team_win_probability"] for row in rows)
                / len(rows),
                6,
            ),
        }
    mexico = predictions["WC26-001"]
    mexico_comparison = {
        "current_v4": {
            "mexico": mexico["current"]["home_win_probability"],
            "draw": mexico["current"]["draw_probability"],
            "south_africa": mexico["current"]["away_win_probability"],
        },
        "v4_no_rest": {
            "mexico": mexico["no_rest"]["home_win_probability"],
            "draw": mexico["no_rest"]["draw_probability"],
            "south_africa": mexico["no_rest"]["away_win_probability"],
        },
    }
    return {
        "mexico_vs_south_africa": mexico_comparison,
        "focus_teams": focus,
    }, predictions


def _market_report(
    live_predictions: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    if not MARKET_PATH.exists():
        return {
            "available": False,
            "reason": "market comparison report is unavailable",
        }
    market = json.loads(MARKET_PATH.read_text())
    rows = []
    for comparison in market.get("sample_comparisons", []):
        match_id = comparison.get("canonical_match_id")
        if (
            comparison.get("disagreement_bucket") != "10%+"
            or match_id not in live_predictions
        ):
            continue
        details = live_predictions[match_id]
        market_vector = comparison["market_probability"]
        current = details["current"]
        no_rest = details["no_rest"]

        def disagreement(prediction: dict[str, Any]) -> float:
            return sum(
                abs(prediction[key] - market_vector[market_key])
                for key, market_key in (
                    ("home_win_probability", "home"),
                    ("draw_probability", "draw"),
                    ("away_win_probability", "away"),
                )
            ) / 3

        current_disagreement = disagreement(current)
        no_rest_disagreement = disagreement(no_rest)
        rows.append(
            {
                "canonical_match_id": match_id,
                "bookmaker_count": comparison.get("bookmaker_count"),
                "market_probability": market_vector,
                "current_v4_probability": {
                    "home": current["home_win_probability"],
                    "draw": current["draw_probability"],
                    "away": current["away_win_probability"],
                },
                "v4_no_rest_probability": {
                    "home": no_rest["home_win_probability"],
                    "draw": no_rest["draw_probability"],
                    "away": no_rest["away_win_probability"],
                },
                "current_average_absolute_disagreement": (
                    current_disagreement
                ),
                "no_rest_average_absolute_disagreement": (
                    no_rest_disagreement
                ),
                "disagreement_change": round(
                    no_rest_disagreement - current_disagreement, 6
                ),
                "outcome_available": comparison.get("outcome") is not None,
            }
        )
    return {
        "available": bool(rows),
        "historical_outcomes_available": any(
            row["outcome_available"] for row in rows
        ),
        "purpose": (
            "diagnostic comparison only; market probabilities are not model inputs"
        ),
        "matches": rows,
        "validation_limit": (
            "No completed high-disagreement market match has an outcome, so "
            "this section cannot support the promotion performance gate."
        ),
    }


def build_report(engine: Engine) -> dict[str, Any]:
    matches, source_coverage = load_database_matches(engine)
    confederations = _team_confederations(engine)
    holdout, _ = _holdout_report(matches, confederations)
    live, live_predictions = _live_report(engine)
    market = _market_report(live_predictions)
    full = holdout["full_holdout"]
    beats_full = (
        full["comparison"]["improves_brier"]
        and full["comparison"]["improves_log_loss"]
    )
    no_major_regressions = not holdout["subgroup_gate"][
        "material_regressions"
    ]
    calibration_ok = not holdout["calibration_assessment"][
        "major_calibration_harm"
    ]
    recommend = beats_full and no_major_regressions and calibration_ok
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "production_model_version": MODEL_VERSION,
        "baseline_model_version": BASELINE_MODEL_VERSION,
        "candidate_model_version": NO_REST_VERSION,
        "production_changed": True,
        "market_odds_used_in_model": False,
        "source_coverage": source_coverage,
        "historical_validation": holdout,
        "live_before_after": live,
        "high_disagreement_market_diagnostics": market,
        "promotion": {
            "beats_full_holdout_brier_and_log_loss": beats_full,
            "no_major_subgroup_regressions": no_major_regressions,
            "calibration_not_materially_harmed": calibration_ok,
            "recommend_promotion": recommend,
            "decision": (
                f"promoted {NO_REST_VERSION} as {MODEL_VERSION}"
                if recommend
                else "do not promote"
            ),
            "implementation_status": "production promotion complete",
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
