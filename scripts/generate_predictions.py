#!/usr/bin/env python3
from __future__ import annotations

import json
import logging
import math
import os
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from dotenv import load_dotenv
from sqlalchemy import JSON, MetaData, Table, inspect, select, text, update
from sqlalchemy.engine import Connection, Engine

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from modeling.src.data import build_fixtures, load_teams, validate_tournament
from scripts.confidence_v1 import (
    CONFIDENCE_VERSION,
    HIGH_THRESHOLD,
    MEDIUM_THRESHOLD,
    DataCompleteness,
    calculate_confidence,
)
from scripts.database import create_database_engine

MODEL_VERSION = "elo-context-v4.2.1"
MODEL_DESCRIPTION = (
    "v4 shot-volume model with v4.2.1 matchup-specific draw calibration and "
    "rest/context component removed."
)
LEGACY_MODEL_VERSION = "poisson-ratings-v1"
CHANCE_QUALITY_MODEL_VERSION = "xg-proxy-v4"
PROMOTION_CONFIG_PATH = (
    ROOT / "data" / "evaluation" / "xg_proxy_v4_promotion_config.json"
)
TEAM_ALIASES_PATH = ROOT / "data" / "seed" / "team_aliases.json"
MAX_GOALS = 6
V3_ATTACK_WEIGHT = 0.15
V3_DEFENSE_WEIGHT = 0.30
V3_DRAW_MULTIPLIER = 1.15
DRAW_MIN_PROBABILITY = 0.185
DRAW_MAX_PROBABILITY = 0.40
DRAW_CLOSE_MATCH_BOOST_WEIGHT = 0.041
DRAW_LOW_TOTAL_BOOST_WEIGHT = 0.0565
DRAW_CLEAR_EDGE_PENALTY_WEIGHT = 0.060
DRAW_HIGH_TOTAL_PENALTY_WEIGHT = 0.040
DRAW_HIGH_ATTACK_PENALTY_WEIGHT = 0.020
SHOT_VOLUME_FACTOR_MINIMUM_IMPACT = 0.0005
PREDICTION_REQUIRED_COLUMNS = {
    "canonical_match_id",
    "model_run_id",
    "elo_base_home_probability",
    "elo_base_draw_probability",
    "elo_base_away_probability",
    "attack_defense_adjustment",
    "draw_calibration_adjustment",
    "context_adjustment_total",
    "final_home_probability",
    "final_draw_probability",
    "final_away_probability",
    "home_xg",
    "away_xg",
    "prediction_timestamp",
    "model_version",
    "confidence_score",
    "confidence_tier",
    "confidence_explanation",
    "top_factors",
    "home_win_probability",
    "draw_probability",
    "away_win_probability",
    "most_likely_scoreline",
    "expected_total_goals",
    "over_2_5_probability",
    "both_teams_to_score_probability",
    "score_probabilities",
}


def load_promotion_config() -> dict[str, Any]:
    config = json.loads(PROMOTION_CONFIG_PATH.read_text())
    if config.get("selected_ablation") != "v3_plus_shot_volume":
        raise ValueError(
            "Production promotion config must select v3_plus_shot_volume"
        )
    if config.get("features_used") != ["shot_volume_rating"]:
        raise ValueError(
            "Production promotion config may only use shot_volume_rating"
        )
    try:
        weight = float(config["selected_weight"])
    except (KeyError, TypeError, ValueError):
        weight = float("nan")
    if not math.isfinite(weight):
        raise ValueError("Production promotion config has an invalid selected_weight")
    return {**config, "selected_weight": weight}


PROMOTION_CONFIG = load_promotion_config()
SHOT_VOLUME_WEIGHT = PROMOTION_CONFIG["selected_weight"]


def load_environment() -> dict[str, str]:
    """Load server-side env files without overriding exported values."""
    load_dotenv(ROOT / ".env", override=False)
    load_dotenv(ROOT / "backend" / ".env", override=False)
    return dict(os.environ)


def generation_time(env: dict[str, str]) -> datetime:
    """Use current UTC time, with an explicit override for backfills and tests."""
    override = env.get("PREDICTION_GENERATION_TIME")
    if not override:
        return datetime.now(timezone.utc)
    parsed = _parse_timestamp(override)
    if parsed is None:
        raise ValueError("PREDICTION_GENERATION_TIME must be an ISO-8601 timestamp")
    return parsed


def _number(value: Any, default: float = 0.0) -> float:
    if value is None:
        return default
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, value))


def _parse_timestamp(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime.combine(value, datetime.min.time())
    else:
        raw = str(value).strip().replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _normalize_name(value: Any) -> str:
    return "".join(character for character in str(value or "").lower() if character.isalnum())


def canonical_prior_elo(rank: int) -> float:
    """Return a conservative rank prior on the database Elo scale."""
    return round(1500.0 + _clamp((50.0 - rank) * 1.5, -75.0, 75.0), 2)


def load_team_aliases() -> dict[str, list[str]]:
    payload = json.loads(TEAM_ALIASES_PATH.read_text())
    canonical_ids = {team.id for team in load_teams()}
    if set(payload) != canonical_ids:
        missing = sorted(canonical_ids - set(payload))
        extra = sorted(set(payload) - canonical_ids)
        raise RuntimeError(
            f"Team aliases must cover all canonical teams; missing={missing}, extra={extra}"
        )
    alias_owners: dict[str, str] = {}
    for team_id, aliases in payload.items():
        for alias in aliases:
            normalized = _normalize_name(alias)
            owner = alias_owners.get(normalized)
            if owner is not None and owner != team_id:
                raise RuntimeError(
                    f"Team alias {alias!r} is shared by {owner} and {team_id}"
                )
            alias_owners[normalized] = team_id
    return payload


def load_canonical_future_matches(
    now: datetime,
    database_matches: list[dict[str, Any]] | None = None,
    database_team_ids: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Load the complete active group-stage catalog, then enrich it.

    Tournament simulations require all 72 group fixtures. Keep already-started
    group matches in prediction snapshots until the group stage is complete.
    """
    teams = load_teams()
    fixtures = build_fixtures(teams)
    validate_tournament(teams, fixtures)
    group_fixtures = [fixture for fixture in fixtures if fixture.stage == "group"]
    if all(fixture.kickoff <= now for fixture in group_fixtures):
        return []
    database_matches = database_matches or []
    database_team_ids = database_team_ids or {}

    by_id = {str(row["id"]): row for row in database_matches}
    enriched = []
    for fixture in group_fixtures:
        if fixture.home_team_id is None or fixture.away_team_id is None:
            continue

        database_match = by_id.get(fixture.id)
        if database_match is None:
            home_database_id = database_team_ids.get(fixture.home_team_id)
            away_database_id = database_team_ids.get(fixture.away_team_id)
            for row in database_matches:
                kickoff = _parse_timestamp(row.get("kickoff") or row.get("match_date"))
                if (
                    kickoff
                    and kickoff == fixture.kickoff
                    and row.get("home_team_id") == home_database_id
                    and row.get("away_team_id") == away_database_id
                ):
                    database_match = row
                    break

        enriched.append(
            {
                "id": fixture.id,
                "canonical_match_id": fixture.id,
                "number": fixture.number,
                "stage": fixture.stage,
                "kickoff": fixture.kickoff,
                "home_team_id": fixture.home_team_id,
                "away_team_id": fixture.away_team_id,
                "database_match_id": (
                    database_match.get("id") if database_match is not None else None
                ),
            }
        )
    return enriched


def missing_canonical_group_fixtures(
    prediction_ids: set[str],
) -> list[str]:
    teams = {team.id: team.name for team in load_teams()}
    return [
        (
            f"{fixture.id} "
            f"({teams[fixture.home_team_id]} vs {teams[fixture.away_team_id]})"
        )
        for fixture in build_fixtures()
        if fixture.stage == "group" and fixture.id not in prediction_ids
    ]


def assert_complete_group_predictions(predictions: list[dict[str, Any]]) -> None:
    prediction_ids = {
        prediction["canonical_match_id"] for prediction in predictions
    }
    missing = missing_canonical_group_fixtures(prediction_ids)
    if missing:
        raise RuntimeError(
            f"Prediction generation is missing {len(missing)} canonical group "
            f"fixtures: {', '.join(missing)}"
        )


def poisson_probability(expected_goals: float, goals: int) -> float:
    return math.exp(-expected_goals) * expected_goals**goals / math.factorial(goals)


def build_score_probabilities(
    home_xg: float,
    away_xg: float,
) -> list[dict[str, float | int]]:
    """Return a normalized, deterministic 0-0 through 6-6 score grid."""
    scores = []
    for home_goals in range(MAX_GOALS + 1):
        for away_goals in range(MAX_GOALS + 1):
            scores.append(
                {
                    "home_goals": home_goals,
                    "away_goals": away_goals,
                    "probability": (
                        poisson_probability(home_xg, home_goals)
                        * poisson_probability(away_xg, away_goals)
                    ),
                }
            )

    # The 7x7 grid omits the tiny probability of either side scoring 7+.
    # Renormalization makes all result and market probabilities sum cleanly.
    total = sum(float(score["probability"]) for score in scores)
    for score in scores:
        score["probability"] = float(score["probability"]) / total
    return scores


def calculate_poisson_ratings_v1(
    home_rating: dict[str, Any],
    away_rating: dict[str, Any],
    home_player_rating: float | None = None,
    away_player_rating: float | None = None,
) -> dict[str, Any]:
    """Calculate transparent xG inputs and their Poisson-derived markets."""
    home_attack = _number(home_rating.get("attack_rating"), 50.0)
    away_attack = _number(away_rating.get("attack_rating"), 50.0)
    home_defense = _number(home_rating.get("defense_rating"), 50.0)
    away_defense = _number(away_rating.get("defense_rating"), 50.0)
    home_form = _number(home_rating.get("form_rating"), 50.0)
    away_form = _number(away_rating.get("form_rating"), 50.0)
    home_elo = _number(home_rating.get("elo_rating"), 1500.0)
    away_elo = _number(away_rating.get("elo_rating"), 1500.0)

    # Baseline goals are 1.35 per team. Attack raises a team's xG, opposing
    # defense suppresses it, form moves it by at most 10%, and Elo moves it by
    # at most 15%. A fixed 8% home advantage is applied only to the home side.
    home_xg = (
        1.35
        * (0.65 + home_attack / 100.0)
        * (1.35 - away_defense / 100.0)
        * (1.0 + (home_form - 50.0) / 500.0)
        * (1.0 + _clamp(home_elo - away_elo, -300.0, 300.0) / 2000.0)
        * 1.08
    )
    away_xg = (
        1.35
        * (0.65 + away_attack / 100.0)
        * (1.35 - home_defense / 100.0)
        * (1.0 + (away_form - 50.0) / 500.0)
        * (1.0 + _clamp(away_elo - home_elo, -300.0, 300.0) / 2000.0)
    )

    # Available player quality makes a deliberately small adjustment: an
    # average rating of 50 is neutral and the full adjustment is capped at 8%.
    if home_player_rating is not None:
        home_xg *= 1.0 + _clamp(home_player_rating - 50.0, -50.0, 50.0) / 625.0
    if away_player_rating is not None:
        away_xg *= 1.0 + _clamp(away_player_rating - 50.0, -50.0, 50.0) / 625.0

    home_xg = round(_clamp(home_xg, 0.2, 4.5), 4)
    away_xg = round(_clamp(away_xg, 0.2, 4.5), 4)
    scores = build_score_probabilities(home_xg, away_xg)
    home_win = sum(
        float(score["probability"])
        for score in scores
        if score["home_goals"] > score["away_goals"]
    )
    draw = sum(
        float(score["probability"])
        for score in scores
        if score["home_goals"] == score["away_goals"]
    )
    away_win = 1.0 - home_win - draw
    most_likely = max(scores, key=lambda score: float(score["probability"]))
    over_2_5 = sum(
        float(score["probability"])
        for score in scores
        if score["home_goals"] + score["away_goals"] >= 3
    )
    both_score = sum(
        float(score["probability"])
        for score in scores
        if score["home_goals"] > 0 and score["away_goals"] > 0
    )

    sample_size = min(
        _number(home_rating.get("matches_played")),
        _number(away_rating.get("matches_played")),
    )
    player_coverage = (
        int(home_player_rating is not None) + int(away_player_rating is not None)
    ) / 2.0
    # Confidence combines sample coverage, outcome separation, and optional
    # player coverage. It is descriptive model confidence, not win probability.
    confidence_score = (
        0.45 * min(1.0, sample_size / 10.0)
        + 0.40 * max(home_win, draw, away_win)
        + 0.15 * player_coverage
    )

    return {
        "home_xg": home_xg,
        "away_xg": away_xg,
        "home_win_probability": home_win,
        "draw_probability": draw,
        "away_win_probability": away_win,
        "most_likely_scoreline": (
            f"{most_likely['home_goals']}-{most_likely['away_goals']}"
        ),
        "expected_total_goals": round(home_xg + away_xg, 4),
        "over_2_5_probability": over_2_5,
        "both_teams_to_score_probability": both_score,
        "confidence_score": round(_clamp(confidence_score, 0.0, 1.0), 6),
        "score_probabilities": [
            {**score, "probability": round(float(score["probability"]), 12)}
            for score in scores
        ],
    }


def _normalize_probabilities(values: tuple[float, float, float]) -> tuple[float, float, float]:
    total = sum(values)
    if total <= 0:
        raise ValueError("Probabilities must have a positive sum")
    return tuple(value / total for value in values)


def _rating_difference(home: Any, away: Any, scale: float) -> float:
    if home is None or away is None:
        return 0.0
    return _clamp((_number(home) - _number(away)) / scale, -1.0, 1.0)


@dataclass(frozen=True)
class DrawCalibrationFeatures:
    elo_gap: float
    projected_total_goals: float
    home_attack_rating: float
    away_attack_rating: float
    home_defense_rating: float
    away_defense_rating: float
    shot_volume_gap: float = 0.0


def projected_total_goals_proxy(
    home_xg: float,
    away_xg: float,
    home_rating: dict[str, Any],
    away_rating: dict[str, Any],
) -> float:
    """Estimate the goal environment used only by draw calibration."""
    home_attack = _number(home_rating.get("attack_rating"), 50.0)
    away_attack = _number(away_rating.get("attack_rating"), 50.0)
    home_defense = _number(home_rating.get("defense_rating"), 50.0)
    away_defense = _number(away_rating.get("defense_rating"), 50.0)
    attack_pressure = ((home_attack + away_attack) / 2.0 - 50.0) / 55.0
    defensive_pressure = ((home_defense + away_defense) / 2.0 - 50.0) / 60.0
    edge_goals = abs(home_attack - away_attack) / 140.0
    multiplier = 1.0 + 0.28 * attack_pressure - 0.22 * defensive_pressure + edge_goals
    return round(_clamp((home_xg + away_xg) * multiplier, 1.45, 3.85), 4)


def calibrate_draw_probability(
    base_probabilities: tuple[float, float, float],
    features: DrawCalibrationFeatures,
) -> tuple[float, float, float]:
    """Spread draw risk by matchup shape while preserving side-win direction."""
    home, draw, away = _normalize_probabilities(base_probabilities)
    elo_edge = abs(features.elo_gap)
    attack_edge = abs(features.home_attack_rating - features.away_attack_rating)
    defense_average = (features.home_defense_rating + features.away_defense_rating) / 2.0
    attack_average = (features.home_attack_rating + features.away_attack_rating) / 2.0

    closeness = 1.0 - _clamp(elo_edge / 260.0, 0.0, 1.0)
    no_attack_edge = 1.0 - _clamp(attack_edge / 35.0, 0.0, 1.0)
    defensive_profile = _clamp((defense_average - 55.0) / 25.0, 0.0, 1.0)
    low_total = _clamp((2.70 - features.projected_total_goals) / 0.80, 0.0, 1.0)
    high_total = _clamp((features.projected_total_goals - 2.85) / 0.75, 0.0, 1.0)
    clear_edge = max(
        _clamp((elo_edge - 110.0) / 240.0, 0.0, 1.0),
        _clamp((attack_edge - 16.0) / 42.0, 0.0, 1.0),
        _clamp((abs(features.shot_volume_gap) - 25.0) / 130.0, 0.0, 1.0),
    )
    high_attack_environment = _clamp((attack_average - 62.0) / 28.0, 0.0, 1.0)

    boost = (
        DRAW_CLOSE_MATCH_BOOST_WEIGHT * closeness * no_attack_edge
        + DRAW_LOW_TOTAL_BOOST_WEIGHT
        * closeness
        * low_total
        * (0.55 + 0.45 * defensive_profile)
    )
    penalty = (
        DRAW_CLEAR_EDGE_PENALTY_WEIGHT * clear_edge
        + DRAW_HIGH_TOTAL_PENALTY_WEIGHT * high_total
        + DRAW_HIGH_ATTACK_PENALTY_WEIGHT
        * high_attack_environment
        * (1.0 - defensive_profile)
    )
    target_draw = _clamp(
        draw + boost - penalty,
        DRAW_MIN_PROBABILITY,
        DRAW_MAX_PROBABILITY,
    )
    side_total = home + away
    if side_total <= 0:
        return (0.5 * (1.0 - target_draw), target_draw, 0.5 * (1.0 - target_draw))
    remaining = 1.0 - target_draw
    return (remaining * home / side_total, target_draw, remaining * away / side_total)


def draw_probability_distribution(
    predictions: list[dict[str, Any]],
) -> dict[str, float | int]:
    draws = [float(prediction["draw_probability"]) for prediction in predictions]
    if not draws:
        return {
            "min": 0.0,
            "max": 0.0,
            "mean": 0.0,
            "standard_deviation": 0.0,
            "above_30_percent": 0,
        }
    mean = sum(draws) / len(draws)
    variance = sum((draw - mean) ** 2 for draw in draws) / len(draws)
    return {
        "min": min(draws),
        "max": max(draws),
        "mean": mean,
        "standard_deviation": math.sqrt(variance),
        "above_30_percent": sum(draw > 0.30 for draw in draws),
    }


def _calibrate_score_probabilities(
    scores: list[dict[str, float | int]],
    target: tuple[float, float, float],
) -> list[dict[str, float | int]]:
    current = (
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
    calibrated = []
    for score in scores:
        if score["home_goals"] > score["away_goals"]:
            outcome = 0
        elif score["home_goals"] == score["away_goals"]:
            outcome = 1
        else:
            outcome = 2
        calibrated.append(
            {
                **score,
                "probability": float(score["probability"])
                * target[outcome]
                / current[outcome],
            }
        )
    return calibrated


def _format_impact(value: float) -> str:
    return f"{value * 100:+.1f}%"


def _top_factors(
    elo_probabilities: tuple[float, float, float],
    attack_probabilities: tuple[float, float, float],
    attack_defense_probabilities: tuple[float, float, float],
    final_probabilities: tuple[float, float, float],
    home_team_name: str,
    away_team_name: str,
    shot_volume_impact: float = 0.0,
) -> list[dict[str, str]]:
    elo_home_impact = (elo_probabilities[0] - elo_probabilities[2]) / 2.0
    attack_impact = attack_probabilities[0] - elo_probabilities[0]
    defense_impact = (
        attack_defense_probabilities[0] - attack_probabilities[0]
    )
    draw_impact = final_probabilities[1] - attack_defense_probabilities[1]

    factors = [
        (
            abs(elo_home_impact),
            {
                "factor": "Elo advantage",
                "team": home_team_name if elo_home_impact >= 0 else away_team_name,
                "impact": _format_impact(abs(elo_home_impact)),
            },
        ),
        (
            abs(attack_impact),
            {
                "factor": "Attack rating",
                "team": (
                    home_team_name
                    if attack_impact >= 0
                    else away_team_name
                ),
                "impact": _format_impact(abs(attack_impact)),
            },
        ),
        (
            abs(defense_impact),
            {
                "factor": "Defense rating",
                "team": (
                    home_team_name
                    if defense_impact >= 0
                    else away_team_name
                ),
                "impact": _format_impact(abs(defense_impact)),
            },
        ),
        (
            abs(draw_impact),
            {
                "factor": "Draw calibration",
                "team": "Draw",
                "impact": _format_impact(draw_impact),
            },
        ),
    ]
    if abs(shot_volume_impact) >= SHOT_VOLUME_FACTOR_MINIMUM_IMPACT:
        factors.append(
            (
                abs(shot_volume_impact),
                {
                    "factor": "Shot volume",
                    "team": (
                        home_team_name
                        if shot_volume_impact >= 0
                        else away_team_name
                    ),
                    "impact": _format_impact(abs(shot_volume_impact)),
                },
            )
        )
    factors.sort(key=lambda factor: factor[0], reverse=True)
    return [factor for magnitude, factor in factors if magnitude > 1e-12]


def calculate_prediction(
    home_rating: dict[str, Any],
    away_rating: dict[str, Any],
    home_player_rating: float | None = None,
    away_player_rating: float | None = None,
    home_rest_days: int | None = None,
    away_rest_days: int | None = None,
    home_team_name: str = "Home",
    away_team_name: str = "Away",
    home_shot_volume_rating: float | None = None,
    away_shot_volume_rating: float | None = None,
) -> dict[str, Any]:
    """Calculate production v4.2.1 without a rest/context contribution.

    The rest arguments remain accepted so historical research scripts can
    replay older ablations, but production v4.2.1 intentionally ignores them.
    """
    home_elo = _number(home_rating.get("elo_rating"), 1500.0)
    away_elo = _number(away_rating.get("elo_rating"), 1500.0)
    elo_gap = home_elo - away_elo
    home_xg = _clamp(1.35 * math.exp(elo_gap / 800.0), 0.2, 4.5)
    away_xg = _clamp(1.35 * math.exp(-elo_gap / 800.0), 0.2, 4.5)
    base_scores = build_score_probabilities(home_xg, away_xg)
    elo_probabilities = (
        sum(
            float(score["probability"])
            for score in base_scores
            if score["home_goals"] > score["away_goals"]
        ),
        sum(
            float(score["probability"])
            for score in base_scores
            if score["home_goals"] == score["away_goals"]
        ),
        sum(
            float(score["probability"])
            for score in base_scores
            if score["home_goals"] < score["away_goals"]
        ),
    )

    attack_signal = _rating_difference(
        home_rating.get("attack_rating"),
        away_rating.get("attack_rating"),
        100.0,
    )
    defense_signal = _rating_difference(
        home_rating.get("defense_rating"),
        away_rating.get("defense_rating"),
        100.0,
    )
    attack_defense_tilt = (
        V3_ATTACK_WEIGHT * attack_signal
        + V3_DEFENSE_WEIGHT * defense_signal
    )
    attack_tilt = V3_ATTACK_WEIGHT * attack_signal
    attack_probabilities = _normalize_probabilities(
        (
            elo_probabilities[0] * math.exp(attack_tilt),
            elo_probabilities[1],
            elo_probabilities[2] * math.exp(-attack_tilt),
        )
    )
    attack_defense_probabilities = _normalize_probabilities(
        (
            elo_probabilities[0] * math.exp(attack_defense_tilt),
            elo_probabilities[1],
            elo_probabilities[2] * math.exp(-attack_defense_tilt),
        )
    )
    v3_probabilities = _normalize_probabilities(
        (
            elo_probabilities[0] * math.exp(attack_defense_tilt),
            elo_probabilities[1] * V3_DRAW_MULTIPLIER,
            elo_probabilities[2] * math.exp(-attack_defense_tilt),
        )
    )
    shot_volume_signal = _rating_difference(
        home_shot_volume_rating,
        away_shot_volume_rating,
        100.0,
    )
    draw_features = DrawCalibrationFeatures(
        elo_gap=elo_gap,
        projected_total_goals=projected_total_goals_proxy(
            home_xg,
            away_xg,
            home_rating,
            away_rating,
        ),
        home_attack_rating=_number(home_rating.get("attack_rating"), 50.0),
        away_attack_rating=_number(away_rating.get("attack_rating"), 50.0),
        home_defense_rating=_number(home_rating.get("defense_rating"), 50.0),
        away_defense_rating=_number(away_rating.get("defense_rating"), 50.0),
        shot_volume_gap=shot_volume_signal * 100.0,
    )
    shot_volume_tilt = shot_volume_signal * SHOT_VOLUME_WEIGHT
    shot_volume_probabilities = _normalize_probabilities(
        (
            attack_defense_probabilities[0] * math.exp(shot_volume_tilt),
            attack_defense_probabilities[1],
            attack_defense_probabilities[2] * math.exp(-shot_volume_tilt),
        )
    )
    probabilities = calibrate_draw_probability(
        shot_volume_probabilities,
        draw_features,
    )
    shot_volume_impact = shot_volume_probabilities[0] - attack_defense_probabilities[0]
    scores = _calibrate_score_probabilities(base_scores, probabilities)
    most_likely = max(scores, key=lambda score: float(score["probability"]))
    expected_total = sum(
        (int(score["home_goals"]) + int(score["away_goals"]))
        * float(score["probability"])
        for score in scores
    )
    over_2_5 = sum(
        float(score["probability"])
        for score in scores
        if int(score["home_goals"]) + int(score["away_goals"]) >= 3
    )
    both_score = sum(
        float(score["probability"])
        for score in scores
        if int(score["home_goals"]) > 0 and int(score["away_goals"]) > 0
    )
    confidence = calculate_confidence(
        elo_probabilities,
        probabilities,
        DataCompleteness(
            team_ratings=bool(
                home_rating.get("_team_rating_available", "elo_rating" in home_rating)
                and away_rating.get("_team_rating_available", "elo_rating" in away_rating)
            ),
            attack_defense_ratings=bool(
                home_rating.get(
                    "_attack_defense_available",
                    home_rating.get("attack_rating") is not None
                    and home_rating.get("defense_rating") is not None,
                )
                and away_rating.get(
                    "_attack_defense_available",
                    away_rating.get("attack_rating") is not None
                    and away_rating.get("defense_rating") is not None,
                )
            ),
            player_ratings=True,
            context=True,
        ),
    )
    top_factors = _top_factors(
        elo_probabilities,
        attack_probabilities,
        attack_defense_probabilities,
        probabilities,
        home_team_name,
        away_team_name,
        shot_volume_impact,
    )

    return {
        "home_xg": round(home_xg, 4),
        "away_xg": round(away_xg, 4),
        "elo_base_home_probability": elo_probabilities[0],
        "elo_base_draw_probability": elo_probabilities[1],
        "elo_base_away_probability": elo_probabilities[2],
        "attack_defense_adjustment": (
            attack_defense_probabilities[0] - elo_probabilities[0]
        ),
        "draw_calibration_adjustment": (
            probabilities[1] - shot_volume_probabilities[1]
        ),
        "context_adjustment_total": (
            attack_defense_probabilities[0] - elo_probabilities[0]
        ),
        "final_home_probability": probabilities[0],
        "final_draw_probability": probabilities[1],
        "final_away_probability": probabilities[2],
        "legacy_v41_draw_probability": v3_probabilities[1],
        "home_win_probability": probabilities[0],
        "draw_probability": probabilities[1],
        "away_win_probability": probabilities[2],
        "most_likely_scoreline": (
            f"{most_likely['home_goals']}-{most_likely['away_goals']}"
        ),
        "expected_total_goals": round(expected_total, 4),
        "over_2_5_probability": over_2_5,
        "both_teams_to_score_probability": both_score,
        **confidence,
        "top_factors": top_factors,
        "score_probabilities": [
            {**score, "probability": round(float(score["probability"]), 12)}
            for score in scores
        ],
    }


class PredictionRepository:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self.schema = None if engine.dialect.name == "sqlite" else "public"
        self.metadata = MetaData()
        self.tables: dict[str, Table] = {}

    def _table(self, name: str) -> Table:
        if name not in self.tables:
            self.tables[name] = Table(
                name,
                self.metadata,
                schema=self.schema,
                autoload_with=self.engine,
            )
        return self.tables[name]

    def assert_schema(self) -> None:
        inspector = inspect(self.engine)
        required_tables = {
            "matches",
            "teams",
            "team_ratings",
            "player_ratings",
            "predictions",
            "model_runs",
            "team_chance_quality_ratings",
        }
        existing = set(inspector.get_table_names(schema=self.schema))
        missing_tables = required_tables - existing
        if missing_tables:
            raise RuntimeError(f"Prediction pipeline tables are missing: {sorted(missing_tables)}")

        prediction_columns = {
            column["name"]
            for column in inspector.get_columns("predictions", schema=self.schema)
        }
        missing_columns = PREDICTION_REQUIRED_COLUMNS - prediction_columns
        if missing_columns:
            raise RuntimeError(
                f"predictions is missing columns {sorted(missing_columns)}. Apply "
                "supabase/migrations/202606100003_prediction_generation.sql and "
                "supabase/migrations/202606110001_prediction_explanations.sql and "
                "supabase/migrations/202606110002_confidence_v1.sql and "
                "supabase/migrations/202606110003_xg_proxy_v4.sql first."
            )

    def load_database_matches(self) -> list[dict[str, Any]]:
        matches = self._table("matches")
        with self.engine.connect() as connection:
            return [dict(row) for row in connection.execute(select(matches)).mappings()]

    def load_database_team_ids(self) -> dict[str, Any]:
        teams = self._table("teams")
        with self.engine.connect() as connection:
            rows = [dict(row) for row in connection.execute(select(teams)).mappings()]
        canonical_teams = load_teams()
        aliases = load_team_aliases()
        database_by_name: dict[str, list[Any]] = defaultdict(list)
        for row in rows:
            if row.get("name"):
                database_by_name[_normalize_name(row["name"])].append(row["id"])
        database_ids = {str(row["id"]): row["id"] for row in rows}
        mapping = {}
        for team in canonical_teams:
            direct = database_ids.get(team.id)
            candidates = {direct} if direct is not None else set()
            for name in (team.name, *aliases[team.id]):
                candidates.update(database_by_name.get(_normalize_name(name), []))
            if len(candidates) > 1:
                raise RuntimeError(
                    f"Canonical team {team.id} maps to multiple database teams: "
                    f"{sorted(str(candidate) for candidate in candidates)}"
                )
            mapping[team.id] = next(iter(candidates), None)

        mapped_ids = [
            str(team_id) for team_id in mapping.values() if team_id is not None
        ]
        collisions = [
            team_id
            for team_id, count in Counter(mapped_ids).items()
            if count > 1
        ]
        if collisions:
            raise RuntimeError(
                f"Database team IDs map to multiple canonical teams: {collisions}"
            )
        return mapping

    def load_current_team_ratings(
        self,
        database_team_ids: dict[str, Any],
    ) -> dict[str, dict[str, Any]]:
        ratings = self._table("team_ratings")
        statement = select(ratings)
        if "model_run_id" in ratings.c:
            statement = statement.where(ratings.c.model_run_id.is_(None))
        with self.engine.connect() as connection:
            rows = [dict(row) for row in connection.execute(statement).mappings()]
        rows.sort(
            key=lambda row: str(row.get("updated_at") or row.get("rated_at") or ""),
            reverse=True,
        )
        rating_by_database_id = {row["team_id"]: row for row in rows}
        canonical_ratings = {}
        for team in load_teams():
            database_rating = rating_by_database_id.get(database_team_ids.get(team.id))
            rating_source = (
                "database_current"
                if database_rating is not None
                else "canonical_rank_prior"
            )
            canonical_ratings[team.id] = {
                "team_id": team.id,
                "elo_rating": canonical_prior_elo(team.rank),
                "attack_rating": 50.0,
                "defense_rating": 50.0,
                "form_rating": 50.0,
                "matches_played": 0,
                **(database_rating or {}),
                "_rating_source": rating_source,
                "_database_team_id": database_team_ids.get(team.id),
                "_team_rating_available": database_rating is not None,
                "_attack_defense_available": bool(
                    database_rating
                    and database_rating.get("attack_rating") is not None
                    and database_rating.get("defense_rating") is not None
                ),
            }
        return canonical_ratings

    def load_player_team_averages(
        self,
        database_team_ids: dict[str, Any],
    ) -> dict[str, float]:
        ratings = self._table("player_ratings")
        statement = select(ratings)
        if "model_run_id" in ratings.c:
            statement = statement.where(ratings.c.model_run_id.is_(None))
        with self.engine.connect() as connection:
            rows = [dict(row) for row in connection.execute(statement).mappings()]
        by_team: dict[Any, list[float]] = defaultdict(list)
        for row in rows:
            if row.get("team_id") is not None and row.get("overall_rating") is not None:
                by_team[row["team_id"]].append(_number(row["overall_rating"]))
        database_averages = {
            team_id: sum(values) / len(values)
            for team_id, values in by_team.items()
            if values
        }
        return {
            canonical_id: database_averages[database_id]
            for canonical_id, database_id in database_team_ids.items()
            if database_id in database_averages
        }

    def load_current_shot_volume_ratings(
        self,
        database_team_ids: dict[str, Any],
    ) -> dict[str, float]:
        return {
            canonical_id: details["shot_volume_rating"]
            for canonical_id, details in self.load_current_shot_volume_details(
                database_team_ids
            ).items()
        }

    def load_current_shot_volume_details(
        self,
        database_team_ids: dict[str, Any],
    ) -> dict[str, dict[str, Any]]:
        ratings = self._table("team_chance_quality_ratings")
        selected_columns = [
            ratings.c.team_id,
            ratings.c.shot_volume_rating,
            ratings.c.rated_at,
        ]
        if "sample_matches" in ratings.c:
            selected_columns.append(ratings.c.sample_matches)
        statement = select(*selected_columns)
        if "model_version" in ratings.c:
            statement = statement.where(
                ratings.c.model_version == CHANCE_QUALITY_MODEL_VERSION
            )
        with self.engine.connect() as connection:
            rows = [dict(row) for row in connection.execute(statement).mappings()]
        rows.sort(key=lambda row: str(row.get("rated_at") or ""), reverse=True)
        by_database_id = {
            row["team_id"]: {
                "shot_volume_rating": _number(row["shot_volume_rating"]),
                "sample_matches": (
                    int(row["sample_matches"])
                    if row.get("sample_matches") is not None
                    else None
                ),
            }
            for row in rows
            if row.get("team_id") is not None
            and row.get("shot_volume_rating") is not None
        }
        return {
            canonical_id: by_database_id[database_id]
            for canonical_id, database_id in database_team_ids.items()
            if database_id in by_database_id
        }

    def load_latest_team_match_dates(
        self,
        database_team_ids: dict[str, Any],
    ) -> dict[str, date]:
        inspector = inspect(self.engine)
        existing = set(inspector.get_table_names(schema=self.schema))
        if not {"matches", "team_match_stats"}.issubset(existing):
            return {}
        matches = self._table("matches")
        stats = self._table("team_match_stats")
        date_column = next(
            (matches.c[name] for name in ("match_date", "kickoff") if name in matches.c),
            None,
        )
        if date_column is None or not {"match_id", "team_id"}.issubset(stats.c.keys()):
            return {}
        with self.engine.connect() as connection:
            rows = connection.execute(
                select(stats.c.team_id, date_column).join(
                    matches, matches.c.id == stats.c.match_id
                )
            ).tuples()
            latest: dict[Any, date] = {}
            for team_id, played_at in rows:
                timestamp = _parse_timestamp(played_at)
                if timestamp is None:
                    continue
                played_on = timestamp.date()
                if team_id not in latest or played_on > latest[team_id]:
                    latest[team_id] = played_on
        return {
            canonical_id: latest[database_id]
            for canonical_id, database_id in database_team_ids.items()
            if database_id in latest
        }

    def store_predictions(
        self,
        predictions: list[dict[str, Any]],
        generated_at: datetime,
    ) -> Any:
        runs = self._table("model_runs")
        prediction_table = self._table("predictions")
        run_id = str(uuid4())
        timestamp = generated_at.isoformat()
        run_values = {
            "id": run_id,
            "run_date": generated_at.date().isoformat(),
            "model_version": MODEL_VERSION,
            "notes": MODEL_DESCRIPTION,
            "data_cutoff": timestamp,
            "status": "completed",
            "random_seed": 0,
            "generated_at": timestamp,
            "metadata": {
                "matches_predicted": len(predictions),
                "score_grid": f"0-{MAX_GOALS}",
                "base_model": "walk-forward Elo probabilities",
                "attack_weight": V3_ATTACK_WEIGHT,
                "defense_weight": V3_DEFENSE_WEIGHT,
                "legacy_v41_draw_multiplier": V3_DRAW_MULTIPLIER,
                "draw_calibration": "v4.2.1 matchup-specific bounded additive calibration",
                "draw_min_probability": DRAW_MIN_PROBABILITY,
                "draw_max_probability": DRAW_MAX_PROBABILITY,
                "draw_close_match_boost_weight": DRAW_CLOSE_MATCH_BOOST_WEIGHT,
                "draw_low_total_boost_weight": DRAW_LOW_TOTAL_BOOST_WEIGHT,
                "draw_clear_edge_penalty_weight": DRAW_CLEAR_EDGE_PENALTY_WEIGHT,
                "draw_high_total_penalty_weight": DRAW_HIGH_TOTAL_PENALTY_WEIGHT,
                "draw_high_attack_penalty_weight": DRAW_HIGH_ATTACK_PENALTY_WEIGHT,
                "selected_ablation": PROMOTION_CONFIG["selected_ablation"],
                "shot_volume_weight": SHOT_VOLUME_WEIGHT,
                "xg_proxy_features_used": PROMOTION_CONFIG["features_used"],
                "xg_proxy_validation_brier": PROMOTION_CONFIG[
                    "validation_brier"
                ],
                "xg_proxy_validation_log_loss": PROMOTION_CONFIG[
                    "validation_log_loss"
                ],
                "recent_form_weight": 0.0,
                "player_weight": 0.0,
                "travel_weight": 0.0,
                "availability_weight": 0.0,
                "confidence_version": CONFIDENCE_VERSION,
                "confidence_medium_threshold": MEDIUM_THRESHOLD,
                "confidence_high_threshold": HIGH_THRESHOLD,
            },
        }
        run_values = self._compatible_values(runs, run_values)

        with self.engine.begin() as connection:
            if self.engine.dialect.name == "postgresql":
                connection.execute(
                    text("select pg_advisory_xact_lock(hashtext('prediction-generation'))")
                )
            connection.execute(runs.insert().values(**run_values))
            for prediction in predictions:
                self._upsert_prediction(
                    connection,
                    prediction_table,
                    prediction,
                    run_id,
                    timestamp,
                )
        return run_id

    def _upsert_prediction(
        self,
        connection: Connection,
        table: Table,
        prediction: dict[str, Any],
        run_id: Any,
        timestamp: str,
    ) -> None:
        values = {
            **prediction,
            "id": str(uuid4()),
            "canonical_match_id": prediction["canonical_match_id"],
            "match_id": prediction.get("database_match_id"),
            "model_run_id": run_id,
            "prediction_timestamp": timestamp,
            "model_version": MODEL_VERSION,
            "data_cutoff": timestamp,
            "created_at": timestamp,
            "updated_at": timestamp,
            "home_win": prediction["home_win_probability"],
            "draw": prediction["draw_probability"],
            "away_win": prediction["away_win_probability"],
            "home_win_prob": prediction["home_win_probability"],
            "draw_prob": prediction["draw_probability"],
            "away_win_prob": prediction["away_win_probability"],
            "explanation_factors": [
                "Elo result probabilities",
                "Validated attack and defense rating adjustments",
                "Validated draw calibration",
                "Validated shot-volume adjustment when ratings are available",
            ],
        }
        values = self._compatible_values(table, values)
        existing_ids = list(
            connection.execute(
                select(table.c.id).where(
                    table.c.canonical_match_id == prediction["canonical_match_id"]
                )
            ).scalars()
        )
        if existing_ids:
            values.pop("id", None)
            connection.execute(update(table).where(table.c.id == existing_ids[0]).values(**values))
            if len(existing_ids) > 1:
                connection.execute(
                    table.delete().where(table.c.id.in_(existing_ids[1:]))
                )
        else:
            connection.execute(table.insert().values(**values))

    @staticmethod
    def _compatible_values(table: Table, values: dict[str, Any]) -> dict[str, Any]:
        compatible = {}
        for key, value in values.items():
            if key not in table.c:
                continue
            if isinstance(value, (dict, list)) and not isinstance(table.c[key].type, JSON):
                value = json.dumps(value)
            compatible[key] = value
        return compatible


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logger = logging.getLogger("generate_predictions")
    env = load_environment()
    database_url = env.get("DATABASE_URL")
    if not database_url:
        logger.error("[prediction-generation] FAILED: DATABASE_URL is required")
        return 2

    try:
        engine = create_database_engine(database_url)
    except Exception:
        logger.exception("[prediction-generation] FAILED: could not initialize database")
        return 1

    try:
        logger.info("[prediction-generation] START model=%s", MODEL_VERSION)
        repository = PredictionRepository(engine)
        repository.assert_schema()
        generated_at = generation_time(env)
        database_team_ids = repository.load_database_team_ids()
        matches = load_canonical_future_matches(
            generated_at,
            repository.load_database_matches(),
            database_team_ids,
        )
        if not matches:
            logger.info("[prediction-generation] SUCCESS: no future matches found")
            return 0

        team_ratings = repository.load_current_team_ratings(database_team_ids)
        for team_id, rating in team_ratings.items():
            if rating["_rating_source"] == "canonical_rank_prior":
                reason = (
                    "database team mapping is missing"
                    if rating["_database_team_id"] is None
                    else "current database rating is missing"
                )
                logger.warning(
                    "Using conservative canonical rank prior for %s: %s",
                    team_id,
                    reason,
                )
        shot_volume_ratings = repository.load_current_shot_volume_ratings(
            database_team_ids
        )
        predictions = []
        legacy_draw_predictions = []
        team_names = {team.id: team.name for team in load_teams()}
        for match in matches:
            home_rating = team_ratings.get(match["home_team_id"])
            away_rating = team_ratings.get(match["away_team_id"])
            if home_rating is None or away_rating is None:
                logger.warning(
                    "Skipping match %s because current team ratings are unavailable",
                    match["id"],
                )
                continue
            prediction = calculate_prediction(
                home_rating,
                away_rating,
                home_team_name=team_names[match["home_team_id"]],
                away_team_name=team_names[match["away_team_id"]],
                home_shot_volume_rating=shot_volume_ratings.get(
                    match["home_team_id"]
                ),
                away_shot_volume_rating=shot_volume_ratings.get(
                    match["away_team_id"]
                ),
            )
            predictions.append(
                {
                    "canonical_match_id": match["canonical_match_id"],
                    "database_match_id": match["database_match_id"],
                    **prediction,
                }
            )
            legacy_draw_predictions.append(
                {"draw_probability": prediction["legacy_v41_draw_probability"]}
            )

        if not predictions:
            logger.info(
                "[prediction-generation] SUCCESS: no future matches have both team ratings"
            )
            return 0
        assert_complete_group_predictions(predictions)
        logger.info(
            "[prediction-generation] Legacy v4.1 draw distribution: %s",
            draw_probability_distribution(legacy_draw_predictions),
        )
        logger.info(
            "[prediction-generation] v4.2.1 draw distribution: %s",
            draw_probability_distribution(predictions),
        )
        run_id = repository.store_predictions(predictions, generated_at)
        logger.info(
            "[prediction-generation] SUCCESS: run=%s predictions=%d",
            run_id,
            len(predictions),
        )
        return 0
    except Exception:
        logger.exception("[prediction-generation] FAILED: unexpected generation error")
        return 1
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
