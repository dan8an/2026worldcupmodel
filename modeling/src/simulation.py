from collections import Counter, defaultdict
from dataclasses import asdict
import math
import random
from datetime import datetime, timezone

from .data import build_fixtures, load_teams
from .domain import MatchPrediction, Standing, Team
from .features.context import ContextRepository
from .poisson import predict_match
from .standings import build_table, rank_third_place

STAGES = ("round_of_32", "round_of_16", "quarterfinal", "semifinal", "final", "champion")
PUBLISHED_SIMULATION_ITERATIONS = 50000


def sample_poisson(expected: float, rng: random.Random) -> int:
    threshold = math.exp(-expected)
    product = 1.0
    count = 0
    while product > threshold:
        count += 1
        product *= rng.random()
    return count - 1


def sample_score(prediction: MatchPrediction, rng: random.Random) -> tuple[int, int]:
    return sample_poisson(prediction.home_xg, rng), sample_poisson(prediction.away_xg, rng)


def _knockout_winner(
    home: Team,
    away: Team,
    rng: random.Random,
    contexts: ContextRepository,
    cutoff: datetime,
    prediction_cache: dict[tuple[str, str], MatchPrediction],
) -> Team:
    prediction = _cached_prediction(
        home, away, contexts, cutoff, prediction_cache
    )
    home_goals, away_goals = sample_score(prediction, rng)
    if home_goals != away_goals:
        return home if home_goals > away_goals else away
    home_strength = 1 / (1 + 10 ** ((away.elo - home.elo) / 400))
    return home if rng.random() < home_strength else away


def _round_of_32_teams(
    group_tables: dict[str, list[Standing]], teams_by_id: dict[str, Team]
) -> list[Team]:
    automatic: list[Team] = []
    third_rows: list[Standing] = []
    for group in "ABCDEFGHIJKL":
        automatic.extend(teams_by_id[row.team_id] for row in group_tables[group][:2])
        third_rows.append(group_tables[group][2])
    best_thirds = rank_third_place(third_rows)[:8]
    automatic.extend(teams_by_id[row.team_id] for row in best_thirds)
    # The seed ordering is deterministic and keeps winners separated. The exact
    # Annex C slot mapping can replace this function without changing callers.
    return sorted(automatic, key=lambda team: (-team.elo, team.id))


def simulate_once(
    teams: list[Team],
    rng: random.Random,
    contexts: ContextRepository,
    cutoff: datetime,
    prediction_cache: dict[tuple[str, str], MatchPrediction],
) -> dict[str, set[str] | str]:
    teams_by_id = {team.id: team for team in teams}
    fixtures = [match for match in build_fixtures(teams) if match.stage == "group"]
    results_by_group: dict[str, list[tuple[str, str, int, int]]] = defaultdict(list)
    for match in fixtures:
        home = teams_by_id[match.home_team_id or ""]
        away = teams_by_id[match.away_team_id or ""]
        prediction = _cached_prediction(
            home, away, contexts, cutoff, prediction_cache
        )
        home_goals, away_goals = sample_score(prediction, rng)
        results_by_group[match.group or ""].append((home.id, away.id, home_goals, away_goals))
    group_tables = {
        group: build_table(
            [team.id for team in teams if team.group == group],
            results_by_group[group],
        )
        for group in "ABCDEFGHIJKL"
    }
    current = _round_of_32_teams(group_tables, teams_by_id)
    reached: dict[str, set[str] | str] = {"round_of_32": {team.id for team in current}}
    for next_stage in ("round_of_16", "quarterfinal", "semifinal", "final"):
        current = [
            _knockout_winner(
                current[index],
                current[index + 1],
                rng,
                contexts,
                cutoff,
                prediction_cache,
            )
            for index in range(0, len(current), 2)
        ]
        reached[next_stage] = {team.id for team in current}
    champion = _knockout_winner(
        current[0], current[1], rng, contexts, cutoff, prediction_cache
    )
    reached["champion"] = champion.id
    return reached


def simulate_tournament(
    iterations: int = PUBLISHED_SIMULATION_ITERATIONS,
    seed: int = 2026,
    context_repository: ContextRepository | None = None,
    cutoff: datetime | None = None,
) -> dict:
    if iterations < 1 or iterations > 50000:
        raise ValueError("iterations must be between 1 and 50000")
    teams = load_teams()
    rng = random.Random(seed)
    contexts = context_repository or ContextRepository()
    cutoff = cutoff or datetime.now(timezone.utc)
    prediction_cache: dict[tuple[str, str], MatchPrediction] = {}
    counts = {stage: Counter() for stage in STAGES}
    for _ in range(iterations):
        result = simulate_once(teams, rng, contexts, cutoff, prediction_cache)
        for stage in STAGES[:-1]:
            counts[stage].update(result[stage])
        counts["champion"].update([result["champion"]])
    rows = []
    for team in teams:
        rows.append(
            {
                "team_id": team.id,
                "team_name": team.name,
                **{
                    stage: round(counts[stage][team.id] / iterations, 6)
                    for stage in STAGES
                },
            }
        )
    rows.sort(key=lambda row: row["champion"], reverse=True)
    worst_case_standard_error = math.sqrt(0.25 / iterations)
    return {
        "iterations": iterations,
        "seed": seed,
        "monte_carlo_precision": {
            "worst_case_standard_error": round(worst_case_standard_error, 6),
            "worst_case_95_margin": round(1.96 * worst_case_standard_error, 6),
        },
        "teams": rows,
    }


def _cached_prediction(
    home: Team,
    away: Team,
    contexts: ContextRepository,
    cutoff: datetime,
    cache: dict[tuple[str, str], MatchPrediction],
) -> MatchPrediction:
    key = (home.id, away.id)
    if key not in cache:
        context = contexts.for_match(home.id, away.id, cutoff)
        cache[key] = predict_match(home, away, "simulation", context=context)
    return cache[key]


def prediction_dict(prediction: MatchPrediction) -> dict:
    payload = asdict(prediction)
    context = payload.pop("context")
    if context["data_cutoff"] is not None:
        context["data_cutoff"] = context["data_cutoff"].isoformat()
    payload["context"] = context
    payload["probabilities"] = {
        "home_win": payload.pop("home_win"),
        "draw": payload.pop("draw"),
        "away_win": payload.pop("away_win"),
    }
    return payload
