"""Leakage-safe opponent-adjusted expected-goal features for v4.4 research."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date

from .features.context import HistoricalResult


@dataclass
class TeamGoalHistory:
    attack: list[tuple[date, float]] = field(default_factory=list)
    defense_weakness: list[tuple[date, float]] = field(default_factory=list)


def weighted_shrunk(values: list[tuple[date, float]], cutoff: date, half_life_days: int, prior_matches: int = 8) -> float:
    eligible = [(played, value) for played, value in values if played < cutoff]
    if not eligible:
        return 0.0
    weights = [math.exp(-math.log(2) * (cutoff - played).days / half_life_days) for played, _ in eligible]
    weighted = sum(weight * value for weight, (_, value) in zip(weights, eligible)) / sum(weights)
    shrinkage = len(eligible) / (len(eligible) + prior_matches)
    return max(-0.8, min(0.8, weighted * shrinkage))


def opponent_adjusted_snapshot(histories: dict[str, TeamGoalHistory], team_id: str, cutoff: date) -> dict[str, float]:
    history = histories.setdefault(team_id, TeamGoalHistory())
    return {
        "long_attack": weighted_shrunk(history.attack, cutoff, 730),
        "long_defense_weakness": weighted_shrunk(history.defense_weakness, cutoff, 730),
        "short_attack": weighted_shrunk(history.attack, cutoff, 180),
        "short_defense_weakness": weighted_shrunk(history.defense_weakness, cutoff, 180),
    }


def update_histories_batched(histories: dict[str, TeamGoalHistory], results: list[HistoricalResult]) -> None:
    """Update after all same-date snapshots; residuals use only prior opponent state."""
    if not results:
        return
    played_on = results[0].played_on
    snapshots = {team: opponent_adjusted_snapshot(histories, team, played_on) for result in results for team in (result.home_team_id, result.away_team_id)}
    for result in results:
        home, away = snapshots[result.home_team_id], snapshots[result.away_team_id]
        home_attack = math.log((result.home_score + 0.35) / 1.35) - away["long_defense_weakness"]
        away_attack = math.log((result.away_score + 0.35) / 1.35) - home["long_defense_weakness"]
        home_defense = math.log((result.away_score + 0.35) / 1.35) - away["long_attack"]
        away_defense = math.log((result.home_score + 0.35) / 1.35) - home["long_attack"]
        histories.setdefault(result.home_team_id, TeamGoalHistory()).attack.append((played_on, max(-1.5, min(1.5, home_attack))))
        histories[result.home_team_id].defense_weakness.append((played_on, max(-1.5, min(1.5, home_defense))))
        histories.setdefault(result.away_team_id, TeamGoalHistory()).attack.append((played_on, max(-1.5, min(1.5, away_attack))))
        histories[result.away_team_id].defense_weakness.append((played_on, max(-1.5, min(1.5, away_defense))))


def competition_category(tournament: str) -> str:
    value = tournament.casefold()
    if "friendly" in value:
        return "friendly"
    if "qualification" in value or "qualifier" in value:
        return "qualification"
    if "nations league" in value:
        return "nations_league"
    if "world cup" in value:
        return "world_cup"
    return "continental_or_other_competitive"


def capped_margin(goals_for: int, goals_against: int) -> float:
    margin = goals_for - goals_against
    return math.copysign(min(2.0, math.log1p(abs(margin))), margin) if margin else 0.0


def amplify_goal_difference(home_xg: float, away_xg: float, factor: float) -> tuple[float, float]:
    if factor <= 0:
        raise ValueError("amplification factor must be positive")
    center = math.sqrt(home_xg * away_xg)
    half_gap = 0.5 * math.log(home_xg / away_xg) * factor
    return max(0.2, min(4.5, center * math.exp(half_gap))), max(0.2, min(4.5, center * math.exp(-half_gap)))


def opponent_adjusted_xg(home_xg: float, away_xg: float, home: dict[str, float], away: dict[str, float], weight: float = 0.25, short_weight: float = 0.0) -> tuple[float, float]:
    home_attack = (1 - short_weight) * home["long_attack"] + short_weight * home["short_attack"]
    away_attack = (1 - short_weight) * away["long_attack"] + short_weight * away["short_attack"]
    home_defense = (1 - short_weight) * home["long_defense_weakness"] + short_weight * home["short_defense_weakness"]
    away_defense = (1 - short_weight) * away["long_defense_weakness"] + short_weight * away["short_defense_weakness"]
    return max(0.2, min(4.5, home_xg * math.exp(weight * (home_attack + away_defense)))), max(0.2, min(4.5, away_xg * math.exp(weight * (away_attack + home_defense))))


@dataclass(frozen=True)
class PoissonGoalModel:
    coefficients: tuple[float, ...]
    feature_names: tuple[str, ...]
    ridge: float

    def predict(self, features: tuple[float, ...]) -> float:
        if len(features) != len(self.coefficients):
            raise ValueError("feature length mismatch")
        return max(0.2, min(4.5, math.exp(sum(c * x for c, x in zip(self.coefficients, features)))))

    def to_dict(self) -> dict:
        return {"type": "ridge_poisson_glm", "coefficients": self.coefficients, "feature_names": self.feature_names, "ridge": self.ridge}

    @classmethod
    def from_dict(cls, payload: dict) -> "PoissonGoalModel":
        return cls(tuple(float(v) for v in payload["coefficients"]), tuple(payload["feature_names"]), float(payload["ridge"]))


def fit_poisson(features: list[tuple[float, ...]], goals: list[int], feature_names: tuple[str, ...], ridge: float = 1.0, iterations: int = 4000, learning_rate: float = 0.01) -> PoissonGoalModel:
    coefficients = [math.log(max(0.2, sum(goals) / len(goals)))] + [0.0] * (len(feature_names) - 1)
    for iteration in range(iterations):
        gradient = [0.0] * len(coefficients)
        for row, goal in zip(features, goals):
            expected = min(10.0, math.exp(max(-5.0, min(3.0, sum(c * x for c, x in zip(coefficients, row))))))
            for index, value in enumerate(row):
                gradient[index] += (expected - goal) * value / len(goals)
        rate = learning_rate / math.sqrt(1 + iteration / 500)
        for index in range(len(coefficients)):
            penalty = 0 if index == 0 else ridge * coefficients[index] / len(goals)
            coefficients[index] -= rate * (gradient[index] + penalty)
    return PoissonGoalModel(tuple(coefficients), feature_names, ridge)
