#!/usr/bin/env python3
"""Leakage-safe walk-forward backfill for completed WC26 knockout matches."""
from __future__ import annotations

import argparse
import json
import logging
import math
import sys
import warnings
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any, Iterable
from uuid import UUID, uuid4

from sqlalchemy import JSON, MetaData, Table, inspect, select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SAWarning

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from modeling.src.data import load_teams
from scripts.build_xg_proxy_features import build_team_ratings as build_chance_ratings
from scripts.database import create_database_engine
from scripts.generate_predictions import (
    MODEL_DESCRIPTION,
    MODEL_VERSION,
    PredictionRepository,
    _parse_timestamp,
    _stage_from_value,
    calculate_prediction,
    canonical_prior_elo,
    load_environment,
    map_database_team_ids,
)
from scripts.update_ratings import calculate_player_ratings, calculate_team_ratings
from scripts.run_simulations import (
    _is_world_cup_2026_provider_row,
    _official_match_number,
)

LOGGER = logging.getLogger("historical-knockout-backfill")
TARGET_STAGES = {"round_of_32", "round_of_16"}
COMPLETED_STATUSES = {"completed", "finished", "ft", "aet", "pen"}
SAFETY_MARGIN = timedelta(seconds=1)


def json_safe(value: Any) -> Any:
    """Recursively convert a value to JSON-native primitives."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Enum):
        return json_safe(value.value)
    if isinstance(value, dict):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return [json_safe(item) for item in sorted(value, key=repr)]
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


@dataclass(frozen=True)
class KnockoutIdentity:
    database_match_id: Any | None
    canonical_match_id: str | None
    official_match_number: int | None
    provider_fixture_id: Any | None
    stable_key: tuple[str, str] | None


def _valid_canonical_match_id(value: Any, stage: str) -> str | None:
    raw = str(value or "").strip().upper()
    if not raw.startswith("WC26-"):
        return None
    try:
        number = int(raw.removeprefix("WC26-"))
    except ValueError:
        return None
    if number not in range(73, 97) or number not in {
        "round_of_32": range(73, 89),
        "round_of_16": range(89, 97),
    }.get(stage, ()):
        return None
    return f"WC26-{number:03d}"


def resolve_knockout_identity(row: dict[str, Any], stage: str) -> KnockoutIdentity:
    """Resolve identity exactly as the official knockout loaders do.

    Provider-only official rows are stable by provider fixture ID, but that ID
    is not an official match number and therefore never becomes a WC26 ID.
    """
    database_match_id = row.get("id") or row.get("match_id")
    provider_fixture_id = (
        row.get("api_football_fixture_id") or row.get("provider_fixture_id")
    )
    canonical = _valid_canonical_match_id(row.get("canonical_match_id"), stage)
    number = _official_match_number(row, stage)
    if canonical is not None:
        try:
            number = int(canonical.removeprefix("WC26-"))
        except ValueError:  # guarded by _valid_canonical_match_id
            number = None
    elif number is not None:
        canonical = f"WC26-{number:03d}"

    if database_match_id is not None:
        stable_key = ("match", str(database_match_id))
    elif provider_fixture_id is not None and _is_world_cup_2026_provider_row(row):
        stable_key = ("provider", str(provider_fixture_id))
    else:
        stable_key = None
    return KnockoutIdentity(
        database_match_id, canonical, number, provider_fixture_id, stable_key
    )


def timestamp(value: Any) -> datetime | None:
    return _parse_timestamp(value)


def source_timestamp(row: dict[str, Any]) -> datetime | None:
    values = [
        timestamp(row.get(name))
        for name in ("match_date", "kickoff", "captured_at", "created_at", "updated_at")
    ]
    return max((value for value in values if value is not None), default=None)


def prediction_generated_at(row: dict[str, Any]) -> datetime | None:
    return timestamp(
        row.get("prediction_timestamp")
        or row.get("created_at")
        or row.get("run_generated_at")
    )


def is_authentic_prediction(row: dict[str, Any], kickoff: datetime) -> bool:
    generated = prediction_generated_at(row)
    return bool(
        row.get("generation_mode", "standard") != "historical_backfill"
        and generated is not None
        and generated < kickoff
    )


def filter_source_rows(
    rows: Iterable[dict[str, Any]],
    cutoff: datetime,
    target_match_id: Any,
) -> list[dict[str, Any]]:
    """Apply the exclusive cutoff to both event and database availability time."""
    accepted = []
    for row in rows:
        if str(row.get("match_id")) == str(target_match_id):
            continue
        match_time = timestamp(row.get("match_date") or row.get("kickoff"))
        if match_time is None or match_time >= cutoff:
            continue
        availability_times = [
            timestamp(row.get(name))
            for name in ("captured_at", "created_at", "updated_at")
            if row.get(name) is not None
        ]
        if any(value is None or value >= cutoff for value in availability_times):
            continue
        accepted.append(row)
    assert all(str(row.get("match_id")) != str(target_match_id) for row in accepted)
    assert all(timestamp(row.get("match_date") or row.get("kickoff")) < cutoff for row in accepted)
    return accepted


@dataclass(frozen=True)
class HistoricalState:
    team_ratings: dict[Any, dict[str, Any]]
    player_team_averages: dict[Any, float]
    shot_volume_ratings: dict[Any, float]
    team_stat_count: int
    player_stat_count: int
    completed_match_count: int
    maximum_source_timestamp: datetime | None


def build_historical_state(
    *,
    teams: list[dict[str, Any]],
    matches: list[dict[str, Any]],
    team_stats: list[dict[str, Any]],
    player_stats: list[dict[str, Any]],
    cutoff: datetime,
    target_match_id: Any,
) -> HistoricalState:
    eligible_matches = filter_source_rows(matches, cutoff, target_match_id)
    eligible_ids = {str(row["id"]) for row in eligible_matches if row.get("id") is not None}
    eligible_matches = [
        row for row in eligible_matches
        if str(row.get("status") or "").lower() in COMPLETED_STATUSES
        or row.get("completed")
        or (row.get("home_score") is not None and row.get("away_score") is not None)
    ]
    eligible_ids = {str(row["id"]) for row in eligible_matches}
    match_times = {
        str(row["id"]): timestamp(row.get("match_date") or row.get("kickoff"))
        for row in eligible_matches
    }

    def enrich(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        candidates = [
            {**row, "match_date": match_times.get(str(row.get("match_id")))}
            for row in rows if str(row.get("match_id")) in eligible_ids
        ]
        return filter_source_rows(candidates, cutoff, target_match_id)

    safe_team_stats = enrich(team_stats)
    safe_player_stats = enrich(player_stats)
    calculated_team = {row["team_id"]: row for row in calculate_team_ratings(safe_team_stats)}
    rank_by_database_id = {
        row["id"]: int(row.get("fifa_rank") or row.get("rank") or 50) for row in teams
    }
    team_ratings: dict[Any, dict[str, Any]] = {}
    for row in teams:
        team_id = row["id"]
        calculated = calculated_team.get(team_id)
        team_ratings[team_id] = {
            "team_id": team_id,
            "elo_rating": canonical_prior_elo(rank_by_database_id[team_id]),
            "attack_rating": 50.0,
            "defense_rating": 50.0,
            "form_rating": 50.0,
            "matches_played": 0,
            **(calculated or {}),
            "_team_rating_available": calculated is not None,
            "_attack_defense_available": calculated is not None,
            "_rating_source": "historical_rebuild" if calculated else "rank_prior",
        }
    player_ratings = calculate_player_ratings(safe_player_stats)
    by_team: dict[Any, list[float]] = defaultdict(list)
    for row in player_ratings:
        if row.get("team_id") is not None and row.get("overall_rating") is not None:
            by_team[row["team_id"]].append(float(row["overall_rating"]))
    player_averages = {
        team_id: sum(values) / len(values) for team_id, values in by_team.items() if values
    }
    chance = build_chance_ratings(safe_team_stats)
    shot_volume = {
        row["team_id"]: float(row["shot_volume_rating"])
        for row in chance if row.get("shot_volume_rating") is not None
    }
    used_rows = eligible_matches + safe_team_stats + safe_player_stats
    maximum = max(
        (value for row in used_rows if (value := source_timestamp(row)) is not None),
        default=None,
    )
    if maximum is not None:
        assert maximum < cutoff, f"source timestamp {maximum.isoformat()} reached cutoff"
    return HistoricalState(
        team_ratings, player_averages, shot_volume, len(safe_team_stats),
        len(safe_player_stats), len(eligible_matches), maximum,
    )


class HistoricalBackfillRepository:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self.schema = None if engine.dialect.name == "sqlite" else "public"
        self.metadata = MetaData()
        self.tables: dict[str, Table] = {}

    def table(self, name: str) -> Table:
        if name not in self.tables:
            # Supabase may expose a literal dialect_options key for a reflected
            # index. Suppress only SQLAlchemy's known harmless warning.
            with warnings.catch_warnings():
                warnings.filterwarnings(
                    "ignore",
                    message=r"Can't validate argument 'dialect_options'.*",
                    category=SAWarning,
                )
                self.tables[name] = Table(
                    name, self.metadata, schema=self.schema,
                    autoload_with=self.engine, resolve_fks=False,
                )
        return self.tables[name]

    def assert_schema(self, apply: bool = False) -> None:
        required = {"matches", "teams", "team_match_stats", "player_match_stats", "predictions", "model_runs"}
        existing = set(inspect(self.engine).get_table_names(schema=self.schema))
        if missing := required - existing:
            raise RuntimeError(f"Historical backfill tables are missing: {sorted(missing)}")
        if apply:
            columns = {column["name"] for column in inspect(self.engine).get_columns("predictions", schema=self.schema)}
            required_columns = {"generation_mode", "historical_cutoff", "backfilled_at", "maximum_source_timestamp"}
            if missing := required_columns - columns:
                raise RuntimeError(f"Apply 202607100002_historical_prediction_backfill.sql; missing {sorted(missing)}")

    def rows(self, name: str) -> list[dict[str, Any]]:
        table = self.table(name)
        with self.engine.connect() as connection:
            return [dict(row) for row in connection.execute(select(table)).mappings()]

    def load_stats(self, name: str) -> list[dict[str, Any]]:
        stats, matches = self.table(name), self.table("matches")
        date_col = next((matches.c[name] for name in ("kickoff", "match_date") if name in matches.c), None)
        columns = [stats]
        if date_col is not None:
            columns.append(date_col.label("match_date"))
        with self.engine.connect() as connection:
            return [dict(row) for row in connection.execute(select(*columns).join(matches, matches.c.id == stats.c.match_id)).mappings()]

    @staticmethod
    def _insert_values(table: Table, values: dict[str, Any]) -> dict[str, Any]:
        """Filter table values and sanitize JSON columns without coercing SQL types."""
        compatible = PredictionRepository._compatible_values(table, values)
        for key in list(compatible):
            if not isinstance(table.c[key].type, JSON):
                continue
            try:
                compatible[key] = json_safe(compatible[key])
                # Validate here so failures identify the exact field instead of
                # surfacing later from a dialect JSON serializer.
                json.dumps(compatible[key])
            except (TypeError, ValueError):
                LOGGER.exception(
                    "JSON serialization failed table=%s field=%s value_type=%s",
                    table.name, key, type(values.get(key)).__name__,
                )
                raise
        return compatible

    def store(self, prediction: dict[str, Any], generated_at: datetime) -> str:
        runs, predictions = self.table("model_runs"), self.table("predictions")
        run_id = str(uuid4())
        now = generated_at.isoformat()
        run_values = self._insert_values(runs, {
            "id": run_id, "run_date": generated_at.date().isoformat(), "model_version": MODEL_VERSION,
            "notes": MODEL_DESCRIPTION, "data_cutoff": prediction["historical_cutoff"],
            "status": "completed", "random_seed": 0, "generated_at": now,
            "metadata": {
                "generation_mode": "historical_backfill", "matches_predicted": 1,
                "database_match_id": prediction.get("database_match_id"),
                "provider_fixture_id": prediction.get("provider_fixture_id"),
                "canonical_match_id": prediction.get("canonical_match_id"),
                "provenance": prediction.get("run_metadata", {}),
            },
        })
        values = self._insert_values(predictions, {
            **prediction, "id": str(uuid4()), "model_run_id": run_id,
            "prediction_timestamp": now, "model_version": MODEL_VERSION,
            "data_cutoff": prediction["historical_cutoff"], "created_at": now, "updated_at": now,
            "generation_mode": "historical_backfill", "backfilled_at": now,
            "home_win": prediction["home_win_probability"], "draw": prediction["draw_probability"],
            "away_win": prediction["away_win_probability"],
            "home_win_prob": prediction["home_win_probability"], "draw_prob": prediction["draw_probability"],
            "away_win_prob": prediction["away_win_probability"],
        })
        with self.engine.begin() as connection:
            identity_conditions = []
            if prediction.get("database_match_id") is not None:
                identity_conditions.append(
                    predictions.c.match_id == prediction["database_match_id"]
                )
            elif prediction.get("provider_fixture_id") is not None:
                identity_conditions.append(
                    predictions.c.provider_fixture_id == prediction["provider_fixture_id"]
                )
            if not identity_conditions:
                raise ValueError("Backfill prediction has no stable fixture identity")
            existing = connection.execute(select(predictions.c.id).where(
                *identity_conditions,
                predictions.c.generation_mode == "historical_backfill",
                predictions.c.model_version == MODEL_VERSION,
            )).scalar_one_or_none()
            if existing is not None:
                return "skipped_existing_backfill"
            connection.execute(runs.insert().values(**run_values))
            connection.execute(predictions.insert().values(**values))
        return run_id


def target_matches(matches: list[dict[str, Any]], predictions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_match: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in predictions:
        for key in (row.get("match_id"), row.get("canonical_match_id"), row.get("provider_fixture_id")):
            if key is not None:
                by_match[str(key)].append(row)
    targets = []
    for row in matches:
        stage = _stage_from_value(row.get("stage") or row.get("tournament_stage"))
        kickoff = timestamp(row.get("kickoff") or row.get("match_date"))
        status = str(row.get("status") or "").lower()
        completed = status in COMPLETED_STATUSES or row.get("completed") or (
            row.get("home_score") is not None and row.get("away_score") is not None
        )
        if kickoff is None:
            continue
        identity = resolve_knockout_identity(row, stage)
        official = (
            identity.official_match_number is not None
            or _is_world_cup_2026_provider_row(row)
        )
        in_tournament_window = (
            datetime(2026, 6, 28, tzinfo=timezone.utc)
            <= kickoff
            < datetime(2026, 7, 20, tzinfo=timezone.utc)
        )
        if stage not in TARGET_STAGES or not completed or not official or not in_tournament_window:
            continue
        identities = [row.get("id"), row.get("canonical_match_id"), row.get("api_football_fixture_id"), row.get("provider_fixture_id")]
        related = {id(candidate): candidate for identity in identities if identity is not None for candidate in by_match[str(identity)]}.values()
        targets.append({
            **row, "_stage": stage, "_kickoff": kickoff, "_identity": identity,
            "_authentic": any(is_authentic_prediction(p, kickoff) for p in related),
        })
    return sorted(targets, key=lambda row: (row["_kickoff"], str(row.get("id"))))


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="persist rows (default is dry-run)")
    parser.add_argument("--match-id")
    parser.add_argument("--stage", choices=sorted(TARGET_STAGES))
    parser.add_argument("--from-date")
    parser.add_argument("--to-date")
    parser.add_argument("--limit", type=int)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    env = load_environment()
    if not env.get("DATABASE_URL"):
        LOGGER.error("DATABASE_URL is required")
        return 2
    repository = HistoricalBackfillRepository(create_database_engine(env["DATABASE_URL"]))
    repository.assert_schema(apply=args.apply)
    matches, teams, predictions = repository.rows("matches"), repository.rows("teams"), repository.rows("predictions")
    team_stats = repository.load_stats("team_match_stats")
    player_stats = repository.load_stats("player_match_stats")
    canonical_ids = map_database_team_ids(teams)
    database_to_canonical = {value: key for key, value in canonical_ids.items() if value is not None}
    selected = target_matches(matches, predictions)
    if args.match_id:
        selected = [row for row in selected if args.match_id in {str(row.get("id")), str(row.get("api_football_fixture_id")), str(row.get("canonical_match_id"))}]
    if args.stage:
        selected = [row for row in selected if row["_stage"] == args.stage]
    if args.from_date:
        start = timestamp(args.from_date)
        selected = [row for row in selected if start and row["_kickoff"] >= start]
    if args.to_date:
        end = timestamp(args.to_date)
        selected = [row for row in selected if end and row["_kickoff"] <= end]
    if args.limit is not None:
        if args.limit < 1:
            raise ValueError("--limit must be positive")
        selected = selected[:args.limit]
    generated_at = datetime.now(timezone.utc)
    canonical_teams = {team.id: team for team in load_teams()}
    for fixture in selected:
        kickoff, cutoff = fixture["_kickoff"], fixture["_kickoff"] - SAFETY_MARGIN
        identity = fixture.get("_identity") or resolve_knockout_identity(
            fixture, fixture["_stage"]
        )
        provider_id = identity.provider_fixture_id
        home = database_to_canonical.get(fixture.get("home_team_id"))
        away = database_to_canonical.get(fixture.get("away_team_id"))
        if fixture["_authentic"]:
            LOGGER.info("match=%s provider=%s kickoff=%s authentic=yes action=skip", fixture.get("id"), provider_id, kickoff.isoformat())
            continue
        if home is None or away is None:
            LOGGER.warning(
                "match=%s provider=%s teams=%s_vs_%s kickoff=%s action=skip reason=unmapped_team",
                identity.database_match_id, provider_id, home, away, kickoff.isoformat(),
            )
            continue
        if identity.stable_key is None:
            LOGGER.warning(
                "match=%s provider=%s teams=%s_vs_%s kickoff=%s action=skip reason=no_stable_fixture_identity",
                identity.database_match_id, provider_id, home, away, kickoff.isoformat(),
            )
            continue
        state = build_historical_state(
            teams=teams, matches=matches, team_stats=team_stats,
            player_stats=player_stats, cutoff=cutoff,
            target_match_id=(identity.database_match_id or provider_id),
        )
        prediction = calculate_prediction(
            state.team_ratings[canonical_ids[home]], state.team_ratings[canonical_ids[away]],
            state.player_team_averages.get(canonical_ids[home]), state.player_team_averages.get(canonical_ids[away]),
            home_team_name=canonical_teams[home].name, away_team_name=canonical_teams[away].name,
            home_shot_volume_rating=state.shot_volume_ratings.get(canonical_ids[home]),
            away_shot_volume_rating=state.shot_volume_ratings.get(canonical_ids[away]),
        )
        triple = (prediction["home_win_probability"], prediction["draw_probability"], prediction["away_win_probability"])
        assert math.isclose(sum(triple), 1.0, abs_tol=1e-12)
        payload = {
            **prediction, "canonical_match_id": identity.canonical_match_id,
            "database_match_id": identity.database_match_id,
            "match_id": identity.database_match_id,
            "provider_fixture_id": provider_id,
            "home_team_id": home, "away_team_id": away, "kickoff": kickoff.isoformat(),
            "historical_cutoff": cutoff.isoformat(),
            "maximum_source_timestamp": state.maximum_source_timestamp.isoformat() if state.maximum_source_timestamp else None,
        }
        LOGGER.info(
            "match=%s provider=%s teams=%s_vs_%s kickoff=%s authentic=no counts=matches:%d,team_stats:%d,player_stats:%d max_source=%s probabilities=(%.6f,%.6f,%.6f) action=%s",
            identity.database_match_id, provider_id, home, away, kickoff.isoformat(), state.completed_match_count,
            state.team_stat_count, state.player_stat_count, payload["maximum_source_timestamp"], *triple,
            "insert" if args.apply else "would_insert",
        )
        if args.apply:
            repository.store(payload, generated_at)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
