import random

from .domain import Standing


def build_table(team_ids: list[str], results: list[tuple[str, str, int, int]]) -> list[Standing]:
    table = {team_id: Standing(team_id=team_id, lots=random.Random(team_id).random()) for team_id in team_ids}
    for home_id, away_id, home_goals, away_goals in results:
        home, away = table[home_id], table[away_id]
        home.played += 1
        away.played += 1
        home.goals_for += home_goals
        home.goals_against += away_goals
        away.goals_for += away_goals
        away.goals_against += home_goals
        if home_goals > away_goals:
            home.wins += 1
            away.losses += 1
            home.points += 3
        elif home_goals < away_goals:
            away.wins += 1
            home.losses += 1
            away.points += 3
        else:
            home.draws += 1
            away.draws += 1
            home.points += 1
            away.points += 1
    return sorted(
        table.values(),
        key=lambda row: (
            row.points,
            row.goal_difference,
            row.goals_for,
            row.fair_play,
            row.lots,
        ),
        reverse=True,
    )


def rank_third_place(rows: list[Standing]) -> list[Standing]:
    return sorted(
        rows,
        key=lambda row: (
            row.points,
            row.goal_difference,
            row.goals_for,
            row.fair_play,
            row.lots,
        ),
        reverse=True,
    )

