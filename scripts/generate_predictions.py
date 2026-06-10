#!/usr/bin/env python3
from __future__ import annotations

import json
import logging
import math
import os
import sys
from collections import defaultdict
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

from scripts.database import create_database_engine
from modeling.src.data import build_fixtures, load_teams, validate_tournament

MODEL_VERSION = "elo-context-v3"
LEGACY_MODEL_VERSION = "poisson-ratings-v1"
MAX_GOALS = 6
V3_ATTACK_WEIGHT = 0.15
V3_DEFENSE_WEIGHT = 0.30
V3_REST_WEIGHT = -0.15
V3_DRAW_MULTIPLIER = 1.15
PREDICTION_REQUIRED_COLUMNS = {
    "canonical_match_id",
    "model_run_id",
    "home_xg",
    "away_xg",
    "prediction_timestamp",
    "model_version",
    "confidence_score",
    "home_win_probability",
    "draw_probability",
    "away_win_probability",
    "most_likely_scoreline",
    "expected_total_goals",
    "over_2_5_probability",
    "both_teams_to_score_probability",
    "score_probabilities",
}


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


def load_canonical_future_matches(
    now: datetime,
    database_matches: list[dict[str, Any]] | None = None,
    database_team_ids: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Load the same canonical fixtures used by /v1/matches, then enrich them."""
    teams = load_teams()
    fixtures = build_fixtures(teams)
    validate_tournament(teams, fixtures)
    database_matches = database_matches or []
    database_team_ids = database_team_ids or {}

    by_id = {str(row["id"]): row for row in database_matches}
    enriched = []
    for fixture in fixtures:
        if fixture.kickoff <= now:
            continue
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


def calculate_prediction(
    home_rating: dict[str, Any],
    away_rating: dict[str, Any],
    home_player_rating: float | None = None,
    away_player_rating: float | None = None,
    home_rest_days: int | None = None,
    away_rest_days: int | None = None,
) -> dict[str, Any]:
    """Calculate promoted Elo-first probabilities with validated v3 context."""
    del home_player_rating, away_player_rating
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
    rest_signal = _rating_difference(home_rest_days, away_rest_days, 14.0)
    context_tilt = (
        V3_ATTACK_WEIGHT * attack_signal
        + V3_DEFENSE_WEIGHT * defense_signal
        + V3_REST_WEIGHT * rest_signal
    )
    probabilities = _normalize_probabilities(
        (
            elo_probabilities[0] * math.exp(context_tilt),
            elo_probabilities[1] * V3_DRAW_MULTIPLIER,
            elo_probabilities[2] * math.exp(-context_tilt),
        )
    )
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
    sample_size = min(
        _number(home_rating.get("matches_played")),
        _number(away_rating.get("matches_played")),
    )
    confidence_score = (
        0.55 * min(1.0, sample_size / 10.0)
        + 0.45 * max(probabilities)
    )

    return {
        "home_xg": round(home_xg, 4),
        "away_xg": round(away_xg, 4),
        "home_win_probability": probabilities[0],
        "draw_probability": probabilities[1],
        "away_win_probability": probabilities[2],
        "most_likely_scoreline": (
            f"{most_likely['home_goals']}-{most_likely['away_goals']}"
        ),
        "expected_total_goals": round(expected_total, 4),
        "over_2_5_probability": over_2_5,
        "both_teams_to_score_probability": both_score,
        "confidence_score": round(_clamp(confidence_score, 0.0, 1.0), 6),
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
                "supabase/migrations/202606100004_canonical_predictions.sql first."
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
        database_by_name = {
            _normalize_name(row.get("name")): row["id"]
            for row in rows
            if row.get("name")
        }
        database_ids = {str(row["id"]): row["id"] for row in rows}
        return {
            team.id: (
                database_ids.get(team.id)
                or database_by_name.get(_normalize_name(team.name))
            )
            for team in canonical_teams
        }

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
            # Rank-derived Elo and neutral component priors let canonical
            # fixtures run before provider data exists. Database ratings enrich
            # these values as soon as the canonical team can be resolved.
            canonical_ratings[team.id] = {
                "team_id": team.id,
                "elo_rating": team.elo,
                "attack_rating": 50.0,
                "defense_rating": 50.0,
                "form_rating": 50.0,
                "matches_played": 0,
                **(database_rating or {}),
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
            "notes": "Promoted Elo-first context v3 predictions",
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
                "rest_weight": V3_REST_WEIGHT,
                "draw_multiplier": V3_DRAW_MULTIPLIER,
                "recent_form_weight": 0.0,
                "player_weight": 0.0,
                "travel_weight": 0.0,
                "availability_weight": 0.0,
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
            "confidence_tier": self._confidence_tier(prediction["confidence_score"]),
            "explanation_factors": [
                "Elo result probabilities",
                "Validated attack and defense rating adjustments",
                "Validated rest adjustment when match history is available",
                "Validated draw calibration",
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
    def _confidence_tier(score: float) -> str:
        if score >= 0.7:
            return "High"
        if score >= 0.5:
            return "Medium"
        return "Low"

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
        latest_match_dates = repository.load_latest_team_match_dates(database_team_ids)
        predictions = []
        for match in matches:
            home_rating = team_ratings.get(match["home_team_id"])
            away_rating = team_ratings.get(match["away_team_id"])
            if home_rating is None or away_rating is None:
                logger.warning(
                    "Skipping match %s because current team ratings are unavailable",
                    match["id"],
                )
                continue
            predictions.append(
                {
                    "canonical_match_id": match["canonical_match_id"],
                    "database_match_id": match["database_match_id"],
                    **calculate_prediction(
                        home_rating,
                        away_rating,
                        home_rest_days=(
                            (match["kickoff"].date() - latest_match_dates[
                                match["home_team_id"]
                            ]).days
                            if match["home_team_id"] in latest_match_dates else None
                        ),
                        away_rest_days=(
                            (match["kickoff"].date() - latest_match_dates[
                                match["away_team_id"]
                            ]).days
                            if match["away_team_id"] in latest_match_dates else None
                        ),
                    ),
                }
            )

        if not predictions:
            logger.info(
                "[prediction-generation] SUCCESS: no future matches have both team ratings"
            )
            return 0
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
