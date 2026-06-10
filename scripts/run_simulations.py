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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from dotenv import load_dotenv
from sqlalchemy import JSON, MetaData, Table, inspect, select, text
from sqlalchemy.engine import Engine

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from modeling.src.data import build_fixtures, load_teams, validate_tournament
from scripts.database import create_database_engine

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


def derive_team_strengths(
    fixtures: list[Any],
    predictions: dict[str, dict[str, Any]],
) -> dict[str, float]:
    expected_points: dict[str, list[float]] = defaultdict(list)
    for fixture in fixtures:
        prediction = predictions[fixture.id]
        home_win = _number(prediction.get("home_win_probability"))
        draw = _number(prediction.get("draw_probability"))
        away_win = _number(prediction.get("away_win_probability"))
        expected_points[fixture.home_team_id].append(3.0 * home_win + draw)
        expected_points[fixture.away_team_id].append(3.0 * away_win + draw)
    return {
        team_id: sum(values) / len(values)
        for team_id, values in expected_points.items()
    }


def knockout_winner(
    home_id: str,
    away_id: str,
    strengths: dict[str, float],
    rng: random.Random,
) -> str:
    difference = strengths[home_id] - strengths[away_id]
    decisive_home = 1.0 / (1.0 + math.exp(-1.6 * difference))
    draw_probability = max(0.16, 0.27 - 0.07 * abs(difference))
    home_regulation = (1.0 - draw_probability) * decisive_home
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
) -> list[dict[str, Any]]:
    if num_simulations < 1:
        raise ValueError("num_simulations must be at least 1")
    teams = load_teams()
    fixtures = build_fixtures(teams)
    validate_tournament(teams, fixtures)
    group_fixtures = [fixture for fixture in fixtures if fixture.stage == "group"]
    missing = [fixture.id for fixture in group_fixtures if fixture.id not in predictions]
    if missing:
        raise ValueError(
            f"Latest prediction run is missing {len(missing)} group fixtures"
        )

    team_groups = {team.id: team.group for team in teams}
    strengths = derive_team_strengths(group_fixtures, predictions)
    score_samplers = {
        fixture.id: compile_score_sampler(predictions[fixture.id])
        for fixture in group_fixtures
    }
    counts = {stage: Counter() for stage in STAGES}
    rng = random.Random(seed)

    for _ in range(num_simulations):
        results_by_group: dict[str, list[tuple[str, str, int, int]]] = defaultdict(list)
        for fixture in group_fixtures:
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
        pairings = build_round_of_32(group_tables, team_groups)
        qualified = {team_id for pairing in pairings for team_id in pairing}
        counts["round_of_32"].update(qualified)
        counts["group_stage_exit"].update(
            team.id for team in teams if team.id not in qualified
        )

        current = [
            knockout_winner(home_id, away_id, strengths, rng)
            for home_id, away_id in pairings
        ]
        counts["round_of_16"].update(current)
        for stage in ("quarterfinal", "semifinal", "final"):
            current = [
                knockout_winner(current[index], current[index + 1], strengths, rng)
                for index in range(0, len(current), 2)
            ]
            counts[stage].update(current)
        champion = knockout_winner(current[0], current[1], strengths, rng)
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
        logger.info("[simulation] Running %d tournaments", args.simulations)
        results = simulate_tournaments(predictions, args.simulations, args.seed)
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
