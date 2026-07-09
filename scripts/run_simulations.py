#!/usr/bin/env python3
from __future__ import annotations

import argparse
import bisect
import json
import logging
import math
import os
import random
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from dotenv import load_dotenv
from sqlalchemy import JSON, MetaData, Table, inspect, select, text
from sqlalchemy.engine import Engine

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from modeling.src.data import build_fixtures, load_teams, validate_tournament
from scripts.database import create_database_engine
from scripts.generate_predictions import (
    PredictionRepository,
    calculate_prediction,
)

DEFAULT_SIMULATIONS = 50_000
DEFAULT_SEED = 2026
STAGES = (
    "group_stage_exit",
    "round_of_32",
    "round_of_16",
    "quarterfinal",
    "semifinal",
    "final",
    "champion",
)
COMPLETED_MATCH_STATUSES = {"completed", "finished", "ft", "aet", "pen"}
KNOCKOUT_STAGES = (
    "round_of_32",
    "round_of_16",
    "quarterfinal",
    "semifinal",
    "final",
)


@dataclass(frozen=True)
class MatchState:
    id: str
    stage: str
    home_team_id: str
    away_team_id: str
    completed: bool
    home_score: int | None = None
    away_score: int | None = None
    home_penalty_score: int | None = None
    away_penalty_score: int | None = None
    match_number: int | None = None
    kickoff: datetime | None = None


def load_environment() -> dict[str, str]:
    """Load server-side env files without replacing exported values."""
    load_dotenv(ROOT / ".env", override=False)
    load_dotenv(ROOT / "backend" / ".env", override=False)
    return dict(os.environ)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the canonical 2026 World Cup Monte Carlo simulation."
    )
    parser.add_argument(
        "--simulations",
        type=int,
        default=DEFAULT_SIMULATIONS,
        help=f"Number of tournaments to simulate (default: {DEFAULT_SIMULATIONS}).",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=DEFAULT_SEED,
        help=f"Deterministic random seed (default: {DEFAULT_SEED}).",
    )
    return parser.parse_args()


def _number(value: Any, default: float = 0.0) -> float:
    if value is None:
        return default
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _json_value(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return None
    return value


def _integer(value: Any) -> int | None:
    if value is None:
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number


def _parse_timestamp(value: Any) -> datetime | None:
    if value is None or isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _stage_from_value(value: Any) -> str:
    raw = str(value or "").lower()
    if "round of 32" in raw or "round_of_32" in raw:
        return "round_of_32"
    if "round of 16" in raw or "round_of_16" in raw:
        return "round_of_16"
    if "quarter" in raw:
        return "quarterfinal"
    if "semi" in raw:
        return "semifinal"
    if "third" in raw or "3rd" in raw:
        return "third_place"
    if "final" in raw:
        return "final"
    return "group" if "group" in raw else raw


def _is_completed_match(row: dict[str, Any]) -> bool:
    status = str(row.get("status") or "").strip().lower()
    return bool(row.get("completed")) or status in COMPLETED_MATCH_STATUSES


def _penalty_scores(row: dict[str, Any]) -> tuple[int | None, int | None]:
    home = _integer(row.get("home_penalty_score"))
    away = _integer(row.get("away_penalty_score"))
    if home is not None or away is not None:
        return home, away
    payload = _json_value(row.get("provider_payload")) or _json_value(row.get("raw")) or {}
    if isinstance(payload, dict):
        penalty = (
            payload.get("score", {}).get("penalty", {})
            if isinstance(payload.get("score"), dict)
            else {}
        )
        if isinstance(penalty, dict):
            return _integer(penalty.get("home")), _integer(penalty.get("away"))
    return None, None


def _completed_winner(match: MatchState) -> str:
    if match.home_score is None or match.away_score is None:
        raise ValueError(f"Completed fixture {match.id} is missing a final score")
    if match.home_score > match.away_score:
        return match.home_team_id
    if match.away_score > match.home_score:
        return match.away_team_id
    if (
        match.home_penalty_score is not None
        and match.away_penalty_score is not None
        and match.home_penalty_score != match.away_penalty_score
    ):
        return (
            match.home_team_id
            if match.home_penalty_score > match.away_penalty_score
            else match.away_team_id
        )
    raise ValueError(
        f"Completed knockout fixture {match.id} is tied without penalty winner data"
    )


def _cumulative_weights(weights: list[float]) -> list[float]:
    cumulative = 0.0
    values = []
    for weight in weights:
        cumulative += weight
        values.append(cumulative)
    return values


def _score_grid(prediction: dict[str, Any]) -> list[tuple[int, int, float]]:
    payload = _json_value(prediction.get("score_probabilities"))
    if isinstance(payload, list) and payload:
        scores = [
            (
                int(row["home_goals"]),
                int(row["away_goals"]),
                _number(row["probability"]),
            )
            for row in payload
        ]
        if sum(score[2] for score in scores) > 0:
            return scores

    home_xg = max(0.05, _number(prediction.get("home_xg"), 1.35))
    away_xg = max(0.05, _number(prediction.get("away_xg"), 1.15))
    scores = []
    for home_goals in range(7):
        for away_goals in range(7):
            probability = (
                math.exp(-home_xg)
                * home_xg**home_goals
                / math.factorial(home_goals)
                * math.exp(-away_xg)
                * away_xg**away_goals
                / math.factorial(away_goals)
            )
            scores.append((home_goals, away_goals, probability))
    return scores


def sample_group_score(
    sampler: tuple[list[tuple[int, int]], list[float]],
    rng: random.Random,
) -> tuple[int, int]:
    scores, cumulative = sampler
    index = bisect.bisect_left(cumulative, rng.random() * cumulative[-1])
    return scores[min(index, len(scores) - 1)]


def compile_score_sampler(
    prediction: dict[str, Any],
) -> tuple[list[tuple[int, int]], list[float]]:
    grid = _score_grid(prediction)
    return (
        [(home_goals, away_goals) for home_goals, away_goals, _ in grid],
        _cumulative_weights([probability for _, _, probability in grid]),
    )


def rank_group(
    team_ids: list[str],
    results: list[tuple[str, str, int, int]],
) -> list[dict[str, Any]]:
    rows = {
        team_id: {
            "team_id": team_id,
            "points": 0,
            "goals_for": 0,
            "goals_against": 0,
        }
        for team_id in team_ids
    }
    for home_id, away_id, home_goals, away_goals in results:
        home = rows[home_id]
        away = rows[away_id]
        home["goals_for"] += home_goals
        home["goals_against"] += away_goals
        away["goals_for"] += away_goals
        away["goals_against"] += home_goals
        if home_goals > away_goals:
            home["points"] += 3
        elif away_goals > home_goals:
            away["points"] += 3
        else:
            home["points"] += 1
            away["points"] += 1
    for row in rows.values():
        row["goal_difference"] = row["goals_for"] - row["goals_against"]

    # Requested FIFA order, followed by canonical team ID as a stable fallback.
    return sorted(
        rows.values(),
        key=lambda row: (
            -row["points"],
            -row["goal_difference"],
            -row["goals_for"],
            row["team_id"],
        ),
    )


def _qualification_key(row: dict[str, Any]) -> tuple[int, int, int, str]:
    return (
        -row["points"],
        -row["goal_difference"],
        -row["goals_for"],
        row["team_id"],
    )


def build_round_of_32(
    group_tables: dict[str, list[dict[str, Any]]],
    team_groups: dict[str, str],
) -> list[tuple[str, str]]:
    winners = [group_tables[group][0] for group in "ABCDEFGHIJKL"]
    runners = [group_tables[group][1] for group in "ABCDEFGHIJKL"]
    best_thirds = sorted(
        (group_tables[group][2] for group in "ABCDEFGHIJKL"),
        key=_qualification_key,
    )[:8]

    # The 48-team format needs eight third-place qualifiers in addition to the
    # top two from each group. Seed 12 winners plus the four best runners
    # against the remaining runners and best thirds, avoiding group rematches.
    ranked_runners = sorted(runners, key=_qualification_key)
    seeded = winners + ranked_runners[:4]
    unseeded = ranked_runners[4:] + best_thirds
    pairings = []
    for seeded_team in seeded:
        opponent_index = next(
            (
                index
                for index, opponent in enumerate(unseeded)
                if team_groups[opponent["team_id"]]
                != team_groups[seeded_team["team_id"]]
            ),
            0,
        )
        opponent = unseeded.pop(opponent_index)
        pairings.append((seeded_team["team_id"], opponent["team_id"]))
    return pairings


KnockoutPrediction = Callable[[str, str], dict[str, Any]]


def build_knockout_prediction_provider(
    team_ratings: dict[str, dict[str, Any]],
    shot_volume_ratings: dict[str, float] | None = None,
) -> KnockoutPrediction:
    shot_volume_ratings = shot_volume_ratings or {}
    team_names = {team.id: team.name for team in load_teams()}
    cache: dict[tuple[str, str], dict[str, Any]] = {}

    def prediction(home_id: str, away_id: str) -> dict[str, Any]:
        key = (home_id, away_id)
        if key not in cache:
            cache[key] = calculate_prediction(
                team_ratings[home_id],
                team_ratings[away_id],
                home_team_name=team_names[home_id],
                away_team_name=team_names[away_id],
                home_shot_volume_rating=shot_volume_ratings.get(home_id),
                away_shot_volume_rating=shot_volume_ratings.get(away_id),
            )
        return cache[key]

    return prediction


def knockout_winner(
    home_id: str,
    away_id: str,
    prediction: dict[str, Any],
    rng: random.Random,
) -> str:
    home_regulation = _number(prediction.get("home_win_probability"))
    draw_probability = _number(prediction.get("draw_probability"))
    away_regulation = _number(prediction.get("away_win_probability"))
    total = home_regulation + draw_probability + away_regulation
    if total <= 0:
        raise ValueError(f"Invalid knockout probabilities for {home_id}-{away_id}")
    home_regulation /= total
    draw_probability /= total
    away_regulation /= total
    decisive_total = home_regulation + away_regulation
    decisive_home = (
        home_regulation / decisive_total if decisive_total > 0 else 0.5
    )
    draw_threshold = home_regulation + draw_probability
    roll = rng.random()
    if roll < home_regulation:
        return home_id
    if roll >= draw_threshold:
        return away_id

    # On a regulation draw, 40% are resolved in extra time using the underlying
    # strength edge. The remaining 60% go to penalties, where that edge is
    # compressed toward 50%.
    if rng.random() < 0.40:
        return home_id if rng.random() < decisive_home else away_id
    penalty_home = 0.5 + 0.35 * (decisive_home - 0.5)
    return home_id if rng.random() < penalty_home else away_id


def simulate_tournaments(
    predictions: dict[str, dict[str, Any]],
    num_simulations: int,
    seed: int,
    knockout_prediction: KnockoutPrediction,
    match_states: list[MatchState] | None = None,
) -> list[dict[str, Any]]:
    if num_simulations < 1:
        raise ValueError("num_simulations must be at least 1")
    teams = load_teams()
    fixtures = build_fixtures(teams)
    validate_tournament(teams, fixtures)
    group_fixtures = [fixture for fixture in fixtures if fixture.stage == "group"]
    match_states = match_states or []
    completed_groups = {
        match.id: match
        for match in match_states
        if match.stage == "group" and match.completed
    }
    missing = [
        fixture
        for fixture in group_fixtures
        if fixture.id not in completed_groups and fixture.id not in predictions
    ]
    if missing:
        teams_by_id = {team.id: team.name for team in teams}
        raise ValueError(
            f"Latest prediction run is missing {len(missing)} group fixtures: "
            + ", ".join(
                (
                    f"{fixture.id} "
                    f"({teams_by_id[fixture.home_team_id]} vs "
                    f"{teams_by_id[fixture.away_team_id]})"
                )
                for fixture in missing
            )
        )

    team_groups = {team.id: team.group for team in teams}
    score_samplers = {
        fixture.id: compile_score_sampler(predictions[fixture.id])
        for fixture in group_fixtures
        if fixture.id not in completed_groups
    }
    known_knockouts = {
        stage: sorted(
            (match for match in match_states if match.stage == stage),
            key=lambda match: (
                match.match_number is None,
                match.match_number or 999,
                match.kickoff or datetime.max.replace(tzinfo=timezone.utc),
                match.id,
            ),
        )
        for stage in KNOCKOUT_STAGES
    }
    counts = {stage: Counter() for stage in STAGES}
    rng = random.Random(seed)

    def resolve_knockout_stage(
        stage: str,
        pairings: list[tuple[str, str]],
    ) -> list[str]:
        winners = []
        known_by_pair = {
            (match.home_team_id, match.away_team_id): match
            for match in known_knockouts[stage]
        }
        for home_id, away_id in pairings:
            known = known_by_pair.get((home_id, away_id))
            if known and known.completed:
                winners.append(_completed_winner(known))
                continue
            prediction_id = known.id if known else None
            if known and prediction_id not in predictions:
                raise ValueError(
                    "Latest prediction run is missing future knockout fixture "
                    f"{known.id} ({known.home_team_id} vs {known.away_team_id})"
                )
            prediction = (
                predictions[prediction_id]
                if prediction_id is not None and prediction_id in predictions
                else knockout_prediction(home_id, away_id)
            )
            winners.append(knockout_winner(home_id, away_id, prediction, rng))
        return winners

    for _ in range(num_simulations):
        results_by_group: dict[str, list[tuple[str, str, int, int]]] = defaultdict(list)
        for fixture in group_fixtures:
            completed = completed_groups.get(fixture.id)
            if completed is not None:
                home_goals = completed.home_score
                away_goals = completed.away_score
                if home_goals is None or away_goals is None:
                    raise ValueError(f"Completed group fixture {fixture.id} is missing a score")
            else:
                home_goals, away_goals = sample_group_score(
                    score_samplers[fixture.id],
                    rng,
                )
            results_by_group[fixture.group].append(
                (
                    fixture.home_team_id,
                    fixture.away_team_id,
                    home_goals,
                    away_goals,
                )
            )

        group_tables = {
            group: rank_group(
                [team.id for team in teams if team.group == group],
                results_by_group[group],
            )
            for group in "ABCDEFGHIJKL"
        }
        round_of_32_matches = known_knockouts["round_of_32"]
        pairings = (
            [(match.home_team_id, match.away_team_id) for match in round_of_32_matches]
            if round_of_32_matches
            else build_round_of_32(group_tables, team_groups)
        )
        round_of_32_teams = {team_id for pairing in pairings for team_id in pairing}
        counts["round_of_32"].update(round_of_32_teams)
        counts["group_stage_exit"].update(
            team.id for team in teams if team.id not in round_of_32_teams
        )

        current = resolve_knockout_stage("round_of_32", pairings)
        for stage in ("round_of_16", "quarterfinal", "semifinal", "final"):
            stage_matches = known_knockouts[stage]
            pairings = (
                [(match.home_team_id, match.away_team_id) for match in stage_matches]
                if stage_matches
                else [
                    (current[index], current[index + 1])
                    for index in range(0, len(current), 2)
                ]
            )
            stage_teams = {team_id for pairing in pairings for team_id in pairing}
            counts[stage].update(stage_teams)
            current = resolve_knockout_stage(stage, pairings)
        champion = current[0]
        counts["champion"].update([champion])

    return [
        {
            "team_id": team.id,
            **{
                f"{stage}_probability": counts[stage][team.id] / num_simulations
                for stage in STAGES
            },
        }
        for team in teams
    ]


class SimulationRepository:
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
        required = {
            "matches",
            "model_runs",
            "predictions",
            "simulation_runs",
            "team_simulation_results",
        }
        existing = set(inspector.get_table_names(schema=self.schema))
        missing = required - existing
        if missing:
            raise RuntimeError(
                f"Simulation pipeline tables are missing: {sorted(missing)}. Apply "
                "supabase/migrations/202606100005_tournament_simulation.sql first."
            )

    def load_match_states(
        self,
        database_team_ids: dict[str, Any],
    ) -> list[MatchState]:
        matches = self._table("matches")
        database_to_canonical_team = {
            str(database_id): team_id
            for team_id, database_id in database_team_ids.items()
            if database_id is not None
        }
        fixtures = build_fixtures()
        fixture_ids = {fixture.id for fixture in fixtures}
        fixtures_by_number = {fixture.number: fixture for fixture in fixtures}
        fixtures_by_key = {
            (
                fixture.kickoff,
                fixture.home_team_id,
                fixture.away_team_id,
            ): fixture
            for fixture in fixtures
        }
        with self.engine.connect() as connection:
            rows = [dict(row) for row in connection.execute(select(matches)).mappings()]

        states = []
        for index, row in enumerate(rows, start=1):
            stage = _stage_from_value(row.get("stage") or row.get("tournament_stage"))
            if stage == "third_place":
                continue
            home_team_id = database_to_canonical_team.get(str(row.get("home_team_id")))
            away_team_id = database_to_canonical_team.get(str(row.get("away_team_id")))
            if home_team_id is None or away_team_id is None:
                continue
            completed = _is_completed_match(row)
            home_score = _integer(row.get("home_score"))
            away_score = _integer(row.get("away_score"))
            if completed and (home_score is None or away_score is None):
                continue
            match_number = _integer(row.get("match_number") or row.get("number"))
            kickoff = _parse_timestamp(row.get("kickoff") or row.get("match_date"))
            if stage == "group":
                fixture = None
                row_id = str(row.get("id") or "")
                if row_id in fixture_ids:
                    fixture = next(item for item in fixtures if item.id == row_id)
                if fixture is None and match_number is not None:
                    fixture = fixtures_by_number.get(match_number)
                if fixture is None and kickoff is not None:
                    fixture = fixtures_by_key.get((kickoff, home_team_id, away_team_id))
                if fixture is None:
                    continue
                match_id = fixture.id
                match_number = fixture.number
            elif stage in KNOCKOUT_STAGES:
                match_id = str(
                    row.get("id")
                    or row.get("api_football_fixture_id")
                    or row.get("provider_fixture_id")
                    or row.get("canonical_match_id")
                    or f"provider-knockout-{index}"
                )
            else:
                continue

            home_penalty_score, away_penalty_score = _penalty_scores(row)
            states.append(
                MatchState(
                    id=match_id,
                    stage=stage,
                    home_team_id=home_team_id,
                    away_team_id=away_team_id,
                    completed=completed,
                    home_score=home_score,
                    away_score=away_score,
                    home_penalty_score=home_penalty_score,
                    away_penalty_score=away_penalty_score,
                    match_number=match_number,
                    kickoff=kickoff,
                )
            )
        return states

    def load_latest_predictions(
        self,
    ) -> tuple[Any, str, dict[str, dict[str, Any]]] | None:
        predictions = self._table("predictions")
        with self.engine.connect() as connection:
            latest = connection.execute(
                select(
                    predictions.c.model_run_id,
                    predictions.c.model_version,
                    predictions.c.prediction_timestamp,
                )
                .where(predictions.c.model_run_id.is_not(None))
                .order_by(predictions.c.prediction_timestamp.desc())
                .limit(1)
            ).mappings().one_or_none()
            if latest is None:
                return None
            rows = [
                dict(row)
                for row in connection.execute(
                    select(predictions).where(
                        predictions.c.model_run_id == latest["model_run_id"]
                    )
                ).mappings()
            ]
        canonical = {
            row["canonical_match_id"]: row
            for row in rows
            if row.get("canonical_match_id")
        }
        return (
            latest["model_run_id"],
            latest["model_version"] or "poisson-ratings-v1",
            canonical,
        )

    def store_results(
        self,
        model_run_id: Any,
        model_version: str,
        num_simulations: int,
        seed: int,
        results: list[dict[str, Any]],
    ) -> Any:
        runs = self._table("simulation_runs")
        result_table = self._table("team_simulation_results")
        run_id = str(uuid4())
        now = datetime.now(timezone.utc).isoformat()
        run_values = self._compatible_values(
            runs,
            {
                "id": run_id,
                "model_run_id": model_run_id,
                "model_version": model_version,
                "num_simulations": num_simulations,
                "iterations": num_simulations,
                "random_seed": seed,
                "created_at": now,
            },
        )
        with self.engine.begin() as connection:
            if self.engine.dialect.name == "postgresql":
                connection.execute(
                    text("select pg_advisory_xact_lock(hashtext('tournament-simulation'))")
                )
            connection.execute(runs.insert().values(**run_values))
            connection.execute(
                result_table.insert(),
                [
                    self._compatible_values(
                        result_table,
                        {
                            "simulation_run_id": run_id,
                            **result,
                            "created_at": now,
                        },
                    )
                    for result in results
                ],
            )
        return run_id

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
    args = parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logger = logging.getLogger("run_simulations")
    if args.simulations < 1:
        logger.error("--simulations must be at least 1")
        return 2

    database_url = load_environment().get("DATABASE_URL")
    if not database_url:
        logger.error("[simulation] FAILED: DATABASE_URL is required")
        return 2

    try:
        engine = create_database_engine(database_url)
    except Exception:
        logger.exception("[simulation] FAILED: could not initialize database")
        return 1

    try:
        logger.info("[simulation] START")
        repository = SimulationRepository(engine)
        repository.assert_schema()
        latest = repository.load_latest_predictions()
        if latest is None:
            logger.info("[simulation] SUCCESS: no prediction run available")
            return 0
        model_run_id, model_version, predictions = latest
        prediction_repository = PredictionRepository(engine)
        database_team_ids = prediction_repository.load_database_team_ids()
        match_states = repository.load_match_states(database_team_ids)
        completed_groups = sum(
            1 for match in match_states if match.stage == "group" and match.completed
        )
        completed_knockouts = sum(
            1 for match in match_states if match.stage in KNOCKOUT_STAGES and match.completed
        )
        upcoming_knockouts = sum(
            1 for match in match_states if match.stage in KNOCKOUT_STAGES and not match.completed
        )
        logger.info(
            "[simulation] Loaded tournament state: completed_group=%d "
            "completed_knockout=%d upcoming_knockout=%d",
            completed_groups,
            completed_knockouts,
            upcoming_knockouts,
        )
        team_ratings = prediction_repository.load_current_team_ratings(
            database_team_ids
        )
        shot_volume_ratings = (
            prediction_repository.load_current_shot_volume_ratings(
                database_team_ids
            )
        )
        knockout_prediction = build_knockout_prediction_provider(
            team_ratings,
            shot_volume_ratings,
        )
        logger.info("[simulation] Running %d tournaments", args.simulations)
        results = simulate_tournaments(
            predictions,
            args.simulations,
            args.seed,
            knockout_prediction,
            match_states,
        )
        logger.info("[simulation] Updating team probabilities")
        run_id = repository.store_results(
            model_run_id,
            model_version,
            args.simulations,
            args.seed,
            results,
        )
        logger.info("[simulation] SUCCESS run=%s", run_id)
        return 0
    except Exception:
        logger.exception("[simulation] FAILED: unexpected simulation error")
        return 1
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
