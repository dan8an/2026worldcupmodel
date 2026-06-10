import json
import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, time, timezone
from pathlib import Path

from ..data import ROOT
from ..domain import MatchContext
from ..features.context import (
    ContextRepository,
    HistoricalResult,
    _competition_weight,
    load_historical_results,
)
from ..poisson import NEUTRAL_TEAM_XG, poisson_probability
from .metrics import ProbabilityVector, evaluate

REPORT_PATH = ROOT / "data" / "evaluation" / "latest.json"
EVALUATION_START_YEAR = 2022
HISTORICAL_HOME_ADVANTAGE = 75.0
ELO_INITIAL = 1500.0
ELO_SCALE = 400.0


@dataclass(frozen=True)
class EvaluationPrediction:
    played_on: str
    home_team_id: str
    away_team_id: str
    outcome: int
    equal: ProbabilityVector
    elo: ProbabilityVector
    context: ProbabilityVector


def _outcome(result: HistoricalResult) -> int:
    if result.home_score > result.away_score:
        return 0
    if result.home_score == result.away_score:
        return 1
    return 2


def _result_probabilities(home_xg: float, away_xg: float, max_goals: int = 10) -> ProbabilityVector:
    home_win = draw = away_win = 0.0
    for home_goals in range(max_goals + 1):
        for away_goals in range(max_goals + 1):
            probability = (
                poisson_probability(home_goals, home_xg)
                * poisson_probability(away_goals, away_xg)
            )
            if home_goals > away_goals:
                home_win += probability
            elif home_goals == away_goals:
                draw += probability
            else:
                away_win += probability
    total = home_win + draw + away_win
    return home_win / total, draw / total, away_win / total


def _poisson_from_gap(elo_gap: float) -> ProbabilityVector:
    home_xg = NEUTRAL_TEAM_XG * math.exp(elo_gap / 800)
    away_xg = NEUTRAL_TEAM_XG * math.exp(-elo_gap / 800)
    return _result_probabilities(home_xg, away_xg)


def _expected_result(home_rating: float, away_rating: float, advantage: float) -> float:
    return 1 / (1 + 10 ** ((away_rating - home_rating - advantage) / ELO_SCALE))


def _update_ratings(
    ratings: dict[str, float],
    result: HistoricalResult,
) -> None:
    home_rating = ratings[result.home_team_id]
    away_rating = ratings[result.away_team_id]
    advantage = 0.0 if result.neutral else HISTORICAL_HOME_ADVANTAGE
    expected = _expected_result(home_rating, away_rating, advantage)
    actual = 1.0 if result.home_score > result.away_score else 0.5 if result.home_score == result.away_score else 0.0
    margin = abs(result.home_score - result.away_score)
    margin_multiplier = 1.0 if margin <= 1 else math.log1p(margin)
    k_factor = 22 * _competition_weight(result.tournament) * margin_multiplier
    change = k_factor * (actual - expected)
    ratings[result.home_team_id] = home_rating + change
    ratings[result.away_team_id] = away_rating - change


def _context_without_availability(context: MatchContext) -> float:
    return (
        context.home_form_elo
        + context.home_h2h_elo
        - context.away_form_elo
        - context.away_h2h_elo
    )


def run_backtest(
    results: list[HistoricalResult] | None = None,
    start_year: int = EVALUATION_START_YEAR,
) -> dict:
    results = sorted(
        results if results is not None else load_historical_results(),
        key=lambda result: result.played_on,
    )
    ratings: dict[str, float] = defaultdict(lambda: ELO_INITIAL)
    match_counts: dict[str, int] = defaultdict(int)
    contexts = ContextRepository(results=results, reports=[], squads=[])
    predictions: list[EvaluationPrediction] = []

    results_by_date: dict = defaultdict(list)
    for result in results:
        results_by_date[result.played_on].append(result)

    for played_on in sorted(results_by_date):
        day_results = results_by_date[played_on]
        cutoff = datetime.combine(played_on, time.min, tzinfo=timezone.utc)
        if played_on.year >= start_year:
            for result in day_results:
                if (
                    match_counts[result.home_team_id] < 5
                    or match_counts[result.away_team_id] < 5
                ):
                    continue
                venue_advantage = 0.0 if result.neutral else HISTORICAL_HOME_ADVANTAGE
                elo_gap = (
                    ratings[result.home_team_id]
                    - ratings[result.away_team_id]
                    + venue_advantage
                )
                context = contexts.for_match(
                    result.home_team_id,
                    result.away_team_id,
                    cutoff,
                )
                predictions.append(
                    EvaluationPrediction(
                        played_on=played_on.isoformat(),
                        home_team_id=result.home_team_id,
                        away_team_id=result.away_team_id,
                        outcome=_outcome(result),
                        equal=(1 / 3, 1 / 3, 1 / 3),
                        elo=_poisson_from_gap(elo_gap),
                        context=_poisson_from_gap(
                            elo_gap + _context_without_availability(context)
                        ),
                    )
                )
        # Update only after every prediction for the date has been made.
        for result in day_results:
            _update_ratings(ratings, result)
            match_counts[result.home_team_id] += 1
            match_counts[result.away_team_id] += 1

    if not predictions:
        raise ValueError("No eligible matches were available for evaluation")

    outcomes = [prediction.outcome for prediction in predictions]
    models = ("equal", "elo", "context")
    aggregate = {
        model: evaluate(
            [getattr(prediction, model) for prediction in predictions],
            outcomes,
        )
        for model in models
    }
    years = {}
    for year in sorted({prediction.played_on[:4] for prediction in predictions}):
        indices = [
            index
            for index, prediction in enumerate(predictions)
            if prediction.played_on.startswith(year)
        ]
        years[year] = {
            model: evaluate(
                [getattr(predictions[index], model) for index in indices],
                [outcomes[index] for index in indices],
            )
            for model in models
        }

    context_improves_elo = (
        aggregate["context"]["log_loss"] < aggregate["elo"]["log_loss"]
        and aggregate["context"]["brier_score"] < aggregate["elo"]["brier_score"]
    )
    return {
        "protocol": {
            "start_year": start_year,
            "end_date": max(result.played_on for result in results).isoformat(),
            "minimum_prior_matches_per_team": 5,
            "same_day_updates": "batched_after_predictions",
            "rank_data": "excluded because current ranks would leak future information",
            "availability_data": "excluded because no historical point-in-time archive is available",
        },
        "aggregate": aggregate,
        "years": years,
        "promotion_gate": {
            "context_beats_elo_on_log_loss_and_brier": context_improves_elo,
            "status": "pass" if context_improves_elo else "fail",
        },
    }


def write_report(path: Path = REPORT_PATH) -> Path:
    report = run_backtest()
    report["generated_at"] = datetime.now(timezone.utc).isoformat()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2))
    return path


if __name__ == "__main__":
    print(write_report())
