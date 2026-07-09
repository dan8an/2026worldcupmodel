import json
import logging
import os
import time
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any

from dotenv import load_dotenv
from sqlalchemy import Engine, MetaData, Table, inspect, select, text

from modeling.src.data import ROOT, build_fixtures, load_teams, load_venues, validate_tournament
from modeling.src.features.context import ContextRepository
from modeling.src.flags import flag_for_team
from modeling.src.poisson import predict_match
from modeling.src.simulation import (
    PUBLISHED_SIMULATION_ITERATIONS,
    prediction_dict,
    simulate_tournament,
)
from modeling.src.team_profiles import (
    form_summary,
    key_players,
    load_squad_metadata,
    load_squad_players,
    player_payload,
    recent_results,
    team_analysis,
)
from scripts.database import create_database_engine
from scripts.generate_predictions import (
    PredictionRepository,
    load_team_aliases,
)

STATIC_MODEL_VERSION = "context-0.2.0"
MODEL_VERSION = STATIC_MODEL_VERSION
LOGGER = logging.getLogger(__name__)


def _json_value(value: Any, default: Any) -> Any:
    if value is None:
        return default
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


class DatabasePredictionSource:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    @classmethod
    def from_environment(cls) -> "DatabasePredictionSource | None":
        load_dotenv(ROOT / ".env", override=False)
        load_dotenv(ROOT / "backend" / ".env", override=False)
        database_url = os.getenv("DATABASE_URL")
        if not database_url:
            LOGGER.warning(
                "DATABASE_URL is unavailable; serving static prediction fallback"
            )
            return None
        try:
            return cls(create_database_engine(database_url))
        except Exception:
            LOGGER.warning(
                "DATABASE_URL could not initialize; serving static prediction "
                "fallback",
                exc_info=True,
            )
            return None

    def load_latest(self) -> dict[str, Any] | None:
        table_prefix = "public." if self.engine.dialect.name == "postgresql" else ""
        with self.engine.connect() as connection:
            latest = connection.execute(
                text(
                    f"""
                    select
                      mr.id as model_run_id,
                      mr.model_version,
                      mr.generated_at,
                      mr.data_cutoff
                    from {table_prefix}model_runs mr
                    where coalesce(mr.status, 'completed') = 'completed'
                      and exists (
                        select 1
                        from {table_prefix}predictions p
                        where p.model_run_id = mr.id
                          and p.canonical_match_id is not null
                      )
                    order by mr.generated_at desc nulls last, mr.id desc
                    limit 1
                    """
                )
            ).mappings().one_or_none()
            if latest is None:
                return None
            rows = [
                dict(row)
                for row in connection.execute(
                    text(
                        f"""
                        select *
                        from {table_prefix}predictions
                        where model_run_id = :model_run_id
                          and canonical_match_id is not null
                        """
                    ),
                    {"model_run_id": latest["model_run_id"]},
                ).mappings()
            ]
        return {
            "model_run_id": latest["model_run_id"],
            "model_version": latest["model_version"],
            "generated_at": latest["generated_at"],
            "data_cutoff": latest.get("data_cutoff")
            or latest["generated_at"],
            "predictions": {
                str(row["canonical_match_id"]): row
                for row in rows
                if row.get("canonical_match_id")
            },
        }


class DatabaseSimulationSource:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def load_latest(self) -> dict[str, Any] | None:
        table_prefix = "public." if self.engine.dialect.name == "postgresql" else ""
        with self.engine.connect() as connection:
            latest = connection.execute(
                text(
                    f"""
                    select sr.*
                    from {table_prefix}simulation_runs sr
                    where exists (
                      select 1
                      from {table_prefix}team_simulation_results tsr
                      where tsr.simulation_run_id = sr.id
                    )
                    order by sr.created_at desc, sr.id desc
                    limit 1
                    """
                )
            ).mappings().one_or_none()
            if latest is None:
                return None
            rows = [
                dict(row)
                for row in connection.execute(
                    text(
                        f"""
                        select *
                        from {table_prefix}team_simulation_results
                        where simulation_run_id = :simulation_run_id
                        """
                    ),
                    {"simulation_run_id": latest["id"]},
                ).mappings()
            ]
        return {
            "run": dict(latest),
            "results": rows,
            "model_inputs": self._load_model_inputs(),
        }

    def _load_model_inputs(self) -> dict[str, dict[str, Any]]:
        try:
            repository = PredictionRepository(self.engine)
            database_team_ids = repository.load_database_team_ids()
            ratings = repository.load_current_team_ratings(database_team_ids)
            shot_volume = repository.load_current_shot_volume_details(
                database_team_ids
            )
        except Exception:
            LOGGER.warning(
                "Simulation rating transparency metadata is unavailable",
                exc_info=True,
            )
            return {}

        ranked_team_ids = sorted(
            ratings,
            key=lambda team_id: float(ratings[team_id]["elo_rating"]),
            reverse=True,
        )
        elo_ranks = {
            team_id: rank
            for rank, team_id in enumerate(ranked_team_ids, start=1)
        }
        return {
            team_id: {
                "elo_rating": float(rating["elo_rating"]),
                "elo_rank": elo_ranks[team_id],
                "attack_rating": float(rating["attack_rating"]),
                "defense_rating": float(rating["defense_rating"]),
                "shot_volume_rating": (
                    float(shot_volume[team_id]["shot_volume_rating"])
                    if team_id in shot_volume
                    else None
                ),
                "rating_source": rating["_rating_source"],
                "rating_matches": int(rating.get("matches_played") or 0),
                "shot_volume_sample_matches": (
                    shot_volume.get(team_id, {}).get("sample_matches")
                ),
            }
            for team_id, rating in ratings.items()
        }


class DatabaseMatchResultSource:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def load(self) -> list[dict[str, Any]]:
        schema = None if self.engine.dialect.name == "sqlite" else "public"
        inspector = inspect(self.engine)
        if "matches" not in inspector.get_table_names(schema=schema):
            return []

        metadata = MetaData()
        matches = Table("matches", metadata, schema=schema, autoload_with=self.engine)
        teams = (
            Table("teams", metadata, schema=schema, autoload_with=self.engine)
            if "teams" in inspector.get_table_names(schema=schema)
            else None
        )
        with self.engine.connect() as connection:
            match_rows = [
                dict(row)
                for row in connection.execute(select(matches)).mappings()
            ]
            team_names = (
                {
                    row["id"]: row["name"]
                    for row in connection.execute(
                        select(teams.c.id, teams.c.name)
                    ).mappings()
                }
                if teams is not None
                else {}
            )

        def display_name(value: Any, team_id: Any) -> str | None:
            text_value = str(value or "").strip()
            if text_value and not text_value.isdigit():
                return text_value
            return team_names.get(team_id)

        return [
            {
                **row,
                "home_team_name": display_name(
                    row.get("home_team"), row.get("home_team_id")
                ),
                "away_team_name": display_name(
                    row.get("away_team"), row.get("away_team_id")
                ),
            }
            for row in match_rows
        ]


class PredictionService:
    def __init__(
        self,
        prediction_source: DatabasePredictionSource | None = None,
        simulation_source: DatabaseSimulationSource | None = None,
        match_result_source: DatabaseMatchResultSource | None = None,
        prediction_cache_seconds: float | None = None,
    ) -> None:
        self.generated_at = datetime.now(timezone.utc).isoformat()
        self.data_cutoff = self.generated_at
        self.teams = load_teams()
        self.teams_by_id = {team.id: team for team in self.teams}
        self.contexts = ContextRepository()
        all_fixtures = build_fixtures(self.teams)
        validate_tournament(self.teams, all_fixtures)
        self.fixtures = [match for match in all_fixtures if match.stage == "group"]
        self.venues = load_venues()
        self.squad_players = load_squad_players()
        self.squad_metadata = load_squad_metadata()
        self.static_predictions = {
            match.id: predict_match(
                self.teams_by_id[match.home_team_id or ""],
                self.teams_by_id[match.away_team_id or ""],
                match.id,
                context=self.contexts.for_match(
                    match.home_team_id or "",
                    match.away_team_id or "",
                    datetime.fromisoformat(self.data_cutoff),
                ),
            )
            for match in self.fixtures
            if match.stage == "group"
        }
        self.predictions = self.static_predictions
        self.prediction_source = (
            prediction_source
            if prediction_source is not None
            else DatabasePredictionSource.from_environment()
        )
        self.simulation_source = simulation_source
        if self.simulation_source is None and self.prediction_source is not None:
            engine = getattr(self.prediction_source, "engine", None)
            if engine is not None:
                self.simulation_source = DatabaseSimulationSource(engine)
        self.match_result_source = match_result_source
        if self.match_result_source is None and self.prediction_source is not None:
            engine = getattr(self.prediction_source, "engine", None)
            if engine is not None:
                self.match_result_source = DatabaseMatchResultSource(engine)
        aliases = load_team_aliases()
        alias_lookup = {}
        for team in self.teams:
            for name in (team.name, *aliases[team.id]):
                alias_lookup[self._normalize_name(name)] = team.id
        self.team_alias_lookup = alias_lookup
        try:
            configured_cache_seconds = float(
                os.getenv("PREDICTION_READ_CACHE_SECONDS", "30")
            )
        except ValueError:
            configured_cache_seconds = 30.0
            LOGGER.warning(
                "PREDICTION_READ_CACHE_SECONDS is invalid; using 30 seconds"
            )
        self.prediction_cache_seconds = max(
            0.0,
            prediction_cache_seconds
            if prediction_cache_seconds is not None
            else configured_cache_seconds,
        )
        self._database_prediction_run: dict[str, Any] | None = None
        self._database_prediction_checked_at = 0.0
        self._simulation: dict | None = None
        self._simulation_generated_at = self.generated_at
        self._simulation_data_cutoff = self.data_cutoff

    @staticmethod
    def _normalize_name(value: Any) -> str:
        return "".join(
            character
            for character in str(value or "").lower()
            if character.isalnum()
        )

    def current_match_rows(self) -> list[dict[str, Any]]:
        if self.match_result_source is None:
            return []
        try:
            return self.match_result_source.load()
        except Exception:
            LOGGER.warning("Database match results are unavailable", exc_info=True)
            return []

    def current_match_results(
        self,
        rows: list[dict[str, Any]] | None = None,
    ) -> dict[str, dict[str, Any]]:
        rows = rows if rows is not None else self.current_match_rows()

        fixtures_by_teams: dict[
            tuple[str, str],
            list[Any],
        ] = {}
        for fixture in self.fixtures:
            if fixture.home_team_id and fixture.away_team_id:
                fixtures_by_teams.setdefault(
                    (fixture.home_team_id, fixture.away_team_id),
                    [],
                ).append(fixture)
        results = {}
        for row in rows:
            raw_match_id = row.get("canonical_match_id") or row.get("match_id") or row.get("id")
            direct_fixture = next(
                (
                    fixture
                    for fixture in self.fixtures
                    if str(raw_match_id) == fixture.id
                    or str(row.get("match_number") or row.get("number") or "")
                    == str(fixture.number)
                ),
                None,
            )
            played_at = row.get("match_date") or row.get("kickoff")
            if played_at is None and direct_fixture is None:
                continue
            if played_at is not None:
                try:
                    played_at = datetime.fromisoformat(
                        str(played_at).replace("Z", "+00:00")
                    )
                except ValueError:
                    continue
                if played_at.tzinfo is None:
                    played_at = played_at.replace(tzinfo=timezone.utc)
            home_id = self.team_alias_lookup.get(
                self._normalize_name(row.get("home_team_name"))
            )
            away_id = self.team_alias_lookup.get(
                self._normalize_name(row.get("away_team_name"))
            )
            fixture = direct_fixture
            if fixture is None:
                candidates = fixtures_by_teams.get((home_id, away_id), [])
                if not candidates:
                    continue
                fixture = min(
                    candidates,
                    key=lambda candidate: abs(
                        (candidate.kickoff - played_at).total_seconds()
                    ),
                )
                if abs((fixture.kickoff - played_at).total_seconds()) > 36 * 60 * 60:
                    continue
            home_score = row.get("home_score")
            away_score = row.get("away_score")
            status = str(row.get("status") or "").strip()
            if not status and row.get("completed"):
                status = "completed"
            results[fixture.id] = {
                "status": status or "scheduled",
                "home_score": (
                    int(home_score) if home_score is not None else None
                ),
                "away_score": (
                    int(away_score) if away_score is not None else None
                ),
                "home_team_id": home_id,
                "away_team_id": away_id,
                "winner_team_id": self.team_alias_lookup.get(
                    self._normalize_name(
                        row.get("winner_team_name") or row.get("winner")
                    )
                ),
            }
        return results

    @staticmethod
    def _stage_from_row(row: dict[str, Any]) -> str:
        raw = str(row.get("stage") or row.get("tournament_stage") or "").lower()
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
        return "group" if "group" in raw else str(row.get("stage") or "group")

    def _row_team_id(self, row: dict[str, Any], side: str) -> str | None:
        direct = row.get(f"{side}_team_id")
        if direct in self.teams_by_id:
            return str(direct)
        return self.team_alias_lookup.get(
            self._normalize_name(row.get(f"{side}_team_name") or row.get(f"{side}_team"))
        )

    def team_payload(self, team_id: str) -> dict:
        team = self.teams_by_id[team_id]
        return {
            **asdict(team),
            "elo": round(team.elo, 1),
            "flag": flag_for_team(team_id),
        }

    def _latest_database_prediction_run(
        self,
        force: bool = False,
    ) -> dict[str, Any] | None:
        if self.prediction_source is None:
            return None
        now = time.monotonic()
        if (
            not force
            and now - self._database_prediction_checked_at
            < self.prediction_cache_seconds
        ):
            return self._database_prediction_run
        self._database_prediction_checked_at = now
        try:
            run = self.prediction_source.load_latest()
        except Exception:
            self._database_prediction_run = None
            LOGGER.warning(
                "Latest database predictions are unavailable; serving static "
                "prediction fallback",
                exc_info=True,
            )
            return None
        if run is None or not run["predictions"]:
            self._database_prediction_run = None
            LOGGER.warning(
                "No database prediction run is available; serving static "
                "prediction fallback"
            )
            return None
        self._database_prediction_run = run
        LOGGER.info(
            "Serving latest database predictions: run=%s model=%s rows=%d",
            run["model_run_id"],
            run["model_version"],
            len(run["predictions"]),
        )
        return run

    def current_prediction_run(self, force: bool = False) -> dict[str, Any]:
        database_run = self._latest_database_prediction_run(force=force)
        if database_run is not None:
            return database_run
        return {
            "model_run_id": None,
            "model_version": STATIC_MODEL_VERSION,
            "generated_at": self.generated_at,
            "data_cutoff": self.data_cutoff,
            "predictions": self.static_predictions,
            "source": "fallback_static",
        }

    def prediction_payload(
        self,
        match_id: str,
        prediction_run: dict[str, Any] | None = None,
    ) -> dict | None:
        prediction_run = prediction_run or self.current_prediction_run()
        database_prediction = (
            prediction_run["predictions"].get(match_id)
            if prediction_run["model_run_id"] is not None
            else None
        )
        prediction = self.static_predictions.get(match_id)
        if not prediction and database_prediction is None:
            return None
        if prediction:
            payload = {
                **prediction_dict(prediction),
                "model_version": STATIC_MODEL_VERSION,
                "generated_at": self.generated_at,
                "data_cutoff": self.data_cutoff,
                "source": "fallback_static",
            }
        else:
            payload = {
                "match_id": match_id,
                "home_team_id": database_prediction.get("home_team_id", ""),
                "away_team_id": database_prediction.get("away_team_id", ""),
                "home_xg": float(database_prediction.get("home_xg") or 0),
                "away_xg": float(database_prediction.get("away_xg") or 0),
                "probabilities": {
                    "home_win": float(
                        database_prediction.get("home_win_probability")
                        or database_prediction.get("home_win")
                        or 0
                    ),
                    "draw": float(
                        database_prediction.get("draw_probability")
                        or database_prediction.get("draw")
                        or 0
                    ),
                    "away_win": float(
                        database_prediction.get("away_win_probability")
                        or database_prediction.get("away_win")
                        or 0
                    ),
                },
                "top_scores": [],
                "confidence": database_prediction.get("confidence_tier")
                or "High uncertainty",
                "key_factors": [],
                "context": {
                    "home_form_elo": 0,
                    "away_form_elo": 0,
                    "home_h2h_elo": 0,
                    "away_h2h_elo": 0,
                    "home_availability_elo": 0,
                    "away_availability_elo": 0,
                    "historical_matches_home": 0,
                    "historical_matches_away": 0,
                    "h2h_matches": 0,
                    "availability_reports": 0,
                    "data_cutoff": None,
                },
                "model_version": STATIC_MODEL_VERSION,
                "generated_at": self.generated_at,
                "data_cutoff": self.data_cutoff,
                "source": "database_latest",
            }
        if database_prediction is None:
            return payload

        home = float(
            database_prediction.get("final_home_probability")
            if database_prediction.get("final_home_probability") is not None
            else database_prediction["home_win_probability"]
        )
        draw = float(
            database_prediction.get("final_draw_probability")
            if database_prediction.get("final_draw_probability") is not None
            else database_prediction["draw_probability"]
        )
        away = float(
            database_prediction.get("final_away_probability")
            if database_prediction.get("final_away_probability") is not None
            else database_prediction["away_win_probability"]
        )
        payload.update(
            {
                "probabilities": {
                    "home_win": home,
                    "draw": draw,
                    "away_win": away,
                },
                "elo_base_home_probability": database_prediction.get(
                    "elo_base_home_probability"
                ),
                "elo_base_draw_probability": database_prediction.get(
                    "elo_base_draw_probability"
                ),
                "elo_base_away_probability": database_prediction.get(
                    "elo_base_away_probability"
                ),
                "attack_defense_adjustment": database_prediction.get(
                    "attack_defense_adjustment"
                ),
                "draw_calibration_adjustment": database_prediction.get(
                    "draw_calibration_adjustment"
                ),
                "context_adjustment_total": database_prediction.get(
                    "context_adjustment_total"
                ),
                "final_home_probability": home,
                "final_draw_probability": draw,
                "final_away_probability": away,
                "confidence_score": database_prediction.get("confidence_score"),
                "confidence_tier": database_prediction.get("confidence_tier"),
                "confidence_explanation": database_prediction.get(
                    "confidence_explanation"
                ),
                "confidence": database_prediction.get("confidence_tier")
                or payload["confidence"],
                "top_factors": _json_value(
                    database_prediction.get("top_factors"),
                    [],
                ),
                "model_version": database_prediction.get("model_version")
                or prediction_run["model_version"],
                "generated_at": str(prediction_run["generated_at"]),
                "data_cutoff": str(prediction_run["data_cutoff"]),
                "source": "database_latest",
            }
        )
        return payload

    def database_match_payloads(
        self,
        rows: list[dict[str, Any]] | None = None,
        prediction_run: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        rows = rows if rows is not None else self.current_match_rows()
        prediction_run = prediction_run or self.current_prediction_run()
        payloads = []
        seen_ids = {fixture.id for fixture in self.fixtures}
        for index, row in enumerate(rows, start=1):
            stage = self._stage_from_row(row)
            if stage == "group":
                continue
            home_id = self._row_team_id(row, "home")
            away_id = self._row_team_id(row, "away")
            if home_id not in self.teams_by_id or away_id not in self.teams_by_id:
                continue
            raw_id = (
                row.get("id")
                or row.get("api_football_fixture_id")
                or row.get("provider_fixture_id")
                or row.get("canonical_match_id")
                or row.get("match_id")
            )
            if raw_id is None:
                raw_id = f"provider-knockout-{index}"
            match_id = str(raw_id)
            if match_id in seen_ids:
                continue
            seen_ids.add(match_id)
            kickoff = row.get("kickoff") or row.get("match_date")
            if kickoff is None:
                continue
            try:
                kickoff_dt = datetime.fromisoformat(str(kickoff).replace("Z", "+00:00"))
            except ValueError:
                continue
            if kickoff_dt.tzinfo is None:
                kickoff_dt = kickoff_dt.replace(tzinfo=timezone.utc)
            status = str(row.get("status") or "").strip()
            if not status and row.get("completed"):
                status = "completed"
            number = row.get("match_number") or row.get("number")
            payloads.append(
                {
                    "id": match_id,
                    "number": int(number) if number is not None else 72 + index,
                    "stage": stage,
                    "kickoff": kickoff_dt.isoformat(),
                    "venue_id": row.get("venue_id") or "TBD",
                    "group": None,
                    "home_team": self.team_payload(home_id),
                    "away_team": self.team_payload(away_id),
                    "home_slot": None,
                    "away_slot": None,
                    "status": status or "scheduled",
                    "home_score": (
                        int(row["home_score"])
                        if row.get("home_score") is not None
                        else None
                    ),
                    "away_score": (
                        int(row["away_score"])
                        if row.get("away_score") is not None
                        else None
                    ),
                    "prediction": self.prediction_payload(match_id, prediction_run),
                }
            )
        return sorted(payloads, key=lambda match: (match["kickoff"], match["number"]))

    def match_payload(
        self,
        match_id: str,
        prediction_run: dict[str, Any] | None = None,
        match_results: dict[str, dict[str, Any]] | None = None,
    ) -> dict:
        match = next(match for match in self.fixtures if match.id == match_id)
        match_results = match_results or {}
        result = match_results.get(match.id, {})
        return {
            "id": match.id,
            "number": match.number,
            "stage": match.stage,
            "kickoff": match.kickoff.isoformat(),
            "venue_id": match.venue_id,
            "group": match.group,
            "home_team": self.team_payload(match.home_team_id),
            "away_team": self.team_payload(match.away_team_id),
            "home_slot": None,
            "away_slot": None,
            "status": result.get("status", "scheduled"),
            "home_score": result.get("home_score"),
            "away_score": result.get("away_score"),
            "prediction": self.prediction_payload(match.id, prediction_run),
        }

    def latest_predictions_payload(self, force: bool = False) -> dict[str, Any]:
        prediction_run = self.current_prediction_run(force=force)
        prediction_ids = (
            prediction_run["predictions"]
            if prediction_run["model_run_id"] is not None
            else self.static_predictions
        )
        return {
            "model_version": prediction_run["model_version"],
            "generated_at": str(prediction_run["generated_at"]),
            "data_cutoff": str(prediction_run["data_cutoff"]),
            "source": (
                "database_latest"
                if prediction_run["model_run_id"] is not None
                else "fallback_static"
            ),
            "predictions": [
                self.prediction_payload(match_id, prediction_run)
                for match_id in prediction_ids
            ],
        }

    def latest_simulation(self) -> dict:
        if self.simulation_source is not None:
            try:
                database_simulation = self.simulation_source.load_latest()
            except Exception:
                LOGGER.warning(
                    "Latest database simulation is unavailable; serving static "
                    "simulation fallback",
                    exc_info=True,
                )
            else:
                if database_simulation is not None:
                    run = database_simulation["run"]
                    model_inputs = database_simulation.get("model_inputs", {})
                    generated_at = run.get("generated_at") or run.get("created_at")
                    iterations = int(
                        run.get("num_simulations") or run.get("iterations") or 0
                    )
                    teams = []
                    for row in database_simulation["results"]:
                        team_id = str(row["team_id"])
                        team = self.teams_by_id.get(team_id)
                        if team is None:
                            LOGGER.warning(
                                "Skipping simulation result for unknown team %s",
                                team_id,
                            )
                            continue
                        teams.append(
                            {
                                "team_id": team_id,
                                "team_name": team.name,
                                "flag": flag_for_team(team_id),
                                "group": team.group,
                                "group_stage_exit": float(
                                    row.get("group_stage_exit_probability")
                                    or row.get("group_stage_exit")
                                    or 0
                                ),
                                "round_of_32": float(
                                    row.get("round_of_32_probability")
                                    or row.get("round_of_32")
                                    or 0
                                ),
                                "round_of_16": float(
                                    row.get("round_of_16_probability")
                                    or row.get("round_of_16")
                                    or 0
                                ),
                                "quarterfinal": float(
                                    row.get("quarterfinal_probability")
                                    or row.get("quarterfinal")
                                    or 0
                                ),
                                "semifinal": float(
                                    row.get("semifinal_probability")
                                    or row.get("semifinal")
                                    or 0
                                ),
                                "final": float(
                                    row.get("final_probability")
                                    or row.get("final")
                                    or 0
                                ),
                                "champion": float(
                                    row.get("champion_probability")
                                    or row.get("champion")
                                    or 0
                                ),
                                "model_inputs": model_inputs.get(team_id),
                            }
                        )
                    return {
                        "iterations": iterations,
                        "seed": int(run.get("random_seed") or run.get("seed") or 2026),
                        "model_version": run.get("model_version") or "unknown",
                        "generated_at": str(generated_at),
                        "created_at": str(run.get("created_at") or generated_at),
                        "data_cutoff": str(run.get("data_cutoff") or generated_at),
                        "source": "database_latest",
                        "monte_carlo_precision": {
                            "worst_case_standard_error": (
                                (0.25 / iterations) ** 0.5 if iterations else 0.0
                            ),
                            "worst_case_95_margin": (
                                1.96 * (0.25 / iterations) ** 0.5
                                if iterations
                                else 0.0
                            ),
                        },
                        "teams": teams,
                    }

        if self._simulation is None:
            snapshot_path = ROOT / "data" / "generated" / "latest.json"
            if snapshot_path.exists():
                snapshot = json.loads(snapshot_path.read_text())
                simulation = snapshot.get("simulation", {})
                if (
                    snapshot.get("model_version") == MODEL_VERSION
                    and simulation.get("iterations") == PUBLISHED_SIMULATION_ITERATIONS
                ):
                    self._simulation = simulation
                    self._simulation_generated_at = snapshot["generated_at"]
                    self._simulation_data_cutoff = snapshot["data_cutoff"]
            if self._simulation is None:
                self._simulation = simulate_tournament(
                    iterations=PUBLISHED_SIMULATION_ITERATIONS,
                    seed=2026,
                    context_repository=self.contexts,
                    cutoff=datetime.fromisoformat(self.data_cutoff),
                )
        return {
            **self._simulation,
            "model_version": MODEL_VERSION,
            "generated_at": self._simulation_generated_at,
            "created_at": self._simulation_generated_at,
            "data_cutoff": self._simulation_data_cutoff,
            "source": "fallback_static",
        }

    def team_profile_payload(self, team_id: str) -> dict:
        team = self.teams_by_id[team_id]
        team_names = {item.id: item.name for item in self.teams}
        cutoff = datetime.fromisoformat(self.data_cutoff)
        recent = recent_results(
            team_id,
            self.contexts.results,
            team_names,
            cutoff,
        )
        players = key_players(team_id, self.squad_players)
        probability = next(
            row
            for row in self.latest_simulation()["teams"]
            if row["team_id"] == team_id
        )
        matches = [
            self.match_payload(match.id, self.current_prediction_run())
            for match in self.fixtures
            if team_id in (match.home_team_id, match.away_team_id)
        ]
        group_path = []
        for match in matches:
            prediction = match["prediction"]
            if prediction is None:
                continue
            is_home = match["home_team"]["id"] == team_id
            opponent = match["away_team"] if is_home else match["home_team"]
            group_path.append(
                {
                    "match_id": match["id"],
                    "opponent_id": opponent["id"],
                    "opponent_name": opponent["name"],
                    "kickoff": match["kickoff"],
                    "team_win_probability": prediction["probabilities"][
                        "home_win" if is_home else "away_win"
                    ],
                    "draw_probability": prediction["probabilities"]["draw"],
                    "opponent_win_probability": prediction["probabilities"][
                        "away_win" if is_home else "home_win"
                    ],
                }
            )
        analysis = team_analysis(
            team,
            probability,
            recent,
            players,
            group_path,
            "experimental",
        )
        return {
            **self.team_payload(team_id),
            "matches": matches,
            "group_path": group_path,
            "tournament_probability": probability,
            "recent_results": recent,
            "form_summary": form_summary(recent),
            "key_players": player_payload(players),
            "analysis": analysis,
            "player_data_source": self.squad_metadata,
            "results_data_cutoff": self.data_cutoff,
        }


service = PredictionService()
