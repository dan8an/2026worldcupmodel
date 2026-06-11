from __future__ import annotations

import math
from typing import Iterable

from modeling.src.evaluation.metrics import ProbabilityVector


def decimal_odds_to_implied_probability(decimal_odds: float) -> float:
    try:
        odds = float(decimal_odds)
    except (TypeError, ValueError) as error:
        raise ValueError("Decimal odds must be numeric") from error
    if not math.isfinite(odds) or odds <= 1.0:
        raise ValueError("Decimal odds must be finite and greater than 1")
    return 1.0 / odds


def normalize_1x2_probabilities(
    probabilities: Iterable[float],
) -> ProbabilityVector:
    values = tuple(float(value) for value in probabilities)
    if len(values) != 3:
        raise ValueError("1X2 probabilities require home, draw, and away values")
    if any(not math.isfinite(value) or value < 0 for value in values):
        raise ValueError("Probabilities must be finite and non-negative")
    total = sum(values)
    if total <= 0:
        raise ValueError("Probability total must be positive")
    home = values[0] / total
    draw = values[1] / total
    return home, draw, 1.0 - home - draw


def remove_bookmaker_margin(
    raw_probabilities: Iterable[float],
) -> ProbabilityVector:
    return normalize_1x2_probabilities(raw_probabilities)


def odds_to_market_probabilities(
    home_decimal_odds: float,
    draw_decimal_odds: float,
    away_decimal_odds: float,
) -> dict[str, float | ProbabilityVector]:
    raw: ProbabilityVector = (
        decimal_odds_to_implied_probability(home_decimal_odds),
        decimal_odds_to_implied_probability(draw_decimal_odds),
        decimal_odds_to_implied_probability(away_decimal_odds),
    )
    overround = sum(raw)
    return {
        "raw": raw,
        "devigged": remove_bookmaker_margin(raw),
        "overround": overround,
        "margin": overround - 1.0,
    }


def average_absolute_disagreement(
    model: ProbabilityVector,
    market: ProbabilityVector,
) -> float:
    return sum(abs(model[index] - market[index]) for index in range(3)) / 3.0


def disagreement_bucket(disagreement: float) -> str:
    if disagreement < 0.02:
        return "0-2%"
    if disagreement < 0.05:
        return "2-5%"
    if disagreement < 0.10:
        return "5-10%"
    return "10%+"
