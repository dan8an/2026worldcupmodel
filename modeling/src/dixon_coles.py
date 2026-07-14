"""Dixon--Coles low-score correction for independent Poisson score models."""

from __future__ import annotations

import math


def valid_rho_bounds(home_xg: float, away_xg: float) -> tuple[float, float]:
    """Return the open interval in which every Dixon--Coles tau is nonnegative."""
    lower = max(-1.0 / home_xg, -1.0 / away_xg)
    upper = min(1.0, 1.0 / (home_xg * away_xg))
    return lower, upper


def tau(home_goals: int, away_goals: int, home_xg: float, away_xg: float, rho: float) -> float:
    if home_goals == 0 and away_goals == 0:
        return 1.0 - home_xg * away_xg * rho
    if home_goals == 0 and away_goals == 1:
        return 1.0 + home_xg * rho
    if home_goals == 1 and away_goals == 0:
        return 1.0 + away_xg * rho
    if home_goals == 1 and away_goals == 1:
        return 1.0 - rho
    return 1.0


def score_matrix(home_xg: float, away_xg: float, rho: float, max_goals: int = 8) -> list[list[float]]:
    lower, upper = valid_rho_bounds(home_xg, away_xg)
    if not lower <= rho <= upper:
        raise ValueError(f"rho {rho} is outside valid bounds [{lower}, {upper}]")
    matrix = []
    for home_goals in range(max_goals + 1):
        row = []
        home_p = math.exp(-home_xg) * home_xg**home_goals / math.factorial(home_goals)
        for away_goals in range(max_goals + 1):
            away_p = math.exp(-away_xg) * away_xg**away_goals / math.factorial(away_goals)
            row.append(home_p * away_p * tau(home_goals, away_goals, home_xg, away_xg, rho))
        matrix.append(row)
    total = sum(map(sum, matrix))
    if total <= 0 or any(value < 0 for row in matrix for value in row):
        raise ValueError("Dixon--Coles produced an invalid score matrix")
    return [[value / total for value in row] for row in matrix]


def result_probabilities(home_xg: float, away_xg: float, rho: float, max_goals: int = 8) -> tuple[float, float, float]:
    matrix = score_matrix(home_xg, away_xg, rho, max_goals)
    home = sum(p for i, row in enumerate(matrix) for j, p in enumerate(row) if i > j)
    draw = sum(p for i, row in enumerate(matrix) for j, p in enumerate(row) if i == j)
    away = sum(p for i, row in enumerate(matrix) for j, p in enumerate(row) if i < j)
    total = home + draw + away
    return home / total, draw / total, away / total
