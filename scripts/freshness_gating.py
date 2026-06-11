from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from datetime import date
from typing import Any

from scripts.generate_predictions import calculate_prediction

EXPERIMENTAL_MODEL_VERSION = "elo-context-v4-freshness-gated"
FRESHNESS_GRACE_DAYS = 90
FRESHNESS_HALF_LIFE_DAYS = 180
SAMPLE_PRIOR_MATCHES = 10
NEUTRAL_RATING_PRIOR = 1500.0
NEUTRAL_FEATURE_PRIOR = 50.0


@dataclass(frozen=True)
class FeatureReliability:
    age_days: int | None
    sample_matches: int
    freshness: float
    sample_size: float
    combined: float


@dataclass(frozen=True)
class TeamReliability:
    elo: FeatureReliability
    attack_defense: FeatureReliability
    shot_volume: FeatureReliability
    rest_context: float


def freshness_score(age_days: int | None) -> float:
    if age_days is None:
        return 0.0
    age_days = max(0, age_days)
    if age_days <= FRESHNESS_GRACE_DAYS:
        return 1.0
    decay_days = age_days - FRESHNESS_GRACE_DAYS
    return math.exp(
        -math.log(2) * decay_days / FRESHNESS_HALF_LIFE_DAYS
    )


def sample_size_score(
    sample_matches: int | None,
    prior_matches: int = SAMPLE_PRIOR_MATCHES,
) -> float:
    sample = max(0, int(sample_matches or 0))
    return sample / (sample + prior_matches) if sample else 0.0


def feature_reliability(
    age_days: int | None,
    sample_matches: int | None,
) -> FeatureReliability:
    freshness = freshness_score(age_days)
    sample_size = sample_size_score(sample_matches)
    return FeatureReliability(
        age_days=age_days,
        sample_matches=max(0, int(sample_matches or 0)),
        freshness=round(freshness, 6),
        sample_size=round(sample_size, 6),
        combined=round(math.sqrt(freshness * sample_size), 6),
    )


def shrink(value: float, prior: float, reliability: float) -> float:
    reliability = max(0.0, min(1.0, reliability))
    return prior + reliability * (value - prior)


def team_reliability(
    rating_age_days: int | None,
    rating_sample_matches: int | None,
    shot_volume_age_days: int | None,
    shot_volume_sample_matches: int | None,
) -> TeamReliability:
    elo = feature_reliability(rating_age_days, rating_sample_matches)
    attack_defense = feature_reliability(
        rating_age_days, rating_sample_matches
    )
    shot_volume = feature_reliability(
        shot_volume_age_days, shot_volume_sample_matches
    )
    return TeamReliability(
        elo=elo,
        attack_defense=attack_defense,
        shot_volume=shot_volume,
        rest_context=round(freshness_score(rating_age_days), 6),
    )


def _adjust_rating(
    rating: dict[str, Any],
    reliability: TeamReliability,
    canonical_elo_prior: float,
    gate_elo: bool,
    gate_attack_defense: bool,
) -> dict[str, Any]:
    adjusted = dict(rating)
    if gate_elo:
        adjusted["elo_rating"] = shrink(
            float(rating.get("elo_rating", NEUTRAL_RATING_PRIOR)),
            canonical_elo_prior,
            reliability.elo.combined,
        )
    if gate_attack_defense:
        adjusted["attack_rating"] = shrink(
            float(rating.get("attack_rating", NEUTRAL_FEATURE_PRIOR)),
            NEUTRAL_FEATURE_PRIOR,
            reliability.attack_defense.combined,
        )
        adjusted["defense_rating"] = shrink(
            float(rating.get("defense_rating", NEUTRAL_FEATURE_PRIOR)),
            NEUTRAL_FEATURE_PRIOR,
            reliability.attack_defense.combined,
        )
    return adjusted


def _scaled_rest_days(
    home_rest_days: int | None,
    away_rest_days: int | None,
    reliability: float,
) -> tuple[float | None, float | None]:
    if home_rest_days is None or away_rest_days is None:
        return None, None
    meaningful_difference = max(
        -14.0, min(14.0, home_rest_days - away_rest_days)
    )
    difference = meaningful_difference * reliability
    return difference / 2.0, -difference / 2.0


def calculate_freshness_gated_prediction(
    home_rating: dict[str, Any],
    away_rating: dict[str, Any],
    *,
    home_canonical_elo_prior: float = NEUTRAL_RATING_PRIOR,
    away_canonical_elo_prior: float = NEUTRAL_RATING_PRIOR,
    home_rating_age_days: int | None,
    away_rating_age_days: int | None,
    home_shot_volume_age_days: int | None = None,
    away_shot_volume_age_days: int | None = None,
    home_shot_volume_rating: float | None = None,
    away_shot_volume_rating: float | None = None,
    home_shot_volume_sample: int | None = None,
    away_shot_volume_sample: int | None = None,
    home_rest_days: int | None = None,
    away_rest_days: int | None = None,
    home_team_name: str = "Home",
    away_team_name: str = "Away",
    gate_elo: bool = True,
    gate_attack_defense: bool = True,
    gate_rest: bool = True,
    gate_shot_volume: bool = True,
) -> dict[str, Any]:
    home_reliability = team_reliability(
        home_rating_age_days,
        int(home_rating.get("matches_played") or 0),
        home_shot_volume_age_days,
        home_shot_volume_sample,
    )
    away_reliability = team_reliability(
        away_rating_age_days,
        int(away_rating.get("matches_played") or 0),
        away_shot_volume_age_days,
        away_shot_volume_sample,
    )
    adjusted_home = _adjust_rating(
        home_rating,
        home_reliability,
        home_canonical_elo_prior,
        gate_elo,
        gate_attack_defense,
    )
    adjusted_away = _adjust_rating(
        away_rating,
        away_reliability,
        away_canonical_elo_prior,
        gate_elo,
        gate_attack_defense,
    )
    rest_reliability = (
        min(
            home_reliability.rest_context,
            away_reliability.rest_context,
        )
        if gate_rest
        else 1.0
    )
    effective_home_rest, effective_away_rest = _scaled_rest_days(
        home_rest_days,
        away_rest_days,
        rest_reliability,
    )
    adjusted_home_volume = home_shot_volume_rating
    adjusted_away_volume = away_shot_volume_rating
    if gate_shot_volume:
        if home_shot_volume_rating is not None:
            adjusted_home_volume = shrink(
                home_shot_volume_rating,
                NEUTRAL_FEATURE_PRIOR,
                home_reliability.shot_volume.combined,
            )
        if away_shot_volume_rating is not None:
            adjusted_away_volume = shrink(
                away_shot_volume_rating,
                NEUTRAL_FEATURE_PRIOR,
                away_reliability.shot_volume.combined,
            )
    prediction = calculate_prediction(
        adjusted_home,
        adjusted_away,
        home_rest_days=effective_home_rest,
        away_rest_days=effective_away_rest,
        home_team_name=home_team_name,
        away_team_name=away_team_name,
        home_shot_volume_rating=adjusted_home_volume,
        away_shot_volume_rating=adjusted_away_volume,
    )
    return {
        **prediction,
        "experimental_model_version": EXPERIMENTAL_MODEL_VERSION,
        "input_reliability": {
            "home": {
                **asdict(home_reliability),
                "canonical_elo_prior": home_canonical_elo_prior,
                "effective_elo": adjusted_home["elo_rating"],
                "effective_attack_rating": adjusted_home.get("attack_rating"),
                "effective_defense_rating": adjusted_home.get("defense_rating"),
                "effective_shot_volume_rating": adjusted_home_volume,
            },
            "away": {
                **asdict(away_reliability),
                "canonical_elo_prior": away_canonical_elo_prior,
                "effective_elo": adjusted_away["elo_rating"],
                "effective_attack_rating": adjusted_away.get("attack_rating"),
                "effective_defense_rating": adjusted_away.get("defense_rating"),
                "effective_shot_volume_rating": adjusted_away_volume,
            },
            "rest_context_reliability": rest_reliability,
            "gates": {
                "elo": gate_elo,
                "attack_defense": gate_attack_defense,
                "rest": gate_rest,
                "shot_volume": gate_shot_volume,
            },
        },
    }
